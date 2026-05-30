"""
BinBot Feature Flags v1.0
=========================
Central flag store for tier-gated features.
Written by upgrade_engine.py — never edit manually.
Read by bot modules on every startup + hot-reload every 60s.
"""

import json, time
from pathlib import Path

_FLAGS_FILE = Path(__file__).parent / ".feature_flags.json"
_cache: dict = {}
_cache_ts: float = 0.0
_TTL: float = 10.0   # v14.5.1 FIX (audit #27): reduced from 60s to 10s for faster emergency flag changes

DEFAULTS: dict = {
    # ── $200 Tier ──────────────────────────────────────
    "tier_200_active":  False,
    "atr_labels":       False,   # ATR-relative trade labels in ML
    "risk_pct":         None,    # None = use config.py value; float overrides
    # ── $500 Tier ──────────────────────────────────────
    "tier_500_active":  False,
    "batched_klines":   False,   # 2000-candle batched fetch
    "rl_per_position":  False,   # RL state tracked per open position
    "regime_v2":        False,   # 8-state regime (was 6)
    # ── $1000 Tier ─────────────────────────────────────
    "tier_1000_active": False,
    "walk_forward_opt": False,   # walk-forward parameter optimization
    "async_ws_exits":   False,   # async WebSocket SL/TP execution
}


def _load_disk() -> dict:
    global _cache, _cache_ts
    try:
        d = json.loads(_FLAGS_FILE.read_text())
        _cache = {**DEFAULTS, **d}
    except FileNotFoundError:
        _cache = dict(DEFAULTS)
    except Exception:
        pass   # keep old cache on parse error
    _cache_ts = time.time()
    return _cache


def flags() -> dict:
    """Return current flags — cached, refreshed every 10s."""
    if time.time() - _cache_ts > _TTL:
        _load_disk()
    return _cache


def get(key: str, default=None):
    """Get a single flag value."""
    return flags().get(key, DEFAULTS.get(key, default))


def set_flags(updates: dict):
    """Write flag updates to disk (called by upgrade_engine only)."""
    current = _load_disk()
    current.update(updates)
    tmp = _FLAGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2))
    tmp.replace(_FLAGS_FILE)
    global _cache, _cache_ts
    _cache = current
    _cache_ts = time.time()


def is_tier(n: int) -> bool:
    """True if tier N or higher is active."""
    return get(f"tier_{n}_active", False)


# ── Initialise cache on import ────────────────────────────────────────────────
_load_disk()
