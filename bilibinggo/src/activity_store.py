from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import delete, func
from sqlmodel import col, select

from src.db.activity_codec import activity_dict_to_row, row_to_activity_dict
from src.db.models import ActivityRow, ForwardParseCacheRow
from src.db.json_cols import loads_json
from src.db.session import session_scope
from src.lottery_classifier import is_charging_lottery_activity
from src.lottery_time import normalize_activity_timing
from src.sources.common import normalize_activity_id
from src.state_store import DATA_DIR

# 路径常量保留供导入脚本 / 测试引用（运行时不再写入）
ACTIVITIES_OUTPUT_PATH = DATA_DIR / "output" / "activities_latest.json"
LEGACY_ENRICHED_PATH = DATA_DIR / "output" / "enriched_latest.json"
_activity_lock = threading.RLock()


def activity_file_lock() -> threading.RLock:
    """活动库进程内锁；串行化本地读写，不阻塞网络请求。"""
    return _activity_lock


def _empty_payload() -> dict[str, Any]:
    return {
        "updated_at": 0,
        "activities": [],
        "counts": {"active": 0, "ended": 0, "unknown": 0},
        "user_status_counts": {"已结束": 0, "已参加": 0, "未参加": 0, "待审核": 0, "已截止": 0},
        "draw_tag_counts": {"即将开奖": 0},
    }


def _rebuild_counts(activities: list[dict]) -> dict[str, Any]:
    counts = {"active": 0, "ended": 0, "unknown": 0}
    user_status = {"已结束": 0, "已参加": 0, "未参加": 0, "待审核": 0, "已截止": 0}
    draw_tag_counts = {"即将开奖": 0}
    for item in activities:
        if is_charging_lottery_activity(item) or item.get("skipped"):
            continue
        if item.get("draw_status") == "ended":
            counts["ended"] += 1
        elif item.get("draw_status") == "active":
            counts["active"] += 1
        else:
            counts["unknown"] += 1
        status = str(item.get("activity_status") or "")
        if status in user_status:
            user_status[status] += 1
        tag = str(item.get("draw_tag") or "")
        if tag in draw_tag_counts:
            draw_tag_counts[tag] += 1
    return {
        "counts": counts,
        "user_status_counts": user_status,
        "draw_tag_counts": draw_tag_counts,
    }


def _normalize_ended_by_time(item: dict, *, now: int | None = None) -> bool:
    changed = False
    if str(item.get("draw_tag") or "") == "已开奖":
        item["draw_tag"] = ""
        changed = True
    if not item.get("skipped"):
        changed = normalize_activity_timing(item, now=now) or changed
    return changed


def _list_activity_dicts(session) -> list[dict]:
    rows = session.exec(select(ActivityRow).order_by(col(ActivityRow.dynamic_id))).all()
    return [row_to_activity_dict(row) for row in rows]


def _upsert_activities(session, items: list[dict], *, updated_at: int) -> None:
    for item in items:
        dynamic_id = str(item.get("dynamic_id") or "").strip()
        if not dynamic_id:
            continue
        row = activity_dict_to_row(item, updated_at=updated_at)
        existing = session.get(ActivityRow, dynamic_id)
        if existing is None:
            session.add(row)
        else:
            for name in ActivityRow.model_fields:
                if name == "dynamic_id":
                    continue
                setattr(existing, name, getattr(row, name))
        # 每条活动一个永久序号（#N），你报号就能指到具体活动；放独立表里，
        # 不会被富化整行覆写冲掉，也不会回收复用。
        from src.activity_serials import assign_with_session
        assign_with_session(session, [dynamic_id])


def _replace_all_unlocked(activities: list[dict]) -> None:
    now = int(time.time())
    with session_scope() as session:
        session.exec(delete(ActivityRow))
        session.flush()
        _upsert_activities(session, activities, updated_at=now)


def _seed_activities_if_empty() -> bool:
    with session_scope() as session:
        existing = session.exec(select(ActivityRow).limit(1)).first()
        if existing is not None:
            return False
    from src.activity_seed import read_seed_activities

    activities = read_seed_activities()
    if not activities:
        return False
    _replace_all_unlocked(activities)
    return True


def seed_activities_if_empty() -> bool:
    with _activity_lock:
        return _seed_activities_if_empty()


