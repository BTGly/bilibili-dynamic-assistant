from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Literal

from src.activity_store import known_activity_ids
from src.pipeline import checkpoint_store as checkpoints
from src.task_lock import serialized_task
from src.participation_guard import failure_kind, get_guard, record_failure
import httpx
import threading
from src.bilibili_client import BilibiliClient
from src.lottery_enricher import (
    ENRICH_SKIP_REASON,
    EnrichedActivity,
    EnrichSkippedError,
    enrich_activity,
    is_enrich_detail_skip_error,
)
from src.lottery_classifier import LotteryType
from src.lottery_time import BEIJING_TZ
from src.participation_store import load_participations
from src.pipeline.classify_step import ClassifyOutcome, classify_new_link
from src.pipeline.status_step import apply_initial_status
from src.sources.common import CheckResult, normalize_activity_id

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class PipelineResult:
    ok: bool
    pipeline_skipped: bool
    raw_link_count: int
    new_link_count: int
    classified_count: int
    skipped_count: int
    enriched_count: int
    persisted_count: int
    skip_reasons: dict[str, int] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _dedupe_new_dynamic_ids(raw_urls: list[str]) -> list[str]:
    known = known_activity_ids()
    seen: set[str] = set()
    ordered: list[str] = []
    for raw_url in raw_urls:
        dynamic_id = normalize_activity_id(str(raw_url))
        if not dynamic_id or dynamic_id in known or dynamic_id in seen:
            continue
        seen.add(dynamic_id)
        ordered.append(dynamic_id)
    return ordered


def _collect_updated_links(ds_results: list[CheckResult]) -> list[str]:
    links: list[str] = []
    for result in ds_results:
        if not result.updated:
            continue
        links.extend(result.activity_links or [])
    return links


def _collect_link_time_hints(ds_results: list[CheckResult]) -> dict[str, dict]:
    """合集/清单贴里逐条写明的开奖日期，带上容器出处供入库时补时间。"""
    hints: dict[str, dict] = {}
    for result in ds_results:
        if not result.updated:
            continue
        for dynamic_id, hint in (result.link_time_hints or {}).items():
            hints[dynamic_id] = {
                **hint,
                "container_url": result.container_url,
                "reference_ts": result.published_at,
            }
    return hints


def _apply_container_time(activity: EnrichedActivity, hint: dict) -> bool:
    """详情里没有开奖时间时，用合集群贴原句补上；只补空，不覆盖平台给的时间。"""
    if activity.lottery_time:
        return False
    value = str(hint.get("lottery_time") or "")
    try:
        stamp = int(
            datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING_TZ).timestamp()
        )
    except ValueError:
        return False
    conditions = dict(activity.conditions or {})
    conditions.update(
        lottery_time_text=value,
        lottery_time_source="container_text",
        lottery_time_precision=hint.get("lottery_time_precision") or "date",
        lottery_time_evidence=hint.get("lottery_time_evidence") or "",
        time_reference_ts=hint.get("reference_ts"),
        container_url=hint.get("container_url") or "",
        time_extraction_checked=True,
    )
    activity.conditions = conditions
    activity.lottery_time = stamp
    return True


def _account_failure(exc: BaseException) -> bool:
    # Provider HTTP 429 is an LLM problem, not a Bilibili account restriction.
    if isinstance(exc, httpx.HTTPStatusError):
        if not exc.request.url.host.endswith("bilibili.com"):
            return False
    if "LLM" in str(exc) or "API Key" in str(exc):
        return False
    return failure_kind(exc) == "account_paused"


