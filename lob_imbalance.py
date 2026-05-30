"""BinBot v14.2 — lob_imbalance.py — Module 2: LOB Imbalance Tracker
Institutional-grade Order Book Imbalance (OBI) analysis.

Weighted multi-depth order book pressure:
- OBI > +0.40 = strong buy wall  → confidence boost
- OBI < -0.40 = strong sell wall → block entry
- Micro-price deviation shows true fair value vs mid-price

Called per-pair during signal validation (not global scan).
Uses REST /api/v3/depth — no additional API keys required.
"""
import json, time, logging, urllib.request
import numpy as np
from numba import jit

log = logging.getLogger('binbot')

@jit(nopython=True)
def _fast_obi_math(bids_qty, asks_qty, n):
    w_bids = 0.0
    w_asks = 0.0
    for i in range(n):
        w_bids += bids_qty[i] * (n - i)
        w_asks += asks_qty[i] * (n - i)
    denom = w_bids + w_asks
    obi = (w_bids - w_asks) / denom if denom > 0 else 0.0
    return w_bids, w_asks, obi


class LOBImbalanceTracker:
    """Tracks weighted order book imbalance per trading pair.

    OBI = (weighted_bids - weighted_asks) / (weighted_bids + weighted_asks)
    Range: -1.0 (all sell pressure) to +1.0 (all buy pressure)

    Decay weights across 20 tiers:
    Top of book (tier 1) = weight 20x (most important)
    Tier 20 = weight 1x (least important, far from spread)
    """

    def __init__(self):
        self._data  = {}        # {symbol: {obi, micro_price, spread_pct, ts}}
        self._cache = 30        # Refresh every 30s (matches scan cycle)

    def update(self, symbol: str):
        """Fetch and compute OBI for a specific symbol.
        v15.0 #2: depth raised from 20→100 levels, decay weighting recalibrated."""
        cached = self._data.get(symbol, {})
        if time.time() - cached.get('ts', 0) < self._cache:
            return
        try:
            url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=100"  # v15.0 #2: 100 levels (was 20) — deeper liquidity picture
            req = urllib.request.Request(url, headers={"User-Agent": "BinBot/14"})
            resp = urllib.request.urlopen(req, timeout=5)
            book = json.loads(resp.read().decode())

            bids = [[float(p), float(q)] for p, q in book.get('bids', [])]
            asks = [[float(p), float(q)] for p, q in book.get('asks', [])]
            if not bids or not asks:
                return

            # Decay-weighted depth across top 100 tiers (v15.0 #2: was 20)
            n = min(100, len(bids), len(asks))
            b_q = np.array([b[1] for b in bids[:n]], dtype=np.float64)
            a_q = np.array([a[1] for a in asks[:n]], dtype=np.float64)
            w_bids, w_asks, obi = _fast_obi_math(b_q, a_q, n)

            # Micro-price: weighted mid-price by opposing liquidity
            bbq, baq = bids[0][1], asks[0][1]
            bbp, bap = bids[0][0], asks[0][0]
            micro  = (bbp * baq + bap * bbq) / (bbq + baq) if (bbq + baq) > 0 else (bbp + bap) / 2
            spread = (bap - bbp) / bbp * 100 if bbp > 0 else 0.0

            self._data[symbol] = {
                'obi':         round(obi, 4),
                'micro_price': round(micro, 6),
                'spread_pct':  round(spread, 4),
                'bid_depth':   round(w_bids, 2),
                'ask_depth':   round(w_asks, 2),
                'ts':          time.time()
            }
        except Exception as e:
            log.debug(f"LOB {symbol}: {e}")

    def get_obi(self, symbol: str) -> float:
        return self._data.get(symbol, {}).get('obi', 0.0)

    def get_boost(self, symbol: str) -> float:
        """Confidence multiplier from order book imbalance."""
        obi = self.get_obi(symbol)
        if obi > 0.55: return 1.12   # strong buy wall
        if obi > 0.40: return 1.06   # moderate buy pressure
        if obi > 0.20: return 1.02   # slight buy lean
        if obi < -0.55: return 0.78  # strong sell wall
        if obi < -0.40: return 0.88  # moderate sell pressure
        if obi < -0.20: return 0.95  # slight sell lean
        return 1.0

    def should_block(self, symbol: str) -> bool:
        """Block if strongly skewed to sell side AND spread is wide (illiquid)."""
        d = self._data.get(symbol, {})
        return d.get('obi', 0.0) < -0.50 and d.get('spread_pct', 0) > 0.05

    def status(self, symbol: str) -> str:
        return f"OBI:{self.get_obi(symbol):+.2f}"

# v18 Fix: Warm up Numba JIT compiler
try:
    _fast_obi_math(np.zeros(10), np.zeros(10), 10)
except Exception:
    pass
