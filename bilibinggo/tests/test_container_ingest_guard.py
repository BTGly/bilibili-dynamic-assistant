"""合集/公告类内容必须在入库前就被挡掉，不能靠人事后发现（10-27 那 11 条 DS-3 合集专栏）。

用 monkeypatch 打桩 `parse_forward_content`：守卫放行时会走到解析步骤（抛 Marker），
被守卫挡下时抛 EnrichSkippedError —— 全程不碰网络、不花 LLM 额度。
"""
from __future__ import annotations


from src.bilibili_client import BilibiliClient
from src.lottery_enricher import EnrichSkippedError, enrich_forward_activity

DID = "1263424789752578054"
SOURCE_MID = 100680137          # DS-3 你的抽奖工具人


class Marker(Exception):
    """代表「两条守卫都没挡，继续往下走了」。"""


def detail(detail_type: str, *, mid: int, draw_card: bool = False) -> dict:
    return {"type": detail_type,
            "major": {"draw": {"id": 1}} if draw_card else {},
            "modules": {"module_author": {"mid": mid, "name": "某UP", "pub_ts": 1793000000}},
            "id_str": DID}


def reached_parse(monkeypatch, detail_item: dict) -> bool:
    def boom(*args, **kwargs):
        raise Marker()

    monkeypatch.setattr("src.lottery_enricher.parse_forward_content", boom)
    with BilibiliClient() as client:
        try:
            enrich_forward_activity(client, dynamic_id=DID,
                                    classify_content="DJI OSMO Pocket 4、B站月度大会员、200元红包",
                                    classify_detail=detail_item)
        except Marker:
            return True
        except EnrichSkippedError:
            return False
    raise AssertionError("既没被守卫挡下也没走到解析步骤")


def test_article_container_is_blocked_before_ingest(monkeypatch):
    assert reached_parse(monkeypatch, detail("DYNAMIC_TYPE_ARTICLE", mid=SOURCE_MID)) is False


def test_source_up_own_post_is_blocked_even_as_ordinary_dynamic(monkeypatch):
    """有的合集是普通图文动态（不是 ARTICLE），但作者是源 UP 自己 → 同样不该入库。"""
    assert reached_parse(monkeypatch, detail("DYNAMIC_TYPE_COMMON", mid=SOURCE_MID)) is False


def test_third_party_draw_passes_both_guards(monkeypatch):
    assert reached_parse(monkeypatch, detail("DYNAMIC_TYPE_FORWARD", mid=999999999)) is True
    assert reached_parse(monkeypatch, detail("DYNAMIC_TYPE_ARTICLE", mid=999999999, draw_card=True)) is True
