#!/usr/bin/env bash
# Start BinBot back up — bot + external watchdog — and re-enable both so they
# survive a reboot. Inverse of stop.sh.
#
# Usage:  bash start.sh
set -uo pipefail
SERVICE="${SERVICE:-binance-bot-v11}"

echo "▶ Starting $SERVICE ..."
sudo systemctl enable --now "$SERVICE"

echo "▶ Enabling the watchdog ..."
sudo systemctl enable --now binbot-watchdog.timer

sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  echo "✅ $SERVICE is ACTIVE and the watchdog is enabled."
  echo "   Logs: journalctl -u $SERVICE -f"
else
  echo "⚠️  $SERVICE did not stay active — check: journalctl -u $SERVICE -n 40"
  exit 1
fi
