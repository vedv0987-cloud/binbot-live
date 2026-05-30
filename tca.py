"""BinBot v15.0 — Transaction Cost Analysis (TCA)

Measures actual fill quality vs theoretical optimal per trade.
Logs to tca.jsonl and provides weekly per-strategy attribution.

Key metrics tracked:
  - slip_bps        : (fill_price - signal_price) / signal_price × 10000
                      Positive = paid more than signal (bad on buys, good on sells)
  - spread_paid_bps : effective spread cost as a function of bid-ask at entry time
  - fee_bps         : actual taker/maker fee paid (in basis points)
  - mae_bps         : maximum adverse excursion — worst drawdown during hold
  - mfe_bps         : maximum favorable excursion — best peak during hold
  - r_multiple      : realized PnL as multiples of intended risk (1R = original SL distance)

Used for:
  1. Weekly per-strategy report: which strategies have realistic fills?
  2. Auto-kill candidates: strategies with consistent negative slip
  3. Maker-fill rate tracking: are LIMIT_MAKER orders actually filling as maker?
"""
from __future__ import annotations
import json, time, logging, os
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from typing import Optional

log = logging.getLogger("binbot")

TCA_LOG = "tca.jsonl"
TCA_MAX_BYTES = 5 * 1024 * 1024  # 5MB rotation


def _rotate(path: Path):
    """Single-generation rotation when file exceeds TCA_MAX_BYTES."""
    try:
        if path.exists() and path.stat().st_size >= TCA_MAX_BYTES:
            bak = path.with_suffix(path.suffix + ".1")
            if bak.exists():
                bak.unlink()
            path.replace(bak)
    except Exception:
        pass


