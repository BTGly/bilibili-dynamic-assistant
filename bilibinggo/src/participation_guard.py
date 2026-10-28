"""Durable participation progress and cross-process account write serialization."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import hashlib
import os
import re
import threading
import time

import httpx
from sqlmodel import select

from src.app_paths import data_dir
from src.db.models import ParticipationGuardRow, ParticipationActionRow
from src.db.session import session_scope
from src.db.uids import participation_uid
from src.db.json_cols import dumps_json, loads_json

_lock = threading.RLock()
_local = threading.local()


@contextmanager
def account_write_lock():
    # Keep the descriptor open: OS releases its lock even after a process crash.
    with _lock:
        if getattr(_local, "depth", 0):
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        folder = data_dir() / "write-locks"
        folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(participation_uid().encode()).hexdigest()
        with (folder / (key + ".lock")).open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    time.sleep(0.1)
            _local.depth = 1
            try:
                yield
            finally:
                _local.depth = 0
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def serial_writes(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if kwargs.get("dry_run", False):
            return fn(*args, **kwargs)
        with account_write_lock():
            return fn(*args, **kwargs)
    return wrapped


def failure_kind(error) -> str:
    text = str(error)
    if "已被 B 站封禁" in text:
        # 目标 UP 被处置后任何人都关注不上，重试只会白烧刻度，直接排除等人核查
        return "excluded"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403, 412, 429):
        return "account_paused"
    if re.search(r"(?:code=|API error )(-101|-111|-352|-509|412|429)(?!\d)", text) or any(
        word in text for word in ("限流", "风控", "验证", "登录失效", "未登录", "请先登录", "请先扫码登录", "未检测到有效登录 Cookie", "账号队列已暂停")
    ):
        return "account_paused"
    if any(word in text for word in ("任务已取消", "用户取消")):
        return "cancelled"
    if any(word in text for word in ("无法确认", "未确认", "网络", "timeout", "Timeout", "JSON", "超时", "开奖时间未知", "需先核查")) or isinstance(error, httpx.RequestError):
        return "review"
    return "deferred"


def get_guard(dynamic_id: str) -> dict:
    with session_scope() as session:
        row = session.get(ParticipationGuardRow, (participation_uid(), dynamic_id))
        if row is None and dynamic_id:
            history = session.exec(select(ParticipationActionRow).where(
                ParticipationActionRow.uid == participation_uid(), ParticipationActionRow.dynamic_id == dynamic_id
            ).order_by(ParticipationActionRow.id)).all()
            if history:
                progress = {}
                for entry in history:
                    for item in loads_json(entry.actions_json, default=[]):
                        name = item.get("action")
                        if not name or progress.get(name, {}).get("state") == "succeeded":
                            continue
                        state = "succeeded" if item.get("ok") else item.get("state") or (
                            "unknown" if failure_kind(item.get("detail", "")) == "review" else "failed")
                        progress[name] = {"state": state, "detail": item.get("detail", ""), "text": entry.action_text}
                latest = history[-1]
                state = "ready" if latest.status == "joined" else "review"
                row = ParticipationGuardRow(uid=participation_uid(), dynamic_id=dynamic_id,
                    state=state, reason="历史参与记录需核查：" + latest.message if state != "ready" else "历史已完成记录",
                    progress_json=dumps_json(progress), updated_at=int(time.time()))
        if row is None:
            return {"state": "ready", "progress": {}, "reason": ""}
        return {"state": row.state, "reason": row.reason, "progress": loads_json(row.progress_json, default={})}


def set_guard(dynamic_id: str, *, state=None, reason=None, action=None, progress=None):
    with session_scope() as session:
        row = session.get(ParticipationGuardRow, (participation_uid(), dynamic_id))
        if row is None:
            initial = get_guard(dynamic_id)
            row = ParticipationGuardRow(uid=participation_uid(), dynamic_id=dynamic_id,
                state=initial["state"], reason=initial["reason"], progress_json=dumps_json(initial["progress"]))
            session.add(row)
        if state is not None:
            row.state = state
        if reason is not None:
            row.reason = reason
        if action is not None:
            current = loads_json(row.progress_json, default={})
            current[action] = progress
            row.progress_json = dumps_json(current)
        row.updated_at = int(time.time())


def _clean_reason(text: str) -> str:
    # 暂停闸会把自己的前缀拼进异常文本，再被 record_failure 原样存回 reason，
    # 下一轮拼接就变成「账号队列已暂停：账号队列已暂停：…」；落库前剥掉这些前缀。
    prefixes = ("账号队列已暂停：", "账号异常，刷新队列已暂停：", "账号异常，需验证或解除限流：")
    stripped = str(text or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                changed = True
    return stripped


def record_failure(dynamic_id: str, error):
    kind = failure_kind(error)
    reason = _clean_reason(str(error))
    if kind == "account_paused":
        set_guard("", state=kind, reason=reason)
    # Cancellation retains progress; unknown writes are already marked in-flight.
    if dynamic_id and kind != "cancelled":
        set_guard(dynamic_id, state=kind if kind != "account_paused" else "deferred", reason=reason)
    return kind


_OBSOLETE_REVIEW_REASONS = (
    "缺少明确参与规则，需刷新详情或人工核查",
    "参与规则为空，需人工核查",
    "评论内容与活动要求需先核查，请先通过人工预览确认",
)


def restore_obsolete_rule_review(dynamic_id: str) -> None:
    """Retire only the removed policy gates, never an attempted/unknown write."""
    def obsolete(guard):
        reason = guard.get("reason", "")
        if reason.startswith("历史参与记录需核查："):
            reason = reason.removeprefix("历史参与记录需核查：")
        return guard["state"] == "review" and not guard["progress"] and reason in _OBSOLETE_REVIEW_REASONS
    if not dynamic_id or not obsolete(get_guard(dynamic_id)):
        return
    with account_write_lock():
        if obsolete(get_guard(dynamic_id)):
            set_guard(dynamic_id, state="ready", reason="已改用固定动作与已保存文案，旧规则/确认门槛已移除")


_TRANSIENT_DEFERRED_PATTERNS = (
    "系统错误", "137002", "请求过于频繁", "稍后再试", "too many requests",
)
TRANSIENT_DEFERRED_COOLDOWN_SEC = 3600


def restore_transient_deferred(dynamic_id: str) -> None:
    """B 站一过性系统错误（如 code=137002「系统错误中，请稍后再试」）时写请求并未执行，
    不该把行永久卡在 deferred：冷却期后自动放行重试。
    冷却期内保持 deferred，避免一行反复占用三连刻度；
    带 in_flight/unknown 写结果的、或原因不在一过性清单里的 deferred，一律不动。"""
    if not dynamic_id:
        return

    def transient(guard: dict) -> bool:
        if guard.get("state") != "deferred":
            return False
        reason = str(guard.get("reason") or "")
        if not any(pattern in reason for pattern in _TRANSIENT_DEFERRED_PATTERNS):
            return False
        for item in (guard.get("progress") or {}).values():
            if item.get("state") in ("in_flight", "unknown"):
                return False
        return True

    if not transient(get_guard(dynamic_id)):
        return
    with session_scope() as session:
        row = session.get(ParticipationGuardRow, (participation_uid(), dynamic_id))
        if row is None or time.time() - int(row.updated_at or 0) < TRANSIENT_DEFERRED_COOLDOWN_SEC:
            return
    with account_write_lock():
        guard = get_guard(dynamic_id)
        if transient(guard):
            set_guard(dynamic_id, state="ready",
                      reason="一过性错误自动放行重试（原：" + str(guard.get("reason") or "")[:60] + "）")


def candidate_allowed(dynamic_id: str) -> bool:
    if get_guard("")["state"] != "ready":
        return False
    restore_obsolete_rule_review(dynamic_id)
    restore_transient_deferred(dynamic_id)
    return get_guard(dynamic_id)["state"] == "ready"


def release_guard(dynamic_id: str, *, evidence: str, resolved_actions: dict | None = None):
    if not isinstance(resolved_actions, (dict, type(None))):
        raise ValueError("resolved_actions 必须是动作核查结果对象")
    if not evidence.strip():
        raise ValueError("恢复必须填写核查依据")
    guard = get_guard(dynamic_id)
    resolved_actions = resolved_actions or {}
    for name, item in guard["progress"].items():
        if item.get("state") in ("in_flight", "unknown") and resolved_actions.get(name) not in ("succeeded", "failed"):
            raise ValueError(f"{name} 结果未知，必须先核查并确认成功或失败")
    for name, state in resolved_actions.items():
        if name not in guard["progress"] or guard["progress"][name].get("state") not in ("unknown", "in_flight"):
            raise ValueError("只能核查已有的未知动作，不能覆盖已确认的成功记录")
        if state not in ("succeeded", "failed"):
            raise ValueError("核查结果必须为 succeeded 或 failed")
    for name, state in resolved_actions.items():
        progress = dict(guard["progress"][name])
        progress.update(state=state, detail=evidence)
        set_guard(dynamic_id, action=name, progress=progress)
    set_guard(dynamic_id, state="ready", reason=evidence)


def list_guards():
    with session_scope() as session:
        ids = set(session.exec(select(ParticipationGuardRow.dynamic_id).where(ParticipationGuardRow.uid == participation_uid())).all())
        ids.update(session.exec(select(ParticipationActionRow.dynamic_id).where(ParticipationActionRow.uid == participation_uid()).distinct()).all())
    return {key: get_guard(key) for key in ids}


def journal_action(dynamic_id, name, handler, *, enabled=True):
    from src.lottery_actions import ActionResult
    if not enabled:
        try:
            return handler()
        except (RuntimeError, httpx.HTTPError) as exc:
            return ActionResult(name, False, f"请求结果无法确认：{exc}", "unknown", True)
    previous = get_guard(dynamic_id)["progress"].get(name, {})
    if previous.get("state") == "succeeded":
        return ActionResult(name, True, "本地已确认完成，跳过", "succeeded", True)
    if previous.get("state") in ("unknown", "in_flight"):
        return ActionResult(name, False, "此前请求结果未知，需核查，禁止重发", "unknown", True)
    set_guard(dynamic_id, state="review", action=name, progress={"state": "in_flight"})
    try:
        result = handler()
    except Exception as exc:
        record_failure(dynamic_id, exc)
        prefix = "账号异常，需验证或解除限流" if failure_kind(exc) == "account_paused" else "请求结果无法确认"
        result = ActionResult(name, False, f"{prefix}：{exc}", "unknown", True)
    result.state = result.state or ("succeeded" if result.ok else "unknown" if "未确认" in result.detail else "failed")
    result.required = True
    set_guard(dynamic_id, action=name, progress={"state": result.state, "detail": result.detail})
    if not result.ok:
        record_failure(dynamic_id, result.detail)
    return result


def observe_account_io(fn):
    """A swallowed read error must still prevent the next account write."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        protected_read = bool(args and getattr(args[0], "_pause_on_account_error", False))
        participating = bool(getattr(_local, "depth", 0)) or protected_read
        if participating and (protected_read or fn.__name__.startswith("post_")) and get_guard("")["state"] != "ready":
            raise RuntimeError("账号队列已暂停：" + get_guard("")["reason"])
        try:
            payload = fn(*args, **kwargs)
        except Exception as exc:
            if participating and failure_kind(exc) == "account_paused":
                record_failure("", exc)
            raise
        if participating and isinstance(payload, dict):
            error = f"code={payload.get('code')} {payload.get('message') or payload.get('msg') or ''}"
            if payload.get("code") not in (0, "0", None) and failure_kind(error) == "account_paused":
                record_failure("", error)
                if not fn.__name__.startswith("post_"):
                    raise RuntimeError(error)
        return payload
    return wrapped
