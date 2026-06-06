# BinBot v11 — analytics.py
import os, json, time, logging
from datetime import datetime, timezone
from collections import deque
from indicators import TA
log = logging.getLogger('binbot')

class SelfHealer:
    """v7.2: Auto-detect and recover from errors.
    v11.2.9: persist last-WS-restart timestamp to disk so cooldown survives bot restarts
    (prevents restart loops if WS is broken AND bot also crashes)."""

    _RESTART_FILE = "selfhealer_state.json"

    def __init__(self):
        self.errors = deque(maxlen=50)
        self.stale_count = 0
        self.last_price_time = time.time()
        self.last_prices = {}
        self.recovery_count = 0
        # v11.2.9: restore last-restart timestamp from disk if present
        try:
            if os.path.exists(self._RESTART_FILE):
                with open(self._RESTART_FILE) as f:
                    self._last_ws_restart = json.load(f).get("last_ws_restart", 0)
            else:
                self._last_ws_restart = 0
        except Exception:
            self._last_ws_restart = 0

    def check_health(self, tickers, ws, ex):
        """Run health checks and auto-recover."""
        issues = []

        # Check 1: Stale WebSocket data
        ws_issues = []
        if not ws.is_active:
            ws_issues.append("WebSocket is dead (_running=False)")
        else:
            now = time.time()
            # v10.6 FIX: snapshot under lock — was iterating live dict while WS thread
            # mutated it under self._lock, raising RuntimeError mid-cycle.
            try:
                with ws._lock:
                    snapshot = list(ws.last_update.items())
            except Exception:
                snapshot = []
            for sym, t in snapshot:
                if now - t > 180:  # v14.6.3: raised 60s→180s for low-volume Group D coins
                    ws_issues.append(f"Stale WS: {sym}")

        if ws_issues:
            issues.extend(ws_issues)
            self.stale_count += 1
            if self.stale_count >= 3:
                # v9.7.6 FIX: cooldown to prevent restart loops
                # v11.2.9: cooldown timestamp now persisted (see __init__)
                _now = time.time()
                _last = getattr(self, "_last_ws_restart", 0)
                if _now - _last < 120:  # v14.6.3: raised cooldown 60s→120s
                    log.info(f"🔧 Self-heal SKIP — cooldown ({int(120 - (_now - _last))}s left)")
                    self.stale_count = 0
                else:
                    log.warning("🔧 Self-heal: Restarting WebSocket")
                    try:
                        ws.stop()
                        time.sleep(2)
                        ws.start(ex.cfg.API_KEY, ex.cfg.API_SECRET)
                        self.stale_count = 0
                        self.recovery_count += 1
                        self._last_ws_restart = _now
                        # v11.2.9: persist so a crash mid-WS-restart doesn't reset cooldown
                        try:
                            with open(self._RESTART_FILE + ".tmp", "w") as f:
                                json.dump({"last_ws_restart": _now}, f)
                            os.replace(self._RESTART_FILE + ".tmp", self._RESTART_FILE)
                        except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
                    except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        else:
            self.stale_count = 0

        # Check 2: Prices not changing (exchange down?)
        if tickers:
            unchanged = 0
            for sym, price in tickers.items():
                if sym in self.last_prices and price == self.last_prices[sym]:
                    unchanged += 1
            if len(tickers) > 0 and unchanged / len(tickers) > 0.8:
                issues.append("80%+ prices unchanged — possible data issue")
            self.last_prices = dict(tickers)

        # Check 3: Too many errors in short time
        recent_errors = sum(1 for e in self.errors if time.time() - e < 300)
        if recent_errors >= 5:
            issues.append(f"{recent_errors} errors in 5min — throttling")

        return issues

    def record_error(self, error_msg):
        self.errors.append(time.time())
        log.error(f"🔧 Error #{len(self.errors)}: {error_msg}")




