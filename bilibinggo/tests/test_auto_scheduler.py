from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from datetime import timedelta, timezone

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.auto_config import ALLOWED_CLICK_ACTIONS
from web.auto_config import REFRESH_HOURS
from web.auto_scheduler import AutoScheduler, CollisionError, _is_hard_failure, _next_slot, _probe_job
from web.job_runner import JobRunner, JobStatus


def _bind_resolve(runner: MagicMock, status_factory) -> None:
    """让 resolve_job_status 与 get_status 行为一致，供调度等待循环使用。"""

    def resolve(_job_id: int) -> JobStatus:
        value = status_factory()
        return value if isinstance(value, JobStatus) else JobStatus(**value)

    runner.resolve_job_status.side_effect = resolve


def test_allowed_actions_are_exactly_four() -> None:
    assert ALLOWED_CLICK_ACTIONS == frozenset(
        {
            "refresh_all",
            "refresh_watch",
            "refresh_status",
            "participate_triple",
        }
    )


def test_click_rejects_forbidden_action() -> None:
    scheduler = AutoScheduler(job_runner=JobRunner())
    with pytest.raises(ValueError, match="禁止的操作"):
        scheduler._click_and_wait("cancel")


def test_collision_when_runner_already_running() -> None:
    runner = MagicMock()
    runner.is_running.return_value = True
    runner.get_status.return_value.to_dict.return_value = {
        "state": "running",
        "action": "participate_triple",
        "message": "进行中",
    }
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(CollisionError, match="仍有任务在运行"):
        scheduler._click_and_wait("refresh_status")
    runner.try_start.assert_not_called()
    runner.cancel.assert_not_called()


def test_collision_when_try_start_returns_none() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = None
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(CollisionError, match="已有任务正在运行"):
        scheduler._click_and_wait("refresh_all")
    runner.cancel.assert_not_called()


def test_wait_until_terminal_never_calls_cancel() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 42
    poll_count = {"n": 0}

    def status_factory() -> JobStatus:
        poll_count["n"] += 1
        if poll_count["n"] < 2:
            return JobStatus(id=42, state="running", action="refresh_status", message="working")
        return JobStatus(id=42, state="success", action="refresh_status", message="done")

    _bind_resolve(runner, status_factory)
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._click_and_wait("refresh_status")
    runner.cancel.assert_not_called()
    runner.try_start.assert_called_once_with("refresh_status", {}, source="auto")


def test_collision_in_refresh_batch_propagates() -> None:
    runner = MagicMock()
    runner.is_running.return_value = True
    runner.get_status.return_value.to_dict.return_value = {
        "state": "running",
        "action": "refresh_all",
        "message": "busy",
    }
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._account_paused = lambda: False
    with pytest.raises(CollisionError):
        scheduler._run_refresh_batch("2026-07-17-3")
    runner.cancel.assert_not_called()


def test_probe_only_reads_runner_status() -> None:
    runner = MagicMock()
    status = MagicMock()
    status.to_dict.return_value = {
        "id": None,
        "state": "idle",
        "action": "",
        "message": "",
        "label": "",
    }
    runner.get_status.return_value = status
    result = _probe_job(runner)
    assert result["reachable"] is True
    assert result["job_state"] == "idle"
    assert result["job_id"] is None
    runner.get_status.assert_called()
    runner.try_start.assert_not_called()
    runner.cancel.assert_not_called()


def test_soft_failure_for_empty_triple() -> None:
    assert _is_hard_failure(RuntimeError("当前列表没有可参与的未参加活动")) is False
    assert _is_hard_failure(RuntimeError("当前没有可参与的未参加活动，已跳过")) is False
    assert _is_hard_failure(CollisionError("撞车")) is True
    assert _is_hard_failure(RuntimeError("请先扫码登录")) is True


def test_click_and_wait_treats_skipped_triple_as_success() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 7
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=7,
            state="success",
            action="participate_triple",
            message="当前没有可参与的未参加活动，已跳过",
            result={"skipped": True, "items": []},
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    outcome = scheduler._click_and_wait("participate_triple")
    assert outcome["skipped"] is True
    runner.try_start.assert_called_once_with("participate_triple", {"from_auto": True}, source="auto")
    runner.cancel.assert_not_called()


