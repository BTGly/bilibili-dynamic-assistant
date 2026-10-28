from __future__ import annotations

from types import SimpleNamespace

from src.pipeline.refresh_all_pipeline import (
    _apply_container_time,
    _collect_link_time_hints,
)
from src.sources.common import CheckResult


def _activity(lottery_time=None, conditions=None):
    return SimpleNamespace(
        lottery_time=lottery_time,
        conditions=dict(conditions or {}),
    )


HINT = {
    "lottery_time": "2026-10-26 00:00",
    "lottery_time_evidence": "开奖日期：10.26",
    "lottery_time_precision": "date",
    "container_url": "https://www.bilibili.com/read/cv53546334",
    "reference_ts": 1792604238,
}


def test_apply_container_time_fills_missing_and_records_provenance() -> None:
    activity = _activity()
    assert _apply_container_time(activity, HINT) is True
    assert activity.lottery_time == 1792944000  # 2026-10-26 00:00 北京时间
    assert activity.conditions["lottery_time_source"] == "container_text"
    assert activity.conditions["lottery_time_evidence"] == "开奖日期：10.26"
    assert activity.conditions["lottery_time_precision"] == "date"
    assert activity.conditions["container_url"] == HINT["container_url"]
    assert activity.conditions["time_reference_ts"] == HINT["reference_ts"]
    assert activity.conditions["time_extraction_checked"] is True


def test_apply_container_time_never_overrides_platform_time() -> None:
    activity = _activity(lottery_time=1792000000, conditions={"lottery_time_source": "platform"})
    assert _apply_container_time(activity, HINT) is False
    assert activity.lottery_time == 1792000000
    assert activity.conditions == {"lottery_time_source": "platform"}


def test_collect_link_time_hints_only_from_updated_containers() -> None:
    def result(updated: bool) -> CheckResult:
        return CheckResult(
            source_id="DS-3",
            updated=updated,
            container_url="https://www.bilibili.com/read/cv53546334",
            container_id="53546334",
            title="title",
            published_at=1792604238,
            previous_container_url=None,
            activity_links=["https://www.bilibili.com/opus/1254051425244151817"],
            checked_at=1,
            link_time_hints={
                "1254051425244151817": {
                    "lottery_time": "2026-10-26 00:00",
                    "lottery_time_evidence": "开奖日期：10.26",
                    "lottery_time_precision": "date",
                }
            },
        )

    assert _collect_link_time_hints([result(False)]) == {}
    hints = _collect_link_time_hints([result(True)])
    assert hints["1254051425244151817"]["container_url"] == "https://www.bilibili.com/read/cv53546334"
    assert hints["1254051425244151817"]["reference_ts"] == 1792604238
