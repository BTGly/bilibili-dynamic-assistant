"""钉住「列表不再逐行查 guard 表」这件事。

活动列表要对全部行逐行判状态，一旦回退成每行 get_guard()，
一次页面加载就会变成几千次开连接（实测 833 行时 /api/activities 要 9.5 秒）。
"""
from __future__ import annotations

import time

import pytest
import src.participation_guard as guard_module
import web.activity_service as svc

_ROW_COUNT = 40


def _items(count: int) -> list[dict]:
    draw_at = int(time.time()) + 86400
    return [
        {
            "dynamic_id": str(1_200_000_000_000_000_000 + index),
            "source_url": f"https://www.bilibili.com/opus/{1_200_000_000_000_000_000 + index}",
            "lottery_type": "转发抽奖",
            "activity_status": "未参加",
            "lottery_time": draw_at,
            "conditions": {"lottery_time_evidence": "测试"},
        }
        for index in range(count)
    ]


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _items(_ROW_COUNT)
    monkeypatch.setattr(svc, "_load_activities_payload", lambda: {"activities": items})
    monkeypatch.setattr(svc, "_load_participation_actions", lambda: {})
    monkeypatch.setattr(svc, "load_participations", lambda: {})


def test_guard_lookups_do_not_scale_with_row_count(monkeypatch: pytest.MonkeyPatch, seeded: None) -> None:
    calls: list[str] = []
    real_get_guard = guard_module.get_guard

    def counting(dynamic_id: str) -> dict:
        calls.append(str(dynamic_id))
        return real_get_guard(dynamic_id)

    monkeypatch.setattr(guard_module, "get_guard", counting)

    rows = svc._filtered_activity_rows()

    assert len(rows) == _ROW_COUNT
    # 账号 guard + guard 表预载的常数级查询，不随行数增长
    assert len(calls) < _ROW_COUNT, f"guard 查询 {len(calls)} 次，接近行数 {_ROW_COUNT}，说明又退化成逐行查库"


def test_batched_and_per_row_paths_agree(monkeypatch: pytest.MonkeyPatch, seeded: None) -> None:
    """预载缓存与逐行查库必须产出完全相同的行，否则就是改了行为而不是改了性能。"""
    participations = svc.load_participations()
    action_map = svc._load_participation_actions()
    guards = svc._load_guard_cache()
    for item in _items(5):
        dynamic_id = str(item["dynamic_id"])
        assert svc._normalize_activity(item, action_map, participations.get(dynamic_id), guards) == \
            svc._normalize_activity(item, action_map, participations.get(dynamic_id), None)
