"""Validate extracted dates against literal source fragments before accepting them."""
from __future__ import annotations

from datetime import datetime
import re

from src.lottery_time import BEIJING_TZ, LOTTERY_TIME_DISPLAY_FMT, normalize_lottery_time_text

# 抽奖语义锚点。10-26 复盘：库里 99 行的证据句写得很明白（「10月17日抽：」
# 「10.10评论区揪1位…」「我们将在10.30抽一位…」），却因关键词太窄被闸门判成时间未知，
# 所以把「抽/揪 + 数量位、句尾『抽：』、抽+粉丝称呼、roll」都纳入。
_DRAW_KEYWORD_RE = re.compile(
    r"开奖|开抽|抽取|抽出|揭晓|抽奖|揪人|抽人|抽选|抽送|开奖日|LT\s*[:：]"
    r"|(?<![A-Za-z])[Rr]oll(?![A-Za-z])"
    r"|(?:抽|揪)\s*(?:[0-9]+|[一二两三四五六七八九十]+)\s*(?:位|名|人|个|只|份)"
    r"|(?:抽|揪)\s*(?:[一二两三]位?|几位)?\s*(?:幸运|粉丝|宝子|观众|朋友|同学|兄弟|姐妹|小伙伴|昊友|义父|铁子|老铁)"
    r"|(?:抽|揪)\s*[:：]\s*$"
    r"|(?:抽|揪)\s*[:：]"
    r"|(?:抽|揪)[^\d]{0,6}(?:送|得|获得|出|取)"
    r"|(?:抽|揪)"        # 句里已有唯一日期时，单个「抽/揪」在抽奖语境下足够（10-26 实测补）
    r"|结果|置顶结果"
)


def mentions_draw(text: str) -> bool:
    return bool(_DRAW_KEYWORD_RE.search(text or ""))


# 允许「2026 年 10 月 30 日」「10 月 30 日」这类数字与年月日之间带空格的写法。
_DATE_START_RE = re.compile(
    r"(?:\d{4}\s*[-/.年]\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\.\d{1,2}"
    r"|\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"
    r"|(?<![\dA-Za-z/])\d{1,2}\s*/\s*\d{1,2}(?![\dA-Za-z/])"
    r"|(?:本月|这个月)?月底"
    r"|下(?:周|星期|礼拜)[一二三四五六日天]"
)
_DATE_TOKEN_RE = re.compile(
    r"\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"
    r"|(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\.\d{1,2}"
    # 「开奖日期：10/30」这种斜杠写法。前后不允许再接数字/字母/斜杠，避开 URL 与页码。
    r"|(?<![\dA-Za-z/])\d{1,2}\s*/\s*\d{1,2}(?![\dA-Za-z/])"
    r"|(?:本月|这个月)?月底"
    r"|下(?:周|星期|礼拜)[一二三四五六日天]"
)


def _units(content: str):  # pragma: no cover - 保留给需要跨行粘句的调用方，抽取器本身刻意不用
    """把「标题行 + 换行 + 日期行」这种排版粘回一句（目前只被排查脚本使用）。"""
    pending: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _DATE_TOKEN_RE.search(stripped) and pending:
            yield "\n".join(pending + [stripped])
            pending = []
            continue
        if not _DATE_TOKEN_RE.search(stripped) and (mentions_draw(stripped) or re.search(r"截止|截至", stripped)):
            if len(pending) < 2:
                pending.append(stripped)
            continue
        yield stripped
    for leftover in pending:
        yield leftover


_CLOCK_RE = re.compile(
    r"^\s*(?:的)?\s*(上午|早上|中午|下午|晚间|晚上|晚|夜里|夜间|夜|凌晨|半夜)?\s*(\d{1,2})\s*"
    r"(?:[:：]\s*(\d{1,2})|([点时])\s*(半)?\s*(\d{1,2})?)"
)


