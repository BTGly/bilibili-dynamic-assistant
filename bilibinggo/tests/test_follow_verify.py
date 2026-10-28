from __future__ import annotations

from unittest.mock import Mock

import src.lottery_actions as actions

TARGET = 900000001


def _client(*codes):
    client = Mock()
    client.post_form.side_effect = [{"code": code} for code in codes]
    client.request_json.return_value = {"code": 0, "data": {"attribute": 0}}
    return client


def test_follow_verifies_relation_after_code_zero(monkeypatch):
    monkeypatch.setattr(actions, "is_following", Mock(return_value=True))
    result = actions.follow_user(_client(0), uid=TARGET, csrf="c", referer="r")
    assert result.ok and result.detail == f"uid={TARGET}"


def test_follow_silently_dropped_is_retried_once(monkeypatch):
    monkeypatch.setattr(actions, "is_following", Mock(side_effect=[False, True]))
    monkeypatch.setattr(actions.time, "sleep", Mock())
    client = _client(0, 0)
    result = actions.follow_user(client, uid=TARGET, csrf="c", referer="r")
    assert result.ok and "补关注成功" in result.detail
    assert client.post_form.call_count == 2
    assert all(call.kwargs["retries"] == 0 for call in client.post_form.call_args_list)


def test_follow_still_missing_after_retry_is_failure(monkeypatch):
    monkeypatch.setattr(actions, "is_following", Mock(return_value=False))
    monkeypatch.setattr(actions.time, "sleep", Mock())
    result = actions.follow_user(_client(0, 0), uid=TARGET, csrf="c", referer="r")
    assert not result.ok and result.state == "failed"
    assert "关注关系未生效" in result.detail


def test_follow_22014_means_already_following(monkeypatch):
    monkeypatch.setattr(actions, "is_following", Mock())
    result = actions.follow_user(_client(22014), uid=TARGET, csrf="c", referer="r")
    assert result.ok and "已关注" in result.detail
    actions.is_following.assert_not_called()
