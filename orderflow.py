"""BinBot v12.0 — orderflow.py — Aggressor Flow Tracker
Tracks buyer vs seller aggression from Binance aggTrades endpoint.
More reliable than order book snapshots — shows what's ACTUALLY being traded,
not what's sitting on the book (which can be spoofed).
"""
import json, time, logging, urllib.request
from collections import deque

log = logging.getLogger('binbot')


class AggressorFlowTracker:
    """Tracks buyer vs seller aggression from Binance recent trades.
    Uses REST polling (not WebSocket) for simplicity — fetches last 100 trades.
    
    buyer_ratio > 0.55 = aggressive buying (bullish)
    buyer_ratio < 0.45 = aggressive selling (bearish)
    large_trade_count = trades > $10K = whale footprints
    """

    def __init__(self):
        self._flows = {}          # {symbol: FlowData}
        self._btc_flow = "NEUTRAL"
        self._cache_sec = 120     # Refresh every 2 min
        self._ts = 0
        self._large_threshold = 10000  # $10K = "large" trade

    def update(self, symbols=None):
        """Fetch aggTrades for key symbols. Call every cycle."""
        if time.time() - self._ts < self._cache_sec:
            return
        if not symbols:
            symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        for sym in symbols[:5]:  # Limit to 5 to avoid rate limits
            try:
                url = f"https://api.binance.com/api/v3/trades?symbol={sym}&limit=100"
                r = urllib.request.Request(url, headers={"User-Agent": "BinBot/12"})
                resp = urllib.request.urlopen(r, timeout=8)
                trades = json.loads(resp.read().decode())
                if not isinstance(trades, list):
                    continue
                buy_vol = 0.0
                sell_vol = 0.0
                large_buys = 0
                large_sells = 0
                total_trades = 0
                for t in trades:
                    qty = float(t.get("qty", 0))
                    price = float(t.get("price", 0))
                    value = qty * price
                    is_buyer_maker = t.get("isBuyerMaker", False)
                    if is_buyer_maker:
                        # Buyer is maker = seller is taker = aggressive sell
                        sell_vol += value
                        if value >= self._large_threshold:
                            large_sells += 1
                    else:
                        # Seller is maker = buyer is taker = aggressive buy
                        buy_vol += value
                        if value >= self._large_threshold:
                            large_buys += 1
                    total_trades += 1

                total_vol = buy_vol + sell_vol
                buyer_ratio = buy_vol / total_vol if total_vol > 0 else 0.5

                if buyer_ratio > 0.60:
                    signal = "STRONG_BUY"
                elif buyer_ratio > 0.55:
                    signal = "BUY_PRESSURE"
                elif buyer_ratio < 0.40:
                    signal = "STRONG_SELL"
                elif buyer_ratio < 0.45:
                    signal = "SELL_PRESSURE"
                else:
                    signal = "NEUTRAL"

                self._flows[sym] = {
                    "buyer_ratio": round(buyer_ratio, 3),
                    "signal": signal,
                    "large_buys": large_buys,
                    "large_sells": large_sells,
                    "total_vol": total_vol
                }

                if sym == "BTCUSDT":
                    self._btc_flow = signal

            except Exception as e:
                log.debug(f"AggressorFlow {sym}: {e}")

        self._ts = time.time()
        # Log summary
        btc = self._flows.get("BTCUSDT", {})
        if btc:
            log.info(f"🔫 Aggressor Flow: BTC buy_ratio={btc.get('buyer_ratio', 0.5):.0%} "
                     f"large_buys={btc.get('large_buys', 0)} "
                     f"large_sells={btc.get('large_sells', 0)} → {self._btc_flow}")

    def get_boost(self, symbol="BTCUSDT"):
        """Confidence boost based on aggressor flow."""
        flow = self._flows.get(symbol, self._flows.get("BTCUSDT", {}))
        sig = flow.get("signal", "NEUTRAL")
        if sig == "STRONG_BUY":    return 1.12
        if sig == "BUY_PRESSURE":  return 1.06
        if sig == "STRONG_SELL":   return 0.80
        if sig == "SELL_PRESSURE": return 0.90
        return 1.0

    def should_block(self, symbol="BTCUSDT"):
        """Block if BTC has strong sell pressure + large sell orders."""
        btc = self._flows.get("BTCUSDT", {})
        if btc.get("signal") == "STRONG_SELL" and btc.get("large_sells", 0) >= 3:
            return True
        return False

    def large_trade_alert(self, symbol):
        """Returns (large_buys, large_sells) for a symbol."""
        flow = self._flows.get(symbol, {})
        return flow.get("large_buys", 0), flow.get("large_sells", 0)

    def btc_signal(self):
        return self._btc_flow

    def status(self):
        btc = self._flows.get("BTCUSDT", {})
        ratio = btc.get("buyer_ratio", 0.5)
        return f"AF:{ratio:.0%}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 1 UPGRADE: Per-Pair Signed Volume Delta Tracker
