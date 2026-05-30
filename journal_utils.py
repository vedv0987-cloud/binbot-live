# BinBot v18.8.5 — journal_utils.py
"""
Size-bounded JSONL append helper.

slip_telemetry.jsonl, stuck_coins.jsonl and the main trade log (trades_v9.jsonl)
are append-only JSONL files. Every consumer in the codebase reads these flat
files — TradeJournal._load (analytics.py), the drawdown-peak recompute, the
daily/weekly Telegram summaries, portfolio_alloc, v15_report, the Group-D
daily-loss fallback (risk.py), and the startup stuck-coin safety check (bot.py).

This module appends one JSON record per line and rotates the file when it crosses
MAX_BYTES. The old file becomes <name>.1 (single-generation backup, overwriting
any prior .1). Subsequent appends start fresh.

v18.8.5 FIX: reverted the v18 aiosqlite migration. That change redirected every
append into a write-only journal.db that NOTHING in the codebase ever read back,
while all readers kept reading the .jsonl files — silently freezing reporting,
restart-time drawdown-peak recovery, strategy-weight learning, and the stuck-coin
sweep guard. It also swallowed every write error (always returning success) and
ignored max_bytes, so the db grew unbounded. Restoring plain file-append +
rotation reconnects writers and readers and makes write failures observable.

Usage:
    from journal_utils import append_jsonl
    append_jsonl("slip_telemetry.jsonl", {"ts": "...", "pair": "BTCUSDT", ...})
"""
import os, json, logging

log = logging.getLogger("binbot")

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def append_jsonl(filename: str, record: dict, max_bytes: int = _DEFAULT_MAX_BYTES) -> bool:
    """Append `record` as one JSON line to `filename`, rotating to `<filename>.1`
    when the file reaches `max_bytes`. Returns True on success, False on failure
    (failures are logged so the caller/operator can see lost telemetry).

    The filename is used exactly as given so it matches the path every reader
    opens (readers use the same relative LOG_FILE string). Pass max_bytes=0 to
    disable rotation.
    """
    try:
        # Rotate BEFORE appending if the current file is already at/over budget.
        try:
            if max_bytes and os.path.exists(filename) and os.path.getsize(filename) >= max_bytes:
                os.replace(filename, filename + ".1")  # atomic single-generation backup
        except Exception as _re:
            log.debug(f"append_jsonl rotation skipped for {filename}: {_re}")
        with open(filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except Exception as e:
        log.warning(f"append_jsonl write to {filename} failed: {e}")
        return False
