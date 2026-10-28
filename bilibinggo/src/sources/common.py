from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

EXCLUDE_CONTEXT_RE = re.compile(r"上期传送门|本期完|\(本期完\)")
OPUS_ID_RE = re.compile(r"/opus/(\d{18,19})")
OPUS_URL_RE = re.compile(r"https://www\.bilibili\.com/opus/\d{18,19}")
T_BILI_RE = re.compile(r"t\.bilibili\.com/(\d{19})")
T_BILI_URL_RE = re.compile(r"https://t\.bilibili\.com/\d{19}")
VALID_OPUS_ID_RE = re.compile(r"^\d{18,19}$")
ZHIDING_SECTION_RE = re.compile(r"【置顶抽奖")
SECTION_HEADING_RE = re.compile(
    r"^(?:#+\s*)?(?:【)?(充电抽奖|预约抽奖|互动抽奖|官方抽奖|非方?官方抽奖|转发抽奖)(?:】)?"
)

LotteryHint = Literal["转发抽奖", "预约抽奖", "互动抽奖", "充电抽奖"]
SECTION_TO_HINT: dict[str, LotteryHint] = {
    "转发抽奖": "转发抽奖",
    "预约抽奖": "预约抽奖",
    "互动抽奖": "互动抽奖",
    "官方抽奖": "互动抽奖",
    "非官方抽奖": "转发抽奖",
    "非方官方抽奖": "转发抽奖",
    "充电抽奖": "充电抽奖",
}


@dataclass
class CheckResult:
    source_id: str
    updated: bool
    container_url: str
    container_id: str
    title: str
    published_at: int
    previous_container_url: str | None
    activity_links: list[str]
    checked_at: int
    link_hints: dict[str, LotteryHint] = field(default_factory=dict)
    link_time_hints: dict[str, dict] = field(default_factory=dict)
    cv_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def commit_source_checkpoint(result: CheckResult) -> None:
    """流水线/检查成功后再写入专栏检查点；检查阶段不得提前调用。"""
    if not result.updated:
        return
    from src.state_store import set_last_container

    set_last_container(
        result.source_id,
        result.container_url,
        container_id=result.container_id or None,
        title=result.title or None,
        cv_id=result.cv_id,
    )


def opus_link(opus_id: str) -> str:
    return f"https://www.bilibili.com/opus/{opus_id}"


def normalize_activity_id(url: str) -> str | None:
    """从活动链接或明确的动态 ID 提取 ID，用于跨数据源去重。"""
    if VALID_OPUS_ID_RE.fullmatch(str(url).strip()):
        return str(url).strip()
    for pattern in (OPUS_ID_RE, T_BILI_RE):
        match = pattern.search(url)
        if match and VALID_OPUS_ID_RE.fullmatch(match.group(1)):
            return match.group(1)
    return None


def normalize_activity_url(url: str) -> str | None:
    activity_id = normalize_activity_id(url)
    if activity_id:
        return opus_link(activity_id)
    return None


def is_valid_dynamic_id(dynamic_id: str) -> bool:
    """校验 B 站动态 ID 格式（18–19 位数字）。"""
    return bool(VALID_OPUS_ID_RE.fullmatch(str(dynamic_id or "").strip()))


def load_previous_output(path: Path) -> dict | None:
    """优先从 DB 快照读取；文件仅作导入/兼容回退。"""
    from src.db.snapshots import FILENAME_TO_SOURCE, load_ds_check_dict, load_watch_sync_dict

    name = Path(path).name
    if name in {"activities_latest.json", "enriched_latest.json"}:
        from src.activity_store import load_payload

        payload = load_payload()
        if not payload.get("activities") and not int(payload.get("updated_at") or 0):
            return None
        return payload

    source_id = FILENAME_TO_SOURCE.get(name)
    if source_id == "WATCH":
        return load_watch_sync_dict()
    if source_id:
        return load_ds_check_dict(source_id)

    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_result(path: Path, result: CheckResult) -> Path | None:
    """无新专栏也要把「本轮检查过、没有更新」写进快照——否则卡片上的 updated 会永远停留在最后一次真有更新的那天。"""
    from src.db.snapshots import save_ds_check_dict

    save_ds_check_dict(result.to_dict())
    if not result.updated:
        return None
    return path


def parse_opus_id(link_obj: dict) -> str | None:
    biz_id = str(link_obj.get("biz_id") or "")
    if biz_id and VALID_OPUS_ID_RE.fullmatch(biz_id):
        return biz_id

    url = link_obj.get("link") or ""
    for pattern in (OPUS_ID_RE, T_BILI_RE):
        match = pattern.search(url)
        if match and VALID_OPUS_ID_RE.fullmatch(match.group(1)):
            return match.group(1)
    return None