def _clock_after(tail: str) -> tuple[int, int] | None:
    """日期后面紧跟的钟点：「晚上20点」「14:30」「中午12点半」。

    必须有「点/时/冒号」这个钟点记号才算——否则「10月30日10元红包」会被
    读成 10:00。
    """
    match = _CLOCK_RE.match(tail or "")
    if not match:
        return None
    label, hour_text, minute_text, _marker, half, minute_alt = match.groups()
    hour = int(hour_text)
    if half:
        minute = 30
    else:
        minute = int(minute_text or minute_alt or 0)
    if minute > 59 or hour > 23:
        return None
    if label in ("凌晨", "半夜") and hour == 12:
        hour = 0
    elif (label in ("下午", "晚间", "晚上", "晚", "夜里", "夜间", "夜") and hour < 12) or (label == "中午" and hour < 11):
        hour += 12
    if hour > 23:
        return None
    return hour, minute


def literal_date_value(clause: str, *, reference: datetime | None) -> tuple[str, str]:
    """从句子里只取「日期 + 紧随的钟点」来定时间。

    10-26 实测：把整句尾巴喂给 normalize 会 fullmatch 失败
    （「开奖时间：2026.10.30   晚上20点」「10月30日评论区随机揪出一个…」），
    所以只截日期片段，再单独读它后面的钟点。
    """
    date_start = _DATE_START_RE.search(clause or "")
    if not date_start:
        return "", "unknown"
    head = date_start.group(0)
    if re.fullmatch(r"\d{1,2}\s*/\s*\d{1,2}", head):        # 「10/30」按「10.30」解析
        head = head.replace("/", ".")
    value = normalize_lottery_time_text(head, ref=reference)
    if not value:
        return "", "unknown"
    clock = _clock_after(clause[date_start.end():])
    if clock:
        value = f"{value[:10]} {clock[0]:02d}:{clock[1]:02d}"
        return value, "minute"
    return value, "date"


# 「10月8日更新后登场」「10月14日19:00 发布会见」这类日期说的是上新/活动本身，不是开奖。
# 邻居标签行不能把它粘成开奖时间（10-26 逐条读页面时实测到的误判）。
_NON_DRAW_DATE_RE = re.compile(
    r"更新|登场|上架|开售|发售|上市|预售|报名|预告|发布会|沟通会|见面|直播|开播|开展|展演|展会|开业|周年庆|纪念日"
)


def _label_clause(unit: str) -> bool:
    """「【开奖日期】」「开奖安排：」「抽奖结果」这种只有一句话题、没有日期的短标签行。"""
    text = (unit or "").strip()
    return bool(text) and len(text) <= 24 and not _DATE_TOKEN_RE.search(text) and mentions_draw(text)


def _candidate_clauses(content: str) -> list[str]:
    """要判定的句子：单句，或「标签行 + 相邻日期行」拼出来的原文切片。

    10-26 复盘：B 站抽奖正文习惯把日期单独排版一行——「【开奖日期】\\n2026 年 10 月 30 日」
    （雅浚 1262209507298115584）、「开奖安排：\\n09.30」、「截止时间：\\n2026年10月30日」。
    按行切开后标签行没日期、日期行没抽奖词，谁都不成立。这里把相邻两行按原文拼起来，
    证据仍然是正文里的字面子串，闸门照样拦得住幻觉。
    """
    units = [m for m in re.finditer(r"[^\n。；;]+", content or "")]
    clauses: list[str] = []
    for index, unit in enumerate(units):
        text = unit.group(0).strip()
        if not text:
            continue
        clauses.append(text)
        dates = _DATE_TOKEN_RE.findall(text)
        if len(dates) != 1 or mentions_draw(text) or _NON_DRAW_DATE_RE.search(text):
            continue
        for neighbour in (index - 1, index + 1):
            if not 0 <= neighbour < len(units) or not _label_clause(units[neighbour].group(0)):
                continue
            start, end = (units[neighbour].start(), unit.end()) if neighbour < index else (unit.start(), units[neighbour].end())
            joined = content[start:end].strip()
            if joined and joined != text and len(_DATE_TOKEN_RE.findall(joined)) == 1:
                clauses.append(joined)
    return clauses


