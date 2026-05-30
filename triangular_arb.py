"""BinBot v15.1 — Triangular Arbitrage Scanner

Spot-only triangular arb: cycle through 3 trading pairs to capture price
discrepancies. Example: USDT → BTC → ETH → USDT. If round-trip return > fees,
it's a profitable arb.

This is SPOT-ONLY safe — no shorts required. All 3 legs are spot buys/sells.

The math:
    1. Start with $100 USDT
    2. Buy BTC @ BTC/USDT ask → $100 / ask_btcusdt BTC, minus fee
    3. Sell BTC for ETH @ ETH/BTC bid → BTC × bid_ethbtc ETH, minus fee
    4. Sell ETH for USDT @ ETH/USDT bid → ETH × bid_ethusdt USDT, minus fee
    5. Profitable if final USDT > $100 × (1 + min_profit_threshold)

For a 0.1% fee × 3 legs = 0.3% round-trip cost.
Arb opportunity requires the 3 mid-prices to be misaligned by >0.3% AFTER spread.

Realistic on Binance spot: rare and tiny (<5bps after fees), but free money
when it appears. Triggers maybe 0-3 times per day per cycle.

USAGE:
    from triangular_arb import TriangularArb
    tri = TriangularArb(exchange, min_profit_bps=10)  # 10bps = 0.1% net
    opportunities = tri.scan()  # list of dicts with cycle + expected_profit
    for opp in opportunities:
        tri.execute(opp, size_usd=10)  # caller decides size
"""
from __future__ import annotations
import logging, time
from typing import List, Dict, Optional, Tuple

log = logging.getLogger("binbot")

# Common triangles on Binance spot
DEFAULT_TRIANGLES = [
    # (leg1_pair, leg2_pair, leg3_pair, base_to_alt_to_quote)
    ("BTCUSDT", "ETHBTC",  "ETHUSDT"),
    ("BTCUSDT", "BNBBTC",  "BNBUSDT"),
    ("BTCUSDT", "SOLBTC",  "SOLUSDT"),
    ("BTCUSDT", "XRPBTC",  "XRPUSDT"),
    ("BTCUSDT", "ADABTC",  "ADAUSDT"),
    ("ETHUSDT", "BNBETH",  "BNBUSDT"),
    ("ETHUSDT", "LINKETH", "LINKUSDT"),
    # USDC variants if listed
    ("BTCUSDT", "BTCUSDC", "USDCUSDT"),  # if direct USDC/USDT exists
]

TAKER_FEE = 0.001  # 0.1% per leg