class TradeJournal:
    """v7.2: Learn from own trades — which strategies/pairs/times work best."""

    def __init__(self, log_file):
        self.log_file = log_file
        self.history = self._load()

    def _load(self):
        if not os.path.exists(self.log_file): return []
        try:
            with open(self.log_file) as f:
                content = f.read().strip()
            if not content: return []
            # v9.0: Support both JSONL (new) and JSON (old) formats
            if content.startswith('['):  # Old JSON array format
                try:
                    return json.loads(content)
                except Exception as e:
                    log.warning(f"TradeJournal: legacy JSON corrupted ({e}) — starting fresh")
                    return []
            # New JSONL format (one JSON per line)
            # v10.7 FIX: per-line try/except so one truncated/corrupt line (e.g.
            # from a power-loss mid-write) doesn't wipe entire trade history and
            # reset all ML strategy weights. Skip bad lines, keep good ones.
            result = []
            skipped = 0
            for line in content.splitlines():
                line = line.strip()
                if not line: continue
                try:
                    result.append(json.loads(line))
                except Exception:
                    skipped += 1
            if skipped:
                log.warning(f"TradeJournal: {skipped} corrupted line(s) skipped, "
                            f"{len(result)} valid trades loaded")
            return result
        except Exception as e:
            log.error(f"TradeJournal load failed: {e}")
            return []

    def strategy_performance(self):
        """Returns {strategy: {wins, losses, avg_pnl, best_pair, best_hour}}

        v13.5.3 audit Bug #21: was counting BUY entries (which always have pnl=0)
        as losses via the `else: losses += 1` branch. Result: every strategy's
        WR was halved (e.g. SMC_OB+FVG showed ~22.7% instead of real 45.5%, but
        SMC_OB showed ~45% instead of real 90.9%). strategy_weight() then over-
        penalized every winning strategy. Now: skip BUYs entirely; only count
        actual close events. Aligned with review.py:25 which already does this.
        """
        # Same close-action set used in audit_wallet.py and review.py.
        # Add new actions here if risk._record_close ever logs new reasons.
        CLOSE_ACTIONS = {"TP", "SL", "TIME", "TIME_MAX", "GHOST", "CRASH",
                         "CRASH_STUCK", "DUST", "FORCE_CLOSE", "TRAIL",
                         "REGIME", "SCALE", "CLOSE", "VELOCITY_EXIT"}
        stats = {}
        for t in self.history:
            if t.get("action") not in CLOSE_ACTIONS:
                continue  # skip BUYs (pnl=0 by definition) and any non-close entry
            s = t.get("strategy", "unknown")
            if s not in stats:
                stats[s] = {"wins":0, "losses":0, "total_pnl":0, "trades":0, "pairs":{}, "hours":{}}
            stats[s]["trades"] += 1
            pnl = t.get("pnl", 0)
            stats[s]["total_pnl"] += pnl
            if pnl > 0: stats[s]["wins"] += 1
            elif pnl < 0: stats[s]["losses"] += 1
            # zero-PnL closes (e.g. exact BE-stop) count as a trade but neither W/L
            # Track by pair
            pair = t.get("pair", "")
            if pair not in stats[s]["pairs"]: stats[s]["pairs"][pair] = 0
            stats[s]["pairs"][pair] += pnl
            # Track by hour
            try:
                hr = datetime.fromisoformat(t.get("ts","")).hour
                if hr not in stats[s]["hours"]: stats[s]["hours"][hr] = 0
                stats[s]["hours"][hr] += pnl
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        return stats

    def best_pairs(self, top_n=5):
        """Returns top N pairs by total PnL."""
        pair_pnl = {}
        for t in self.history:
            p = t.get("pair", "")
            pair_pnl[p] = pair_pnl.get(p, 0) + t.get("pnl", 0)
        return sorted(pair_pnl.items(), key=lambda x: x[1], reverse=True)[:top_n]

    def strategy_weight(self, strategy):
        """Returns confidence multiplier based on past performance."""
        stats = self.strategy_performance()
        if strategy not in stats: return 1.0
        s = stats[strategy]
        if s["trades"] < 5: return 1.0  # Not enough data
        wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0.5
        if wr > 0.6: return 1.15  # Boost winning strategies
        elif wr > 0.5: return 1.05
        elif wr < 0.3: return 0.75  # Penalize losers
        elif wr < 0.4: return 0.90
        return 1.0




