"""逐条打开「开奖时间未识别」的活动，把正文取回来读。

- 正文走项目自己的路径：dynamic/detail → opus/detail → opus 网页 __INITIAL_STATE__（`fetch_dynamic_content`）；
- 每条都跑一遍修好的字面抽取器，能自动定的先标出来；
- 剩下的一律把正文里含日期/抽奖词的句子打出来，交给人（我）逐条读中文判断；
- 断点续跑：结果累积在 data/_page_reads.json，已读过的不重读；
- 限流防 412：每条之间 sleep，遇到风控立即保存退出（退出码 3），下轮再继续。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bilibinggo"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover - 控制台不支持时忽略
    pass
DB = ROOT / "bilibinggo" / "data" / "binggo.db"
STATE = ROOT / "bilibinggo" / "data" / "_page_reads.json"
TZ = timezone(timedelta(hours=8))

DATE_RE = re.compile(
    r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*[日号]?|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,2}\.\d{1,2}"
    r"|(?:本月|这个月)?月底|下(?:周|星期|礼拜)[一二三四五六日天]|后天|明天|大后天|\d+\s*(?:天后|日后|周后|个月后)"
)
DRAW_RE = re.compile(r"开奖|开抽|抽取|抽出|揭晓|抽奖|揪|抽|roll|截止|截至", re.I)
RISK_MARKS = ("412", "拒绝了这个请求", "Precondition Failed")


def candidates() -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    out = []
    for row in con.execute(
        "select dynamic_id,lottery_type,activity_status,payload_json,enriched_at from activities "
        "where lottery_time is null and skipped=0 and activity_status in ('未参加','待审核') order by enriched_at desc"
    ):
        conditions = (json.loads(row["payload_json"] or "{}").get("conditions") or {})
        out.append({"dynamic_id": row["dynamic_id"], "type": row["lottery_type"],
                    "status": row["activity_status"],
                    "ref_ts": conditions.get("time_reference_ts") or row["enriched_at"]})
    con.close()
    return out


def sentences_with_dates(text: str) -> list[str]:
    out = []
    for chunk in re.split(r"[\n。；;！!？?]", text or ""):
        chunk = chunk.strip()
        if chunk and DATE_RE.search(chunk) and DRAW_RE.search(chunk):
            out.append(chunk[:180])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--sleep", type=float, default=2.5)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--recheck", action="store_true", help="只用已缓存的正文重跑抽取器，不访问 B 站")
    args = ap.parse_args()

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    pool = candidates()

    if args.recheck:
        from src.time_evidence import extract_literal_times
        by_id = {c["dynamic_id"]: c for c in pool}
        fixed = 0
        for did, rec in state.items():
            item = by_id.get(did)
            if item is None:
                continue
            extracted = extract_literal_times(rec.get("text") or "", reference_ts=item["ref_ts"]) or {}
            auto = extracted.get("lottery_time") if extracted.get("lottery_time_verified") else None
            if auto and auto != rec.get("auto_time"):
                print(f"新定出 {did} → {auto}（{extracted.get('lottery_time_precision')}）证据：{extracted.get('lottery_time_evidence')[:70]}")
            if auto is None and rec.get("auto_time"):
                print(f"撤回 {did} 旧结果 {rec['auto_time']}（现行抽取器不再认可这条证据）")
            if auto != rec.get("auto_time"):
                fixed += 1
            rec["auto_time"] = auto
            rec["auto_evidence"] = extracted.get("lottery_time_evidence") if auto else ""
            rec["auto_precision"] = extracted.get("lottery_time_precision") if auto else "unknown"
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        resolved = sum(1 for rec in state.values() if rec.get("auto_time"))
        print(f"重算完成：{len(state)} 条已读正文里自动定出 {resolved} 条（本轮新增 {fixed}）｜仍需人工读 {len(state) - resolved}")
        return 0

    todo = [c for c in pool if c["dynamic_id"] not in state]
    print(f"时间未知候选 {len(pool)} 条｜已读 {len(pool) - len(todo)}｜本轮待读 {min(len(todo), args.limit)}")

    if args.report or not todo:
        auto = [k for k, v in state.items() if v.get("auto_time")]
        print(f"已自动定出时间 {len(auto)} 条；其余需要人工读：")
        for c in pool:
            rec = state.get(c["dynamic_id"])
            if not rec or rec.get("auto_time"):
                continue
            print(f"-- {c['dynamic_id']} {c['type']} 正文 {rec.get('text_len')} 字")
            for line in (rec.get("hints") or [])[:4]:
                print("     ", line)
            if not (rec.get("hints") or []):
                print("      （没有含日期+抽奖词的句子，需要整段读）", (rec.get("text") or "")[:200].replace("\n", " / "))
        return 0

    from src.bilibili_client import BilibiliClient
    from src.forward_parser import fetch_dynamic_content
    from src.time_evidence import extract_literal_times

    done = 0
    with BilibiliClient() as client:
        for item in todo[: args.limit]:
            did = item["dynamic_id"]
            try:
                text = fetch_dynamic_content(client, did, try_html=True) or ""
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                risk = any(mark in message for mark in RISK_MARKS)
                print(f"[中断] {did} 读取失败{'（风控）' if risk else ''}：{message[:120]}")
                STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
                return 3 if risk else 2
            extracted = extract_literal_times(text, reference_ts=item["ref_ts"]) or {}
            auto_time = extracted.get("lottery_time") if extracted.get("lottery_time_verified") else None
            record = {
                "text_len": len(text),
                "text": text[:4000],
                "ref_ts": item["ref_ts"],
                "auto_time": auto_time,
                "auto_evidence": extracted.get("lottery_time_evidence") if auto_time else "",
                "hints": sentences_with_dates(text),
                "at": int(time.time()),
            }
            state[did] = record
            done += 1
            when = datetime.fromtimestamp(item["ref_ts"], TZ).strftime("%m-%d") if item["ref_ts"] else "-"
            print(f"{did} {item['type']}｜发布~{when}｜正文 {len(text)} 字｜自动={'✓ ' + str(auto_time) if auto_time else '✗'}")
            if not auto_time:
                for line in record["hints"][:3]:
                    print("      ·", line)
                if not record["hints"]:
                    print("      !", text[:180].replace("\n", " / ") or "（无正文）")
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            time.sleep(args.sleep)

    print(f"本轮读完 {done} 条，累计已读 {len(state)} 条 → {STATE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
