from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from src.participation_guard import candidate_allowed, get_guard, set_guard
from src.lottery_actions import ActionResult
from src.participation import participate_activity

TARGET = "1253309987185229824"
OBSOLETE = "缺少明确参与规则，需刷新详情或人工核查"


@pytest.mark.parametrize("conditions", [{}, {"need_follow": False, "need_repost": False, "need_comment": False}, {"need_follow": "unknown"}])
def test_forward_fixed_actions_do_not_depend_on_model_fields(monkeypatch, conditions):
    from src.activity_store import replace_all_activities
    replace_all_activities([{"dynamic_id": TARGET, "lottery_type": "转发抽奖", "conditions": conditions}])
    monkeypatch.setattr("src.participation.fetch_dynamic_detail", lambda *a: {"modules": {}})
    monkeypatch.setattr("src.participation.is_upower_dynamic", lambda *a: False)
    monkeypatch.setattr("src.participation._resolve_action_text", lambda *a, **k: ("自己的固定文案", {}))
    monkeypatch.setattr("src.participate_preflight._load_activity_item", lambda *a: pytest.fail("模型规则不应作为执行门槛"))
    actions = [ActionResult(name, True, required=True) for name in ("like", "follow", "repost", "comment")]
    execute = Mock(return_value=(actions, SimpleNamespace()))
    monkeypatch.setattr("src.participation.execute_full_participation", execute)
    result = participate_activity(object(), dynamic_id=TARGET, lottery_type="转发抽奖", persist=False)
    assert result.status == "joined"
    assert execute.call_args.kwargs["action_names"] == ("like", "follow", "repost", "comment")
    assert execute.call_args.kwargs["required_actions"] == ("like", "follow", "repost", "comment")
    assert execute.call_args.kwargs["action_text"] == "自己的固定文案"
    assert result.context_snapshot["action_plan_source"] == "fixed_strategy"


def test_saved_comment_is_used_in_manual_and_automatic_jobs_without_review(monkeypatch):
    """无显式文案时 actions 交回 None，由解析器按模式定；custom 模式解析出的仍是已保存文案，全程不碰 LLM 审查。"""
    from src.participate_text import resolve_participate_text_for_activity
    from src.user_settings import set_participate_text, set_participate_text_mode
    from web.actions import _reviewed_action_text
    set_participate_text_mode("custom")
    set_participate_text("已保存的自己的文案")
    monkeypatch.setattr("src.comment_preview.ensure_auto_comment_review", lambda *a: pytest.fail("不应逐条要求确认"))
    assert _reviewed_action_text({}, TARGET, from_auto=True) is None
    assert _reviewed_action_text({}, TARGET, from_auto=False) is None
    assert resolve_participate_text_for_activity(object(), dynamic_id=TARGET).text == "已保存的自己的文案"
    assert _reviewed_action_text({"action_text": "接口指定文案"}, TARGET, from_auto=False) == "接口指定文案"


@pytest.mark.parametrize("reason", [OBSOLETE, "参与规则为空，需人工核查", "评论内容与活动要求需先核查，请先通过人工预览确认"])
def test_obsolete_reviews_with_no_writes_recover_automatically(reason):
    set_guard(TARGET, state="review", reason=reason)
    assert candidate_allowed(TARGET)
    assert get_guard(TARGET)["state"] == "ready"


@pytest.mark.parametrize("state", ["in_flight", "unknown", "succeeded", "failed"])
def test_old_review_reason_cannot_release_any_recorded_write(state):
    set_guard(TARGET, state="review", reason=OBSOLETE, action="repost", progress={"state": state})
    assert not candidate_allowed(TARGET)
    assert get_guard(TARGET)["progress"]["repost"]["state"] == state


def test_account_pause_and_manual_exclusion_are_not_auto_released():
    set_guard("", state="account_paused", reason="HTTP 412")
    set_guard(TARGET, state="review", reason=OBSOLETE)
    assert not candidate_allowed(TARGET)
    assert get_guard(TARGET)["state"] == "review"
    set_guard("", state="ready")
    set_guard(TARGET, state="excluded", reason=OBSOLETE)
    assert not candidate_allowed(TARGET)
    assert get_guard(TARGET)["state"] == "excluded"


def test_cli_execute_needs_no_extra_confirmation_flag(monkeypatch):
    import scripts.participate as cli
    calls = Mock(return_value=SimpleNamespace(status="joined", to_dict=lambda: {"status": "joined"}))
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr("sys.argv", ["participate.py", TARGET, "--execute", "--text", "自己的文案"])
    monkeypatch.setattr("src.app_paths.ensure_user_dirs", lambda: None)
    monkeypatch.setattr(cli, "_lookup_lottery_type", lambda *a: "转发抽奖")
    monkeypatch.setattr(cli, "BilibiliClient", Client)
    monkeypatch.setattr(cli, "participate_activity", calls)
    monkeypatch.setattr("src.comment_preview.create_preview", lambda *a, **k: {"body": {"items": [{"text": k["text"]}]}})
    assert cli.main() == 0
    assert calls.call_args.kwargs["action_text"] == "自己的文案"


def test_transient_system_error_deferred_auto_restores(monkeypatch):
    """code=137002「系统错误中，请稍后再试」这类写请求未执行的一过性失败，冷却期后自动放行重试。"""
    import src.participation_guard as guard_module
    monkeypatch.setattr(guard_module, "TRANSIENT_DEFERRED_COOLDOWN_SEC", 0)
    set_guard("", state="ready")
    set_guard(TARGET, state="deferred", reason="code=137002 系统错误中，请稍后再试")
    assert candidate_allowed(TARGET)
    assert get_guard(TARGET)["state"] == "ready"


def test_transient_deferred_respects_cooldown():
    """冷却期内不放行：一行反复 137002 不能每 15 分钟吃掉一个三连刻度。"""
    set_guard("", state="ready")
    set_guard(TARGET, state="deferred", reason="code=137002 系统错误中，请稍后再试")
    assert not candidate_allowed(TARGET)
    assert get_guard(TARGET)["state"] == "deferred"


def test_login_deferred_guard_stays_blocked():
    set_guard("", state="ready")
    set_guard(TARGET, state="deferred", reason="code=-101 账号未登录")
    assert not candidate_allowed(TARGET)
    assert get_guard(TARGET)["state"] == "deferred"


def test_transient_deferred_with_unknown_write_is_not_released():
    set_guard("", state="ready")
    set_guard(TARGET, state="deferred", reason="请求过于频繁", action="repost", progress={"state": "unknown"})
    assert not candidate_allowed(TARGET)
    assert get_guard(TARGET)["state"] == "deferred"
