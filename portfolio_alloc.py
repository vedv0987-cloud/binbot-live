"""BinBot v15.1 — Portfolio-Level Capital Allocation

Three institutional allocation models, all spot-friendly:

  1. PortfolioKelly  — allocate capital across strategies in proportion to
                       each strategy's individual Kelly fraction × edge confidence
  2. ERCSizing       — Equal Risk Contribution: each open position contributes
                       the same % of total portfolio risk
  3. MVOPairSelector — Mean-Variance Optimization: pick today's best non-
                       correlated pair set from candidate universe

All three use historical data from trades_v9.jsonl + tca.jsonl.

USAGE:
    from portfolio_alloc import PortfolioKelly, ERCSizing, MVOPairSelector
    pk = PortfolioKelly()
    weights = pk.compute(strategies=["SMC_OB","TREND","VWAP"])
    # weights = {"SMC_OB": 0.45, "TREND": 0.30, "VWAP": 0.25}

    erc = ERCSizing()
    sizes = erc.compute(open_positions=[...], total_capital=50.0)
    # sizes = {"BTCUSDT": 0.50, "ETHUSDT": 0.50}  if 2 positions

    mvo = MVOPairSelector(exchange)
    best_pairs = mvo.pick(candidates=["BTCUSDT","ETHUSDT","SOLUSDT",...], k=5)
"""
from __future__ import annotations
import logging, json, os, math
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict

log = logging.getLogger("binbot")

CLOSE_ACTIONS = {"TP","SL","TIME","TIME_MAX","TRAIL","REGIME","SCALE",
                 "GHOST","CRASH","CRASH_STUCK","DUST","FORCE_CLOSE"}