def extract_t_bilibili_links_with_hints(desc: str) -> tuple[list[str], dict[str, LotteryHint]]:
    """从视频简介等纯文本提取 t.bilibili.com 链接及分区提示。"""
    text = desc or ""
    seen: set[str] = set()
    links: list[str] = []
    hints: dict[str, LotteryHint] = {}
    current_section: LotteryHint | None = None

    for line in text.splitlines():
        current_section = _section_hint_from_text(line, current_section)
        for url in T_BILI_URL_RE.findall(line):
            activity_id = normalize_activity_id(url)
            if not activity_id or activity_id in seen:
                continue
            seen.add(activity_id)
            links.append(url)
            if current_section:
                hints[activity_id] = current_section

    for url in T_BILI_URL_RE.findall(text):
        activity_id = normalize_activity_id(url)
        if not activity_id or activity_id in seen:
            continue
        seen.add(activity_id)
        links.append(url)

    return links, hints


def extract_desc_link_time_hints(desc: str, *, reference_ts: int) -> dict[str, dict]:
    """视频简介里「日期 + 链接」逐条排版的源（DS-1 那种）：把 UP 自己标的开奖日期贴到对应活动上。

    合集那条通道只读 opus 段落，而且要求句子自带开奖语义；视频简介两句都不满足——
    开奖语义在分区标题（【非官方抽奖】）上，日期在链接行上。10-28 实测 DS-1 最新一期
    25 组「日期+链接」与库里已有的开奖日期 25/25 一致，语义成立。
    「上期传送门」这类清单会先把分区关掉，避免往期日期沾到当期活动上。
    """
    if not desc or not reference_ts:
        return {}
    hints: dict[str, dict] = {}
    section: LotteryHint | None = None
    for raw in desc.splitlines():
        line = raw.strip()
        if not line:
            continue
        if EXCLUDE_CONTEXT_RE.search(line):
            section = None
        section = _section_hint_from_text(line, section)
        if section is None:
            continue
        if not CONTAINER_DATE_RE.search(line):
            continue
        dynamic_ids = T_BILI_RE.findall(line)
        if not dynamic_ids:
            continue
        value = _container_evidence_value(line, reference_ts)
        if not value:
            continue
        precision = "minute" if re.search(r"\d{1,2}[:：]\d{2}", line) else "date"
        for dynamic_id in dynamic_ids:
            hints.setdefault(dynamic_id, {
                "lottery_time": value,
                "lottery_time_evidence": line,
                "lottery_time_precision": precision,
            })
    return hints


def extract_collection_links_with_hints(text: str) -> tuple[list[str], dict[str, LotteryHint]]:
    """从纯文本同时提取 t.bilibili.com 与 /opus/ 两种活动链接。

    有些合集 UP 把清单写在评论区而不是简介里，链接形式是 opus 而非短链。
    这类清单是「奖品标签行 / 链接行」交替出现的，所以「上期传送门」写在标签行上时，
    要排除的是紧随其后的链接行，直到下一个标签行为止。
    """
    seen: set[str] = set()
    links: list[str] = []
    hints: dict[str, LotteryHint] = {}
    current_section: LotteryHint | None = None
    pending_excluded = False

    for line in (text or "").splitlines():
        current_section = _section_hint_from_text(line, current_section)
        urls = [*T_BILI_URL_RE.findall(line), *OPUS_URL_RE.findall(line)]
        if not urls:
            pending_excluded = bool(EXCLUDE_CONTEXT_RE.search(line))
            continue
        if pending_excluded or EXCLUDE_CONTEXT_RE.search(line):
            continue
        for url in urls:
            activity_id = normalize_activity_id(url)
            if not activity_id or activity_id in seen:
                continue
            seen.add(activity_id)
            links.append(url)
            if current_section:
                hints[activity_id] = current_section

    return links, hints


def _section_hint_from_text(text: str, current: LotteryHint | None) -> LotteryHint | None:
    if ZHIDING_SECTION_RE.search(text):
        return "转发抽奖"
    match = SECTION_HEADING_RE.match(text.strip())
    if match:
        return SECTION_TO_HINT.get(match.group(1), current)
    return current


def extract_opus_links_with_hints(
    article: dict,
    *,
    container_opus_id: str | None = None,
    exclude_link_text: re.Pattern | None = None,
    default_hint: LotteryHint | None = None,
) -> tuple[list[str], dict[str, LotteryHint]]:
    """从专栏/Opus 帖结构化正文提取活动链接及分区提示。"""
    opus = article.get("opus") or {}
    content = opus.get("content")
    if not content:
        raise RuntimeError("内容缺少 opus 结构化正文，无法提取活动链接")

    paragraphs = content.get("paragraphs") or []
    seen: set[str] = set()
    links: list[str] = []
    hints: dict[str, LotteryHint] = {}
    current_section: LotteryHint | None = default_hint

    for para in paragraphs:
        text_nodes = (para.get("text") or {}).get("nodes") or []
        para_text = "".join(
            (node.get("word") or {}).get("words", "")
            for node in text_nodes
            if node.get("node_type") == 1
        )
        current_section = _section_hint_from_text(para_text, current_section)
        skip_links = bool(EXCLUDE_CONTEXT_RE.search(para_text))

        for node in text_nodes:
            if node.get("node_type") != 4:
                continue

            link_obj = node.get("link") or {}
            show_text = link_obj.get("show_text") or ""
            if exclude_link_text and (
                exclude_link_text.search(show_text) or exclude_link_text.search(para_text)
            ):
                continue

            opus_id = parse_opus_id(link_obj)
            if not opus_id:
                continue
            if container_opus_id and opus_id == container_opus_id:
                continue
            if opus_id in seen or skip_links:
                continue

            seen.add(opus_id)
            links.append(opus_link(opus_id))
            if current_section:
                hints[opus_id] = current_section

    return links, hints


