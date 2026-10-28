"""Durable read-work queue; activity insert and completion share one transaction."""
from dataclasses import asdict
import json
import time

from sqlmodel import select
from src.db.models import ActivityRow, RefreshWorkRow
from src.db.session import session_scope
from src.db.activity_codec import activity_dict_to_row
from src.pipeline.classify_step import ClassifyOutcome

MAX_ATTEMPTS = 3


def enqueue(ids):
    with session_scope() as session:
        for did in ids:
            if session.get(RefreshWorkRow, did) is None:
                session.add(RefreshWorkRow(dynamic_id=did, updated_at=int(time.time())))


def pending_ids():
    now = int(time.time())
    with session_scope() as session:
        rows = session.exec(select(RefreshWorkRow).where(
            RefreshWorkRow.state.in_(("pending", "classified", "deferred")),
            RefreshWorkRow.retry_at <= now,
        ).order_by(RefreshWorkRow.updated_at, RefreshWorkRow.dynamic_id)).all()
        return [row.dynamic_id for row in rows]


def cached_outcome(did):
    with session_scope() as session:
        row = session.get(RefreshWorkRow, did)
        if not row or not row.classify_json:
            return None
        payload = json.loads(row.classify_json)
        saved_at = payload.pop("_saved_at", row.updated_at)
        if int(time.time()) - saved_at >= 300:
            # Keep the type checkpoint, but re-read changing text/platform state.
            payload.update(classify_content="", detail_item=None, lottery_notice=None)
        return ClassifyOutcome(**payload)


def classified(outcome):
    with session_scope() as session:
        row = session.get(RefreshWorkRow, outcome.dynamic_id)
        payload = asdict(outcome)
        payload["_saved_at"] = int(time.time())
        row.classify_json = json.dumps(payload, ensure_ascii=False)
        row.state = "skipped" if outcome.skipped else "classified"
        row.reason = str(outcome.skip_reason or "")
        row.updated_at = int(time.time())


def failed(did, exc):
    from src.llm_client import is_provider_blocked

    provider_blocked = is_provider_blocked(exc)
    with session_scope() as session:
        row = session.get(RefreshWorkRow, did)
        if provider_blocked:
            # LLM 充值/换 Key 之前重试无意义：不消耗 attempts，队列原地保持 deferred，
            # 额度恢复后下一轮窗口自动全部跑完。
            row.state = "deferred"
            row.reason = f"LLM 服务商受阻（不计重试次数）：{type(exc).__name__}: {exc}"
        else:
            row.attempts += 1
            row.state = "review" if row.attempts >= MAX_ATTEMPTS else "deferred"
            row.reason = f"{type(exc).__name__}: {exc}"
        row.retry_at = int(time.time()) + (900 if provider_blocked else min(3600, 300 * (2 ** (row.attempts - 1))))
        row.updated_at = int(time.time())


def skipped(did, reason):
    with session_scope() as session:
        row = session.get(RefreshWorkRow, did)
        row.state, row.reason = "skipped", reason
        row.updated_at = int(time.time())


def persist(item):
    did = str(item["dynamic_id"])
    now = int(time.time())
    with session_scope() as session:
        fresh = session.get(ActivityRow, did) is None
        if fresh:
            session.add(activity_dict_to_row(item, updated_at=now))
        row = session.get(RefreshWorkRow, did)
        row.state, row.reason, row.updated_at = "done", "", now
        row.classify_json = ""
        return int(fresh)


def preview():
    with session_scope() as session:
        rows = session.exec(select(RefreshWorkRow).where(
            RefreshWorkRow.state.notin_(("done", "skipped")))).all()
        return [{"dynamic_id": row.dynamic_id, "state": row.state, "attempts": row.attempts,
                 "retry_at": row.retry_at, "reason": row.reason} for row in rows]


def release(did, evidence):
    if not evidence.strip():
        raise ValueError("恢复重试必须填写核查依据")
    with session_scope() as session:
        row = session.get(RefreshWorkRow, did)
        if row is None or row.state not in ("deferred", "review", "skipped"):
            raise ValueError("目标没有可恢复的刷新检查点")
        row.state, row.attempts, row.retry_at = "pending", 0, 0
        row.classify_json = ""
        row.reason = "人工核查：" + evidence
        row.updated_at = int(time.time())