def test_click_and_wait_treats_legacy_empty_triple_error_as_skip() -> None:
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 8
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=8,
            state="error",
            action="participate_triple",
            message="当前列表没有可参与的未参加活动",
            result=None,
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    outcome = scheduler._click_and_wait("participate_triple")
    assert outcome["skipped"] is True
    runner.cancel.assert_not_called()


def test_click_and_wait_cancelled_raises(isolated_home: Path) -> None:
    _ = isolated_home
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 9
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=9,
            state="cancelled",
            action="refresh_status",
            message="任务已取消",
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(RuntimeError, match="已取消"):
        scheduler._click_and_wait("refresh_status")
    runner.cancel.assert_not_called()


def test_click_and_wait_interrupted_raises(isolated_home: Path) -> None:
    _ = isolated_home
    runner = MagicMock()
    runner.is_running.return_value = False
    runner.try_start.return_value = 10
    _bind_resolve(
        runner,
        lambda: JobStatus(
            id=10,
            state="interrupted",
            action="refresh_status",
            message="进程退出，任务中断",
        ),
    )
    scheduler = AutoScheduler(job_runner=runner)
    with pytest.raises(RuntimeError, match="中断"):
        scheduler._click_and_wait("refresh_status")
    runner.cancel.assert_not_called()


def test_next_slot_before_refresh() -> None:
    now = datetime(2026, 7, 17, 8, 56, tzinfo=timezone(timedelta(hours=8)))
    slot = _next_slot(now)
    assert slot["kind"] == "refresh"
    assert slot["hour"] == 9
    assert slot["action"] == "refresh_all"
    assert slot["action_label"] == "一键更新活动链接"
    assert slot["at_unix"]


def test_next_slot_triple_minute() -> None:
    now = datetime(2026, 7, 17, 2, 50, tzinfo=timezone(timedelta(hours=8)))
    slot = _next_slot(now)
    assert slot["kind"] == "triple"
    assert slot["minute"] == 0
    assert slot["action"] == "participate_triple"
    assert slot["action_label"] == "三连参与"


def test_triple_cadence_is_fifteen_minutes() -> None:
    now = datetime(2026, 7, 17, 2, 1, tzinfo=timezone(timedelta(hours=8)))
    first = _next_slot(now)
    second = _next_slot(datetime.fromtimestamp(first["at_unix"], tz=timezone(timedelta(hours=8))))
    assert first["minute"] == 15
    assert second["minute"] == 30
    assert second["hour"] == 2


def test_refresh_hour_still_has_its_quarter_hour_triple() -> None:
    """9:00 那一分钟让给刷新批次，但 9:15 必须照常三连 —— 三连是严格每 15 分钟。"""
    slot = _next_slot(datetime(2026, 7, 17, 9, 5, tzinfo=CN))
    assert slot["kind"] == "triple"
    assert (slot["hour"], slot["minute"]) == (9, 15)


def test_fifteen_minute_cadence_holds_across_the_refresh_hour() -> None:
    """08:46 → 09:00（批次）→ 09:15（三连）→ 09:30，间隔始终是 15 分钟。"""
    marks = []
    now = datetime(2026, 7, 17, 8, 46, tzinfo=CN)
    for _ in range(3):
        slot = _next_slot(now)
        marks.append((slot["kind"], slot["hour"], slot["minute"]))
        now = datetime.fromtimestamp(slot["at_unix"], tz=CN) + timedelta(minutes=1)
    assert marks == [("refresh", 9, 0), ("triple", 9, 15), ("triple", 9, 30)]


CN = timezone(timedelta(hours=8))


