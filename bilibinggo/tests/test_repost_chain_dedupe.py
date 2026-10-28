"""转发链核对：B 站会把「转发型抽奖」的转发压到链根显示，同链两条活动不能被转发两遍。

10-26 实测：HUGKIS 原动态 1256733200674193409（抽 2 位）与 AJAZZ黑爵 1257011291170537494
（转发它，抽 1 位）在同一轮 22:15 里都被选中，结果我的空间动态出现两条 orig 都指向 HUGKIS 原动态的转发，
面板上看着就是「同一条转发两次」。
"""
from __future__ import annotations

from unittest.mock import Mock

import src.lottery_actions as actions
from src.lottery_actions import dynamic_chain_ids, has_reposted_in_space_feed
from web.activity_service import pick_triple_participate_targets

ROOT_ID = "1256733200674193409"
SIBLING_ID = "1257011291170537494"


def _forward_item(orig_id: str, *, mine: str = "1263518000000000001") -> dict:
    return {"id_str": mine, "type": "DYNAMIC_TYPE_FORWARD", "orig": {"id_str": orig_id, "type": "DYNAMIC_TYPE_DRAW"}}


def _feed_client(orig_ids: list[str]) -> Mock:
    client = Mock()
    client.request_json.return_value = {
        "code": 0,
        "data": {"offset": "", "items": [_forward_item(oid) for oid in orig_ids]},
    }
    return client


def test_chain_ids_walk_the_repost_chain() -> None:
    item = {"orig": {"id_str": SIBLING_ID, "orig": {"id_str": ROOT_ID}}}
    assert dynamic_chain_ids(item) == [SIBLING_ID, ROOT_ID]
    assert dynamic_chain_ids({"orig": None}) == []
    assert dynamic_chain_ids(None) == []


def test_space_feed_check_is_blind_without_the_chain(monkeypatch) -> None:
    monkeypatch.setattr(actions, "get_login_uid", lambda: 3546851227732145)
    client = _feed_client([ROOT_ID])          # 我们那条转发在 B 站侧只挂了链根
    # 只比对自身 id：查不到自己已经转发过（旧行为，正是重复转发的根因）
    assert has_reposted_in_space_feed(client, dynamic_id=SIBLING_ID) is False
    # 带上链根就能认出来
    assert has_reposted_in_space_feed(client, dynamic_id=SIBLING_ID, chain_ids=[ROOT_ID]) is True


def test_unrelated_forward_does_not_count_as_mine(monkeypatch) -> None:
    monkeypatch.setattr(actions, "get_login_uid", lambda: 3546851227732145)
    client = _feed_client(["1299999999999999999"])
    assert has_reposted_in_space_feed(client, dynamic_id=SIBLING_ID, chain_ids=[ROOT_ID]) is False


def _item(dynamic_id: str, **conditions) -> dict:
    return {"dynamic_id": dynamic_id, "lottery_type": "转发抽奖", "lottery_time": None,
            "activity_status": "待审核", "draw_status": "active",
            "prizes": [{"description": "探险活宝毛绒挂件", "winner_count": 1}],
            "conditions": conditions}


def test_same_chain_rows_are_not_participated_twice_in_one_round() -> None:
    from src.activity_store import replace_all_activities

    replace_all_activities([
        _item(ROOT_ID),
        _item(SIBLING_ID, repost_chain_ids=[ROOT_ID]),
    ])
    picked = [t["dynamic_id"] for t in pick_triple_participate_targets(limit=3)]
    assert picked == [ROOT_ID]


def test_different_chains_are_still_picked_together() -> None:
    from src.activity_store import replace_all_activities

    other = "1260000000000000001"
    replace_all_activities([
        _item(ROOT_ID),
        _item(SIBLING_ID, repost_chain_ids=[ROOT_ID]),
        _item(other),
    ])
    picked = sorted(t["dynamic_id"] for t in pick_triple_participate_targets(limit=3))
    assert picked == sorted([ROOT_ID, other])
