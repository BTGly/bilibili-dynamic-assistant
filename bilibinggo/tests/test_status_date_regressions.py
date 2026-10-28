from datetime import datetime
import json
import sqlite3
import time

import pytest

from src.activity_store import load_activities, replace_all_activities
from src.db.engine import db_path
from src.forward_parse_cache import put_cached_parse
from src.forward_parser import parse_forward_content
from src.lottery_time import BEIJING_TZ, lottery_time_unix, classify_activity_timing
from src.participation_guard import set_guard, get_guard
from src.time_evidence import extract_literal_times, validate_extracted_times
from src.verified_time_repair import preview_verified_dates, apply_verified_dates
from web.activity_service import list_activities, get_summary, pick_triple_participate_targets

ID = "1250786818450784258"


def ts(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=BEIJING_TZ).timestamp())


def item():
    return {"dynamic_id": ID, "lottery_type": "转发抽奖", "lottery_time": int(time.time()) + 86400,
            "activity_status": "未参加", "draw_status": "active", "conditions": {}}


def proof():
    return {"id": ID, "text": "关注转发，抽鼠标。09.30（周三开奖）", "publication_ts": ts("2026-09-22 12:00"),
            "lottery_time": "2026-09-30 00:00", "lottery_time_precision": "date", "lottery_time_evidence": "09.30（周三开奖）"}


@pytest.mark.parametrize("expired", [False, True])
def test_exclusion_overrides_display_filters_summary_and_auto_candidates(expired):
    activity = item()
    if expired:
        activity["lottery_time"] = int(time.time()) - 86400
    replace_all_activities([activity])
    set_guard(ID, state="excluded", reason="游戏月卡" if not expired else "已经开奖")
    row = list_activities(status="已排除")["items"][0]
    assert row["activity_status"] == "已排除"
    assert row["activity_lifecycle_status"] == ("已结束" if expired else "未参加")
    assert not row["can_participate"] and not row["auto_can_participate"]
    assert not list_activities(status="未参加")["items"]
    assert get_summary()["user_status_counts"]["已排除"] == 1
    assert not pick_triple_participate_targets()


def test_legacy_v7_future_date_is_not_a_trusted_automatic_candidate():
    original = item()
    replace_all_activities([original])
    put_cached_parse(ID, "旧正文", {"parser_version": 7, "lottery_time": "2026-10-31 00:00"})
    saved = load_activities()[0]
    assert saved["lottery_time"] == original["lottery_time"]  # retain audit value
    assert saved["activity_status"] == "待审核"
    assert lottery_time_unix(saved) is None
    # 10-26 政策：开奖时间未知也直接参加（排队列尾），不再等人工批准
    assert [t["dynamic_id"] for t in pick_triple_participate_targets()] == [ID]
    from src.fetch_activity_info import list_missing_enrich_tasks
    assert (ID, "转发抽奖") in list_missing_enrich_tasks()


def test_month_end_uses_publication_month_not_refresh_month():
    extracted = extract_literal_times("关注转发抽充电器。月底开奖", reference_ts=ts("2026-09-05 12:00"))
    assert extracted["lottery_time"] == "2026-09-30 00:00"
    assert extracted["lottery_time_verified"]
    assert not extract_literal_times("月底开奖", reference_ts=None)
    candidate = dict(proof(), lottery_time="2026-10-12 00:00")
    assert not validate_extracted_times(candidate, candidate["text"], reference_ts=candidate["publication_ts"])["lottery_time_verified"]


def test_literal_date_wins_over_wrong_llm_calendar_date(monkeypatch):
    monkeypatch.setattr("src.forward_parser.chat_json", lambda **kw: dict(proof(), is_lottery=True, winner_count=1, prize_description="鼠标", lottery_time="2026-10-12 00:00"))
    result = parse_forward_content(ID, proof()["text"], reference_ts=proof()["publication_ts"])
    assert result["lottery_time"] == "2026-09-30 00:00"
    assert result["lottery_time_verified"]


def test_llm_and_literal_agree_on_cross_line_draw_schedule(monkeypatch):
    """跨行「开奖安排：\n09.30」本地已能定；模型给出同一句原文证据也必须被闸门放行。"""
    text = "关注转发抽鼠标，兑奖说明详见图片。开奖安排：\n09.30"
    assert extract_literal_times(text, reference_ts=proof()["publication_ts"])["lottery_time"] == "2026-09-30 00:00"
    def llm(**kw):
        assert text in kw["user"]
        return dict(proof(), is_lottery=True, lottery_time_evidence="开奖安排：\n09.30")
    monkeypatch.setattr("src.forward_parser.chat_json", llm)
    result = parse_forward_content(ID, text, reference_ts=proof()["publication_ts"])
    assert result["lottery_time"] == "2026-09-30 00:00"
    assert result["lottery_time_verified"]


def test_verified_repair_preserves_exclusion_and_makes_backup(tmp_path):
    replace_all_activities([item()])
    set_guard(ID, state="excluded", reason="已经开奖")
    preview = preview_verified_dates(db_path(), [proof()])
    backup = tmp_path / "backup.db"
    result = apply_verified_dates(db_path(), preview, backup=backup)
    assert result["corrected_ids"] == [ID]
    assert backup.is_file()
    saved = load_activities()[0]
    assert saved["conditions"]["lottery_time_evidence"] == proof()["lottery_time_evidence"]
    assert classify_activity_timing(saved, now=ts("2026-10-09 12:00"))[0] == "已结束"
    assert get_guard(ID)["state"] == "excluded"


def test_stale_verified_preview_aborts_before_any_update(tmp_path):
    replace_all_activities([item()])
    preview = preview_verified_dates(db_path(), [proof()])
    with sqlite3.connect(db_path()) as conn:
        conn.execute("UPDATE activities SET updated_at=updated_at+1 WHERE dynamic_id=?", (ID,))
    with pytest.raises(RuntimeError, match="数据有变化"):
        apply_verified_dates(db_path(), preview, backup=tmp_path / "backup.db")
    with sqlite3.connect(db_path()) as conn:
        assert conn.execute("SELECT lottery_time FROM activities WHERE dynamic_id=?", (ID,)).fetchone()[0] == preview["records"][0]["before"]["lottery_time"]
