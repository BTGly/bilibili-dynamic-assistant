"""归档必须只影响列表分层，不影响三连队列；并且绝不吞掉还能参加的行。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from src.activity_archive import archive, archived_map, candidates, stats, unarchive
from src.activity_store import replace_all_activities
from src.lottery_time import BEIJING_TZ
from web.activity_service import list_activities, pick_triple_participate_targets

PAST = "1200000000000000001"
LIVE = "1200000000000000002"
FUTURE = "1200000000000000003"
JOINED = "1200000000000000004"


def ts(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=BEIJING_TZ).timestamp())


def ahead(days: int) -> int:
    """锚在「现在」之后的开奖时间——写死日历日期的行跨过那天会被判已结束，用例自己烂掉。"""
    base = (datetime.now(BEIJING_TZ) + timedelta(days=days)).replace(
        hour=20, minute=0, second=0, microsecond=0
    )
    return int(base.timestamp())


import pytest


@pytest.fixture(autouse=True)
def _clear_archive_marks():
    from src.activity_archive import archived_map as _map
    from src.activity_archive import unarchive as _rm

    _rm(list(_map()))
    yield
    _rm(list(_map()))


def item(dynamic_id: str, *, status: str, when: int | None, **extra) -> dict:
    base = {"dynamic_id": dynamic_id, "lottery_type": "转发抽奖", "lottery_time": when,
            "activity_status": status, "draw_status": "active",
            "prizes": [{"description": "键盘", "winner_count": 1}], "conditions": {}}
    base.update(extra)
    return base


def fixture_rows() -> None:
    replace_all_activities([
        item(PAST, status="已结束", when=ts("2026-10-01 20:00")),
        item(LIVE, status="未参加", when=ahead(3)),
        item(FUTURE, status="未参加", when=ahead(30)),
        item(JOINED, status="已参加", when=ts("2026-10-05 20:00")),
    ])


def test_rules_only_touch_rows_that_cannot_be_joined():
    fixture_rows()
    ids = {row["dynamic_id"] for row in candidates()}
    assert ids == {PAST, JOINED}          # 未参加的两条绝不进归档候选


def test_archiving_leaves_the_triple_queue_identical():
    fixture_rows()
    before = [t["dynamic_id"] for t in pick_triple_participate_targets(limit=5)]
    assert before == [LIVE, FUTURE]
    result = archive([PAST, JOINED, LIVE], reason="规则归档")
    assert LIVE not in result["added"]                      # 还在三连口径内，被挡下
    assert any(x["dynamic_id"] == LIVE and "三连" in x["why"] for x in result["skipped"])
    after = [t["dynamic_id"] for t in pick_triple_participate_targets(limit=5)]
    assert after == before
    assert set(archived_map()) == {PAST, JOINED}


def test_default_list_hides_archived_but_all_tier_and_search_still_find_them():
    fixture_rows()
    archive([PAST, JOINED], reason="规则归档")
    active_ids = {i["dynamic_id"] for i in list_activities(page_size=20)["items"]}
    assert PAST not in active_ids and JOINED not in active_ids
    assert list_activities(archive="archived")["total"] == 2
    everything = {i["dynamic_id"] for i in list_activities(archive="all", page_size=20)["items"]}
    assert {PAST, JOINED, LIVE, FUTURE} <= everything
    # 面板默认返回里带上归档统计与档位
    payload = list_activities()
    assert payload["archive"]["archived"] == 2 and payload["archive"]["tier"] == "active"
    assert payload["items"] and all("archived" in row for row in payload["items"])


def test_unarchive_restores_rows_and_marks_are_idempotent():
    fixture_rows()
    first = archive([PAST], reason="规则归档")
    second = archive([PAST], reason="重复归档")
    assert first["added"] == [PAST] and second["added"] == []
    assert len(stats()["by_month"]) >= 1
    removed = unarchive([PAST])["removed"]
    assert removed == [PAST] and not archived_map()


def test_grace_day_keeps_recent_joined_rows_visible():
    fixture_rows()
    now = int(time.time())
    replace_all_activities([item(JOINED, status="已参加", when=now - 3600)])
    assert JOINED in {row["dynamic_id"] for row in candidates(joined_grace_days=0)}
    assert JOINED not in {row["dynamic_id"] for row in candidates(joined_grace_days=3)}
