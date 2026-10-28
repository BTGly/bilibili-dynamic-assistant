from __future__ import annotations

import time

from src.draw_reminder import DRAWING_SOON_SECONDS
from src.lottery_time import normalize_activity_timing, time_boundary

THREE_DAYS = DRAWING_SOON_SECONDS


def apply_initial_status(activity: dict, *, now: int | None = None) -> dict:
    """为新入库活动计算参与状态与开奖标签（已结束仅由开奖时间决定）。"""
    current = int(now if now is not None else time.time())
    activity["user_status_source"] = "default"
    activity["draw_tag"] = ""
    activity["status_classified"] = True
    normalize_activity_timing(activity, now=current)
    deadline = time_boundary(activity, "lottery_time")
    if activity["draw_status"] == "active" and deadline and deadline - THREE_DAYS <= current < deadline:
        activity["draw_tag"] = "即将开奖"
    return activity
