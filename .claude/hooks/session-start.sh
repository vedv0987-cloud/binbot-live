#!/bin/bash
# SessionStart hook — prepares a Claude Code on the web session for BinBot so the
# test gate (python -m unittest test_core) runs without manual `pip install`, and
# the session starts aware of the latest shared `main`. Runs for BOTH Claudes via
# the committed .claude/settings.json, but only does work in remote (web) sessions.
#
# It is READ-ONLY toward git: it fetches but never resets/pulls, so it can't clobber
# work. It NEVER touches the live VM (the bot runs under systemd, not a Claude session).
set -euo pipefail

# Only set up dependencies in Claude Code on the web (remote) containers.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# Keep the session aware of the latest shared main (read-only — does not reset).
git fetch -q origin main 2>/dev/null || true

# Install Python deps so the gate runs. PEP 668 (externally-managed) fallback,
# same approach as bootstrap.sh. Idempotent: pip skips already-satisfied pkgs.
PYBIN="$(command -v python3 || command -v python || true)"
if [ -n "$PYBIN" ] && [ -f requirements.txt ]; then
  if ! "$PYBIN" -m pip install -q -r requirements.txt 2>/tmp/binbot_pip_err; then
    if grep -qi 'externally-managed' /tmp/binbot_pip_err; then
      "$PYBIN" -m pip install -q --break-system-packages -r requirements.txt || true
    else
      cat /tmp/binbot_pip_err >&2 || true
    fi
  fi
fi