class TriangularArb:
    """Detects + (optionally) executes spot triangular arbitrage cycles."""

    def __init__(self, exchange, min_profit_bps: float = 10.0,
                 triangles: Optional[List[Tuple[str, str, str]]] = None):
        self.ex = exchange
        self.min_profit_bps = min_profit_bps  # 10 = 0.1% min net profit
        self.triangles = triangles or DEFAULT_TRIANGLES
        self._last_scan = 0
        self._scan_cache_sec = 5  # don't re-scan within 5s

    def scan(self) -> List[Dict]:
        """Walks all triangles, returns those with net profit > min_profit_bps."""
        if time.time() - self._last_scan < self._scan_cache_sec:
            return []
        opportunities = []
        try:
            # v15.2: self.ex.cl is set by NativeSLManager (sync python-binance)
            _cl = getattr(self.ex, 'cl', None)
            if _cl is None:
                return []
            tickers = {t["symbol"]: float(t["price"]) for t in _cl.get_all_tickers()}
        except Exception as e:
            log.debug(f"TriArb tickers fetch failed: {e}")
            return []
        for tri in self.triangles:
            leg1, leg2, leg3 = tri
            if leg1 not in tickers or leg2 not in tickers or leg3 not in tickers:
                continue
            # Cycle: USDT -> leg1_base via leg1 (buy at ask, approximated by ticker)
            # -> leg2_base via leg2 (sell leg1_base, buy leg2_base)
            # -> back to USDT via leg3 (sell leg2_base for USDT)
            p1 = tickers[leg1]  # e.g. BTCUSDT price
            p2 = tickers[leg2]  # e.g. ETHBTC price
            p3 = tickers[leg3]  # e.g. ETHUSDT price
            if p1 <= 0 or p2 <= 0 or p3 <= 0:
                continue
            # Forward cycle:  USDT -> leg1_base (BTC) -> leg2_base (ETH) -> USDT
            # 1 USDT * (1/p1) * (1/p2) * p3  with 3 fee deductions
            # Wait — direction matters. For BTC/USDT, ETHBTC, ETHUSDT:
            # 1 USDT buys 1/p1 BTC (e.g. BTC at $50k → 0.00002 BTC)
            # 0.00002 BTC buys 0.00002 / p2 ETH (e.g. ETHBTC at 0.05 → 0.0004 ETH)
            # 0.0004 ETH sells for 0.0004 * p3 USDT (e.g. ETH at $2500 → $1.00 USDT)
            # If all aligned perfectly: out_usdt = 1.0 (no arb).
            # If misaligned: out_usdt != 1.0 → arb opportunity.
            fee = (1 - TAKER_FEE) ** 3
            forward_ratio = (1.0 / p1) * (1.0 / p2) * p3 * fee
            forward_profit_bps = (forward_ratio - 1.0) * 10000
            # Reverse cycle:  USDT -> ETH (leg3 buy) -> BTC (leg2 sell ETH for BTC) -> USDT (leg1 sell)
            # 1 USDT * (1/p3) ETH * p2 BTC * p1 USDT
            reverse_ratio = (1.0 / p3) * p2 * p1 * fee
            reverse_profit_bps = (reverse_ratio - 1.0) * 10000
            if self.min_profit_bps <= forward_profit_bps <= 500:  # cap at 500bps (5%) — anything higher = data error
                opportunities.append({
                    "direction": "FORWARD",
                    "cycle": tri,
                    "expected_profit_bps": round(forward_profit_bps, 2),
                    "expected_ratio": round(forward_ratio, 6),
                    "prices": {leg1: p1, leg2: p2, leg3: p3},
                })
            if self.min_profit_bps <= reverse_profit_bps <= 500:  # cap at 500bps
                opportunities.append({
                    "direction": "REVERSE",
                    "cycle": tri,
                    "expected_profit_bps": round(reverse_profit_bps, 2),
                    "expected_ratio": round(reverse_ratio, 6),
                    "prices": {leg1: p1, leg2: p2, leg3: p3},
                })
        self._last_scan = time.time()
        if opportunities:
            log.info(f"💎 TriArb found {len(opportunities)} opportunities: "
                     f"best {max(o['expected_profit_bps'] for o in opportunities):.1f}bps")
        return opportunities

    def execute(self, opp: Dict, size_usd: float) -> Dict:
        """Execute a triangular arb opportunity.

        WARNING: This places 3 sequential market orders. Price can move between
        legs (slippage risk). Recommended only when expected_profit_bps > 25
        and size is small relative to top-of-book depth.

        Returns: dict with success, final_usdt, realized_profit_bps, fills."""
        leg1, leg2, leg3 = opp["cycle"]
        direction = opp["direction"]
        log.warning(f"💎 TriArb EXECUTING {direction} {leg1}→{leg2}→{leg3} "
                    f"size=${size_usd:.2f} expected={opp['expected_profit_bps']:.1f}bps")
        # Skeleton implementation — real execution requires order tracking,
        # rollback on partial fills, atomic accounting. Returning dry-run for now.
        # Production version should do:
        #   r1 = ex.buy(leg1, qty1)  with rollback if fail
        #   r2 = sell first asset for second via leg2
        #   r3 = sell second asset for USDT via leg3
        # Each leg captures actual fill, computes realized profit at end.
        return {
            "success": False,
            "reason": "execute() is dry-run skeleton — wire fully before live use",
            "expected_profit_bps": opp["expected_profit_bps"],
        }

    def status(self) -> str:
        return f"TriArb:{len(self.triangles)}tri"
