from __future__ import annotations
from types import SimpleNamespace
import os
import shutil
import subprocess
import tempfile
import sys
import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest
from sqlmodel import select

from src.activity_store import known_activity_ids
from src.db.models import RefreshWorkRow
from src.db.session import session_scope
from src.pipeline import checkpoint_store as cp
from src.pipeline.classify_step import ClassifyOutcome
from src.pipeline import refresh_all_pipeline as pipe

IDS = [str(1224962472871460885 + i) for i in range(5)]


class DummyClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def offline_pipeline(monkeypatch):
    calls = []
    monkeypatch.setattr(pipe, "BilibiliClient", DummyClient)
    def classify(client, did):
        calls.append(did)
        return ClassifyOutcome(did, "转发抽奖", False, classify_content="有原文依据的抽奖活动")
    monkeypatch.setattr(pipe, "classify_new_link", classify)
    def enrich(client, **kwargs):
        did = kwargs["dynamic_id"]
        return SimpleNamespace(skipped=False, to_dict=lambda: {
            "dynamic_id": did, "lottery_type": "转发抽奖", "draw_status": "unknown",
            "conditions": {"time_extraction_checked": True}, "lottery_time": None,
        })
    monkeypatch.setattr(pipe, "enrich_activity", enrich)
    return calls


def test_cancel_drains_inflight_and_resumes_without_reclassification(offline_pipeline):
    def progress(done, total, message):
        if message.startswith("详情进度"):
            raise ValueError("任务已取消")
    with pytest.raises(ValueError, match="取消"):
        pipe.run_new_links_pipeline(IDS[:3], batch_size=2, workers=2, on_progress=progress)
    assert known_activity_ids() == set(IDS[:2])
    assert cp.pending_ids() == [IDS[2]]
    result = pipe.run_refresh_all_pipeline([SimpleNamespace(updated=False, activity_links=[])])
    assert result.persisted_count == 1
    assert offline_pipeline == IDS[:3]
    assert cp.preview() == []


def test_classification_checkpoint_survives_progress_failure(offline_pipeline):
    def progress(done, total, message):
        if message == "分类进度":
            raise ValueError("任务已取消")
    with pytest.raises(ValueError):
        pipe.run_new_links_pipeline(IDS[:1], on_progress=progress)
    assert cp.cached_outcome(IDS[0]).classify_content == "有原文依据的抽奖活动"
    assert cp.preview()[0]["state"] == "classified"
    assert pipe.run_new_links_pipeline([]).persisted_count == 1
    assert offline_pipeline == IDS[:1]


def test_cooldown_limit_and_manual_release(offline_pipeline, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(cp.time, "time", lambda: clock[0])
    monkeypatch.setattr(pipe, "enrich_activity", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("详情暂不可访问")))
    pipe.run_new_links_pipeline(IDS[:1])
    assert cp.preview()[0]["retry_at"] == 1300
    assert pipe.run_new_links_pipeline(IDS[:1]).pipeline_skipped
    for now in (1300, 1900):
        clock[0] = now
        pipe.run_new_links_pipeline([])
    assert cp.preview()[0]["state"] == "review"
    assert cp.preview()[0]["attempts"] == 3
    assert offline_pipeline == IDS[:1]  # Details retries reuse classification.
    with pytest.raises(ValueError, match="依据"):
        cp.release(IDS[0], "")
    cp.release(IDS[0], "已核查原文，接口已恢复")
    assert cp.pending_ids() == IDS[:1]


def test_account_failure_pauses_whole_read_queue(offline_pipeline, monkeypatch):
    seen = []
    def classify(client, did):
        seen.append(did)
        request = httpx.Request("GET", "https://api.bilibili.com/detail")
        raise httpx.HTTPStatusError("restricted", request=request, response=httpx.Response(412, request=request))
    monkeypatch.setattr(pipe, "classify_new_link", classify)
    with pytest.raises(RuntimeError, match="账号异常"):
        pipe.run_new_links_pipeline(IDS[:3])
    assert seen == IDS[:1]
    assert len(cp.preview()) == 3
    from src.participation_guard import get_guard
    assert get_guard("")["state"] == "account_paused"
    with pytest.raises(RuntimeError, match="账号队列已暂停"):
        pipe.run_new_links_pipeline([])
    assert seen == IDS[:1]


def test_provider_failure_does_not_pause_bilibili_account(offline_pipeline, monkeypatch):
    original = pipe.classify_new_link
    def classify(client, did):
        if did == IDS[0]:
            raise RuntimeError("LLM 服务暂时不可用（HTTP 429）")
        return original(client, did)
    monkeypatch.setattr(pipe, "classify_new_link", classify)
    assert pipe.run_new_links_pipeline(IDS[:2]).persisted_count == 1
    from src.participation_guard import get_guard
    assert get_guard("")["state"] == "ready"


