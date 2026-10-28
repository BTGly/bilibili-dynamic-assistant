from __future__ import annotations

import time
from typing import Any

from src.activity_status import resolve_activity_status
from src.activity_store import load_payload
from src.db.snapshots import load_ds_check_dict, load_watch_sync_dict
from src.draw_reminder import matches_draw_window_filter
from src.lottery_classifier import PARTICIPATABLE_TYPES, is_charging_lottery_activity
from src.lottery_time import format_timestamp, is_activity_past_end, lottery_time_text, lottery_time_unix, participation_deadline_unix, classify_activity_timing, manual_time_approved, time_field_display
from src.participation_log import load_action_entries_for_uid
from src.participation_store import ParticipationRecord, load_participations
from src.sources.common import is_valid_dynamic_id
from src.state_store import DATA_DIR, get_last_pipeline_persisted

from src.watch_sync import OUTPUT_PATH as WATCH_OUTPUT_PATH

DS_OUTPUTS = {
    "DS-1": DATA_DIR / "output" / "ds1_latest.json",
    "DS-2": DATA_DIR / "output" / "ds2_latest.json",
    "DS-3": DATA_DIR / "output" / "ds3_latest.json",
    "DS-4": DATA_DIR / "output" / "ds4_latest.json",
    "DS-5": DATA_DIR / "output" / "ds5_latest.json",
    "DS-6": DATA_DIR / "output" / "ds6_latest.json",
    "DS-7": DATA_DIR / "output" / "ds7_latest.json",
    "DS-8": DATA_DIR / "output" / "ds8_latest.json",
    "DS-9": DATA_DIR / "output" / "ds9_latest.json",
    "WATCH": WATCH_OUTPUT_PATH,
}
SOURCE_LABELS = {
    "DS-1": "哔哩抽奖小助理",
    "DS-2": "番茄薯条喵",
    "DS-3": "你的抽奖工具人",
    "DS-4": "J君名",
    "DS-5": "互动抽奖娘",
    "DS-6": "糯米是个背包",
    "DS-7": "大锦鲤",
    "DS-8": "欠抽菌",
    "DS-9": "锦鲤送瑞",
    "WATCH": "监控用户",
}
SOURCE_SPACE_URLS = {
    "DS-1": "https://space.bilibili.com/885439/upload/video",
    "DS-2": "https://space.bilibili.com/3546836235193146/upload/opus",
    "DS-3": "https://space.bilibili.com/100680137/upload/opus",
    "DS-4": "https://space.bilibili.com/126038161/upload/opus",
    "DS-5": "https://space.bilibili.com/3546776042736296/upload/opus",
    "DS-6": "https://space.bilibili.com/492426375/upload/opus",
    "DS-7": "https://space.bilibili.com/226257459/upload/opus",
    "DS-8": "https://space.bilibili.com/1673762431/upload/video",
    "DS-9": "https://space.bilibili.com/99584491/upload/video",
    "WATCH": "",
}


def _load_source_snapshot(source_id: str) -> dict:
    if source_id == "WATCH":
        return load_watch_sync_dict() or {}
    return load_ds_check_dict(source_id) or {}


SOURCE_UPDATE_FRESH_SEC = 26 * 3600


def _source_updated_this_round(data: dict) -> bool:
    """「本次有更新」的判定：最近一轮检查确实发现新专栏，且检查时间还在一个刷新周期内。
    没有这条判断，停更的源会永远顶着最后一次更新时的徽章（快照过去只在有更新时才写）。"""
    if not data or not data.get("updated"):
        return False
    checked_at = int(data.get("checked_at") or 0)
    return bool(checked_at) and time.time() - checked_at <= SOURCE_UPDATE_FRESH_SEC


def _activity_title(item: dict) -> str:
    return _prize_summary(item) or str(item.get("dynamic_id") or "未知活动")


def _load_participation_actions() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for entry in load_action_entries_for_uid():
        if not isinstance(entry, dict):
            continue
        dynamic_id = str(entry.get("dynamic_id") or "")
        if dynamic_id:
            latest[dynamic_id] = entry
    return latest


