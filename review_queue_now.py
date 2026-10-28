"""审核并归队：开奖时间在今日之后的行，必须进三连队列；拿不到时间的重新排队解析。

只做本地库/队列改写（不发送任何参与、关注类账号动作）：
1) 重新解析队列：时间未知、且库里连证据句都没有的行 → 放回 refresh_work=pending，
   交给下一次刷新窗口用「已修复的抽取器 + 可用 LLM」重读一次；
2) 归队审计：逐行看「开奖时间>现在」的行是否真在队列里，不在的必须给出口径可查的原因。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bilibinggo"))
DB = ROOT / "bilibinggo" / "data" / "binggo.db"
TZ = timezone(timedelta(hours=8))


def unknown_without_evidence() -> list[str]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = []
    for row in con.execute(
        "select dynamic_id,payload_json from activities "
        "where lottery_time is null and skipped=0 and activity_status in ('未参加','待审核')"
    ):
        conditions = (json.loads(row["payload_json"] or "{}").get("conditions") or {})
        if str(conditions.get("lottery_time_evidence") or "").strip():
            continue
        out.append(row["dynamic_id"])
    con.close()
    return out


def requeue_for_parse(ids: list[str]) -> tuple[int, int, int]:
    """时间未知且没有证据句的行：已入库的关掉假队列，只有还没入库的才真的排队等新链接流水线。

    10-27 复核：`run_new_links_pipeline` 取的是 `checkpoints.pending_ids() - known_activity_ids()`，
    已在 activities 里的行属于「已知」，被过滤掉 → 昨晚我把 86 行重置成 pending 指望今天窗口重读，
    那条路根本走不通（09:00 窗口一条没动）。而且这些动态的正文已逐条人工读过、解析缓存也命中，
    重读不会有新时间，所以这里改成标记 skipped，把假队列关掉，别让后续巡查一直盯着一个不降的数字。
    """
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    now = int(time.time())
    closed = kept = added = 0
    reason_closed = "10-27 复核：正文已逐条人工读过、解析缓存命中，重读不产生新时间 → 关掉假队列（原 10-26 重排）"
    # 先总清理：所有「行已入库 + 挂着我自己的重排标记」的 pending 都是走不通的假队列（不限本轮 ids）
    sweep = con.execute(
        "update refresh_work set state='skipped', reason=?, updated_at=? "
        "where state='pending' and reason like '%重排解析%' and dynamic_id in (select dynamic_id from activities)",
        (reason_closed, now),
    )
    closed += int(sweep.rowcount or 0)
    if not ids:
        con.commit()
        con.close()
        return closed, 0, 0
    for did in ids:
        in_library = con.execute("select 1 from activities where dynamic_id=?", (did,)).fetchone()
        cur = con.execute("select state from refresh_work where dynamic_id=?", (did,)).fetchone()
        if in_library:
            if cur and cur["state"] == "pending":
                con.execute("update refresh_work set state='skipped', reason=?, updated_at=? where dynamic_id=?",
                            (reason_closed, now, did))
                closed += 1
            else:
                kept += 1
            continue
        if cur:
            if cur["state"] != "pending":
                con.execute("update refresh_work set state='pending', attempts=0, retry_at=0, classify_json='', "
                            "reason=?, updated_at=? where dynamic_id=?",
                            ("10-27 复核：尚未入库的新链接，排队由流水线处理", now, did))
                added += 1
        else:
            con.execute("insert into refresh_work (dynamic_id, state, classify_json, attempts, retry_at, reason, updated_at) "
                        "values (?, 'pending', '{}', 0, 0, ?, ?)",
                        (did, "10-27 复核：尚未入库的新链接，排队由流水线处理", now))
            added += 1
    con.commit()
    con.close()
    return closed, kept, added


def audit() -> list[dict]:
    from web.activity_service import list_activities, pick_triple_participate_targets

    queued = {row["dynamic_id"] for row in pick_triple_participate_targets(limit=400)}
    now = time.time()
    problems = []
    seen: set[str] = set()
    for status in ("未参加", "待审核"):
        page = 1
        while True:
            payload = list_activities(status=status, page=page)
            items = payload.get("items") or []
            for row in items:
                did = row["dynamic_id"]
                if did in seen:
                    continue
                seen.add(did)
                draw_at = row.get("lottery_time_unix")
                if draw_at is not None and draw_at <= now:
                    continue                      # 已开奖的不该参加
                if did in queued:
                    continue
                problems.append({
                    "dynamic_id": did,
                    "type": row.get("lottery_type"),
                    "status": status,
                    "draw": datetime.fromtimestamp(draw_at, TZ).strftime("%m-%d %H:%M") if draw_at else "未知",
                    "skipped": row.get("skipped"),
                    "skip_reason": str(row.get("skip_reason") or "")[:60],
                    "guard": (row.get("participation_guard") or {}).get("state"),
                    "block": str(row.get("participation_block_reason") or "")[:60],
                    "can": row.get("can_participate"),
                    "auto": row.get("auto_can_participate"),
                })
            if page >= int(payload.get("pages") or 1):
                break
            page += 1
    return problems


def main() -> int:
    ids = unknown_without_evidence()
    closed, kept, added = requeue_for_parse(ids)
    print(f"时间未知且无证据句: {len(ids)} 条 → 关掉假队列 {closed} 条（已入库、重读无收益）"
          f"｜本就非 pending {kept} 条｜真正待入库的新链接 {added} 条")
    problems = audit()
    print(f"归队审计：开奖时间在过去之后、却不在三连队列里的行 = {len(problems)} 条")
    for p in problems[:25]:
        print("  ", p["dynamic_id"], p["type"], "| 开奖", p["draw"], "| skipped", p["skipped"], p["skip_reason"],
              "| guard", p["guard"], p["block"])
    Path(ROOT / "bilibinggo" / "data" / "_review_audit.json").write_text(
        json.dumps(problems, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
