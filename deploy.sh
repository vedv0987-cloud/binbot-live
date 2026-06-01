#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# BinBot deploy — pull latest code from GitHub, gate on compile+tests, restart.
#
# Code is updated with `git reset --hard origin/main`. Because all runtime state
# (bot_state.json, *.jsonl, *.db, *.pkl, .env) is gitignored, reset --hard ONLY
# touches tracked .py code and NEVER your live state. No `git clean` is run, so
# ignored files are always preserved.
#
# If compile OR the unit tests fail, the script rolls back to the previous commit
# and does NOT restart the bot — a bad push can't take live trading down.
#
# Usage on the VM:   bash /home/ubuntu/binbot_live/deploy.sh
# ──────────────────────────────────────────────────────────────────────────
set -uo pipefail

REPO_DIR="/home/ubuntu/binbot_live"
SERVICE="binance-bot-v11"
BRANCH="main"

cd "$REPO_DIR" || { echo "❌ repo dir $REPO_DIR not found"; exit 1; }

# Use the SAME python the systemd service runs (so the test gate has the bot's deps).
PYTHON="$(systemctl show -p ExecStart --value "$SERVICE" 2>/dev/null | grep -oE '[^ ]*/(python[0-9.]*)' | head -n1)"
[ -x "$PYTHON" ] || PYTHON="python3"
echo "▶ Using python: $PYTHON"

echo "▶ Fetching origin/$BRANCH ..."
git fetch --quiet origin "$BRANCH" || { echo "❌ git fetch failed (check VM repo auth)"; exit 1; }

PREV="$(git rev-parse HEAD)"
NEW="$(git rev-parse "origin/$BRANCH")"
if [ "$PREV" = "$NEW" ]; then
  echo "✓ Already up to date ($(git rev-parse --short HEAD)). Nothing to deploy."
  exit 0
fi
echo "  $(git rev-parse --short "$PREV") → $(git rev-parse --short "$NEW")"

echo "▶ Updating code (gitignored state is left untouched) ..."
git reset --hard "origin/$BRANCH" || { echo "❌ git reset failed"; exit 1; }

echo "▶ GATE 1/2: byte-compile all .py ..."
if ! $PYTHON -m py_compile *.py; then
  echo "❌ compile FAILED — rolling back to $(git rev-parse --short "$PREV"), NOT restarting."
  git reset --hard "$PREV"; exit 1
fi

echo "▶ GATE 2/2: unit tests (test_core) ..."
if ! BINANCE_API_KEY=dummy BINANCE_API_SECRET=dummy $PYTHON -m unittest test_core; then
  echo "❌ TESTS FAILED — rolling back to $(git rev-parse --short "$PREV"), NOT restarting."
  git reset --hard "$PREV"; exit 1
fi

echo "▶ Backing up bot_state.json ..."
[ -f bot_state.json ] && cp -a bot_state.json "bot_state.json.deploy_$(date +%F_%H%M%S)"

echo "▶ Restarting $SERVICE ..."
sudo systemctl restart "$SERVICE"
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  echo "✅ Deployed $(git rev-parse --short "$NEW") — $SERVICE is ACTIVE"
else
  echo "⚠️  $SERVICE not active after restart — check: journalctl -u $SERVICE -n 50"
  exit 1
fi

echo "▶ Recent logs:"
sudo journalctl -u "$SERVICE" -n 12 --no-pager -o cat
