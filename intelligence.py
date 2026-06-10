"""BinBot v12.2 — intelligence_v2.py — Advanced Free Intelligence Modules
5 new modules using FREE APIs (no keys required):
1. FundingRateTracker — Binance Futures funding rate for contrarian signals
2. LiquidationDetector — Detects liquidation cascades from forced orders
3. SmartCoinDetector — Auto-finds best coins by volume/momentum/spread
4. CryptoPanicNews — Real-time crypto news sentiment (free RSS)
5. MomentumScanner — Cross-pair momentum divergence detector
"""
import json, time, logging, urllib.request, os, re
from collections import defaultdict
from pathlib import Path

log = logging.getLogger('binbot')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 1: Funding Rate Tracker (FREE — Binance Futures)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FundingRateTracker:
    """Tracks funding rates across top coins for contrarian signals.
    Negative funding = shorts paying longs = bullish (shorts will close).
    High positive funding = longs paying shorts = bearish (overleveraged).
    Uses: https://fapi.binance.com/fapi/v1/premiumIndex (FREE, no key).
    """
    def __init__(self):
        self._rates = {}       # {symbol: funding_rate}
        self._signals = {}     # {symbol: signal_string}
        self._avg_rate = 0.0
        self._extreme_coins = []  # Coins with extreme funding
        self._ts = 0
        self._cache_sec = 300  # 5 min

    def update(self):
        if time.time() - self._ts < self._cache_sec:
            return
        try:
            url = "https://fapi.binance.com/fapi/v1/premiumIndex"
            r = urllib.request.Request(url, headers={"User-Agent": "BinBot/12"})
            resp = urllib.request.urlopen(r, timeout=10)
            data = json.loads(resp.read().decode())
            if not isinstance(data, list):
                return
            rates = {}
            for item in data:
                sym = item.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                fr = float(item.get("lastFundingRate", 0))
                rates[sym] = fr
            self._rates = rates
            # Calculate average and find extremes
            vals = list(rates.values())
            if vals:
                self._avg_rate = sum(vals) / len(vals)
            self._extreme_coins = []
            for sym, fr in rates.items():
                if fr < -0.001:    # Very negative = shorts paying heavily
                    self._signals[sym] = "EXTREME_SHORT"
                    self._extreme_coins.append((sym, fr, "SHORT_SQUEEZE_RISK"))
                elif fr < -0.0003:
                    self._signals[sym] = "NEGATIVE"
                elif fr > 0.002:   # Very positive = longs overleveraged
                    self._signals[sym] = "EXTREME_LONG"
                    self._extreme_coins.append((sym, fr, "LONG_SQUEEZE_RISK"))
                elif fr > 0.0008:
                    self._signals[sym] = "HIGH_POSITIVE"
                else:
                    self._signals[sym] = "NEUTRAL"
            self._ts = time.time()
            neg = sum(1 for v in rates.values() if v < -0.0003)
            pos = sum(1 for v in rates.values() if v > 0.0008)
            btc_fr = rates.get("BTCUSDT", 0)
            log.info(f"💰 Funding: BTC={btc_fr:.4%} avg={self._avg_rate:.4%} | "
                     f"neg={neg} high_pos={pos} extremes={len(self._extreme_coins)}")
        except Exception as e:
            log.debug(f"FundingRate update: {e}")

    def get_boost(self, symbol="BTCUSDT"):
        sig = self._signals.get(symbol, "NEUTRAL")
        if sig == "EXTREME_SHORT":  return 1.15  # +15% — short squeeze incoming
        if sig == "NEGATIVE":       return 1.08  # +8% — shorts paying longs
        if sig == "EXTREME_LONG":   return 0.75  # -25% — overleveraged longs
        if sig == "HIGH_POSITIVE":  return 0.88  # -12% — caution
        return 1.0

    def should_block(self, symbol="BTCUSDT"):
        """Block on extreme positive funding (overleveraged longs about to get rekt)."""
        fr = self._rates.get(symbol, 0)
        return fr > 0.003  # >0.3% = extreme

    def get_extreme_coins(self):
        """Returns coins with extreme funding for opportunity detection."""
        return self._extreme_coins[:5]

    def status(self):
        btc = self._rates.get("BTCUSDT", 0)
        return f"FR:{btc:.3%}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 2: Liquidation Cascade Detector (FREE — Binance Futures)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LiquidationDetector:
    """Detects liquidation cascades from Binance forced orders.
    Burst of liquidations = potential reversal or continuation.
    Uses: https://fapi.binance.com/fapi/v1/allForceOrders (FREE).
    """
    def __init__(self):
        self._recent_liqs = []     # Recent liquidation events
        self._cascade_active = False
        self._cascade_side = "NONE"  # "LONG" or "SHORT"
        self._total_liq_usd = 0
        self._ts = 0
        self._cache_sec = 120  # 2 min

    def update(self):
        if time.time() - self._ts < self._cache_sec:
            return
        try:
            url = "https://fapi.binance.com/fapi/v1/allForceOrders?limit=50"
            r = urllib.request.Request(url, headers={"User-Agent": "BinBot/12"})
            resp = urllib.request.urlopen(r, timeout=8)
            data = json.loads(resp.read().decode())
            if not isinstance(data, list):
                return
            now = time.time() * 1000
            recent = []
            long_liq_usd = 0
            short_liq_usd = 0
            for liq in data:
                liq_time = int(liq.get("time", 0))
                if now - liq_time > 600000:  # Only last 10 min
                    continue
                price = float(liq.get("price", 0))
                qty = float(liq.get("origQty", 0))
                side = liq.get("side", "")  # SELL = long liquidated, BUY = short liquidated
                value = price * qty
                recent.append({"side": side, "value": value, "symbol": liq.get("symbol", "")})
                if side == "SELL":
                    long_liq_usd += value  # Longs being liquidated
                else:
                    short_liq_usd += value  # Shorts being liquidated
            self._recent_liqs = recent
            self._total_liq_usd = long_liq_usd + short_liq_usd
            # Cascade detection: >$1M in liquidations in 10 min
            if self._total_liq_usd > 1_000_000:
                self._cascade_active = True
                self._cascade_side = "LONG" if long_liq_usd > short_liq_usd else "SHORT"
            elif self._total_liq_usd > 500_000:
                self._cascade_active = False
                self._cascade_side = "LONG" if long_liq_usd > short_liq_usd else "SHORT"
            else:
                self._cascade_active = False
                self._cascade_side = "NONE"
            self._ts = time.time()
            if self._total_liq_usd > 100_000:
                log.info(f"💥 Liquidations: ${self._total_liq_usd/1000:.0f}K "
                         f"(L:{long_liq_usd/1000:.0f}K S:{short_liq_usd/1000:.0f}K) "
                         f"cascade={'🔴YES' if self._cascade_active else '🟢NO'} "
                         f"side={self._cascade_side}")
        except Exception as e:
            log.debug(f"Liquidation update: {e}")

    def get_boost(self):
        if self._cascade_active:
            if self._cascade_side == "LONG":
                return 0.70  # -30% — longs getting liquidated, don't buy
            else:
                return 1.15  # +15% — shorts liquidated, squeeze up
        if self._cascade_side == "LONG" and self._total_liq_usd > 500_000:
            return 0.85
        if self._cascade_side == "SHORT" and self._total_liq_usd > 500_000:
            return 1.08
        return 1.0

    def should_block(self):
        """Block during active long liquidation cascade."""
        return self._cascade_active and self._cascade_side == "LONG"

    def status(self):
        tag = "🔴CASCADE" if self._cascade_active else "OK"
        return f"LIQ:{self._total_liq_usd/1000:.0f}K/{tag}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 3: Smart Coin Auto-Detector (FREE — Binance Spot)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SmartCoinDetector:
    """Auto-detects best tradable coins based on real-time metrics:
    - Volume surge (vs 24h average)
    - Price momentum (% change)
    - Spread tightness (bid-ask)
    - Not already in portfolio
    Uses Binance 24h ticker (FREE, no key).
    """
    def __init__(self):
        self._rankings = []      # [(symbol, score, metrics)]
        self._hot_coins = []     # Top 10 auto-detected
        self._blacklist = set()  # Stablecoins, low-cap, etc.
        self._ts = 0
        self._cache_sec = 300    # 5 min
        # Exclude stablecoins and wrapped tokens
        self._blacklist = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT",
                           "DAIUSDT", "USDPUSDT", "EURUSDT", "GBPUSDT",
                           "WBTCUSDT", "WBETHUSDT", "BETHUSDT"}

    def update(self, existing_positions=None):
        if time.time() - self._ts < self._cache_sec:
            return
        try:
            url = "https://api.binance.com/api/v3/ticker/24hr"
            r = urllib.request.Request(url, headers={"User-Agent": "BinBot/12"})
            resp = urllib.request.urlopen(r, timeout=15)
            data = json.loads(resp.read().decode())
            if not isinstance(data, list):
                return
            held = set()
            if existing_positions:
                held = {p.pair for p in existing_positions}
            candidates = []
            for t in data:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT") or sym in self._blacklist:
                    continue
                if sym in held:
                    continue
                try:
                    quote_vol = float(t.get("quoteVolume", 0))
                    pct_change = float(t.get("priceChangePercent", 0))
                    last_price = float(t.get("lastPrice", 0))
                    bid = float(t.get("bidPrice", 0))
                    ask = float(t.get("askPrice", 0))
                    high = float(t.get("highPrice", 0))
                    low = float(t.get("lowPrice", 0))
                    count = int(t.get("count", 0))
                except (ValueError, TypeError):
                    continue
                # Minimum filters
                if quote_vol < 5_000_000:  # Need $5M+ daily volume
                    continue
                if last_price <= 0 or count < 10000:
                    continue
                # Spread tightness (lower = better liquidity)
                spread_pct = (ask - bid) / last_price * 100 if last_price > 0 else 999
                if spread_pct > 0.3:  # Skip illiquid
                    continue
                # Volatility (range / price)
                range_pct = (high - low) / last_price * 100 if last_price > 0 else 0
                # Score: higher volume + momentum + tight spread + good range
                vol_score = min(quote_vol / 100_000_000, 3.0)  # Cap at $300M
                mom_score = max(-2, min(3, pct_change / 3))  # -2 to +3
                spread_score = max(0, 1.0 - spread_pct * 10)  # Tighter = higher
                range_score = min(range_pct / 5, 2.0)  # Some volatility is good
                total = vol_score * 0.3 + mom_score * 0.3 + spread_score * 0.2 + range_score * 0.2
                candidates.append((sym, round(total, 2), {
                    "vol_M": round(quote_vol / 1_000_000, 1),
                    "chg": round(pct_change, 2),
                    "spread": round(spread_pct, 4),
                    "range": round(range_pct, 2)
                }))
            candidates.sort(key=lambda x: x[1], reverse=True)
            self._rankings = candidates[:30]
            self._hot_coins = [c[0] for c in candidates[:10]]
            self._ts = time.time()
            top5 = [(c[0].replace("USDT",""), c[1]) for c in candidates[:5]]
            log.info(f"🔍 SmartDetect: {len(candidates)} coins scored | "
                     f"Top: {top5}")
        except Exception as e:
            log.debug(f"SmartCoinDetector: {e}")

    def get_hot_coins(self):
        """Returns top 10 auto-detected coins by composite score."""
        return self._hot_coins

    def coin_score(self, symbol):
        """Get score for a specific coin (0-5 scale)."""
        for sym, score, _ in self._rankings:
            if sym == symbol:
                return score
        return 0.0

    def get_boost(self, symbol):
        """Boost based on coin quality score."""
        score = self.coin_score(symbol)
        if score > 2.0: return 1.10   # Top-tier coin
        if score > 1.5: return 1.05
        if score < 0.5: return 0.90   # Poor quality
        return 1.0

    def should_add_pairs(self, current_pairs):
        """Suggests new pairs to add based on detection."""
        current_syms = {p["s"] for p in current_pairs}
        new_suggestions = [c for c in self._hot_coins if c not in current_syms]
        return new_suggestions[:5]

    def status(self):
        return f"SC:{len(self._hot_coins)}hot"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 4: CryptoPanic News Sentiment (FREE — RSS)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CryptoPanicNews:
    """Multi-source crypto news sentiment aggregator.
    Sources: CoinTelegraph RSS, CoinDesk RSS, Bitcoin Magazine RSS.
    Analyzes headlines for bullish/bearish signals per coin.
    Detects 'hot' news events that could cause price spikes.
    """
    BULL = ['surge','soar','rally','pump','bullish','breakout','record','gain','rise',
            'moon','adopt','approve','etf','launch','partner','upgrade','institutional',
            'accumulate','support','recover','momentum','spike','profit','boom','inflow',
            'reserve','treasury','halving','milestone','mainnet','listing','integration']
    BEAR = ['crash','plunge','dump','bearish','hack','ban','scam','fraud','sell','drop',
            'fear','crisis','warning','regulat','fine','lawsuit','collapse','bubble',
            'exploit','vulnerability','panic','liquidat','decline','loss','outflow',
            'depegged','rug','ponzi','investigation','sec','subpoena','bankrupt']
    # Coin-specific keywords for targeted sentiment
    COIN_KEYWORDS = {
        "BTC": ["bitcoin","btc"], "ETH": ["ethereum","eth","vitalik"],
        "SOL": ["solana","sol"], "BNB": ["binance","bnb"],
        "DOGE": ["dogecoin","doge","elon"], "XRP": ["ripple","xrp"],
        "ADA": ["cardano","ada"], "AVAX": ["avalanche","avax"],
        "LINK": ["chainlink","link"], "DOT": ["polkadot","dot"],
        "MATIC": ["polygon","matic","pol"], "NEAR": ["near protocol"],
    }

    def __init__(self):
        self._global_score = 0.0
        self._global_label = "Neutral"
        self._coin_scores = {}  # {coin: score}
        self._hot_events = []   # Headlines with extreme sentiment
        self._ts = 0
        self._cache_sec = 300   # 5 min
        self._sources = [
            "https://cointelegraph.com/rss",
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://bitcoinmagazine.com/.rss/full/",
        ]

    def update(self):
        if time.time() - self._ts < self._cache_sec:
            return
        import defusedxml.ElementTree as ET
        all_headlines = []
        for src_url in self._sources:
            try:
                req = urllib.request.Request(src_url, headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                })
                with urllib.request.urlopen(req, timeout=8) as resp:
                    root = ET.fromstring(resp.read().decode())
                    for item in root.findall('.//item')[:20]:
                        title = item.find('title')
                        if title is not None and title.text:
                            all_headlines.append(title.text)
            except Exception:
                continue
        if not all_headlines:
            return
        # Global sentiment
        total_score = 0.0
        count = 0
        coin_scores = defaultdict(list)
        self._hot_events = []
        for h in all_headlines:
            hl = h.lower()
            bull = sum(1 for w in self.BULL if w in hl)
            bear = sum(1 for w in self.BEAR if w in hl)
            if bull + bear > 0:
                score = (bull - bear) / (bull + bear)
                total_score += score
                count += 1
                # Check if extreme
                if abs(score) >= 0.8 and (bull + bear) >= 2:
                    self._hot_events.append({"headline": h[:100], "score": score})
            # Per-coin sentiment
            for coin, keywords in self.COIN_KEYWORDS.items():
                if any(kw in hl for kw in keywords):
                    s = (bull - bear) / max(bull + bear, 1)
                    coin_scores[coin].append(s)
        self._global_score = round(total_score / max(count, 1), 2)
        labels = {True: "📈Bull", False: "📉Bear"}
        if abs(self._global_score) < 0.1:
            self._global_label = "➡️Neutral"
        else:
            self._global_label = labels[self._global_score > 0]
        # Per-coin
        self._coin_scores = {}
        for coin, scores in coin_scores.items():
            self._coin_scores[coin] = round(sum(scores) / len(scores), 2) if scores else 0
        self._ts = time.time()
        hot_count = len(self._hot_events)
        log.info(f"📰 MultiNews: {self._global_label} ({self._global_score:+.2f}) | "
                 f"{len(all_headlines)} headlines | {hot_count} hot events | "
                 f"coins: {dict(list(self._coin_scores.items())[:5])}")

    def get_boost(self, symbol="BTCUSDT"):
        coin = symbol.replace("USDT", "")
        coin_score = self._coin_scores.get(coin, 0)
        # Use coin-specific if available, otherwise global
        score = coin_score if coin_score != 0 else self._global_score
        if score > 0.5:  return 1.10
        if score > 0.2:  return 1.05
        if score < -0.5: return 0.85
        if score < -0.2: return 0.93
        return 1.0

    def should_block(self, symbol="BTCUSDT"):
        """Block on extremely bearish news for this coin."""
        coin = symbol.replace("USDT", "")
        return self._coin_scores.get(coin, 0) < -0.7

    def get_hot_events(self):
        return self._hot_events[:5]

    def status(self):
        return f"News:{self._global_score:+.2f}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 4b: Binance OFFICIAL Announcements gate (delisting / halt)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class BinanceAnnouncements:
    """v18.9.5 — pre-trade safety gate from Binance OFFICIAL signals.

    Two layers, both FAIL-OPEN (any error leaves trading UNBLOCKED — a feed
    outage must NEVER halt the bot):
      1. Authoritative live symbol status via exchangeInfo (the bot's own client):
         a managed USDT pair whose status != TRADING / not spot-allowed, or that
         has vanished from exchangeInfo entirely, is treated as delisted/halted.
      2. Early-warning delisting announcements (best-effort web feed): titles
         containing a delist keyword → the managed coins named in that title.

    `should_block(pair)` is pure (no network); call `update()` periodically. It
    only ever blocks NEW entries — a conservative, safe-side action (you never
    want to buy a coin that's being delisted or is halted).
    """
    DELIST_KW = ('delist', 'will remove', 'removal of', 'cease trading',
                 'ceases trading', 'will cease', 'suspend the trading of')
    _ANN_URL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/"
                "catalog/list/query?catalogId=161&pageNo=1&pageSize=20")
    _UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

    def __init__(self, ttl_sec=900):
        self.ttl = ttl_sec
        self._delist = set()      # base assets with a delisting announced
        self._halted = set()      # base assets not actively TRADING per exchangeInfo
        self._titles = []
        self._ts = 0.0
        self._fails = 0

    @staticmethod
    def _titles_from_payload(data):
        """Defensively pull article titles out of the bapi CMS response shape."""
        out = []
        try:
            cats = ((data or {}).get('data') or {}).get('catalogs') or []
            for c in cats:
                for a in (c.get('articles') or []):
                    t = a.get('title')
                    if t:
                        out.append(t)
        except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        return out

    @staticmethod
    def _symbols_in_title(title, managed_bases):
        """Return the managed base assets explicitly named in a title (intersection
        only — random uppercase tokens never match, so no false delistings)."""
        if not managed_bases:
            return set()
        toks = set(re.findall(r'[A-Z0-9]{2,10}', (title or '').upper()))
        return {b for b in managed_bases if b in toks}

    def update(self, managed_pairs=None, client=None):
        now = time.time()
        if now - self._ts < self.ttl:
            return
        self._ts = now
        managed_pairs = list(managed_pairs or [])
        managed_bases = { (p['s'] if isinstance(p, dict) else p).replace('USDT', '') for p in managed_pairs }

        # Layer 1 — authoritative live status (reliable, via existing client)
        if client is not None:
            try:
                info = client.get_exchange_info()
                syms = info.get('symbols', [])
                live = {s['symbol'] for s in syms}
                halted = set()
                for s in syms:
                    if s.get('quoteAsset') != 'USDT':
                        continue
                    if s.get('status') != 'TRADING' or not s.get('isSpotTradingAllowed', True):
                        halted.add(s.get('baseAsset', ''))
                for p in managed_pairs:        # vanished from exchangeInfo = delisted
                    _sym = p['s'] if isinstance(p, dict) else p
                    if _sym not in live:
                        halted.add(_sym.replace('USDT', ''))
                self._halted = {b for b in halted if b}
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")  # fail-open: keep previous

        # Layer 2 — delisting announcements (best-effort early warning)
        try:
            req = urllib.request.Request(self._ANN_URL, headers=self._UA)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            titles = self._titles_from_payload(data)
            delist = set()
            for t in titles:
                if any(k in t.lower() for k in self.DELIST_KW):
                    delist |= self._symbols_in_title(t, managed_bases)
            self._titles = titles[:10]
            self._delist = delist
            self._fails = 0
        except Exception:
            self._fails += 1  # fail-open: keep previous _delist (may be empty)

        if self._delist or self._halted:
            log.info(f"📢 Binance announce gate: delist={sorted(self._delist)} "
                     f"halted={sorted(self._halted)[:6]}")

    def should_block(self, symbol="BTCUSDT"):
        base = symbol.replace('USDT', '')
        return base in self._delist or base in self._halted

    def status(self):
        return f"announce delist={len(self._delist)} halted={len(self._halted)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 5: Momentum Cross-Pair Scanner (FREE — Binance)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MomentumScanner:
    """Scans multiple pairs for momentum divergence.
    Detects: sector rotation, relative strength, BTC divergence.
    Uses Binance mini-ticker stream data.
    """
    def __init__(self):
        self._momentum = {}     # {symbol: {m5, m15, m1h}}
        self._btc_momentum = 0
        self._leaders = []      # Coins outperforming BTC
        self._laggards = []     # Coins underperforming
        self._ts = 0
        self._cache_sec = 180   # 3 min

    def update(self, symbols=None, exchange=None):
        if time.time() - self._ts < self._cache_sec:
            return
        if not exchange:
            return
        try:
            # Get BTC reference
            btc_candles = exchange.klines_sync("BTCUSDT", "5m", 20)  # v15.3 FIX: sync helper
            if not btc_candles or len(btc_candles) < 12:
                return
            btc_m5 = (btc_candles[-1].c - btc_candles[-2].c) / btc_candles[-2].c * 100
            btc_m15 = (btc_candles[-1].c - btc_candles[-4].c) / btc_candles[-4].c * 100
            btc_m1h = (btc_candles[-1].c - btc_candles[-13].c) / btc_candles[-13].c * 100 if len(btc_candles) >= 13 else 0
            self._btc_momentum = btc_m15
            if not symbols:
                symbols = ["ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                           "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOGEUSDT"]
            for sym in symbols[:10]:
                try:
                    candles = exchange.klines_sync(sym, "5m", 20)  # v15.3 FIX: sync helper
                    if not candles or len(candles) < 13:
                        continue
                    m5 = (candles[-1].c - candles[-2].c) / candles[-2].c * 100
                    m15 = (candles[-1].c - candles[-4].c) / candles[-4].c * 100
                    m1h = (candles[-1].c - candles[-13].c) / candles[-13].c * 100
                    # Relative strength vs BTC
                    rs = m15 - btc_m15
                    self._momentum[sym] = {
                        "m5": round(m5, 3), "m15": round(m15, 3),
                        "m1h": round(m1h, 3), "rs": round(rs, 3)
                    }
                except Exception:
                    continue
            # Rank by relative strength
            ranked = sorted(self._momentum.items(), key=lambda x: x[1]["rs"], reverse=True)
            self._leaders = [(s, d["rs"]) for s, d in ranked[:3] if d["rs"] > 0.1]
            self._laggards = [(s, d["rs"]) for s, d in ranked[-3:] if d["rs"] < -0.1]
            self._ts = time.time()
            if self._leaders:
                log.info(f"📊 Momentum: BTC={btc_m15:+.2f}% | "
                         f"Leaders: {[(s.replace('USDT',''),r) for s,r in self._leaders]} | "
                         f"Laggards: {[(s.replace('USDT',''),r) for s,r in self._laggards]}")
        except Exception as e:
            log.debug(f"MomentumScanner: {e}")

    def get_boost(self, symbol):
        """Boost leaders, penalize laggards."""
        data = self._momentum.get(symbol, {})
        rs = data.get("rs", 0)
        if rs > 0.5:  return 1.10  # Strong leader
        if rs > 0.2:  return 1.05  # Outperformer
        if rs < -0.5: return 0.85  # Strong laggard
        if rs < -0.2: return 0.93  # Underperformer
        return 1.0

    def is_leader(self, symbol):
        return any(s == symbol for s, _ in self._leaders)

    def is_laggard(self, symbol):
        return any(s == symbol for s, _ in self._laggards)

    def get_leaders(self):
        return self._leaders

    def status(self):
        return f"MOM:BTC{self._btc_momentum:+.1f}%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 3 UPGRADE: Enhanced Per-Pair Liquidation Cascade
# Adds per-symbol tracking + velocity detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LiquidationCascadeTracker:
    """Enhanced liquidation cascade detection with per-symbol tracking.

    Improvements over LiquidationDetector:
    - Per-symbol liquidation volume (not just global)
    - Velocity tracking: rate of liquidations per minute
    - Short window (5 min) + long window (30 min) comparison
    - Shock detection: sudden spike = cascade imminent

    Uses: https://fapi.binance.com/fapi/v1/allForceOrders (FREE, no key)
    """

    def __init__(self):
        self._sym_data   = {}    # {symbol: {long_liq, short_liq, velocity, ts}}
        self._global     = {'long': 0, 'short': 0, 'total': 0, 'cascade': False, 'side': 'NONE'}
        self._cache_sec  = 90    # 90s refresh

    def update(self):
        if time.time() - self._global.get('ts', 0) < self._cache_sec:
            return
        try:
            url = "https://fapi.binance.com/fapi/v1/allForceOrders?limit=100"
            req = urllib.request.Request(url, headers={"User-Agent": "BinBot/14"})
            resp = urllib.request.urlopen(req, timeout=8)
            data = json.loads(resp.read().decode())
            if not isinstance(data, list):
                return

            now = time.time() * 1000
            sym_long  = defaultdict(float)
            sym_short = defaultdict(float)
            g_long = g_short = 0.0

            for liq in data:
                age = now - int(liq.get('time', 0))
                if age > 600_000:   # Only last 10 min
                    continue
                sym   = liq.get('symbol', '')
                price = float(liq.get('price', 0))
                qty   = float(liq.get('origQty', 0))
                side  = liq.get('side', '')
                val   = price * qty
                # SELL = long position liquidated; BUY = short position liquidated
                if side == 'SELL':
                    sym_long[sym]  += val
                    g_long += val
                else:
                    sym_short[sym] += val
                    g_short += val

            # Build per-symbol data
            for sym in set(list(sym_long.keys()) + list(sym_short.keys())):
                ll = sym_long.get(sym, 0)
                sl = sym_short.get(sym, 0)
                total = ll + sl
                self._sym_data[sym] = {
                    'long_liq':   round(ll, 2),
                    'short_liq':  round(sl, 2),
                    'total':      round(total, 2),
                    'dominant':   'LONG' if ll > sl else 'SHORT',
                    'cascade':    total > 200_000,   # $200K per symbol = local cascade
                    'ts':         time.time()
                }

            g_total = g_long + g_short
            cascade = g_total > 1_000_000    # $1M global = macro cascade
            self._global = {
                'long':    round(g_long, 2),
                'short':   round(g_short, 2),
                'total':   round(g_total, 2),
                'cascade': cascade,
                'side':    'LONG' if g_long > g_short else 'SHORT',
                'ts':      time.time()
            }
            if g_total > 100_000:
                log.info(f"💥 LiqCascade: global=${g_total/1000:.0f}K "
                         f"(L:{g_long/1000:.0f}K S:{g_short/1000:.0f}K) "
                         f"cascade={'🔴' if cascade else '🟢'} "
                         f"active_symbols={len(sym_long)+len(sym_short)}")
        except Exception as e:
            log.debug(f"LiqCascade update: {e}")

    def should_block(self, symbol: str = None) -> bool:
        """Block on macro cascade OR per-symbol long liquidation flood."""
        g = self._global
        if g.get('cascade') and g.get('side') == 'LONG':
            return True
        if symbol:
            sd = self._sym_data.get(symbol, {})
            if sd.get('cascade') and sd.get('dominant') == 'LONG':
                return True
        return False

    def get_boost(self, symbol: str = None) -> float:
        g = self._global
        if g.get('cascade'):
            return 0.70 if g.get('side') == 'LONG' else 1.15
        # Per-symbol
        if symbol:
            sd = self._sym_data.get(symbol, {})
            if sd.get('cascade'):
                return 0.80 if sd.get('dominant') == 'LONG' else 1.10
        total = g.get('total', 0)
        if total > 500_000:
            return 0.88 if g.get('side') == 'LONG' else 1.08
        return 1.0

    def status(self) -> str:
        g = self._global
        tag = '🔴' if g.get('cascade') else '🟢'
        return f"LIQ:{g.get('total', 0)/1000:.0f}K{tag}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 4 UPGRADE: Spot-Perp Basis Tracker
# Tracks premium/discount of futures vs spot price
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SpotPerpBasisTracker:
    """Tracks spot-perp basis (premium/discount) per symbol.

    Basis = (futures_mark_price - spot_price) / spot_price × 100

    Interpretation:
    +0.5% or higher = futures expensive = overleveraged longs = bearish signal
    Negative basis  = futures cheap = potential upside squeeze = bullish signal
    Combined with funding rate for stronger signal confirmation.

    Uses: Binance mark price (FREE, no futures key needed for mark price).
    """

    def __init__(self):
        self._basis   = {}   # {symbol: {basis_pct, mark_price, signal, ts}}
        self._cache   = 120  # 2 min

    def update(self, symbols=None):
        if not symbols:
            symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
        for sym in symbols[:8]:
            cached = self._basis.get(sym, {})
            if time.time() - cached.get('ts', 0) < self._cache:
                continue
            try:
                # Get futures mark price
                url_f = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
                req = urllib.request.Request(url_f, headers={"User-Agent": "BinBot/14"})
                resp = urllib.request.urlopen(req, timeout=5)
                fdata = json.loads(resp.read().decode())
                mark_price  = float(fdata.get('markPrice', 0))
                spot_price  = float(fdata.get('indexPrice', 0))  # index = spot proxy
                funding     = float(fdata.get('lastFundingRate', 0))

                if spot_price == 0:
                    continue

                basis_pct = (mark_price - spot_price) / spot_price * 100

                if basis_pct > 0.50:
                    signal = 'EXPENSIVE'       # longs paying big premium
                elif basis_pct > 0.20:
                    signal = 'ELEVATED'        # slightly expensive
                elif basis_pct < -0.20:
                    signal = 'DISCOUNT'        # futures cheap = bullish
                else:
                    signal = 'FAIR'

                self._basis[sym] = {
                    'basis_pct':  round(basis_pct, 4),
                    'mark_price': round(mark_price, 6),
                    'spot_price': round(spot_price, 6),
                    'funding':    round(funding, 6),
                    'signal':     signal,
                    'ts':         time.time()
                }
            except Exception as e:
                log.debug(f"SpotPerpBasis {sym}: {e}")

    def get_boost(self, symbol: str) -> float:
        d = self._basis.get(symbol, {})
        sig = d.get('signal', 'FAIR')
        basis = d.get('basis_pct', 0.0)
        if sig == 'DISCOUNT':      return 1.08   # futures cheap = bullish
        if sig == 'FAIR':          return 1.0
        if sig == 'ELEVATED':      return 0.93   # -7% caution
        if sig == 'EXPENSIVE':     return 0.82   # -18% overleveraged
        return 1.0

    def should_block(self, symbol: str) -> bool:
        d = self._basis.get(symbol, {})
        basis = d.get('basis_pct', 0.0)
        funding = d.get('funding', 0.0)
        # Block if both basis AND funding are extreme positive (double confirmation)
        return basis > 0.80 and funding > 0.002

    def status(self, symbol: str) -> str:
        d = self._basis.get(symbol, {})
        return f"BASIS:{d.get('basis_pct', 0):+.2f}%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODULE 6: Statistical Arbitrage — Spread Z-Score Filter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class StatArbSignal:
    """Cointegration spread Z-score signal.
    Tracks BTC/ETH/SOL price ratios.
    Extreme Z-score = structural imbalance = reduce confidence.
    Mean-reverting Z-score = convergence = boost.
    Uses statsmodels (already installed). No extra RAM needed.
    """
    def __init__(self):
        self._cache  = {}
        self._cache_s = 120

    def update(self, prices_a: list, prices_b: list, label: str):
        if len(prices_a) < 30 or len(prices_b) < 30: return
        if time.time() - self._cache.get(label, {}).get('ts', 0) < self._cache_s: return
        try:
            import numpy as np
            n  = min(len(prices_a), len(prices_b))
            pa = np.array(prices_a[-n:], dtype=float)
            pb = np.array(prices_b[-n:], dtype=float)
            spread = np.log(pa) - np.log(pb)
            std    = spread.std()
            z      = float((spread[-1] - spread.mean()) / std) if std > 0 else 0.0
            self._cache[label] = {'z': round(z, 3), 'ts': time.time()}
        except Exception as e:
            log.debug(f"StatArb {label}: {e}")

    def get_z(self, label: str) -> float:
        return self._cache.get(label, {}).get('z', 0.0)

    def get_boost(self, label: str) -> float:
        z = abs(self.get_z(label))
        if z > 2.5: return 0.85
        if z > 2.0: return 0.92
        if z < 0.5: return 1.05
        return 1.0

    def should_block(self, label: str) -> bool:
        return abs(self.get_z(label)) > 3.0

    def status(self) -> str:
        zs = [f"{k}:Z={v['z']:+.1f}" for k, v in self._cache.items()]
        return " ".join(zs) if zs else "StatArb:no data"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POINT 4: HuggingFace FinBERT Sentiment (FREE API)
# Runs FinBERT on HF servers — zero local RAM cost
# Falls back to keyword scoring if API unavailable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FinBERTSentiment:
    """HuggingFace Inference API for FinBERT financial sentiment.
    Sends headlines to HF servers — zero RAM cost locally.
    Free tier = 30k requests/month.
    Falls back to keyword scoring if API unreachable.
    """
    HF_URL = "https://api-inference.huggingface.co/models/ProsusAI/finbert"

    def __init__(self, hf_token: str = None):
        self._token  = hf_token or os.environ.get("HF_API_TOKEN", "")
        self._cache  = {}
        self._cache_s = 3600
        self._fails  = 0

    def analyze(self, headline: str) -> tuple:
        if not headline or len(headline) < 10:
            return "neutral", 0.5
        key = str(hash(headline[:100]))
        cached = self._cache.get(key, {})
        if time.time() - cached.get("ts", 0) < self._cache_s:
            return cached["label"], cached["score"]
        if self._token and self._fails < 3:
            try:
                import json as _j, urllib.request as _u
                payload = _j.dumps({"inputs": headline[:512]}).encode()
                req = _u.Request(self.HF_URL, data=payload,
                    headers={"Authorization": f"Bearer {self._token}",
                             "Content-Type": "application/json"})
                resp = _u.urlopen(req, timeout=5)
                result = _j.loads(resp.read().decode())
                if result and isinstance(result, list) and result[0]:
                    best = max(result[0], key=lambda x: x["score"])
                    label, score = best["label"].lower(), best["score"]
                    self._cache[key] = {"label": label, "score": score, "ts": time.time()}
                    self._fails = 0
                    return label, score
            except Exception as e:
                self._fails += 1
                log.debug(f"FinBERT API: {e} (fail {self._fails}/3)")
        return self._keyword_sentiment(headline)

    def _keyword_sentiment(self, text: str) -> tuple:
        t = text.lower()
        pos = sum(1 for w in ["surge","rally","bullish","breakout","approval","etf",
                               "partnership","upgrade","profit","adoption","record"] if w in t)
        neg = sum(1 for w in ["crash","hack","breach","ban","lawsuit","fraud","bearish",
                               "dump","warning","scam","arrested","collapse"] if w in t)
        if neg > pos: return "negative", min(0.5 + neg*0.1, 0.85)
        if pos > neg: return "positive", min(0.5 + pos*0.1, 0.85)
        return "neutral", 0.5

    def get_conf_boost(self, headline: str) -> float:
        label, score = self.analyze(headline)
        if label == "negative" and score > 0.80: return 0.70
        if label == "negative" and score > 0.65: return 0.88
        if label == "positive" and score > 0.80: return 1.12
        if label == "positive" and score > 0.65: return 1.06
        return 1.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# VPIN: Volume-Synchronized Probability of Informed Trading
# Detects toxic/informed order flow BEFORE price moves
# Protects against flash crashes and adverse selection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class VPINTracker:
    """VPIN — Volume-Synchronized Probability of Informed Trading.

    Unlike time-based metrics, VPIN operates in volume-time:
    state updates only after a threshold of volume transacted.
    This eliminates statistical distortion from low-activity periods.

    VPIN = |buy_vol - sell_vol| / total_vol per volume bucket
    High VPIN (>0.50) = toxic informed flow = crash risk = BLOCK
    Low  VPIN (<0.20) = clean retail flow  = safe conditions = BOOST

    Uses aggTrades data already fetched by M1 VolumeDeltaTracker.
    Zero extra API calls. Pure numpy. ~5KB RAM.
    """

    def __init__(self, bucket_size_pct: float = 0.02, n_buckets: int = 50):
        self._data    = {}       # {symbol: {vpin, buckets, ts}}
        self._cache_s = 60       # 60s cache per symbol
        self._buckets = n_buckets
        self._bkt_pct = bucket_size_pct  # bucket = 2% of avg daily vol

    def update(self, symbol: str):
        cached = self._data.get(symbol, {})
        if time.time() - cached.get('ts', 0) < self._cache_s:
            return
        try:
            import json, urllib.request, numpy as np
            url = f"https://api.binance.com/api/v3/aggTrades?symbol={symbol}&limit=500"
            req = urllib.request.Request(url, headers={"User-Agent": "BinBot/14"})
            resp = urllib.request.urlopen(req, timeout=5)
            trades = json.loads(resp.read().decode())

            buy_vols  = []
            sell_vols = []
            bucket_bv = bucket_sv = bucket_total = 0.0

            # Determine bucket size = 2% of total traded volume in window
            total_vol = sum(float(t['q']) * float(t['p']) for t in trades)
            bucket_threshold = total_vol * self._bkt_pct

            for t in trades:
                val = float(t['q']) * float(t['p'])
                if t['m']:   # buyer is maker = aggressive SELL
                    bucket_sv    += val
                else:        # seller is maker = aggressive BUY
                    bucket_bv    += val
                bucket_total += val

                if bucket_total >= bucket_threshold and bucket_total > 0:
                    buy_vols.append(bucket_bv)
                    sell_vols.append(bucket_sv)
                    bucket_bv = bucket_sv = bucket_total = 0.0

            if not buy_vols:
                return

            buy_arr  = np.array(buy_vols)
            sell_arr = np.array(sell_vols)
            total    = buy_arr + sell_arr
            total    = np.where(total == 0, 1e-9, total)
            vpin_series = np.abs(buy_arr - sell_arr) / total
            vpin = float(np.mean(vpin_series[-self._buckets:]))

            self._data[symbol] = {
                'vpin':    round(vpin, 4),
                'buckets': len(buy_vols),
                'toxic':   vpin > 0.65,
                'ts':      time.time()
            }
        except Exception as e:
            log.debug(f"VPIN {symbol}: {e}")

    def get_vpin(self, symbol: str) -> float:
        return self._data.get(symbol, {}).get('vpin', 0.0)

    def should_block(self, symbol: str) -> bool:
        """Block entry if toxic informed flow detected."""
        return self._data.get(symbol, {}).get('toxic', False)

    def get_boost(self, symbol: str) -> float:
        vpin = self.get_vpin(symbol)
        # v14.6.5 AUDIT FIX (F40): Academic VPIN literature (Easley, López de Prado)
        # uses 0.40-0.50 as toxicity threshold; crypto runs structurally higher but
        # rarely exceeds 0.80 — previous thresholds (0.85/0.90/0.95) effectively
        # never triggered, leaving the entire toxicity-penalty path dormant.
        # New ladder targets actual crypto VPIN distribution.
        if vpin > 0.80: return 0.70   # Extreme toxicity
        if vpin > 0.70: return 0.85   # High toxicity
        if vpin > 0.60: return 0.93   # Elevated
        if vpin < 0.30: return 1.08   # Clean flow = boost
        if vpin < 0.45: return 1.03   # Mild clean
        return 1.0

    def status(self, symbol: str) -> str:
        d = self._data.get(symbol, {})
        return f"VPIN:{d.get('vpin',0):.2f}({'TOXIC' if d.get('toxic') else 'CLEAN'})"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# KALMAN FILTER PAIRS TRADING — M6 Upgrade
# Replaces static Z-score with dynamic hedge ratio
# Updates beta (hedge ratio) every price tick
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KalmanPairsSpreader:
    """Dynamic Kalman Filter for BTC/ETH/SOL spread tracking.

    State variables: [beta (hedge ratio), alpha (intercept)]
    Updated recursively at each price observation.
    Dynamic hedge ratio outperforms static OLS in volatile crypto.

    Y_t = beta_t * X_t + alpha_t + noise
    State transition: [beta, alpha] = [beta, alpha] + state_noise

    Z-score based on Kalman-filtered spread (not static mean).
    Z > +2.0 = spread too high = mean reversion expected
    Z < -2.0 = spread too low  = mean reversion expected
    """

    def __init__(self):
        self._state  = {}   # {label: {beta, alpha, P, z, ts}}
        self._cache_s = 60  # 60s

    def update(self, prices_x: list, prices_y: list, label: str):
        """Run Kalman Filter on price pair. X = independent, Y = dependent."""
        if len(prices_x) < 30 or len(prices_y) < 30:
            return
        if time.time() - self._state.get(label, {}).get('ts', 0) < self._cache_s:
            return
        try:
            import numpy as np
            n  = min(len(prices_x), len(prices_y))
            px = np.array(prices_x[-n:], dtype=float)
            py = np.array(prices_y[-n:], dtype=float)

            # Kalman Filter initialization
            # State: [beta, alpha] — hedge ratio and intercept
            beta  = py[0] / px[0] if px[0] != 0 else 1.0
            alpha = 0.0
            P     = np.eye(2) * 10.0   # state covariance (uncertainty)
            Q     = np.eye(2) * 0.001  # process noise
            R     = np.var(py) * 0.01  # measurement noise

            spreads = []
            for i in range(n):
                x_t = np.array([[px[i]], [1.0]])  # observation vector
                y_t = py[i]

                # Predict
                y_hat   = float(x_t.T @ np.array([[beta], [alpha]]))
                P_pred  = P + Q

                # Innovation (prediction error)
                innov = y_t - y_hat
                S     = float(x_t.T @ P_pred @ x_t) + R

                # Kalman Gain
                K = P_pred @ x_t / S

                # Update state
                state = np.array([[beta], [alpha]]) + K * innov
                beta  = float(state[0, 0])
                alpha = float(state[1, 0])
                P     = (np.eye(2) - K @ x_t.T) @ P_pred

                # Record spread
                spread = y_t - (beta * px[i] + alpha)
                spreads.append(spread)

            # Z-score of current spread
            sp_arr = np.array(spreads)
            mean   = sp_arr.mean()
            std    = sp_arr.std()
            z      = float((sp_arr[-1] - mean) / std) if std > 0 else 0.0

            self._state[label] = {
                'beta': round(beta, 6),
                'alpha': round(alpha, 6),
                'z': round(z, 3),
                'spread': round(spreads[-1], 6),
                'ts': time.time()
            }
        except Exception as e:
            log.debug(f"KalmanPairs {label}: {e}")

    def get_z(self, label: str) -> float:
        return self._state.get(label, {}).get('z', 0.0)

    def get_boost(self, label: str) -> float:
        z = abs(self.get_z(label))
        if z > 2.5: return 0.83    # extreme divergence = regime stress
        if z > 2.0: return 0.91    # elevated spread
        if z < 0.5: return 1.06    # tight spread = stable market
        return 1.0

    def should_block(self, label: str) -> bool:
        return abs(self.get_z(label)) > 3.0

    def status(self) -> str:
        out = []
        for k, v in self._state.items():
            out.append(f"{k}:Z={v['z']:+.1f}(β={v['beta']:.3f})")
        return " ".join(out) if out else "Kalman:no data"



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v15.1: Token Unlock Tracker
# Blocks buying coins with major supply unlocks imminent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TokenUnlockTracker:
    """v15.1: Tracks upcoming token unlock/vesting events.
    Major cliff unlocks (>5% of circulating supply) cause 5-15% price drops.
    Blocks buying within 14 days of a major unlock.
    Uses hardcoded known unlock calendar + high-vesting coin list.
    Refreshes every 6 hours."""

    # Known major unlock events (date, symbol_base, pct_of_supply, description)
    KNOWN_UNLOCKS = [
        # Format: ("YYYY-MM-DD", "SYMBOL_BASE", pct_supply, "description")
        # Add entries as they become known from tokenunlocks.app
    ]

    # Coins with aggressive vesting schedules (permanent caution, -10% conf)
    HIGH_VESTING_COINS = {"APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "TIAUSDT",
                          "SEIUSDT", "JUPUSDT", "WUSDT", "STRKUSDT", "ZETAUSDT"}

    def __init__(self):
        self._cache = {}
        self._ts = 0
        self._cache_sec = 21600  # 6 hours

    def update(self):
        if time.time() - self._ts < self._cache_sec:
            return
        try:
            from datetime import datetime as _dt
            now = _dt.now()
            for date_str, sym, pct, desc in self.KNOWN_UNLOCKS:
                try:
                    unlock_date = _dt.strptime(date_str, "%Y-%m-%d")
                    days_until = (unlock_date - now).days
                    if 0 <= days_until <= 30:
                        self._cache[sym + "USDT"] = {
                            "date": date_str, "pct": pct, "days": days_until,
                            "blocked": days_until <= 14 and pct >= 5.0,
                            "desc": desc, "ts": time.time()
                        }
                except Exception:
                    continue
            self._ts = time.time()
            blocked = [s for s, d in self._cache.items() if d.get("blocked")]
            if blocked:
                log.info(f"🔓 TokenUnlock: {len(blocked)} coins blocked — {blocked}")
        except Exception as e:
            log.debug(f"TokenUnlock update: {e}")

    def should_block(self, symbol):
        data = self._cache.get(symbol, {})
        return data.get("blocked", False)

    def get_boost(self, symbol):
        if symbol in self.HIGH_VESTING_COINS:
            return 0.90
        data = self._cache.get(symbol, {})
        days = data.get("days", 999)
        if days <= 7: return 0.70
        if days <= 14: return 0.85
        if days <= 30: return 0.93
        return 1.0

    def status(self):
        blocked = sum(1 for d in self._cache.values() if d.get("blocked"))
        return f"UNLOCK:{blocked}blk"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v15.1: Economic Calendar — FOMC/CPI/NFP Event Blocking
# Prevents entering positions during high-volatility macro events
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EconomicCalendar:
    """v15.1: Hardcoded major US macro event dates for 2026.
    FOMC rate decisions cause 3-8% BTC moves within minutes.
    CPI releases cause 2-5% volatility spikes.
    Blocks new entries within 2 hours before and 30 min after events.
    Dates from Federal Reserve and BLS published schedules."""

    # (month, day, hour_utc, event_name)
    EVENTS_2026 = [
        # FOMC Rate Decisions (2:00 PM ET = 18:00/19:00 UTC)
        (1, 29, 19, "FOMC"), (3, 18, 18, "FOMC"), (5, 6, 18, "FOMC"),
        (6, 17, 18, "FOMC"), (7, 29, 18, "FOMC"), (9, 16, 18, "FOMC"),
        (11, 4, 19, "FOMC"), (12, 16, 19, "FOMC"),
        # CPI Releases (8:30 AM ET = 12:30/13:30 UTC)
        (1, 14, 13, "CPI"), (2, 12, 13, "CPI"), (3, 12, 12, "CPI"),
        (4, 10, 12, "CPI"), (5, 13, 12, "CPI"), (6, 10, 12, "CPI"),
        (7, 15, 12, "CPI"), (8, 12, 12, "CPI"), (9, 10, 12, "CPI"),
        (10, 13, 12, "CPI"), (11, 12, 13, "CPI"), (12, 10, 13, "CPI"),
        # NFP Employment (8:30 AM ET)
        (1, 10, 13, "NFP"), (2, 7, 13, "NFP"), (3, 6, 13, "NFP"),
        (4, 3, 12, "NFP"), (5, 2, 12, "NFP"), (6, 5, 12, "NFP"),
        (7, 2, 12, "NFP"), (8, 7, 12, "NFP"), (9, 4, 12, "NFP"),
        (10, 2, 12, "NFP"), (11, 6, 13, "NFP"), (12, 4, 13, "NFP"),
    ]

    def __init__(self):
        self._next_event = None
        self._next_event_time = None
        self._blocked = False
        self._ts = 0

    def update(self):
        if time.time() - self._ts < 60:
            return
        try:
            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc)
            year = now.year
            closest_event = None
            closest_minutes = float('inf')
            for month, day, hour, name in self.EVENTS_2026:
                try:
                    event_time = _dt(year, month, day, hour, 0, tzinfo=_tz.utc)
                    diff_minutes = (event_time - now).total_seconds() / 60
                    if diff_minutes > -30 and abs(diff_minutes) < abs(closest_minutes):
                        closest_event = name
                        closest_minutes = diff_minutes
                        self._next_event_time = event_time
                except Exception:
                    continue
            self._next_event = closest_event
            if closest_event and -30 <= closest_minutes <= 120:
                if not self._blocked:
                    log.warning(f"⚠️ MACRO EVENT: {closest_event} in {closest_minutes:.0f} min — BLOCKING new entries")
                self._blocked = True
            else:
                self._blocked = False
            self._ts = time.time()
        except Exception as e:
            log.debug(f"EconCalendar: {e}")

    def should_block(self):
        return self._blocked

    def get_next_event(self):
        if self._next_event and self._next_event_time:
            from datetime import datetime as _dt, timezone as _tz
            now = _dt.now(_tz.utc)
            diff = self._next_event_time - now
            hours = diff.total_seconds() / 3600
            if hours > 0:
                return f"{self._next_event} in {hours:.1f}h"
        return ""

    def status(self):
        if self._blocked:
            return f"MACRO:🔴{self._next_event}"
        evt = self.get_next_event()
        return f"MACRO:{evt}" if evt else "MACRO:clear"


