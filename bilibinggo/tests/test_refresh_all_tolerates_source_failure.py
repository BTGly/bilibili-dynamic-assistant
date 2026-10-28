"""钉住一键更新的容错边界：单源失败不再拖垮整轮，账号级故障仍然必须中断。

背景：9 个数据源是并发检查的，原先任何一个抛错就 `raise`，导致一个停更的源、
或一个「清单还没贴到评论区」的源，能让其余 8 个源整轮白跑。
"""
from __future__ import annotations

import httpx
import pytest

import web.actions as actions
from src.participation_guard import set_guard
from src.sources.common import CheckResult


def _same_container(source_id: str) -> CheckResult:
    return CheckResult(
        source_id=source_id,
        updated=False,
        container_url=f"https://example.com/{source_id}",
        container_id=source_id,
        title="同一专栏",
        published_at=1,
        previous_container_url=None,
        activity_links=[],
        checked_at=1,
        link_hints={},
    )


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.bilibili.com/x/space/article")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"Client error '{code}'", request=request, response=response)


@pytest.fixture()
def two_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    ok_handler = lambda *, force=False: _same_container("DS-A")
    set_guard("", state="ready", reason="")
    monkeypatch.setattr(
        actions,
        "DS_HANDLERS",
        [("DS-A", ok_handler, lambda result: None)],
    )
    monkeypatch.setattr(actions, "REFRESH_ALL_TOTAL", 1 + 3)
    monkeypatch.setattr("web.actions.pending_ids", lambda: [])


def _failing_handler(exc: Exception):
    def check_update(*, force: bool = False):
        raise exc

    return check_update, (lambda result: None)


def test_single_source_failure_no_longer_aborts_the_round(
    monkeypatch: pytest.MonkeyPatch, two_sources: None
) -> None:
    bad_check, bad_save = _failing_handler(RuntimeError("未能从简介或作者评论中解析出活动链接"))
    monkeypatch.setattr(
        actions,
        "DS_HANDLERS",
        [
            actions.DS_HANDLERS[0],
            ("DS-B", bad_check, bad_save),
        ],
    )
    monkeypatch.setattr(actions, "REFRESH_ALL_TOTAL", 2 + 3)

    out = actions.run_action("refresh_all", {})

    assert out["ok"] is True
    assert out["result"]["sources_failed"] == ["DS-B: 未能从简介或作者评论中解析出活动链接"]
    assert "1 个源本轮检查失败" in out["message"]


def test_account_level_failure_still_aborts(monkeypatch: pytest.MonkeyPatch, two_sources: None) -> None:
    bad_check, bad_save = _failing_handler(_status_error(412))
    monkeypatch.setattr(
        actions,
        "DS_HANDLERS",
        [
            actions.DS_HANDLERS[0],
            ("DS-B", bad_check, bad_save),
        ],
    )
    monkeypatch.setattr(actions, "REFRESH_ALL_TOTAL", 2 + 3)

    with pytest.raises(RuntimeError, match="DS-B 检查失败"):
        actions.run_action("refresh_all", {})


def test_all_sources_failing_is_reported_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    set_guard("", state="ready", reason="")
    bad_a, bad_b = _failing_handler(RuntimeError("A 挂了")), _failing_handler(RuntimeError("B 挂了"))
    monkeypatch.setattr(actions, "DS_HANDLERS", [("DS-A", bad_a[0], bad_a[1]), ("DS-B", bad_b[0], bad_b[1])])
    monkeypatch.setattr(actions, "REFRESH_ALL_TOTAL", 2 + 3)

    with pytest.raises(RuntimeError, match="全部数据源检查失败"):
        actions.run_action("refresh_all", {})
