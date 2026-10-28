"""巡查 9 个数据源：谁的容器比断点新，就立刻按生产同款流水线补入库（带容器日期兜底）。

- 只做 check_update + run_new_links_pipeline + 推断点，全部走项目自己的函数，不碰参与接口；
- 与面板任务共用执行锁（面板 refresh 在跑时这里自动等待/跳过本轮）；
- 无新容器时零副作用，可被定时任务按小时反复调用。
"""
import sys
import json
import re
import time
import sqlite3
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "bilibinggo"))
ROOT = Path(__file__).resolve().parent

from src.bilibili_client import BilibiliClient  # noqa: E402
from src.pipeline.refresh_all_pipeline import run_new_links_pipeline  # noqa: E402
from src.sources import (  # noqa: E402
    ds1_xiaozhuli, ds2_fanqiao, ds3_gongjuren, ds4_junming, ds5_hudong,
    ds6_nuomi, ds7_dajinli, ds8_qianchoujun, ds9_jinlisongrui,
)
from src.sources.common import commit_source_checkpoint  # noqa: E402
from src.state_store import get_last_container, get_last_cv_id  # noqa: E402

MODULES = (ds1_xiaozhuli, ds2_fanqiao, ds3_gongjuren, ds4_junming, ds5_hudong,
           ds6_nuomi, ds7_dajinli, ds8_qianchoujun, ds9_jinlisongrui)
VIDEO_SOURCES = {"DS-1", "DS-8", "DS-9"}
# 巡查补入库每轮上限（条链接），其余留给下一轮。
# 10-28 三连改成每 10 分钟一格后从 25 降到 8：25 条实测要 9 分钟左右，而两格之间只有 10 分钟，
# 再扣掉「离下一格不足 3 分钟就让车」的预留，25 条的批次几乎必然被切在起点，等于巡查再也不入库。
# 8 条约 3 分钟，能在格点空隙里跑完；没跑完的留在检查点，下一轮或面板窗口继续。
PATROL_INGEST_LIMIT = 8
# 补入库是 @serialized_task，全程占着与面板共用的执行锁；离三连格点不足这个分钟数就收手，
# 把锁让给调度器（10-28 00:15 那格就是被巡查占到 :18 才撞成 fatal 的）。
INGEST_SLOT_RESERVE_MIN = 3.0


def _changed_container(client, mod) -> str | None:
    """比断点新则返回可展示的容器标识，否则 None。视频源比 URL，专栏源比 cv_id。"""
    source_id = mod.SOURCE_ID
    if source_id in VIDEO_SOURCES:
        latest = client.get_latest_video(mod.MID)
        url = mod.video_url(latest["bvid"])
        return url if url != (get_last_container(source_id) or "") else None
    latest = client.get_latest_article(mod.MID)
    cv = str(int(latest["id"]))
    prev_cv = str(get_last_cv_id(source_id) or "")
    if prev_cv:
        return None if cv == prev_cv else f"cv{cv}"
    prev = get_last_container(source_id) or ""
    expected = f"https://www.bilibili.com/read/cv{cv}"
    return None if expected == prev else f"cv{cv}"


API = "http://127.0.0.1:8787"


def _api(path: str, method: str = "GET") -> dict:
    req = urllib.request.Request(API + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:
        return {"_error": str(exc)[:120]}


def heal_scheduler() -> None:
    """巡查补入库可能横跨 15 分钟刻度，调度器会按设计判「撞车」并 fatal 停摆；
    巡查自己负责把它拉回来（start 会清 fatal），顺带报告。"""
    status = _api("/api/auto/status")
    state = status.get("state")
    if state == "fatal":
        print(f"调度器 fatal（{str(status.get('fatal_error'))[:60]}），自动重启调度…")
        restarted = _api("/api/auto/start", "POST")
        print("调度器恢复:", restarted.get("state"), "| 下一格:", (restarted.get("next_hint") or "")[:30])
    elif state not in ("running",):
        print("调度器状态异常:", state or status.get("_error"))


# 动作日志的可靠起点：10-18 13:01 起写过，但 10-21 深夜重登后跑到 10-22 03:00 的进程仍不写
# participation_actions（10-22 02:00~03:00 有 7 次参加只留了 participations），
# 第一条连续可靠记录是 10-22 03:01。用这个点做基线，避免把记账空窗误报成新问题。
ACTIONS_EPOCH = datetime(2026, 10, 22, 3, 1).timestamp()
PAUSE_RECHECK_AFTER_SEC = 1800


def _post_json(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:160]}"}
    except Exception as exc:
        return {"_error": str(exc)[:160]}


