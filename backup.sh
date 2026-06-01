#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# BinBot backup — snapshot the secrets + live state that are NOT in git, so a
# fresh VM can be restored with `bootstrap.sh --restore <dir>`.
#
# These files are gitignored on purpose (secrets + per-VM runtime state), so a
# `git clone` alone does NOT bring them back. Run this periodically (e.g. cron)
# and keep the tarball somewhere off the VM (Object Storage, scp to your laptop).
#
# Usage:
#   bash backup.sh                 # → ./binbot_backups/binbot_backup_<ts>.tgz
#   bash backup.sh /mnt/backups    # write into a chosen directory
#
# Restore on a new VM:
#   tar xzf binbot_backup_<ts>.tgz -C /tmp/restore
#   sudo bash bootstrap.sh --restore /tmp/restore
# ──────────────────────────────────────────────────────────────────────────
set -uo pipefail

SRC_DIR="${REPO_DIR:-$(cd "$(dirname "$0")" && pwd)}"
OUT_DIR="${1:-${SRC_DIR}/binbot_backups}"
TS="$(date +%Y-%m-%d_%H%M%S)"
OUT="${OUT_DIR}/binbot_backup_${TS}.tgz"

# Secrets + live state worth preserving across VMs (all gitignored).
FILES=(.env bot_state.json trade_history.json equity_curve.json coin_profiles.json
       kelly_history.json selfhealer_state.json .feature_flags.json heartbeat.txt
       watchdog_state.txt)

mkdir -p "$OUT_DIR"
present=()
for f in "${FILES[@]}"; do
  [ -e "${SRC_DIR}/${f}" ] && present+=("$f")
done
# Trained ML models / scalers, if any.
while IFS= read -r m; do present+=("$(basename "$m")"); done \
  < <(find "$SRC_DIR" -maxdepth 1 -type f \( -name '*.pkl' -o -name '*.joblib' \) 2>/dev/null)

if [ "${#present[@]}" -eq 0 ]; then
  echo "❌ Nothing to back up in $SRC_DIR (no .env or state files found)."; exit 1
fi

tar czf "$OUT" -C "$SRC_DIR" "${present[@]}" || { echo "❌ tar failed"; exit 1; }
chmod 600 "$OUT"
echo "✓ Backed up ${#present[@]} item(s) → $OUT"
printf '   %s\n' "${present[@]}"
echo "Keep this OFF the VM. Restore with: sudo bash bootstrap.sh --restore <unpacked-dir>"
