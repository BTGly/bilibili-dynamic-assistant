"""攒数据用：正文里有相对开奖说法、却被判「开奖时间未知」的活动计数。

10-28 的存量测试（台账 §79）只能说明「那批困难样本里相对说法≈0 条」，说明不了新增活动里
这种写法常不常见；而要不要给解析器加相对时长换算，取决于后者。所以富化时顺手记一笔。

只写一个 JSON 计数文件，不进业务表、不参与任何判定；任何异常都必须被调用方吞掉，
不能因为统计而拖垮入库。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# 「两周后 / 3 天后 / 半个月后 / 下下周 / 明天 / 今晚」这类需要换算的说法。
# 这里只是"疑似有相对说法"的粗筛，不做算术，所以宁可宽一点。
RELATIVE_TIME_RE = re.compile(
    r"(?:半|[一二两俩三四五六七八九十百]|\d+)\s*(?:个)?\s*(?:天|日|周|星期|礼拜|月|小时)\s*(?:之?后|以后|过后|左右)"
    r"|明天|后天|大后天|今晚|今夜|下(?:周|星期|礼拜)|下个月|本月内|这个月内|周末"
)
MAX_SAMPLES_PER_DAY = 30


def stats_path(data_dir: Path) -> Path:
    return Path(data_dir) / "_relative_time_watch.json"


def relative_snippet(text: str) -> str:
    """返回第一处疑似相对说法所在的短句（截断，够我回看是谁写的）。"""
    match = RELATIVE_TIME_RE.search(text or "")
    if not match:
        return ""
    start = max(text.rfind("\n", 0, match.start()) + 1, match.start() - 24)
    end = text.find("\n", match.end())
    return text[start:end if end > 0 else min(len(text), match.end() + 24)].strip()[:90]


def note_candidate(path: Path, dynamic_id: str, snippet: str, *, now: float | None = None) -> dict:
    """记一条「正文疑似有相对开奖说法但没拿到时间」。返回写入后的汇总。"""
    day = time.strftime("%m-%d", time.localtime(now if now is not None else time.time()))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            data = {"days": {}}
    except (OSError, ValueError):
        data = {"days": {}}
    bucket = data["days"].setdefault(day, {"count": 0, "samples": []})
    bucket["count"] = int(bucket.get("count") or 0) + 1
    samples = bucket.setdefault("samples", [])
    if len(samples) < MAX_SAMPLES_PER_DAY:
        samples.append({"id": str(dynamic_id), "snippet": snippet})
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # 只留最近 30 天，文件不会无限长
    if len(data["days"]) > 30:
        for key in sorted(data["days"])[: len(data["days"]) - 30]:
            data["days"].pop(key, None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def summary(path: Path) -> dict:
    """给命令行看的汇总：总次数、按天、样例。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"total": 0, "days": {}}
    days = data.get("days") or {}
    return {
        "total": sum(int(b.get("count") or 0) for b in days.values()),
        "days": {k: int(v.get("count") or 0) for k, v in sorted(days.items())},
        "samples": {k: (v.get("samples") or [])[:5] for k, v in sorted(days.items())[-3:]},
    }
