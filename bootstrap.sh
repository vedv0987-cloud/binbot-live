#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# BinBot bootstrap — first-time setup of a FRESH VM (Oracle free tier, etc.).
#
# Use this when you spin up a brand-new VM (e.g. an old one's kernel broke) and
# want to get BinBot live again with your EXISTING data. It is idempotent — safe
# to re-run — and it NEVER overwrites your .env or live state files.
#
# What it does, in order:
#   1. Installs OS packages (python3, pip, git, build tools).
#   2. Clones the repo to $REPO_DIR (or fast-forwards it if already there).
#   3. Installs the Python dependencies for the SAME python systemd will run.
#   4. Restores your .env + state from a backup dir if you pass --restore <dir>;
#      otherwise leaves whatever is already there untouched.
#   5. Installs the systemd units (bot + watchdog timer), patched to this user.
#   6. Runs the compile + unit-test gate — aborts before starting if it fails.
#   7. Verifies the Binance API works FROM THIS VM'S IP (the #1 fresh-VM gotcha:
#      a new VM = a new public IP that Binance rejects until you whitelist it).
#   8. Enables + starts the services — but only if the API check passed.
#
# Usage on the fresh VM:
#   sudo bash bootstrap.sh                      # clone + set up + start
#   sudo bash bootstrap.sh --restore /path/bak  # also restore .env + state
#   sudo bash bootstrap.sh --no-start           # set everything up, don't start
#
# After cloning the repo yourself you can also just run it in place:
#   sudo bash /home/ubuntu/binbot_live/bootstrap.sh
# ──────────────────────────────────────────────────────────────────────────
set -uo pipefail

# ── Tunables (override via env, e.g. RUN_USER=opc sudo -E bash bootstrap.sh) ──
REPO_URL="${REPO_URL:-https://github.com/vedv0987-cloud/binbot-live.git}"
RUN_USER="${RUN_USER:-ubuntu}"
REPO_DIR="${REPO_DIR:-/home/${RUN_USER}/binbot_live}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-binance-bot-v11}"
PYTHON="${PYTHON:-/usr/bin/python3}"

# ── Flags ──
RESTORE_DIR=""
DO_START=1
while [ $# -gt 0 ]; do
  case "$1" in
    --restore) RESTORE_DIR="${2:-}"; shift 2 ;;
    --no-start) DO_START=0; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠️  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo (needs apt + systemctl). Try: sudo bash $0"
id "$RUN_USER" >/dev/null 2>&1 || die "User '$RUN_USER' does not exist. Set RUN_USER=<your-vm-user>."

# Run a command AS the unprivileged service user (for git/pip/tests).
as_user() { sudo -u "$RUN_USER" -H bash -lc "$*"; }

# ── 1. OS packages ──────────────────────────────────────────────────────────
say "Installing OS packages (python3, pip, git, build tools) ..."
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq || warn "apt-get update had warnings"
  apt-get install -y -qq python3 python3-pip python3-venv git build-essential curl ca-certificates \
    || die "apt-get install failed"
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip git gcc gcc-c++ make curl ca-certificates || die "dnf install failed"
else
  warn "No apt-get/dnf found — make sure python3, pip, git, a C toolchain and curl are installed."
fi
ok "OS packages ready ($($PYTHON --version 2>&1))"

# ── 2. Repo: clone or fast-forward ───────────────────────────────────────────
if [ -d "$REPO_DIR/.git" ]; then
  say "Repo exists at $REPO_DIR — fetching origin/$BRANCH ..."
  as_user "cd '$REPO_DIR' && git fetch --quiet origin '$BRANCH' && git checkout --quiet '$BRANCH' && git reset --hard 'origin/$BRANCH'" \
    || die "git update failed in $REPO_DIR"
else
  say "Cloning $REPO_URL → $REPO_DIR ..."
  install -d -o "$RUN_USER" -g "$RUN_USER" "$(dirname "$REPO_DIR")"
  as_user "git clone --branch '$BRANCH' '$REPO_URL' '$REPO_DIR'" || die "git clone failed"
fi
chown -R "$RUN_USER":"$RUN_USER" "$REPO_DIR"
ok "Code at $(as_user "cd '$REPO_DIR' && git rev-parse --short HEAD") on $BRANCH"

# ── 3. Python dependencies (into the python systemd runs) ────────────────────
say "Installing Python dependencies ..."
PIP_INSTALL="$PYTHON -m pip install --upgrade -r '$REPO_DIR/requirements.txt'"
if ! as_user "$PIP_INSTALL" 2>/tmp/pip_err; then
  if grep -qi 'externally-managed' /tmp/pip_err; then
    warn "PEP 668 externally-managed env — retrying with --break-system-packages"
    as_user "$PYTHON -m pip install --upgrade --break-system-packages -r '$REPO_DIR/requirements.txt'" \
      || die "pip install failed (see /tmp/pip_err)"
  else
    cat /tmp/pip_err >&2; die "pip install failed"
  fi
