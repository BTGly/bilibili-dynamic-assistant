from __future__ import annotations

from pathlib import Path

from src.db.snapshots import load_ds_check_dict
from src.sources.common import (
    CheckResult,
    extract_collection_links_with_hints,
    extract_t_bilibili_links_with_hints,
    save_result,
)


def test_extract_collection_links_covers_opus_and_skips_previous_issue() -> None:
    text = (
        "互动抽奖\n"
        "【29】哔哩哔哩大会员月卡\n"
        "https://www.bilibili.com/opus/1251599443447250952\n"
        "【30】拍立得照片一张\n"
        "https://www.bilibili.com/opus/1253863844650942471\n"
        "上期传送门 https://www.bilibili.com/opus/1111111111111111111\n"
        "https://t.bilibili.com/1234567890123456789"
    )
    links, hints = extract_collection_links_with_hints(text)
    assert links == [
        "https://www.bilibili.com/opus/1251599443447250952",
        "https://www.bilibili.com/opus/1253863844650942471",
        "https://t.bilibili.com/1234567890123456789",
    ]
    assert hints == {
        "1251599443447250952": "互动抽奖",
        "1253863844650942471": "互动抽奖",
        "1234567890123456789": "互动抽奖",
    }


def test_extract_collection_links_skips_previous_issue_block() -> None:
    text = (
        "上期传送门\n"
        "https://www.bilibili.com/opus/1111111111111111111\n"
        "https://www.bilibili.com/opus/2222222222222222222\n"
        "【1】大会员\n"
        "https://www.bilibili.com/opus/3333333333333333333\n"
    )
    links, _hints = extract_collection_links_with_hints(text)
    assert links == ["https://www.bilibili.com/opus/3333333333333333333"]


def test_extract_collection_links_dedupes_across_forms() -> None:
    same = "1251599443447250952"
    text = (
        f"https://www.bilibili.com/opus/{same}\n"
        f"https://t.bilibili.com/{same}\n"
    )
    links, _hints = extract_collection_links_with_hints(text)
    assert links == [f"https://www.bilibili.com/opus/{same}"]


def test_extract_t_bilibili_links_with_hints() -> None:
    desc = (
        "互动抽奖\n"
        "https://t.bilibili.com/1234567890123456789\n"
        "转发抽奖\n"
        "https://t.bilibili.com/9876543210987654321"
    )
    links, hints = extract_t_bilibili_links_with_hints(desc)
    assert links == [
        "https://t.bilibili.com/1234567890123456789",
        "https://t.bilibili.com/9876543210987654321",
    ]
    assert hints == {
        "1234567890123456789": "互动抽奖",
        "9876543210987654321": "转发抽奖",
    }


def test_save_result_records_even_no_update_checks(isolated_home: Path) -> None:
    """写入隔离库；source_id 使用测试专用前缀，避免万一泄漏污染正式源列表。"""
    from src.db.engine import db_path

    assert db_path().resolve().is_relative_to(isolated_home.resolve())
    source_id = "__pytest_ds__"
    path = Path("ds_test.json")
    unchanged = CheckResult(
        source_id=source_id,
        updated=False,
        container_url="https://example.com/container",
        container_id="c1",
        title="title",
        published_at=1,
        previous_container_url="https://example.com/container",
        activity_links=["https://t.bilibili.com/1234567890123456789"],
        checked_at=99,
    )
    assert save_result(path, unchanged) is None
    data0 = load_ds_check_dict(source_id)
    assert data0 is not None and data0["updated"] is False
    assert data0["activity_links"] == ["https://t.bilibili.com/1234567890123456789"]

    updated = CheckResult(
        source_id=source_id,
        updated=True,
        container_url="https://example.com/container",
        container_id="c1",
        title="title",
        published_at=1,
        previous_container_url=None,
        activity_links=["https://t.bilibili.com/1234567890123456789"],
        checked_at=100,
    )
    saved = save_result(path, updated)
    assert saved == path
    data = load_ds_check_dict(source_id)
    assert data is not None
    assert data["updated"] is True
    assert data["checked_at"] == 100


def _article(*paragraphs):
    return {"opus": {"content": {"paragraphs": paragraphs}}}


def _para(text: str = "", links: tuple[str, ...] = ()):
    nodes = []
    if text:
        nodes.append({"node_type": 1, "word": {"words": text}})
    for opus_id in links:
        nodes.append(
            {
                "node_type": 4,
                "link": {"biz_id": opus_id, "link": f"https://www.bilibili.com/opus/{opus_id}"},
            }
        )
    return {"text": {"nodes": nodes}}


PUBLISH_TS = 1792505019  # 2026-10-20 22:03 北京时间


def test_link_time_hints_follow_entry_and_section_dates() -> None:
    from src.sources.common import extract_opus_link_time_hints

    article = _article(
        _para("【置顶抽奖①】"),
        _para("开奖日期：12.20"),
        _para("奖品", ("1253856929797636100",)),
        _para("【10.26开奖的非官方抽奖部分】"),
        _para("1、→10.26→→奖品→→【UP】", ("1253082551430414336",)),
        _para("2、→10.26→→奖品→→【UP】", ("1254051425244151817",)),
    )
    hints = extract_opus_link_time_hints(article, reference_ts=PUBLISH_TS)
    assert hints["1253856929797636100"]["lottery_time"] == "2026-12-20 00:00"
    assert hints["1253856929797636100"]["lottery_time_evidence"] == "开奖日期：12.20"
    assert hints["1253082551430414336"]["lottery_time"] == "2026-10-26 00:00"
    assert hints["1254051425244151817"]["lottery_time"] == "2026-10-26 00:00"


def test_link_time_hints_use_lt_minute_and_stop_at_other_dates() -> None:
    from src.sources.common import extract_opus_link_time_hints

    article = _article(
        _para("互动抽奖"),
        _para("[01] ------- 某UP"),
        _para("LT:2026-10-21 02:35:00"),
        _para("奖品", ("1257762253348798481",)),
        _para("1、→10.27→→奖品→→【UP】", ("1254051425244151817",)),
        _para("【10.26开奖部分】"),
        _para("2、→10.27→→奖品→→【UP】", ("1253082551430414336",)),
        _para("结束:2026-10-21 09:00:00"),
        _para("奖品", ("1260846920304164882",)),
    )
    hints = extract_opus_link_time_hints(article, reference_ts=PUBLISH_TS)
    assert hints["1257762253348798481"]["lottery_time"] == "2026-10-21 02:35"
    assert hints["1257762253348798481"]["lottery_time_precision"] == "minute"
    # 条目自己写 10.27，与上方 10.26 标题不一致：不猜
    assert "1254051425244151817" not in hints
    assert "1253082551430414336" not in hints
    # 「结束:」字段不是开奖句，后面的链接不能沾上旧标题
    assert "1260846920304164882" not in hints
