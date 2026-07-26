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

# Optional local secrets file (gitignored). If present, its exports are picked
# up here so local runs (macOS) don't depend on the OSC VietHuy/.env paths
# below. Put TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS / DEEPSEEK_API_KEY here for
# local testing. Pre-existing env values always win (the readers below use
# `[ -z "${VAR:-}" ]` guards).
if [ -f "$HERE/.env.local" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERE/.env.local"
  set +a
  echo ">> .env.local loaded"
fi

# Explicit CLI locations: the cron keepalive restarts this script with cron's
# bare PATH (/usr/bin:/bin), which silently breaks every claude/grok spawn
# with "CLI not found on PATH". Always assert the full set here.
export PATH="/users/PGS0407/binben14/VietHuy/AI_AGENT_SYSTEM/bin:$HOME/.local/bin:$HOME/.grok/bin:$PATH"

# RTK is shared by every AgentUI subprocess. Claude-family adapters use RTK's
# global PreToolUse hook; Codex uses ~/.codex/AGENTS.md + RTK.md instructions.
# Keeping ~/.local/bin explicit here also covers cron/keepalive's minimal PATH.
if command -v rtk >/dev/null 2>&1; then
  echo ">> $(rtk --version) available — Claude hook + Codex instructions enabled"
else
  echo ">> WARNING: rtk not found — agents will receive uncompressed shell output"
fi

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

# Telegram control channel (drives the BOSS orchestrator by default). Loaded
# from the same gitignored VietHuy/.env so the token never lands in code or the
# db. Absent token ⇒ channel disabled (no-op). TELEGRAM_CHAT_IDS is the
# numeric allow-list of chats allowed to control the bot.
TG_ENV="/users/PGS0407/binben14/VietHuy/.env"
_read_env_key() {
  [ -f "$TG_ENV" ] || return 0
  grep -m1 -E "^$1=" "$TG_ENV" | cut -d= -f2- | tr -d '\r\n ' || true
}
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  _val="$(_read_env_key TELEGRAM_BOT_TOKEN)"
  [ -n "$_val" ] && export TELEGRAM_BOT_TOKEN="$_val"
fi
if [ -z "${TELEGRAM_CHAT_IDS:-}" ]; then
  _val="$(_read_env_key TELEGRAM_CHAT_IDS)"
  [ -n "$_val" ] && export TELEGRAM_CHAT_IDS="$_val"
fi
# Optional knobs with defaults (only export if the user set them).
for _k in TELEGRAM_AGENT_SLUG TELEGRAM_AGENT_ID; do
  _val="$(_read_env_key "$_k")"
  [ -n "$_val" ] && export "$_k=$_val"
done
unset _val
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo ">> TELEGRAM_BOT_TOKEN loaded — control channel enabled (agent ${TELEGRAM_AGENT_ID:-BOSS} in ${TELEGRAM_AGENT_SLUG:-energy})"
else
  echo ">> TELEGRAM_BOT_TOKEN not set — control channel disabled"
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