class TCALogger:
    """Logs transaction-cost-analysis data per trade lifecycle."""

    def __init__(self, log_file: str = TCA_LOG):
        self._path = Path(os.path.dirname(os.path.abspath(__file__))) / log_file

    def record_entry(self, pos, signal_price: float, order_type: str = "MARKET",
                     spread_bid: float = 0, spread_ask: float = 0):
        """Call from risk.open_pos after position is registered. Captures the
        execution side of the entry. signal_price is what the strategy decided;
        pos.entry is what actually filled."""
        try:
            fill_price = pos.avg_entry or pos.entry
            slip_bps = ((fill_price - signal_price) / signal_price * 10000) if signal_price > 0 else 0
            mid = (spread_bid + spread_ask) / 2 if (spread_bid > 0 and spread_ask > 0) else 0
            half_spread_bps = ((spread_ask - spread_bid) / 2 / mid * 10000) if mid > 0 else 0
            fee_bps = (pos.entry_fee / (pos.qty * fill_price) * 10000) if (pos.qty * fill_price) > 0 else 10
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "ENTRY",
                "pair": pos.pair,
                "strategy": pos.strategy,
                "grade": pos.grade,
                "signal_price": round(signal_price, 8),
                "fill_price": round(fill_price, 8),
                "slip_bps": round(slip_bps, 2),
                "half_spread_bps": round(half_spread_bps, 2),
                "fee_bps": round(fee_bps, 2),
                "order_type": order_type,
                "size_usd": round(pos.size, 4),
                "qty": pos.qty,
            }
            self._append(entry)
        except Exception as e:
            log.debug(f"TCA record_entry failed: {e}")

    def record_exit(self, pos, exit_price: float, reason: str, pnl: float,
                    high_seen: Optional[float] = None, low_seen: Optional[float] = None):
        """Call from risk._record_close after a position closes. Captures the
        full trade lifecycle including MAE/MFE and R-multiple."""
        try:
            entry_p = pos.avg_entry or pos.entry
            if entry_p <= 0 or pos.qty <= 0:
                return
            # R-multiple: realized PnL / initial risk
            # Initial risk = entry × original_sl_distance × qty
            # pos.sl has been ratcheted up; use the entry context if available.
            # Fallback: estimate original risk from atr × 3 (matches strategy sizing).
            init_sl_pct = (pos.atr * 3 / entry_p) if pos.atr > 0 else 0.03
            initial_risk_usd = entry_p * init_sl_pct * pos.qty
            r_multiple = (pnl / initial_risk_usd) if initial_risk_usd > 0 else 0
            # MAE/MFE
            high_seen = high_seen if high_seen is not None else max(getattr(pos, "high", entry_p), exit_price)
            low_seen = low_seen if low_seen is not None else min(entry_p, exit_price)
            mfe_bps = ((high_seen - entry_p) / entry_p * 10000) if entry_p > 0 else 0
            mae_bps = ((low_seen - entry_p) / entry_p * 10000) if entry_p > 0 else 0
            # Exit slip vs intended exit (TP or SL)
            target = pos.tp if pnl > 0 else pos.sl
            exit_slip_bps = ((exit_price - target) / target * 10000) if target > 0 else 0
            # Hold time
            try:
                entry_dt = datetime.fromisoformat(pos.entry_time)
                hold_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
            except Exception:
                hold_min = 0
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "EXIT",
                "pair": pos.pair,
                "strategy": pos.strategy,
                "reason": reason,
                "entry_price": round(entry_p, 8),
                "exit_price": round(exit_price, 8),
                "intended_target": round(target, 8),
                "exit_slip_bps": round(exit_slip_bps, 2),
                "pnl_usd": round(pnl, 4),
                "r_multiple": round(r_multiple, 3),
                "mfe_bps": round(mfe_bps, 2),
                "mae_bps": round(mae_bps, 2),
                "hold_min": round(hold_min, 1),
                "tp_floor_locked": getattr(pos, "tp_floor_locked", False),
                "be_locked": getattr(pos, "be_locked", False),
            }
            self._append(entry)
        except Exception as e:
            log.debug(f"TCA record_exit failed: {e}")

    def _append(self, entry: dict):
        _rotate(self._path)
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.warning(f"TCA write failed: {e}")

    # ─── Reporting ────────────────────────────────────────────────────────────

    def per_strategy_report(self, days: int = 7) -> dict:
        """Aggregate TCA stats per strategy over the last N days.
        Returns: {strategy: {trades, avg_slip_bps, avg_fee_bps, avg_r_mult,
                              avg_mfe_bps, avg_mae_bps, win_rate, ...}}"""
        cutoff = time.time() - days * 86400
        events = []
        try:
            if self._path.exists():
                with open(self._path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                            ts = e.get("ts", "")
                            t = datetime.fromisoformat(ts).timestamp() if ts else 0
                            if t >= cutoff:
                                events.append(e)
                        except Exception:
                            pass
        except Exception:
            pass

        by_strat = defaultdict(lambda: {
            "entries": [], "exits": [], "wins": 0, "losses": 0
        })
        for e in events:
            s = e.get("strategy", "?")
            if e.get("event") == "ENTRY":
                by_strat[s]["entries"].append(e)
            elif e.get("event") == "EXIT":
                by_strat[s]["exits"].append(e)
                if e.get("pnl_usd", 0) > 0:
                    by_strat[s]["wins"] += 1
                else:
                    by_strat[s]["losses"] += 1

        report = {}
        for s, data in by_strat.items():
            n_exits = len(data["exits"])
            if n_exits == 0:
                continue
            avg_slip = sum(x.get("slip_bps", 0) for x in data["entries"]) / max(len(data["entries"]), 1)
            avg_fee = sum(x.get("fee_bps", 0) for x in data["entries"]) / max(len(data["entries"]), 1)
            avg_r = sum(x.get("r_multiple", 0) for x in data["exits"]) / n_exits
            avg_mfe = sum(x.get("mfe_bps", 0) for x in data["exits"]) / n_exits
            avg_mae = sum(x.get("mae_bps", 0) for x in data["exits"]) / n_exits
            wr = data["wins"] / max(n_exits, 1) * 100
            total_pnl = sum(x.get("pnl_usd", 0) for x in data["exits"])
            report[s] = {
                "trades": n_exits,
                "win_rate": round(wr, 1),
                "total_pnl_usd": round(total_pnl, 4),
                "avg_entry_slip_bps": round(avg_slip, 2),
                "avg_fee_bps": round(avg_fee, 2),
                "avg_r_multiple": round(avg_r, 3),
                "avg_mfe_bps": round(avg_mfe, 2),
                "avg_mae_bps": round(avg_mae, 2),
                "edge_assessment": (
                    "POSITIVE" if avg_r > 0.25 else
                    "MARGINAL" if avg_r > 0 else
                    "NEGATIVE"
                ),
            }
        return report

    def print_report(self, days: int = 7):
        """Pretty-print the per-strategy TCA report. Used by review tools."""
        r = self.per_strategy_report(days)
        if not r:
            print(f"No TCA data in last {days} days.")
            return
        print(f"\n{'=' * 80}")
        print(f"  TCA REPORT — Last {days} days")
        print(f"{'=' * 80}")
        for s, m in sorted(r.items(), key=lambda x: -x[1]["total_pnl_usd"]):
            print(f"  {s:<22} {m['trades']:>3}t  WR:{m['win_rate']:>5.1f}%  "
                  f"PnL:${m['total_pnl_usd']:>+8.4f}  R:{m['avg_r_multiple']:>+5.2f}  "
                  f"slip:{m['avg_entry_slip_bps']:>+5.1f}bps  fee:{m['avg_fee_bps']:>4.1f}bps  "
                  f"[{m['edge_assessment']}]")
        print(f"{'=' * 80}\n")