def test_refresh_due_is_once_per_window_with_catchup() -> None:
    """每个刷新窗口只跑一次；没跑完的下一窗口照常来（窗口点由排期算，改节奏不用改测试）。"""
    scheduler = AutoScheduler(job_runner=MagicMock())
    day = datetime(2026, 7, 17, tzinfo=CN)
    hours = sorted(REFRESH_HOURS)
    for hour in hours:
        at = day.replace(hour=hour)
        before = at - timedelta(minutes=1)
        if before.hour not in hours:            # 当天第一个窗口之前不该有得跑
            assert scheduler._refresh_due(before) is False
        assert scheduler._refresh_due(at) is True
        scheduler._done_refresh.add(f"{at:%Y-%m-%d-%H}")
    last = day.replace(hour=hours[-1])
    assert scheduler._refresh_due(last + timedelta(hours=1)) is False
    assert scheduler._refresh_due(day.replace(hour=hours[0]) + timedelta(days=1)) is True


def test_missed_daily_refresh_shows_pending_slot() -> None:
    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._status.state = "running"
    now = datetime(2026, 7, 17, 15, 20, tzinfo=CN)
    slot = _next_slot(now, refresh_at=scheduler._refresh_next(now))
    assert slot["kind"] == "refresh"
    assert "补跑" in slot["hint"]
    first = datetime(2026, 7, 17, min(REFRESH_HOURS), tzinfo=CN)
    assert scheduler._refresh_pending(first - timedelta(minutes=1)) == first


def test_retry_gap_does_not_hide_the_imminent_triple() -> None:
    """欠一轮源检查但重试还在 1 小时后 → 面板必须先显示 10 分钟后的三连，不许谎报。"""
    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._status.state = "running"
    now = datetime(2026, 7, 17, 10, 50, tzinfo=CN)
    scheduler._refresh_last_try["2026-07-17-09"] = datetime(2026, 7, 17, 10, 30, tzinfo=CN)
    slot = _next_slot(now, refresh_at=scheduler._refresh_pending(now))
    assert (slot["kind"], slot["hour"], slot["minute"]) == ("triple", 11, 0)
    assert scheduler._refresh_due(now) is False
    assert scheduler._refresh_due(datetime(2026, 7, 17, 11, 30, tzinfo=CN)) is True


def _batch_runner(step_errors: dict[str, str] | None = None):
    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._account_paused = lambda: False
    clicked: list[str] = []

    def fake_click(action: str, *, pipeline_index: int | None = None) -> dict:
        clicked.append(action)
        if step_errors and action in step_errors:
            raise RuntimeError(step_errors[action])
        return {"skipped": False}

    scheduler._click_and_wait = fake_click
    return scheduler, clicked


def test_refresh_batch_reports_success_only_when_every_step_ok() -> None:
    scheduler, clicked = _batch_runner()
    assert scheduler._run_refresh_batch("2026-07-17-9") is True
    assert clicked == ["refresh_all", "refresh_watch", "refresh_status"]


def test_refresh_batch_reports_failure_when_one_step_errors() -> None:
    """失败的一轮不能算「今天刷过了」，否则账号掉了之后一整天静默不查源。"""
    scheduler, clicked = _batch_runner({"refresh_all": "请求过于频繁，请稍后重试"})
    assert scheduler._run_refresh_batch("2026-07-17-9") is False
    assert clicked == ["refresh_all", "refresh_watch", "refresh_status"]


def test_refresh_batch_skips_and_costs_nothing_when_account_paused() -> None:
    scheduler, clicked = _batch_runner()
    scheduler._account_paused = lambda: True
    assert scheduler._run_refresh_batch("2026-07-17-9") is False
    assert clicked == []


def test_failed_refresh_batch_retries_after_one_hour() -> None:
    scheduler, _ = _batch_runner({"refresh_all": "请求过于频繁，请稍后重试"})
    now = datetime(2026, 7, 17, 9, 0, tzinfo=CN)
    key = "2026-07-17-09"
    scheduler._refresh_last_try[key] = now
    assert scheduler._run_refresh_batch(key) is False
    assert scheduler._refresh_due(now + timedelta(minutes=30)) is False
    assert scheduler._refresh_due(now + timedelta(hours=1)) is True
    scheduler._status.state = "running"
    assert scheduler._refresh_pending(now + timedelta(minutes=30)) == now + timedelta(hours=1)
    scheduler._done_refresh.add(key)
    later = now + timedelta(hours=2)
    next_window = min(h for h in REFRESH_HOURS if now.replace(hour=h) > later)
    assert scheduler._refresh_pending(later) == now.replace(hour=next_window)
