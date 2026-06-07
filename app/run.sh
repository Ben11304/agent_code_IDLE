#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if [ ! -d ".venv" ]; then
  echo ">> tạo venv (.venv)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q --upgrade pip
pip install -q -r backend/requirements.txt

PORT="${PORT:-5174}"
echo ">> AgentUI chạy ở http://127.0.0.1:${PORT}"
exec uvicorn backend.main:app --host 127.0.0.1 --port "${PORT}" --reload
