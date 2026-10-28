"""「相对说法但判未知」的计数：只攒数据用，所以更要保证它不写业务表、不抛异常。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.relative_time_stats import (
    RELATIVE_TIME_RE,
    note_candidate,
    relative_snippet,
    stats_path,
    summary,
)


@pytest.fixture()
def target(tmp_path: Path) -> Path:
    return stats_path(tmp_path)


def test_relative_phrases_are_detected_and_absolute_dates_are_not() -> None:
    for text in ("两周后开奖", "3 天后揭晓", "半个月后抽出", "明天晚上开奖", "下周五开奖"):
        assert RELATIVE_TIME_RE.search(text), text
    for text in ("10月30日开奖", "2026-11-01 12:00 抽取", "本条动态参与即视为同意"):
        assert not RELATIVE_TIME_RE.search(text), text


def test_snippet_returns_the_line_that_carries_the_phrase() -> None:
    text = "关注转发即可参与。\n我会在两周后开奖，送出键盘一把。\n谢谢支持"
    assert "两周后" in relative_snippet(text)


def test_note_candidate_counts_per_day_and_caps_samples(target: Path) -> None:
    for index in range(35):
        note_candidate(target, f"12000000000000000{index:02d}", "两周后开奖", now=1_793_000_000)
    data = json.loads(target.read_text(encoding="utf-8"))
    day = next(iter(data["days"].values()))
    assert day["count"] == 35
    assert len(day["samples"]) == 30                      # 样例有上限，文件不会无限长


def test_summary_totals_and_survives_missing_file(target: Path) -> None:
    assert summary(target) == {"total": 0, "days": {}}
    note_candidate(target, "1200000000000000001", "两周后开奖", now=1_793_000_000)
    note_candidate(target, "1200000000000000002", "明天开奖", now=1_793_000_000 + 86_400)
    out = summary(target)
    assert out["total"] == 2 and len(out["days"]) == 2
