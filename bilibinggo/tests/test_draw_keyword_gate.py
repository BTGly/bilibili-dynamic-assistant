from __future__ import annotations

from datetime import datetime

from src.lottery_time import BEIJING_TZ, normalize_lottery_time_text
from src.time_evidence import extract_literal_times, mentions_draw, validate_extracted_times


def ref(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=BEIJING_TZ).timestamp())


def test_draw_keyword_covers_real_wording():
    for text in (
        "10月17日抽：",
        "10月21日抽：",
        "我们将在10.30抽一位幸运观众",
        "10.10评论区揪1位昊友送出西昊J3一把",
        "10月10日揪一位幸运儿送周边大礼包",
        "11月2日抽3位粉丝送激活码",
        "10月26日 12:00 开奖",
        "楼下roll一位10月30号",
        "非官方抽奖，10月20号直播抽奖",
    ):
        assert mentions_draw(text), text


def test_draw_keyword_rejects_plain_date_or_sale_text():
    for text in (
        "10月15日 20:00 全平台正式开售",
        "9月11日 - 10月10日 门店快闪",
        "10月14日新品发布会直播",
        "2026年10月16日下午5点公布中奖名单预告",  # 无抽奖动词
    ):
        assert not mentions_draw(text), text


def test_m_d_d_form_now_extracts_with_year_from_reference():
    content = "关注+评论抽奖，我们将在10.30抽一位幸运观众"
    reference = ref("2026-10-16 17:00")
    result = extract_literal_times(content, reference_ts=reference)
    assert result.get("lottery_time") == "2026-10-30 00:00"
    assert result["lottery_time_precision"] == "date"
    assert "10.30" in result["lottery_time_evidence"]


def test_trailing_draw_colon_is_accepted():
    content = "10月17日抽：\n关注转发即可参与"
    result = extract_literal_times(content, reference_ts=ref("2026-10-15 09:00"))
    assert result.get("lottery_time") == "2026-10-17 00:00"


def test_jiu_one_person_clause_is_accepted():
    content = "10.10评论区揪1位昊友送出西昊J3一把"
    result = extract_literal_times(content, reference_ts=ref("2026-10-05 12:00"))
    assert result.get("lottery_time") == "2026-10-10 00:00"


def test_gate_still_needs_literal_evidence_and_precision_match():
    content = "10月17日抽："
    forged = {
        "lottery_time": "2026-11-17 00:00",
        "lottery_time_evidence": content,
        "lottery_time_precision": "date",
    }
    checked = validate_extracted_times(forged, content, reference_ts=ref("2026-10-15 09:00"))
    assert not checked["lottery_time_verified"]
    assert checked["lottery_time"] is None


def test_two_dates_in_one_clause_stays_unambiguous_and_rejected():
    content = "9月15日 - 10月14日 17:00 抽奖"
    assert extract_literal_times(content, reference_ts=ref("2026-09-01 10:00")).get("lottery_time") is None


def test_gate_accepts_models_cross_line_evidence():
    """雅浚那条的排版是「【开奖日期】」一行、「2026 年 10 月 30 日」一行。
    本地字面抽取刻意不去猜跨行，闸门必须能认可模型给出的跨行证据句。"""
    content = (
        "【关注＋转发＋评论  抽散热】\n本期奖品如下：\n雅浚X400 PRO\n"
        "【开奖日期】\n2026 年 10 月 30 日 \n中奖后请在 7 天内回复确定"
    )
    evidence = "【开奖日期】\n2026 年 10 月 30 日"
    checked = validate_extracted_times(
        {"lottery_time": "2026-10-30 00:00", "lottery_time_evidence": evidence, "lottery_time_precision": "date"},
        content,
        reference_ts=ref("2026-10-23 09:37"),
    )
    assert checked["lottery_time_verified"], checked
    assert checked["lottery_time"] == "2026-10-30 00:00"


def test_spaced_month_day_without_year_is_resolved_from_reference():
    checked = validate_extracted_times(
        {"lottery_time": "2026-11-06 20:00", "lottery_time_evidence": "开奖时间\n11 月 6 日 20:00",
         "lottery_time_precision": "minute"},
        "开奖时间\n11 月 6 日 20:00 开抽",
        reference_ts=ref("2026-10-26 12:00"),
    )
    assert checked["lottery_time_verified"]


def test_cross_line_label_and_date_are_stitched_from_original_text():
    """「开奖安排：\n09.30」这类跨行排版由相邻标签行拼接，证据仍是原文字面子串。"""
    content = "关注转发抽鼠标，兑奖说明详见图片。开奖安排：\n09.30"
    extracted = extract_literal_times(content, reference_ts=ref("2026-09-23 12:00"))
    assert extracted["lottery_time"] == "2026-09-30 00:00"
    assert extracted["lottery_time_evidence"] == "开奖安排：\n09.30"
    assert extracted["lottery_time_evidence"] in content


def test_cross_line_date_without_draw_label_stays_unknown():
    """日期行的邻居没有抽奖语义词时不许拼接——否则任何日期都能被猜成开奖日。"""
    content = "关注转发抽鼠标。\n库存 10 台\n09.30 补货"
    assert not extract_literal_times(content, reference_ts=ref("2026-09-23 12:00"))


def test_clock_after_date_needs_a_clock_marker():
    """钟点必须有「点/时/冒号」，否则「10月30日10元红包」会被读成 10:00。"""
    from src.time_evidence import _clock_after, literal_date_value

    assert _clock_after("晚8点开奖") == (20, 0)
    assert _clock_after("晚上8点半") == (20, 30)
    assert _clock_after(" 14:30") == (14, 30)
    assert _clock_after("10元红包") is None
    assert literal_date_value("2026 年 10 月 30 日晚8点", reference=None) == ("2026-10-30 20:00", "minute")
    assert literal_date_value("10月30日 10元红包", reference=None) == ("2026-10-30 00:00", "date")


def test_date_only_llm_answer_is_accepted_as_midnight():
    from src.forward_parser import _coerce_lottery_time, _normalize_parsed

    assert _coerce_lottery_time("2026-10-30") == "2026-10-30 00:00"
    assert _coerce_lottery_time("2026/10/30") == "2026-10-30 00:00"
    assert _coerce_lottery_time("2026-13-40") is None
    parsed = _normalize_parsed({
        "is_lottery": True, "lottery_time": "2026-10-30",
        "lottery_time_evidence": "开奖日期 2026 年 10 月 30 日", "lottery_time_precision": "minute",
    })
    assert parsed["lottery_time"] == "2026-10-30 00:00"
    assert parsed["lottery_time_precision"] == "date"

