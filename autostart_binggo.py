"""Binggo 面板守护脚本（幂等）：面板没在跑就拉起来，并恢复你之前开着的自动调度。

设计要点
- 面板已经健康时**什么都不做**，所以它被反复触发也不会打断你正在跑的任务。
- 只有在它真的把面板从「没跑」拉起来之后，才顺手开启调度器 —— 这样你在活着的面板里
  手动点「停止调度」不会被下一轮守护偷偷改回来。
- 只跟本机 127.0.0.1:8787 说话，不碰外部网络。

用法：
    pythonw.exe autostart_binggo.py           # 静默（计划任务用这个）
    python.exe  autostart_binggo.py --status  # 只看状态，不动任何东西
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8787"
LOG_PATH = ROOT / "logs" / "autostart.log"
READINESS_TIMEOUT = 150


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {line}\n")


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8787), timeout=2):
            return True
    except OSError:
        return False


def http_json(path: str, timeout: float = 8.0, payload: dict | None = None) -> dict | None:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def launch() -> bool:
    """用 restart_dashboard.py 拉起面板：没有旧进程时它只会启动，不会杀任何东西。"""
    python = ROOT / "bilibinggo" / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        log(f"找不到解释器 {python}")
        return False
    try:
        result = subprocess.run(
            [str(python), "-u", str(ROOT / "restart_dashboard.py")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        # 10-16 实测：子进程不带 stdin=DEVNULL 会继承守护进程那根不关闭的管道并卡死，
        # 面板其实早就起来了。这里不能让它把整个守护脚本炸掉。
        log("启动脚本 600 秒未返回（面板可能已经起来了），改由下面的 /api/summary 判定")
        return http_json("/api/summary") is not None
    for line in (result.stdout or "").splitlines():
        if line.strip():
            log(f"启动输出：{line.strip()}")
    if result.returncode not in (0, 2):
        log(f"启动脚本退出码 {result.returncode}，stderr 末尾：{(result.stderr or '')[-300:]}")
    return http_json("/api/summary") is not None


def wait_ready() -> bool:
    deadline = time.time() + READINESS_TIMEOUT
    while time.time() < deadline:
        if http_json("/api/summary") is not None:
            return True
        time.sleep(3)
    return False


def scheduler_state() -> str:
    status = http_json("/api/auto/status") or {}
    return str(status.get("state") or "unknown")


def main() -> int:
    parser = argparse.ArgumentParser(description="确保 Binggo 面板与自动调度在运行")
    parser.add_argument("--status", action="store_true", help="只报告状态，不做任何改动")
    args = parser.parse_args()

    alive = port_open() and http_json("/api/summary") is not None
    state = scheduler_state() if alive else "down"

    if args.status:
        print(f"面板: {'在跑' if alive else '没在跑'} | 调度器: {state}")
        return 0 if alive else 1

    if alive:
        # 已经健康：不打扰，也不去改调度器开关。
        return 0

    log("面板未在运行，尝试拉起")
    if not launch() and not wait_ready():
        log(f"拉起后面板在 {READINESS_TIMEOUT}s 内没响应，放弃本轮")
        return 3

    state = scheduler_state()
    if state == "running":
        log("面板已恢复，调度器本来就在运行")
        return 0
    started = http_json("/api/auto/start", payload={})
    if started and started.get("state") == "running":
        log(f"面板已恢复，并重新开启自动调度（启动于 {started.get('started_at')}）")
        return 0
    log("面板已恢复，但自动调度没能开启，需要手动点一下")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
