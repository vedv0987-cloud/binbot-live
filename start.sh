#!/usr/bin/env bash
# Start BinBot back up — bot + external watchdog — and re-enable both so they
# survive a reboot. Inverse of stop.sh.
#
# Usage:  bash start.sh
set -uo pipefail
SERVICE="${SERVICE:-binance-bot-v11}"

echo "▶ Starting $SERVICE ..."
# enable so it survives reboot; restart (not --now) so it ALWAYS loads the current
# on-disk code — `enable --now` is a no-op on an already-running service and would
# leave a stale process running old code after a git pull.
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"

echo "▶ Enabling the watchdog ..."
sudo systemctl enable --now binbot-watchdog.timer

# ExecStartPre (pre_start.sh) makes several Binance API calls before the bot's
# ExecStart begins, so the service can sit in 'activating' for 10-20s. Poll for
# up to 40s instead of guessing at a fixed sleep (which caused false warnings).
echo "▶ Waiting for startup (pre_start.sh runs Binance API calls first) ..."
for _i in $(seq 1 40); do
  _state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
  [ "$_state" = "active" ] && break
  [ "$_state" = "failed" ] && break
  sleep 1
done
if systemctl is-active --quiet "$SERVICE"; then
  echo "✅ $SERVICE is ACTIVE and the watchdog is enabled."
  echo "   Logs: journalctl -u $SERVICE -f"
else
  echo "⚠️  $SERVICE did not become active within 40s — check: journalctl -u $SERVICE -n 40"
  exit 1
fi
