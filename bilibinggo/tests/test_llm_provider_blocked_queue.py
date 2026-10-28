from __future__ import annotations

import time

from src.db.session import session_scope
from src.llm_client import is_provider_blocked
from src.pipeline import checkpoint_store as cp
from src.db.models import RefreshWorkRow

IDS = [str(12999000000000000 + i) for i in range(6)]
PROVIDER_ERROR = RuntimeError("连接失败（HTTP 402）：Sorry, your account balance is insufficient")
RATE_LIMIT_ERROR = RuntimeError("LLM 服务暂时不可用（HTTP 429）：Request was rejected due to rate limiting. Details: RPM limit reached.")
ORDINARY_ERROR = RuntimeError("无法获取活动热度: 1211103403650842648")


def _snapshot(dynamic_id: str) -> dict:
    with session_scope() as session:
        row = session.get(RefreshWorkRow, dynamic_id)
        return {"state": row.state, "attempts": row.attempts, "reason": row.reason, "retry_at": row.retry_at}


def test_provider_error_phrases_are_recognised() -> None:
    for text in (
        "连接失败（HTTP 402）：Sorry, your account balance is insufficient",
        "API Key 无效或已过期",
        "接口地址或模型不存在",
        "未配置 LLM，请检查 config/llm.env",
        "余额不足，请充值",
        "LLM 服务暂时不可用（HTTP 429）：Request was rejected due to rate limiting. Details: RPM limit reached.",
    ):
        assert is_provider_blocked(RuntimeError(text)), text
    assert not is_provider_blocked(ORDINARY_ERROR)
    assert not is_provider_blocked(RuntimeError("code=-101 账号未登录"))


def test_rate_limited_failure_does_not_burn_attempts_either() -> None:
    cp.enqueue([IDS[4]])
    for _ in range(6):
        cp.failed(IDS[4], RATE_LIMIT_ERROR)
    row = _snapshot(IDS[4])
    assert row["attempts"] == 0 and row["state"] == "deferred"
    assert "LLM 服务商受阻" in row["reason"]


def test_provider_blocked_failure_keeps_queue_alive_without_burning_attempts() -> None:
    cp.enqueue([IDS[0]])
    for _ in range(6):
        cp.failed(IDS[0], PROVIDER_ERROR)
    row = _snapshot(IDS[0])
    assert row["attempts"] == 0
    assert row["state"] == "deferred"
    assert "LLM 服务商受阻" in row["reason"]
    assert row["retry_at"] > time.time(), "受阻项应按节流延后，而不是每格重试"


def test_ordinary_failure_still_escalates_to_review() -> None:
    cp.enqueue([IDS[1]])
    for _ in range(3):
        cp.failed(IDS[1], ORDINARY_ERROR)
    row = _snapshot(IDS[1])
    assert row["attempts"] == 3
    assert row["state"] == "review"


def test_blocked_deferral_then_real_error_still_escalates() -> None:
    cp.enqueue([IDS[2]])
    cp.failed(IDS[2], PROVIDER_ERROR)
    assert _snapshot(IDS[2])["attempts"] == 0
    for _ in range(3):
        cp.failed(IDS[2], ORDINARY_ERROR)
    assert _snapshot(IDS[2])["state"] == "review"


def test_blocked_row_returns_to_queue_once_retry_is_due() -> None:
    did = IDS[3]
    cp.enqueue([did])
    cp.failed(did, PROVIDER_ERROR)
    with session_scope() as session:
        session.get(RefreshWorkRow, did).retry_at = int(time.time()) - 1
    assert did in cp.pending_ids()