def _load_trades(path: str = "trades_v9.jsonl") -> List[Dict]:
    p = Path(os.path.dirname(os.path.abspath(__file__))) / path
    if not p.exists(): return []
    out = []
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: out.append(json.loads(line))
                except Exception: pass
    except Exception: pass
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Portfolio Kelly — allocate across STRATEGIES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PortfolioKelly:
    """Quarter-Kelly allocation across strategies based on historical edge."""

    def __init__(self, kelly_fraction: float = 0.25, min_trades: int = 5):
        self.kelly_fraction = kelly_fraction  # quarter-Kelly safety
        self.min_trades = min_trades

    def regime_mult(self, regime: str) -> float:
        """v15.16: Dynamic Kelly multiplier by regime quality.
        Strong trend = full Kelly. Chop = fractional Kelly to preserve capital."""
        return {'TREND_UP': 1.0, 'RANGE': 0.50, 'CHOPPY': 0.25}.get(
            str(regime).upper(), 0.50)

    def compute(self, strategies: Optional[List[str]] = None,
                days: int = 30) -> Dict[str, float]:
        """Returns weights summing to 1.0. Strategies without enough data get 0."""
        import time as _t
        cutoff = _t.time() - days * 86400
        trades = _load_trades()
        per_strat = defaultdict(lambda: {"wins": 0, "losses": 0,
                                          "avg_win_pct": 0.0, "avg_loss_pct": 0.0})
        for t in trades:
            if t.get("action") not in CLOSE_ACTIONS: continue
            ts = t.get("ts", "")
            try:
                tstamp = datetime.fromisoformat(ts).timestamp()
                if tstamp < cutoff: continue
            except Exception: continue
            s = t.get("strategy", "?")
            pnl_pct = t.get("pnl_pct", 0)
            if pnl_pct > 0:
                per_strat[s]["wins"] += 1
                # running mean
                n = per_strat[s]["wins"]
                per_strat[s]["avg_win_pct"] = ((per_strat[s]["avg_win_pct"] * (n - 1)) + pnl_pct) / n
            elif pnl_pct < 0:
                per_strat[s]["losses"] += 1
                n = per_strat[s]["losses"]
                per_strat[s]["avg_loss_pct"] = ((per_strat[s]["avg_loss_pct"] * (n - 1)) + abs(pnl_pct)) / n

        # Kelly per strategy: f = W - (1-W)/R
        kellys = {}
        for s, d in per_strat.items():
            n = d["wins"] + d["losses"]
            if n < self.min_trades: continue
            if strategies and s not in strategies: continue
            w = d["wins"] / n
            r = d["avg_win_pct"] / d["avg_loss_pct"] if d["avg_loss_pct"] > 0 else 0
            if r <= 0: continue
            k = w - (1 - w) / r
            k = max(0, k)  # negative Kelly → don't allocate
            kellys[s] = k * self.kelly_fraction

        # Normalize to sum=1 (or all zero if no strategy has edge)
        total = sum(kellys.values())
        if total <= 0:
            log.info("PortfolioKelly: no strategies with positive edge yet")
            return {}
        weights = {s: round(k / total, 4) for s, k in kellys.items()}
        log.info(f"📊 Portfolio Kelly weights: {weights}")
        return weights


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Equal Risk Contribution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ERCSizing:
    """Each position should contribute equal % to total portfolio risk.
    Risk = position size × (entry - SL distance %).

    For 2 open positions A (5% SL distance) and B (3% SL distance) with total
    capital $50: solve so size_A × 0.05 = size_B × 0.03 → size_B = size_A × 5/3.
    Then scale to fit (size_A + size_B) ≤ total_capital × max_exposure."""

    def compute(self, positions: List[Dict], total_capital: float,
                max_exposure: float = 0.75) -> Dict[str, float]:
        """positions: list of dicts with at least {pair, entry, sl}.
        Returns {pair: target_size_usd}."""
        if not positions: return {}
        # Compute SL distance % for each
        risks = {}
        for p in positions:
            entry = p.get("entry") or p.get("avg_entry")
            sl = p.get("sl")
            pair = p.get("pair")
            if not all([entry, sl, pair]) or entry <= 0: continue
            sl_pct = abs(entry - sl) / entry
            if sl_pct <= 0: continue
            risks[pair] = sl_pct
        if not risks: return {}
        # Inverse-risk weighting: more risk → smaller size
        inv_risks = {p: 1.0 / r for p, r in risks.items()}
        total_inv = sum(inv_risks.values())
        if total_inv <= 0: return {}
        budget = total_capital * max_exposure
        sizes = {p: round(budget * (iv / total_inv), 2) for p, iv in inv_risks.items()}
        log.info(f"⚖️ ERC sizes: {sizes}")
        return sizes


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mean-Variance Optimization — pair selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MVOPairSelector:
    """Pick the K best non-correlated pairs from a candidate universe.

    Maximizes expected return / minimizes correlation. Uses 24h percent-change
    as proxy for expected return (cheap), historical correlation of recent
    candles for the constraint.

    Greedy selection: take highest-return pair, then iteratively add next
    candidate that has lowest avg correlation with already-selected set."""

    def __init__(self, exchange, corr_lookback_candles: int = 100):
        self.ex = exchange
        self.lookback = corr_lookback_candles

    def pick(self, candidates: List[str], k: int = 5,
             min_return_pct: float = 0.5) -> List[str]:
        """candidates: list of symbol strings like 'BTCUSDT'.
        Returns top K non-correlated pairs."""
        if not candidates or k <= 0: return []
        # Fetch 24h tickers for expected-return proxy
        ticker_data = {}
        try:
            # v15.2: self.ex.cl is set by NativeSLManager (sync python-binance client)
            # Falls back to empty list if async Exchange has no sync client attached
            _cl = getattr(self.ex, 'cl', None)
            if _cl is None:
                log.debug("MVO: no sync client available — skipping MVO")
                return candidates[:k]
            for t in _cl.get_ticker():
                if t["symbol"] in candidates:
                    ticker_data[t["symbol"]] = {
                        "change_pct": float(t.get("priceChangePercent", 0)),
                        "vol_usd": float(t.get("quoteVolume", 0)),
                    }
        except Exception as e:
            log.warning(f"MVO ticker fetch failed: {e}")
            return candidates[:k]  # fallback
        # Filter by min expected return + liquidity
        ranked = sorted(
            [(s, d["change_pct"]) for s, d in ticker_data.items()
             if d["vol_usd"] > 1_000_000 and abs(d["change_pct"]) >= min_return_pct],
            key=lambda x: -x[1]
        )
        if not ranked: return candidates[:k]
        # Greedy: start with highest-return, add next with lowest avg correlation
        selected = [ranked[0][0]]
        candle_cache = {selected[0]: self._fetch_closes(selected[0])}
        for sym, _ in ranked[1:]:
            if len(selected) >= k: break
            closes = self._fetch_closes(sym)
            if not closes: continue
            candle_cache[sym] = closes
            # Compute avg correlation with selected set
            corrs = []
            for s in selected:
                c = self._correlation(closes, candle_cache.get(s, []))
                if c is not None: corrs.append(abs(c))
            avg_corr = sum(corrs) / len(corrs) if corrs else 0
            if avg_corr < 0.80:  # threshold: skip highly correlated
                selected.append(sym)
        log.info(f"📐 MVO selected {len(selected)} pairs: {selected}")
        return selected

    def _fetch_closes(self, symbol: str) -> List[float]:
        try:
            # v15.2: ex.klines is async now; use sync cl if available
            _cl = getattr(self.ex, 'cl', None)
            if _cl is None:
                return []
            klines = _cl.get_klines(symbol=symbol, interval="1h", limit=self.lookback)
            return [float(k[4]) for k in klines]  # k[4] = close price
        except Exception: return []

    def _correlation(self, a: List[float], b: List[float]) -> Optional[float]:
        n = min(len(a), len(b))
        if n < 20: return None
        a = a[-n:]; b = b[-n:]
        ra = [(a[i] - a[i-1]) / a[i-1] for i in range(1, n) if a[i-1] > 0]
        rb = [(b[i] - b[i-1]) / b[i-1] for i in range(1, n) if b[i-1] > 0]
        n2 = min(len(ra), len(rb))
        if n2 < 10: return None
        ra = ra[-n2:]; rb = rb[-n2:]
        ma = sum(ra) / n2; mb = sum(rb) / n2
        cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n2)) / n2
        sa = math.sqrt(sum((x - ma) ** 2 for x in ra) / n2)
        sb = math.sqrt(sum((x - mb) ** 2 for x in rb) / n2)
        if sa * sb <= 0: return None
        return cov / (sa * sb)

class ExposureGuard:
    """Active portfolio exposure monitor."""
    def __init__(self, cfg):
        self.cfg = cfg  # v18.7.4: keep ref so MAX_EXPOSURE is read live (capital-tier switcher rewrites it)
        self.max_crypto_pct = getattr(cfg, "MAX_EXPOSURE", 0.75)
        self.warn_pct = self.max_crypto_pct * 0.85
        self._last_warn_ts = 0

    def check(self, usdt_free, positions_value, total_capital):
        # v18.7.4: re-read MAX_EXPOSURE every call so the auto capital-tier switcher takes
        # immediate effect (it was snapshotted at __init__ before, so tier changes were ignored).
        self.max_crypto_pct = getattr(self.cfg, "MAX_EXPOSURE", self.max_crypto_pct)
        self.warn_pct = self.max_crypto_pct * 0.85
        crypto_pct = positions_value / total_capital if total_capital > 0 else 0
        if crypto_pct >= self.max_crypto_pct:
            return False, crypto_pct, "BLOCKED"
        if crypto_pct >= self.warn_pct:
            return True, crypto_pct, "WARNING"
        return True, crypto_pct, "OK"
