from __future__ import annotations

from unittest.mock import Mock, patch
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
import httpx

from src.lottery_actions import ActionResult, DynamicContext, execute_full_participation
from src.participation import participate_activity
from src.participation_guard import (
    account_write_lock, candidate_allowed, get_guard, record_failure, release_guard, set_guard,
)
from src.participation_log import participation_succeeded
from web.actions import run_action

TARGET = "100000000000000001"


def context():
    return DynamicContext(TARGET, 1, "https://example.org", TARGET, 17, False, False, True, False, False, False)


def execute(names=("like", "repost", "comment")):
    return execute_full_participation(Mock(), dynamic_id=TARGET, action_names=names,
        required_actions=names, journal=True, stop_on_failure=True, action_text="我的评论")


def test_timeout_checkpoints_success_and_blocks_repost_even_after_negative_readback():
    with patch("src.lottery_actions.build_dynamic_context", side_effect=lambda *a, **k: context()), \
         patch("src.lottery_actions.require_login", return_value=("csrf", 1)), \
         patch("src.lottery_actions.time.sleep"), \
         patch("src.lottery_actions.like_dynamic", return_value=ActionResult("like", True)) as like, \
         patch("src.lottery_actions.repost_dynamic", side_effect=RuntimeError("网络超时")) as repost, \
         patch("src.lottery_actions.comment_dynamic") as comment:
        actions, _ = execute()
        assert get_guard(TARGET)["progress"]["like"]["state"] == "succeeded"
        assert get_guard(TARGET)["progress"]["repost"]["state"] == "unknown"
        assert not participation_succeeded(actions, lottery_type="转发抽奖")
        assert not candidate_allowed(TARGET)
        with pytest.raises(ValueError, match="结果未知"):
            release_guard(TARGET, evidence="已检查")
        # Direct action entry also refuses to repeat a write, even if the bounded
        # readback reports False. A persisted success is likewise not resent.
        with pytest.raises(RuntimeError, match="已隔离"):
            execute()
        assert like.call_count == 1
        assert repost.call_count == 1
        comment.assert_not_called()


def test_manual_negative_verification_retries_only_unfinished_actions():
    set_guard(TARGET, state="review", action="like", progress={"state": "succeeded"})
    set_guard(TARGET, action="repost", progress={"state": "unknown"})
    release_guard(TARGET, evidence="在个人动态中核查，确认未转发", resolved_actions={"repost": "failed"})
    with patch("src.lottery_actions.build_dynamic_context", return_value=context()), \
         patch("src.lottery_actions.require_login", return_value=("csrf", 1)), \
         patch("src.lottery_actions.time.sleep"), \
         patch("src.lottery_actions.like_dynamic") as like, \
         patch("src.lottery_actions.repost_dynamic", return_value=ActionResult("repost", True)) as repost:
        actions, _ = execute(("like", "repost"))
    like.assert_not_called()
    repost.assert_called_once()
    assert participation_succeeded(actions, lottery_type="转发抽奖")


def test_crash_in_flight_requires_verification_and_survives_engine_reset():
    set_guard(TARGET, state="review", action="comment", progress={"state": "in_flight"})
    from src.db.engine import reset_engine_for_tests
    reset_engine_for_tests()
    assert get_guard(TARGET)["progress"]["comment"]["state"] == "in_flight"
    with pytest.raises(ValueError):
        release_guard(TARGET, evidence="重新启动")
    release_guard(TARGET, evidence="已找到本人评论", resolved_actions={"comment": "succeeded"})
    assert get_guard(TARGET)["progress"]["comment"]["state"] == "succeeded"


@pytest.mark.parametrize("error", [RuntimeError("code=-101 未登录"), RuntimeError("code=-352"),
    httpx.HTTPStatusError("412", request=httpx.Request("GET", "https://example.org"),
                         response=httpx.Response(412))])
def test_account_errors_pause_every_candidate(error):
    record_failure(TARGET, error)
    assert get_guard("")["state"] == "account_paused"
    assert not candidate_allowed("100000000000000002")
    release_guard("", evidence="账号验证已完成")
    assert candidate_allowed("100000000000000002")
    assert not candidate_allowed(TARGET)


def test_account_failure_stops_batch_without_setting_cancel_event():
    event = threading.Event()
    targets = [{"dynamic_id": f"10000000000000000{i}", "lottery_type": "互动抽奖"} for i in (1, 2, 3)]
    with patch("web.actions.pick_triple_participate_targets", return_value=targets), \
         patch("web.actions.ensure_activity_participatable", side_effect=RuntimeError("code=-352 风控")) as check, \
         patch("web.actions.BilibiliClient"), patch("web.actions.refresh_local_activity_statuses"), \
         patch("web.actions.invalidate_activity_cache"):
        result = run_action("participate_triple", {}, cancel_event=event)
    assert result["result"]["status"] == "account_paused"
    assert check.call_count == 1
    assert not event.is_set()


