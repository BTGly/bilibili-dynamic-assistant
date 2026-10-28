from __future__ import annotations

import time
from pathlib import Path

from src.bilibili_client import BilibiliClient
from src.sources.common import (
    CheckResult,
    extract_collection_links_with_hints,
    load_previous_output,
    normalize_activity_id,
    save_result as write_result,
)
from src.state_store import DATA_DIR, get_last_container

SOURCE_ID = "DS-9"
MID = 99584491
OUTPUT_PATH = DATA_DIR / "output" / "ds9_latest.json"


def video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def collect_author_comment_links(client: BilibiliClient, *, aid: int, referer: str) -> tuple[list[str], dict]:
    """只取视频作者本人的评论 —— 合集清单由 UP 自己分条发出，别人的回复不算来源。"""
    replies = client.get_video_replies(aid, referer=referer, page_size=49)
    links: list[str] = []
    hints: dict = {}
    for reply in replies:
        member = reply.get("member") or {}
        if int(member.get("mid") or 0) != MID:
            continue
        message = str((reply.get("content") or {}).get("message") or "")
        extra_links, extra_hints = extract_collection_links_with_hints(message)
        links.extend(extra_links)
        hints.update(extra_hints)
    return links, hints


def _dedupe(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for url in group:
            activity_id = normalize_activity_id(url)
            if not activity_id or activity_id in seen:
                continue
            seen.add(activity_id)
            merged.append(url)
    return merged


def check_update(*, force: bool = False) -> CheckResult:
    with BilibiliClient() as client:
        latest = client.get_latest_video(MID)
        bvid = latest["bvid"]
        container_url = video_url(bvid)
        previous = get_last_container(SOURCE_ID)

        if not force and previous == container_url:
            prev_output = load_previous_output(OUTPUT_PATH)
            return CheckResult(
                source_id=SOURCE_ID,
                updated=False,
                container_url=container_url,
                container_id=bvid,
                title=latest.get("title", ""),
                published_at=int(latest.get("created") or 0),
                previous_container_url=previous,
                activity_links=(prev_output or {}).get("activity_links") or [],
                checked_at=int(time.time()),
                link_hints=(prev_output or {}).get("link_hints") or {},
            )

        detail = client.get_video_detail(bvid)
        desc = detail.get("desc") or ""
        desc_links, hints = extract_collection_links_with_hints(desc)
        aid = int(detail.get("aid") or 0)
        comment_links: list[str] = []
        if aid:
            comment_links, comment_hints = collect_author_comment_links(
                client, aid=aid, referer=container_url
            )
            hints.update(comment_hints)

        links = _dedupe(desc_links, comment_links)
        if not links:
            raise RuntimeError(
                f"DS-9 未能从简介或作者评论中解析出活动链接（简介 {len(desc)} 字，aid={aid or '缺失'}）"
            )

        return CheckResult(
            source_id=SOURCE_ID,
            updated=True,
            container_url=container_url,
            container_id=bvid,
            title=detail.get("title") or latest.get("title", ""),
            published_at=int(detail.get("pubdate") or latest.get("created") or 0),
            previous_container_url=previous,
            activity_links=links,
            checked_at=int(time.time()),
            link_hints=hints,
        )


def save_result(result: CheckResult) -> Path | None:
    return write_result(OUTPUT_PATH, result)