fi
ok "Python deps installed"

# ── 4. Restore .env + live state (optional, never clobbers) ──────────────────
STATE_FILES=(.env bot_state.json trade_history.json equity_curve.json coin_profiles.json \
             kelly_history.json selfhealer_state.json .feature_flags.json)
if [ -n "$RESTORE_DIR" ]; then
  [ -d "$RESTORE_DIR" ] || die "--restore dir '$RESTORE_DIR' not found"
  say "Restoring data from $RESTORE_DIR ..."
  for f in "${STATE_FILES[@]}"; do
    if [ -e "$RESTORE_DIR/$f" ]; then
      cp -a "$RESTORE_DIR/$f" "$REPO_DIR/$f"; ok "restored $f"
    fi
  done
  # ML models / scalers, if you backed them up
  for f in "$RESTORE_DIR"/*.pkl "$RESTORE_DIR"/*.joblib; do
    [ -e "$f" ] && { cp -a "$f" "$REPO_DIR/"; ok "restored $(basename "$f")"; }
  done
  chown -R "$RUN_USER":"$RUN_USER" "$REPO_DIR"
fi

# .env is mandatory to trade — create a stub and stop if it's still missing.
if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  chown "$RUN_USER":"$RUN_USER" "$REPO_DIR/.env"; chmod 600 "$REPO_DIR/.env"
  warn "No .env found — created one from .env.example."
  die "Edit $REPO_DIR/.env with your Binance keys (and restore your state), then re-run."
fi
chmod 600 "$REPO_DIR/.env"
ok ".env present"

# ── 5. systemd units (patched to THIS user + dir) ────────────────────────────
say "Installing systemd units ..."
for unit in "$SERVICE.service" binbot-watchdog.service binbot-watchdog.timer; do
  src="$REPO_DIR/$unit"
  [ -f "$src" ] || { warn "missing $unit in repo — skipping"; continue; }
  sed -e "s#/home/ubuntu/binbot_live#${REPO_DIR}#g" \
      -e "s#^User=ubuntu#User=${RUN_USER}#g" \
      "$src" > "/etc/systemd/system/$unit"
done
chmod +x "$REPO_DIR"/*.sh 2>/dev/null || true
systemctl daemon-reload
ok "Units installed"

# ── 6. Compile + unit-test gate (same gate deploy.sh uses) ───────────────────
say "GATE: byte-compile + unit tests ..."
as_user "cd '$REPO_DIR' && $PYTHON -m py_compile *.py" || die "compile FAILED — not starting"
as_user "cd '$REPO_DIR' && BINANCE_API_KEY=dummy BINANCE_API_SECRET=dummy $PYTHON -m unittest test_core" \
  || die "unit tests FAILED — not starting"
ok "Gate passed"

# ── 7. Verify Binance API works from THIS VM's public IP ─────────────────────
say "Checking Binance API connectivity from this VM ..."
PUBLIC_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null \
            || curl -fsS --max-time 8 https://ifconfig.me 2>/dev/null || echo '?')"
echo "  This VM's public IP: ${PUBLIC_IP}"
API_OK=0
if as_user "cd '$REPO_DIR' && set -a && . ./.env && set +a && $PYTHON - <<'PY'
import os, sys
try:
    from binance.client import Client
    c = Client(os.environ['BINANCE_API_KEY'], os.environ['BINANCE_API_SECRET'])
    c.get_account()  # signed + IP-restricted: fails if this IP isn't whitelisted
    print('API_OK')
except Exception as e:
    print('API_FAIL:', e); sys.exit(1)
PY" ; then
  API_OK=1; ok "Binance API reachable and key authorized from ${PUBLIC_IP}"
else
  warn "Binance API check FAILED. Most likely this new VM's IP isn't whitelisted yet."
  warn "Fix: Binance → API Management → edit key → add IP ${PUBLIC_IP} → save (then re-run)."
fi

# ── 8. Enable + start (only if API check passed) ─────────────────────────────
systemctl enable binbot-watchdog.timer >/dev/null 2>&1 || true
if [ "$DO_START" -eq 1 ] && [ "$API_OK" -eq 1 ]; then
  say "Enabling + starting $SERVICE and watchdog ..."
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"
  systemctl start binbot-watchdog.timer
  sleep 3
  if systemctl is-active --quiet "$SERVICE"; then
    ok "$SERVICE is ACTIVE — BinBot is live."
    echo "  Logs: journalctl -u $SERVICE -f"
  else
    warn "$SERVICE did not stay active — check: journalctl -u $SERVICE -n 50"
  fi
else
  warn "NOT starting the bot."
  [ "$API_OK" -ne 1 ] && echo "  Reason: Binance API check failed (whitelist IP ${PUBLIC_IP} first)."
  [ "$DO_START" -ne 1 ] && echo "  Reason: --no-start was passed."
  echo "  When ready:  sudo systemctl enable --now $SERVICE && sudo systemctl start binbot-watchdog.timer"
fi

say "Done."
