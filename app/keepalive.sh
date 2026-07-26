#!/usr/bin/env bash
# Watchdog: restart AgentUI if 127.0.0.1:5174 stops answering.
# Installed in crontab (NFS-shared across login nodes) — the hostname gate below
# makes ONLY the canonical node act, so every other login node exits 0 and never
# starts a second server writing the same SQLite file over NFS (corruption risk).
# Canonical node switched ascend-login01 → ascend-login02 on 2026-07-21.
[ "$(hostname -s)" = "ascend-login02" ] || exit 0

APP_DIR="/users/PGS0407/binben14/VietHuy/agent_code_IDLE/app"
LOG="/tmp/agentui.log"

code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:5174/ 2>/dev/null)
if [ "$code" = "200" ]; then
    exit 0
fi

echo "[keepalive $(date '+%F %T')] server down (HTTP ${code:-none}), restarting" >> "$LOG"
cd "$APP_DIR" || exit 1
setsid nohup bash run.sh </dev/null >>"$LOG" 2>&1 &
