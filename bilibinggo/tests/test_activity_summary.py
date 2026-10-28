from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from web.activity_service import get_summary


def test_get_summary_counts_are_consistent(tmp_path, monkeypatch) -> None:
    path = tmp_path / "activities_latest.json"
    payload = {
        "updated_at": 1,
        "activities": [
            {
                "dynamic_id": "1",
                "lottery_type": "转发抽奖",
                "draw_status": "active",
                "activity_status": "未参加",
            },
            {
                "dynamic_id": "2",
                "lottery_type": "互动抽奖",
                "draw_status": "ended",
                "activity_status": "已结束",
            },
            {
                "dynamic_id": "3",
                "lottery_type": "充电抽奖",
                "skipped": True,
                "draw_status": "active",
                "activity_status": "未参加",
            },
            {
                "dynamic_id": "4",
                "lottery_type": "转发抽奖",
                "skipped": True,
                "skip_reason": "非抽奖活动",
                "draw_status": "active",
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("web.activity_service.load_participations", lambda: {})
    monkeypatch.setattr("web.activity_service.load_payload", lambda: payload)

    summary = get_summary()
    status = summary["user_status_counts"]
    draw = summary["counts"]

    assert summary["total_count"] == 2
    assert sum(status.values()) == 2
    assert status["待审核"] == 2
    assert sum(draw.values()) == 2
    assert draw == {"active": 0, "ended": 0, "unknown": 2}


def test_get_summary_loads_seeded_activities(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_activities = [
        {
            "dynamic_id": "1001",
            "lottery_type": "互动抽奖",
            "draw_status": "active",
            "activity_status": "未参加",
            "lottery_time": int(time.time()) + 3600,
            "prizes": [{"description": "测试奖品", "winner_count": 1}],
            "repost_count": 10,
            "repost_fetched": True,
        }
    ]
    seed_path = isolated_home / "config" / "activities_seed.json"
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text(
        json.dumps({"seed_version": 1, "activities": seed_activities}, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.activity_seed.CONFIG_DIR", isolated_home / "config")
    monkeypatch.setattr("src.activity_seed.USER_SEED_PATH", seed_path)
    monkeypatch.setattr("src.activity_seed.BUNDLED_SEED_PATH", seed_path)
    monkeypatch.setattr("web.activity_service.load_participations", lambda: {})

    from src.activity_store import seed_activities_if_empty

    assert seed_activities_if_empty() is True
    summary = get_summary()
    assert summary["total_count"] == 1
    assert summary["counts"]["active"] == 1


def test_source_updated_badge_requires_fresh_round(monkeypatch) -> None:
    """「本次有更新」只给最近一轮真有新专栏的源：updated=1 但检查时间过期也必须显示缓存。"""
    import time as _t
    from web.activity_service import _source_updated_this_round
    now = int(_t.time())
    assert _source_updated_this_round({"updated": True, "checked_at": now - 60}) is True
    assert _source_updated_this_round({"updated": False, "checked_at": now - 60}) is False
    assert _source_updated_this_round({"updated": True, "checked_at": now - 5 * 86400}) is False
    assert _source_updated_this_round({}) is False
