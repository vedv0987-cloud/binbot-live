#!/usr/bin/env bash
# Stop BinBot COMPLETELY and keep it stopped — bot + external watchdog.
#
# The watchdog (binbot-watchdog.timer) restarts the bot on a stale heartbeat,
# so a plain `systemctl stop binance-bot-v11` comes back ~10 min later. This
# stops the watchdog FIRST (so it can't restart the bot), then the bot, and
# disables both so nothing auto-starts on a reboot either.
#
# Usage:  bash stop.sh
# Start again later with:  bash start.sh
set -uo pipefail
SERVICE="${SERVICE:-binance-bot-v11}"

echo "▶ Stopping the watchdog first (so it can't restart the bot) ..."
sudo systemctl disable --now binbot-watchdog.timer 2>/dev/null || true
sudo systemctl stop binbot-watchdog.service 2>/dev/null || true

echo "▶ Stopping $SERVICE ..."
sudo systemctl disable --now "$SERVICE" 2>/dev/null || true

sleep 2
if systemctl is-active --quiet "$SERVICE"; then
  echo "⚠️  $SERVICE is STILL active — check: systemctl status $SERVICE"
  exit 1
fi
echo "✅ STOPPED. $SERVICE + watchdog are down and disabled — they will NOT restart"
echo "   (not on a stale heartbeat, not on reboot). Run 'bash start.sh' to bring it back."
