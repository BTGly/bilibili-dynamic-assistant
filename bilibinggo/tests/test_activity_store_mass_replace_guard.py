"""整表替换的保险丝：临时脚本误调 replace_all_activities 不该能把库刷成两行。

10-29 03:29 真实事故：验证并发修复的一次性脚本没设隔离环境，直接对着生产库调
`replace_all_activities([两条假数据])`，1962 行活动表瞬间只剩 2 行。
"""
from __future__ import annotations

import time

import pytest

import src.test_isolation as isolation
from src.activity_store import load_activities, replace_all_activities


def item(did: str) -> dict:
    return {"dynamic_id": did, "lottery_type": "转发抽奖", "lottery_time": int(time.time()) + 86400,
            "activity_status": "未参加", "draw_status": "active", "conditions": {}}


def seed_many(count: int = 501) -> None:
    replace_all_activities([item(f"12000000000000{i:04d}") for i in range(count)], allow_mass_replace=True)


def test_mass_replace_is_refused_outside_test_isolation(monkeypatch) -> None:
    seed_many()
    monkeypatch.setattr(isolation, "under_test_isolation", lambda: False)
    with pytest.raises(RuntimeError, match="误操作整表覆写"):
        replace_all_activities([item("120000000000009999")])
    assert len(load_activities()) == 501                 # 被拒之后库没被动过


def test_explicit_flag_is_still_honoured(monkeypatch) -> None:
    seed_many()
    monkeypatch.setattr(isolation, "under_test_isolation", lambda: False)
    replace_all_activities([item("120000000000009999")], allow_mass_replace=True)
    assert [row["dynamic_id"] for row in load_activities()] == ["120000000000009999"]


def test_normal_full_replace_is_not_blocked() -> None:
    """生产路径（状态刷新）传的是全量列表，不该被保险丝挡住。"""
    seed_many()
    before = load_activities()
    replace_all_activities(before)
    assert len(load_activities()) == len(before)