def test_old_checkpoint_refreshes_original_text_and_platform_state(offline_pipeline, monkeypatch):
    cp.enqueue(IDS[:1])
    cp.classified(ClassifyOutcome(IDS[0], "转发抽奖", False, classify_content="旧正文",
                                 detail_item={"old": True}, lottery_notice={"status": "active"}))
    with session_scope() as session:
        row = session.get(RefreshWorkRow, IDS[0])
        import json
        payload = json.loads(row.classify_json)
        saved = payload["_saved_at"]
    monkeypatch.setattr(cp.time, "time", lambda: saved + 301)
    outcome = cp.cached_outcome(IDS[0])
    assert outcome.lottery_type == "转发抽奖"
    assert outcome.classify_content == ""
    assert outcome.detail_item is None
    assert outcome.lottery_notice is None


def test_protected_reads_pause_at_first_412_without_retry(monkeypatch):
    from src.bilibili_client import BilibiliClient
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(412, request=request)
    client = BilibiliClient(warmup=False)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client._pause_on_account_error = True
    monkeypatch.setattr("src.bilibili_client.acquire_bilibili_request_slot", lambda: None)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_json("https://api.bilibili.com/test", retries=3)
        with pytest.raises(RuntimeError, match="账号队列已暂停"):
            client.get_json("https://api.bilibili.com/another", retries=3)
        assert len(calls) == 1
    finally:
        client.close()


def test_limit_registers_unprocessed_links_before_source_checkpoint(offline_pipeline):
    assert pipe.run_new_links_pipeline(IDS[:3], limit=1).persisted_count == 1
    assert set(cp.pending_ids()) == set(IDS[1:3])
    assert pipe.run_new_links_pipeline([], batch_size=1).persisted_count == 2


def test_empty_monitor_skips_without_advancing_window(monkeypatch):
    from src.state_store import get_watch_last_synced_at, set_watch_last_synced_at
    from web.actions import run_action
    monkeypatch.setattr("src.watch_sync.list_watch_users", lambda: [])
    set_watch_last_synced_at(123)
    result = run_action("refresh_watch")
    assert result["result"]["skipped"]
    assert get_watch_last_synced_at() == 123


def test_empty_monitor_api_does_not_require_credentials(monkeypatch):
    from fastapi.testclient import TestClient
    from web.app import app
    monkeypatch.setattr("web.app.get_account_profile", lambda: {"logged_in": False})
    monkeypatch.setattr("src.watch_users.list_watch_users", lambda: [])
    monkeypatch.setattr("web.app.require_llm_ready", lambda: pytest.fail("empty watch needs no LLM"))
    monkeypatch.setattr("web.app.runner.try_start", lambda *a, **k: 1)
    assert TestClient(app).post("/api/jobs", json={"action": "refresh_watch"}).status_code == 200


def test_checkpoint_api_requires_evidence_and_observes_task_lock():
    from fastapi.testclient import TestClient
    from web.app import app
    from src.task_lock import execution_lock
    cp.enqueue(IDS[:1])
    cp.failed(IDS[0], RuntimeError("详情不可访问"))
    client = TestClient(app)
    url = f"/api/refresh/checkpoints/{IDS[0]}/release"
    assert client.post(url, json={"evidence": ""}).status_code == 400
    with execution_lock():
        assert client.post(url, json={"evidence": "核查完成"}).status_code == 409
    assert client.post(url, json={"evidence": "已核查，页面恢复"}).status_code == 200
    assert cp.pending_ids() == IDS[:1]


def test_task_lock_excludes_other_processes_and_releases_on_exit():
    from src.task_lock import execution_lock
    code = "from src.task_lock import claim; x=claim(); print('busy' if x is None else 'free'); x and x.close()"
    def probe():
        return subprocess.check_output([sys.executable, "-c", code], env=os.environ.copy(), text=True).strip()
    with execution_lock():
        assert probe() == "busy"
    assert probe() == "free"


def test_second_runner_cannot_interrupt_live_job(monkeypatch):
    from web.job_runner import JobRunner
    from src.job_store import get_job
    entered, release = threading.Event(), threading.Event()
    def action(*a, **k):
        entered.set()
        release.wait(5)
        return {"ok": True, "message": "done", "result": {}}
    monkeypatch.setattr("web.job_runner.run_action", action)
    first, second = JobRunner(), JobRunner()
    job = first.try_start("refresh_links")
    try:
        assert entered.wait(2)
        second.recover_on_startup()
        assert get_job(job)["state"] == "running"
        assert second.try_start("refresh_links") is None
    finally:
        release.set()
        first.wait_until_terminal(job, timeout_sec=5, poll_interval_sec=.05, initial_delay_sec=0)
        # Terminal status is published immediately before the lease is released.
        from src.task_lock import claim
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            lease = claim()
            if lease:
                lease.close()
                break
            time.sleep(.01)