RISK_CODES = {-799, -799_000, 401, 403, 412, 419, 429, -429, -412}


def _control_dynamic_id() -> str:
    con = sqlite3.connect(ROOT / "bilibinggo" / "data" / "binggo.db")
    try:
        row = con.execute(
            "select dynamic_id from activities where draw_status='active' order by updated_at desc limit 1"
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        con.close()


def heal_account_gate(client: BilibiliClient) -> None:
    """412/429 这类风控会把账号闸按设计暂停（连参与都做不了）。暂停满 30 分钟后复查：
    只有仍然被风控（HTTP 抛错或返回风控码）才继续暂停；
    B 站正常应答了（哪怕是「动态不存在」这类内容码）就说明会话已恢复，release。"""
    guards = _api("/api/participation/guards")
    gate = (guards.get("items") or {}).get("") or {}
    if gate.get("state") != "account_paused":
        return
    reason = str(gate.get("reason") or "")
    con = sqlite3.connect(ROOT / "bilibinggo" / "data" / "binggo.db")
    row = con.execute("select updated_at from participation_guards where dynamic_id=''").fetchone()
    con.close()
    paused_for = time.time() - (row[0] if row else 0)
    if paused_for < PAUSE_RECHECK_AFTER_SEC:
        print(f"账号闸暂停中（已 {int(paused_for // 60)} 分钟，未到 30 分钟复查点），本轮不动")
        return
    match = re.search(r"for url '([^']+)'", reason)
    probes = []
    if match:
        url = match.group(1)
        base, _, query = url.partition("?")
        probes.append((base, dict(urllib.parse.parse_qsl(query))))
    control = _control_dynamic_id()
    if control:
        probes.append(("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", {"id": control}))
    if not probes:
        print("账号闸原因里没解析到可探测 URL，保持暂停：", reason[:80])
        return
    blocked = None
    reachable = False
    for base, params in probes:
        try:
            payload = client.request_json(base, params, referer="https://www.bilibili.com/", retries=0)
            code = payload.get("code")
        except Exception as exc:
            code = f"EXC:{type(exc).__name__}"
            blocked = code
            print(f"账号闸复查：探测 {base[:56]} → {code}（会话仍被拒）")
            continue
        print(f"账号闸复查：探测 id={params.get('id','')} → code={code}")
        if isinstance(code, int) and code in RISK_CODES:
            blocked = code
        else:
            reachable = True
    if blocked is not None and not reachable:
        print("账号闸仍在风控中，保持暂停")
        return
    if blocked is not None and reachable:
        print(f"注意：原 URL 仍回 {blocked}，但健康动态可正常读取 → 视为会话已恢复（那条链接本身可能已失效）")
    result = _post_json("/api/participation/guards/account/release",
                        {"evidence": f"巡查复查：只读探测 B 站已正常应答（原暂停原因 {reason[:60]}）"})
    print("账号闸 release:", "已恢复 ready" if result.get("ok") else result)


def report_ghost_joined() -> None:
    """只读报警：库里写「已参加」但没有任何参与动作留痕的行。
    10-22 03:01 之前是动作日志空窗（老进程不写），算遗留；之后再出现才需要人工查。
    守卫 progress 里有 succeeded 动作的行算「有实证」（那是当次参加的细节留痕），不报警。
    平台复核证明参与已生效的（预约在架 / 已转发）算「平台实证」——那种情况本来就不该再动手，
    没有动作留痕是正常的，不是幽灵（10-27 实测 1256747893749972999 预约恢复生效即属此类）。"""
    con = sqlite3.connect(ROOT / "bilibinggo" / "data" / "binggo.db")
    con.row_factory = sqlite3.Row
    joined = {r[0] for r in con.execute("select distinct dynamic_id from participation_actions where status='joined'")}
    rows = [dict(r) for r in con.execute(
        "select dynamic_id,lottery_time,platform_participated,reserve_reserved,payload_json "
        "from activities where activity_status='已参加'")]
    legacy = new = orphan = journaled = proven = 0
    now = time.time()
    unopened_new = []
    for item in rows:
        did = item["dynamic_id"]
        if did in joined:
            continue
        if item["platform_participated"] or item["reserve_reserved"]:
            proven += 1
            continue
        guard = con.execute("select progress_json from participation_guards where dynamic_id=?", (did,)).fetchone()
        if guard and '"succeeded"' in (guard["progress_json"] or ""):
            journaled += 1
            continue
        part = con.execute("select updated_at from participations where dynamic_id=?", (did,)).fetchone()
        if part is None:
            orphan += 1
        elif part["updated_at"] >= ACTIONS_EPOCH:
            new += 1
            if item["lottery_time"] and item["lottery_time"] > now:
                unopened_new.append(did)
        else:
            legacy += 1
    con.close()
    print(f"幽灵已参加体检：平台实证 {proven} 条｜守卫实证 {journaled} 条｜遗留 {legacy} 条（10-22 03:01 前）｜"
          f"新增 {new} 条｜无 participations {orphan} 条")
    if new or orphan:
        print(f"！！需要人工核查：新增 {new} 条 {unopened_new[:5]}｜无记录 {orphan} 条")


def report_duplicate_forwards() -> None:
    """按 UP 体检重复转发：同一 UP 转了几条属正常，出现「同一条链两个壳」才要停下来看。
    链根信息只有 10-26 之后入库的行才有，老行按自身 id 计，不猜。"""
    import re

    con = sqlite3.connect(ROOT / "bilibinggo" / "data" / "binggo.db")
    con.row_factory = sqlite3.Row
    joined = {str(r["dynamic_id"]) for r in con.execute("select distinct dynamic_id from participation_actions where status='joined'")}
    by_up: dict[int, list[str]] = {}
    chains: dict[str, str] = {}
    for row in con.execute("select dynamic_id,payload_json from activities"):
        did = str(row["dynamic_id"])
        if did not in joined:
            continue
        conditions = (json.loads(row["payload_json"] or "{}").get("conditions") or {})
        chain = [str(x) for x in (conditions.get("repost_chain_ids") or []) if str(x)]
        chains[did] = chain[-1] if chain else did
        g = con.execute("select progress_json from participation_guards where dynamic_id=?", (did,)).fetchone()
        detail = str(json.loads((g["progress_json"] if g else "{}") or "{}").get("follow", {}).get("detail") or "")
        match = re.search(r"uid=(\d{4,})", detail)
        if match:
            by_up.setdefault(int(match.group(1)), []).append(did)
    con.close()
    dup = [(uid, ids) for uid, ids in by_up.items() if len({chains[d] for d in ids}) < len(ids)]
    print(f"重复转发体检：已转发 UP {len(by_up)} 位｜同一 UP 多场抽奖 {sum(1 for ids in by_up.values() if len(ids) > 1)} 位"
          f"（正常，UP 只作归类线索）｜按链接判定同一条链换壳重复 {len(dup)} 位 {[u for u, _ in dup[:5]]}")


def _triple_minutes() -> tuple[int, ...]:
    """三连格点只认一个出处：面板的 auto_config。改节奏时这里跟着走，不再各写一份。"""
    try:
        from web.auto_config import TRIPLE_MINUTES

        return tuple(sorted(int(m) % 60 for m in TRIPLE_MINUTES)) or (0, 15, 30, 45)
    except Exception:
        return (0, 15, 30, 45)


def slot_minutes_text() -> str:
    return "/".join(f":{m:02d}" for m in _triple_minutes())


def _near_triple_slot(minute: int | None = None, minutes: int = 4) -> bool:
    """当前分钟是否落在三连格点前后 minutes 分钟内。"""
    m = time.localtime().tm_min if minute is None else int(minute)
    return any(min(abs(m - s), 60 - abs(m - s)) <= minutes for s in _triple_minutes())


def _minutes_to_next_slot() -> float:
    t = time.localtime()
    m = t.tm_min + t.tm_sec / 60.0
    return min((s - m) % 60 for s in _triple_minutes())


def gate_state() -> tuple[str, str]:
    items = (_api("/api/participation/guards").get("items") or {})
    gate = items.get("") or {}
    return str(gate.get("state") or ""), str(gate.get("reason") or "")


BREAKER_FILE = ROOT / "bilibinggo" / "data" / "_patrol_breaker.json"
BREAKER_TRIPS = 2
BREAKER_WINDOW_SEC = 6 * 3600
BREAKER_COOLDOWN_SEC = 6 * 3600


def _breaker_load() -> dict:
    try:
        data = json.loads(BREAKER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _breaker_events() -> list[int]:
    events = _breaker_load().get("events") or []
    now = int(time.time())
    return [int(e) for e in events if isinstance(e, (int, float)) and now - int(e) <= BREAKER_WINDOW_SEC]


def _breaker_record() -> None:
    """冷却时间是「粘性」的：写死一个 cooldown_until，不靠事件条数维持——
    否则事件滑出 6 小时窗口就会突然放行（06:10 那次 412 就是这么漏的）。"""
    events = _breaker_events() + [int(time.time())]
    trips = len([e for e in events if int(time.time()) - e <= BREAKER_WINDOW_SEC])
    cooldown_until = int(time.time()) + BREAKER_COOLDOWN_SEC if trips >= BREAKER_TRIPS else 0
    previous = int(_breaker_load().get("cooldown_until") or 0)
    BREAKER_FILE.write_text(json.dumps({
        "events": events[-12:],
        "cooldown_until": max(cooldown_until, previous if trips < BREAKER_TRIPS else 0),
    }), encoding="utf-8")


def _breaker_active() -> bool:
    if int(time.time()) < int(_breaker_load().get("cooldown_until") or 0):
        return True
    return len(_breaker_events()) >= BREAKER_TRIPS


def llm_available() -> tuple[bool, str]:
    """补入库必须要 LLM（classify + parse 都走它）；LLM 不可用时硬闯批量读只是白撞风控。
    直接问服务商（一次极短请求），不经过面板接口，免得顺带产生 B 站读。"""
    try:
        from src.llm_client import test_llm_connection
        from src.llm_settings import build_llm_config_from_inputs, load_llm_values

        saved = load_llm_values()
        config = build_llm_config_from_inputs(
            api_key=saved.get("LLM_API_KEY", "").strip(),
            base_url=(saved.get("LLM_BASE_URL") or "").strip(),
            model_name=(saved.get("LLM_MODEL_NAME") or "").strip(),
        )
        test_llm_connection(config)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:120]


def chain_key(row: dict) -> str:
    """三连与审计共用的链身份：链根优先，否则用自身动态 id。"""
    chain = [str(x) for x in (row.get("repost_chain_ids") or []) if str(x)]
    return chain[-1] if chain else str(row.get("dynamic_id") or "")


def report_queue_audit() -> None:
    """只读归队审计（10-26 起的长期规则）：开奖时间在现在之后的行必须在三连队列里；
    不在的就是漏，立刻报出来。参加前置检查仍会在真正参加前按 B 站实际状态再判一次。"""
    try:
        from web.activity_service import list_activities, pick_triple_participate_targets
    except Exception as exc:
        print(f"归队审计跳过：{type(exc).__name__}")
        return
    targets = pick_triple_participate_targets(limit=400)
    queued = {row["dynamic_id"] for row in targets}
    # 三连按「同一条转发链一轮只取一条」去重（10-26 修重复转发时加的），钥匙与取队列那边一致：
    # 有链根按链根，没有就按自身 id。同链的其他行本轮不在队列里不是漏，下一轮补上或按「已转发」挡掉。
    queued_keys = {chain_key(row) for row in targets}
    now = time.time()
    missing = []
    seen = set()
    for status in ("未参加", "待审核"):
        page = 1
        while True:
            payload = list_activities(status=status, page=page)
            for row in payload.get("items") or []:
                did = row["dynamic_id"]
                if did in seen:
                    continue
                seen.add(did)
                draw_at = row.get("lottery_time_unix")
                if draw_at is not None and draw_at <= now:
                    continue
                if did in queued:
                    continue
                if chain_key(row) in queued_keys:
                    continue
                missing.append((did, str(row.get("participation_block_reason") or row.get("skip_reason") or "")[:50]))
            if page >= int(payload.get("pages") or 1):
                break
            page += 1
    print(f"归队审计：队列 {len(queued)} 条｜开奖未过期却不在队列 {len(missing)} 条"
          + (f" → {missing[:5]}" if missing else "（合格）"))


def main() -> int:
    touched = []
    deferred = []
    state, reason = gate_state()
    with BilibiliClient() as client:
        if state != "ready":
            # 风控窗口里再批量读同一批接口只会反复触发 412，把闸门一次次按停；
            # 这轮只按节流做恢复复查，补入库让给下一个整点。
            print(f"账号闸当前是 {state}（{reason[:70]}），本轮跳过补入库，只做恢复复查")
            heal_account_gate(client)
            heal_scheduler()
            report_ghost_joined()
            report_duplicate_forwards()
            report_queue_audit()
            return 0
        if _breaker_active():
            print("412 熔断中（近期反复被按停账号闸），本轮跳过补入库，只做恢复复查")
            heal_account_gate(client)
            heal_scheduler()
            report_ghost_joined()
            report_duplicate_forwards()
            report_queue_audit()
            return 0
        # 临近格点只影响「能不能占执行锁补入库」，不影响查源：查源是几个列表读，
        # 整轮跳过会让人误以为「9 个源均与断点一致」（15:11 那轮就是这样白丢一次可见性）。
        slot_close = _near_triple_slot(minutes=2)
        if slot_close:
            print(f"临近三连格点（{slot_minutes_text()}）：本轮照常查源，但只报不补入库，新容器留给下一轮/面板窗口")
        llm_ok, llm_message = llm_available()
        if not llm_ok:
            print(f"LLM 不可用（{llm_message}），补入库跑不完，本轮跳过批量读，只做恢复复查")
            heal_account_gate(client)
            heal_scheduler()
            report_ghost_joined()
            report_duplicate_forwards()
            report_queue_audit()
            return 0
        for mod in MODULES:
            source_id = mod.SOURCE_ID
            try:
                marker = _changed_container(client, mod)
            except Exception as exc:
                print(f"[{source_id}] 取最新容器失败：{str(exc)[:100]}")
                continue
            if not marker:
                continue
            if slot_close:
                # 补入库带 @serialized_task，会一路占着与面板共用的执行锁；00:15 那格就是这么撞成 fatal 的。
                print(f"[{source_id}] 发现新容器 {marker}，但临近三连格点，本轮不补入库（留给下一轮/面板窗口）")
                deferred.append(f"{source_id} {marker}")
                continue
            print(f"[{source_id}] 发现新容器 {marker}，开始补入库…")
            if _minutes_to_next_slot() < INGEST_SLOT_RESERVE_MIN:
                print(f"[{source_id}] 距三连格点只剩 {_minutes_to_next_slot():.1f} 分钟，"
                      f"本轮不再开新批次（断点未推进，下轮继续）")
                deferred.append(f"{source_id} {marker}")
                break
            slot_yield = threading.Event()

            def _watch_slot(done, count, message):
                # 流水线只在条目/批次边界查这个标志，所以最迟下一批就停，不会硬掐在途请求。
                if _minutes_to_next_slot() < INGEST_SLOT_RESERVE_MIN:
                    slot_yield.set()

            try:
                result = mod.check_update(force=True)
                mod.save_result(result)
                hints = {
                    did: {**hint, "container_url": result.container_url, "reference_ts": result.published_at}
                    for did, hint in (result.link_time_hints or {}).items()
                }
                # 10-27 02:10 那轮用默认 20×4 并发补 43 条，十几分钟后账号闸被 412 按停；
                # 巡查是后台补漏，不赶时间，改成小批量低并发。没跑完的链接留在检查点里，
                # 由下一个刷新窗口继续（流水线是 enqueue+pending 制，limit/分批都不会丢数据）。
                pipeline = run_new_links_pipeline(
                    result.activity_links,
                    link_time_hints=hints,
                    batch_size=6,
                    workers=2,
                    # 10-27 实测：02:10 那轮一次补 43 条，十几分钟后账号闸被 412 按停、连累一个三连槽位。
                    # 每轮最多 25 条；没跑完的留在检查点里（pending − 已知），下一轮自动继续，不丢数据。
                    limit=PATROL_INGEST_LIMIT,
                    cancel_event=slot_yield,
                    on_progress=_watch_slot,
                )
                commit_source_checkpoint(result)
                touched.append(source_id)
                print(f"[{source_id}] {pipeline.message}｜容器日期证据 {len(hints)} 条")
            except Exception:
                if slot_yield.is_set():
                    deferred.append(f"{source_id} {marker}")
                    print(f"[{source_id}] 让车：距三连格点不足 {INGEST_SLOT_RESERVE_MIN:.0f} 分钟，"
                          f"已入库的保留、未跑完的留在检查点，本轮到此为止")
                    break
                print(f"[{source_id}] 补入库失败（断点未推进，下轮重试）：")
                traceback.print_exc(limit=3)
                time.sleep(2)
        if gate_state()[0] != "ready":
            _breaker_record()
            print("本轮把账号闸按停了：已记一次 412 事件（6 小时内 ≥2 次就熔断补入库 3 小时，只留探活）")
        heal_account_gate(client)
    heal_scheduler()
    report_ghost_joined()
    report_duplicate_forwards()
    report_queue_audit()
    if not touched and not deferred:
        print(f"巡查完成：9 个源均与断点一致，无需补入库（{time.strftime('%m-%d %H:%M')}）")
    if deferred:
        print(f"让车台账：{len(deferred)} 个源的容器留到下轮（断点未推进，不会丢）：{'; '.join(deferred)}"
              f"（{time.strftime('%m-%d %H:%M')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
