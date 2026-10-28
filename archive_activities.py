"""一键归档：给「已经不可能再参加」的活动打标，让面板默认视图只剩活跃行。

- 归档=打标，写在 `activity_archive` 表里；`activities` 行、`participations`、`participation_actions`、
  `participation_guards` 一律不动，所以「这个我参加过」的核对照旧生效，防重复转发不受影响；
- 未参加/待审核的行被规则层和执行层各挡一次，绝不会被归档吞掉三连队列；
- `--apply` 前用 sqlite backup API 整库备份，逐条留痕写 `bilibinggo/data/_archive_log.json`，可 `--undo-all` 撤销。
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
LOG = ROOT / "bilibinggo" / "data" / "_archive_log.json"
TZ = timezone(timedelta(hours=8))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真归档（默认只预演）")
    ap.add_argument("--joined-grace-days", type=int, default=0, help="已参加的行再等几天才归档")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多归档多少条")
    ap.add_argument("--reason", default="规则归档")
    ap.add_argument("--undo-all", action="store_true", help="撤销全部归档标记")
    args = ap.parse_args()

    from src.activity_archive import archive, archived_map, candidates, stats, unarchive
    from src.db.schema import init_db

    init_db()   # 首次使用会把 activity_archive 建出来（幂等）

    if args.undo_all:
        marks = archived_map()
        result = unarchive(list(marks))
        print(f"已撤销全部归档标记：{len(result['removed'])} 条 → 面板回到全量视图")
        return 0

    pool = candidates(joined_grace_days=args.joined_grace_days)
    if args.limit > 0:
        pool = pool[: args.limit]
    buckets: dict[str, int] = {}
    for row in pool:
        key = row["activity_status"]
        buckets[key] = buckets.get(key, 0) + 1
    print(f"可归档 {len(pool)} 条｜分布 {buckets}｜当前已归档 {stats()['archived']} 条")
    for row in pool[:15]:
        when = time.strftime("%m-%d %H:%M", time.localtime(row["lottery_time"])) if row["lottery_time"] else "时间未知"
        print(f"  {row['dynamic_id']} {row['activity_status']}｜开奖 {when}｜{(row['title'] or '')[:22]}｜{row['reason'][:34]}")
    if len(pool) > 15:
        print(f"  ...其余 {len(pool) - 15} 条见预演清单")
    if not args.apply:
        print("[预演] 加 --apply 才打标")
        return 0

    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "bilibinggo" / "data" / f"backup-before-archive-{stamp}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()
    src.close()
    print("整库备份 →", backup.name)

    result = archive([row["dynamic_id"] for row in pool], reason=args.reason)
    entries = []
    if LOG.exists():
        try:
            entries = json.loads(LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    for row in pool:
        if row["dynamic_id"] in result["added"]:
            entries.append({"dynamic_id": row["dynamic_id"], "at": stamp, "backup": backup.name,
                            "status": row["activity_status"], "reason": row["reason"]})
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"归档 {len(result['added'])} 条，跳过 {len(result['skipped'])} 条（仍在三连口径内的都不会被归档）")
    for item in result["skipped"][:5]:
        print("  跳过：", item["dynamic_id"], item["why"])
    print("留痕 →", LOG.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
