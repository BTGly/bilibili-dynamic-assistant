"""把「库里写已参加、但平台现在证明我们根本没符合条件」的行放回三连队列。

默认只预演，加 --apply 才写库；写库前强制：
  1) 平台只读复核（抽奖仍 active、开奖时间未到、followed=false）——证据不在就拒绝；
  2) 库里没有任何 joined 动作记录；
  3) sqlite3 backup API 整库备份 + 逐行 before 快照落 data/_reopen_log.json。
回滚：把 _reopen_log.json 里的 before 原样写回即可（脚本 --restore <动态ID>）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "bilibinggo"
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "binggo.db"
LOG = ROOT / "data" / "_reopen_log.json"
TZ = timezone(timedelta(hours=8))


def stamp() -> str:
    return datetime.now(TZ).strftime("%Y%m%d-%H%M%S")


def verify_platform(dynamic_id: str, business_id: str, business_type: int) -> dict:
    from src.bilibili_client import BilibiliClient
    from src.lottery_api import fetch_lottery_notice
    from src.sources.common import opus_link

    with BilibiliClient() as client:
        notice = fetch_lottery_notice(
            client,
            business_id=str(business_id),
            business_type=int(business_type),
            referer=opus_link(dynamic_id),
        )
    if not notice:
        raise RuntimeError("平台复核失败：读不到抽奖信息")
    if int(notice.get("status") or 0) != 0:
        raise RuntimeError(f"平台复核：抽奖状态 status={notice.get('status')}，不是可参与状态")
    draw = int(notice.get("lottery_time") or 0)
    if draw and draw <= time.time():
        raise RuntimeError("平台复核：开奖时间已过，放回队列没有意义")
    if notice.get("followed") is not False:
        raise RuntimeError(f"平台复核：followed={notice.get('followed')}，不是「明确未关注」，不动")
    return {
        "participated": notice.get("participated"),
        "followed": notice.get("followed"),
        "reposted": notice.get("reposted"),
        "need_post": notice.get("need_post"),
        "lottery_id": notice.get("lottery_id"),
        "lottery_time": draw,
        "participants": notice.get("participants"),
    }


def snapshot(dynamic_id: str) -> dict:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    act = con.execute("select * from activities where dynamic_id=?", (dynamic_id,)).fetchone()
    part = con.execute("select * from participations where dynamic_id=?", (dynamic_id,)).fetchone()
    guard = con.execute("select * from participation_guards where dynamic_id=?", (dynamic_id,)).fetchone()
    joined = con.execute(
        "select count(*) from participation_actions where dynamic_id=? and status='joined'", (dynamic_id,)
    ).fetchone()[0]
    con.close()
    if act is None:
        raise RuntimeError("库里没有这条活动")
    if joined:
        raise RuntimeError("库里已经有 joined 动作记录，这不是幽灵行")
    return {
        "activity": {k: act[k] for k in act.keys()},
        "participation": {k: part[k] for k in part.keys()} if part else None,
        "guard": {k: guard[k] for k in guard.keys()} if guard else None,
    }


def apply_reopen(dynamic_id: str, before: dict) -> None:
    from src.activity_store import update_activity
    from src.participation_store import set_participation

    item = json.loads(before["activity"]["payload_json"] or "{}")
    item["dynamic_id"] = dynamic_id
    item["activity_status"] = "未参加"
    item["platform_participated"] = False
    item["reserve_reserved"] = False
    item["user_status_source"] = "verified_reopen"
    item.setdefault("conditions", {})["reopen_proof"] = before["proof"]
    update_activity(dynamic_id, item)

    set_participation(dynamic_id, "未参加")

    con = sqlite3.connect(DB)
    uid = before["guard"]["uid"] if before.get("guard") else None
    if uid is None:
        uid = con.execute("select uid from participations where dynamic_id=?", (dynamic_id,)).fetchone()[0]
    reason = "10-25 平台复核：抽奖 active、followed=false、库里无动作记录 → 原「已参加」是假的，放回队列重参"
    if before.get("guard"):
        con.execute(
            "update participation_guards set state='ready', reason=?, progress_json='{}', updated_at=? "
            "where uid=? and dynamic_id=?",
            (reason, int(time.time()), uid, dynamic_id),
        )
    else:
        con.execute(
            "insert into participation_guards (uid, dynamic_id, state, reason, progress_json, updated_at) "
            "values (?,?,?,?,?,?)",
            (uid, dynamic_id, "ready", reason, "{}", int(time.time())),
        )
    con.commit()
    con.close()


def restore(dynamic_id: str) -> None:
    entries = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    entry = next((e for e in entries if e["dynamic_id"] == dynamic_id), None)
    if not entry:
        raise RuntimeError("日志里没有这条行的 before 快照")
    before = entry["before"]
    con = sqlite3.connect(DB)
    act = before["activity"]
    cols = ", ".join(f"{k}=?" for k in act if k != "dynamic_id")
    con.execute(f"update activities set {cols} where dynamic_id=?",
                [*list(v for k, v in act.items() if k != "dynamic_id"), dynamic_id])
    if before.get("participation"):
        p = before["participation"]
        con.execute("update participations set user_status=?, updated_at=?, source=? where dynamic_id=?",
                    (p["user_status"], p["updated_at"], p["source"], dynamic_id))
    if before.get("guard"):
        g = before["guard"]
        con.execute("update participation_guards set state=?, reason=?, progress_json=?, updated_at=? where dynamic_id=?",
                    (g["state"], g["reason"], g["progress_json"], g["updated_at"], dynamic_id))
    con.commit()
    con.close()
    print(f"[回滚] {dynamic_id} 已恢复为日志里的 before 状态")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", default="")
    args = ap.parse_args()

    if args.restore:
        restore(args.restore)
        return 0

    entries = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    targets = []
    for did in args.ids:
        row = con.execute("select dynamic_id,business_id,business_type,lottery_type,lottery_time,activity_status "
                          "from activities where dynamic_id=?", (did,)).fetchone()
        if row is None:
            print(f"[跳过] {did} 库里没有")
            continue
        targets.append(dict(row))
    con.close()

    backup = None
    plan = []
    for t in targets:
        did = t["dynamic_id"]
        print(f"== {did} {t['lottery_type']} 库里状态={t['activity_status']} "
              f"开奖={datetime.fromtimestamp(t['lottery_time'], TZ).strftime('%m-%d %H:%M') if t['lottery_time'] else '-'}")
        before = snapshot(did)
        proof = verify_platform(did, t["business_id"], t["business_type"])
        before["proof"] = proof
        print("   平台复核:", json.dumps(proof, ensure_ascii=False)[:220])
        plan.append((did, before))

    if not plan:
        print("没有可放回的行")
        return 0
    if not args.apply:
        print(f"[预演] 将放回 {len(plan)} 行：{[p[0] for p in plan]}（加 --apply 生效）")
        return 0

    dst = ROOT / "data" / f"backup-before-reopen-{stamp()}.db"
    src = sqlite3.connect(DB)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()
    print("整库备份 ->", dst)

    for did, before in plan:
        apply_reopen(did, before)
        entries.append({"dynamic_id": did, "at": stamp(), "backup": str(dst.name), "before": before})
        print(f"[写回] {did} → 未参加，守卫 ready，动作日志已清空")

    LOG.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成 {len(plan)} 行，before 快照 -> {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
