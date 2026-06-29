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

# Explicit CLI locations: the cron keepalive restarts this script with cron's
# bare PATH (/usr/bin:/bin), which silently breaks every claude/grok spawn
# with "CLI not found on PATH". Always assert the full set here.
export PATH="/users/PGS0407/binben14/VietHuy/AI_AGENT_SYSTEM/bin:$HOME/.local/bin:$HOME/.grok/bin:$PATH"

# Ensure the aas CLI is executable — a git/rsync of AI_AGENT_SYSTEM can drop the
# +x bit (mode 644), which silently breaks every agent's `aas ...` call with
# "Permission denied" (exit 126). Re-assert it on each launch.
AAS_BIN="/users/PGS0407/binben14/VietHuy/AI_AGENT_SYSTEM/bin/aas"
[ -f "$AAS_BIN" ] && chmod +x "$AAS_BIN" 2>/dev/null || true

# DeepSeek adapter key (model: deepseek). Loaded from the gitignored
# VietHuy/.env so it never lands in registry.yaml / project.yaml / the db.
# deepseek_stream reads DEEPSEEK_API_KEY and injects it per-subprocess only —
# Claude nodes + the user's terminal `claude` keep using subscription OAuth.
# Accepts either DEEPSEEK_API or DEEPSEEK_API_KEY in .env.
DS_ENV="/users/PGS0407/binben14/VietHuy/.env"
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f "$DS_ENV" ]; then
  _ds_val="$(grep -m1 -E '^DEEPSEEK_API(_KEY)?=' "$DS_ENV" | cut -d= -f2- | tr -d '\r\n ' || true)"
  [ -n "$_ds_val" ] && export DEEPSEEK_API_KEY="$_ds_val"
  unset _ds_val
fi
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  echo ">> DEEPSEEK_API_KEY loaded — deepseek nodes enabled"
else
  echo ">> DEEPSEEK_API_KEY not set — deepseek nodes will error until set"
fi

PORT="${PORT:-5174}"
echo ">> AgentUI chạy ở http://127.0.0.1:${PORT}"
# Reload AN TOÀN (bật/tắt qua RELOAD env, mặc định bật): chỉ watch backend/
# (code .py), KHÔNG watch cả app/. Lý do: agentui.db (+ -wal/-journal) nằm ở
# app/ (không phải app/backend/), nên DB-write lúc agent đang trả lời KHÔNG còn
# trigger reload → không tái hiện bug "reload giữa turn giết claude -p con →
# agent đứt giữa câu". --reload-exclude là lớp chặn dự phòng. Sửa code .py → tự
# reload; sửa app.js → hard-refresh trình duyệt.
#   RELOAD=0 ./run.sh   → tắt watcher (ít process nền hơn trên login node)
RELOAD="${RELOAD:-1}"
RELOAD_ARGS=()
if [ "$RELOAD" = "1" ]; then
  echo ">> reload BẬT (watch $HERE/backend, bỏ qua *.db) — RELOAD=0 để tắt"
  RELOAD_ARGS=(--reload --reload-dir "$HERE/backend"
    --reload-exclude '*.db' --reload-exclude '*.db-wal' --reload-exclude '*.db-journal')
else
  echo ">> reload TẮT — sửa code .py cần restart thủ công"
fi
exec uvicorn backend.main:app --host 127.0.0.1 --port "${PORT}" ${RELOAD_ARGS[@]+"${RELOAD_ARGS[@]}"}