@serialized_task
def run_new_links_pipeline(
    raw_urls: list[str], *, workers: int = 4,
    on_progress: ProgressCallback | None = None,
    batch_size: int = 20,
    limit: int | None = None,
    cancel_event: threading.Event | None = None,
    link_time_hints: dict[str, dict] | None = None,
) -> PipelineResult:
    """Register first, classify in bounded batches, persist every finished item."""
    if not 1 <= batch_size <= 100 or not 1 <= workers <= 16:
        raise ValueError("batch_size 必须为 1～100，workers 必须为 1～16")
    if limit is not None and limit <= 0:
        raise ValueError("limit 必须为正整数")
    checkpoints.enqueue(_dedupe_new_dynamic_ids(raw_urls))
    known = known_activity_ids()
    dynamic_ids = [did for did in checkpoints.pending_ids() if did not in known]
    if limit:
        dynamic_ids = dynamic_ids[:limit]
    total = len(dynamic_ids)
    if not total:
        pending = checkpoints.preview()
        message = "无待处理的新链接，流水线结束"
        if pending:
            message += f"；检查点待处理/核查 {len(pending)} 条"
        return PipelineResult(True, True, len(raw_urls), 0, 0, 0, 0, 0, message=message)
    participations = load_participations()
    reasons: dict[str, int] = {}
    classified_count = enriched_count = persisted = processed = 0
    container_dated = 0

    def check_cancel():
        if cancel_event and cancel_event.is_set():
            raise ValueError("任务已取消；已完成的活动已入库，未完成项保留检查点")

    def notify(done, count, message):
        check_cancel()
        if on_progress:
            on_progress(done, count, message)

    def note_failure(did, exc, phase):
        checkpoints.failed(did, exc)
        reasons[phase] = reasons.get(phase, 0) + 1

    if get_guard("")["state"] != "ready":
        raise RuntimeError("账号队列已暂停：" + get_guard("")["reason"])
    with BilibiliClient() as client:
        client._pause_on_account_error = True
        for offset in range(0, total, batch_size):
            check_cancel()
            if get_guard("")["state"] != "ready":
                raise RuntimeError("账号队列已暂停：" + get_guard("")["reason"])
            batch = dynamic_ids[offset:offset + batch_size]
            tasks = []
            notify(offset, total, f"正在分类 {total} 条新链接（每批最多 {batch_size} 条）…")
            for index, did in enumerate(batch):
                check_cancel()
                outcome = checkpoints.cached_outcome(did)
                if outcome is None:
                    try:
                        outcome = classify_new_link(client, did)
                    except Exception as exc:
                        note_failure(did, exc, "分类异常")
                        if _account_failure(exc):
                            record_failure("", exc)
                            raise RuntimeError(f"账号异常，刷新队列已暂停：{exc}") from exc
                        notify(offset + index + 1, total, "分类进度（异常目标已暂缓）")
                        continue
                    if get_guard("")["state"] != "ready":
                        raise RuntimeError("账号队列已暂停：" + get_guard("")["reason"])
                    checkpoints.classified(outcome)
                if outcome.skipped:
                    reason = outcome.skip_reason or "skipped"
                    reasons[reason] = reasons.get(reason, 0) + 1
                elif outcome.lottery_type in ("互动抽奖", "预约抽奖", "转发抽奖"):
                    tasks.append(outcome)
                    classified_count += 1
                else:
                    checkpoints.skipped(did, "非抽奖活动")
                    reasons["非抽奖活动"] = reasons.get("非抽奖活动", 0) + 1
                notify(offset + index + 1, total, "分类进度")

            notify(0, len(tasks), f"正在拉取 {len(tasks)} 条活动详情…")
            # Stop submitting at batch boundaries. Drain already issued requests even
            # when cancellation/progress reporting fails, preserving their results.
            deferred_error = None
            with ThreadPoolExecutor(max_workers=min(workers, max(1, len(tasks)))) as executor:
                futures = {executor.submit(
                    enrich_activity, client, dynamic_id=o.dynamic_id, lottery_type=o.lottery_type,
                    participation=participations.get(o.dynamic_id),
                    classify_content=o.classify_content or None, classify_detail=o.detail_item,
                    classify_notice=o.lottery_notice,
                    classify_notice_business_type=o.notice_business_type,
                    classify_notice_business_id=o.notice_business_id,
                ): o.dynamic_id for o in tasks}
                for index, future in enumerate(as_completed(futures), 1):
                    did = futures[future]
                    try:
                        activity = future.result()
                    except Exception as exc:
                        if isinstance(exc, EnrichSkippedError) or is_enrich_detail_skip_error(exc):
                            # Inaccessible details may be temporary; don't permanently lose them.
                            note_failure(did, exc, ENRICH_SKIP_REASON)
                        else:
                            note_failure(did, exc, "详情异常")
                        if _account_failure(exc) and deferred_error is None:
                            record_failure("", exc)
                            deferred_error = RuntimeError(f"账号异常，刷新队列已暂停：{exc}")
                    else:
                        if activity.skipped:
                            checkpoints.skipped(did, str(activity.skip_reason or "详情跳过"))
                            reasons[ENRICH_SKIP_REASON] = reasons.get(ENRICH_SKIP_REASON, 0) + 1
                        else:
                            if link_time_hints and did in link_time_hints:
                                if _apply_container_time(activity, link_time_hints[did]):
                                    container_dated += 1
                            enriched_count += 1
                            persisted += checkpoints.persist(apply_initial_status(activity.to_dict()))
                    if deferred_error is None:
                        try:
                            notify(index, len(tasks), "详情进度（完成项已逐条入库）")
                        except Exception as exc:
                            deferred_error = exc
                if deferred_error is not None:
                    raise deferred_error
            processed += len(batch)
            notify(processed, total, f"入库完成，累计新增 {persisted} 条活动")
    failures = sum(reasons.get(k, 0) for k in ("分类异常", "详情异常", ENRICH_SKIP_REASON))
    pending = checkpoints.preview()
    message = f"新入库 {persisted} 条活动"
    if not total:
        message = "无待处理的新链接，流水线结束"
    if failures:
        message += f"；{failures} 条异常已暂缓，最多自动尝试 3 次后待核查"
    if container_dated:
        message += f"；{container_dated} 条开奖时间取自合集群贴原文"
    if pending:
        message += f"；检查点待处理/核查 {len(pending)} 条"
    return PipelineResult(
        ok=True, pipeline_skipped=not total, raw_link_count=len(raw_urls),
        new_link_count=total, classified_count=classified_count,
        skipped_count=sum(reasons.values()), enriched_count=enriched_count,
        persisted_count=persisted, skip_reasons=reasons, message=message,
    )


def run_refresh_all_pipeline(
    ds_results: list[CheckResult], *, workers: int = 4,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> PipelineResult:
    # Saved pending work is eligible even when all source containers are unchanged.
    kwargs = {
        "workers": workers,
        "on_progress": on_progress,
        "link_time_hints": _collect_link_time_hints(ds_results),
    }
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    return run_new_links_pipeline(_collect_updated_links(ds_results), **kwargs)
