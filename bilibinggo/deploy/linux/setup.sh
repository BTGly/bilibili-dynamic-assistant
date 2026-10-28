#!/usr/bin/env bash
set -euo pipefail
umask 077
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_root"
python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit('需要 Python 3.12 或更高版本；请先提供服务器系统版本，再安装相应 Python。')
PY
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements-server.lock
if [[ ! -f web/static/dist/index.html ]]; then
    printf '%s\n' '缺少已构建的前端文件，请使用完整发布包。' >&2
    exit 1
fi
printf '%s\n' '依赖安装完成。启动命令：bash deploy/linux/start.sh'
