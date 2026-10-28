"""按序号查活动：`activity_by_number.py 1372` 或 `--queue 20` 列出三连队列前 20 条带序号。

序号存在 `activity_serials` 表，每条活动一个、永不变更/复用，所以你说「#1372」我就能定到唯一一条。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bilibinggo"))
TZ = "Asia/Shanghai"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


def show(seq: int) -> int:
    from src.activity_serials import id_for_seq
    from src.db.activity_codec import row_to_activity_dict
    from src.db.models import ActivityRow
    from src.db.session import session_scope
    from src.participation_guard import get_guard

    dynamic_id = id_for_seq(seq)
    if not dynamic_id:
        print(f"没有 #{seq} 这个序号（当前最大序号见 --stats）")
        return 2
    with session_scope() as session:
        row = session.get(ActivityRow, dynamic_id)
        item = row_to_activity_dict(row) if row is not None else {}
    guard = get_guard(dynamic_id) or {}
    conditions = item.get("conditions") or {}
    draw = item.get("lottery_time")
    print(f"#{seq} ＝ {dynamic_id}")
    print(f"  标题/奖品：{json.dumps(item.get('prizes') or [], ensure_ascii=False)[:200]}")
    print(f"  类型 {item.get('lottery_type')}｜状态 {item.get('activity_status')}｜热度 {item.get('repost_count')}")
    print(f"  开奖 {time.strftime('%m-%d %H:%M', time.localtime(draw)) if draw else '未知'}"
          f"｜截止 {time.strftime('%m-%d %H:%M', time.localtime(conditions.get('participation_deadline') or 0)) if conditions.get('participation_deadline') else '未知'}")
    print(f"  时间来源 {conditions.get('lottery_time_source')}｜证据 {str(conditions.get('lottery_time_evidence'))[:70]!r}")
    print(f"  守卫 {guard.get('state')}｜原因 {str(guard.get('reason') or '')[:80]}")
    print(f"  人工判定 {item.get('time_review') or '-'}")
    print(f"  链接 {item.get('source_url')}")
    return 0


def list_queue(limit: int) -> int:
    from web.activity_service import list_activities

    rows, page = [], 1
    while page <= 40:
        payload = list_activities(page=page, page_size=20)
        items = payload.get("items") or []
        rows += [item for item in items if item.get("auto_can_participate")]
        if len(items) < 20:
            break
        page += 1
    queue = [t["dynamic_id"] for t in list_activities(page=1)["triple_targets"]["items"]]
    print(f"三连口径共 {len(rows)} 条，面板当前要参加的 3 条：{queue}")
    from src.activity_archive import archived_map
    marks = archived_map()
    for item in rows[:limit]:
        draw = item.get("lottery_time_unix")
        print(f"  #{item.get('seq')}  {item.get('activity_status'):4s}｜开奖 {time.strftime('%m-%d %H:%M', time.localtime(draw)) if draw else '未知':12s}"
              f"｜{(item.get('activity_title') or '')[:34]}｜{item.get('dynamic_id')}"
              + ("｜已归档" if item.get("dynamic_id") in marks else ""))
    if len(rows) > limit:
        print(f"  ...其余 {len(rows) - limit} 条未列出（--queue N 调整）")
    return 0


def up_activities(uid: int) -> int:
    from src.activity_serials import by_uid
    from src.db.activity_codec import row_to_activity_dict
    from src.db.models import ActivityRow
    from src.db.session import session_scope

    rows = sorted(by_uid(uid), key=lambda r: r[1])
    print(f"UP uid={uid} 共 {len(rows)} 条活动：")
    with session_scope() as session:
        for dynamic_id, seq, _uid in rows:
            item = row_to_activity_dict(session.get(ActivityRow, dynamic_id)) if session.get(ActivityRow, dynamic_id) else {}
            print(f"  #{seq} {item.get('activity_status') or '-':4s}｜{str((item.get('prizes') or [{}])[0].get('description') or '')[:40]}｜{dynamic_id}")
    return 0


def duplicate_forwards() -> int:
    """按 UP uid 体检重复转发：列出我们转发过的 UP，以及同一 UP 被转 ≥2 次的情况。

    真危险的是「同一条抽奖换了壳又被转一遍」（跨期合集里同一场抽奖的两个 opus id）；
    同一 UP 的两场不同抽奖是正常参与，只作提示。链根相同的才算重复。"""
    import sqlite3

    from src.activity_serials import map_all
    from src.participation_guard import get_guard

    from src.db.engine import db_path

    con = sqlite3.connect(str(db_path()))
    con.row_factory = sqlite3.Row
    joined = {str(r["dynamic_id"]) for r in con.execute("select distinct dynamic_id from participation_actions where status='joined'")}
    serials = map_all()
    by_up: dict[int, list[str]] = {}
    for row in con.execute("select dynamic_id, payload_json from activities"):
        did = str(row["dynamic_id"])
        if did not in joined:
            continue
        guard = get_guard(did) or {}
        detail = str(((guard.get("progress") or {}).get("follow") or {}).get("detail") or "")
        match = re.search(r"uid=(\d{4,})", detail)
        uid = int(match.group(1)) if match else 0
        if uid:
            by_up.setdefault(uid, []).append(did)
    def chain_root(did: str) -> str:
        # 有链根按链根算（同一场抽奖换壳会出现两个 opus id）；没有链信息的按自身 id，绝不猜测。
        row = con.execute("select payload_json from activities where dynamic_id=?", (did,)).fetchone()
        chain = ((json.loads(row["payload_json"] or "{}").get("conditions") or {}).get("repost_chain_ids") or []) if row else []
        return str(chain[-1]) if chain else did

    multi = {uid: ids for uid, ids in by_up.items() if len(ids) >= 2}
    risky = {uid: ids for uid, ids in multi.items() if len({chain_root(d) for d in ids}) < len(ids)}
    print(f"已转发过的 UP 共 {len(by_up)} 位；同一 UP 有 ≥2 条的 {len(multi)} 位"
          f"（多场抽奖是正常的）；其中存在「同一条链被转两次」风险的 {len(risky)} 位")
    for uid, ids in sorted(risky.items(), key=lambda kv: -len(kv[1]))[:10]:
        roots: dict[str, list[str]] = {}
        for d in ids:
            roots.setdefault(chain_root(d), []).append(d)
        dup = {r: v for r, v in roots.items() if len(v) > 1}
        print(f"  UP {uid}：{len(ids)} 条，链根 {len(roots)} 种，重复链根 {len(dup)} 处 "
              f"#{'/'.join(str(serials.get(v[0], '?')) for v in dup.values())}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("seq", nargs="?", type=int, help="活动序号 #N")
    ap.add_argument("--queue", type=int, default=0, help="列出三连队列前 N 条（带序号）")
    ap.add_argument("--stats", action="store_true", help="序号总量与最大值")
    ap.add_argument("--up", type=int, default=0, help="列出某个抽奖 UP（uid）的全部活动")
    ap.add_argument("--dupes", action="store_true", help="按 UP 体检重复转发风险")
    args = ap.parse_args()

    if args.dupes:
        return duplicate_forwards()
    if args.up:
        return up_activities(args.up)
    if args.stats:
        from src.activity_serials import map_all
        m = map_all()
        print(f"已分配序号 {len(m)} 个｜最大 #{max(m.values()) if m else 0}")
        return 0
    if args.queue:
        return list_queue(args.queue)
    if args.seq:
        return show(args.seq)
    print("用法：activity_by_number.py 1372 ｜ --queue 20 ｜ --stats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
