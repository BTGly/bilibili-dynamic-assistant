"""归档=打标。活动行永远留在 activities 里，参加痕迹（participations / participation_actions /
participation_guards）一律不动，所以「这个我参加过」的核对照旧生效，防重复转发不受归档影响。

规则只碰已经不可能再参加的行（已结束 / 已截止 / 已参加且开奖已过 / 人工判定不参加或已过期），
未参加、待审核的行不会被归档——这样归档永远不会把三连队列里的东西吞掉。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import time

from sqlmodel import select

from src.db.activity_codec import row_to_activity_dict
from src.db.engine import db_path
from src.db.models import ActivityArchiveRow, ActivityRow
from src.db.session import session_scope
from src.lottery_time import BEIJING_TZ
from src.participation_guard import get_guard

# 还能参加的状态：一律不归档
LIVE_STATUSES = ("未参加", "待审核")
ARCHIVABLE_STATUSES = ("已参加", "已截止", "已结束", "已排除")


def archived_map() -> dict[str, dict]:
    with session_scope() as session:
        return {str(row.dynamic_id): {"archived_at": int(row.archived_at or 0), "reason": row.reason or ""}
                for row in session.exec(select(ActivityArchiveRow)).all()}


def stats() -> dict:
    marks = archived_map()
    months = Counter(
        datetime.fromtimestamp(value["archived_at"], BEIJING_TZ).strftime("%Y-%m") if value["archived_at"] else "(未知)"
        for value in marks.values()
    )
    reasons = Counter((value["reason"] or "(无原因)").split("：")[0] for value in marks.values())
    return {"archived": len(marks), "by_month": dict(sorted(months.items(), reverse=True)), "by_reason": dict(reasons)}


def candidates(*, now: int | None = None, joined_grace_days: int = 0) -> list[dict]:
    """按规则给出「可以归档」的行。joined_grace_days 控制已参加的要再等几天。"""
    current = int(now or time.time())
    grace = max(0, int(joined_grace_days)) * 86400
    marks = archived_map()
    out: list[dict] = []
    with session_scope() as session:
        for row in session.exec(select(ActivityRow)).all():
            item = row_to_activity_dict(row)
            dynamic_id = str(item.get("dynamic_id") or "")
            if not dynamic_id or dynamic_id in marks:
                continue
            status = str(item.get("activity_status") or "")
            draw_at = item.get("lottery_time")
            draw_at = int(draw_at) if draw_at else None
            if status in LIVE_STATUSES:
                continue
            reason = ""
            if status == "已结束":
                reason = "已结束（开奖时间已过）"
            elif status == "已截止":
                reason = "已截止（参与窗口关闭）"
            elif status == "已参加" and (draw_at is None or draw_at <= current - grace):
                reason = "已参加" + ("，开奖已过" if draw_at else "，开奖时间未知")
            elif status == "已排除":
                guard = get_guard(dynamic_id) or {}
                reason = "人工判定不参加：" + str(guard.get("reason") or "")[:40]
            if not reason:
                continue
            out.append({"dynamic_id": dynamic_id, "activity_status": status,
                        "lottery_time": draw_at, "reason": reason,
                        "title": str((item.get("prizes") or [{}])[0].get("description") or "")[:30]})
    return out


def archive(dynamic_ids: list[str], *, reason: str, now: int | None = None) -> dict:
    """打标。仍在队列口径（未参加/待审核）的行会被跳过并原样报告。"""
    current = int(now or time.time())
    added: list[str] = []
    skipped: list[dict] = []
    with session_scope() as session:
        for dynamic_id in {str(x).strip() for x in dynamic_ids if str(x).strip()}:
            row = session.get(ActivityRow, dynamic_id)
            if row is None:
                skipped.append({"dynamic_id": dynamic_id, "why": "库里没有这一行"})
                continue
            status = str(row.activity_status or "")
            if status in LIVE_STATUSES:
                skipped.append({"dynamic_id": dynamic_id, "why": f"当前状态「{status}」还在三连口径内，不归档"})
                continue
            if session.get(ActivityArchiveRow, dynamic_id) is not None:
                continue
            session.add(ActivityArchiveRow(dynamic_id=dynamic_id, archived_at=current, reason=reason))
            added.append(dynamic_id)
    return {"added": added, "skipped": skipped, "db": str(db_path())}


def unarchive(dynamic_ids: list[str]) -> dict:
    removed: list[str] = []
    with session_scope() as session:
        for dynamic_id in {str(x).strip() for x in dynamic_ids if str(x).strip()}:
            row = session.get(ActivityArchiveRow, dynamic_id)
            if row is None:
                continue
            session.delete(row)
            removed.append(dynamic_id)
    return {"removed": removed}