class DrawdownShield:
    """v7.2: Progressive risk reduction as drawdown increases.
    v9.1: Daily peak reset prevents permanent KILLED state.
    v11.2.8 FIX (May 4, 2026): peak persistence — same bug class as v11.2.7 #23
    (Risk._peak_equity) and v11.2.3 #14 (_btc_24h_high). Audit pass #5 specifically
    searched for missed instances and found one (_peak_equity) but missed THIS one.
    Without persistence, every boot reset peak to cfg.TOTAL_CAPITAL → after restart
    mid-drawdown, sizing tier reverted to FULL even when actual DD was 8-11%.
    Now: bot.py wires saved peak from state.py via set_peak()."""

    def __init__(self, capital):
        self.peak = capital
        self.capital = capital

    def set_peak(self, saved_peak):
        """v11.2.8: restore persisted peak after init (called from bot.py at startup).
        v13.5.5 P5 auto-validation: validates saved_peak against TRUE peak computed
        from trades_v9.jsonl. If saved peak is inflated relative to verified equity
        curve, auto-recomputes. Trade journal is source of truth. No more manual
        DD peak resets needed — bot self-heals on every startup."""
        if not (saved_peak and saved_peak > 0):
            return  # nothing to restore

        try:
            true_peak = self._compute_true_peak_from_journal()
        except Exception as _e:
            # If journal unavailable or unparseable, fall back to saved_peak
            # but clamp to a sane ceiling (max 15% above current capital, since
            # 12%+ drawdown triggers KILLED state — anything beyond is impossible
            # for a live bot to have been tracking).
            sane_ceiling = self.capital * 1.15 if self.capital > 0 else saved_peak
            if saved_peak > sane_ceiling:
                _msg = (f"DD peak ${saved_peak:.2f} exceeds sane ceiling "
                       f"${sane_ceiling:.2f} (current cap ${self.capital:.2f}). "
                       f"Trade journal unavailable ({_e}). Clamping to ceiling.")
                try: log.warning(f"\u26a0\ufe0f  DD shield auto-fix: {_msg}")
                except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
                self.peak = sane_ceiling
            else:
                self.peak = saved_peak
            return

        # Decision: compare saved_peak vs true_peak
        # Floor true_peak at current capital (peak must be >= current)
        floor_peak = max(true_peak, self.capital)

        # Allow saved_peak if it's within 10% of true_peak (unrealized PnL can
        # legitimately create small discrepancy)
        discrepancy = abs(saved_peak - floor_peak) / max(floor_peak, 1.0)

        if discrepancy <= 0.10:
            # Within tolerance — trust saved_peak (preserves any high-water mark
            # that includes recent unrealized profit at peak time)
            self.peak = saved_peak
            try: log.info(f"\U0001f6e1\ufe0f  DD shield peak: ${saved_peak:.2f} "
                         f"(validated against journal, dd={self.drawdown_pct:.1f}%)")
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        else:
            # Discrepancy too large — saved_peak is contaminated. Use computed.
            old_peak = saved_peak
            self.peak = floor_peak
            try: log.info(
                f"\U0001f6e1\ufe0f  DD shield AUTO-FIX: saved peak ${old_peak:.2f} "
                f"contaminated (true peak from journal: ${true_peak:.2f}, "
                f"current cap ${self.capital:.2f}). Using ${floor_peak:.2f}. "
                f"New dd={self.drawdown_pct:.1f}%")
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        # Smart floor: peak can never be below current realized capital

    def _compute_true_peak_from_journal(self, journal_path="trades_v9.jsonl"):
        """v13.5.5 P5: compute the TRUE historical peak equity by replaying
        the trade journal chronologically. Returns the running-max equity
        ever observed. If journal is empty/missing, returns current capital.

        Uses same CLOSE_ACTIONS filter as TradeJournal.strategy_stats() to
        ensure consistency with Bug #21 fix."""
        import json, os

        CLOSE_ACTIONS = {"TP", "SL", "TIME", "TIME_MAX", "GHOST", "CRASH",
                         "CRASH_STUCK", "DUST", "FORCE_CLOSE", "TRAIL",
                         "REGIME", "SCALE", "CLOSE", "VELOCITY_EXIT"}

        if not os.path.exists(journal_path):
            return self.capital

        # Read all close trades chronologically
        trades = []
        try:
            with open(journal_path) as _f:
                for line in _f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                    except Exception:
                        continue
                    if t.get("action") not in CLOSE_ACTIONS:
                        continue
                    pnl = float(t.get("pnl", 0) or 0)
                    trades.append((t.get("ts", ""), pnl))
        except Exception:
            return self.capital

        if not trades:
            return self.capital

        # Sort chronologically (just in case)
        trades.sort(key=lambda x: x[0])

        # Total realized PnL across all trades
        total_realized = sum(pnl for _, pnl in trades)

        # Reconstruct equity curve:
        # current_equity ≈ initial_equity + total_realized_pnl
        # → initial_equity = current_capital - total_realized
        initial_equity = self.capital - total_realized

        # Walk forward, tracking max
        running = initial_equity
        true_peak = max(initial_equity, self.capital)
        for _, pnl in trades:
            running += pnl
            if running > true_peak:
                true_peak = running

        return true_peak

    def update(self, current_capital):
        """Track live capital (drawdown_pct denominator).
        v14.1 FIX (ISSUE A): does NOT update peak — peak now driven by
        update_peak() with REALIZED equity only. Was: peak rose with
        unrealized PnL highs via current_capital (which includes pos_value
        at market price). Intraday spikes inflated peak; when positions
        closed lower, peak retained the unrealized high → dd_peak drifted
        above true realized peak, causing false KILLED states.
        See CHANGES_v14_1.md ISSUE A for full context."""
        self.capital = current_capital

    def update_peak(self, realized_capital):
        """v14.1 FIX (ISSUE A): update high-water mark from REALIZED equity only.
        realized = USDT (free + locked) + sum(pos.size for open positions),
        where pos.size is cost basis at entry, NOT current market value.
        This makes peak immune to unrealized PnL intraday spikes.

        v10.6 design notes (peak is monotonic high-water mark):
        - removed daily peak reset entirely (was v9.1 unconditional reset
          → bypassed kill state silently)
        - removed v10.4 gated-on-dd<8% reset (still eroded peak up to 7.99%
          daily, allowing slow multi-day bleed to evade 12% kill)
        - KILLED is NOT permanent: the `status` property recomputes from
          current dd every call, so KILLED auto-clears when equity recovers
          below 12% drawdown. No arbitrary midnight reset needed."""
        if realized_capital and realized_capital > self.peak:
            self.peak = realized_capital

    @property
    def drawdown_pct(self):
        if self.peak <= 0: return 0
        return (self.peak - self.capital) / self.peak * 100

    @property
    def risk_multiplier(self):
        """Reduce risk as drawdown increases. Renaissance-style.
        v13.5.7 FIX #2 (May 21, 2026): added absolute-dollar floor. Below $200
        capital, a single $1 swing can trigger 5% dd tier — over-aggressive on
        small accounts. Now: only honor dd_pct buckets if absolute dollar drawdown
        also exceeds the corresponding floor. Floors: $4 / $10 / $16 / $24.
        Above $200 capital, behavior is unchanged from v13.5.6.

        v15.3 AUDIT FIX (auto-scale): the v14.6.5 OR→AND change protected small
        accounts but over-restricted accounts above ~$200. Example: $1000 capital
        at 3% DD ($30) → AND logic forced KILLED because all dollar floors are
        below $30. Now uses the LESS RESTRICTIVE (higher multiplier) of two
        independent views:
          - tier_from_pct(dd):     bucket by drawdown %
          - tier_from_dollars($):  bucket by absolute dollars lost
        The two views naturally cross over: on small accounts ($-floor protects
        from over-kill on noise-level losses), on large accounts (%-view drives
        because dollar floors are negligibly small). Bot only escalates when
        BOTH metrics agree the drawdown is bad in absolute AND relative terms.
        This means no manual re-tune is needed when scaling $44 → $200 → $1000.
        """
        return self._tier()[1]

    @staticmethod
    def _tier_from_pct(dd: float):
        if dd < 2:  return ("FULL",      1.0)
        if dd < 5:  return ("CAUTION",   0.75)
        if dd < 8:  return ("DEFENSIVE", 0.50)
        if dd < 12: return ("SURVIVAL",  0.25)
        return ("KILLED", 0.0)

    @staticmethod
    def _tier_from_dollars(dd_dollars: float):
        # Dollar floors: protect small accounts from over-aggressive escalation
        # when percentage looks scary but absolute dollar loss is trivial.
        if dd_dollars < 4:  return ("FULL",      1.0)
        if dd_dollars < 10: return ("CAUTION",   0.75)
        if dd_dollars < 16: return ("DEFENSIVE", 0.50)
        if dd_dollars < 24: return ("SURVIVAL",  0.25)
        return ("KILLED", 0.0)

    def _tier(self):
        """Return (status, multiplier) using the less-restrictive of pct vs $ views.

        Both views compute a tier independently; the safer one wins. This means
        an escalation triggers only when BOTH metrics agree the drawdown is bad."""
        dd = self.drawdown_pct
        dd_dollars = max(0.0, self.peak - self.capital)
        pct = self._tier_from_pct(dd)
        dol = self._tier_from_dollars(dd_dollars)
        # Pick the tier with the higher multiplier (less restrictive). On ties,
        # either is fine — they'll have the same multiplier by definition.
        if pct[0] == "KILLED": return pct
        return pct if pct[1] >= dol[1] else dol

    @property
    def status(self):
        return self._tier()[0]