def _prize_summary(item: dict) -> str:
    prizes = item.get("prizes") or []
    if not prizes:
        return ""
    first = prizes[0] if isinstance(prizes[0], dict) else {}
    description = str(first.get("description") or "").strip()
    count = int(first.get("winner_count") or 0)
    if description and count:
        return f"{description} ×{count}"
    return description or (f"×{count}" if count else "")


def _resolve_activity_status(
    item: dict, participation: ParticipationRecord | None, guards: dict[str, Any] | None = None
) -> str:
    if _guard_for(str(item.get("dynamic_id") or ""), guards).get("state") == "excluded":
        return "已排除"
    if is_charging_lottery_activity(item):
        return str(item.get("activity_status") or "未参加")
    return classify_activity_timing(item, participation)[0]


def _timing_hold(item: dict) -> bool:
    """人明确批过「继续搁着」（time_review.decision=defer）的行不自动参加；
    其余开奖时间未知的行按 10-26 的新政策直接进队列尾部。"""
    return (item.get("time_review") or {}).get("decision") == "defer"


def _can_participate(item: dict, activity_status: str) -> bool:
    # 10-26 起：开奖时间未知的「待审核」行也直接参加（人不再逐条审），
    # 由 _pick_triple_from_rows 把它们排到三连队列尾部。
    return (not item.get("skipped") and activity_status in ("未参加", "待审核")
            and not _timing_hold(item))


def _should_list_activity(item: dict, activity_status: str, can_participate: bool) -> bool:
    if is_charging_lottery_activity(item):
        return False
    if activity_status in ("已参加", "待审核", "已截止", "已排除"):
        return True
    if activity_status == "已结束":
        return True
    if activity_status == "未参加" and can_participate:
        return True
    return False


def invalidate_activity_cache() -> None:
    """兼容旧调用点；活动数据已走 DB，无需文件 mtime 缓存。"""
    return None


def _sort_key(item: dict, *, sort: str, order: str) -> tuple:
    if sort == "lottery_time":
        value = item.get("lottery_time_unix")
        return (value is None, (-value if order == "desc" else value) if value is not None else 0, str(item.get("dynamic_id") or ""))
    if sort == "heat":
        repost = int(item.get("repost_count") or 0)
        heat_key = -repost if order == "desc" else repost
        return (heat_key, str(item.get("dynamic_id") or ""))
    status_rank = {"未参加": 0, "待审核": 1, "已参加": 2, "已截止": 3, "已结束": 4}
    draw_rank = {"active": 0, "ended": 1}
    activity_status = str(item.get("activity_status") or "")
    draw_status = str(item.get("draw_status") or "")
    return (
        status_rank.get(activity_status, 9),
        draw_rank.get(draw_status, 9),
        str(item.get("dynamic_id") or ""),
    )


