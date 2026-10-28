"""离线补时间：库里有一批行，LLM 已经把「原句」当证据存下来了（evidence 在解析时就验证过是正文子串），
但旧的关键词闸门太窄（只认 开奖/抽取/抽出/开抽/揭晓/LT:），把「10月17日抽：」「10.30抽一位」「揪1位」这类判成时间未知。
现在关键词放宽了，就用**已存的证据句**在本地重新过一遍闸门，把能确定的开奖时间补回去——不访问 B 站。

默认只预演；--apply 才写库（写前 sqlite backup API 整库备份，逐行 before 快照落 data/_evidence_repair_log.json）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bilibinggo"))
DB = ROOT / "bilibinggo" / "data" / "binggo.db"
LOG = ROOT / "bilibinggo" / "data" / "_evidence_repair_log.json"
TZ = timezone(timedelta(hours=8))

DATE_TOKEN_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|\d{1,2}\.\d{1,2}"
    r"|(?:本月|这个月)?月底|下(?:周|星期|礼拜)[一二三四五六日天]"
)
DISPLAY = "%Y-%m-%d %H:%M"


def _candidate(dynamic_id: str, conditions: dict) -> dict | None:
    from src.lottery_time import normalize_lottery_time_text
    from src.time_evidence import validate_extracted_times

    evidence = str(conditions.get("lottery_time_evidence") or "").strip()
    if not evidence:
        return None
    if len(DATE_TOKEN_RE.findall(evidence)) != 1:      # 一句里多个日期 → 仍然歧义，不猜
        return None
    reference_ts = conditions.get("time_reference_ts")
    reference = datetime.fromtimestamp(reference_ts, TZ) if reference_ts else None
    value = normalize_lottery_time_text(evidence, ref=reference)
    if not value:
        return None
    precision = "minute" if re.search(r"\d{1,2}[:：]\d{2}|\d{1,2}点", evidence) else "date"
    checked = validate_extracted_times(
        {"lottery_time": value, "lottery_time_evidence": evidence, "lottery_time_precision": precision},
        evidence,
        reference_ts=reference_ts,
    )
    if not checked.get("lottery_time_verified"):
        return None
    try:
        stamp = int(datetime.strptime(str(checked["lottery_time"]), DISPLAY).replace(tzinfo=TZ).timestamp())
    except ValueError:
        return None
    return {
        "lottery_time": stamp,
        "lottery_time_evidence": evidence,
        "lottery_time_precision": checked.get("lottery_time_precision") or precision,
        "lottery_time_text": str(checked["lottery_time"]),
        "lottery_time_source": "stored_evidence_repair",
        "time_extraction_checked": True,
    }


def collect() -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select dynamic_id,lottery_type,lottery_time,activity_status,skipped,payload_json "
        "from activities where lottery_time is null and activity_status in ('未参加','待审核') and skipped=0"
    ).fetchall()
    con.close()
    out = []
    for row in rows:
        item = json.loads(row["payload_json"] or "{}")
        conditions = dict(item.get("conditions") or {})
        repair = _candidate(row["dynamic_id"], conditions)
        if not repair:
            continue
        item["conditions"] = {**conditions, **repair}
        item["lottery_time"] = repair["lottery_time"]
        out.append({"dynamic_id": row["dynamic_id"], "type": row["lottery_type"],
                    "old_status": row["activity_status"], "item": item, "repair": repair})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    plans = collect()
    now = time.time()
    future = [p for p in plans if p["repair"]["lottery_time"] > now]
    past = [p for p in plans if p["repair"]["lottery_time"] <= now]
    print(f"可补时间的行: {len(plans)} | 补完仍在开奖前: {len(future)} | 补完发现已过期(将转为已截止/已结束): {len(past)}")
    for p in plans[:40]:
        when = datetime.fromtimestamp(p["repair"]["lottery_time"], TZ).strftime("%m-%d %H:%M")
        print(f"  {p['dynamic_id']} {p['type']:5s} {p['old_status']} → 开奖 {when} ｜ 证据「{p['repair']['lottery_time_evidence'][:46]}」")
    if len(plans) > 40:
        print(f"  ...其余 {len(plans) - 40} 条见预演清单")
    if not args.apply:
        print("[预演] 加 --apply 才写库")
        return 0

    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "bilibinggo" / "data" / f"backup-before-evidence-repair-{stamp}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()
    src.close()
    print("整库备份 →", backup.name)

    from src.activity_store import update_activity
    from src.lottery_time import normalize_activity_timing

    entries = []
    for p in plans:
        con = sqlite3.connect(DB)
        before = con.execute(
            "select dynamic_id,lottery_time,activity_status,payload_json,updated_at from activities where dynamic_id=?",
            (p["dynamic_id"],),
        ).fetchone()
        con.close()
        item = dict(p["item"])
        item["dynamic_id"] = p["dynamic_id"]
        normalize_activity_timing(item)          # 让库里的状态列与补出的时间一致（过期的转已截止/已结束）
        update_activity(p["dynamic_id"], item)
        entries.append({"dynamic_id": p["dynamic_id"], "at": stamp, "backup": backup.name,
                        "before": {"lottery_time": before[1], "activity_status": before[2],
                                   "payload_json": before[3], "updated_at": before[4]},
                        "repair": p["repair"]})
        print("  [写回]", p["dynamic_id"], "→", datetime.fromtimestamp(p["repair"]["lottery_time"], TZ).strftime(DISPLAY))

    LOG.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成 {len(entries)} 行，before 快照 → {LOG.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
