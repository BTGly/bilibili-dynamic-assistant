from __future__ import annotations

import pytest

from src.sources import ds9_jinlisongrui


class _FakeClient:
    def __init__(self, *, detail: dict, replies: list[dict]) -> None:
        self._detail = detail
        self._replies = replies
        self.reply_calls: list[tuple[int, int]] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get_latest_video(self, mid: int) -> dict:
        return {"bvid": "BV1fake", "title": "2026年10月10日|每日更新", "created": 1_700_000_000}

    def get_video_detail(self, bvid: str) -> dict:
        return self._detail

    def get_video_replies(self, aid: int, *, referer: str | None = None, page_size: int = 20) -> list[dict]:
        self.reply_calls.append((aid, page_size))
        return self._replies


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(ds9_jinlisongrui, "BilibiliClient", lambda *a, **k: client)


def test_ds9_takes_links_from_author_comments_not_other_users(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = {
        "aid": 123,
        "desc": "抽奖详细链接见评论区",
        "pubdate": 1_700_000_010,
        "title": "2026年10月10日|每日更新",
        "owner": {"mid": ds9_jinlisongrui.MID, "name": "锦鲤送瑞"},
    }
    replies = [
        {
            "member": {"mid": ds9_jinlisongrui.MID},
            "content": {"message": "【1】大会员\nhttps://www.bilibili.com/opus/1251599443447250952"},
        },
        {
            "member": {"mid": 999999},
            "content": {"message": "路人 https://www.bilibili.com/opus/1111111111111111111"},
        },
        {
            "member": {"mid": ds9_jinlisongrui.MID},
            "content": {"message": "【2】键盘 上期传送门\nhttps://www.bilibili.com/opus/2222222222222222222"},
        },
        {
            "member": {"mid": ds9_jinlisongrui.MID},
            "content": {"message": "【3】鼠标\nhttps://www.bilibili.com/opus/1253863844650942471"},
        },
    ]
    client = _FakeClient(detail=detail, replies=replies)
    _install(monkeypatch, client)

    result = ds9_jinlisongrui.check_update(force=True)

    assert result.source_id == "DS-9"
    assert result.updated is True
    assert result.container_url.endswith("/video/BV1fake")
    assert result.activity_links == [
        "https://www.bilibili.com/opus/1251599443447250952",
        "https://www.bilibili.com/opus/1253863844650942471",
    ]
    assert client.reply_calls == [(123, 49)]


def test_ds9_raises_when_nothing_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = {
        "aid": 456,
        "desc": "本期完",
        "pubdate": 1,
        "title": "t",
        "owner": {"mid": ds9_jinlisongrui.MID},
    }
    client = _FakeClient(detail=detail, replies=[])
    _install(monkeypatch, client)

    with pytest.raises(RuntimeError, match="未能从简介或作者评论中解析出活动链接"):
        ds9_jinlisongrui.check_update(force=True)