def _normalize_activity(
    item: dict,
    action_map: dict[str, dict],
    participation: ParticipationRecord | None,
    guards: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dynamic_id = str(item.get("dynamic_id") or "")
    account_guard = _account_guard(guards)
    guard = _guard_for(dynamic_id, guards)
    allowed = account_guard.get("state") == "ready"
    if allowed and guard.get("state") != "ready":
        # 只有可能被拦截的行才需要回源（其中含旧门槛误拦的自动恢复）
        from src.participation_guard import candidate_allowed, get_guard

        allowed = candidate_allowed(dynamic_id)
        guard = get_guard(dynamic_id)
    last_action = action_map.get(dynamic_id)
    activity_status = _resolve_activity_status(item, participation, guards)
    can_participate = _can_participate(item, activity_status)
    timing = classify_activity_timing(item, participation)
    draw_at = lottery_time_unix(item)
    repost_count = int(item.get("repost_count") or 0)
    repost_fetched = bool(item.get("repost_fetched"))
    heat_missing = not repost_fetched or (
        repost_count == 0 and not item.get("repost_zero_confirmed")
    )
    return {
        "dynamic_id": dynamic_id,
        "source_url": str(item.get("source_url") or ""),
        "activity_title": _activity_title(item),
        "lottery_type": str(item.get("lottery_type") or ""),
        "activity_status": activity_status,
        "activity_lifecycle_status": timing[0],
        "draw_tag": str(item.get("draw_tag") or ""),
        "draw_status": timing[1],
        "prize": _prize_summary(item),
        "repost_count": repost_count,
        "repost_fetched": repost_fetched,
        "heat_missing": heat_missing,
        "lottery_time": time_field_display(item, "lottery_time"),
        "lottery_time_unix": draw_at,
        "participation_deadline": time_field_display(item, "participation_deadline"),
        "participation_deadline_unix": participation_deadline_unix(item),
        "repost_chain_ids": [str(x) for x in ((item.get("conditions") or {}).get("repost_chain_ids") or [])],
        "lottery_time_evidence": (item.get("conditions") or {}).get("lottery_time_evidence", ""),
        "participation_deadline_evidence": (item.get("conditions") or {}).get("participation_deadline_evidence", ""),
        "time_review_required": draw_at is None,
        "time_review": item.get("time_review") or {},
        # 10-26 起不再要求「知道开奖时间」或人工批准才进自动三连：
        # 开奖时间未知的行照样入队，只排在有时间之后（见 _pick_triple_from_rows）。
        "auto_can_participate": can_participate and allowed,
        "check_at_recommended": False,
        "skipped": bool(item.get("skipped")),
        "skip_reason": str(item.get("skip_reason") or ""),
        "can_participate": can_participate and allowed,
        "participation_block_reason": (guard.get("reason") if guard.get("state") != "ready" else
                                       account_guard.get("reason") if account_guard.get("state") == "account_paused" else ""),
        "participation_guard": guard,
        "last_participation": {
            "status": last_action.get("status") if last_action else None,
            "message": last_action.get("message") if last_action else None,
            "recorded_at": last_action.get("recorded_at") if last_action else None,
            "actions": last_action.get("actions") if last_action else None,
        }
        if last_action
        else None,
    }


def _participatable_stored(item: dict) -> bool:
    return (
        isinstance(item, dict)
        and not item.get("skipped")
        and not is_charging_lottery_activity(item)
    )


def _load_activities_payload() -> dict:
    return load_payload()


DEFAULT_GUARD = {"state": "ready", "progress": {}, "reason": ""}


def _load_guard_cache() -> dict[str, Any]:
    """一次请求只读一遍 guard 表。

    列表要对全部活动逐行判状态，逐行 get_guard() 会把一次页面加载变成几千次开连接。
    """
    from src.participation_guard import get_guard, list_guards

    return {"account": get_guard(""), "by_id": list_guards()}


def _guard_for(dynamic_id: str, guards: dict[str, Any] | None) -> dict:
    if guards is None:
        from src.participation_guard import get_guard

        return get_guard(dynamic_id)
    return guards["by_id"].get(dynamic_id) or DEFAULT_GUARD


def _account_guard(guards: dict[str, Any] | None) -> dict:
    if guards is None:
        from src.participation_guard import get_guard

        return get_guard("")
    return guards["account"]


def get_summary() -> dict[str, Any]:
    enriched = _load_activities_payload()
    participations = load_participations()
    activities = [
        item for item in (enriched.get("activities") or []) if _participatable_stored(item)
    ]
    status_counts = {"已结束": 0, "已参加": 0, "未参加": 0, "待审核": 0, "已截止": 0, "已排除": 0}
    draw_counts = {"active": 0, "ended": 0, "unknown": 0}
    guards = _load_guard_cache()
    for item in activities:
        dynamic_id = str(item.get("dynamic_id") or "")
        timing = classify_activity_timing(item, participations.get(dynamic_id))
        status = _resolve_activity_status(item, participations.get(dynamic_id), guards)
        if status in status_counts:
            status_counts[status] += 1
        draw_counts[timing[1]] += 1

    sources: list[dict[str, Any]] = []
    for source_id, _path in DS_OUTPUTS.items():
        if source_id == "WATCH":
            continue
        data = _load_source_snapshot(source_id)
        checked_at = data.get("checked_at") or data.get("synced_at")
        title = str(data.get("title") or "")
        sources.append(
            {
                "id": source_id,
                "name": SOURCE_LABELS.get(source_id, source_id),
                "updated": _source_updated_this_round(data),
                "link_count": len(data.get("activity_links") or []),
                "title": title,
                "checked_at": checked_at,
                "checked_at_text": format_timestamp(int(checked_at)) if checked_at else "尚未更新",
                "space_url": SOURCE_SPACE_URLS.get(source_id, ""),
                "container_url": str(data.get("container_url") or ""),
            }
        )
    last_pipeline = get_last_pipeline_persisted()
    return {
        "enriched_at": enriched.get("updated_at") or enriched.get("enriched_at"),
        "total_count": len(activities),
        "new_count": last_pipeline["persisted_count"],
        "last_pipeline_sync": last_pipeline,
        "user_status_counts": status_counts,
        "counts": draw_counts,
        "sources": sources,
    }


PARTICIPATE_TRIPLE_LIMIT = 3
ACTIVITY_PAGE_SIZE = 20


def participate_step_budget(lottery_type: str, *, dynamic_id: str | None = None) -> int:
    """单次参与在进度条上占用的步数（预约抽奖为关注 + 预约共 2 步）。"""
    normalized = (lottery_type or "").strip()
    if normalized == "预约抽奖":
        return 2
    if normalized == "转发抽奖":
        return 4
    if normalized == "互动抽奖":
        return 5
    if dynamic_id:
        try:
            resolved = lookup_lottery_type(dynamic_id)
            return 2 if resolved == "预约抽奖" else 4 if resolved == "转发抽奖" else 5
        except RuntimeError:
            pass
    return 5


def build_triple_progress_plan(
    targets: list[dict[str, Any]],
) -> tuple[int, dict[str, tuple[int, int]]]:
    """返回总步数与各活动的 (offset, budget) 映射。"""
    plan: dict[str, tuple[int, int]] = {}
    offset = 0
    for target in targets:
        dynamic_id = str(target.get("dynamic_id") or "").strip()
        if not dynamic_id:
            continue
        budget = participate_step_budget(str(target.get("lottery_type") or ""), dynamic_id=dynamic_id)
        plan[dynamic_id] = (offset, budget)
        offset += budget
    return offset, plan


def _filtered_activity_rows(
    *,
    status: str | None = None,
    lottery_type: str | None = None,
    draw: str | None = None,
    draw_window: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    archive: str = "all",
) -> list[dict[str, Any]]:
    from src.activity_archive import archived_map

    from src.activity_serials import assign_with_session, map_all, per_up_index, rows_with_uid
    from src.db.session import session_scope

    marks = archived_map()
    with session_scope() as _session:
        serials = assign_with_session(_session, [str((i or {}).get("dynamic_id") or "") for i in
                                                 (_load_activities_payload().get("activities") or []) if isinstance(i, dict)])
        _session.commit()
    serials = serials or map_all()
    uids = {did: uid for did, _seq, uid in rows_with_uid()}
    up_index = per_up_index()
    enriched = _load_activities_payload()
    action_map = _load_participation_actions()
    participations = load_participations()
    guards = _load_guard_cache()
    items = [item for item in (enriched.get("activities") or []) if isinstance(item, dict)]

    normalized: list[dict[str, Any]] = []
    draw_window_value = (draw_window or "").strip().lower()
    if draw_window_value not in {"", "soon"}:
        draw_window_value = ""
    for item in items:
        dynamic_id = str(item.get("dynamic_id") or "")
        participation = participations.get(dynamic_id)
        if draw_window_value and not matches_draw_window_filter(
            item, participation, draw_window_value
        ):
            continue
        activity_status = _resolve_activity_status(item, participation, guards)
        can_participate = _can_participate(item, activity_status)
        if not _should_list_activity(item, activity_status, can_participate):
            continue
        row = _normalize_activity(item, action_map, participation, guards)
        row["seq"] = serials.get(str(row.get("dynamic_id") or ""))
        row["sender_uid"] = int(uids.get(str(row.get("dynamic_id") or "")) or 0)
        row["up_seq"] = up_index.get(str(row.get("dynamic_id") or ""))
        mark = marks.get(str(row.get("dynamic_id") or "")) or {}
        row["archived"] = bool(mark)
        row["archived_at"] = int(mark.get("archived_at") or 0)
        row["archived_reason"] = str(mark.get("reason") or "")
        normalized.append(row)

    if draw_window_value:
        status = None
    if status:
        normalized = [item for item in normalized if item.get("activity_status") == status]
    if lottery_type:
        normalized = [item for item in normalized if item.get("lottery_type") == lottery_type]
    if draw and not status:
        normalized = [item for item in normalized if item.get("draw_status") == draw]
    if q:
        keyword = q.strip().lower()
        if keyword:

            def _matches(item: dict) -> bool:
                fields = [
                    f"#{item.get('seq')}" if item.get("seq") else "",
                    str(item.get("seq") or ""),
                    str(item.get("dynamic_id") or ""),
                    str(item.get("activity_title") or ""),
                    str(item.get("prize") or ""),
                    str(item.get("skip_reason") or ""),
                    str(item.get("lottery_time") or ""),
                ]
                return any(keyword in field.lower() for field in fields if field)

            normalized = [item for item in normalized if _matches(item)]

    tier = (archive or "all").strip().lower()
    if tier == "active":
        normalized = [item for item in normalized if not item.get("archived")]
    elif tier == "archived":
        normalized = [item for item in normalized if item.get("archived")]

    sort_mode = (sort or "default").strip().lower()
    sort_order = (order or "desc").strip().lower()
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"
    if sort_mode not in ("heat", "default", "lottery_time"):
        sort_mode = "default"
    normalized.sort(key=lambda item: _sort_key(item, sort=sort_mode, order=sort_order))
    return normalized


def _pick_triple_from_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int = PARTICIPATE_TRIPLE_LIMIT,
) -> list[dict[str, Any]]:
    from src.participation_guard import candidate_allowed

    targets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_chains: set[str] = set()

    def _chain_key(row: dict[str, Any]) -> str:
        # 转发链根 id；本体动态用自己当根，这样「原动态 + 转发它的抽奖」会并成一条链。
        chain = [str(x) for x in (row.get("repost_chain_ids") or []) if str(x)]
        return chain[-1] if chain else str(row.get("dynamic_id") or "")

    for row in rows:
        # 开奖时间未知（面板口径「待审核」）的行也进队列，排在有时间之后
        if str(row.get("activity_status") or "") not in ("未参加", "待审核"):
            continue
        if not row.get("can_participate") or row.get("auto_can_participate") is False:
            continue
        dynamic_id = str(row.get("dynamic_id") or "").strip()
        if not is_valid_dynamic_id(dynamic_id) or dynamic_id in seen_ids:
            continue
        row_lottery_type = str(row.get("lottery_type") or "")
        if row_lottery_type not in PARTICIPATABLE_TYPES:
            continue
        if not candidate_allowed(dynamic_id):
            continue
        # 最终判定只看「动态链接本身」：同一个 UP 短时间发几场抽奖是正常的，不能按 UP 去重；
        # 只有链根相同（同一条抽奖换了壳）才算重复，那仍然是链接层面的身份。
        chain_key = _chain_key(row)
        if chain_key in seen_chains:
            # 同一条转发链一轮里只发一次：B 站会把第二次转发压到同一个根动态上，
            # 在页面上就是「同一条被转发两遍」，重复转发不计参与。
            continue
        seen_chains.add(chain_key)
        seen_ids.add(dynamic_id)
        targets.append(row)
    # 排队优先级：有开奖时间的按「先开奖先参加」；人工批准进来的次之；
    # 开奖时间未知的排到后面，其中「只写了参与截止」的再靠前——截止日是原文给的确定信息，
    # 过了截止就必然开奖；连截止都没写的（正文真的什么都没有）排最后。
    manual_approved = lambda item: (item.get("time_review") or {}).get("decision") == "allow_manual"
    targets.sort(
        key=lambda item: (
            item.get("lottery_time_unix") is None,
            manual_approved(item),
            item.get("lottery_time_unix") or 0,
            item.get("participation_deadline_unix") is None,
            item.get("participation_deadline_unix") or 0,
            str(item.get("dynamic_id") or ""),
        )
    )
    return targets[:limit]


