"""「开奖时间未知」不等于"还来得及"：证据日期已过或发布太旧的行不再自动参加。

10-29 实测：一加16 那 6 条正文只写「10 月 12 日 19:00（发布会）」，被非开奖屏蔽判成未知，
于是 10-29 还在转发 10-12 就该结束的活动，6 次全无效。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from src.lottery_time import BEIJING_TZ
from src.activity_store import replace_all_activities
from web.activity_service import (
    list_activities,
    pick_triple_participate_targets,
    unknown_time_is_stale,
)

LIVE = "1200000000000000101"
STALE = "1200000000000000102"
ANCIENT = "1200000000000000103"
FUTURE = "1200000000000000104"


def ts(days_from_now: float) -> int:
    return int(time.time() + days_from_now * 86400)


def item(did: str, *, evidence: str = "", ref_days: float = 0.0, when: int | None = None) -> dict:
    return {"dynamic_id": did, "lottery_type": "转发抽奖", "lottery_time": when,
            "activity_status": "待审核" if when is None else "未参加",
            "draw_status": "active",
            "conditions": {"lottery_time_evidence": evidence, "time_reference_ts": ts(ref_days)},
            "enriched_at": ts(ref_days)}


def test_row_view_carries_the_publish_anchor() -> None:
    replace_all_activities([item(LIVE, evidence="10 月 12 日 19:00", ref_days=-1)])
    row = list_activities(page_size=5)["items"][0]
    assert row["time_reference_ts"], "视图必须带上发布锚点，否则过期判断无从算起"


def test_evidence_date_already_past_is_stale() -> None:
    row = {"lottery_time_unix": None, "lottery_time_evidence": "10 月 12 日 19:00",
           "time_reference_ts": ts(-19)}
    assert "已过" in unknown_time_is_stale(row)


def test_evidence_date_in_the_future_is_not_stale() -> None:
    future = (datetime.now(BEIJING_TZ) + timedelta(days=6)).strftime("%m 月 %d 日") + " 19:00"
    row = {"lottery_time_unix": None, "lottery_time_evidence": future, "time_reference_ts": ts(-1)}
    assert unknown_time_is_stale(row) == ""


def test_no_evidence_is_never_called_stale_however_old() -> None:
    """10-29 你否掉了"发布超 14 天就不参加"：正文没写时间的活动多老都可能仍在抽，天数不是证据。"""
    assert unknown_time_is_stale({"lottery_time_unix": None, "time_reference_ts": ts(-30)}) == ""
    assert unknown_time_is_stale({"lottery_time_unix": None, "time_reference_ts": ts(-2)}) == ""


def test_known_future_draw_is_never_called_stale() -> None:
    row = {"lottery_time_unix": ts(3), "lottery_time_evidence": "10 月 12 日 19:00", "time_reference_ts": ts(-40)}
    assert unknown_time_is_stale(row) == ""


def test_stale_unknown_rows_are_kept_out_of_the_triple_queue() -> None:
    replace_all_activities([
        item(STALE, evidence="10 月 12 日 19:00", ref_days=-19),      # 证据日期已过 → 不参加
        item(ANCIENT, evidence="", ref_days=-30),                     # 很旧但没写时间 → 照参加
        item(LIVE, evidence="", ref_days=-1),                         # 未知但很新 → 该参加
        item(FUTURE, evidence="", ref_days=-1, when=ts(5)),           # 有未来开奖时间
    ])
    picked = {row["dynamic_id"] for row in pick_triple_participate_targets(limit=10)}
    assert picked == {LIVE, FUTURE, ANCIENT}
    assert STALE not in picked
