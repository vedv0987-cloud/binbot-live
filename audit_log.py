"""BinBot v15.0 — Audit Log (hash-chain, tamper-evident)

Every trade-decision event is appended to audit.jsonl with a SHA-256 hash
chain. Each entry's hash includes the previous entry's hash, so any tampering
with historical entries breaks the chain at the point of modification.

Use cases:
  - Regulatory audit (proves trade decisions weren't backdated)
  - Disaster recovery (deterministic replay of what bot saw + decided)
  - Forensic analysis when behavior is unexpected

Usage:
    from audit_log import AuditLog
    audit = AuditLog()
    audit.log("ENTRY", pair="BTCUSDT", price=50000, strategy="SMC_OB", conf=0.85)
    audit.log("EXIT",  pair="BTCUSDT", price=51000, pnl=10.0, reason="TP")

    # Verify integrity:
    ok, broken_at = audit.verify_chain()
    if not ok:
        print(f"AUDIT CHAIN BROKEN at line {broken_at}")
"""
from __future__ import annotations
import json, hashlib, os, logging, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple, Optional

log = logging.getLogger("binbot")


class AuditLog:
    def __init__(self, path: str = "audit.jsonl"):
        self._path = Path(os.path.dirname(os.path.abspath(__file__))) / path
        self._prev_hash = self._tail_hash()

    def _tail_hash(self) -> str:
        """Read last line's hash to chain from. Empty string if file new."""
        if not self._path.exists() or self._path.stat().st_size == 0:
            return "GENESIS"
        try:
            with open(self._path, "rb") as f:
                # Read last 4KB for efficiency
                f.seek(max(0, self._path.stat().st_size - 4096))
                tail = f.read().decode("utf-8", errors="ignore")
            last_line = tail.strip().split("\n")[-1]
            if not last_line:
                return "GENESIS"
            entry = json.loads(last_line)
            return entry.get("hash", "GENESIS")
        except Exception:
            return "GENESIS"

    def _compute_hash(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def log(self, event: str, **kwargs):
        """Append an event with hash chained to previous entry.
        event: short string ("ENTRY", "EXIT", "SL_MOVE", "BLOCK", "PAUSE", etc.)
        kwargs: any JSON-serializable fields."""
        try:
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ts_unix": time.time(),
                "event": event,
                "prev_hash": self._prev_hash,
                **kwargs,
            }
            payload["hash"] = self._compute_hash(payload)
            with open(self._path, "a") as f:
                f.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._prev_hash = payload["hash"]
        except Exception as e:
            log.warning(f"Audit log failed for {event}: {e}")

    def verify_chain(self) -> Tuple[bool, Optional[int]]:
        """Walk every entry, re-compute hash, compare against stored hash and
        previous-link. Returns (True, None) if intact, (False, line_no) otherwise."""
        if not self._path.exists():
            return True, None
        prev = "GENESIS"
        try:
            with open(self._path) as f:
                for i, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    claimed_hash = entry.pop("hash", "")
                    if entry.get("prev_hash") != prev:
                        return False, i
                    if self._compute_hash(entry) != claimed_hash:
                        return False, i
                    prev = claimed_hash
            return True, None
        except Exception as e:
            log.warning(f"Audit verify error: {e}")
            return False, None