def _format_triple_target_items(targets: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "dynamic_id": str(item.get("dynamic_id") or ""),
            "activity_title": str(item.get("activity_title") or item.get("prize") or "未知活动"),
            "lottery_type": str(item.get("lottery_type") or ""),
            "activity_status": str(item.get("activity_status") or ""),
        }
        for item in targets
    ]


def build_triple_target_preview(targets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(targets),
        "limit": PARTICIPATE_TRIPLE_LIMIT,
        "items": _format_triple_target_items(targets),
    }


def pick_triple_participate_targets(
    *,
    status: str | None = None,
    lottery_type: str | None = None,
    draw: str | None = None,
    draw_window: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = PARTICIPATE_TRIPLE_LIMIT,
) -> list[dict[str, Any]]:
    """按当前列表筛选与排序，取最前面可参与的未参加活动（默认最多 3 个）。"""
    # 归档只是列表分层，三连的取数一律看全量：被归档的行本来就不在参加口径内，
    # 但队列的数量、顺序必须与归档前完全一致。
    rows = _filtered_activity_rows(
        status=status,
        lottery_type=lottery_type,
        draw=draw,
        draw_window=draw_window,
        q=q,
        sort=sort,
        order=order,
        archive="all",
    )
    return _pick_triple_from_rows(rows, limit=limit)