class PairRotator:
    """v9.4: AUTO-SCANNER — discovers hottest coins from ALL Binance USDT pairs."""
    ANCHORS = {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"}
    BLACKLIST = {"USDCUSDT","FDUSDUSDT","TUSDUSDT","USDSUSDT","DAIUSDT","USD1USDT",
                 "EURUSDT","EURIUSDT","RLUSDUSDT","PAXGUSDT"}
    MIN_VOL_USD = 5_000_000
    MAX_COINS = 35  # v11.2.19 FIX: was 999 — 200 pairs × 0.08s = 90s cycle, SL delayed 1.5min

    def __init__(self, pairs):
        self.pairs = pairs
        self.scores = {p["s"]: 0.0 for p in pairs}
        self.last_rank = 0
        self.last_scan = 0

    def _assign_group(self, vol_usd):
        if vol_usd > 500_000_000: return "A"
        if vol_usd > 100_000_000: return "B"
        if vol_usd > 30_000_000: return "C"
        return "D"

    def auto_scan(self, ex):
        return  # v14.5 FIX: disabled — use config PAIRS whitelist only
        now = time.time()
        if now - self.last_scan < 3600: return
        try:
            log.info("\U0001f50d Auto-scanner: Scanning all Binance USDT pairs...")
            tickers = ex.cl.get_ticker()
            candidates = []
            for t in tickers:
                sym = t["symbol"]
                if not sym.endswith("USDT"): continue
                if sym in self.BLACKLIST: continue
                if "UP" in sym or "DOWN" in sym or "BEAR" in sym or "BULL" in sym: continue
                vol = float(t.get("quoteVolume", 0))
                if vol < self.MIN_VOL_USD: continue
                change = abs(float(t.get("priceChangePercent", 0)))
                if change > 30: continue  # Pump & dump — skip
                price = float(t.get("lastPrice", 0))
                if price <= 0: continue
                if price < 0.0001: continue  # Penny scam — skip
                candidates.append({"s": sym, "vol": vol, "change": change, "price": price})
            max_vol = max(c["vol"] for c in candidates) if candidates else 1
            for c in candidates:
                vol_score = c["vol"] / max_vol
                # v11.2.10 FIX: was `change * 0.4 + vol_score * 0.3 + change * 0.3`
                # — used change twice (0.7 weight), volume only 0.3. Third factor now uses
                # volume-weighted momentum to differentiate from raw change.
                c["score"] = c["change"] * 0.4 + vol_score * 10 * 0.3 + (c["change"] * vol_score) * 0.3
            candidates.sort(key=lambda x: x["score"], reverse=True)
            new_pairs = []
            seen = set()
            for p in self.pairs:
                if p["s"] in self.ANCHORS:
                    new_pairs.append(p); seen.add(p["s"])
            for p in self.pairs:
                if p["s"] not in seen:
                    new_pairs.append(p); seen.add(p["s"])
            new_count = 0
            for c in candidates:
                if len(new_pairs) >= self.MAX_COINS: break
                if c["s"] in seen: continue
                coin_name = c["s"].replace("USDT","")
                group = self._assign_group(c["vol"])
                new_pairs.append({"s": c["s"], "n": coin_name, "g": group, "t": 2})
                seen.add(c["s"]); new_count += 1
            if len(new_pairs) > self.MAX_COINS:
                pair_scores = {c["s"]: c["score"] for c in candidates}
                scored = [(p, pair_scores.get(p["s"], 0)) for p in new_pairs]
                scored.sort(key=lambda x: (x[0]["s"] in self.ANCHORS, x[1]), reverse=True)
                new_pairs = [s[0] for s in scored[:self.MAX_COINS]]
            self.pairs = new_pairs
            self.scores = {p["s"]: 0.0 for p in new_pairs}
            self.last_scan = now
            hot = [p["n"] for p in new_pairs if p["s"] not in self.ANCHORS][:5]
            log.info(f"\U0001f50d Scanner: {len(candidates)} pairs found, tracking {len(new_pairs)} | Hot: {hot}")
            if new_count > 0:
                log.info(f"\U0001f195 New coins added: {new_count}")
        except Exception as e:
            log.warning(f"Scanner error: {e}")

    def rank_pairs(self, ex, interval_min=30):
        # v11.2.18 FIX: API bomb — was blocking main thread 1.5-3min (900+ REST calls)
        # Now runs in daemon thread — returns cached pairs instantly, never stalls SL/TP
        self.auto_scan(ex)
        now = time.time()
        if now - self.last_rank < interval_min * 60: return self.pairs
        if getattr(self, "_ranking", False): return self.pairs  # already running in background
        self._ranking = True
        import threading
        def _do_rank():
            try:
                for pair in list(self.pairs):
                    sym = pair["s"]
                    c = ex.klines_sync(sym, "1h", 24)  # v15.3 FIX: sync helper for thread
                    if not c or len(c) < 10: continue
                    momentum = (c[-1].c - c[0].c) / c[0].c * 100 if c[0].c > 0 else 0
                    vol_avg = sum(x.v for x in c) / len(c)
                    vol_now = c[-1].v / vol_avg if vol_avg > 0 else 1
                    atr = TA.atr(c) / c[-1].c * 100 if c[-1].c > 0 else 0
                    self.scores[sym] = abs(momentum) * 0.4 + vol_now * 0.3 + atr * 0.3
                    time.sleep(0.1)
                self.last_rank = time.time()
                ranked = sorted(self.pairs, key=lambda p: self.scores.get(p["s"], 0), reverse=True)
                top = [(p["n"], round(self.scores.get(p["s"],0), 2)) for p in ranked[:5]]
                log.info(f"\U0001f504 Pair rotation: Top 5 = {top}")
                self.pairs = ranked
            except Exception as e:
                log.warning(f"rank_pairs thread error: {e}")
            finally:
                self._ranking = False
        threading.Thread(target=_do_rank, daemon=True).start()
        return self.pairs  # return cached immediately — main thread never blocked




class Analytics:
    """v7.2: Real-time Sharpe ratio, profit factor, max drawdown tracking.
    v15.0: Extended with Sortino, Calmar, Ulcer, Tail Ratio via risk_metrics module."""

    def __init__(self):
        self.returns = []
        self._return_ts = []  # v13.2: track timestamps for Sharpe annualization
        self.peak_pnl = 0
        self.max_dd = 0
        # v15.0: lazy-load risk_metrics — falls back gracefully if module missing
        try:
            import risk_metrics
            self._rm = risk_metrics
        except Exception:
            self._rm = None

    def record(self, pnl_pct):
        self.returns.append(pnl_pct)
        self._return_ts.append(time.time())
        if len(self.returns) > 500:
            self.returns = self.returns[-500:]
            self._return_ts = self._return_ts[-500:]
        cum = sum(self.returns)
        if cum > self.peak_pnl: self.peak_pnl = cum
        dd = self.peak_pnl - cum
        if dd > self.max_dd: self.max_dd = dd

    @property
    def sharpe(self):
        if len(self.returns) < 10: return 0
        avg = sum(self.returns) / len(self.returns)
        std = (sum((r-avg)**2 for r in self.returns) / len(self.returns)) ** 0.5
        if std <= 0: return 0
        # v13.2 FIX: Annualize based on actual trade frequency, not assumed daily
        # Old: sqrt(252) assumed daily returns — wrong for per-trade recording
        if len(self._return_ts) >= 2:
            span_days = (self._return_ts[-1] - self._return_ts[0]) / 86400
            trades_per_year = len(self.returns) / max(span_days, 0.1) * 252 if span_days > 0 else 252
        else:
            trades_per_year = 252  # fallback
        return round(avg / std * (trades_per_year**0.5), 2)

    @property
    def profit_factor(self):
        wins = sum(r for r in self.returns if r > 0)
        losses = abs(sum(r for r in self.returns if r < 0))
        return round(wins / losses, 2) if losses > 0 else 999

    @property
    def expectancy(self):
        if not self.returns: return 0
        return round(sum(self.returns) / len(self.returns), 4)

    # ── v15.0 institutional risk metrics ──────────────────────────────────────
    @property
    def sortino(self):
        """Downside-adjusted Sharpe — only penalizes negative returns."""
        if not self._rm or len(self.returns) < 10: return 0
        return self._rm.sortino(self.returns, self._return_ts)

    @property
    def calmar(self):
        """Annualized return / max drawdown. Higher = better."""
        if not self._rm or len(self.returns) < 10: return 0
        return self._rm.calmar(self.returns, self._return_ts)

    @property
    def ulcer(self):
        """Ulcer index — RMS of drawdown depth × duration. Lower = smoother."""
        if not self._rm or not self.returns: return 0
        return self._rm.ulcer_index(self.returns)

    @property
    def tail_ratio(self):
        """P95 gain / |P5 loss|. > 1 means right tail dominates."""
        if not self._rm or len(self.returns) < 20: return 0
        return self._rm.tail_ratio(self.returns)

    @property
    def common_sense_ratio(self):
        """Profit factor × tail ratio. > 1.5 = robust strategy."""
        if not self._rm or not self.returns: return 0
        return self._rm.common_sense_ratio(self.returns)

    @property
    def skewness(self):
        """3rd moment — positive = more big wins than big losses."""
        if not self._rm or len(self.returns) < 10: return 0
        return self._rm.skewness(self.returns)

    @property
    def kurtosis(self):
        """4th moment, excess kurtosis. High = fat tails / black-swan risk."""
        if not self._rm or len(self.returns) < 10: return 0
        return self._rm.kurtosis(self.returns)

    @property
    def cvar(self):
        """v15.1: CVaR at 95% — expected loss in worst 5% of trades."""
        if not self._rm or len(self.returns) < 20: return 0
        return self._rm.cvar_95(self.returns)

    @property
    def max_consec_losses(self):
        """v15.1: Longest consecutive losing streak."""
        if not self._rm or not self.returns: return 0
        return self._rm.max_consecutive_losses(self.returns)

    @property
    def recovery(self):
        """v15.1: Recovery factor — total return / max drawdown."""
        if not self._rm or len(self.returns) < 10: return 0
        return self._rm.recovery_factor(self.returns)

    def full_metrics(self):
        """All v15.1 metrics in one dict — for periodic reports."""
        if not self._rm: return {"sharpe": self.sharpe, "profit_factor": self.profit_factor,
                                  "expectancy": self.expectancy}
        d = self._rm.full_report(self.returns, self._return_ts)
        # v15.1: expose new metrics at analytics level too
        d["cvar_95"] = self.cvar
        d["max_consec_losses"] = self.max_consec_losses
        d["recovery_factor"] = self.recovery
        return d



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EXCHANGE (v7: limit orders added)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

