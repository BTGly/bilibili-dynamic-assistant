"""一键重启 Binggo 控制面板。

用法（在本目录下打开 Git Bash 或 PowerShell）：
    python restart_dashboard.py            # 正常重启
    python restart_dashboard.py --force    # 有任务在跑也照样重启
    python restart_dashboard.py --status   # 只看状态，不动进程

为什么需要它：scripts/run_dashboard.py 没有开热重载，FastAPI 路由在进程启动时
就注册进内存了，改了 web/ 或 src/ 不重启永远不生效；而 web/static/dist 是每次请求
从磁盘读的，前端 build 完立刻生效 —— 于是出现「新前端调旧后端」的 404。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "bilibinggo"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LAUNCHER = ROOT / "scripts" / "run_dashboard.py"
DB = ROOT / "data" / "binggo.db"
BASE = "http://127.0.0.1:8787"
PROBE_TIMEOUT_SEC = 20


class ProbeError(RuntimeError):
    """进程探针超时/失败 —— 宁可什么都不做，也不要盲起第二个实例。"""


def _run(cmd: list[str], *, timeout: float = PROBE_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """所有外部探针统一走这里：必须 stdin=DEVNULL 且带 timeout。

    10-16 实测：不带走 stdin=DEVNULL 时，从 Bash 工具里起的 powershell 会继承那根
    永不关闭的管道并卡在 read 上 —— 10:23 那次重启脚本因此在 10:23→10:36 之间不返回，
    「重启成功」被拖成假象；第二份脚本（10:32）同样卡死，只能手杀。
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, stdout="", stderr=f"超时 {timeout}s")