def summarize_triple_participate_targets(
    *,
    status: str | None = None,
    lottery_type: str | None = None,
    draw: str | None = None,
    draw_window: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
) -> dict[str, Any]:
    """预览三连参与将命中的活动（供前端展示按钮状态）。"""
    targets = pick_triple_participate_targets(
        status=status,
        lottery_type=lottery_type,
        draw=draw,
        draw_window=draw_window,
        q=q,
        sort=sort,
        order=order,
    )
    return build_triple_target_preview(targets)


def list_activities(
    *,
    status: str | None = None,
    lottery_type: str | None = None,
    draw: str | None = None,
    draw_window: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    page: int = 1,
    page_size: int = ACTIVITY_PAGE_SIZE,
    archive: str = "active",
) -> dict[str, Any]:
    from src.activity_archive import stats as archive_stats

    all_rows = _filtered_activity_rows(
        status=status,
        lottery_type=lottery_type,
        draw=draw,
        draw_window=draw_window,
        q=q,
        sort=sort,
        order=order,
        archive="all",
    )
    tier = (archive or "active").strip().lower()
    if tier == "active":
        normalized = [item for item in all_rows if not item.get("archived")]
    elif tier == "archived":
        normalized = [item for item in all_rows if item.get("archived")]
    else:
        normalized = all_rows
    total = len(normalized)
    page = max(1, page)
    page_size = ACTIVITY_PAGE_SIZE
    start = (page - 1) * page_size
    page_items = normalized[start : start + page_size]
    # 三连预览按全量算：归档只给列表分层，绝不能改变队列的数量与顺序
    triple_targets = build_triple_target_preview(_pick_triple_from_rows(all_rows))

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": page_items,
        "triple_targets": triple_targets,
        "archive": {**archive_stats(), "tier": tier},
    }


def lookup_lottery_type(dynamic_id: str) -> str:
    enriched = _load_activities_payload()
    for item in enriched.get("activities") or []:
        if str(item.get("dynamic_id") or "") == dynamic_id:
            lottery_type = str(item.get("lottery_type") or "")
            if lottery_type in PARTICIPATABLE_TYPES:
                return lottery_type
    raise RuntimeError(f"未找到活动 {dynamic_id} 的类型信息")


def resolve_participate_lottery_type(dynamic_id: str, *, hint: str | None = None) -> str:
    """参与时以 enriched 缓存中的类型为准，列表类型仅作兜底。"""
    hinted = str(hint or "").strip()
    try:
        return lookup_lottery_type(dynamic_id)
    except RuntimeError:
        if hinted in PARTICIPATABLE_TYPES:
            return hinted
        raise
