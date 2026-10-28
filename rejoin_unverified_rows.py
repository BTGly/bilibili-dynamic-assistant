"""把「库里写已参加、但我们自己的空间里查不到任何转发实证、且还没开奖」的行放回三连队列。

判据就是你定的那条：参加过就不参加，没参加就参加。这里用三层证据决定「参加过没有」：
1) 库里没有任何 joined 动作记录；
2) 守卫日志里也没有任何「成功动作」—— 10-22 02:00~03:00 那段动作日志有空窗，
   但守卫的 progress 仍然记着 like/follow/repost succeeded，那就是参加过的实证，必须算参加过；
3) 我们自己空间的完整动态列表里查不到对该动态的转发（_ghost_verify.json 的全量翻页结果）；
4) 开奖时间还在未来。
放回后由参与前置检查按 B 站实际状态逐条判定：已关注/已转发/已评论就跳过并留痕，
所以即使历史真参加过也不会重复发——这一层是最终仲裁。
只改本地库，写前整库备份 + 逐行 before 快照，可 --restore 回滚。
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
EVIDENCE = ROOT / "bilibinggo" / "data" / "_ghost_verify.json"
LOG = ROOT / "bilibinggo" / "data" / "_rejoin_reopen_log.json"
TZ = timezone(timedelta(hours=8))


def candidates() -> list[dict]:
    hits = set(json.loads(EVIDENCE.read_text(encoding="utf-8"))["hits"]) if EVIDENCE.exists() else set()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    now = time.time()
    joined = {r[0] for r in con.execute("select distinct dynamic_id from participation_actions where status='joined'")}
    out = []
    for row in con.execute(
        "select dynamic_id,lottery_type,lottery_time,activity_status,payload_json,skipped "
        "from activities where activity_status='已参加' and skipped=0"
    ):
        did = row["dynamic_id"]
        if did in joined or did in hits:
            continue
        if not row["lottery_time"] or row["lottery_time"] <= now:
            continue
        part = con.execute("select user_status,updated_at,source from participations where dynamic_id=?", (did,)).fetchone()
        guard = con.execute("select state,reason,progress_json from participation_guards where dynamic_id=?", (did,)).fetchone()
        progress = {}
        if guard and guard["progress_json"]:
            try:
                progress = json.loads(guard["progress_json"])
            except ValueError:
                progress = {}
        if any(item.get("state") == "succeeded" for item in progress.values() if isinstance(item, dict)):
            continue          # 守卫日志里有成功动作 = 参加过，不再放
        out.append({"dynamic_id": did, "type": row["lottery_type"],
                    "draw": datetime.fromtimestamp(row["lottery_time"], TZ).strftime("%m-%d %H:%M"),
                    "payload": row["payload_json"], "participation": dict(part) if part else None,
                    "guard": dict(guard) if guard else None})
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", default="")
    args = ap.parse_args()

    if args.restore:
        entries = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
        entry = next((e for e in entries if e["dynamic_id"] == args.restore), None)
        if not entry:
            print("日志里没有这条行的 before 快照")
            return 1
        before = entry["before"]
        con = sqlite3.connect(DB)
        con.execute("update activities set activity_status=?, payload_json=? where dynamic_id=?",
                    (before["activity_status"], before["payload_json"], args.restore))
        if before.get("participation"):
            con.execute("update participations set user_status=?, updated_at=?, source=? where dynamic_id=?",
                        (before["participation"]["user_status"], before["participation"]["updated_at"],
                         before["participation"]["source"], args.restore))
        if before.get("guard"):
            con.execute("update participation_guards set state=?, reason=?, progress_json=? where dynamic_id=?",
                        (before["guard"]["state"], before["guard"]["reason"], before["guard"]["progress_json"], args.restore))
        con.commit()
        con.close()
        print(f"[回滚] {args.restore} 已恢复 before 状态")
        return 0

    plans = candidates()
    print(f"候选（无 joined 记录 + 全量空间列表查无转发 + 未开奖）: {len(plans)} 条")
    for p in plans:
        print(f"  {p['dynamic_id']} {p['type']} 开奖 {p['draw']}")
    if not plans or not args.apply:
        print("[预演] 加 --apply 才写库" if plans else "没有要放回的行")
        return 0

    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "bilibinggo" / "data" / f"backup-before-rejoin-{stamp}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close()
    src.close()
    print("整库备份 →", backup.name)

    from src.lottery_time import normalize_activity_timing
    from src.activity_store import update_activity

    entries = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    reason = "10-26 审核：无任何转发实证且未开奖 → 放回队列；参加前置检查会按 B 站实际状态逐条判定，已做过只会跳过"
    for p in plans:
        item = json.loads(p["payload"] or "{}")
        item["dynamic_id"] = p["dynamic_id"]
        item["activity_status"] = "未参加"
        item["platform_participated"] = False
        item["reserve_reserved"] = False
        item["user_status_source"] = "review_reopen"
        (item.get("conditions") or {}).setdefault("reopen_reason", reason)
        normalize_activity_timing(item)
        update_activity(p["dynamic_id"], item)
        con = sqlite3.connect(DB)
        con.execute("update participations set user_status='未参加', updated_at=?, source='review_reopen' where dynamic_id=?",
                    (int(time.time()), p["dynamic_id"]))
        if p["guard"]:
            con.execute("update participation_guards set state='ready', reason=?, progress_json='{}' where dynamic_id=?",
                        (reason, p["dynamic_id"]))
        else:
            uid = con.execute("select uid from participations where dynamic_id=?", (p["dynamic_id"],)).fetchone()
            con.execute("insert into participation_guards (uid, dynamic_id, state, reason, progress_json, updated_at) values (?,?,'ready',?,'{}',?)",
                        (uid[0] if uid else "", p["dynamic_id"], reason, int(time.time())))
        con.commit()
        con.close()
        entries.append({"dynamic_id": p["dynamic_id"], "at": stamp, "backup": backup.name,
                        "before": {"activity_status": "已参加", "payload_json": p["payload"],
                                   "participation": p["participation"], "guard": p["guard"]}})
        print("  [放回]", p["dynamic_id"], p["type"], "开奖", p["draw"])
    LOG.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成 {len(plans)} 条，before 快照 → {LOG.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