# Institutional-grade signed order flow per trading pair.
# Called during signal validation — not global scan.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class VolumeDeltaTracker:
    """Per-pair signed volume delta.

    delta_score = (aggressive_buy_vol - aggressive_sell_vol) / total_vol
    Range: -1.0 (all sells) to +1.0 (all buys)

    Uses /api/v3/aggTrades last 100 trades per pair.
    Large trade threshold: $5,000 per order (institutional footprint).

    Integration: update(pair) called per-signal in entry gate.
    """

    def __init__(self):
        self._data  = {}    # {symbol: {score, buy_vol, sell_vol, large_buys, large_sells, ts}}
        self._cache = 45    # 45s per-pair cache

    def update(self, symbol: str):
        d = self._data.get(symbol, {})
        if time.time() - d.get('ts', 0) < self._cache:
            return
        try:
            url = f"https://api.binance.com/api/v3/aggTrades?symbol={symbol}&limit=100"
            req = urllib.request.Request(url, headers={"User-Agent": "BinBot/14"})
            resp = urllib.request.urlopen(req, timeout=5)
            trades = json.loads(resp.read().decode())

            buy_vol = sell_vol = 0.0
            large_buys = large_sells = 0

            for t in trades:
                qty   = float(t.get('q', 0))
                price = float(t.get('p', 0))
                value = qty * price
                # m=True: buyer is maker → aggressive SELL
                # m=False: seller is maker → aggressive BUY
                if t.get('m', False):
                    sell_vol += value
                    if value > 5000: large_sells += 1
                else:
                    buy_vol  += value
                    if value > 5000: large_buys  += 1

            total = buy_vol + sell_vol
            if total == 0:
                return

            self._data[symbol] = {
                'score':        round((buy_vol - sell_vol) / total, 4),
                'buy_vol':      round(buy_vol, 2),
                'sell_vol':     round(sell_vol, 2),
                'large_buys':   large_buys,
                'large_sells':  large_sells,
                'ts':           time.time()
            }
        except Exception as e:
            log.debug(f"VolDelta {symbol}: {e}")

    def get_score(self, symbol: str) -> float:
        return self._data.get(symbol, {}).get('score', 0.0)

    def get_boost(self, symbol: str) -> float:
        """Confidence multiplier from signed volume delta."""
        d  = self._data.get(symbol, {})
        sc = d.get('score', 0.0)
        lb = d.get('large_buys', 0)
        ls = d.get('large_sells', 0)
        if sc > 0.40 and lb >= 2: return 1.15  # institutional buying
        if sc > 0.40:             return 1.08   # broad buying pressure
        if sc > 0.20:             return 1.03   # mild buy lean
        if sc < -0.40 and ls >= 2: return 0.75 # institutional selling
        if sc < -0.40:            return 0.88   # broad selling pressure
        if sc < -0.20:            return 0.94   # mild sell lean
        return 1.0

    def should_block(self, symbol: str) -> bool:
        """Block if heavy institutional selling detected."""
        d = self._data.get(symbol, {})
        return d.get('score', 0.0) < -0.45 and d.get('large_sells', 0) >= 3

    def status(self, symbol: str) -> str:
        return f"ΔV:{self.get_score(symbol):+.2f}"