class Context:
    def __init__(self):
        self.regime = "NORMAL"
        self.killzone = "NORMAL"
        self.news_score = 0.0
        self.daily = "NEUTRAL"
        self.h4 = "NEUTRAL"
        self.h1 = "NEUTRAL"
        self.fg = 50.0
        self.squeeze = False

class Intel:
    def __init__(self, cfg, ex):
        self.cfg = cfg
        self.ex = ex
        self.funding = FundingRateTracker()
        self.liq = LiquidationDetector()
        self.momentum = MomentumScanner()
        self.news = CryptoPanicNews()
        self.vpin = VPINTracker()
        
    def update(self):
        # Update background trackers safely
        for tracker in [self.funding, self.liq, self.news]:
            try: tracker.update()
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        
        try: 
            if hasattr(self, 'ex') and self.ex:
                self.momentum.update(symbols=["ETHUSDT", "SOLUSDT", "BNBUSDT"], exchange=self.ex)
        except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")

    def context(self, heat=None):
        ctx = Context()
        
        # 1. Fetch Real Fear & Greed
        try:
            import urllib.request, json
            req = urllib.request.Request("https://api.alternative.me/fng/?limit=1", headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read().decode())
            ctx.fg = float(data['data'][0]['value'])
        except Exception:
            ctx.fg = 50.0 # Fallback
            
        # 2. Inject News Score
        ctx.news_score = getattr(self.news, '_global_score', 0.0)

        # v14.6.5 AUDIT FIX (F20+F21): previously ctx.regime was derived solely
        # from BTC 15m momentum, and ctx.daily/h4/h1 were never populated — leaving
        # the ta_score (0-4) used by TREND/BREAKOUT/SMC_OB strategies permanently
        # capped at 0-1. This effectively starved those strategies of signal.
        # Now: use TA.regime_detect() on BTC 5m candles for richer regime, and
        # set daily/h4/h1 from BTC 1d/4h/1h trend (EMA-cross + slope).
        try:
            from indicators import TA as _TA
        except Exception:
            _TA = None
        btc_mom = getattr(self.momentum, '_btc_momentum', 0.0)
        _regime_set = False
        if _TA is not None and getattr(self, 'ex', None) is not None:
            # v15.3 FIX: V15.2 made self.ex.klines() ASYNC. Intel.context() is sync.
            # Calling `self.ex.klines(...)` here returned a coroutine which was
            # then passed into TA.regime_detect / _trend_label as if it were a
            # candle list — TypeError on iteration, caught by the bare except.
            # F21 was silently dead. Now: use the sync python-binance client
            # (self.ex.cl, the same one NativeSL and portfolio_alloc use) and
            # wrap rows into Candle objects locally. Null-guard handles boot
            # races where cl isn't ready yet.
            try:
                from models import Candle as _Candle
            except Exception:
                _Candle = None

            def _sync_klines(symbol, interval, limit):
                _cl = getattr(self.ex, 'cl', None)
                if _cl is None or _Candle is None:
                    return []
                try:
                    raw = _cl.get_klines(symbol=symbol, interval=interval, limit=limit)
                    return [_Candle(k[0]/1000, float(k[1]), float(k[2]),
                                    float(k[3]), float(k[4]), float(k[5])) for k in raw]
                except Exception:
                    return []

            try:
                _c5 = _sync_klines("BTCUSDT", "5m", 120)
                if _c5 and len(_c5) >= 60:
                    _rd = _TA.regime_detect(_c5)
                    # regime_detect can return a string or a tuple — normalize
                    if isinstance(_rd, tuple): _rd = _rd[0]
                    if isinstance(_rd, str) and _rd:
                        ctx.regime = _rd
                        _regime_set = True
            except Exception as _re:
                log.debug(f"regime_detect skipped: {_re}")
            # Higher-timeframe trends — used by ta_score scoring in strategies.py
            def _trend_label(candles, n_fast=10, n_slow=30):
                try:
                    if not candles or len(candles) < n_slow + 5:
                        return "NEUTRAL"
                    closes = [c.c for c in candles]
                    ef = _TA.ema(closes, n_fast)[-1]
                    es = _TA.ema(closes, n_slow)[-1]
                    if ef > es * 1.002: return "BULL"
                    if ef < es * 0.998: return "BEAR"
                    return "NEUTRAL"
                except Exception:
                    return "NEUTRAL"
            try:
                ctx.daily = _trend_label(_sync_klines("BTCUSDT", "1d", 60))
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
            try:
                ctx.h4 = _trend_label(_sync_klines("BTCUSDT", "4h", 60))
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
            try:
                ctx.h1 = _trend_label(_sync_klines("BTCUSDT", "1h", 60))
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        # Fallback: momentum-only regime when TA / exchange unavailable
        if not _regime_set:
            if btc_mom > 1.0:
                ctx.regime = "TREND_UP"
            elif btc_mom < -1.0:
                ctx.regime = "TREND_DOWN"
            elif btc_mom > 0.3 or btc_mom < -0.3:
                ctx.regime = "CHOPPY"
            else:
                ctx.regime = "RANGE"

        return ctx

# v19.0.4: AzureOpenAIIntelligence removed entirely — no API key on the VM (always returned 0),
# required the heavy `openai` SDK, and was dead weight on the lite VM. Global news sentiment is
# handled by news.py (NewsSentiment, free RSS, no key) + CryptoPanicNews.

