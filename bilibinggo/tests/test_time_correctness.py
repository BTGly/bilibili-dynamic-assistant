from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from src.activity_store import replace_all_activities, load_activities
from src.db.engine import db_path
from src.draw_reminder import compute_draw_reminders, matches_draw_window_filter
from src.lottery_time import BEIJING_TZ, classify_activity_timing, lottery_time_unix, lottery_time_text
from src.participation_store import ParticipationRecord
from src.time_evidence import validate_extracted_times
from src.time_repair import preview_time_repairs, apply_time_repairs
from src.time_review import review_activity_time, ensure_timing_allows_manual_participation
from web.activity_service import list_activities, pick_triple_participate_targets, _sort_key

TARGET = "100000000000000001"


def timestamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=BEIJING_TZ).timestamp())


def ahead(days, *, hour=12):
    """相对「现在」的时间戳。

    排队顺序的用例原来写死了日历日期，跨过那一天就自己烂掉（10-28 00:26 就挂过一次：
    被判「已过期」而从队列消失，测试却仍期望它在队里）。顺序规则与日历无关，所以锚在 now。
    """
    base = (datetime.now(BEIJING_TZ) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return int(base.timestamp())


def activity(did=TARGET, *, inferred=False, joined=False, when=None):
    return {"dynamic_id": did, "lottery_type": "转发抽奖", "lottery_time": when,
            "draw_status": "active", "activity_status": "已参加" if joined else "未参加",
            "conditions": {"lottery_time_inferred": True} if inferred else {}}


def test_inferred_dates_are_unknown_in_every_consumer():
    now = timestamp("2026-10-09 12:00")
    item = activity(inferred=True, when=now + 3600, joined=True)
    item.update(draw_tag="即将开奖", status_classified=True)
    record = ParticipationRecord(TARGET, "已参加", now - 100)
    assert lottery_time_unix(item) is None
    assert lottery_time_text(item) == ""
    assert classify_activity_timing(item, record, now=now) == ("已参加", "unknown", True)
    assert compute_draw_reminders([item], {TARGET: record}, now=now)["drawing_soon_count"] == 0
    assert not matches_draw_window_filter(item, record, "soon", now=now)


def test_platform_ended_activity_never_creates_a_future_reminder():
    now = timestamp("2026-10-09 12:00")
    item = activity(when=now + 3600, joined=True)
    item.update(draw_status="ended", draw_status_source="platform")
    record = ParticipationRecord(TARGET, "已参加", now - 100)
    assert compute_draw_reminders([item], {TARGET: record}, now=now)["drawing_soon_count"] == 0


def test_unknown_time_enters_auto_queue_without_approval_or_invented_dates():
    """10-26 新政策：开奖时间未知的行不用再人工批准，直接进三连队列，
    但依然不给它编一个开奖日期（面板仍显示「未知」）。"""
    replace_all_activities([activity()])
    row = list_activities()["items"][0]
    assert row["activity_status"] == "待审核"
    assert row["lottery_time"] == "未知"
    ensure_timing_allows_manual_participation(TARGET)
    assert [t["dynamic_id"] for t in pick_triple_participate_targets()] == [TARGET]
    row = list_activities()["items"][0]
    assert row["can_participate"] and row["auto_can_participate"]
    assert row["lottery_time"] == "未知"


def test_unknown_time_rows_queue_behind_known_draw_times():
    """队尾顺序：有时间的先参加，开奖时间未知的排最后。"""
    known_late = activity("1200000000000000303", when=ahead(18))
    known_soon = activity("1200000000000000101", when=ahead(1))
    unknown = activity("1200000000000000202", when=None)
    replace_all_activities([unknown, known_late, known_soon])
    picked = [t["dynamic_id"] for t in pick_triple_participate_targets(limit=3)]
    assert picked == ["1200000000000000101", "1200000000000000303", "1200000000000000202"]


def test_deadline_only_rows_queue_ahead_of_rows_with_no_time_at_all():
    """只写了参与截止的行排在「什么都没说」的行前面：截止是原句给的确定信息，过了截止必然开奖。"""
    soon_ts, late_ts = ahead(3, hour=0), ahead(6, hour=0)
    known = activity("1200000000000000101", when=ahead(25))
    no_time = activity("1200000000000000202", when=None)
    deadline_late = activity("1200000000000000404", when=None)
    deadline_soon = activity("1200000000000000505", when=None)
    deadline_late["conditions"] = {"participation_deadline": late_ts,
                                   "participation_deadline_precision": "date"}
    deadline_soon["conditions"] = {"participation_deadline": soon_ts,
                                   "participation_deadline_precision": "date"}
    replace_all_activities([no_time, deadline_late, deadline_soon, known])
    rows = {item["dynamic_id"]: item for item in list_activities(page_size=50)["items"]}
    assert rows["1200000000000000505"]["participation_deadline_unix"] == soon_ts
    assert rows["1200000000000000202"]["participation_deadline_unix"] is None
    picked = [t["dynamic_id"] for t in pick_triple_participate_targets(limit=4)]
    assert picked == ["1200000000000000101", "1200000000000000505", "1200000000000000404", "1200000000000000202"]


def test_manual_approved_still_sits_behind_auto_rows():
    """人工批准（allow_manual）的行仍然排在有时间自动行之后——历史规则不变。"""
    known = activity("1200000000000000101", when=ahead(2))
    approved = activity("1200000000000000202", when=ahead(1))
    replace_all_activities([known, approved])
    review_activity_time("1200000000000000202", decision="allow_manual", evidence="人已确认仍开放")
    picked = [t["dynamic_id"] for t in pick_triple_participate_targets(limit=3)]
    assert picked == ["1200000000000000101", "1200000000000000202"]


def test_not_approved_unknown_time_stays_out_of_auto_queue():
    replace_all_activities([activity()])
    review_activity_time(TARGET, decision="defer", evidence="原文没有开奖时间，继续搁着")
    row = list_activities()["items"][0]
    assert row["activity_status"] == "待审核"
    assert not row["auto_can_participate"]
    assert not pick_triple_participate_targets()


def test_deadline_and_draw_are_distinct_and_day_precision_does_not_end_at_midnight():
    item = activity(when=timestamp("2026-10-15 00:00"))
    item["conditions"] = {"lottery_time_precision": "date", "participation_deadline": timestamp("2026-10-10 18:00")}
    assert classify_activity_timing(item, now=timestamp("2026-10-11 12:00"))[:2] == ("已截止", "active")
    item["conditions"].pop("participation_deadline")
    assert classify_activity_timing(item, now=timestamp("2026-10-15 12:00"))[:2] == ("未参加", "active")
    assert lottery_time_text(item) == "2026-10-15"
    assert classify_activity_timing(item, now=timestamp("2026-10-16 00:00"))[:2] == ("已结束", "ended")


def test_pipeline_initial_status_and_prune_preserve_unknown_review():
    from src.pipeline.status_step import apply_initial_status
    from src.fetch_activity_info import EnrichResult
    from web.enrich_prune import prune_ended_activities
    item = apply_initial_status(activity(), now=timestamp("2026-10-09 12:00"))
    assert item["activity_status"] == "待审核"
    assert item["draw_status"] == "unknown"
    result = EnrichResult(enriched_at=1, total_count=1, new_count=1, p1_count=1, skipped_count=0,
                          counts={}, user_status_counts={}, activities=[item])
    kept, removed = prune_ended_activities(result)
    assert removed == 0
    assert kept.user_status_counts["待审核"] == 1
    assert kept.counts["unknown"] == 1


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_unknown_time_sorts_last_in_both_directions(order):
    rows = [{"dynamic_id": "missing", "lottery_time_unix": None},
            {"dynamic_id": "late", "lottery_time_unix": 20}, {"dynamic_id": "early", "lottery_time_unix": 10}]
    rows.sort(key=lambda row: _sort_key(row, sort="lottery_time", order=order))
    assert rows[-1]["dynamic_id"] == "missing"
    assert rows[0]["dynamic_id"] == ("early" if order == "asc" else "late")


def test_evidence_rejects_cutoff_as_draw_and_keeps_actual_cutoff():
    text = "抽奖截止时间：2026年10月15日。送键盘一把。"
    parsed = {"lottery_time": "2026-10-15 00:00", "lottery_time_evidence": "抽奖截止时间：2026年10月15日", "lottery_time_precision": "date",
              "participation_deadline": "2026-10-15 00:00", "participation_deadline_evidence": "抽奖截止时间：2026年10月15日", "participation_deadline_precision": "date"}
    result = validate_extracted_times(parsed, text, reference_ts=None)
    assert result["lottery_time"] is None
    assert result["participation_deadline"] == "2026-10-15 00:00"
    assert result["participation_deadline_verified"]


def test_evidence_requires_exact_fragment_and_matching_date():
    text = "2026年10月15日晚8点开奖。"
    parsed = {"lottery_time": "2026-10-15 20:00", "lottery_time_evidence": text, "lottery_time_precision": "minute"}
    assert validate_extracted_times(parsed, text, reference_ts=None)["lottery_time_verified"]
    parsed["lottery_time"] = "2026-10-16 20:00"
    assert not validate_extracted_times(parsed, text, reference_ts=None)["lottery_time_verified"]
    parsed["lottery_time_evidence"] = "2026年10月16日晚8点开奖。"
    assert not validate_extracted_times(parsed, text, reference_ts=None)["lottery_time_verified"]


def test_missing_publication_reference_rejects_omitted_year():
    text = "10月15日开奖。"
    parsed = {"lottery_time": "2026-10-15 00:00", "lottery_time_evidence": text, "lottery_time_precision": "date"}
    assert not validate_extracted_times(parsed, text, reference_ts=None)["lottery_time_verified"]
    assert validate_extracted_times(parsed, text, reference_ts=timestamp("2026-10-09 12:00"))["lottery_time_verified"]


def test_cache_cannot_drop_inference_flag_or_platform_provenance():
    from src.lottery_enricher import activity_from_cache
    item = activity(when=timestamp("2030-10-15 20:00"))
    item["lottery_time_inferred"] = True
    cached = activity_from_cache(TARGET, "转发抽奖", item)
    assert cached is not None
    assert lottery_time_unix(cached.to_dict()) is None
    assert cached.conditions["lottery_time_inferred"] is True
    item.pop("lottery_time_inferred")
    item.update(draw_status="ended", draw_status_source="platform")
    cached = activity_from_cache(TARGET, "转发抽奖", item)
    assert cached.draw_status_source == "platform"
    assert classify_activity_timing(cached.to_dict())[:2] == ("已结束", "ended")


def test_refresh_keeps_unknown_activities_and_review_audit():
    from src.fetch_activity_info import load_cached_enrich_result, is_enriched_complete, _all_enriched_activities
    from src.lottery_enricher import activity_from_cache
    item = activity()
    item.update(prizes=[{"description": "键盘", "winner_count": 1}], repost_fetched=True,
                time_review={"decision": "allow_manual", "evidence": "已核查原文"},
                time_repair={"reason": "removed_explicit_inferred_date"})
    item["conditions"]["time_extraction_checked"] = True
    assert is_enriched_complete(item)
    replace_all_activities([item])
    result = load_cached_enrich_result()
    assert result.counts["unknown"] == 1
    assert result.counts["active"] == 0
    assert result.activities[0]["time_review"] == item["time_review"]
    assert result.activities[0]["time_repair"] == item["time_repair"]
    refreshed = dict(item, time_review={}, time_repair={})
    merged = _all_enriched_activities(existing_by_id={TARGET: item},
        newly_enriched={TARGET: activity_from_cache(TARGET, "转发抽奖", refreshed)}, participations={})
    assert merged[0].time_review == item["time_review"]
    assert merged[0].time_repair == item["time_repair"]
    item["time_review"] = {}
    replace_all_activities([item])
    assert load_cached_enrich_result().user_status_counts["待审核"] == 1


def test_preview_is_read_only_and_repair_backs_up_only_marked_records(tmp_path):
    future = timestamp("2030-10-15 20:00")
    replace_all_activities([activity(inferred=True, when=future), activity("100000000000000002", when=future),
                            activity("100000000000000003", inferred=True, when=future, joined=True)])
    database = db_path()
    before = sqlite3.connect(database).execute("SELECT dynamic_id,payload_json FROM activities ORDER BY dynamic_id").fetchall()
    preview = preview_time_repairs(database)
    assert preview["inferred_count"] == 2
    after = sqlite3.connect(database).execute("SELECT dynamic_id,payload_json FROM activities ORDER BY dynamic_id").fetchall()
    assert before == after
    backup = tmp_path / "backup.db"
    result = apply_time_repairs(database, preview, backup=backup)
    assert result["repaired_count"] == 2
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT lottery_time FROM activities WHERE dynamic_id=?", (TARGET,)).fetchone()[0] == future
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT payload_json FROM activities WHERE dynamic_id='100000000000000002'").fetchone()[0] == before[1][1]
        assert conn.execute("SELECT lottery_time FROM activities WHERE dynamic_id=?", (TARGET,)).fetchone()[0] is None
    items = {item["dynamic_id"]: item for item in load_activities()}
    assert items[TARGET]["activity_status"] == "待审核"
    assert items["100000000000000003"]["activity_status"] == "已参加"
    assert items[TARGET]["lottery_time"] is None  # A stale JSON payload cannot resurrect a cleared promoted column.
    assert not preview_time_repairs(database)["records"]


def test_stale_preview_aborts_without_partial_update(tmp_path):
    replace_all_activities([activity(inferred=True, when=9999999999)])
    preview = preview_time_repairs(db_path())
    with sqlite3.connect(db_path()) as conn:
        conn.execute("UPDATE activities SET updated_at=updated_at+1")
    with pytest.raises(RuntimeError, match="预览已过期"):
        apply_time_repairs(db_path(), preview, backup=tmp_path / "backup.db")
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute("SELECT lottery_time FROM activities").fetchone()[0] == 9999999999


def test_conflicting_evidence_is_previewed_but_not_cleared(tmp_path):
    item = activity(inferred=True, when=9999999999)
    item["conditions"].update(lottery_time_source="platform", lottery_time_evidence="lottery_notice.lottery_time=9999999999")
    replace_all_activities([item])
    preview = preview_time_repairs(db_path())
    assert preview["conflicting_count"] == 1
    result = apply_time_repairs(db_path(), preview, backup=tmp_path / "backup.db")
    assert result["repaired_count"] == 0
    assert result["conflict_skipped_ids"] == [TARGET]
