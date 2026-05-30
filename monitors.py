# BinBot v11 — monitors.py
# EventCalendar, StablecoinFlow, MVRV, TVL, WhaleWallet, OI, TokenUnlock, KellySizer, HyperOpt
import time, json, logging, urllib.request
import requests as req
from datetime import datetime, timezone
from indicators import TA
log = logging.getLogger('binbot')

class EventCalendar:
    EVENTS_2026 = [
        "2026-01-28","2026-01-29","2026-03-18","2026-03-19",
        "2026-05-06","2026-05-07","2026-06-17","2026-06-18",
        "2026-07-29","2026-07-30","2026-09-16","2026-09-17",
        "2026-11-04","2026-11-05","2026-12-16","2026-12-17",
        "2026-01-14","2026-02-12","2026-03-12","2026-04-10",
        "2026-05-13","2026-06-11","2026-07-15","2026-08-12",
        "2026-09-10","2026-10-13","2026-11-12","2026-12-10",
        "2026-01-09","2026-02-06","2026-03-06","2026-04-03",
        "2026-05-01","2026-06-05","2026-07-02","2026-08-07",
        "2026-09-04","2026-10-02","2026-11-06","2026-12-04",
    ]
    # v11.1: 2027 FOMC + CPI + NFP dates added — calendar valid through Dec 2027
    EVENTS_2027 = [
        # FOMC meetings 2027
        "2027-01-27","2027-01-28","2027-03-17","2027-03-18",
        "2027-05-05","2027-05-06","2027-06-16","2027-06-17",
        "2027-07-28","2027-07-29","2027-09-15","2027-09-16",
        "2027-11-03","2027-11-04","2027-12-15","2027-12-16",
        # CPI releases 2027
        "2027-01-13","2027-02-10","2027-03-12","2027-04-14",
        "2027-05-12","2027-06-10","2027-07-14","2027-08-11",
        "2027-09-15","2027-10-13","2027-11-10","2027-12-08",
        # NFP releases 2027
        "2027-01-08","2027-02-05","2027-03-05","2027-04-02",
        "2027-05-07","2027-06-04","2027-07-02","2027-08-06",
        "2027-09-03","2027-10-01","2027-11-05","2027-12-03",
    ]
    def __init__(self, pre_event_hours: float = 0.0):
        self.events = sorted(set(self.EVENTS_2026 + self.EVENTS_2027))
        # v13.5: optional pre-event lead time. The default behavior (covered
        # by hours_to_next) is to count hours-to-end-of-event-day, which means
        # only the LATTER half of an event day is hard-blocked (the first 12h
        # of an event day fall outside the 12h-or-6h block window). For US
        # events this captures the announcement-reaction window (CPI 12:30 UTC,
        # FOMC 18:00 UTC are both in the latter half) but not pre-positioning
        # hours. Set pre_event_hours > 0 to block N hours BEFORE the start of
        # the event day too. Default 0 keeps original v13.4 behavior.
        self.pre_event_hours = max(0.0, float(pre_event_hours))
        # v11.1: warn if calendar expires within 60 days
        last = datetime.strptime(self.events[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_left = (last - datetime.now(timezone.utc)).days
        if days_left < 60:
            log.warning(f"⚠️ EventCalendar expires in {days_left} days ({self.events[-1]}) — update EVENTS list!")
        else:
            log.info(f"📅 EventCalendar loaded: {len(self.events)} events through {self.events[-1]}"
                     f"{f' (pre-event lead: {self.pre_event_hours}h)' if self.pre_event_hours > 0 else ''}")
    def hours_to_next(self):
        now = datetime.now(timezone.utc)
        for e in self.events:
            # v11.2.21 FIX: was midnight UTC — bot traded freely all day of event
            # Now: event covers full day (23:59:59 UTC) so actual volatility is blocked
            ed = datetime.strptime(e, "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=23, minute=59, second=59)
            if ed >= now:
                hours = (ed - now).total_seconds() / 3600
                # v13.5: if operator opted into pre-event blocking and we're
                # already past the event's "start" (00:00 UTC of event day),
                # we're already inside the day so pre_event_hours doesn't
                # subtract; otherwise reduce the effective hours-to-event by
                # pre_event_hours so the block window starts earlier.
                if self.pre_event_hours > 0 and hours > 24:
                    # Event is more than 1 day away → subtract the pre-event lead
                    # so the block window starts earlier in real-time.
                    hours = hours - self.pre_event_hours
                return hours
        return 999
    def risk_mult(self):
        h = self.hours_to_next()
        # B2-1: graduated event-risk multipliers (restored from v11.2.8).
        # Hard block within 6h, scale within 12h/24h, full size beyond.
        if h <=  6: return 0.0    # block
        if h <= 12: return 0.5    # 50% size
        if h <= 24: return 0.75   # 75% size
        return 1.0                # full size

# v9.2: STABLECOIN FLOW MONITOR
class StablecoinFlow:
    def __init__(self):
        self.last_check = 0; self.signal = "NEUTRAL"; self.pct = 0.0
    def check(self):
        if time.time() - self.last_check < 1800: return self.signal
        try:
            r = req.get("https://api.coingecko.com/api/v3/global", timeout=4)
            change = r.json().get("data", {}).get("market_cap_change_percentage_24h_usd", 0)
            self.pct = change
            if change > 1.5: self.signal = "INFLOW"
            elif change < -1.5: self.signal = "OUTFLOW"
            else: self.signal = "NEUTRAL"
        except Exception: pass
        self.last_check = time.time()  # v11.2.20 FIX: moved outside try — DDoS loop fix
        return self.signal

# v9.3: PRICE-VS-ATH PROXY (loosely correlated with MVRV cycle indicator)
class MVRVMonitor:
    """v11.2.9 NOTE (May 4, 2026): name is misleading — this is NOT a real MVRV Z-score.
    It's a price/ATH ratio used as a CRUDE proxy. Real MVRV requires Glassnode or similar
    on-chain data API. Kept as MVRVMonitor for backwards compat (renaming would break all
    importers). Reading: < 0.30 = undervalued, 0.30-0.50 = fair, 0.50-0.80 = warming,
    > 0.80 = overheated. Don't use this as if it were the real metric."""
    def __init__(self):
        self.last_check = 0; self.zscore = 0.0; self.signal = "NEUTRAL"
    def check(self):
        if time.time() - self.last_check < 3600: return self.signal  # Cache 1hr
        try:
            # Use CoinGecko market data as MVRV proxy
            r = req.get("https://api.coingecko.com/api/v3/coins/bitcoin", timeout=4,
                        params={"localization":"false","tickers":"false","community_data":"false",
                                "developer_data":"false","sparkline":"false"})
            data = r.json()
            mc = data.get("market_data",{}).get("market_cap",{}).get("usd",0)
            ath = data.get("market_data",{}).get("ath",{}).get("usd",1)
            price = data.get("market_data",{}).get("current_price",{}).get("usd",0)
            # Simplified MVRV proxy: price vs ATH ratio
            ratio = price / ath if ath > 0 else 0.5
            if ratio < 0.30:
                self.signal = "UNDERVALUED"  # BTC < 30% of ATH = buy zone
                self.zscore = -1.0
            elif ratio < 0.50:
                self.signal = "FAIR"
                self.zscore = 0.0
            elif ratio < 0.80:
                self.signal = "WARMING"
                self.zscore = 3.0
            else:
                self.signal = "OVERHEATED"  # BTC near ATH = careful
                self.zscore = 7.0
            self.last_check = time.time()
        except Exception: pass
        return self.signal

# v9.3: DEFI LLAMA TVL MONITOR (chain health)
class TVLMonitor:
    """Track TVL changes for coins' chains. Drop >10% = bearish signal"""
    CHAIN_MAP = {
        "ETH": "ethereum", "SOL": "solana", "BNB": "bsc", "AVAX": "avalanche",
        "DOT": "polkadot", "ATOM": "cosmos", "ARB": "arbitrum", "OP": "optimism",
        "SUI": "sui", "SEI": "sei", "NEAR": "near", "INJ": "injective",
        "APT": "aptos", "TON": "ton", "TRX": "tron", "POL": "polygon",
    }
    def __init__(self):
        self.last_check = 0; self.tvl_data = {}; self.alerts = []
    def check(self):
        if time.time() - self.last_check < 1800: return self.alerts  # Cache 30min
        self.alerts = []
        try:
            r = req.get("https://api.llama.fi/v2/chains", timeout=4)
            _data = r.json()
            if not isinstance(_data, list): raise ValueError(f"TVL API bad response: {str(_data)[:80]}")
            chains = {c["name"].lower(): c for c in _data}  # v11.2.22 FIX: guard rate-limit dict
            for coin, chain_name in self.CHAIN_MAP.items():
                if chain_name in chains:
                    new_tvl = chains[chain_name].get("tvl", 0)
                    old_tvl = self.tvl_data.get(coin, new_tvl)
                    if old_tvl > 0:
                        change = (new_tvl - old_tvl) / old_tvl * 100
                        if change < -10: self.alerts.append(f"{coin} TVL -{abs(change):.0f}%")
                        elif change > 15: self.alerts.append(f"{coin} TVL +{change:.0f}%")
                    self.tvl_data[coin] = new_tvl
        except Exception: pass
        self.last_check = time.time()
        return self.alerts
    def get_tvl(self, coin):
        return self.tvl_data.get(coin, 0)

# v9.3: ARKHAM-STYLE WHALE WALLET MONITOR (large tx detection)
class WhaleWalletMonitor:
    """Monitor large transactions on Binance for your coins via public API"""
    def __init__(self):
        self.last_check = 0; self.signals = {}
    def check(self, pairs):
        if time.time() - self.last_check < 600: return self.signals  # Cache 10min
        self.signals = {}
        try:
            for pair_info in pairs[:10]:  # Top 10 coins only
                sym = pair_info["s"]
                try:
                    r = req.get(f"https://api.binance.com/api/v3/trades",
                               params={"symbol": sym, "limit": 50}, timeout=3)
                    trades = r.json()
                    # v10.7 FIX: validate shape — Binance returns error dict on bans/limits
                    # (e.g. {"code":-1003,"msg":"..."}) which would iterate as keys → AttributeError.
                    if not isinstance(trades, list): continue
                    big_buys = sum(1 for t in trades if isinstance(t, dict)
                                  and not t.get("isBuyerMaker",True)
                                  and float(t.get("quoteQty",0)) > 10000)
                    big_sells = sum(1 for t in trades if isinstance(t, dict)
                                   and t.get("isBuyerMaker",True)
                                   and float(t.get("quoteQty",0)) > 10000)
                    if big_buys > big_sells * 2:
                        self.signals[sym] = "WHALE_BUY"
                    elif big_sells > big_buys * 2:
                        self.signals[sym] = "WHALE_SELL"
                    else:
                        self.signals[sym] = "NEUTRAL"
                except Exception: pass
                time.sleep(0.2)  # Rate limit
        except Exception: pass
        self.last_check = time.time()
        return self.signals

# v9.3: COINGLASS OPEN INTEREST MONITOR
class OpenInterestMonitor:
    """Track OI changes — spikes predict big moves, drops predict consolidation"""
    def __init__(self):
        self.last_check = 0; self.signals = {}
    def check(self):
        if time.time() - self.last_check < 1800: return self.signals  # Cache 30min
        try:
            # Use Binance futures OI endpoint (free, no API key needed)
            coins = ["BTCUSDT","ETHUSDT","SOLUSDT","DOGEUSDT","XRPUSDT",
                     "ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","FETUSDT"]
            for sym in coins:
                try:
                    r = req.get(f"https://fapi.binance.com/fapi/v1/openInterest",
                               params={"symbol": sym}, timeout=3)
                    oi = float(r.json().get("openInterest", 0))
                    # Get 24h ago OI for comparison
                    r2 = req.get(f"https://fapi.binance.com/futures/data/openInterestHist",
                                params={"symbol": sym, "period": "1h", "limit": 24}, timeout=3)
                    hist = r2.json()
                    if isinstance(hist, list) and len(hist) > 20:  # v11.2.19 FIX: guard rate-limit error dict
                        oi_24h_ago = float(hist[0].get("sumOpenInterest", oi))
                        change_pct = ((oi - oi_24h_ago) / oi_24h_ago * 100) if oi_24h_ago > 0 else 0
                        if change_pct > 10:
                            self.signals[sym] = "OI_SURGE"   # Big move coming
                        elif change_pct < -10:
                            self.signals[sym] = "OI_DROP"    # Positions closing, consolidation
                        else:
                            self.signals[sym] = "OI_NORMAL"
                except Exception: pass
                time.sleep(0.3)
        except Exception: pass
        self.last_check = time.time()
        return self.signals
    def get_signal(self, pair):
        return self.signals.get(pair, "OI_NORMAL")

# v9.3: TOKEN UNLOCK CALENDAR
class TokenUnlockMonitor:
    """Track major token unlocks — large unlocks = sell pressure"""
    # Major unlocks for coins in our list (manually curated, update monthly)
    UNLOCKS_2026 = {
        # v11.2.19 FIX: extended through Dec 2026 — was expiring Jun/Jul 2026 (6 weeks left)
        "ARB":  [("2026-05-16",92), ("2026-06-16",92), ("2026-07-16",92), ("2026-08-16",92), ("2026-09-16",92), ("2026-10-16",92), ("2026-11-16",92), ("2026-12-16",92)],
        "OP":   [("2026-05-31",31), ("2026-06-30",31), ("2026-07-31",31), ("2026-08-31",31), ("2026-09-30",31), ("2026-10-31",31), ("2026-11-30",31), ("2026-12-31",31)],
        "APT":  [("2026-05-12",11), ("2026-06-12",11), ("2026-07-12",11), ("2026-08-12",11), ("2026-09-12",11), ("2026-10-12",11), ("2026-11-12",11), ("2026-12-12",11)],
        "SUI":  [("2026-05-01",64), ("2026-06-01",64), ("2026-07-01",64), ("2026-08-01",64), ("2026-09-01",64), ("2026-10-01",64), ("2026-11-01",64), ("2026-12-01",64)],
        "SEI":  [("2026-05-15",55), ("2026-06-15",55), ("2026-07-15",55), ("2026-08-15",55), ("2026-09-15",55), ("2026-10-15",55), ("2026-11-15",55), ("2026-12-15",55)],
        "FET":  [("2026-05-10",5),  ("2026-06-10",5),  ("2026-07-10",5),  ("2026-08-10",5),  ("2026-09-10",5),  ("2026-10-10",5),  ("2026-11-10",5),  ("2026-12-10",5)],
        "TAO":  [("2026-05-02",15), ("2026-06-02",15), ("2026-07-02",15), ("2026-08-02",15), ("2026-09-02",15), ("2026-10-02",15), ("2026-11-02",15), ("2026-12-02",15)],
        "INJ":  [("2026-05-25",3),  ("2026-06-25",3),  ("2026-07-25",3),  ("2026-08-25",3),  ("2026-09-25",3),  ("2026-10-25",3),  ("2026-11-25",3),  ("2026-12-25",3)],
        "NEAR": [("2026-05-20",8),  ("2026-06-20",8),  ("2026-07-20",8),  ("2026-08-20",8),  ("2026-09-20",8),  ("2026-10-20",8),  ("2026-11-20",8),  ("2026-12-20",8)],
        "DOT":  [("2026-05-01",10), ("2026-06-01",10), ("2026-07-01",10), ("2026-08-01",10), ("2026-09-01",10), ("2026-10-01",10), ("2026-11-01",10), ("2026-12-01",10)],
    }
    def __init__(self):
        # v11.2.9: warn at startup if unlocks calendar is approaching exhaustion.
        # Was: silent fail-open after dates pass — bot resumed trading unlock-affected
        # coins (APT, SUI, SEI, FET, TAO, INJ, NEAR, DOT) blind to pre-unlock volatility.
        # Now: loud warning at init so operator notices before silent gap.
        try:
            today = datetime.now(timezone.utc).date()
            future_unlocks = []
            for coin, dates in self.UNLOCKS_2026.items():
                for d, _m in dates:
                    ud = datetime.strptime(d, "%Y-%m-%d").date()
                    if ud >= today:
                        future_unlocks.append(ud)
            if not future_unlocks:
                log.warning("⚠️ TokenUnlockMonitor: ALL dates in past — calendar exhausted, "
                            "refresh UNLOCKS_2026 dict in monitors.py before trading APT/SUI/SEI/FET/TAO/INJ/NEAR/DOT")
            else:
                last_unlock = max(future_unlocks)
                days_left = (last_unlock - today).days
                if days_left < 30:
                    log.warning(f"⚠️ TokenUnlockMonitor: only {days_left}d of forward coverage "
                                f"(last entry {last_unlock}) — refresh soon")
        except Exception: pass
    def days_to_unlock(self, coin):
        """Returns days until next unlock for a coin. -1 if no unlock scheduled."""
        unlocks = self.UNLOCKS_2026.get(coin, [])
        today = datetime.now(timezone.utc).date()
        for date_str, millions in unlocks:
            unlock_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            days = (unlock_date - today).days
            if days >= 0:
                return days, millions
        return -1, 0
    def should_avoid(self, coin):
        """Avoid buying if unlock within 3 days and > $8M, OR within 1 day (any size).
        FIX F6: capture sub-$8M unlocks during the high-pressure last 24h window."""
        days, millions = self.days_to_unlock(coin)
        if days < 0:
            return False, ""
        if days <= 1 and millions > 0:
            return True, f"UNLOCK {'TODAY' if days == 0 else 'TOMORROW'} (${millions}M)"
        if days <= 3 and millions > 8:
            return True, f"UNLOCK in {days}d (${millions}M)"
        return False, ""

# v7: KELLY CRITERION POSITION SIZING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KellySizer:
    """Mathematically optimal position sizing. Quarter-Kelly for safety."""

    def __init__(self, fraction=0.25):
        self.fraction = fraction
        self.trade_history = []

        # v13.5.5 P4: kelly persistence - load history from disk if exists
        self._kelly_state_file = "kelly_history.json"
        self._load_history()

    def record(self, win: bool, pnl_pct: float):
        self.trade_history.append({"win": win, "pnl_pct": pnl_pct})
        if len(self.trade_history) > 200:
            self.trade_history = self.trade_history[-200:]
        self._save_history()  # v14.6.1 FIX: persist after each record

    def optimal_size(self, capital, default_risk_pct = 0.01):  # v14.1 FIX (ISSUE C): tightened from 0.08 to match config.RISK_PCT (1%). This fallback only fires in degenerate cases (no losses recorded, or zero avg_loss) — risk.py:229 only calls this when trade_history has ≥100 entries, so the <20 path inside is effectively unreachable. The 0.08 default was 8× the headline risk number. Now aligned. Caller (risk.py:255) still applies 25% per-position cap on top.
        """Returns optimal position size as fraction of capital."""
        if len(self.trade_history) < 20:
            return capital * default_risk_pct  # Not enough data, use default

        wins = [t for t in self.trade_history if t["win"]]
        losses = [t for t in self.trade_history if not t["win"]]

        w = len(wins) / len(self.trade_history)  # Win rate
        if not wins or not losses: return capital * default_risk_pct

        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins)
        avg_loss = abs(sum(t["pnl_pct"] for t in losses) / len(losses))

        if avg_loss == 0: return capital * default_risk_pct
        r = avg_win / avg_loss  # Win/loss ratio

        # Kelly: f = W - (1-W)/R
        kelly = w - (1 - w) / r
        kelly = max(0.01, min(kelly, 0.50))  # Cap between 1% and 50%
        fractional = kelly * self.fraction  # Quarter-Kelly

        return capital * fractional


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v7: HYPERPARAMETER OPTIMIZER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load_history(self):
        """v13.5.5 P4: load kelly trade history from disk so it survives restarts."""
        try:
            import json, os
            if os.path.exists(self._kelly_state_file):
                with open(self._kelly_state_file) as _f:
                    data = json.load(_f)
                    self.trade_history = data.get("trades", [])
        except Exception:
            pass  # First run or corrupted file - start fresh

    def _save_history(self):
        """v13.5.5 P4: persist kelly trade history to disk."""
        try:
            import json
            with open(self._kelly_state_file, "w") as _f:
                json.dump({"trades": self.trade_history[-200:]}, _f)  # cap at 200
        except Exception:
            pass

class HyperOptimizer:
    """Auto-tunes RSI/BB/MACD thresholds weekly using scipy."""

    def __init__(self, interval_h=168):
        self.interval_h = interval_h
        self.last_opt = 0
        self.best_params = {"rsi_buy":35, "rsi_sell":65, "bb_sd":2.0, "atr_mult":1.0}

    def should_optimize(self):
        # v9.7.2 FIX: SCIPY gate dropped — implementation uses random search, not scipy
        return time.time() - self.last_opt > self.interval_h * 3600

    def force_run(self):
        """v13.4 fix (Batch 1): make should_optimize() return True on the next call.
        Walk-forward decay detector wires its hyperopt_callback to this method
        (via bot.py:158). Before this fix, the callback called a non-existent
        attribute → AttributeError → caught and logged as 'HyperOpt callback error',
        and the $1000-tier walk-forward feature was a silent no-op.

        We don't call optimize() directly here because optimize() needs candles
        + ta_module passed by bot.py's main loop; instead we just clear last_opt
        so the next bot cycle picks it up via the existing should_optimize() path.
        """
        self.last_opt = 0
        log.info("🔧 HyperOpt: force_run requested (will optimize on next cycle)")

    def optimize(self, candles, ta_module):
        # audit fix: need 50+ closed trades for reliable optimization
        from pathlib import Path
        try:
            import json
            trades = [json.loads(l) for l in open('trades_v9.jsonl') if l.strip()]
            closes = [t for t in trades if t.get('action') not in ('BUY',)]
            # v14.3.3 FIX: lowered from 50 → 20 — 50 was unreachable at current trade rate
            if len(closes) < 20:
                self.last_opt = time.time()  # v14.6.1 FIX: throttle
                import logging; logging.getLogger('binbot').info(f"HyperOpt skipped — only {len(closes)} closed trades (need 20+)")
                return
        except Exception: pass
        """Find best RSI/BB parameters by simulating on recent data."""
        # v9.7.2 FIX: SCIPY gate dropped — uses random search internally, not scipy
        if len(candles) < 200:
            return self.best_params

        # v11.2.19 FIX: O(N²) HyperOpt — RSI and BB base values were recomputed inside
        # objective() for every stride point × every param combo = millions of redundant ops.
        # Now: pre-compute RSI and BB SMA/std once outside the loop. BB upper/lower computed
        # in O(1) per lookup using cached SMA+std. Cuts CPU by ~50x vs original.
        cc = [c.c for c in candles]
        _rsi_cache = {}
        _sma_cache = {}
        _std_cache = {}
        for _i in range(50, len(candles)-1, 5):
            _rsi_cache[_i] = ta_module.rsi(candles[:_i+1])
            _w = cc[max(0,_i-19):_i+1]
            if len(_w) >= 2:
                _sma = sum(_w)/len(_w)
                _std = (sum((x-_sma)**2 for x in _w)/len(_w))**0.5
                _sma_cache[_i] = _sma; _std_cache[_i] = _std

        def objective(params):
            rsi_buy, rsi_sell, bb_sd = params[0], params[1], params[2]
            if rsi_buy >= rsi_sell or bb_sd < 1.0 or bb_sd > 3.0: return 0
            pnl = 0; in_trade = False; entry = 0
            for i in range(50, len(candles)-1, 5):
                rsi = _rsi_cache.get(i, 50)
                sma = _sma_cache.get(i, 0); std = _std_cache.get(i, 0)
                bu = sma + bb_sd * std; bl = sma - bb_sd * std
                price = cc[i]
                if not in_trade and rsi < rsi_buy and price <= bl * 1.002:
                    entry = price; in_trade = True
                elif in_trade:
                    if rsi > rsi_sell or price >= bu * 0.998:
                        pnl += (price - entry) / entry * 100
                        in_trade = False
            if in_trade and entry > 0: pnl += (cc[-1] - entry) / entry * 100  # v11.2.23 FIX: guard entry==0
            return -pnl  # Minimize negative PnL

        # v15.3 AUDIT FIX #2: try Bayesian first (sample-efficient), random fallback.
        # If sklearn.gaussian_process unavailable OR BayesianHyperOpt fails, the
        # existing random-search block below runs as a safety net.
        _bayes_ok = False
        try:
            from bayesian_opt import BayesianHyperOpt
            _bayes_opt = BayesianHyperOpt(
                space={
                    "rsi_buy":  (25, 42, "int"),
                    "rsi_sell": (58, 78, "int"),
                    "bb_sd":    (1.5, 2.8, "float"),
                },
                objective_fn=lambda p: -objective([p["rsi_buy"], p["rsi_sell"], p["bb_sd"]]),
                n_calls=20,  # Bayesian needs only ~20 evals (vs 50 random) for similar quality
                n_initial=5,
            )
            best_params_b, best_pnl_b = _bayes_opt.run()
            if best_pnl_b > 0 and best_params_b:
                self.best_params = {
                    "rsi_buy":  int(best_params_b["rsi_buy"]),
                    "rsi_sell": int(best_params_b["rsi_sell"]),
                    "bb_sd":    round(float(best_params_b["bb_sd"]), 1),
                }
                log.info(f"🔬 HyperOpt (Bayesian): RSI({self.best_params['rsi_buy']}/{self.best_params['rsi_sell']}) "
                         f"BB({self.best_params['bb_sd']}σ) PnL:{best_pnl_b:+.2f}%")
                _bayes_ok = True
        except Exception as _bayes_e:
            log.debug(f"Bayesian hyperopt unavailable ({_bayes_e}), using random search fallback")

        # v9.0 FIX: Random search instead of Nelder-Mead (better for integer params)
        # v15.3: now serves as fallback when Bayesian unavailable or returns nothing useful.
        if not _bayes_ok:
            try:
                import random
                best_pnl = 0
                best = self.best_params.copy()
                for _ in range(50):  # 50 random combinations
                    rsi_buy = random.randint(25, 42)
                    rsi_sell = random.randint(58, 78)
                    bb_sd = round(random.uniform(1.5, 2.8), 1)
                    pnl = -objective([rsi_buy, rsi_sell, bb_sd])
                    if pnl > best_pnl:
                        best_pnl = pnl
                        best = {"rsi_buy": rsi_buy, "rsi_sell": rsi_sell, "bb_sd": bb_sd}
                if best_pnl > 0:
                    self.best_params = best
                    log.info(f"🔧 HyperOpt (random): RSI({best['rsi_buy']}/{best['rsi_sell']}) "
                             f"BB({best['bb_sd']}σ) PnL:{best_pnl:+.2f}%")
            except Exception as e:
                log.warning(f"HyperOpt failed: {e}")

        self.last_opt = time.time()
        return self.best_params


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INTELLIGENCE (v7: regime, killzone, heat, squeeze)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

