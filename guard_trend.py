"""只读：守卫状态计数 + 与上一次巡查的快照对比（deferred/review 异常增长预警）。
快照写在 data/_guard_trend.json，本脚本不写业务表、不做任何账号动作。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "bilibinggo" / "data" / "binggo.db"
SNAP = ROOT / "bilibinggo" / "data" / "_guard_trend.json"
TZ = timezone(timedelta(hours=8))
ALARM_DELTA = 15  # 单次巡查 review/deferred 增长超过这个数就预警
sys.path.insert(0, str(ROOT / "bilibinggo"))


def live_triple_queue_depth() -> tuple[int, int]:
    """三连队列真实深度（含 10-26 起入队的时间未知行）；纯本地读库，不发 B 站请求。
    返回 (当前能进的条数, 其中时间未知)；账号闸暂停时前者会是 0，
    所以另外报一个「面板未参加+待审核」总量，避免把「被闸挡住」读成「没活干」。"""
    try:
        from web.activity_service import list_activities, pick_triple_participate_targets

        queue = pick_triple_participate_targets(limit=200)
        backlog = sum(int(list_activities(status=st, page_size=1).get("total") or 0) for st in ("未参加", "待审核"))
    except Exception:
        return -1, -1
    print(f"   面板潜在可参加（未参加+待审核）={backlog}")
    return len(queue), sum(1 for row in queue if row.get("lottery_time_unix") is None)


def counts() -> dict[str, int]:
    con = sqlite3.connect(DB)
    try:
        rows = con.execute("select state, count(*) c from participation_guards group by state").fetchall()
        out = {state: c for state, c in rows}
        out["review_带in_flight"] = con.execute(
            "select count(*) from participation_guards where state='review' "
            "and (progress_json like '%in_flight%' or progress_json like '%\"unknown\"%')"
        ).fetchone()[0]
        out["待参加(有时间)"] = con.execute(
            "select count(*) from activities where activity_status='未参加' and lottery_time>? and skipped=0",
            (int(time.time()),),
        ).fetchone()[0]
    finally:
        con.close()
    # 队列深度必须按面板同一套实时口径算（库里的 activity_status 是旧快照，161 vs 实际 15）
    queue = live_triple_queue_depth()
    out["三连队列深度"] = queue[0]
    out["其中时间未知"] = queue[1]
    return out


def main() -> int:
    now = counts()
    prev = json.loads(SNAP.read_text(encoding="utf-8")) if SNAP.exists() else {}
    prev_counts = prev.get("counts", {})
    when = datetime.now(TZ).strftime("%m-%d %H:%M")
    line = "｜".join(f"{k}={now.get(k, 0)}" for k in ("ready", "deferred", "review", "excluded", "account_paused"))
    print(f"[{when}] 守卫计数：{line}｜review 带未确认写={now.get('review_带in_flight', 0)}｜"
          f"三连队列={now.get('三连队列深度', 0)}（其中时间未知 {now.get('其中时间未知', 0)}）")

    alarms = []
    for key in ("deferred", "review", "account_paused"):
        delta = now.get(key, 0) - prev_counts.get(key, 0)
        if prev_counts and delta >= ALARM_DELTA:
            alarms.append(f"{key} 相比上次({prev.get('at')})增长 {delta}")
    if prev_counts.get("review_带未确认写", prev_counts.get("review_带in_flight")) and now.get("review_带in_flight", 0) > prev_counts.get("review_带in_flight", 0):
        alarms.append(f"未确认写(in_flight/unknown)新增 {now['review_带in_flight'] - prev_counts.get('review_带in_flight', 0)} 条")
    if not prev_counts:
        print("   首次快照，无历史可对比")
    for a in alarms:
        print("！！", a)

    SNAP.write_text(json.dumps({"at": when, "counts": now}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if alarms else 0


if __name__ == "__main__":
    raise SystemExit(main())
