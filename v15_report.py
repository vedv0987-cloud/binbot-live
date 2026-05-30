#!/usr/bin/env python3
"""BinBot v15.0 — Weekly Performance Report

Run manually or via cron to see TCA + advanced risk metrics over last N days.

Usage:
    python3 v15_report.py            # last 7 days
    python3 v15_report.py 14         # last 14 days
    python3 v15_report.py 30 verbose # last 30 days with per-trade detail
"""
import sys, os, json
from pathlib import Path

# Ensure local modules importable when run from any cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tca import TCALogger
import risk_metrics

CLOSE_ACTIONS = {"TP", "SL", "TIME", "TIME_MAX", "TRAIL", "REGIME",
                 "GHOST", "CRASH", "CRASH_STUCK", "DUST", "FORCE_CLOSE", "SCALE"}


def load_returns(days: int):
    """Load per-trade PnL% from trades_v9.jsonl for the last N days."""
    path = Path(os.path.dirname(os.path.abspath(__file__))) / "trades_v9.jsonl"
    if not path.exists():
        return [], []
    import time
    from datetime import datetime
    cutoff = time.time() - days * 86400
    returns = []
    timestamps = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("action") not in CLOSE_ACTIONS:
                continue
            ts = t.get("ts", "")
            try:
                tstamp = datetime.fromisoformat(ts).timestamp()
            except Exception:
                continue
            if tstamp < cutoff:
                continue
            pnl_pct = t.get("pnl_pct")
            if pnl_pct is None:
                # Compute from pnl_usd and size if pct not available
                pnl_usd = t.get("pnl", 0)
                size = t.get("size", 0)
                pnl_pct = (pnl_usd / size * 100) if size > 0 else 0
            returns.append(float(pnl_pct))
            timestamps.append(tstamp)
    return returns, timestamps


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    verbose = "verbose" in sys.argv

    print("\n" + "=" * 80)
    print(f"  BINBOT v15.0 PERFORMANCE REPORT — Last {days} days")
    print("=" * 80)

    # Risk metrics from trades_v9.jsonl
    returns, timestamps = load_returns(days)
    if not returns:
        print("\n  No closed trades in this window.\n")
    else:
        print(f"\n  Closed trades : {len(returns)}")
        metrics = risk_metrics.full_report(returns, timestamps)
        for k, v in metrics.items():
            print(f"  {k:<24} {v}")

        # Edge assessment
        sharpe = metrics["sharpe"]
        sortino = metrics["sortino"]
        calmar = metrics["calmar"]
        print()
        if sortino >= 2.0:
            print("  ✅ Sortino ≥ 2.0 — strong downside-adjusted edge")
        elif sortino >= 1.0:
            print("  🟡 Sortino 1-2 — marginal edge")
        else:
            print("  🔴 Sortino < 1 — no statistically meaningful edge yet")

        if calmar >= 3.0:
            print("  ✅ Calmar ≥ 3 — strong return-vs-drawdown ratio")
        elif calmar >= 1.0:
            print("  🟡 Calmar 1-3 — acceptable")
        else:
            print("  🔴 Calmar < 1 — drawdown too large relative to return")

    # TCA per-strategy report
    print()
    tca = TCALogger()
    tca.print_report(days)

    # v15.0: Audit chain integrity check (if audit.jsonl exists)
    try:
        from audit_log import AuditLog
        audit = AuditLog()
        ok, broken = audit.verify_chain()
        print("=" * 80)
        if ok:
            print("  ✅ AUDIT CHAIN INTACT — no tampering detected")
        else:
            print(f"  🚨 AUDIT CHAIN BROKEN at line {broken} — possible tampering or corruption")
        print("=" * 80 + "\n")
    except Exception as _e:
        print(f"  (audit chain check skipped: {_e})\n")

    # v15.0: Regime-aware backtest (only if "regime" arg passed — expensive)
    if "regime" in sys.argv:
        try:
            from config import Config
            from exchange import Exchange
            from indicators import TA
            from regime_backtest import RegimeAwareBacktester
            print("Running regime-aware backtest (this takes ~30s per pair)...\n")
            cfg = Config()
            ex = Exchange(cfg)
            rbt = RegimeAwareBacktester(ex, TA, cfg)
            matrix = rbt.run("BTCUSDT", days=min(days, 30))
            rbt.print_matrix(matrix)
        except Exception as _e:
            print(f"  Regime backtest failed: {_e}\n")


if __name__ == "__main__":
    main()