def test_swallowed_read_account_error_prevents_following_writes():
    from src.participation_guard import observe_account_io
    @observe_account_io
    def request_json():
        return {"code": -352, "message": "风控"}
    attempted = Mock()
    @observe_account_io
    def post_form():
        attempted()
        return {"code": 0}
    with account_write_lock():
        try:
            request_json()
        except RuntimeError:
            pass
        assert get_guard("")["state"] == "account_paused"
        with pytest.raises(RuntimeError, match="账号队列已暂停"):
            post_form()
    attempted.assert_not_called()


def test_cancel_keeps_prior_success_and_original_reason():
    def step(index, *_):
        if index == 2:
            raise ValueError("任务已取消")
    with patch("src.participation.fetch_dynamic_detail", return_value={}), \
         patch("src.participation.fetch_notice_for_interact", return_value=({"status": 0}, None, None)), \
         patch("src.lottery_actions.build_dynamic_context", return_value=context()), \
         patch("src.lottery_actions.require_login", return_value=("csrf", 1)), \
         patch("src.lottery_actions.time.sleep"), \
         patch("src.lottery_actions.like_dynamic", return_value=ActionResult("like", True)):
        result = participate_activity(Mock(), dynamic_id=TARGET, lottery_type="互动抽奖", action_text="本人内容", on_step=step)
    assert result.status == "cancelled"
    assert result.message == "任务已取消"
    assert result.actions[0].ok
    assert get_guard(TARGET)["progress"]["like"]["state"] == "succeeded"


def test_optional_favorite_does_not_change_completion():
    actions = [ActionResult("repost", True, required=True), ActionResult("favorite", False, required=False)]
    assert participation_succeeded(actions, lottery_type="转发抽奖")


def test_legacy_failure_inherits_success_before_retry():
    from src.participation_log import ParticipationActionRecord, append_action_record
    append_action_record(ParticipationActionRecord(int(time.time()), TARGET, "转发抽奖", "failed",
        "评论网络超时", "历史本人评论", [{"action": "repost", "ok": True},
        {"action": "comment", "ok": False, "detail": "网络超时"}], {}))
    assert not candidate_allowed(TARGET)
    guard = get_guard(TARGET)
    assert guard["progress"]["repost"]["state"] == "succeeded"
    release_guard(TARGET, evidence="核查未发现评论", resolved_actions={"comment": "failed"})
    guard = get_guard(TARGET)
    assert guard["progress"]["repost"]["state"] == "succeeded"
    assert guard["progress"]["comment"]["text"] == "历史本人评论"


def test_release_cannot_overwrite_confirmed_success():
    set_guard(TARGET, state="review", action="repost", progress={"state": "succeeded"})
    with pytest.raises(ValueError):
        release_guard(TARGET, evidence="更改", resolved_actions={"repost": "failed"})
    assert get_guard(TARGET)["progress"]["repost"]["state"] == "succeeded"


def test_guard_api_excludes_and_requires_verification():
    from web.app import api_exclude_participation, api_release_participation
    from web.api_errors import AppError
    api_exclude_participation(TARGET, {"reason": "资格不满足"})
    assert not candidate_allowed(TARGET)
    with pytest.raises(AppError):
        api_release_participation(TARGET, {})
    api_release_participation(TARGET, {"evidence": "资格已满足，核查原文"})
    assert candidate_allowed(TARGET)


def test_database_engine_rejects_path_outside_test_root(monkeypatch, tmp_path):
    from src.db.engine import get_engine
    outside = tmp_path.parent / "outside-test-root" / "binggo.db"
    monkeypatch.setattr("src.db.engine.db_path", lambda: outside)
    with pytest.raises(RuntimeError, match="隔离目录"):
        get_engine()
    assert not outside.exists()


def test_account_lock_serializes_processes(isolated_home):
    code = '''
from src.participation_guard import account_write_lock
with account_write_lock():
    print("locked", flush=True)
    input()
'''
    env = dict(os.environ, BINGGO_HOME=str(isolated_home))
    child = subprocess.Popen([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                             env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    acquired = threading.Event()
    worker = None
    try:
        assert child.stdout.readline().strip() == "locked"
        def acquire():
            with account_write_lock():
                acquired.set()
        worker = threading.Thread(target=acquire)
        worker.start()
        assert not acquired.wait(0.3)
        child.communicate("\n", timeout=5)
        assert acquired.wait(5)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        if worker:
            worker.join(5)