def http_json(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(BASE + path, timeout=4) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def listener_pids(port: int = 8787) -> set[int]:
    """用 netstat 找出监听该端口的进程 PID。"""
    res = _run(["netstat", "-ano", "-p", "TCP"])
    if res.returncode != 0:
        raise ProbeError(f"netstat 探针失败：{res.stderr.strip() or res.returncode}")
    pids: set[int] = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        if parts[1].endswith(f":{port}"):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return pids


def dashboard_pids() -> set[int]:
    """按命令行找出所有 run_dashboard.py 进程。

    .venv\\Scripts\\python.exe 只是个跳板父进程，真正监听端口的是它拉起的
    .uv-python 子进程；只杀监听者会留一个空转的父进程。
    """
    probe = (
        "Get-CimInstance -ClassName Win32_Process | "
        "Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'run_dashboard' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    res = _run(["powershell", "-NoProfile", "-Command", probe])
    if res.returncode != 0:
        raise ProbeError(f"powershell 探针失败：{res.stderr.strip() or res.returncode}")
    return {int(token) for token in res.stdout.split() if token.isdigit()}


def target_pids() -> set[int]:
    return listener_pids() | dashboard_pids()


def safe_target_pids() -> tuple[set[int], str]:
    """探针不可用时返回 (空集, 原因)；调用方必须自己决定「未知」怎么处理。"""
    try:
        return target_pids(), ""
    except ProbeError as exc:
        return set(), str(exc)


def running_jobs() -> list[int]:
    if not DB.exists():
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id from jobs where state in ('running','pending') order by id"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [int(r[0]) for r in rows]


def backup_db() -> Path | None:
    if not DB.exists():
        return None
    dst = DB.with_name(f"binggo.db.bak-{datetime.now():%H%M%S}")
    src = sqlite3.connect(DB)
    dst_con = sqlite3.connect(dst)
    try:
        with dst_con:
            src.backup(dst_con)
    finally:
        src.close()
        dst_con.close()
    return dst


def show_status() -> None:
    pids, probe_note = safe_target_pids()
    print(f"控制面板进程: {sorted(pids) or '无'}" + (f"（探针不可用：{probe_note}）" if probe_note else ""))
    account = http_json("/api/account")
    if account:
        print(f"账号: {account.get('uname')}  mid={account.get('mid')}  登录={account.get('logged_in')}")
    auto = http_json("/api/auto/status")
    if auto:
        print(f"调度器: {auto.get('state')} — {auto.get('message')}")
        hint = auto.get("next_hint")
        if hint:
            print(f"下一次: {hint}")
    if pids or probe_note:
        print("新代码路由检查: GET /api/participation/guards -> ", end="")
        try:
            with urllib.request.urlopen(BASE + "/api/participation/guards", timeout=4) as resp:
                print(resp.status)
        except urllib.error.HTTPError as exc:
            print(exc.code, "← 404 说明进程比前端旧，需要重启")
        except (urllib.error.URLError, OSError):
            print("连不上")


def restart(*, force: bool, backup: bool, rps: str = "") -> int:
    if not PYTHON.exists() or not LAUNCHER.exists():
        print(f"找不到 {PYTHON} 或 {LAUNCHER}", file=sys.stderr)
        return 1

    try:
        pids = target_pids()
    except ProbeError as exc:
        print(f"进程探针失败（{exc}），无法确认现有实例；什么都没动。", file=sys.stderr)
        return 5
    auto_was_running = False
    if pids:
        auto = http_json("/api/auto/status") or {}
        auto_was_running = auto.get("state") == "running"
        jobs = running_jobs()
        if auto.get("state") == "running" and not force:
            print("调度器正在运行，重启会打断它。确认无误后加 --force 再来。")
            return 2
        if jobs and not force:
            print(f"有任务在跑（job {jobs}），重启会留下 interrupted。加 --force 再来。")
            return 2
        if backup:
            print(f"备份数据库 -> {backup_db()}")
        for pid in sorted(pids):
            print(f"结束 PID {pid}")
            _run(["taskkill", "/PID", str(pid), "/T", "/F"])
        time.sleep(2)

    # 用 PowerShell Start-Process 起分离进程：关掉这个终端后服务不会跟着死。
    # 必须重定向 stdout/stderr —— 之前不重定向时启动失败看不到任何原因，
    # 只能干等超时（第三批就踩过这个坑）。
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    out_file = log_dir / f"boot-{stamp}.out.txt"
    err_file = log_dir / f"boot-{stamp}.err.txt"
    # BILI_RPS 只从启动环境读取，不重启就改不回来；19:01 那次重启把用户设的 0.5
    # 打回默认 3.0，所以这里必须显式带上。
    env_prefix = ""
    if rps:
        env_prefix = f"$env:BILI_RPS='{rps}'; "
    launch = (
        "{env}Start-Process -FilePath '{exe}' -ArgumentList '\"{launcher}\"' "
        "-WorkingDirectory '{root}' -WindowStyle Hidden "
        "-RedirectStandardOutput '{out}' -RedirectStandardError '{err}' "
        "-PassThru | Select-Object -ExpandProperty Id"
    ).format(
        env=env_prefix, exe=PYTHON, launcher=LAUNCHER, root=ROOT, out=out_file, err=err_file
    )
    # 启动这一条不能走 _run() 的管道：实测 Start-Process 把面板拉起来之后，
    # powershell 自己 0.4 秒就退出，但 python 的 capture_output 还要等管道 EOF，
    # 而那根写端被刚拉起的分离进程链继承着 —— 10-16 脚本因此挂 25 分钟、
    # 10-21 挂 10 分钟没返回，守护任务那一次更是拖到 09:14 才恢复调度器（比实际晚 6 分钟）。
    # 把输出直接落文件，Python 这边不建管道，就没有任何东西可等。
    launch_log = log_dir / f"launch-{stamp}.txt"
    try:
        with launch_log.open("w", encoding="utf-8", errors="replace") as handle:
            popen = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", launch],
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        deadline = time.time() + 60
        while popen.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if popen.poll() is None:
            popen.kill()
            print("启动命令 60 秒未返回（子进程其实已经拉起），改为直接轮询端口…")
        tokens = [t for t in launch_log.read_text(encoding="utf-8", errors="replace").split() if t.isdigit()]
        new_pid = tokens[-1] if tokens else "?"
    except OSError as exc:
        print(f"启动命令没能执行：{exc}", file=sys.stderr)
        return 6
    print(f"已启动新进程 PID {new_pid}，启动输出 -> {out_file.name} / {err_file.name}")

    # 第三批之后启动会变慢（凭据权限自检等），窗口放宽到 150 秒，
    # 并且一旦发现进程消失就立刻退出并把 stderr 打出来。
    for attempt in range(150):
        time.sleep(1)
        if http_json("/api/summary") is not None:
            break
        if attempt > 8:
            alive, note = safe_target_pids()
            if not alive and not note:
                print("进程已退出，启动失败。stderr 末尾：", file=sys.stderr)
                tail = err_file.read_text(encoding="utf-8", errors="replace") if err_file.exists() else "(空)"
                print("\n".join(tail.splitlines()[-25:]), file=sys.stderr)
                return 4
    else:
        print("150 秒内端口没有响应。stderr 末尾：", file=sys.stderr)
        tail = err_file.read_text(encoding="utf-8", errors="replace") if err_file.exists() else "(空)"
        print("\n".join(tail.splitlines()[-25:]), file=sys.stderr)
        return 3

    up, note = safe_target_pids()
    print(f"服务已起来，进程 {sorted(up) or '未知'}" + (f"（探针：{note}）" if note else "") + "。")
    guards = None
    try:
        with urllib.request.urlopen(BASE + "/api/participation/guards", timeout=6) as resp:
            guards = resp.status
    except urllib.error.HTTPError as exc:
        guards = exc.code
    print(f"GET /api/participation/guards -> {guards}")
    # 重启会把调度器一起带走（面板重启后是 idle，不会自己开），10-28 就出现过
    # 「重启完到下一格之间没人参加」——重启前是开着的，这里就必须负责开回来。
    if auto_was_running:
        now_state = (http_json("/api/auto/status") or {}).get("state")
        if now_state != "running":
            try:
                req = urllib.request.Request(BASE + "/api/auto/start", method="POST",
                                             data=b"{}", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    back = json.loads(resp.read().decode("utf-8", "replace")).get("state")
                print(f"重启前调度器是开着的 → 已自动重新开启：state={back}")
            except Exception as exc:
                print(f"！！重启前调度器是开着的，但自动重开失败（{str(exc)[:80]}）；"
                      f"到点不会参加，请手动 POST /api/auto/start", file=sys.stderr)
        else:
            print("重启前调度器是开着的 → 现在仍是 running，无需重开。")
    show_status()
    print("\n最后一步：浏览器按 Ctrl+F5 硬刷新，否则还是旧 JS。")
    return 0


def _inherited_rps() -> str:
    """沿用当前共享限流文件里比默认 3.0 更保守的值，避免重启把它打回默认。"""
    path = ROOT / "data" / "task-locks" / "bilibili-rate.json"
    try:
        value = float(json.loads(path.read_text(encoding="utf-8")).get("rps"))
    except (OSError, ValueError, TypeError):
        return ""
    return f"{value:g}" if 0 < value < 3.0 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="重启 Binggo 控制面板")
    parser.add_argument("--force", action="store_true", help="有任务在跑也重启")
    parser.add_argument("--status", action="store_true", help="只看状态")
    parser.add_argument("--no-backup", action="store_true", help="重启前不备份数据库")
    parser.add_argument("--rps", default="", help="B 站请求上限，如 0.5；留空则沿用现有更保守的值")
    args = parser.parse_args()

    if args.status:
        show_status()
        return 0
    rps = args.rps or _inherited_rps()
    if rps:
        print(f"本次启动沿用限流 BILI_RPS={rps}")
    return restart(force=args.force, backup=not args.no_backup, rps=rps)


if __name__ == "__main__":
    raise SystemExit(main())
