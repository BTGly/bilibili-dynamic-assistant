"""活动序号：唯一、稳定、不回收，并且面板能按 #号搜到。"""
from __future__ import annotations

import time

from src.activity_serials import assign, backfill_all, for_id, id_for_seq, map_all
from src.activity_store import replace_all_activities, update_activity
from web.activity_service import list_activities, pick_triple_participate_targets

A = "1200000000000000101"
B = "1200000000000000102"


def item(dynamic_id: str, *, when: int | None = None, **extra) -> dict:
    base = {"dynamic_id": dynamic_id, "lottery_type": "转发抽奖",
            "lottery_time": when or int(time.time()) + 86400, "activity_status": "未参加",
            "draw_status": "active", "prizes": [{"description": "键盘", "winner_count": 1}],
            "conditions": {}, "enriched_at": when or int(time.time())}
    base.update(extra)
    return base


def test_serials_are_unique_and_survive_row_rewrites():
    replace_all_activities([item(A, when=1_700_000_000), item(B, when=1_700_000_500)])
    backfill_all()
    first = for_id(A)
    assert first and for_id(B) == first + 1          # 按入库先后，老活动号小
    # 富化整行覆写不能把序号冲掉（这就是不复用 activities 加列的原因）
    update_activity(A, {**item(A, when=1_700_000_000), "activity_status": "已参加"})
    assert for_id(A) == first
    replace_all_activities([item(A, when=1_700_000_000)])
    assert for_id(A) == first


def test_deleted_activity_never_has_its_number_reused():
    replace_all_activities([item(A), item(B)])
    backfill_all()
    high = max(map_all().values())
    replace_all_activities([item(A)])
    fresh = assign(["1200000000000000999"])[ "1200000000000000999"]
    assert fresh > high
    assert id_for_seq(high) is not None


def test_panel_shows_seq_and_search_by_hash_number_works():
    replace_all_activities([item(A), item(B)])
    backfill_all()
    rows = list_activities(page_size=20)["items"]
    assert {row["seq"] for row in rows} == {for_id(A), for_id(B)}
    target = for_id(A)
    hit = list_activities(q=f"#{target}")["items"]
    assert [row["dynamic_id"] for row in hit] == [A]
    assert list_activities(q="不可能存在的奖品名")["items"] == []


def test_same_up_different_draws_are_all_picked():
    """UP uid 只用来归类与查找；一个 UP 短时间发几场抽奖是常态，去重只看链接身份。"""
    a = "1200000000000000301"
    b = "1200000000000000302"
    c = "1200000000000000303"
    replace_all_activities([item(a), item(b), item(c)])
    from src.activity_serials import bind_uid

    bind_uid(a, 700001)
    bind_uid(b, 700001)          # 同一 UP 的第二条
    bind_uid(c, 700002)          # 另一个 UP
    picked = [row["dynamic_id"] for row in pick_triple_participate_targets(limit=5)]
    assert sorted(picked) == sorted([a, b, c])          # 三条都排，不按 UP 去重
