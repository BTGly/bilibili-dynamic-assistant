"""离线重解析：用昨晚已读回的正文快照喂 LLM 再判一次开奖时间，**完全不访问 B 站**。

为什么这么干：`refresh_work` 里那 86 条「重排解析」永远不会被刷新窗口捞起来——
流水线的取数条件是 `checkpoints.pending_ids() - known_activity_ids()`，这些行早已在库里，
属于「已知」，所以被过滤掉了（10-27 09:10 实测 09:00 窗口 pending 一条没动）。
但正文我已经逐条读过并存在 `data/_page_reads.json`，可以直接喂模型，零风控风险。

写回标准和昨晚一样：模型给的时间必须能被**证据原句逐字复算**出来（`validate_extracted_times`），
否则丢弃；`--apply` 前整库备份、逐行 before 快照进 `data/_llm_reread_log.json`。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bilibinggo"))
DB = ROOT / "bilibinggo" / "data" / "binggo.db"
READS = ROOT / "bilibinggo" / "data" / "_page_reads.json"
LOG = ROOT / "bilibinggo" / "data" / "_llm_reread_log.json"
TZ = timezone(timedelta(hours=8))
DISPLAY = "%Y-%m-%d %H:%M"
STOP_MARKS = ("402", "余额", "insufficient", "arrearage", "429", "rate limit", "无法连接", "暂时不可用")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


def targets() -> list[dict]:
    reads = json.loads(READS.read_text(encoding="utf-8"))
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = []
    for row in con.execute("select dynamic_id,lottery_type,activity_status,payload_json from activities "
                           "where lottery_time is null and skipped=0 and activity_status in ('未参加','待审核')"):
        rec = reads.get(row["dynamic_id"])
        if not rec or not (rec.get("text") or "").strip():
            continue
        out.append({"dynamic_id": row["dynamic_id"], "type": row["lottery_type"],
                    "status": row["activity_status"], "text": rec["text"],
                    "ref_ts": rec.get("ref_ts")})
    con.close()
    return out


def resolve(item: dict) -> dict | None:
    from src.forward_parser import parse_forward_content

    parsed = parse_forward_content(item["dynamic_id"], item["text"], reference_ts=item["ref_ts"] or None)
    patch = {}
    for field in ("lottery_time", "participation_deadline"):
        if parsed.get(field + "_verified") and parsed.get(field):
            patch[field] = str(parsed[field])
            patch[field + "_evidence"] = str(parsed.get(field + "_evidence") or "")
            patch[field + "_precision"] = str(parsed.get(field + "_precision") or "date")
    if not patch:
        return None
    patch["from_cache"] = bool(parsed.get("from_cache"))
    return patch


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.2)
    args = ap.parse_args()

    pool = targets()
    if args.limit > 0:
        pool = pool[: args.limit]
    print(f"离线重解析候选 {len(pool)} 条（正文已缓存，不访问 B 站）")

    from src.llm_client import is_provider_blocked

    plans, stopped = [], ""
    for index, item in enumerate(pool, start=1):
        try:
            patch = resolve(item)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if is_provider_blocked(message) or any(mark in message.lower() for mark in STOP_MARKS):
                stopped = message[:110]
                print(f"[服务商受阻] 第 {index} 条中止：{stopped}")
                break
            print(f"[跳过] {item['dynamic_id']} 解析失败：{message[:90]}")
            continue
        if patch:
            plans.append({"dynamic_id": item["dynamic_id"], "type": item["type"], "status": item["status"], "patch": patch})
            got = "；".join(f"{key}={patch[key]}" for key in ("lottery_time", "participation_deadline") if key in patch)
            ev = (patch.get("lottery_time_evidence") or patch.get("participation_deadline_evidence") or "")[:44]
            print(f"  [{index}/{len(pool)}] {item['dynamic_id']} → {got}｜证据「{ev}」")
        if not stopped and index < len(pool):
            time.sleep(args.sleep)

    print(f"重解析出时间 {len(plans)} 条（将来 {sum(1 for p in plans if p['patch'].get('lottery_time') and datetime.strptime(p['patch']['lottery_time'], DISPLAY).replace(tzinfo=TZ).timestamp() > time.time())}）")
    if not plans:
        print("没有可写回的（模型给的时间都过不了逐字复算闸门，或服务商受阻）")
        return 3 if stopped else 0
    if not args.apply:
        print("[预演] 加 --apply 才写库")
        return 3 if stopped else 0

    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "bilibinggo" / "data" / f"backup-before-llm-reread-{stamp}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()
    src.close()
    print("整库备份 →", backup.name)

    from src.activity_store import update_activity
    from src.lottery_time import normalize_activity_timing

    entries = []
    for plan in plans:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        before = con.execute("select dynamic_id,lottery_time,activity_status,payload_json,updated_at from activities where dynamic_id=?",
                             (plan["dynamic_id"],)).fetchone()
        con.close()
        item = json.loads(before["payload_json"] or "{}")
        item["dynamic_id"] = plan["dynamic_id"]
        conditions = dict(item.get("conditions") or {})
        for field in ("lottery_time", "participation_deadline"):
            if field not in plan["patch"]:
                continue
            stamp_value = int(datetime.strptime(plan["patch"][field], DISPLAY).replace(tzinfo=TZ).timestamp())
            conditions[field] = stamp_value
            conditions[field + "_evidence"] = plan["patch"][field + "_evidence"]
            conditions[field + "_precision"] = plan["patch"][field + "_precision"]
            conditions[field + "_source"] = "llm_reread_2026-10-27"
            conditions[field + "_text"] = plan["patch"][field]
            if field == "lottery_time":
                item["lottery_time"] = stamp_value
            else:
                item["participation_deadline"] = stamp_value
        conditions["time_extraction_checked"] = True
        item["conditions"] = conditions
        normalize_activity_timing(item)
        update_activity(plan["dynamic_id"], item)
        entries.append({"dynamic_id": plan["dynamic_id"], "at": stamp, "backup": backup.name,
                        "patch": plan["patch"], "new_status": item.get("activity_status"),
                        "before": {k: before[k] for k in before.keys()}})
        print("  [写回]", plan["dynamic_id"], "→", item.get("activity_status"))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成 {len(entries)} 行，before 快照 → {LOG.name}")
    return 3 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
