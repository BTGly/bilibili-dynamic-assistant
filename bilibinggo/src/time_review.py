from __future__ import annotations

import time

from src.db.activity_codec import row_to_activity_dict, activity_dict_to_row
from src.db.models import ActivityRow
from src.db.session import session_scope
from src.lottery_time import classify_activity_timing, normalize_activity_timing
from src.participation_guard import account_write_lock


def ensure_timing_allows_manual_participation(dynamic_id: str):
    with session_scope() as session:
        row = session.get(ActivityRow, dynamic_id)
        if row is None:
            return
        status, _, _ = classify_activity_timing(row_to_activity_dict(row))
    if status == "待审核":
        # 10-26 起开奖时间未知不再是需要人工批准的硬门槛：自动队列会直接把它排到队尾参加
        return
    if status in ("已截止", "已结束", "已参加"):
        raise RuntimeError(f"活动当前为「{status}」，无法参与")


def review_activity_time(dynamic_id: str, *, decision: str, evidence: str) -> dict:
    if decision not in ("allow_manual", "defer") or not evidence.strip():
        raise ValueError("必须选择人工参与或继续待审核，并填写核查依据")
    with account_write_lock(), session_scope() as session:
        row = session.get(ActivityRow, dynamic_id)
        if row is None:
            raise ValueError("未找到活动")
        item = row_to_activity_dict(row)
        item["time_review"] = {"decision": decision, "evidence": evidence.strip(), "reviewed_at": int(time.time())}
        normalize_activity_timing(item)
        updated = activity_dict_to_row(item)
        for name in ActivityRow.model_fields:
            if name != "dynamic_id":
                setattr(row, name, getattr(updated, name))
        return item
