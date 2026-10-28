"""把「逐条读页面」读出来的开奖时间/截止时间写回库。

两个来源：
1. `data/_page_reads.json`——148 条时间未知活动的正文快照，字面抽取器已经定出的时间（证据是原文子串，闸门复算通过）；
2. `data/_hand_times.json`——我读完中文正文人工判定的时间，附逐字摘录的证据句（相对说法「下周的这个时候」「一周后」也在这里落到具体时刻）。

写库前用 sqlite backup API 整库备份，逐行 before 快照落到 `data/_page_read_time_log.json`，可逐行回滚。
默认只预演，`--apply` 才写。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bilibinggo"))
DB = ROOT / "bilibinggo" / "data" / "binggo.db"
READS = ROOT / "bilibinggo" / "data" / "_page_reads.json"
HAND = ROOT / "bilibinggo" / "data" / "_hand_times.json"
LOG = ROOT / "bilibinggo" / "data" / "_page_read_time_log.json"
TZ = timezone(timedelta(hours=8))
DISPLAY = "%Y-%m-%d %H:%M"
SOURCE = "page_read_2026-10-26"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


def _stamp(value: str) -> int:
    return int(datetime.strptime(value, DISPLAY).replace(tzinfo=TZ).timestamp())


def _patch(field: str, value: str, evidence: str, precision: str) -> dict:
    stamp = _stamp(value)
    return {
        field: stamp,
        field + "_evidence": evidence,
        field + "_precision": precision,
        field + "_source": SOURCE,
        field + "_text": value,
    }


def build() -> list[dict]:
    reads = json.loads(READS.read_text(encoding="utf-8"))
    hand_doc = json.loads(HAND.read_text(encoding="utf-8"))
    hand = hand_doc["rows"]
    skip = hand_doc.get("_skip") or {}
    plans: dict[str, dict] = {}

    for did, rec in reads.items():
        value = rec.get("auto_time")
        evidence = rec.get("auto_evidence") or ""
        if not value or not evidence:
            continue
        if did in skip:
            print(f"[人工否决] {did} 自动定出 {value}：{skip[did]}")
            continue
        text = rec.get("text") or ""
        if evidence not in text:
            print(f"[跳过] {did} 自动证据不是正文子串（快照已过期，下轮重读）")
            continue
        plans[did] = {"dynamic_id": did, "field": "lottery_time", "value": value,
                      "evidence": evidence,
                      "precision": rec.get("auto_precision") or ("minute" if not value.endswith(" 00:00") else "date"),
                      "origin": "literal", "ref_ts": rec.get("ref_ts"), "text": text}

    from src.time_evidence import literal_date_value

    for row in hand:
        did = row["dynamic_id"]
        rec = reads.get(did)
        if rec is None:
            print(f"[跳过] {did} 没有页面快照")
            continue
        text = rec.get("text") or ""
        evidence = row["evidence"]
        if evidence not in text:
            print(f"[跳过] {did} 人工证据不是正文子串：{evidence[:40]}")
            continue
        reference = datetime.fromtimestamp(rec.get("ref_ts"), TZ) if rec.get("ref_ts") else None
        proof, _ = literal_date_value(evidence, reference=reference)
        flag = "" if proof in ("", row["value"]) else f"  ⟲本地复算={proof or '无'}（人工裁定，仅登记）"
        plans[did] = {"dynamic_id": did, "field": row["field"], "value": row["value"],
                      "evidence": evidence,
                      "precision": "minute" if not row["value"].endswith(" 00:00") else "date",
                      "origin": "hand", "note": row.get("note", ""), "flag": flag,
                      "ref_ts": rec.get("ref_ts"), "text": text}

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = []
    for plan in plans.values():
        row = con.execute("select dynamic_id,lottery_type,activity_status,lottery_time,payload_json from activities where dynamic_id=?",
                          (plan["dynamic_id"],)).fetchone()
        if row is None:
            print(f"[跳过] {plan['dynamic_id']} 库里已没有这一行")
            continue
        if row["lottery_time"] is not None:
            continue          # 已经补上过，不重复写
        if row["activity_status"] not in ("未参加", "待审核"):
            continue          # 只有还没参加的才需要时间
        stamp = _stamp(plan["value"])
        plan["row"] = dict(row)
        plan["future"] = stamp > time.time()
        out.append(plan)
    con.close()
    return sorted(out, key=lambda p: (p["field"], p["value"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    plans = build()
    future = [p for p in plans if p["future"]]
    past = [p for p in plans if not p["future"]]
    print(f"待写回 {len(plans)} 行｜开奖/截止仍在将来（会排进三连队列）{len(future)}｜已在过去（将转为已截止/已结束，退出队列）{len(past)}")
    for p in plans:
        mark = "将来" if p["future"] else "已过期"
        print(f"  {p['dynamic_id']} {p['row']['lottery_type']} {p['row']['activity_status']}｜{p['field']}={p['value']}（{mark}）"
              f"｜{'人工' if p['origin'] == 'hand' else '字面'}｜证据「{p['evidence'][:44].replace(chr(10), '⏎')}」{p.get('flag', '')}")
    if not args.apply:
        print("[预演] 加 --apply 才写库")
        return 0

    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "bilibinggo" / "data" / f"backup-before-page-read-times-{stamp}.db"
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
        con.row_factory = sqlite3.Row
        before = con.execute("select dynamic_id,lottery_time,activity_status,payload_json,updated_at from activities where dynamic_id=?",
                             (p["dynamic_id"],)).fetchone()
        con.close()
        item = json.loads(before["payload_json"] or "{}")
        item["dynamic_id"] = p["dynamic_id"]
        conditions = dict(item.get("conditions") or {})
        conditions.update(_patch(p["field"], p["value"], p["evidence"], p["precision"]))
        conditions["time_extraction_checked"] = True
        conditions.setdefault("time_reference_ts", p["ref_ts"])
        item["conditions"] = conditions
        if p["field"] == "lottery_time":
            item["lottery_time"] = conditions["lottery_time"]
        else:
            item["participation_deadline"] = conditions["participation_deadline"]
        normalize_activity_timing(item)
        update_activity(p["dynamic_id"], item)
        entries.append({"dynamic_id": p["dynamic_id"], "at": stamp, "backup": backup.name,
                        "field": p["field"], "value": p["value"], "evidence": p["evidence"],
                        "origin": p["origin"], "note": p.get("note", ""),
                        "new_status": item.get("activity_status"),
                        "before": {k: before[k] for k in before.keys()}})
        print("  [写回]", p["dynamic_id"], p["field"], p["value"], "→", item.get("activity_status"))

    LOG.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成 {len(entries)} 行，before 快照 → {LOG.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
