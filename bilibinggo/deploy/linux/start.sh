#!/usr/bin/env bash
set -euo pipefail
umask 077
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_root"
if [[ ! -x .venv/bin/python ]]; then
    printf '%s\n' '请先运行 bash deploy/linux/setup.sh' >&2
    exit 1
fi
# 使用上游真实支持的环境变量；部署数据与源码分开。
export BINGGO_HOME="${BINGGO_HOME:-$project_root/var}"
mkdir -p "$BINGGO_HOME"
exec .venv/bin/python scripts/run_dashboard.py