def extract_opus_links_from_article(
    article: dict,
    *,
    container_opus_id: str | None = None,
    exclude_link_text: re.Pattern | None = None,
    default_hint: LotteryHint | None = None,
) -> list[str]:
    """从专栏/Opus 帖结构化正文提取去重后的活动链接。"""
    links, _ = extract_opus_links_with_hints(
        article,
        container_opus_id=container_opus_id,
        exclude_link_text=exclude_link_text,
        default_hint=default_hint,
    )
    return links


CONTAINER_DATE_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|\d{1,2}\.\d{1,2}"
)
CONTAINER_DRAW_RE = re.compile(r"开奖|抽取|抽出|开抽|揭晓|LT\s*[:：]")


def _opus_paragraph_rows(article: dict) -> list[tuple[str, list[str]]]:
    content = (article.get("opus") or {}).get("content")
    if not content:
        return []
    rows: list[tuple[str, list[str]]] = []
    for para in content.get("paragraphs") or []:
        nodes = (para.get("text") or {}).get("nodes") or []
        text = "".join(
            (node.get("word") or {}).get("words", "")
            for node in nodes
            if node.get("node_type") == 1
        ).strip()
        opus_ids = []
        for node in nodes:
            if node.get("node_type") != 4:
                continue
            opus_id = parse_opus_id(node.get("link") or {})
            if opus_id:
                opus_ids.append(opus_id)
        rows.append((text, opus_ids))
    return rows


def _container_evidence_value(evidence: str, reference_ts: int | None) -> str | None:
    from datetime import datetime

    from src.lottery_time import BEIJING_TZ, normalize_lottery_time_text

    absolute = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+(\d{1,2}):([0-5]\d))?", evidence)
    if absolute:
        year, month, day, hour, minute = absolute.groups()
        try:
            return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return None
    match = CONTAINER_DATE_RE.search(evidence)
    clause = evidence[match.start():] if match else evidence
    reference = datetime.fromtimestamp(reference_ts, BEIJING_TZ) if reference_ts else None
    return normalize_lottery_time_text(clause, ref=reference)


def extract_opus_link_time_hints(article: dict, *, reference_ts: int) -> dict[str, dict]:
    """合集/清单贴自带「开奖日期」时，把它贴到对应活动上。

    逐条优先：链接所在段落自带「开奖+日期」；否则用上方最近一条「开奖+日期」
    标题，且标题日期必须与本条自己写的日期一致。候选仍走
    validate_extracted_times，证据必须是正文原句，过了闸门才返回。
    """
    rows = _opus_paragraph_rows(article)
    if not rows or not reference_ts:
        return {}
    from src.time_evidence import validate_extracted_times

    content = "\n".join(text for text, _ in rows)
    hints: dict[str, dict] = {}
    pending: str | None = None
    for text, opus_ids in rows:
        if text in ("互动抽奖", "预约抽奖"):
            pending = None
            continue
        own_date = CONTAINER_DATE_RE.search(text)
        own_draw = bool(CONTAINER_DRAW_RE.search(text))
        if own_date and own_draw:
            pending = text
        evidence = None
        if opus_ids:
            own_value = _container_evidence_value(text, reference_ts) if own_date else None
            if own_draw and own_date:
                evidence = text
            elif own_date:
                heading_value = _container_evidence_value(pending, reference_ts) if pending else None
                evidence = pending if heading_value and heading_value == own_value else None
            else:
                evidence = pending
        if own_date and not own_draw and not opus_ids:
            # 「结束:2026-10-21 09:00」这类字段不是开奖句，别让它后面的链接沾上旧标题
            pending = None
        if not evidence:
            continue
        value = _container_evidence_value(evidence, reference_ts)
        precision = "minute" if re.search(r"\d{1,2}[:：]\d{2}", evidence) else "date"
        checked = validate_extracted_times(
            {
                "lottery_time": value,
                "lottery_time_evidence": evidence,
                "lottery_time_precision": precision,
            },
            content,
            reference_ts=reference_ts,
        )
        if not checked.get("lottery_time_verified"):
            continue
        for opus_id in opus_ids:
            hints.setdefault(
                opus_id,
                {
                    "lottery_time": checked["lottery_time"],
                    "lottery_time_evidence": checked["lottery_time_evidence"],
                    "lottery_time_precision": precision,
                },
            )
    return hints
