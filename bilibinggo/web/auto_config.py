"""定时点击调度器配置（仅允许 4 个按钮 action）。"""

from __future__ import annotations

# 刷新批次优先且会把调度循环独占几分钟，所以 REFRESH_HOURS 整点那一分钟的三连会被批次吃掉，
# 该小时的 :15/:30/:45 仍照常三连。
# 一天只查一次源会让「09:00 之后发的合集」等到第二天才入库、当晚开奖的预约直接漏掉，
# 所以按 9/15/21 三个窗口各查一轮（每轮成功即记该窗口，失败一小时后补跑）。
REFRESH_HOURS = frozenset({5, 9, 13, 17, 21})   # 10-27：每 4 小时一轮，面板负责主发现，巡查退居兜底
TRIPLE_MINUTES = frozenset({0, 15, 30, 45})

ALLOWED_CLICK_ACTIONS = frozenset(
    {
        "refresh_all",
        "refresh_watch",
        "refresh_status",
        "participate_triple",
    }
)

ACTION_LABELS = {
    "refresh_all": "一键更新活动链接",
    "refresh_watch": "更新监控用户动态",
    "refresh_status": "刷新任务状态",
    "participate_triple": "三连参与",
}

JOB_POLL_INTERVAL_SEC = 2.0
JOB_POLL_TIMEOUT_SEC = 6 * 60 * 60
