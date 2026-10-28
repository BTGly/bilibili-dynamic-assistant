from __future__ import annotations

from datetime import datetime

from src.lottery_enricher import _lottery_time_from_llm_parse
from src.lottery_time import BEIJING_TZ, LOTTERY_TIME_DISPLAY_FMT, SIX_MONTHS_SECONDS


def test_lottery_time_from_llm_parse_uses_display_string() -> None:
    display = "2026-07-08 00:00"
    unix, conditions = _lottery_time_from_llm_parse({"lottery_time": display, "lottery_time_verified": True, "lottery_time_evidence": "2026年7月8日开奖", "lottery_time_precision": "date"})
    expected = int(
        datetime.strptime(display, LOTTERY_TIME_DISPLAY_FMT)
        .replace(tzinfo=BEIJING_TZ)
        .timestamp()
    )
    assert unix == expected
    assert conditions["lottery_time_text"] == display
    assert "lottery_time_inferred" not in conditions


def test_lottery_time_from_llm_parse_rejects_chinese_text() -> None:
    unix, conditions = _lottery_time_from_llm_parse({"lottery_time": "2026年7月8日"})
    assert "lottery_time_inferred" not in conditions
    assert unix is None


def test_lottery_time_from_llm_parse_default_when_null() -> None:
    unix, conditions = _lottery_time_from_llm_parse({"lottery_time": None})
    assert "lottery_time_inferred" not in conditions
    assert "lottery_time_text" in conditions
    assert unix is None
