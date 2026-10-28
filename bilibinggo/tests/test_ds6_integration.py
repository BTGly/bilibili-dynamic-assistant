from __future__ import annotations

from src.sources import ds8_qianchoujun, ds9_jinlisongrui
from web.actions import DS_HANDLERS, REFRESH_ALL_TOTAL, REFRESH_WATCH_TOTAL
from web.activity_service import DS_OUTPUTS, SOURCE_LABELS, SOURCE_SPACE_URLS
from src.source_outputs import SOURCE_OUTPUTS


def test_all_registries_include_latest_ds_and_watch() -> None:
    handler_ids = [source_id for source_id, _, _ in DS_HANDLERS]
    merge_ids = [source_id for source_id, _ in SOURCE_OUTPUTS]
    output_ids = sorted(DS_OUTPUTS.keys())

    assert handler_ids == merge_ids[: len(handler_ids)]
    assert "DS-9" in handler_ids
    assert "WATCH" in merge_ids
    assert "WATCH" in output_ids
    assert len(handler_ids) == 9
    assert len(merge_ids) == 10


def test_refresh_all_total_matches_handlers() -> None:
    assert REFRESH_ALL_TOTAL == len(DS_HANDLERS) + 3


def test_ds7_metadata() -> None:
    assert SOURCE_LABELS["DS-7"] == "大锦鲤"
    assert SOURCE_SPACE_URLS["DS-7"] == "https://space.bilibili.com/226257459/upload/opus"
    assert DS_OUTPUTS["DS-7"].name == "ds7_latest.json"
    assert SOURCE_LABELS["WATCH"] == "监控用户"
    assert DS_OUTPUTS["WATCH"].name == "watch_latest.json"


def test_ds8_metadata() -> None:
    assert SOURCE_LABELS["DS-8"] == "欠抽菌"
    assert SOURCE_SPACE_URLS["DS-8"] == "https://space.bilibili.com/1673762431/upload/video"
    assert DS_OUTPUTS["DS-8"].name == "ds8_latest.json"
    assert ds8_qianchoujun.SOURCE_ID == "DS-8"
    assert ds8_qianchoujun.MID == 1673762431
    assert ds8_qianchoujun.OUTPUT_PATH.name == "ds8_latest.json"


def test_ds9_metadata() -> None:
    assert SOURCE_LABELS["DS-9"] == "锦鲤送瑞"
    assert SOURCE_SPACE_URLS["DS-9"] == "https://space.bilibili.com/99584491/upload/video"
    assert DS_OUTPUTS["DS-9"].name == "ds9_latest.json"
    assert ds9_jinlisongrui.SOURCE_ID == "DS-9"
    assert ds9_jinlisongrui.MID == 99584491
    assert ds9_jinlisongrui.OUTPUT_PATH.name == "ds9_latest.json"


def test_ds6_metadata_unchanged() -> None:
    assert SOURCE_LABELS["DS-6"] == "糯米是个背包"
    assert SOURCE_SPACE_URLS["DS-6"] == "https://space.bilibili.com/492426375/upload/opus"
    assert DS_OUTPUTS["DS-6"].name == "ds6_latest.json"
