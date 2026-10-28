"""Signed participation previews bind the displayed text to activities and their rules."""
from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import time
from src.db.models import ActivityRow
from src.db.session import session_scope
from src.db.activity_codec import row_to_activity_dict
from src.user_settings import get_participate_text, normalize_participate_text
from src.db.uids import participation_uid

_KEY = secrets.token_bytes(32)


def _activity(dynamic_id):
    with session_scope() as session:
        row = session.get(ActivityRow, dynamic_id)
        if row is None:
            raise ValueError("未找到活动，无法生成参与预览")
        return row_to_activity_dict(row)


def rule_fingerprint(item):
    rules = item.get("conditions") or {}
    payload = {"lottery_type": item.get("lottery_type"),
               "rules": {key: value for key, value in rules.items() if key.startswith("need_") or key.startswith("comment_")}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _sign(body):
    return hmac.new(_KEY, json.dumps(body, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


def create_preview(action: str, dynamic_ids: list[str], *, text: str | None = None) -> dict:
    if action not in ("participate", "participate_triple") or not dynamic_ids:
        raise ValueError("当前没有可参与目标")
    if text is not None and not isinstance(text, str):
        raise ValueError("参与文案必须是文本")
    chosen = normalize_participate_text(text if text is not None else get_participate_text())
    items = []
    for dynamic_id in dynamic_ids:
        item = _activity(dynamic_id)
        items.append({"dynamic_id": dynamic_id, "lottery_type": item.get("lottery_type"),
                      "source_url": item.get("source_url"), "text": chosen,
                      "rules": {key: value for key, value in (item.get("conditions") or {}).items() if key.startswith("need_")},
                      "rule_fingerprint": rule_fingerprint(item),
                      "planned_actions": ["like", "follow", "repost", "comment"] if item.get("lottery_type") == "转发抽奖" else ["follow", "reserve"] if item.get("lottery_type") == "预约抽奖" else ["like", "follow", "repost"],
                      "rule_match": "按固定动作和已保存文案执行；模型规则仅供参考"})
    body = {"action": action, "expires_at": int(time.time()) + 600, "items": items, "source": "custom", "uid": participation_uid()}
    return {"body": body, "signature": _sign(body)}


def validate_preview(preview: dict, *, action: str, dynamic_ids: list[str]) -> dict[str, str]:
    if not isinstance(preview, dict):
        raise ValueError("参与预览格式无效")
    body = preview.get("body") or {}
    if not hmac.compare_digest(str(preview.get("signature") or ""), _sign(body)):
        raise ValueError("参与预览无效，请重新预览并确认")
    if body.get("action") != action:
        raise ValueError("参与预览的操作类型与本次提交不一致，请重新预览")
    if body.get("expires_at", 0) < time.time():
        raise ValueError("参与预览已过期，请重新确认")
    if body.get("uid") != participation_uid():
        raise ValueError("账号已变化，请重新预览")
    items = body.get("items") or []
    if [item["dynamic_id"] for item in items] != dynamic_ids:
        raise ValueError("参与目标已变化，请重新预览")
    for item in items:
        if item["rule_fingerprint"] != rule_fingerprint(_activity(item["dynamic_id"])):
            raise ValueError("活动规则已变化，请重新核对原文")
    return {item["dynamic_id"]: item["text"] for item in items}


def confirm_comment_review(dynamic_id: str, text: str) -> None:
    from src.activity_store import update_activity
    item = _activity(dynamic_id)
    item["comment_review"] = {"text": text, "rule_fingerprint": rule_fingerprint(item), "reviewed_at": int(time.time()), "uid": participation_uid()}
    update_activity(dynamic_id, item)


def ensure_auto_comment_review(dynamic_id: str, text: str) -> None:
    """Compatibility helper: saved custom text no longer requires per-target review."""
    return None
