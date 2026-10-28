import argparse
from unittest.mock import Mock

import httpx
import pytest

from scripts.dynamic_operations import main, parse_target
from src import lottery_actions as actions

TARGET = "1248895125820538904"


@pytest.fixture
def execution(monkeypatch):
    context = actions.DynamicContext(
        dynamic_id=TARGET, sender_uid=1, referer=f"https://www.bilibili.com/opus/{TARGET}",
        comment_rid=TARGET, comment_type=17, liked=False, favorited=False,
        favorite_available=True, followed=False, reposted=False, commented=False,
    )
    monkeypatch.setattr(actions, "build_dynamic_context", Mock(return_value=context))
    monkeypatch.setattr(actions, "require_login", Mock(return_value=("test-token", 2)))
    monkeypatch.setattr(actions.time, "sleep", Mock())
    calls = {}
    for name, func in (
        ("like", "like_dynamic"), ("follow", "follow_user"), ("favorite", "favorite_dynamic"),
        ("repost", "repost_dynamic"), ("comment", "comment_dynamic"),
    ):
        calls[name] = Mock(return_value=actions.ActionResult(name, True))
        monkeypatch.setattr(actions, func, calls[name])
    return context, calls


def test_preview_performs_no_write_or_login_requirement(execution):
    _, calls = execution
    result, _ = actions.execute_full_participation(
        Mock(), dynamic_id=TARGET, dry_run=True, action_names=("follow", "like", "repost", "comment"),
    )
    assert [r.action for r in result] == ["follow", "like", "repost", "comment"]
    assert all(not call.called for call in calls.values())
    actions.require_login.assert_not_called()


def test_selected_four_operations_never_collect(execution):
    _, calls = execution
    result, _ = actions.execute_full_participation(
        Mock(), dynamic_id=TARGET, action_names=("follow", "like", "repost", "comment"),
    )
    assert len(result) == 4
    calls["favorite"].assert_not_called()
    for name in ("follow", "like", "repost", "comment"):
        calls[name].assert_called_once()


def test_existing_actions_are_skipped(execution):
    context, calls = execution
    context.followed = context.liked = context.reposted = context.commented = True
    result, _ = actions.execute_full_participation(
        Mock(), dynamic_id=TARGET, action_names=("follow", "like", "repost", "comment"),
    )
    assert all(r.ok for r in result)
    assert all(not call.called for call in calls.values())


@pytest.mark.parametrize("failure", [actions.ActionResult("follow", False, "failure"), RuntimeError("timeout"), httpx.ReadTimeout("timeout")])
def test_failed_first_action_stops_later_writes(execution, failure):
    _, calls = execution
    if isinstance(failure, Exception):
        calls["follow"].side_effect = failure
    else:
        calls["follow"].return_value = failure
    result, _ = actions.execute_full_participation(
        Mock(), dynamic_id=TARGET, action_names=("follow", "like", "repost", "comment"), stop_on_failure=True,
    )
    assert len(result) == 1 and not result[0].ok
    assert all(not calls[name].called for name in ("like", "repost", "comment", "favorite"))


@pytest.mark.parametrize("selected", [(), ("like", "like"), ("reserve",), "like"])
def test_invalid_action_list_is_rejected_before_requests(execution, selected):
    with pytest.raises(ValueError):
        actions.execute_full_participation(Mock(), dynamic_id=TARGET, action_names=selected)
    actions.build_dynamic_context.assert_not_called()


@pytest.mark.parametrize("message", ["已结束", "已被限制", "重复请求错误"])
def test_error_text_is_not_evidence_of_success(monkeypatch, message):
    client = Mock()
    client.post_form.return_value = {"code": 999, "message": message}
    monkeypatch.setattr(actions, "is_reposted", Mock(return_value=False))
    result = actions.repost_dynamic(client, dynamic_id=TARGET, my_uid=2, csrf="test", referer="test", content="test")
    assert not result.ok
    assert client.post_form.call_args.kwargs["retries"] == 0


def test_repost_error_requires_confirmed_account_state(monkeypatch):
    client = Mock()
    client.post_form.return_value = {"code": 999, "message": "重复"}
    monkeypatch.setattr(actions, "is_reposted", Mock(return_value=True))
    assert actions.repost_dynamic(client, dynamic_id=TARGET, my_uid=2, csrf="test", referer="test", content="test").ok


def test_repost_verification_failure_is_not_success(monkeypatch):
    client = Mock()
    client.post_form.return_value = {"code": 999, "message": "重复"}
    monkeypatch.setattr(actions, "is_reposted", Mock(side_effect=httpx.ReadTimeout("timeout")))
    assert not actions.repost_dynamic(client, dynamic_id=TARGET, my_uid=2, csrf="test", referer="test", content="test").ok


def test_mutation_requests_never_retry():
    client = Mock()
    client.post_form.return_value = client.post_json.return_value = {"code": 0}
    actions.like_dynamic(client, dynamic_id=TARGET, csrf="test", referer="test")
    actions.follow_user(client, uid=1, csrf="test", referer="test")
    actions.comment_dynamic(client, rid=TARGET, comment_type=17, message="test", csrf="test", referer="test")
    for call in [*client.post_form.call_args_list, *client.post_json.call_args_list]:
        assert call.kwargs["retries"] == 0


def test_cli_requires_explicit_comment_before_any_network(monkeypatch):
    login = Mock()
    monkeypatch.setattr("scripts.dynamic_operations.require_login", login)
    with pytest.raises(SystemExit) as error:
        main([TARGET, "--execute"])
    assert error.value.code == 2
    login.assert_not_called()


def test_accepts_exact_known_dynamic_url():
    assert parse_target(f"https://www.bilibili.com/opus/{TARGET}?spm_id_from=333.1365.0.0") == TARGET
    assert parse_target(f"https://t.bilibili.com/{TARGET}") == TARGET


@pytest.mark.parametrize("target", [f"https://example.com/opus/{TARGET}", f"https://www.bilibili.com.example.com/opus/{TARGET}", "https://b23.tv/j6dxPog", "https://www.bilibili.com/video/BV1Wi4y1x74E/", f"https://user@www.bilibili.com/opus/{TARGET}"])
def test_rejects_unsupported_or_ambiguous_targets(target):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_target(target)


@pytest.mark.parametrize("basic", [{}, {"comment_id_str": TARGET}, {"comment_type": 17}])
def test_missing_comment_metadata_is_never_guessed(monkeypatch, basic):
    monkeypatch.setattr(actions, "fetch_dynamic_detail", Mock(return_value={
        "modules": {"module_author": {"mid": 1}}, "basic": basic,
    }))
    client = Mock()
    with pytest.raises(RuntimeError, match="basic.comment"):
        actions.build_dynamic_context(client, dynamic_id=TARGET, action_text="test", strict=True)
    client.post_form.assert_not_called()