def extract_literal_times(content: str, *, reference_ts: int | None) -> dict:
    """Accept only unambiguous date clauses; the LLM handles the remaining prose.

    刻意保持保守：只认「同一句里有唯一日期，且本句或紧邻的标签行有抽奖动词」。
    """
    reference = datetime.fromtimestamp(reference_ts, BEIJING_TZ) if reference_ts else None
    found = {"lottery_time": [], "participation_deadline": []}
    for clause in _candidate_clauses(content):
        dates = _DATE_TOKEN_RE.findall(clause)
        if len(dates) != 1:
            continue
        # 只截日期片段，证据仍保留整句（闸门要求它是正文子串）。
        value, precision = literal_date_value(clause, reference=reference)
        candidate = {}
        for field in found:
            candidate.update({field: value, field + "_evidence": clause, field + "_precision": precision})
        checked = validate_extracted_times(candidate, content, reference_ts=reference_ts)
        for field in found:
            if checked.get(field + "_verified"):
                found[field].append(checked)
    result = {}
    for field, entries in found.items():
        # 同一个时间被两句排版重复提到（「开奖时间\n10月10号」+「10月10号\n抽奖就会中奖」）不算歧义；
        # 只有给出两个不同时间才交给模型/人工，证据取更贴近日期的那一句。
        unique = {entry[field]: entry for entry in entries}
        if len(unique) == 1:
            entry = sorted(unique.values(), key=lambda item: len(str(item[field + "_evidence"])))[0]
            for suffix in ("", "_evidence", "_precision", "_verified"):
                result[field + suffix] = entry[field + suffix]
    return result


def validate_extracted_times(parsed: dict, content: str, *, reference_ts: int | None) -> dict:
    result = dict(parsed)
    for field in ("lottery_time", "participation_deadline"):
        value = parsed.get(field)
        evidence = str(parsed.get(field + "_evidence") or "").strip()
        precision = parsed.get(field + "_precision")
        valid = bool(value and evidence and evidence in content and precision in ("date", "minute"))
        if field == "lottery_time":
            valid = valid and mentions_draw(evidence) and not (
                re.search(r"截止|截至", evidence) and not re.search(r"同[时日].*开奖|开奖.*同[时日]", evidence))
        else:
            valid = valid and bool(re.search(r"截止|截至|参与结束", evidence))
        try:
            candidate = datetime.strptime(str(value), LOTTERY_TIME_DISPLAY_FMT).replace(tzinfo=BEIJING_TZ)
        except ValueError:
            valid = False
            candidate = None
        # A current fetch time cannot anchor an old post's "tomorrow" or omitted year.
        explicit_year = bool(re.search(r"\d{4}\s*[年/.]", evidence))
        valid = valid and (explicit_year or reference_ts is not None)
        reference = datetime.fromtimestamp(reference_ts, BEIJING_TZ) if reference_ts else None
        # 与本地抽取器共用同一套算法：只截证据里的日期片段（含紧随的钟点），
        # 这样「模型给的时间」必须能被原句字面复算出来才放行。
        proof, proof_precision = literal_date_value(evidence, reference=reference)
        # Unsupported relative phrasing remains unknown, even if the model supplied a date.
        if not proof or candidate is None or proof != candidate.strftime(LOTTERY_TIME_DISPLAY_FMT):
            valid = False
        # 精度以原句为准：句子里有钟点就必须给到钟点，没有就不许凭空造一个 20:00。
        valid = valid and precision == proof_precision
        if valid and not explicit_year and reference is not None and candidate <= reference:
            # 年份是从发布时间推断的：推出来的时间却早于发布本身，说明这个裸日期不是
            # 「今年的开奖日」（10 月发布却推成 9 月 30），宁可判未知交给模型/人工。
            valid = False
        result[field] = value if valid else None
        result[field + "_evidence"] = evidence if evidence and evidence in content else ""
        result[field + "_precision"] = precision if valid else "unknown"
        result[field + "_verified"] = bool(valid)
    result["time_reference_ts"] = reference_ts
    return result
