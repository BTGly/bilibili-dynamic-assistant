"""定时点击调度器：只向 JobRunner 投递意图，撞车即停，绝不 cancel。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from src.app_logging import get_logger
from src.participation_guard import get_guard
from web.auto_config import (
    ACTION_LABELS,
    ALLOWED_CLICK_ACTIONS,
    JOB_POLL_INTERVAL_SEC,
    JOB_POLL_TIMEOUT_SEC,
    REFRESH_HOURS,
    TRIPLE_MINUTES,
)
from web.event_hub import event_hub
from web.job_runner import JobRunner, runner
from web.user_messages import friendly_error

logger = get_logger("auto")
# 固定 UTC+8，与 lottery_time / forward_parser 一致；避免 Windows 打包缺 tzdata 时启动失败
CN_TZ = timezone(timedelta(hours=8))
SchedulerState = Literal["idle", "running", "stopped", "fatal"]
STATE_LABELS = {
    "idle": "尚未启动",
    "running": "调度运行中",
    "stopped": "已停止",
    "fatal": "已停机",
}
REFRESH_STEPS = (
    {"action": "refresh_all", "label": "一键更新活动链接"},
    {"action": "refresh_watch", "label": "更新监控用户动态"},
    {"action": "refresh_status", "label": "刷新任务状态"},
)
_AUTO_SNAPSHOT_MIN_INTERVAL_SEC = 0.5
_AUTO_SNAPSHOT_LOG_LIMIT = 30
# 一轮源检查没真正成功时，隔这么久再试一次（登录恢复后一小时内自动接上，不用人点）
REFRESH_RETRY_GAP = timedelta(hours=1)
# B 站风控类失败（412「拒绝了这个请求」）后，整批刷新退避这么久：
# 批次要读几十条动态详情，正是风控计数的那类流量，按小时重试只会把账号闸反复按停。
# 10-26 实测：17:17 探活刚 code=0，17:2x 一轮批量读就再次 412，说明这个会话的冷却
# 以「小时」计；3 小时退避会让窗口每晚再撞一次，故放宽到 6 小时。
RISK_REFRESH_BACKOFF = timedelta(hours=6)
_RISK_MARKERS = ("拒绝了这个请求", "平台校验或风控", "Precondition Failed", "412")


def _is_risk_control_text(text: str) -> bool:
    return any(marker in text for marker in _RISK_MARKERS)


def refresh_window_done_keys(now: datetime) -> set[str]:
    """今天已经「成功跑过刷新批次」的窗口 key，读持久化的 jobs 表。

    10-27 实测：`_done_refresh` 只在内存里，于是我每重启一次面板就算「本窗口还没查过源」，
    10:29/10:42/10:43/10:49/10:50 连着补跑了 5 轮全量刷新（每轮 refresh_status 要过 575 条），
    爬取量成倍翻。窗口归属按批次实际开始时间落到最近的 REFRESH_HOURS 整点。
    """
    from sqlmodel import select

    from src.db.models import JobRow
    from src.db.session import session_scope

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as session:
        starts = session.exec(
            select(JobRow.started_at).where(
                JobRow.action == "refresh_all",
                JobRow.state == "success",
                JobRow.started_at >= int(day_start.timestamp()),
            )
        ).all()
    keys: set[str] = set()
    for ts in starts:
        if not ts:
            continue
        moment = datetime.fromtimestamp(int(ts), CN_TZ)
        slots = sorted(moment.replace(hour=h, minute=0, second=0, microsecond=0) for h in REFRESH_HOURS)
        active = [slot for slot in slots if slot <= moment]
        if active:
            keys.add(f"{active[-1]:%Y-%m-%d-%H}")
    return keys


def recent_risk_refresh_failure(now: datetime | None = None) -> bool:
    """读持久化的 jobs 表判断「最近 3 小时内有风控类刷新失败」——
    fatal 后重启、巡查自愈都绕不过它（内存里的 last_try 会被重启清掉）。"""
    from sqlmodel import select

    from src.db.models import JobRow
    from src.db.session import session_scope

    current = now or datetime.now(CN_TZ)
    since = int((current - RISK_REFRESH_BACKOFF).timestamp())
    with session_scope() as session:
        rows = session.exec(
            select(JobRow.state, JobRow.message)
            .where(JobRow.action == "refresh_all", JobRow.created_at >= since)
            .order_by(JobRow.id.desc())
            .limit(5)
        ).all()
    return any(
        state == "error" and _is_risk_control_text(message or "")
        for state, message in rows
    )


class CollisionError(RuntimeError):
    """抽奖端已有任务在跑，自动调度必须立即停机。"""


@dataclass
class LogEntry:
    ts: str
    level: str
    message: str


@dataclass
class SchedulerStatus:
    state: SchedulerState = "idle"
    message: str = "尚未启动"
    started_at: str | None = None
    stopped_at: str | None = None
    fatal_error: str | None = None
    last_tick_at: str | None = None
    current_phase: str = ""
    next_hint: str = ""
    last_click: dict[str, Any] | None = None
    refresh_batch_key: str | None = None
    triple_slot_key: str | None = None
    refresh_pipeline: dict[str, Any] = field(default_factory=dict)
    next_slot: dict[str, Any] | None = None
    job_probe: dict[str, Any] | None = None
    server_now: str = ""
    server_now_unix: int = 0
    logs: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, self.state),
            "message": self.message,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "fatal_error": self.fatal_error,
            "last_tick_at": self.last_tick_at,
            "current_phase": self.current_phase,
            "next_hint": self.next_hint,
            "next_slot": self.next_slot,
            "last_click": self.last_click,
            "refresh_batch_key": self.refresh_batch_key,
            "triple_slot_key": self.triple_slot_key,
            "refresh_pipeline": self.refresh_pipeline or _idle_pipeline(),
            "job_probe": self.job_probe,
            "server_now": self.server_now,
            "server_now_unix": self.server_now_unix,
            "logs": list(self.logs),
            "schedule": {
                "refresh_hours": sorted(REFRESH_HOURS),
                "triple_minutes": sorted(TRIPLE_MINUTES),
                "actions": [
                    {"action": key, "label": ACTION_LABELS[key]}
                    for key in ("refresh_all", "refresh_watch", "refresh_status", "participate_triple")
                ],
            },
        }


class AutoScheduler:
    def __init__(self, job_runner: JobRunner | None = None) -> None:
        self._runner = job_runner or runner
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs: deque[LogEntry] = deque(maxlen=200)
        self._status = SchedulerStatus(refresh_pipeline=_idle_pipeline())
        self._done_refresh: set[str] = set()
        self._refresh_last_try: dict[str, datetime] = {}
        self._refresh_risk_abandoned: set[str] = set()
        self._done_triple: set[str] = set()
        self._snapshot_timer: threading.Timer | None = None
        self._last_snapshot_mono = 0.0
        self._snapshot_pending = False

    def get_status(self) -> dict[str, Any]:
        now = datetime.now(CN_TZ)
        slot = _next_slot(now, refresh_at=self._refresh_pending(now))
        probe = _probe_job(self._runner)
        with self._lock:
            self._status.logs = [
                {"ts": e.ts, "level": e.level, "message": e.message} for e in list(self._logs)[-80:]
            ]
            self._status.next_slot = slot
            self._status.next_hint = slot.get("hint") or ""
            self._status.job_probe = probe
            self._status.server_now = _now_iso()
            self._status.server_now_unix = int(now.timestamp())
            if not self._status.refresh_pipeline:
                self._status.refresh_pipeline = _idle_pipeline()
            return self._status.to_dict()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("调度器已在运行" if self._status.state == "running" else "调度器正在停止，请稍后重新启动")
            if self._status.state == "fatal":
                self._status.fatal_error = None
            self._stop_event = threading.Event()
            self._status.state = "running"
            self._status.message = "调度器运行中"
            self._status.started_at = _now_iso()
            self._status.stopped_at = None
            self._status.fatal_error = None
            self._status.current_phase = "等待下一刻度"
            self._status.refresh_pipeline = _idle_pipeline()
            self._thread = threading.Thread(target=self._loop, name="binggo-auto-scheduler", daemon=True)
            self._thread.start()
        self._log("info", "调度器已启动（仅点击 4 个按钮，不干涉抽奖程序其它功能）")
        self._schedule_auto_snapshot(force=True)
        return self.get_status()

    def stop(self, *, reason: str = "用户停止") -> dict[str, Any]:
        """只停止本调度器，绝不取消抽奖端任务。"""
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            if self._status.state == "running":
                self._status.state = "stopped"
                self._status.message = reason
                self._status.stopped_at = _now_iso()
                self._status.current_phase = ""
                self._status.refresh_pipeline = _idle_pipeline()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._log("warn", f"调度器已停止：{reason}")
        self._schedule_auto_snapshot(force=True)
        return self.get_status()

    def _fatal(self, message: str) -> None:
        self._stop_event.set()
        with self._lock:
            self._status.state = "fatal"
            self._status.message = "因任务撞车或严重错误已停机"
            self._status.fatal_error = message
            self._status.stopped_at = _now_iso()
            self._status.current_phase = "已停机"
            self._status.refresh_pipeline = {
                **(self._status.refresh_pipeline or _idle_pipeline()),
                "active": False,
            }
        self._log("error", f"致命停机：{message}")
        self._schedule_auto_snapshot(force=True)

    def _log(self, level: str, message: str) -> None:
        entry = LogEntry(ts=_now_iso(), level=level, message=message)
        with self._lock:
            self._logs.append(entry)
        self._publish_auto_log(entry)

    def _set_phase(self, phase: str, message: str | None = None) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            prev = (
                self._status.current_phase,
                self._status.message,
                (self._status.next_slot or {}).get("due_at"),
            )
            self._status.current_phase = phase
            if message is not None:
                self._status.message = message
            phase_now = datetime.now(CN_TZ)
            slot = _next_slot(phase_now, refresh_at=self._refresh_pending(phase_now))
            self._status.next_slot = slot
            self._status.next_hint = slot.get("hint") or ""
            changed = prev != (
                self._status.current_phase,
                self._status.message,
                (self._status.next_slot or {}).get("due_at"),
            )
        if changed:
            self._schedule_auto_snapshot(force=False)

    def _publish_auto_log(self, entry: LogEntry) -> None:
        try:
            event_hub.publish(
                "auto.log",
                {
                    "level": entry.level,
                    "message": entry.message,
                    "log_ts": entry.ts,
                },
            )
        except Exception:
            logger.exception("发布 auto.log 失败")

    def _schedule_auto_snapshot(self, *, force: bool = False) -> None:
        """变更合并推送；fatal/stopped/启停 force 立即发。"""
        with self._lock:
            if force:
                if self._snapshot_timer is not None:
                    self._snapshot_timer.cancel()
                    self._snapshot_timer = None
                self._snapshot_pending = False
                should_emit_now = True
            else:
                now = time.monotonic()
                elapsed = now - self._last_snapshot_mono
                if elapsed >= _AUTO_SNAPSHOT_MIN_INTERVAL_SEC:
                    should_emit_now = True
                    if self._snapshot_timer is not None:
                        self._snapshot_timer.cancel()
                        self._snapshot_timer = None
                    self._snapshot_pending = False
                else:
                    should_emit_now = False
                    self._snapshot_pending = True
                    if self._snapshot_timer is None:
                        delay = max(0.05, _AUTO_SNAPSHOT_MIN_INTERVAL_SEC - elapsed)

                        def _fire() -> None:
                            with self._lock:
                                self._snapshot_timer = None
                                if not self._snapshot_pending:
                                    return
                                self._snapshot_pending = False
                            self._emit_auto_snapshot()

                        self._snapshot_timer = threading.Timer(delay, _fire)
                        self._snapshot_timer.daemon = True
                        self._snapshot_timer.start()
        if should_emit_now:
            self._emit_auto_snapshot()

    def _emit_auto_snapshot(self) -> None:
        try:
            payload = self.get_status()
            logs = payload.get("logs")
            if isinstance(logs, list) and len(logs) > _AUTO_SNAPSHOT_LOG_LIMIT:
                payload = dict(payload)
                payload["logs"] = logs[-_AUTO_SNAPSHOT_LOG_LIMIT:]
            event_hub.publish("auto.snapshot", payload)
            with self._lock:
                self._last_snapshot_mono = time.monotonic()
        except Exception:
            logger.exception("发布 auto.snapshot 失败")

    def _set_pipeline(self, *, active: bool, step_index: int = -1, waiting: bool = False) -> None:
        if self._stop_event.is_set():
            return
        steps = []
        for i, item in enumerate(REFRESH_STEPS):
            if not active or step_index < 0:
                status = "pending"
            elif i < step_index:
                status = "done"
            elif i == step_index:
                status = "waiting" if waiting else "active"
            else:
                status = "pending"
            steps.append({**item, "status": status, "index": i})
        pipeline = {
            "active": active,
            "step_index": step_index,
            "waiting": waiting,
            "steps": steps,
        }
        with self._lock:
            prev = self._status.refresh_pipeline
            if (
                prev
                and prev.get("active") == pipeline["active"]
                and prev.get("step_index") == pipeline["step_index"]
                and prev.get("waiting") == pipeline["waiting"]
            ):
                return
            self._status.refresh_pipeline = pipeline
        self._schedule_auto_snapshot(force=False)

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                now = datetime.now(CN_TZ)
                with self._lock:
                    self._status.last_tick_at = _now_iso()
                    slot = _next_slot(now, refresh_at=self._refresh_next(now))
                    self._status.next_slot = slot
                    self._status.next_hint = slot.get("hint") or ""

                due_refresh_key = self._due_refresh_key(now)
                if due_refresh_key:
                    self._refresh_last_try[due_refresh_key] = now
                    self._done_refresh.add(due_refresh_key)
                    if not self._run_refresh_batch(due_refresh_key):
                        self._done_refresh.discard(due_refresh_key)
                    continue

                if now.minute in TRIPLE_MINUTES:
                    # 刷新批次优先且会把循环占住，所以 9:00 那一分钟必然被批次吃掉；
                    # 9:30 只有在批次还没跑完时才会错过，不存在三连和批次同时点按钮。
                    key = f"{now:%Y-%m-%d-%H-%M}"
                    if key not in self._done_triple:
                        self._run_triple_slot(key)
                        continue

                if self._status.state == "running":
                    self._set_phase("等待下一刻度", "调度器运行中")
                self._stop_event.wait(1.0)
        except CollisionError as exc:
            if self._stop_event.is_set():
                return
            self._fatal(f"任务撞车：{exc}")
        except Exception as exc:
            if self._stop_event.is_set():
                return
            logger.exception("定时调度未预期错误")
            self._fatal(f"未预期错误：{friendly_error(exc)}")

    def _refresh_slot_time(self, now: datetime) -> datetime | None:
        """当前所属的刷新窗口（今天最近一个已到的 REFRESH_HOURS 整点）；还没到第一个就 None。"""
        slots = sorted(
            now.replace(hour=hour, minute=0, second=0, microsecond=0) for hour in REFRESH_HOURS
        )
        active = [slot for slot in slots if slot <= now]
        return active[-1] if active else None

    def _refresh_next_slot_time(self, now: datetime) -> datetime | None:
        slots = sorted(
            now.replace(hour=hour, minute=0, second=0, microsecond=0) for hour in REFRESH_HOURS
        )
        later = [slot for slot in slots if slot > now]
        return later[0] if later else None

    def _due_refresh_key(self, now: datetime) -> str | None:
        """每个刷新窗口最多成功一次；失败/账号暂停时按 1 小时间隔在本窗口内补跑。"""
        slot = self._refresh_slot_time(now)
        if slot is None:
            return None
        if recent_risk_refresh_failure(now):
            # 风控窗口里整批读详情只会把账号闸反复按停，等退避过去再说
            return None
        key = f"{slot:%Y-%m-%d-%H}"
        if key in self._refresh_risk_abandoned:
            # 本窗口已经因风控失败过一次：同一窗口反复补跑只会反复批量读、反复按停账号闸，
            # 所以放弃这个窗口，交给下一个正常窗口（09→15→21）再试。
            return None
        if key in self._done_refresh:
            return None
        if key in refresh_window_done_keys(now):
            # 内存标记被重启清了，但库里记着这个窗口已经成功跑过：绝不再补一轮批量读
            self._done_refresh.add(key)
            return None
        last_try = self._refresh_last_try.get(key)
        if last_try is not None and now - last_try < REFRESH_RETRY_GAP:
            return None
        return key

    def _refresh_next(self, now: datetime) -> datetime | None:
        """下一个刷新窗口/补跑时间；今天的窗口都完成了就返回 None。"""
        if self._due_refresh_key(now) is not None:
            return now
        slot = self._refresh_slot_time(now)
        if slot is not None:
            key = f"{slot:%Y-%m-%d-%H}"
            last_try = self._refresh_last_try.get(key)
            if key not in self._done_refresh and last_try is not None:
                return last_try + REFRESH_RETRY_GAP
        return self._refresh_next_slot_time(now)

    def _refresh_due(self, now: datetime) -> bool:
        return self._due_refresh_key(now) is not None

    def _refresh_pending(self, now: datetime) -> datetime | None:
        """给面板预览用的下一次尝试时间；调度器没在跑或今天已刷好就返回 None。"""
        if self._status.state != "running":
            return None
        return self._refresh_next(now)

    def _account_paused(self) -> bool:
        return get_guard("").get("state") == "account_paused"

    def _run_refresh_batch(self, key: str) -> bool:
        """跑一轮刷新批次，返回「今天这波源检查是否真的成功」。"""
        if self._account_paused():
            self._log("warn", f"账号未登录/已暂停，跳过刷新批次 {key}（一个请求都不发）；登录恢复后一小时内自动补跑")
            self._set_phase("等待下一刻度", "账号未登录，刷新批次已跳过")
            return False
        self._set_phase("刷新批次", f"开始刷新批次 {key}")
        self._log("info", f"刷新批次开始 {key}：一键更新 → 监控动态 → 刷新状态")
        self._set_pipeline(active=True, step_index=0, waiting=False)
        actions = ("refresh_all", "refresh_watch", "refresh_status")
        failed: list[str] = []
        try:
            for index, action in enumerate(actions):
                if self._stop_event.is_set():
                    self._set_pipeline(active=False)
                    return False
                self._set_pipeline(active=True, step_index=index, waiting=False)
                try:
                    self._click_and_wait(action, pipeline_index=index)
                except CollisionError:
                    raise
                except Exception as exc:
                    if _is_hard_failure(exc):
                        if _is_risk_control_text(str(exc)):
                            self._refresh_risk_abandoned.add(key)
                        raise
                    if _is_risk_control_text(str(exc)):
                        self._refresh_risk_abandoned.add(key)
                        self._log("warn", f"刷新批次 {key} 遇 B 站风控，本窗口不再补跑（避免反复批量读把账号闸按停），交给下一个窗口")
                    failed.append(ACTION_LABELS.get(action, action))
                    self._log("warn", f"「{ACTION_LABELS.get(action, action)}」业务结束：{exc}，继续下一项")
            if self._stop_event.is_set():
                return False
            with self._lock:
                self._status.refresh_batch_key = key
            self._set_pipeline(active=False)
            if failed:
                self._log("warn", f"刷新批次 {key} 未全部成功：{'、'.join(failed)} 失败，一小时后重试")
                self._set_phase("等待下一刻度", "刷新批次有失败，稍后重试")
                return False
            self._log("info", f"刷新批次完成 {key}")
            self._set_phase("等待下一刻度", "刷新批次已完成")
            return True
        except CollisionError:
            raise
        except Exception as exc:
            if self._stop_event.is_set():
                return False
            if _is_hard_failure(exc):
                self._fatal(str(exc))
                return False
            self._log("error", f"刷新批次中断 {key}：{exc}")
            self._set_pipeline(active=False)
            self._set_phase("等待下一刻度", f"刷新批次异常已跳过：{exc}")
            return False

    def _run_triple_slot(self, key: str) -> None:
        self._set_pipeline(active=False)
        self._set_phase("三连参与", f"触发三连参与 {key}")
        self._log("info", f"三连参与刻度 {key}")
        try:
            outcome = self._click_and_wait("participate_triple")
            if self._stop_event.is_set():
                return
            self._done_triple.add(key)
            with self._lock:
                self._status.triple_slot_key = key
            if outcome and outcome.get("skipped"):
                msg = str(outcome.get("message") or "当前没有可参与活动，已跳过")
                self._log("info", f"三连参与已跳过：{msg}")
                self._set_phase("等待下一刻度", msg)
            else:
                self._set_phase("等待下一刻度", "三连参与已完成")
        except CollisionError:
            raise
        except Exception as exc:
            if self._stop_event.is_set():
                return
            if _is_hard_failure(exc):
                self._fatal(str(exc))
                return
            self._log("info", f"三连参与已跳过：{exc}")
            self._done_triple.add(key)
            self._set_phase("等待下一刻度", f"已跳过：{exc}")

    def _click_and_wait(self, action: str, *, pipeline_index: int | None = None) -> dict[str, Any]:
        if action not in ALLOWED_CLICK_ACTIONS:
            raise ValueError(f"禁止的操作：{action}")

        label = ACTION_LABELS.get(action, action)
        self._set_phase(f"点击：{label}", f"正在点击「{label}」")
        self._log("info", f"点击按钮：{label} ({action})")

        if self._runner.is_running():
            current = self._runner.get_status().to_dict()
            raise CollisionError(
                f"准备点击「{label}」时发现抽奖端仍有任务在运行"
                f"（action={current.get('action')}, message={current.get('message')}）"
            )

        params = {"from_auto": True} if action == "participate_triple" else {}
        job_id = self._runner.try_start(action, params, source="auto")
        if job_id is None:
            raise CollisionError(f"点击「{label}」失败：已有任务正在运行")

        with self._lock:
            self._status.last_click = {
                "action": action,
                "label": label,
                "at": _now_iso(),
                "response_ok": True,
                "job_id": job_id,
            }
        self._schedule_auto_snapshot(force=False)

        if pipeline_index is not None:
            self._set_pipeline(active=True, step_index=pipeline_index, waiting=True)

        final = self._wait_until_terminal(job_id, label)
        if self._stop_event.is_set():
            return {"stopped": True, "job": final}
        state = str(final.get("state") or "")
        msg = str(final.get("message") or "")
        result = final.get("result") if isinstance(final.get("result"), dict) else {}
        if result.get("skipped") or (
            action == "participate_triple" and state == "error" and _is_triple_empty_skip(msg)
        ):
            return {"skipped": True, "message": msg, "job": final}
        if state == "cancelled":
            raise RuntimeError(msg or f"「{label}」已取消")
        if state in {"error", "interrupted"}:
            raise RuntimeError(msg or f"「{label}」以 {state} 结束")
        if state != "success":
            raise RuntimeError(msg or f"「{label}」异常结束：state={state}")
        return {"skipped": False, "message": msg, "job": final}

    def _wait_until_terminal(self, job_id: int, label: str) -> dict[str, Any]:
        """按 job id 只读轮询至终态。绝不 cancel。"""
        deadline = time.monotonic() + JOB_POLL_TIMEOUT_SEC
        self._set_phase(f"等待结束：{label}", f"已点击「{label}」，等待抽奖端自行结束…")
        self._stop_event.wait(0.8)
        last: dict[str, Any] = {}
        while not self._stop_event.is_set():
            if time.monotonic() > deadline:
                raise RuntimeError(f"等待「{label}」超时（超过 {int(JOB_POLL_TIMEOUT_SEC)} 秒）")
            job = self._runner.resolve_job_status(job_id).to_dict()
            last = job
            state = str(job.get("state") or "")
            if state != "running":
                msg = str(job.get("message") or state or "done")
                self._log("info", f"「{label}」已结束：state={state} · {msg}")
                return job
            detail = str(job.get("progress_message") or job.get("message") or "")
            if detail:
                self._set_phase(f"等待结束：{label}", detail)
            self._stop_event.wait(JOB_POLL_INTERVAL_SEC)
        return last


def _probe_job(job_runner: JobRunner) -> dict[str, Any]:
    job = job_runner.get_status().to_dict()
    state = str(job.get("state") or "idle")
    return {
        "ok": True,
        "reachable": True,
        "job_state": state,
        "job_id": job.get("id"),
        "job_action": str(job.get("action") or ""),
        "job_message": str(job.get("message") or ""),
        "job_label": str(job.get("label") or ""),
        "checked_at": _now_iso(),
    }


def _idle_pipeline() -> dict[str, Any]:
    return {
        "active": False,
        "step_index": -1,
        "waiting": False,
        "steps": [{**item, "status": "pending", "index": i} for i, item in enumerate(REFRESH_STEPS)],
    }


def _is_triple_empty_skip(message: str) -> bool:
    text = str(message or "")
    markers = ("没有可参与", "当前列表没有", "无可参与", "已跳过")
    return any(marker in text for marker in markers)


def _is_hard_failure(exc: BaseException) -> bool:
    if isinstance(exc, CollisionError):
        return True
    if _is_triple_empty_skip(str(exc)):
        return False
    text = str(exc)
    soft_markers = ("没有可参与", "当前列表没有", "无可参与", "已跳过")
    if any(m in text for m in soft_markers):
        return False
    hard_markers = (
        "连接",
        "timeout",
        "Timeout",
        "ConnectError",
        "等待「",
        "HTTP 5",
        "扫码登录",
        "LLM",
        "Cookie",
        "401",
    )
    return any(m in text for m in hard_markers)


def _now_iso() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _refresh_slot(candidate: datetime, *, hint: str) -> dict[str, Any]:
    return {
        "kind": "refresh",
        "label": "刷新批次",
        "action": "refresh_all",
        "action_label": ACTION_LABELS["refresh_all"],
        "actions": [
            {"action": item["action"], "label": item["label"]}
            for item in REFRESH_STEPS
        ],
        "at": candidate.strftime("%Y-%m-%d %H:%M:%S"),
        "at_unix": int(candidate.timestamp()),
        "hour": candidate.hour,
        "minute": candidate.minute,
        "hint": hint,
    }


def _scan_slot(now: datetime) -> dict[str, Any]:
    """按固定刻度找下一个要点击的刻度（三连 / 明天起的刷新整点）。"""
    for offset_min in range(1, 24 * 60 + 1):
        candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=offset_min)
        h, m = candidate.hour, candidate.minute
        if h in REFRESH_HOURS and m == 0:
            return _refresh_slot(candidate, hint=f"下次刷新批次约 {h:02d}:00（一键更新→监控→状态）")
        if m in TRIPLE_MINUTES:
            return {
                "kind": "triple",
                "label": "三连参与",
                "action": "participate_triple",
                "action_label": ACTION_LABELS["participate_triple"],
                "actions": [
                    {"action": "participate_triple", "label": ACTION_LABELS["participate_triple"]}
                ],
                "at": candidate.strftime("%Y-%m-%d %H:%M:%S"),
                "at_unix": int(candidate.timestamp()),
                "hour": h,
                "minute": m,
                "hint": f"下次三连参与约 {h:02d}:{m:02d}",
            }
    return {
        "kind": "none",
        "label": "暂无",
        "action": None,
        "action_label": "暂无",
        "actions": [],
        "at": None,
        "at_unix": None,
        "hint": "暂无下一刻度",
    }


def _next_slot(now: datetime, *, refresh_at: datetime | None = None) -> dict[str, Any]:
    """面板上「下一步做什么」的答案：固定刻度与「今天还欠一轮源检查」两者取更早的那个。

    只判断欠不欠而不给时间，就会在补跑还在重试间隔里时谎报成下一分钟，
    或者把 30 分钟后就要点的三连挤掉。
    """
    planned = _scan_slot(now)
    if refresh_at is None:
        return planned
    when = refresh_at if refresh_at > now else now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    planned_unix = planned.get("at_unix")
    if planned_unix is not None and planned_unix <= int(when.timestamp()):
        return planned
    immediate = when <= now + timedelta(minutes=1)
    hint = "刷新批次待补跑（今天还没查过源）" if immediate else f"下次刷新批次约 {when:%H:%M}（今天还没查过源）"
    return _refresh_slot(when, hint=hint)


auto_scheduler = AutoScheduler()
