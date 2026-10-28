from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlmodel import select

from src.db.models import JobRow
from src.db.session import session_scope
from web.auto_config import REFRESH_HOURS
from web.auto_scheduler import RISK_REFRESH_BACKOFF, AutoScheduler, recent_risk_refresh_failure

CN_TZ = timezone(timedelta(hours=8))
RISK_MESSAGE = (
    "B 站拒绝了这个请求（多为平台校验或风控），不等于请求过快；请先停止批量更新，"
    "稍后逐个数据源重试，必要时重新扫码登录 — Client error '412 Precondition Failed' for url "
    "'https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id=1253379874850078729'"
)
ORDINARY_MESSAGE = "RuntimeError: 详情接口返回 code=-404 动态不存在"
NOW = datetime(2026, 10, 26, 15, 30, tzinfo=CN_TZ)


def slot_key(moment: datetime) -> str:
    """按实际排期算某个时刻属于哪个刷新窗口（窗口点改了测试也不用跟着改）。"""
    slots = sorted(moment.replace(hour=h, minute=0, second=0, microsecond=0) for h in REFRESH_HOURS)
    active = [s for s in slots if s <= moment]
    return f"{active[-1]:%Y-%m-%d-%H}" if active else ""


@pytest.fixture
def clean_jobs():
    def purge() -> None:
        with session_scope() as session:
            for value in session.exec(select(JobRow.id)).all():
                job_id = value[0] if isinstance(value, tuple) else value
                row = session.get(JobRow, job_id)
                if row is not None:
                    session.delete(row)

    purge()
    yield
    purge()


def _add_job(message: str, *, minutes_ago: int, state: str = "error") -> None:
    created = int((NOW - timedelta(minutes=minutes_ago)).timestamp())
    with session_scope() as session:
        session.add(
            JobRow(
                action="refresh_all",
                label="一键更新活动链接",
                state=state,
                message=message,
                created_at=created,
                finished_at=created,
            )
        )


def test_risk_failure_is_detected_from_jobs_table(clean_jobs) -> None:
    _add_job(RISK_MESSAGE, minutes_ago=10)
    assert recent_risk_refresh_failure(NOW)


def test_successful_batch_is_not_a_risk_failure(clean_jobs) -> None:
    _add_job("一键更新完成：检查 9 个数据源", minutes_ago=10, state="success")
    assert not recent_risk_refresh_failure(NOW)


def test_ordinary_error_is_not_treated_as_risk(clean_jobs) -> None:
    _add_job(ORDINARY_MESSAGE, minutes_ago=10)
    assert not recent_risk_refresh_failure(NOW)


def test_risk_backoff_expires(clean_jobs) -> None:
    _add_job(RISK_MESSAGE, minutes_ago=int(RISK_REFRESH_BACKOFF.total_seconds() // 60) + 20)
    assert not recent_risk_refresh_failure(NOW)


def test_refresh_window_is_not_due_during_risk_backoff(clean_jobs) -> None:
    scheduler = AutoScheduler(job_runner=MagicMock())
    assert scheduler._due_refresh_key(NOW) == slot_key(NOW)
    _add_job(RISK_MESSAGE, minutes_ago=10)
    assert scheduler._due_refresh_key(NOW) is None


def test_risk_block_expires_after_backoff(clean_jobs) -> None:
    """退避过去后风控压制解除，窗口照常（6 小时退避后已到 21 点窗口）。"""
    _add_job(RISK_MESSAGE, minutes_ago=10)
    scheduler = AutoScheduler(job_runner=MagicMock())
    assert scheduler._due_refresh_key(NOW) is None
    later = NOW + RISK_REFRESH_BACKOFF + timedelta(minutes=5)
    assert not recent_risk_refresh_failure(later)
    assert scheduler._due_refresh_key(later) == "2026-10-26-21"


def test_risk_failure_abandons_only_the_window_it_tried(clean_jobs, monkeypatch) -> None:
    """同一窗口反复补跑只会反复批量读、反复按停账号闸（今天 09 点窗口连败 3 次）：
    风控失败后本窗口放弃，退避过去也不再来，交给下一个窗口。"""
    scheduler = AutoScheduler(job_runner=MagicMock())
    calls: list[str] = []

    def fake_click(action: str, **_kwargs):
        calls.append(action)
        raise RuntimeError(RISK_MESSAGE)

    monkeypatch.setattr(scheduler, "_click_and_wait", fake_click)
    assert scheduler._run_refresh_batch(slot_key(NOW)) is False
    assert calls == ["refresh_all"]
    assert slot_key(NOW) in scheduler._refresh_risk_abandoned
    assert scheduler._due_refresh_key(NOW) is None
    # 16 点仍在 6 小时退避内，15 点窗口也不会被重放
    assert scheduler._due_refresh_key(NOW + timedelta(minutes=30)) is None
    # 下一个窗口照常，不会被这次放弃连带堵死
    later_window = datetime(2026, 10, 26, max(REFRESH_HOURS), 30, tzinfo=CN_TZ)
    assert scheduler._due_refresh_key(later_window) == slot_key(later_window)


def test_soft_risk_failure_also_abandons_the_window(clean_jobs, monkeypatch) -> None:
    """风控话术没被当成硬失败时（批次会继续跑完），同样要放弃本窗口不重放。"""
    scheduler = AutoScheduler(job_runner=MagicMock())
    calls: list[str] = []

    def fake_click(action: str, **_kwargs):
        calls.append(action)
        if action == "refresh_all":
            raise RuntimeError("B 站拒绝了这个请求（多为平台校验或风控）：Client error '412 Precondition Failed'")

    monkeypatch.setattr(scheduler, "_click_and_wait", fake_click)
    assert scheduler._run_refresh_batch(slot_key(NOW)) is False
    assert calls == ["refresh_all", "refresh_watch", "refresh_status"]
    assert slot_key(NOW) in scheduler._refresh_risk_abandoned
