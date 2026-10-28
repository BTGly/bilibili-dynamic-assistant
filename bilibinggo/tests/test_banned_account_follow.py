from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

import src.lottery_actions as actions
from src.participation_guard import (
    candidate_allowed,
    failure_kind,
    get_guard,
    record_failure,
    release_guard,
)

BANNED_UID = 14137123
BANNED_TARGET = "1262724662829252612"


def _card_payload(*, fans: int, title: str | None) -> dict:
    official = {"role": 3, "title": title, "desc": "", "type": 1} if title is not None else {}
    return {"code": 0, "data": {"card": {"mid": str(BANNED_UID), "fans": fans, "Official": official}}}


def _client(payload: dict) -> Mock:
    client = Mock()
    client.request_json.return_value = payload
    return client


def test_scrubbed_profile_is_read_as_banned() -> None:
    client = _client(_card_payload(fans=0, title=str(BANNED_UID)))
    assert actions.is_banned_account(client, uid=BANNED_UID, referer="https://www.bilibili.com/opus/1")


def test_normal_profile_is_not_banned() -> None:
    client = _client(_card_payload(fans=1713492, title="bilibili 知名科技UP主"))
    assert not actions.is_banned_account(client, uid=BANNED_UID, referer="r")


def test_zero_fans_alone_is_not_banned() -> None:
    client = _client(_card_payload(fans=0, title="小UP主"))
    assert not actions.is_banned_account(client, uid=BANNED_UID, referer="r")


@pytest.mark.parametrize("payload", [{"code": -799, "message": "请求过于频繁"}, {"code": 0, "data": {}}])
def test_unreadable_profile_is_not_banned(payload: dict) -> None:
    assert not actions.is_banned_account(_client(payload), uid=BANNED_UID, referer="r")


def test_card_network_error_is_not_banned() -> None:
    client = Mock()
    client.request_json.side_effect = httpx.ConnectTimeout("boom")
    assert not actions.is_banned_account(client, uid=BANNED_UID, referer="r")


def _follow_client(banned: bool) -> Mock:
    client = Mock()
    client.post_form.return_value = {"code": 137002, "message": "系统升级中，敬请谅解"}
    client.request_json.return_value = _card_payload(fans=0, title=str(BANNED_UID)) if banned else _card_payload(
        fans=1713492, title="bilibili 知名科技UP主"
    )
    return client


def test_follow_137002_on_banned_up_is_permanent_failure() -> None:
    result = actions.follow_user(_follow_client(True), uid=BANNED_UID, csrf="c", referer="r")
    assert not result.ok and result.state == "failed"
    assert "已被 B 站封禁" in result.detail


def test_follow_137002_on_live_up_stays_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Mock(return_value=False)
    monkeypatch.setattr(actions, "is_banned_account", probe)
    result = actions.follow_user(_follow_client(True), uid=BANNED_UID, csrf="c", referer="r")
    assert not result.ok
    assert result.detail == "code=137002 系统升级中，敬请谅解"
    probe.assert_called_once()


def test_banned_up_row_is_excluded_and_never_requeued() -> None:
    error = RuntimeError(
        f"code=137002 且 uid={BANNED_UID} 已被 B 站封禁（粉丝数清零、官方名被替换为 UID），关注条件无法满足"
    )
    assert failure_kind(error) == "excluded"
    record_failure(BANNED_TARGET, error)
    assert get_guard(BANNED_TARGET)["state"] == "excluded"
    assert not candidate_allowed(BANNED_TARGET)
    release_guard(BANNED_TARGET, evidence="人工确认该 UP 已恢复正常")
    assert candidate_allowed(BANNED_TARGET)