def _load_payload_unlocked() -> dict[str, Any]:
    _seed_activities_if_empty()
    with session_scope() as session:
        rows = session.exec(select(ActivityRow).order_by(col(ActivityRow.dynamic_id))).all()
        max_updated = max((int(row.updated_at or 0) for row in rows), default=0)
        activities = [row_to_activity_dict(row) for row in rows]
        old_model_dates = set()
        for cache in session.exec(select(ForwardParseCacheRow)).all():
            parsed = loads_json(cache.parsed_json, {})
            if isinstance(parsed, dict) and isinstance(parsed.get("parser_version"), int) and parsed["parser_version"] < 8:
                old_model_dates.add(cache.dynamic_id)
        changed_items: list[dict] = []
        for item in activities:
            changed = quarantine_legacy_future_date(item, old_model_dates)
            if _normalize_ended_by_time(item) or changed:
                changed_items.append(item)
        if changed_items:
            now = int(time.time())
            _upsert_activities(session, changed_items, updated_at=now)
            max_updated = max(max_updated, now)
            activities = _list_activity_dicts(session)
    payload = _empty_payload()
    payload["activities"] = activities
    payload["updated_at"] = max_updated
    payload.update(_rebuild_counts(activities))
    return payload


def quarantine_legacy_future_date(item: dict, old_model_dates: set[str], *, now: int | None = None) -> bool:
    """Keep old values for audit; unverified v7 model dates cannot enable participation."""
    from src.lottery_time import lottery_time_unix
    conditions = item.get("conditions") or {}
    timestamp = lottery_time_unix(item)
    if (item.get("lottery_type") != "转发抽奖" or item.get("dynamic_id") not in old_model_dates
            or timestamp is None or timestamp <= (int(time.time()) if now is None else now)
            or conditions.get("lottery_time_source") or conditions.get("lottery_time_evidence")):
        return False
    conditions = dict(conditions)
    conditions["lottery_time_source"] = "legacy_unverified"
    conditions["legacy_lottery_time_text"] = conditions.get("lottery_time_text") or str(timestamp)
    item["conditions"] = conditions
    return True


def load_payload() -> dict[str, Any]:
    with _activity_lock:
        return _load_payload_unlocked()


def load_activities() -> list[dict]:
    return [item for item in (load_payload().get("activities") or []) if isinstance(item, dict)]


def known_activity_ids() -> set[str]:
    with session_scope() as session:
        rows = session.exec(select(ActivityRow.dynamic_id)).all()
        return {str(item) for item in rows if item}


def activity_exists(dynamic_id: str) -> bool:
    did = str(dynamic_id or "").strip()
    if not did:
        return False
    with session_scope() as session:
        return session.get(ActivityRow, did) is not None


def append_activities(new_items: list[dict]) -> int:
    if not new_items:
        return 0
    with _activity_lock:
        with session_scope() as session:
            existing_ids = set(session.exec(select(ActivityRow.dynamic_id)).all())
            to_add: list[dict] = []
            for item in new_items:
                dynamic_id = str(item.get("dynamic_id") or "").strip()
                if not dynamic_id or dynamic_id in existing_ids:
                    continue
                existing_ids.add(dynamic_id)
                to_add.append(item)
            if not to_add:
                return 0
            _upsert_activities(session, to_add, updated_at=int(time.time()))
            return len(to_add)


# 整表替换的保险丝：生产路径传进来的永远是"全量列表"，一次少掉一大半必然是误操作。
# 10-29 03:29 我拿临时脚本直接调 replace_all_activities([两条假数据])，把 1962 行库刷成 2 行。
MASS_REPLACE_MIN_ROWS = 500


def replace_all_activities(activities: list[dict], *, allow_mass_replace: bool = False) -> None:
    rows = sorted(
        [item for item in activities if isinstance(item, dict) and item.get("dynamic_id")],
        key=lambda row: str(row.get("dynamic_id")),
    )
    if not allow_mass_replace:
        from src.test_isolation import under_test_isolation

        if not under_test_isolation():
            with session_scope() as session:
                current = int(session.exec(select(func.count()).select_from(ActivityRow)).one())
            if len(rows) < MASS_REPLACE_MIN_ROWS and current >= MASS_REPLACE_MIN_ROWS and len(rows) < current // 2:
                raise RuntimeError(
                    f"拒绝把 {current} 条活动一次性替换成 {len(rows)} 条（看起来是误操作整表覆写）；"
                    f"确认要这么做就显式传 allow_mass_replace=True"
                )
    with _activity_lock:
        _replace_all_unlocked(rows)


def remove_activity_ids(dynamic_ids: set[str]) -> int:
    if not dynamic_ids:
        return 0
    with _activity_lock:
        with session_scope() as session:
            removed = 0
            for dynamic_id in dynamic_ids:
                row = session.get(ActivityRow, str(dynamic_id))
                if row is not None:
                    session.delete(row)
                    removed += 1
            return removed


def update_activity(dynamic_id: str, item: dict) -> None:
    with _activity_lock:
        with session_scope() as session:
            payload = dict(item)
            payload["dynamic_id"] = dynamic_id
            _upsert_activities(session, [payload], updated_at=int(time.time()))


def collect_urls_from_ids(dynamic_ids: set[str]) -> set[str]:
    return {normalize_activity_id(did) or did for did in dynamic_ids if did}


def activity_count() -> int:
    with session_scope() as session:
        return int(session.exec(select(func.count()).select_from(ActivityRow)).one())