def test_scheduler_restart_cannot_revive_retiring_thread(monkeypatch):
    from web.auto_scheduler import AutoScheduler
    scheduler = AutoScheduler(job_runner=MagicMock())
    entered, retire = threading.Event(), threading.Event()
    def slow_loop():
        entered.set()
        retire.wait(5)
    monkeypatch.setattr(scheduler, "_loop", slow_loop)
    scheduler.start()
    assert entered.wait(2)
    old_event = scheduler._stop_event
    scheduler.stop()
    assert old_event.is_set()
    try:
        with pytest.raises(RuntimeError, match="正在停止"):
            scheduler.start()
        assert scheduler._stop_event is old_event
    finally:
        retire.set()
        scheduler._thread.join(2)
    monkeypatch.setattr(scheduler, "_loop", lambda: scheduler._stop_event.wait(5))
    scheduler.start()
    assert scheduler._stop_event is not old_event
    assert old_event.is_set()
    scheduler.stop()


def test_shared_rate_limit_between_processes(monkeypatch):
    # Absolute request timestamps, separate interpreters, same data directory.
    monkeypatch.setenv("BILI_RPS", "2")   # 目标间隔 0.5s：全量跑时 CPU 忙，0.2s 的量级会被时钟粒度吃掉（10-27 复现）
    code = "from src.bilibili_rate_limit import acquire_bilibili_request_slot; import time; acquire_bilibili_request_slot(); print(time.time())"
    # 锁文件目录必须自己钉死：整套测试跑时前序用例可能改过 BINGGO_HOME，
    # 那样子进程会把锁写进 pytest 的 tmp_path、被测试隔离守卫判成越界（10-26/10-27 各误报一次）。
    home = tempfile.mkdtemp(prefix="binggo-ratelimit-")
    env = dict(os.environ, BINGGO_HOME=home, BINGGO_TEST_DATA_ROOT=home)
    processes = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env) for _ in range(3)]
    values = []
    for process in processes:
        out, err = process.communicate(timeout=10)
        assert process.returncode == 0, err
        values.append(float(out.strip()))
    values.sort()
    shutil.rmtree(home, ignore_errors=True)
    # rps=2 → 期望间隔 0.5s。限流真失效时三个进程会挤在同一瞬间发（间隔≈0），
    # 0.2 的门槛离 0 远、离 0.5 也远，既能抓到回归又不会被调度噪声误报。
    gaps = [b - a for a, b in zip(values, values[1:])]
    assert min(gaps) >= 0.2


def test_llm_pool_limits_retry_and_distinguishes_read_timeout(monkeypatch):
    from src import llm_client as llm
    from src.llm_config import LlmConfig
    llm.close_llm_clients()
    clients, payloads, calls = [], [], []
    mode = ["ok"]
    def handler(request):
        import json
        calls.append(request)
        payloads.append(json.loads(request.content))
        if mode[0] == "read":
            raise httpx.ReadTimeout("slow", request=request)
        if mode[0] == "connect":
            raise httpx.ConnectTimeout("offline", request=request)
        return httpx.Response(200, json={"choices": [{"finish_reason": mode[0],
                               "message": {"content": '{"ok":true}'}}]})
    real_client = httpx.Client
    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(llm.httpx, "Client", factory)
    monkeypatch.setattr(llm.time, "sleep", lambda delay: None)
    cfg = LlmConfig("key-one", "https://provider.invalid/v1", "same-model")
    try:
        llm.chat_json(system="s", user="u", config=cfg)
        llm.chat_json(system="s", user="u", config=LlmConfig("key-two", cfg.base_url, cfg.model_name))
        assert len(clients) == 1
        assert [r.headers["Authorization"] for r in calls] == ["Bearer key-one", "Bearer key-two"]
        assert payloads[0]["max_tokens"] == 2048
        mode[0] = "read"
        with pytest.raises(RuntimeError, match="模型响应超时"):
            llm.chat_json(system="s", user="u", config=cfg)
        assert len(calls) == 3
        mode[0] = "connect"
        with pytest.raises(RuntimeError, match="建立连接超时"):
            llm.chat_json(system="s", user="u", config=cfg)
        assert len(calls) == 5
        mode[0] = "length"
        with pytest.raises(RuntimeError, match="长度上限"):
            llm.chat_json(system="s", user="u", config=cfg)
        assert len(calls) == 6
    finally:
        llm.close_llm_clients()
    assert clients[0].is_closed
