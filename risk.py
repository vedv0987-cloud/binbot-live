# BinBot v11 — risk.py
import time, json, logging, asyncio  # v16.0 AUDIT FIX C2: was 'asyncio, asyncio' (duplicate)
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from models import Position, Signal, Context
from state import StateManager
from monitors import KellySizer, EventCalendar, MVRVMonitor, OpenInterestMonitor, TokenUnlockMonitor, TVLMonitor, WhaleWalletMonitor, StablecoinFlow
# v15.0: TCA logger — measures actual fill quality per trade
try:
    from tca import TCALogger
    _TCA = TCALogger()
except Exception:
    _TCA = None
log = logging.getLogger('binbot')

class Risk:
    def __init__(self, cfg, state_mgr):
        self.cfg=cfg; self.sm=state_mgr; self.tg=None  # v13.5.4 wired by bot.py
        self.positions:List[Position]=[]; self.pnl=0.0; self.daily_pnl=0.0; self.daily_t=0
        self.fees=0.0; self.trades=[]; self.last_close={}; self.last_result={}; self.pair_losses_today={}
        self.last_reset=""; self.closs=0; self.wins=0; self.losses=0
        self.pause_until:Optional[datetime]=None; self._grid_exp=0.0
        # v11.2.3 FIX: BTC 24h-high persistence. Default 0 means "uninitialized";
        # bot.py will set it on first BTC price seen, then save survives restarts.
        self._btc_24h_high=0
        self._peak_equity = cfg.TOTAL_CAPITAL  # v10.0: peak-equity DD anchor (rises with profits, never falls)
        self._pending_partials: list = []  # v16.0: partial scale-out queue
        # v15.16: equity curve — (date_str, equity) daily samples for 10d/20d MA
        self._equity_samples: list = []
        try:
            import json as _j, os as _os
            _ecf = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'equity_curve.json')
            if _os.path.exists(_ecf):
                with open(_ecf) as _ef:  # v16.0 AUDIT FIX M8: was open() without close — file handle leak
                    self._equity_samples = _j.load(_ef).get('samples', [])
        except Exception: pass
        self._saved_dd_peak = 0  # v11.2.8: holds restored DrawdownShield.peak; bot.py reads this to wire into ddshield
        self.kelly = KellySizer(cfg.KELLY_FRACTION)
        # v13.5: pass PRE_EVENT_HOURS so operator can opt into pre-event blocking
        # without modifying monitors.py. Default 0 keeps v13.4 behavior.
        self.event_cal = EventCalendar(pre_event_hours=getattr(cfg, "PRE_EVENT_HOURS", 0.0))
        self.mvrv = MVRVMonitor()
        self.oi_monitor = OpenInterestMonitor()
        self.token_unlocks = TokenUnlockMonitor()
        self.tvl = TVLMonitor()
        self.whale_wallets = WhaleWalletMonitor()
        self.stable_flow = StablecoinFlow()

        saved = state_mgr.load()
        # v18.8 FIX: flag a fresh start so bot.run() anchors the drawdown peak (both the
        # DD-shield AND the circuit-breaker) to the REAL starting equity instead of the
        # stale config TOTAL_CAPITAL — otherwise a wipe leaves the peak at the config
        # default ($45.65) and the bot trips a bogus drawdown that blocks ALL trading.
        # IMPORTANT: bot.py's _heal_memory() pre-creates an empty bot_state.json ("{}") at
        # import, so state_mgr.load() returns a defaults-filled (truthy) dict even on a true
        # wipe. So `not bool(saved)` was always False. Detect fresh by the ABSENCE of real
        # history: no open positions AND no saved peak-equity.
        self._fresh_start = (saved is None) or (
            not saved.get("positions") and float(saved.get("peak_equity", 0) or 0) <= 0)
        if saved:
            self.positions=saved["positions"]; self.pnl=saved["pnl"]
            self.wins=saved["wins"]; self.losses=saved["losses"]; self.fees=saved["fees"]
            # v8.4 FIX: Restore daily limits so restart doesn't bypass them
            self.daily_pnl=saved.get("daily_pnl",0)
            self.daily_t=saved.get("daily_trades",0)
            # v9.5 FIX: Restore cooldown memory — prevents repeat-loss loop on same coin
            self.last_close=saved.get("last_close", {})
            self.last_result=saved.get("last_result", {})
            self.pair_losses_today=saved.get("pair_losses_today", {})
            # v11.2.1 FIX: Restore daily-reset gate, consec-loss streak, active pause.
            # Without these, _reset() fires on every boot (because last_reset="" != today)
            # and wipes daily_pnl/daily_t/closs/pair_losses_today, completely bypassing
            # MAX_DAILY_LOSS, MAX_DAILY_TRADES, MAX_CONSEC_LOSSES, and active pause windows.
            self.last_reset = saved.get("last_reset", "")
            self.closs = saved.get("closs", 0)
            # v11.2.3 FIX (May 3, 2026): restore BTC 24h-high. Was RAM-only before;
            # restart-during-crash blinded the 5% drop trigger.
            self._btc_24h_high = saved.get("btc_24h_high", 0)
            # v11.2.7 FIX (May 3, 2026): restore peak-equity DD anchor. Same bug class as #14
            # that audits #1-4 missed. Was RAM-only — every boot reset peak to TOTAL_CAPITAL,
            # so a restart mid-drawdown silently wiped the breaker reference. Real-world:
            # bot peaks $80, drops to $70 (12.5% DD over 10% breaker), restart → peak=$50,
            # cycles 1-10 auto-compound TOTAL_CAPITAL=$70, paused() rewrites peak to $70 →
            # drawdown_pct=0 → breaker never trips, bot trades through actual drawdown.
            # Only override default if saved value is positive (preserves first-run default).
            saved_peak = saved.get("peak_equity", 0)
            if saved_peak and saved_peak > 0:
                self._peak_equity = saved_peak
                log.info(f"  📈 Peak-equity anchor restored: ${saved_peak:.2f}")
            # v11.2.8 FIX (May 4, 2026): capture saved DrawdownShield.peak so bot.py can
            # wire it into ddshield AFTER ddshield is constructed. We can't directly
            # modify ddshield here because Risk.__init__ runs before bot.py creates it.
            self._saved_dd_peak = saved.get("dd_peak", 0)
            if self._saved_dd_peak and self._saved_dd_peak > 0:
                log.info(f"  🛡️ DD-shield peak anchor restored: ${self._saved_dd_peak:.2f}")
            # v14.5.1 FIX (audit #5): restore total_capital from state. Was saved (v11.2.20)
            # and loaded (v11.2.21) into the return dict but never applied to cfg.
            # If wallet balance query fails at startup, bot falls back to config default
            # instead of last-known capital. run() auto-compound overrides if wallet works.
            saved_cap = saved.get("total_capital")
            if saved_cap and saved_cap > 0:
                self.cfg.TOTAL_CAPITAL = saved_cap
                log.info(f"  💰 Capital restored from state: ${saved_cap:.2f}")
            pu_raw = saved.get("pause_until", None)
            if pu_raw:
                try:
                    self.pause_until = datetime.fromisoformat(pu_raw) if isinstance(pu_raw, str) else pu_raw
                    if self.pause_until and self.pause_until > datetime.now(timezone.utc):
                        log.warning(f"⏸ Active pause restored: until {self.pause_until.isoformat()}")
                except Exception as e:
                    log.warning(f"pause_until restore failed: {e}")
            if self.last_close:
                log.info(f"  🛡️ Cooldown memory restored: {len(self.last_close)} pairs tracked")
            if self.last_reset:
                log.info(f"  📅 Daily-reset gate restored: last_reset={self.last_reset} closs={self.closs}")

    def save_state(self, grid_pnl=None, grid_trades=None, hyperopt_params=None, total_capital=None):
        # v9.5 FIX: pass cooldown dicts so they survive restarts
        # v9.7.2 FIX: defaults are None so StateManager preserves existing grid stats from disk
        # (was 0, which wiped grid_pnl on every save called without explicit args)
        # v11.2.1 FIX: persist daily-reset gate, consec-loss streak, active pause timer
        # v11.2.3 FIX: persist BTC 24h-high for crash-protection across restart
        # v11.2.7 FIX: persist peak-equity DD anchor (audit #5, same class as #14)
        # v11.2.8 FIX: persist DrawdownShield.peak (third instance of the same bug class)
        # v11.2.10 FIX: guard against early save_state() before bot.py wires ddshield.
        # Was: first call overwrote persisted dd_peak with 0, defeating v11.2.8 persistence fix.
        dd_peak_val = 0
        if hasattr(self, 'ddshield') and self.ddshield:
            dd_peak_val = self.ddshield.peak
        elif self._saved_dd_peak > 0:
            dd_peak_val = self._saved_dd_peak  # preserve loaded value until ddshield is wired
        self.sm.save(self.positions,self.pnl,self.daily_pnl,self.daily_t,
                     self.wins,self.losses,self.fees,grid_pnl,grid_trades,hyperopt_params,
                     self.last_close, self.last_result, self.pair_losses_today,
                     self.last_reset, self.closs, self.pause_until, self._btc_24h_high,
                     self._peak_equity, dd_peak_val, total_capital)

    @property
    def exposure(self): return sum(p.size for p in self.positions)

    def get_and_clear_partials(self) -> list:
        """v16.0: Return and clear pending partial scale-out orders."""
        p = list(self._pending_partials)
        self._pending_partials.clear()
        return p

    def record_equity(self, equity: float) -> None:
        """v15.16: log one equity sample per day, persist to equity_curve.json."""
        try:
            from datetime import datetime, timezone as _tz
            import json as _j, os as _os
            today = datetime.now(_tz.utc).strftime('%Y-%m-%d')
            if self._equity_samples and self._equity_samples[-1][0] == today:
                self._equity_samples[-1] = [today, round(equity, 4)]
            else:
                self._equity_samples.append([today, round(equity, 4)])
            self._equity_samples = self._equity_samples[-20:]
            _ecf = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'equity_curve.json')
            with open(_ecf, 'w') as _f: _j.dump({'samples': self._equity_samples}, _f)
        except Exception as _e:
            pass

    def equity_size_mult(self) -> float:
        """v15.16: position size multiplier from equity curve.
        Above 10d MA=1.0 | Below 10d MA=0.5 | Below 20d MA=0.25"""
        if not getattr(self.cfg, 'EQUITY_CURVE_ENABLED', True):
            return 1.0
        _vals = [e for _, e in self._equity_samples]
        if len(_vals) < 3:
            return 1.0  # not enough data — full size
        # v16.0 AUDIT FIX H5: require minimum 10 samples for 10d MA.
        # With fewer samples, the MA is a shorter-period average that's
        # much more volatile and triggers unnecessary size reductions.
        if len(_vals) >= 10:
            _ma10 = sum(_vals[-10:]) / 10
        else:
            return 1.0  # not enough data for reliable MA
        _curr = _vals[-1]
        if _curr >= _ma10:
            return 1.0
        if len(_vals) >= 20:  # v16.0 AUDIT FIX H5: need 20 real samples for 20d MA
            _ma20 = sum(_vals[-20:]) / 20
            if _curr >= _ma20:
                return 0.50
            return 0.25
        return 0.50
    @property
    def available(self):
        # v11.2.5 FIX: defensive floor at 0.0.
        # v14.6.3 FIX: use real_usdt if available (set by wallet sync) to prevent
        # phantom capital allocation when TOTAL_CAPITAL drifts from actual balance.
        base = getattr(self, '_real_usdt_free', None)
        if base is not None and base > 0:
            return max(0.0, base - self._grid_exp)
        return max(0.0, self.cfg.TOTAL_CAPITAL - self.exposure - self._grid_exp)
    @property
    def wr(self): t=self.wins+self.losses; return self.wins/t if t>0 else 0.5
    @property
    def portfolio_heat(self):
        """v7: Total risk across all open positions."""
        if not self.positions: return 0.0
        # v10.2 FIX: only count distance BELOW entry as risk. After BE-lock moves SL above
        # avg_entry, abs(...) would inflate heat with guaranteed profit, blocking new trades.
        total_risk = sum(max(0.0, p.avg_entry - p.sl) / p.avg_entry * p.size
                        for p in self.positions if p.avg_entry > 0)
        return total_risk / self.cfg.TOTAL_CAPITAL if self.cfg.TOTAL_CAPITAL > 0 else 0.0

    def set_grid_exp(self, e): self._grid_exp=e

    def paused(self):
        self._reset()
        if self.daily_pnl<=-self.cfg.max_daily_loss: return True,"DailyLoss"
        if self.pause_until and datetime.now(timezone.utc)<self.pause_until: return True,"Streak"
        # v10.0: PEAK-EQUITY drawdown (was initial-cap anchor — broke on TOTAL_CAPITAL changes)
        # Track running high-water mark; DD = drop from peak, not from boot value.
        # Auto-compounds with realized PnL via wallet balance updates.
        # v11.2.3 + v11.2.7-LIVE_ONLY: live mode auto-compounds TOTAL_CAPITAL = wallet_bal
        # (which already includes realized PnL via auto-compound). Use it directly without
        # adding self.pnl on top — that would double-count and trigger the circuit breaker
        # on smaller drawdowns than reality.
        current_cap = self.cfg.TOTAL_CAPITAL  # already reflects PnL via auto-compound
        if current_cap > self._peak_equity:
            self._peak_equity = current_cap
        drawdown_pct = (self._peak_equity - current_cap) / self._peak_equity if self._peak_equity > 0 else 0
        if drawdown_pct >= self.cfg.CIRCUIT_BREAKER_PCT:
            return True, f"CircuitBreaker (DD: {drawdown_pct*100:.1f}% peak=${self._peak_equity:.2f})"
        if self.daily_t>=self.cfg.MAX_DAILY_TRADES: return True,"MaxTrades"
        return False,""

    def _reset(self):
        d=datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if d!=self.last_reset:
            if self.last_reset: log.info(f"📊 Reset | PnL:${self.daily_pnl:+.4f} T:{self.daily_t}")
            self.daily_pnl=0; self.daily_t=0; self.closs=0; self.pause_until=None; self.pair_losses_today={}; self.last_reset=d

    def _session_ok(self, pair, now_min=None):
        """v18.9.0: True if `pair`'s base asset may ENTER at the current IST time.
        Golden window (SESSION_GOLDEN) is open to ALL coins; a listed coin also trades
        in its own window; an unlisted coin trades ONLY in the golden window. Windows may
        cross midnight (end < start). Fails OPEN on any clock/config error — a time bug
        must never block trading. `now_min` (minutes since IST midnight) lets tests inject."""
        try:
            if now_min is None:
                from datetime import datetime, timezone, timedelta
                _ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
                now_min = _ist.hour * 60 + _ist.minute
            base = pair.replace("USDT", "").replace("BUSD", "")
            g = getattr(self.cfg, 'SESSION_GOLDEN', (1110, 1350))
            if g[0] <= now_min < g[1]:
                return True, "GOLDEN"
            listed = False
            for _name, _start, _end, _coins in getattr(self.cfg, 'SESSION_WINDOWS', ()):
                if base in _coins:
                    listed = True
                    _hit = (_start <= now_min < _end) if _start <= _end else (now_min >= _start or now_min < _end)
                    if _hit:
                        return True, _name
            return (False, "unlisted") if not listed else (False, "off-window")
        except Exception:
            return True, "err-allow"

    def can_trade(self, sig, fg=50):
        # v9.2: FOMC/CPI event protection
        # v13.4 fix (Batch 1): docs aligned to actual code behavior.
        # v11.2.8 introduced graduated multipliers (0.5 within 12h, 0.75 within 24h).
        # v11.2.14 reverted to binary block-or-allow but left v11.2.8 graduated
        # comments + the dead `if emult < 1.0` branch in place. README v13.2 also
        # still bragged about graduated multipliers though they were gone.
        # Behavior NOW: hard block within 12h of FOMC/CPI, full size otherwise.
        # To restore graduated multipliers see Batch 2 patch:
        # B2-1_restore_graduated_event_multipliers.patch
        emult = 1.0
        if hasattr(self, 'event_cal'):
            emult = self.event_cal.risk_mult()
            if emult == 0.0:
                log.info(f"🏛 {sig.pair} BLOCKED — FOMC/CPI event within 12 hours")
                return False, "EVENT_BLOCK", 0

        p,r=self.paused()
        if p: return False,r,0
        if len(self.positions)>=self.cfg.MAX_POSITIONS: return False,"MaxPos",0
        if any(p.pair==sig.pair for p in self.positions): return False,"Held",0
        # v18.9.0 SESSION FILTER: only enter a coin during its active IST liquidity window
        # (golden window open to all; unlisted coins → golden only). Entries only — exits
        # are handled in check_exits and are never gated.
        if getattr(self.cfg, 'SESSION_FILTER_ENABLED', False):
            _sok, _swin = self._session_ok(sig.pair)
            if not _sok:
                return False, f"OffSession:{_swin}", 0
        # v9.3: Token unlock protection (applies to all signals, not just those with cooldowns)
        coin_name = sig.pair.replace("USDT","")
        avoid, unlock_reason = self.token_unlocks.should_avoid(coin_name)
        if avoid:
            log.info(f"🔓 {sig.pair} BLOCKED — {unlock_reason}")
            return False, "UNLOCK", 0
        # v9.3: EXPERT TRADER COOLDOWN — memory-based, not blind timer
        # v11.2.10 FIX: merged duplicate `if sig.pair in self.last_close` blocks.
        # Was: first block computed `m` then fell through; second block used `m` from
        # first — worked by accident but any refactoring would cause NameError.
        if sig.pair in self.last_close:
            m=(datetime.now(timezone.utc)-self.last_close[sig.pair]).total_seconds()/60
            last_result = self.last_result.get(sig.pair, "LOSS")
            losses_today = self.pair_losses_today.get(sig.pair, 0)
            # v11.2.8 FIX (May 4, 2026): default cd_min so any unexpected last_result
            # value (corrupted state, future code change) doesn't UnboundLocalError
            # at the `if m >= cd_min` check below.
            cd_min = 480

            if last_result == "WIN":
                cd_min = 30   # 30 min after win — let price settle
            elif last_result == "TIMEOUT":
                cd_min = 1440  # 24 HOURS after timeout — coin was dead, skip today
            elif last_result == "LOSS":
                cd_min = 480   # 8 HOURS after SL — needs time to recover

            # Lost twice on same coin today? Block for rest of day
            if losses_today >= 2:
                cd_min = 1440  # 24h block — expert never buys same loser 3x in a day

            # EXPERT OVERRIDE: If cooldown passed but signal is weak, still skip
            # Only re-enter same coin if signal is STRONGER than last time
            if m >= cd_min and last_result != "WIN":
                min_conf_rebuy = 0.90  # Need 90%+ confidence to rebuy a recent loser
                if sig.conf < min_conf_rebuy:
                    return False, f"CD_WEAK({sig.conf:.0%})", 0
                # Need A+ grade to rebuy a loser
                if sig.grade not in ("A+",):
                    return False, "CD_GRADE", 0
                log.info(f"🔄 Re-entry {sig.pair} after {m:.0f}m | conf:{sig.conf:.0%} grade:{sig.grade} | Expert override")

            if m < cd_min: return False, f"CD({cd_min//60}h)", 0
        grp=sum(1 for p in self.positions if p.group==sig.group)
        # v14.6.2: Group D only in TREND_UP or BTC pump
        if sig.group == 'D':
            import json as _j, os as _os, time as _t
            _regime = getattr(self, '_last_regime', 'RANGE')
            # v16.0 AUDIT FIX C4: removed dead _btc_1h_change / _btc_pump gate.
            # _btc_1h_change was never computed anywhere in the codebase, so the
            # BTC pump exception always evaluated to False. Group D now gates
            # solely on TREND_UP regime, which matches actual observed behavior.
            if _regime not in ('TREND_UP',):
                return False, 'GroupD_NoTrend', 0
            # Group D daily loss check (3% limit)
            # v14.6.4 AUDIT FIX: previously read ONLY self.journal.history. journal is wired by
            # bot.py AFTER Risk.__init__, so first-cycle Group D entries could bypass the daily
            # loss cap. Now falls back to reading trades_v9.jsonl directly when journal isn't
            # attached, and logs a warning so the operator can detect the wiring race.
            try:
                from datetime import datetime as _dt
                # v14.6.4 AUDIT FIX (M5): datetime.utcnow() deprecated in Python 3.12+.
                _today = _dt.now(timezone.utc).date().isoformat()
                _trades = self.journal.history if hasattr(self, 'journal') and self.journal else None
                if _trades is None:
                    # Fallback: read trade journal from disk (last 500 lines is plenty for "today")
                    log.warning("Group D loss check: journal not wired yet — using JSONL fallback")
                    _trades = []
                    try:
                        import json as _jj, os as _oo
                        _log = getattr(self.cfg, 'LOG_FILE', 'trades_v9.jsonl')
                        if _oo.path.exists(_log):
                            with open(_log, 'r') as _f:
                                _lines = _f.readlines()[-500:]
                            for _ln in _lines:
                                try: _trades.append(_jj.loads(_ln))
                                except Exception: pass
                    except Exception as _ee:
                        log.warning(f"Group D loss check JSONL fallback failed: {_ee}")
                _d_loss = sum(float(t.get('pnl',0)) for t in _trades 
                              if t.get('action') in ('TP','SL','TIME','CRASH','FORCE_CLOSE') 
                              and _today in t.get('ts','') 
                              and t.get('group','') == 'D')
                if _d_loss < -(self.cfg.TOTAL_CAPITAL * self.cfg.GROUP_D_DAILY_LOSS_PCT):
                    return False, 'GroupD_DailyLoss', 0
            except Exception: pass
        # v14.6.2: Group D max 1 position
        if sig.group == 'D' and grp >= self.cfg.GROUP_D_MAX_POS:
            return False, 'MaxPos_D', 0
        if self.exposure>=self.cfg.TOTAL_CAPITAL*self.cfg.MAX_EXPOSURE: return False,"MaxExp",0

        # v7.2: Drawdown shield check — block trades if drawdown > 12%
        # (Applied from bot cycle level)

        # v7: Portfolio heat check
        max_heat = self.cfg.FEAR_HEAT if fg < 20 else self.cfg.MAX_HEAT
        if self.portfolio_heat >= max_heat: return False,"HeatMax",0

        # v14.6.4 AUDIT FIX: removed dead `if True and len(...) >= 0` wrapper.
        # OLD code: Kelly was called every cycle, its result set `size`, then `size` was
        # IMMEDIATELY overwritten by the 33.33% formula below for Groups A/B/C (and by
        # GROUP_D_SIZE_PCT for Group D). Kelly had ZERO effect on actual sizing.
        # The else-branch SL-capping logic (10% ceiling, sig.sl rewrite) was permanently
        # DEAD even though it provides safety not duplicated by strategies.py (which
        # only enforces the 3% MIN, not the 10% MAX).
        # FIX: drop the Kelly call (no-op), run SL capping unconditionally.
        # No behavior change for sizing; restores the 10% SL ceiling safety.
        sl_pct = abs(sig.price - sig.sl) / sig.price if sig.price > 0 else 0.01
        if sl_pct == 0: sl_pct = 0.01
        # v13.5 FIX: apply SL floor/ceiling here so size matches the SL that
        # open_pos will actually place. Hard ceiling 10%.
        # v13.5.2 audit Fix #6: floor raised 0.5%→3% to match strategies.py:304.
        # v14.6.2: Group D uses wider 4% SL floor.
        _sl_floor = self.cfg.GROUP_D_SL_FLOOR if getattr(sig, "group", "A") == "D" else 0.03
        capped_sl_pct = max(min(sl_pct, 0.10), _sl_floor)
        if capped_sl_pct != sl_pct:
            old_sl = sig.sl
            sig.sl = sig.price * (1 - capped_sl_pct)
            if hasattr(sig, 'rr') and sig.rr > 0:
                sig.tp = sig.price + (sig.price - sig.sl) * sig.rr
            if abs(capped_sl_pct - sl_pct) > 0.0001:
                log.info(f"  🔧 {sig.pair} SL {sl_pct*100:.3f}%→{capped_sl_pct*100:.3f}% "                             f"(${old_sl:.6f}→${sig.sl:.6f}), TP→${sig.tp:.6f} (R:R={sig.rr})")
        sl_pct = capped_sl_pct
        # NOTE: `size` is assigned UNCONDITIONALLY below by the 33.33% / GROUP_D formula.

        # v7.2: Drawdown shield — reduce size based on drawdown
        # v13.5.7 FIX #1 (May 21, 2026): per-position cap now scales with MAX_POSITIONS.
        # Was: hard-coded 0.25 (75%/3 slots). MAX_POSITIONS dropped to 2 in v14.0
        # but this cap stayed at 25%, leaving 25% of capital unreachable. At small
        # capital, the over-tight cap pushed sizing below Binance MIN_NOTIONAL once
        # other multipliers (DD shield, closs) stacked. Formula: 0.75/MAX_POSITIONS,
        # clamped to [0.25, 0.50] so this never goes wildly outside expected range
        # (e.g. MAX_POSITIONS=1 would give 75% — too aggressive).
        _pos_cap = max(0.20, min(0.95, self.cfg.MAX_EXPOSURE / max(self.cfg.MAX_POSITIONS, 1)))

        # v14.6.2: Volatility-targeting size scalar
        # v14.6.4 AUDIT FIX: comment said "2.5%" but value is 1.0%. SL floor is actually 3%.
        # Comment now reflects reality. If 2.5% was the original intent, change value to 0.025.
        # Target vol = 1.0%. When coin is calm (<1.0% ATR), size up.
        # When coin is volatile (>1.0% ATR), size down. Clamped to [0.5, 1.5]×
        # so we never go below half or above 1.5× base size.
        # Formula: scalar = target_vol / realized_vol (ATR%)
        # Example: ATR=0.7% → scalar=1.43 (calm, size up)
        #          ATR=2.0% → scalar=0.50 (volatile, size down to 0.5×)
        _TARGET_VOL = 0.010  # 1.0% — vol-target (NOT SL floor; SL floor is 3% in strategies.py)
        _atr_pct = sig.atr / sig.price if sig.price > 0 and sig.atr > 0 else _TARGET_VOL
        _vol_scalar = max(0.50, min(1.50, _TARGET_VOL / _atr_pct)) if _atr_pct > 0 else 1.0
        if abs(_vol_scalar - 1.0) > 0.05:
            log.info(f"  📐 {sig.pair} vol-size scalar: {_vol_scalar:.2f}× (ATR={_atr_pct*100:.2f}%)")

        # v14.6.2: Group D uses smaller 15% size
        if getattr(sig, 'group', 'A') == 'D':
            size = min(self.cfg.TOTAL_CAPITAL * self.cfg.GROUP_D_SIZE_PCT, self.available * 0.90)
        else:
            # v15.3 AUDIT FIX #4: opt-in ERCSizing — equal risk contribution across positions.
            # When cfg.USE_ERC_SIZING is True, replaces the static 33.33% formula with
            # portfolio_alloc.ERCSizing. Falls back to the 33.33% formula on any error so
            # a bad ERC compute never blocks trading.
            _erc_size = None
            if getattr(self.cfg, 'USE_ERC_SIZING', False):
                try:
                    from portfolio_alloc import ERCSizing
                    _erc = ERCSizing()
                    # Build positions list in the shape ERCSizing expects.
                    # Adds the new candidate at proposed nominal so ERC sees the full set.
                    _pos_list = [{'pair': p.pair, 'qty': p.qty, 'entry': p.entry,
                                  'size': p.size} for p in self.positions]
                    _pos_list.append({'pair': sig.pair, 'qty': 0, 'entry': sig.price, 'size': 0})
                    _sizes = _erc.compute(_pos_list, total_capital=self.cfg.TOTAL_CAPITAL,
                                          max_exposure=self.cfg.MAX_EXPOSURE)
                    if isinstance(_sizes, dict) and sig.pair in _sizes:
                        _erc_size = float(_sizes[sig.pair])
                        if _erc_size > 0:
                            log.info(f"  📊 {sig.pair} ERCSizing: ${_erc_size:.2f} "
                                     f"(vs 33% formula would give ${self.cfg.TOTAL_CAPITAL*0.3333*_vol_scalar:.2f})")
                except Exception as _erc_e:
                    log.warning(f"ERCSizing failed for {sig.pair}, falling back to 33% formula: {_erc_e}")
                    _erc_size = None
            if _erc_size is not None and _erc_size > 0:
                size = min(_erc_size, self.cfg.TOTAL_CAPITAL * _pos_cap, self.available * 0.90)
            else:
                _base_frac = getattr(self.cfg, 'POSITION_SIZE_PCT', 0.3333)  # v18.7.4: tier-driven (was hardcoded 0.3333)
                size=min(self.cfg.TOTAL_CAPITAL*_base_frac*_vol_scalar, self.cfg.TOTAL_CAPITAL*_pos_cap, self.available*0.90)  # v14.6.2: vol-targeted
        # v8.4 FIX: Actually apply ddshield multiplier to size
        if hasattr(self, 'ddshield'):
            dd_mult = self.ddshield.risk_multiplier
            if dd_mult <= 0: return False,"DD_Kill",0
            pass  # v14.6: DD shield size reduction removed
        # B2-1 (applied): graduated event-risk multiplier scales position size
        # when an event is within 24h. emult comes from EventCalendar.risk_mult().
        if emult < 1.0:
            log.info(f"🏛 {sig.pair} event-risk size ×{emult} (FOMC/CPI within 24h)")
            size *= emult
        # v15.16: equity curve meta-risk multiplier
        _eq_mult = self.equity_size_mult()
        if _eq_mult < 1.0:
            _ma_val = sum([e for _,e in self._equity_samples[-10:]]) / max(len(self._equity_samples[-10:]),1)
            log.info(f"📉 {sig.pair} equity below MA (${_ma_val:.2f}) → size ×{_eq_mult} (${size:.2f}→${size*_eq_mult:.2f})")
            if self.tg:
                try: self.tg.send(f"📉 <b>EQUITY CURVE</b> — sizing reduced ×{_eq_mult}\nEquity below {len(self._equity_samples[-10:])}d MA")
                except Exception: pass
            size *= _eq_mult
        # v16.0: Daily volatility harvesting — reduce size on big days / losing days.
        # Paper §5.3: rebalancing bots siphon excess profits into stablecoins during rallies.
        # Your version: scale back new position sizes to protect daily gains.
        if getattr(self.cfg, 'DAILY_HARVEST_ENABLED', True):
            _day_pct = self.daily_pnl / max(self.cfg.TOTAL_CAPITAL, 1.0)
            _thresh = getattr(self.cfg, 'DAILY_HARVEST_THRESHOLD', 0.01)  # 1%
            if _day_pct >= _thresh * 2:  # 2%+ gain today → protect 25% of size
                _hm = 0.75
                log.info(f"  💰 {sig.pair} harvest: day +{_day_pct*100:.1f}% → size ×{_hm}")
                size *= _hm
            elif _day_pct >= _thresh:    # 1%+ gain today → protect 15%
                _hm = 0.85
                log.info(f"  💰 {sig.pair} harvest: day +{_day_pct*100:.1f}% → size ×{_hm}")
                size *= _hm
            elif _day_pct <= -_thresh:   # losing day → damage control
                _hm = 0.80
                log.info(f"  🛡 {sig.pair} damage ctrl: day {_day_pct*100:.1f}% → size ×{_hm}")
                size *= _hm
        # v15.16: Dynamic Regime Kelly multiplier (activates at 50+ trades)
        if getattr(self.cfg, 'PORTFOLIO_KELLY_ENABLED', False) and getattr(self.cfg, 'KELLY_REGIME_AWARE', True):
            try:
                from portfolio_alloc import PortfolioKelly as _PK
                _regime_now = getattr(sig, 'regime', 'RANGE') if hasattr(sig, 'regime') else 'RANGE'
                _km = _PK().regime_mult(_regime_now)
                if _km < 1.0:
                    log.info(f"  🎓 {sig.pair} Kelly regime ×{_km} ({_regime_now}) → ${size:.2f}→${size*_km:.2f}")
                    size *= _km
            except Exception as _ke:
                log.debug(f"Kelly regime mult: {_ke}")
        # v14.6: closs penalty removed — fixed 33.33% allocation
        # v13.2: MTF alignment — reduce size when timeframes disagree
        # Prevents trading against macro trend (institutional best practice)
        try:
            from types import SimpleNamespace
            if hasattr(sig, '_ctx_mtf_align'):
                _align = sig._ctx_mtf_align
            else:
                _align = 50  # neutral default
            if _align < 30:
                size *= 0.6  # Strong disagreement → 40% reduction
                log.info(f"📐 {sig.pair} MTF misalign ({_align:.0f}) → size ×0.6")
            elif _align < 45:
                size *= 0.8  # Mild disagreement → 20% reduction
        except Exception: pass
        if size<self.cfg.MIN_TRADE: return False,"Small",0
        # v8.3: Fee gate — skip if expected profit < 2x fees
        try:
            exp_profit = size * (sig.rr * abs(sig.price-sig.sl)/sig.price) if sig.price>0 else 0
            fees_2x = size * self.cfg.TAKER_FEE * 4
            if exp_profit < fees_2x:
                return False,"FeeLow",0
        except Exception as e:
            log.warning(f"Fee gate calc failed for {sig.pair}: {type(e).__name__}: {e}")
        return True,"OK",round(size,2)

    def volatility_adjusted_size(self, base_size, atr_pct, group="B"):
        """v15.1: Risk parity — inverse-volatility position sizing.

        High-volatility coins get SMALLER positions, low-volatility get LARGER.
        This equalizes the RISK contribution of each position, not the dollar amount.

        Example:
          BTC (ATR 2%) → multiplier 2.5/2.0 = 1.25× base → larger position
          MEME (ATR 8%) → multiplier 2.5/8.0 = 0.31× base → smaller position

        Args:
            base_size: Dollar amount from can_trade() sizing
            atr_pct: ATR as % of price (e.g. 2.5 for 2.5%)
            group: Pair group (A/B/C/D) for target vol calibration
        Returns:
            Adjusted dollar size, clamped to [0.5×, 2.0×] of base."""
        TARGET_VOL = {"A": 3.0, "B": 2.5, "C": 2.0, "D": 1.5}
        target = TARGET_VOL.get(group, 2.5)
        if atr_pct <= 0:
            return base_size
        vol_ratio = target / atr_pct
        vol_ratio = max(0.5, min(2.0, vol_ratio))
        adjusted = base_size * vol_ratio
        if abs(vol_ratio - 1.0) > 0.1:
            log.info(f"  📐 VolSize: ATR={atr_pct:.1f}% target={target:.1f}% → {vol_ratio:.2f}× (${base_size:.2f}→${adjusted:.2f})")
        return adjusted

    def open_pos(self, sig, size, order, ctx, tg):
        # v18.5 AUDIT FIX (C5): defensive duplicate-pair guard. open_pos is reachable
        # from several async paths (instant maker fill, market fallback, hybrid background
        # task, pending-order poll loop, main scan). can_trade's "Held" check + the
        # _limit_orders dedup normally prevent doubles, but they are not atomic across the
        # background task and the cycle loop. If a position for this pair already exists,
        # refuse to open a second — better to drop a duplicate fill into reconciliation
        # than to silently run two untracked-as-one positions on the same symbol.
        if any(p.pair == sig.pair for p in self.positions):
            log.warning(f"⚠️ open_pos DUP-GUARD {sig.pair}: position already tracked — skipping duplicate open")
            return None
        # v18.5 AUDIT FIX (C4): fee rate must reflect the ACTUAL fill type, not the
        # USE_LIMIT config flag. Hybrid-maker entries frequently fall back to a MARKET
        # (taker) buy while USE_LIMIT is still True — booking those at MAKER_FEE=0
        # over-stated realized PnL. Infer from the order's echoed `type`; fall back
        # conservatively (never under-count) when the response carries no type.
        _otype = str(order.get("type", "") if isinstance(order, dict) else "").upper()
        if _otype == "MARKET":
            fee_rate = self.cfg.TAKER_FEE
        elif _otype in ("LIMIT_MAKER", "LIMIT"):
            fee_rate = self.cfg.MAKER_FEE
        else:
            # No type info (synthesized maker fill via _build_filled_result, or TWAP).
            # Maker fills come from the LIMIT path (USE_LIMIT True); TWAP runs only when
            # USE_LIMIT is False (taker). So the USE_LIMIT flag is the correct tiebreaker here.
            fee_rate = self.cfg.MAKER_FEE if self.cfg.USE_LIMIT else self.cfg.TAKER_FEE
        qty=size/sig.price; fee=size*fee_rate
        # v9.6 FIX: capture original SL distance BEFORE overwriting sig.price with fill price
        orig_signal_price = sig.price
        orig_sl_dist = abs(orig_signal_price - sig.sl) if sig.sl else 0
        orig_tp_dist = abs(sig.tp - orig_signal_price) if sig.tp else 0
        # v10.0: slippage telemetry
        slip_pct_raw = 0.0
        if order.get("fills"):
            tq=sum(float(f["qty"]) for f in order["fills"])
            tc=sum(float(f["qty"])*float(f["price"]) for f in order["fills"])
            if tq>0:
                qty,sig.price,size=tq,tc/tq,tc
                # v9.6 FIX: re-anchor SL and TP to actual fill price so R:R is preserved
                if orig_sl_dist > 0:
                    # v11.2.19 FIX: slippage distortion — was absolute dollar shift which
                    # skews R:R on cheap/volatile coins. Now pct-based: preserves intended
                    # risk geometry regardless of price magnitude.
                    sl_pct = orig_sl_dist / orig_signal_price if orig_signal_price > 0 else 0.01
                    tp_pct = orig_tp_dist / orig_signal_price if orig_signal_price > 0 else 0.02
                    new_sl = sig.price * (1 - sl_pct)
                    new_tp = sig.price * (1 + tp_pct)
                    slip_pct_raw = (sig.price - orig_signal_price) / orig_signal_price if orig_signal_price > 0 else 0.0
                    slip_pct = slip_pct_raw * 100
                    # v11.2.1: SLIP-WARN — loud alert when slip exceeds MAX_SLIP_PCT, but
                    # DO NOT abort. Order already filled on Binance — assets are in account
                    # whether we track them or not. Returning None on abort created an
                    # untracked-position bug (asset orphaned, no SL, no TP, no exit logic).
                    # Now: alert operator, persist to slip_telemetry.jsonl for analysis,
                    # and continue to track the position with re-anchored SL/TP. Worst case
                    # is a worse-than-intended entry with proper R:R-preserved exits, which
                    # is strictly better than an invisible untracked position in a live account.
                    if abs(slip_pct_raw) > self.cfg.MAX_SLIP_PCT:
                        log.warning(f"⚠️ SLIP-WARN {sig.pair}: signal=${orig_signal_price:.4f} "
                                    f"fill=${sig.price:.4f} slip={slip_pct:+.3f}% "
                                    f"> {self.cfg.MAX_SLIP_PCT*100:.2f}% — TRACKED with re-anchored SL/TP")
                        try:
                            tg.send(f"⚠️ <b>HIGH SLIP</b> {sig.pair}\n"
                                    f"Signal: ${orig_signal_price:.4f}\n"
                                    f"Fill: ${sig.price:.4f}\n"
                                    f"Slip: {slip_pct:+.3f}% (threshold {self.cfg.MAX_SLIP_PCT*100:.2f}%)\n"
                                    f"Position TRACKED — SL/TP re-anchored to fill price\n"
                                    f"R:R preserved. Review slip_telemetry.jsonl.")
                        except Exception: pass
                    elif abs(slip_pct) > 5.0:  # v11.2.22 FIX: was 0.05% (5bps) — spammed log for every normal spread
                        log.info(f"  📐 {sig.pair} slip {slip_pct:+.2f}% — SL/TP re-anchored: "
                                 f"SL ${sig.sl:.4f}→${new_sl:.4f}  TP ${sig.tp:.4f}→${new_tp:.4f}")
                    sig.sl, sig.tp = new_sl, new_tp
                    fee = size * fee_rate
        # v10.0: structured slip telemetry — append to trade journal regardless of abort
        # v13.5: switched to append_jsonl helper for size-bounded rotation (5 MB).
        try:
            from journal_utils import append_jsonl
            append_jsonl("slip_telemetry.jsonl", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "pair": sig.pair, "strategy": sig.strategy,
                "signal_price": orig_signal_price, "fill_price": sig.price,
                "slip_pct": round(slip_pct_raw * 100, 4),
                "exceeded_max": abs(slip_pct_raw) > self.cfg.MAX_SLIP_PCT,
                "size": round(size, 4),
            })
        except Exception as e:
            log.warning(f"slip_telemetry write failed: {e}")

        # v13.5: SL floor/ceiling now applied in can_trade() before size calc,
        # so size and SL stay consistent. We just need a defensive re-cap here
        # in case slippage re-anchoring above (line ~318) widened SL beyond
        # the 10% ceiling. Floor doesn't apply post-slip because slip already
        # respected it.
        _sl_pct = abs(sig.price - sig.sl) / sig.price if sig.price > 0 else 0.01
        if _sl_pct > 0.10:
            log.warning(f"  ⚠️ {sig.pair} post-slip SL exceeds 10% — capping (sl_pct={_sl_pct*100:.2f}%)")
            _sl_pct = 0.10
        _capped_sl = sig.price * (1 - _sl_pct)
        pos=Position(pair=sig.pair,entry=sig.price,qty=qty,size=size,
            entry_time=datetime.now(timezone.utc).isoformat(),sl=_capped_sl,tp=sig.tp,
            group=sig.group,high=sig.price,strategy=sig.strategy,atr=sig.atr,
            entry_fee=fee,avg_entry=sig.price,total_qty=qty,total_cost=size,
            rr=sig.rr,grade=sig.grade,context=f"{ctx.regime}/{ctx.killzone}",
            regime_at_entry=getattr(ctx, 'daily', ''))  # v18.5 (D5): daily trend at entry
        self.positions.append(pos); self.daily_t+=1; self.fees+=fee
        # v15.13 FIX (1A): offload native SL/TP attach to background threads.
        # open_pos is called from _cycle — synchronous HTTP calls here block the
        # entire async event loop for 200–800ms per trade, freezing WS feeds and
        # trailing stops during multi-signal breakouts.
        # v16.0 AUDIT FIX H1: was fire-and-forget — now checks result, retries, alerts.
        async def _safe_sl_attach(_pos):
            try:
                _ok = await asyncio.to_thread(self.native_sl.attach, _pos)
                if _ok is False:
                    log.warning(f'⚠️ Native SL attach returned False for {_pos.pair} — retrying once')
                    await asyncio.sleep(2)
                    _ok2 = await asyncio.to_thread(self.native_sl.attach, _pos)
                    if _ok2 is False:
                        log.error(f'❌ Native SL attach FAILED {_pos.pair} after retry — position UNPROTECTED on exchange')
                        if self.tg:
                            try: self.tg.send(f"⚠️ <b>NATIVE SL ATTACH FAILED</b> {_pos.pair}\n"
                                              f"⛔ Position has NO exchange-side SL\n"
                                              f"🛡️ Software SL active at ${_pos.sl:.4f}\n"
                                              f"⚠️ Manual review required")
                            except Exception: pass
                        return
                log.info(f'  🛡️ Native SL attached for {_pos.pair}')
            except Exception as _e:
                log.warning(f'Native SL attach failed {_pos.pair}: {_e}')
                if self.tg:
                    try: self.tg.send(f"⚠️ <b>NATIVE SL ERROR</b> {_pos.pair}: {_e}")
                    except Exception: pass
        async def _safe_tp_attach(_pos):
            try:
                _ok = await asyncio.to_thread(self.native_tp.attach, _pos)
                if _ok is False:
                    log.warning(f'⚠️ Native TP attach returned False for {_pos.pair}')
                else:
                    log.info(f'  🎯 Native TP attached for {_pos.pair}')
            except Exception as _e:
                log.warning(f'Native TP attach failed {_pos.pair}: {_e}')
        if getattr(self, 'native_sl', None):
            try: asyncio.create_task(_safe_sl_attach(pos))
            except Exception as _e: log.warning(f'Native SL attach dispatch failed: {_e}')
        # v18.3 FIX C3: Native TP dispatch removed from open_pos to avoid race condition with Native SL.
        # Native TP is now only attached in chase mode (when Native SL is detached).
        self._log_trade("BUY",pos,sig.price,qty,size,fee)
        # v15.0: TCA entry capture
        if _TCA is not None:
            try: _TCA.record_entry(pos, signal_price=orig_signal_price,
                                   order_type=("LIMIT_MAKER" if self.cfg.USE_LIMIT else "MARKET"))
            except Exception: pass
        # v15.2 #2 FIX: audit log entry decision (hash-chained, tamper-evident)
        _audit = getattr(getattr(self, '_bot_ref', None), '_audit', None)
        if _audit is not None:
            try: _audit.log("ENTRY", pair=pos.pair, strategy=pos.strategy,
                            grade=pos.grade, conf=getattr(sig, 'conf', 0),
                            entry=pos.avg_entry, sl=pos.sl, tp=pos.tp,
                            qty=pos.qty, size_usd=pos.size)
            except Exception: pass
        self.save_state()
        # v15.4 TG UPGRADE: render chart and attach to BUY alert (best-effort, never blocks).
        # Fetches its own candles via exchange.klines() — 1 extra REST call per BUY (~50ms).
        _chart_png = None
        if getattr(self.cfg, 'TG_CHARTS_ENABLED', True) and getattr(self.cfg, 'TG_ENABLED', False):
            try:
                from telegram_charts import render_trade_chart
                _exch = getattr(self, '_bot_ref', None)
                _exch = _exch.ex if _exch else None
                if _exch:
                    _candles = _exch.klines_sync(sig.pair, "5m", 60)  # v15.3 FIX: sync helper
                    if _candles and len(_candles) >= 5:
                        _chart_png = render_trade_chart(_candles, entry=sig.price, sl=sig.sl, tp=sig.tp,
                                                         pair=sig.pair, strategy=sig.strategy, action="BUY")
            except Exception as _ce:
                log.debug(f"BUY chart render skipped: {_ce}")
        tg.trade_alert("BUY",sig.pair,sig.price,sig.strategy,conf=sig.conf,
                       qty=qty,size=size,tp=sig.tp,sl=sig.sl,grade=sig.grade,
                       chart_bytes=_chart_png)
        return pos

    async def check_pyramid(self, tickers, ctx, ex):  # v15.4 FIX (P1-2): async — ex.buy is async
        """v7: Anti-Martingale — add to winning positions."""
        if not self.cfg.PYRAMID_ENABLED: return
        # v15.13 FIX (2B): enforce MAX_HEAT cap before pyramid buys.
        # Without this, pyramids bypass the global portfolio heat circuit
        # breaker during cascading dumps, draining the account.
        _max_heat = getattr(self.cfg, 'FEAR_HEAT', self.cfg.MAX_HEAT) if getattr(ctx, 'fg', 50) < 20 else self.cfg.MAX_HEAT
        if self.portfolio_heat >= _max_heat:
            log.debug(f"🔥 Pyramid skipped — heat {self.portfolio_heat*100:.1f}% >= cap {_max_heat*100:.1f}%")
            return
        for pos in self.positions:
            if pos.pyramids >= 2: continue  # Max 2 adds
            price = tickers.get(pos.pair, 0)
            if price == 0: continue
            pct = (price - pos.avg_entry) / pos.avg_entry if pos.avg_entry > 0 else 0
            if pct >= self.cfg.PYRAMID_THRESHOLD:
                add_size = pos.size * 0.3  # Add 30% of original
                if self.available >= add_size and add_size >= self.cfg.MIN_TRADE:
                    q = add_size / price
                    result = await ex.buy(pos.pair, q)  # v15.4 FIX: was unawaited — buy never executed
                    if "error" in result: continue
                    fq = sum(float(f["qty"]) for f in result.get("fills",[])) or q
                    fc = sum(float(f["qty"])*float(f["price"]) for f in result.get("fills",[])) or (fq*price)  # v11.2.18 FIX: actual fill cost
                    pos.total_cost += fc  # v11.2.18 FIX: was add_size (requested) → fc (actual filled cost)
                    pos.total_qty += fq  # v11.2.19 FIX: was inside comment — total_qty never updated, avg_entry exploded
                    # v11.2.21 FIX: weighted avg of remaining+new, not lifetime total
                    pos.avg_entry = ((pos.qty * pos.avg_entry) + fc) / (pos.qty + fq) if (pos.qty + fq) > 0 else pos.avg_entry
                    pos.qty += fq  # v11.2.21 FIX: was pos.total_qty — resurrected sold coins
                    pos.size = pos.qty * pos.avg_entry
                    pos.entry_fee += price * fq * self.cfg.TAKER_FEE  # v11.2.22 FIX: pyramid fee never tracked
                    pos.pyramids += 1
                    # v9.7.2 FIX: use avg_entry (volume-weighted) for true breakeven, not original entry
                    pos.sl = max(pos.sl, pos.avg_entry * 0.998)
                    log.info(f"🔺 Pyramid #{pos.pyramids} {pos.pair} +${add_size:.2f} | Avg:${pos.avg_entry:.4f}")
                    self.save_state()

    async def check_exits(self, tickers, ctx, ex, tg) -> list:  # v15.4 FIX (P1-1): async — ex.sell is async
        """v8.4: Returns list of (pos, price, reason) — does NOT sell or record PnL.
        Main loop sells first, then calls _record_close() only after sell confirms."""
        to_close = []  # list of (pos, price, reason)
        self._exit_cycle_counter = getattr(self, '_exit_cycle_counter', 0) + 1  # v16.0 AUDIT FIX M9
        for pos in self.positions:
            price=tickers.get(pos.pair,0)
            # v9.1: Fallback — if WS price is 0, get from API (SL protection)
            if price==0:
                try:
                    price = float((await ex.get_symbol_ticker(pos.pair))['price'])  # v15.4 FIX (P1-3): async, no event loop block
                except Exception as _e: log.debug(f"Ticker fetch failed for {pos.pair}: {_e}")  # v14.1 FIX (ISSUE D): was silent — now logged at debug level
            if price==0: continue
            if float(price) > float(pos.high): pos.high = float(price)
            pct=(float(price)-float(pos.avg_entry))/float(pos.avg_entry) if float(pos.avg_entry)>0 else 0
            # v14.5.2: Check recent candle HIGH for BE/profit locks
            # Fixes: brief spike crosses BE trigger between 30s scan cycles → missed
            # Example: BCH hit $385 (BE=$384.38) but bot polled at $382 → missed BE
            # v16.0 AUDIT FIX M9: only run every 3rd cycle to reduce API calls
            # (was 1 REST call per position per cycle = 12 calls/min with 3 positions)
            _cycle_num = getattr(self, '_exit_cycle_counter', 0)
            if not getattr(pos, "be_locked", False) and _cycle_num % 3 == 0:
                try:
                    _c5 = await ex.klines(pos.pair, "5m", 3)  # v15.3 FIX: sync helper
                    _c_high = max(c.h for c in _c5) if _c5 else price
                    if _c_high > price and _c_high > pos.high:
                        pos.high = _c_high
                        _c_pct = (_c_high - pos.avg_entry) / pos.avg_entry if pos.avg_entry > 0 else 0
                        if _c_pct > pct:
                            pct = _c_pct
                            log.debug(f"📈 {pos.pair} candle high ${_c_high:.4f} used for BE check (WS had ${price:.4f})")
                except Exception: pass

            # Scale-out (sells immediately, records partial PnL)
            if self.cfg.SCALE_OUT and pct>0:
                for i,lvl in enumerate(self.cfg.SCALE_LEVELS):
                    if i in pos.scale_done: continue
                    rr_t=lvl["rr"]
                    if rr_t==0: continue
                    sl_d=(pos.atr if pos.atr>0 else pos.avg_entry*0.01); tp_t=pos.avg_entry+sl_d*rr_t  # v11.2.18 FIX: use ATR not mutating SL
                    if price>=tp_t:
                        # v9.7.1 FIX: use total_qty (entry size) so percentages stay stable across levels
                        sq=pos.total_qty*lvl["pct"]
                        # v11.2.20 FIX: scale-out 0% runner bug — if levels sum to 100%
                        # pos.qty hits 0, final exit records $0 PnL + Binance MIN_NOTIONAL error
                        remaining = pos.qty - sq
                        if remaining < pos.qty * 0.05 and remaining > 0:  # must leave ≥5% runner, allow final sell
                            to_close.append((pos, price, "SCALE_DUST_SWEEP"))
                            continue
                        if sq>0:
                            detached_native_sl = False
                            if getattr(self, 'native_sl', None) and getattr(pos, 'native_sl_order_id', None):
                                try:
                                    detached_native_sl = bool(await asyncio.to_thread(self.native_sl.detach, pos))  # v15.4 FIX (P3): offload sync HTTP
                                    if not detached_native_sl:
                                        log.warning(f"Scale-out skipped for {pos.pair}: native SL detach failed")
                                        continue
                                except Exception as _e:
                                    log.warning(f"Scale-out skipped for {pos.pair}: native SL detach error {_e}")
                                    continue
                            r=await ex.sell(pos.pair,sq)  # v15.4 FIX: was unawaited — sell never executed
                            if "error" in r:
                                if detached_native_sl:
                                    try: await asyncio.to_thread(self.native_sl.attach, pos)  # v15.6 FIX: was create_task (fire-forget) — must await to catch errors
                                    except Exception as _e: log.warning(f"Native SL restore after scale-out sell failure failed: {_e}")
                                continue
                            pp=(price-pos.avg_entry)*sq
                            scale_fee=price*sq*self.cfg.TAKER_FEE
                            # v10.7 FIX: also deduct prorated ENTRY fee for the scaled-out
                            # portion. Was: only exit fee subtracted → entry fee for scaled
                            # qty leaked into PnL. Now: scale_pct applied against ORIGINAL
                            # total_qty (preserves v9.7.1 stable-percentage design).
                            # pos.entry_fee and pos.total_qty UNCHANGED so _record_close's
                            # existing proration math (line 2845) still works correctly:
                            # final close prorates entry_fee × (remaining_qty/total_qty),
                            # which equals exactly the un-scaled portion's entry fee.
                            scale_pct = sq / pos.total_qty if pos.total_qty > 0 else 0
                            scale_entry_fee = pos.entry_fee * scale_pct
                            total_scale_fee = scale_fee + scale_entry_fee
                            self.pnl+=pp-total_scale_fee
                            self.daily_pnl+=pp-total_scale_fee
                            self.fees+=scale_fee  # v11.2.21 FIX: was total_scale_fee — entry fee already counted at open_pos
                            pos.qty-=sq; pos.scale_done.append(i)
                            pos.size = pos.qty * pos.avg_entry  # v9.1: keep size accurate
                            log.info(f"💰 Scale {pos.pair} {lvl['pct']*100:.0f}% +${pp:.4f}")
                            self._log_trade("SCALE",pos,price,sq,sq*price,total_scale_fee)  # v11.2.23 FIX: scale-out was missing from journal
                            tg.send(f"💰 <b>SCALE OUT</b> {pos.pair}\n📊 Sold {lvl['pct']*100:.0f}% | Qty: {sq:.2f}\n💲 Price: ${price:.4f} | Profit: ${pp:.4f}\n📦 Remaining: {pos.qty:.2f} | Size: ${pos.size:.2f}")
                            if detached_native_sl and getattr(self, 'native_sl', None):
                                try:
                                    await asyncio.to_thread(self.native_sl.attach, pos)  # v15.6 FIX: was create_task (fire-forget) — must await
                                except Exception as _e:
                                    log.warning(f"Native SL reattach after scale-out failed: {_e}")
                            self.save_state()

            # ═══ v8.4: SMART EXIT LOGIC ═══
            price = float(price)  # v14.6.3 FIX: tickers may return str
            # 1. Hard SL — absolute floor
            # v11.2.16: SLIPPAGE FIX — trigger SL 0.3% early so market order fills near intended level
            sl_trigger = pos.sl * (1 + getattr(self.cfg, 'SL_TRIGGER_BUFFER', 0.003))
            if float(price)<=sl_trigger: to_close.append((pos,float(price),"SL")); continue

            # v15.11.0: VELOCITY EXIT — exit early when price crashes fast toward SL.
            # Fires only when: price below entry (in loss), not BE-locked, fast consecutive decline.
            # Gives better fill than waiting for SL slippage on a sharp move.
            # Design: track last 3 price readings (90s window). If all declining AND rate >
            # VELOCITY_EXIT_THRESHOLD (%/min) AND within VELOCITY_EXIT_PROXIMITY of SL → exit now.
            if (price < pos.avg_entry
                    and price > pos.sl
                    and not getattr(pos, 'be_locked', False)
                    and getattr(self.cfg, 'VELOCITY_EXIT_ENABLED', True)):
                _now = time.time()
                _vh = getattr(pos, 'px_vel_hist', [])
                _vh.append((_now, price))
                _vh = [(t, p) for t, p in _vh if t > _now - 90]  # keep 90s window
                pos.px_vel_hist = _vh
                if len(_vh) >= 3:
                    _px = [p for t, p in _vh[-3:]]
                    _ts = [t for t, p in _vh[-3:]]
                    # All 3 readings must be consecutively declining (no bounce)
                    if all(_px[i] > _px[i+1] for i in range(len(_px) - 1)):
                        _elapsed = max(_ts[-1] - _ts[0], 5.0)  # v15.13 FIX (2A): was 0.1 — micro-tick panic. 3 WS ticks in 0.1s made 0.05% flutter = 30%/min velocity → instant panic sell. 5s minimum ensures velocity is calculated over meaningful time window.
                        _drop_rate = (_px[0] - _px[-1]) / max(_px[0], 0.0001)
                        _vel_per_min = (_drop_rate / _elapsed) * 60
                        _gap_to_sl = (price - pos.sl) / max(pos.sl, 0.0001)
                        _vel_thresh = getattr(self.cfg, 'VELOCITY_EXIT_THRESHOLD', 0.004)
                        _prox_thresh = getattr(self.cfg, 'VELOCITY_EXIT_PROXIMITY', 0.015)
                        if _vel_per_min > _vel_thresh and _gap_to_sl < _prox_thresh:
                            log.info(
                                f"⚡ {pos.pair} VELOCITY EXIT — crash {_vel_per_min*100:.2f}%/min "
                                f"| gap to SL {_gap_to_sl*100:.2f}% | exiting early"
                            )
                            if tg:
                                try:
                                    tg.send(
                                        f"⚡ <b>VELOCITY EXIT</b> {pos.pair}\n"
                                        f"🔻 Speed: {_vel_per_min*100:.2f}%/min\n"
                                        f"📍 Gap to SL: {_gap_to_sl*100:.2f}%\n"
                                        f"🚪 Exiting early — better than SL fill"
                                    )
                                except Exception:
                                    pass
                            to_close.append((pos, price, "VELOCITY_EXIT"))
                            continue

            # v16.0: PROGRESSIVE SL TIGHTENING — time-decay stop-loss.
            # Paper §3.4: widen SL initially, then tighten to free stagnant capital.
            # Only fires when: (a) no lock yet, (b) price below entry, (c) trade aged.
            # Tightens -3% → -2% → -1.5% linearly over hours 2-4 of hold time.
            if (not getattr(pos, 'be_locked', False)
                    and price < pos.avg_entry
                    and getattr(self.cfg, 'PROGRESSIVE_SL_ENABLED', True)):
                try:
                    from datetime import datetime, timezone as _tzone
                    _edt = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
                    _age_h = (datetime.now(_tzone.utc) - _edt).total_seconds() / 3600
                    if _age_h >= 2.0:
                        _orig_sl_pct = getattr(self.cfg, 'STOP_LOSS_PCT', 0.03)
                        _floor_sl_pct = getattr(self.cfg, 'PROGRESSIVE_SL_FLOOR', 0.015)
                        _factor = min(1.0, (_age_h - 2.0) / 2.0)  # 0→1 over hours 2-4
                        _tight_pct = _orig_sl_pct - _factor * (_orig_sl_pct - _floor_sl_pct)
                        _new_sl = round(pos.avg_entry * (1.0 - _tight_pct), 8)
                        if _new_sl > pos.sl:  # only tighten, never widen
                            _old = pos.sl
                            pos.sl = _new_sl
                            log.info(
                                f"⏱ {pos.pair} SL decay {_age_h:.1f}h "
                                f"${_old:.4f}→${pos.sl:.4f} (-{_tight_pct*100:.1f}%)"
                            )
                            if tg and abs(_new_sl - _old) > pos.avg_entry * 0.002:
                                try: tg.send(
                                    f"⏱ <b>SL TIGHTENING</b> {pos.pair}\n"
                                    f"Hold: {_age_h:.1f}h\n"
                                    f"SL: ${_old:.4f} → ${pos.sl:.4f} (-{_tight_pct*100:.1f}%)"
                                )
                                except Exception: pass
                except Exception as _pse:
                    log.debug(f"Progressive SL {pos.pair}: {_pse}")

            # v15.15: TIME-BASED EXIT — close stagnant trades in BEAR regime.
            # A trade sitting at/below entry for 4h locks capital and is probably failing.
            # Only exits if price < entry+0.5% (no meaningful progress made).
            # Preserves trades that ARE working (+0.5%+ progress → keep holding).
            if getattr(self.cfg, 'TIME_EXIT_ENABLED', True):
                try:
                    from datetime import datetime, timezone as _tz
                    _entry_dt = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
                    _age_h = (datetime.now(_tz.utc) - _entry_dt).total_seconds() / 3600
                    _progress = (price - pos.avg_entry) / pos.avg_entry if pos.avg_entry > 0 else 0
                    _max_age = getattr(self.cfg, 'TIME_EXIT_HOURS_BEAR', 4.0)
                    _min_prog = getattr(self.cfg, 'TIME_EXIT_PROGRESS_MIN', 0.005)
                    _regime_daily = getattr(getattr(self, '_last_ctx', None), 'daily', '') if hasattr(self, '_last_ctx') else ''
                    _is_bear = 'BEAR' in str(getattr(pos, 'regime_at_entry', _regime_daily))
                    if _age_h >= _max_age and _progress < _min_prog and _is_bear:
                        log.info(
                            f"⏰ {pos.pair} TIME EXIT — {_age_h:.1f}h hold, "
                            f"progress {_progress*100:.2f}% < {_min_prog*100:.1f}% threshold in BEAR"
                        )
                        if tg:
                            try:
                                tg.send(
                                    f"⏰ <b>TIME EXIT</b> {pos.pair}\n"
                                    f"⏱ Hold: {_age_h:.1f}h\n"
                                    f"📊 Progress: {_progress*100:.2f}%\n"
                                    f"🚪 Stagnant in BEAR — freeing capital"
                                )
                            except Exception:
                                pass
                        to_close.append((pos, price, "TIME_EXIT"))
                        continue
                except Exception as _te:
                    log.debug(f"Time exit check failed {pos.pair}: {_te}")

            # ═══ v14.7 EXIT LADDER — proportional locks + TP floor with chase ═══
            # Replaces: BE-lock, +2%/+3.5%/75% locks, Group-D ladder, trail.
            #   A. Lock profit at proportional steps across the entry→TP range
            #   B. When TP is reached, DO NOT sell — pin SL at TP exactly
            #   C. Above TP, ratchet SL upward (chase) with proportional gap
            #   D. Crash protection: when price falls back to SL (≥ TP), hard SL fires
            tp_dist = pos.tp - pos.avg_entry
            if tp_dist > 0:
                high_price = max(pos.high, price)

                # ── A. In-range proportional locks (only BEFORE TP) ──
                if not getattr(pos, 'tp_floor_locked', False):
                    # v15.14: 3-rung real-profit ladder.
                    # REMOVED the +0.68% tiny lock — it was creating +0.5% consolation exits
                    # in CHOPPY regime where every micro-pump gets immediately sold back.
                    # Philosophy: either the trade makes REAL money or it takes the full SL.
                    # No more +0.5% tiny exits that feel worse than a clean loss.
                    #
                    # Format: (peak_trig_frac_of_TP, lock_frac_of_TP, label)
                    # v15.15: ABSOLUTE-% ladder — trigger/lock levels are % from entry,
                    # completely independent of TP setting. Clean and TP-change-safe.
                    # Format: (peak_pct_above_entry, lock_pct_above_entry, label)
                    # Any entry, any TP:
                    #   Rung 1: peak +1.5% → SL locked at +1.0%  effective exit ~+1.3%
                    #   Rung 2: peak +3.0% → SL locked at +2.5%  effective exit ~+2.8%
                    #   Rung 3: peak +3.5% → SL locked at +3.0%  effective exit ~+3.3%
                    # After peak +3.5%: ATR ghost trail takes over.
                    # TP hit: SL pinned at TP → CHASE mode.
                    # Iterates REVERSED: only highest applicable rung fires per cycle.
                    if getattr(self.cfg, 'PROFIT_LADDER_ENABLED', False) and pos.avg_entry > 0:
                        # v18.8.9 SCALE LADDER: fixed (trigger, lock) levels — sell a chunk AND ratchet
                        # the SL at each level (+1.5/+2.5/+3.5 → lock +1.0/+2.0/+3.0 by default).
                        # Replaces v18.8.7's ATR micro-steps that exited for tiny profit. Reuses the
                        # SL-ratchet loop below; per-level scale-outs (min-notional-gated) fire in the
                        # scale block. Chase mode (sections B/C) still rides past TP.
                        _lvls = getattr(self.cfg, 'PROFIT_LADDER_LEVELS', None) \
                                or ((0.015, 0.010), (0.025, 0.020), (0.035, 0.030))
                        _ladder = [(float(_t), float(_l), "📤 SCALE") for (_t, _l) in _lvls]
                    elif getattr(self.cfg, "USE_MULTI_TIER_LADDER", True):
                        # v15.15.1: Group-aware ladder — 0.50% buffer at every rung.
                        # A=calm/large-cap (lock later)  B=standard  C=fast alts  D=volatile
                        _grp = getattr(pos, 'group', 'B')
                        if _grp == 'A':
                            _ladder = [
                                (0.0200, 0.0150, "🔒 LOCK"),  # +2.0%→+1.5%  buf 0.50% CALM
                                (0.0350, 0.0300, "🪜 LOCK"),  # +3.5%→+3.0%  buf 0.50%
                                (0.0400, 0.0350, "🪜 LOCK"),  # +4.0%→+3.5%  buf 0.50%
                            ]
                        elif _grp == 'C':
                            _ladder = [
                                (0.0125, 0.0075, "🔒 LOCK"),  # +1.25%→+0.75% buf 0.50% FAST
                                (0.0200, 0.0150, "🪜 LOCK"),  # +2.00%→+1.50% buf 0.50%
                                (0.0300, 0.0250, "🪜 LOCK"),  # +3.00%→+2.50% buf 0.50%
                            ]
                        elif _grp == 'D':
                            _ladder = [
                                (0.0100, 0.0050, "🔒 LOCK"),  # +1.0%→+0.5%  buf 0.50% VOLATILE
                                (0.0150, 0.0100, "🪜 LOCK"),  # +1.5%→+1.0%  buf 0.50%
                                (0.0250, 0.0200, "🪜 LOCK"),  # +2.5%→+2.0%  buf 0.50%
                            ]
                        else:  # Group B default
                            _ladder = [
                                (0.0150, 0.0100, "🔒 LOCK"),  # +1.5%→+1.0%  buf 0.50% STANDARD
                                (0.0250, 0.0200, "🪜 LOCK"),  # +2.5%→+2.0%  buf 0.50%
                                (0.0350, 0.0300, "🪜 LOCK"),  # +3.5%→+3.0%  buf 0.50%
                            ]
                    else:
                        _ladder = [(0.010, 0.002, "🔒 BE LOCK")]  # legacy fallback
                    for _enum_i, (trig_pct, lock_pct, label) in enumerate(reversed(_ladder)):
                        trig_price = pos.avg_entry * (1 + trig_pct)
                        if high_price >= trig_price:
                            new_sl = pos.avg_entry * (1 + lock_pct)
                            new_sl = round(new_sl, 8)
                            # v18.8.8 FIX: never lock the SL at/above the current price. A sell-stop
                            # above market is rejected by Binance ("would trigger immediately" →
                            # "NATIVE SL MOVE FAILED") and forces a worse-than-intended market exit.
                            # Clamp the lock to just below price so it stays a valid, placeable trailing
                            # stop. Only bites when price has already retraced THROUGH the lock level
                            # (fast pullback between scans) — the exact case that hit TIAUSDT.
                            _sl_cap = round(float(price) * (1 - getattr(self.cfg, 'NATIVE_SL_BUFFER_PCT', 0.005)), 8)
                            if new_sl > _sl_cap:
                                new_sl = _sl_cap
                            if new_sl > pos.sl:
                                old_sl = pos.sl
                                pos.sl = new_sl
                                pos.be_locked = True
                                # v15.10.0: dynamic lock-% label so user sees actual profit secured
                                _lock_pct = ((new_sl - pos.avg_entry) / pos.avg_entry * 100) if pos.avg_entry > 0 else 0
                                _label_full = f"{label} (+{_lock_pct:.2f}% locked)"
                                log.info(f"🔒 {pos.pair} {_label_full} → SL ${old_sl:.4f}→${pos.sl:.4f}")
                                if self.tg:
                                    try: self.tg.send(f"🔒 <b>{_label_full}</b> {pos.pair}\nSL ${old_sl:.4f} → ${pos.sl:.4f}")
                                    except Exception: pass
                                if getattr(self, 'native_sl', None):
                                    # v15.12.0: retry up to SL_MOVE_RETRIES times — fixes MOVE FAILED
                                    _move_ok = False
                                    _retries = getattr(self.cfg, 'SL_MOVE_RETRIES', 3)
                                    _delay = getattr(self.cfg, 'SL_MOVE_RETRY_DELAY', 2.0)
                                    for _attempt in range(_retries):
                                        try:
                                            _m = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)
                                            if _m is not False:
                                                _move_ok = True
                                                break
                                            log.debug(f"Native SL move attempt {_attempt+1}/{_retries} returned False {pos.pair}")
                                        except Exception as _e:
                                            log.warning(f"Native SL move attempt {_attempt+1}/{_retries} failed {pos.pair}: {_e}")
                                        if _attempt < _retries - 1:
                                            await asyncio.sleep(_delay)
                                    if not _move_ok:
                                        log.warning(f"Native SL move FAILED all {_retries} attempts {pos.pair}")
                                        if self.tg:
                                            try: self.tg.send(f"⚠️ NATIVE SL {label} MOVE FAILED {pos.pair} ({_retries} retries)")
                                            except Exception: pass
                                self.save_state()
                                # v16.0 / v18.8.7: partial scale-out at rungs.
                                _total_rungs = len(_ladder)
                                _fwd_idx = _total_rungs - 1 - _enum_i  # convert reversed→forward
                                if getattr(self.cfg, 'PROFIT_LADDER_ENABLED', False):
                                    # v18.8.7: bank a slice at EACH new rung. Runs only when the SL just
                                    # ratcheted up (so each rung fires at most once). Smart-skip when the
                                    # slice OR the leftover would fall under the exchange min-notional —
                                    # on a small account this means pure trailing, no sells, until the
                                    # position is large enough to slice cleanly.
                                    if getattr(self.cfg, 'PARTIAL_SCALEOUT_ENABLED', True) and pos.qty > 0:
                                        _slice_pct = getattr(self.cfg, 'PROFIT_LADDER_SCALE_PCT', 0.15)
                                        _min_usd = getattr(self.cfg, 'PROFIT_LADDER_MIN_SLICE_USD', 5.0)
                                        _slice_usd = pos.qty * _slice_pct * float(price)
                                        _remain_usd = pos.qty * (1.0 - _slice_pct) * float(price)
                                        if _slice_usd >= _min_usd and _remain_usd >= _min_usd:
                                            self._pending_partials.append(
                                                (pos, float(price), _slice_pct, f"LADDER_R{_fwd_idx+1}")
                                            )
                                            log.info(f"📤 {pos.pair} ladder scale {_slice_pct*100:.0f}% (${_slice_usd:.2f}) at rung {_fwd_idx+1}")
                                        else:
                                            log.debug(f"{pos.pair} ladder rung {_fwd_idx+1} sell skipped — slice ${_slice_usd:.2f} < ${_min_usd}; trailing only")
                                elif (getattr(self.cfg, 'PARTIAL_SCALEOUT_ENABLED', True)
                                        and _fwd_idx == 1  # rung 2 (0-indexed)
                                        and getattr(pos, 'last_scale_rung', -1) < 1
                                        and pos.qty > 0):
                                    pos.last_scale_rung = 1
                                    self._pending_partials.append(
                                        (pos, float(price), getattr(self.cfg, 'PARTIAL_SCALEOUT_PCT', 0.40), "SCALE_OUT_R2")
                                    )
                                    log.info(f"📤 {pos.pair} partial scale-out 40% queued at rung 2")
                            break  # v15.10.0: only ratchet to highest applicable rung per cycle

                # ── B. TP REACHED — pin SL at TP, NO SELL, enter chase mode ──
                if high_price >= pos.tp and not getattr(pos, 'tp_floor_locked', False):
                    pos.tp_floor_locked = True
                    pos.be_locked = True
                    new_sl = round(pos.tp, 8)
                    if new_sl > pos.sl:
                        old_sl = pos.sl
                        pos.sl = new_sl
                        log.info(f"🎯 {pos.pair} TP REACHED ${pos.tp:.4f} → SL pinned at TP (chase mode, NO SELL)")
                        if self.tg:
                            try: self.tg.send(f"🎯 <b>TP REACHED — CHASE</b> {pos.pair}\n💲 Hit TP ${pos.tp:.4f}\n🔒 SL = TP exactly\n🚀 Letting it run")
                            except Exception: pass
                        if getattr(self, "native_tp", None):
                            try: await asyncio.to_thread(self.native_tp.detach, pos)
                            except Exception: pass
                        if getattr(self, 'native_sl', None):
                            try:
                                _m = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                                if _m is False and self.tg:
                                    try: self.tg.send(f"⚠️ NATIVE SL TP-FLOOR MOVE FAILED {pos.pair}")
                                    except Exception: pass
                            except Exception as _e:
                                log.warning(f"Native SL TP-floor move failed {pos.pair}: {_e}")
                        self.save_state()

                # ── C. CHASE — ratchet SL up as price climbs above TP ──
                # v14.7.3: progressive tightening — gap shrinks as overshoot grows.
                # The higher above TP, the tighter we ratchet (lock more profit).
                #
                # Example (entry $100, TP $150, range $50):
                #   High $160 (overshoot  7%) → gap 15% of range ($7.50)  → SL ~$152
                #   High $165 (overshoot 10%) → gap 15%             ($7.50) → SL ~$157
                #   High $180 (overshoot 20%) → gap 12%             ($6.00) → SL ~$174
                #   High $200 (overshoot 33%) → gap  8%             ($4.00) → SL ~$196
                #   High $225 (overshoot 50%) → gap  5%             ($2.50) → SL ~$222
                if getattr(pos, 'tp_floor_locked', False) and high_price > pos.tp:
                    overshoot_pct = (high_price - pos.tp) / pos.tp if pos.tp > 0 else 0
                    if   overshoot_pct >= 0.50: chase_frac = 0.05   # very tight
                    elif overshoot_pct >= 0.30: chase_frac = 0.08
                    elif overshoot_pct >= 0.15: chase_frac = 0.12
                    elif overshoot_pct >= 0.05: chase_frac = 0.15
                    else:                       chase_frac = 0.20   # default
                    chase_gap = tp_dist * chase_frac
                    chase_sl = round(max(pos.tp, high_price - chase_gap), 8)
                    if chase_sl > pos.sl:
                        old_sl = pos.sl
                        pos.sl = chase_sl
                        log.info(f"📈 {pos.pair} CHASE — SL ${old_sl:.4f}→${pos.sl:.4f} (high ${high_price:.4f}, gap ${chase_gap:.4f})")
                        if self.tg:
                            try: self.tg.send(f"📈 <b>CHASE</b> {pos.pair}\nPeak ${high_price:.4f}\nSL ${old_sl:.4f} → ${pos.sl:.4f}")
                            except Exception: pass
                        if getattr(self, 'native_sl', None):
                            try:
                                _m = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                                if _m is False and self.tg:
                                    try: self.tg.send(f"⚠️ NATIVE SL CHASE MOVE FAILED {pos.pair}")
                                    except Exception: pass
                            except Exception as _e:
                                log.warning(f"Native SL chase move failed {pos.pair}: {_e}")
                        self.save_state()

            # v14.7: legacy be_target kept for symbol references in lines below (dead but harmless)
            be_target = pos.avg_entry + (pos.tp - pos.avg_entry) * 0.30
            _atr_pct = pos.atr / pos.avg_entry if getattr(pos,'atr',0) > 0 and pos.avg_entry > 0 else 0.010
            _be_trig = max(0.015, min(_atr_pct * 1.2, 0.06))  # ATR-aware: 2.5% min, 4% max
            # v14.5.3 FIX: use max(current_pct, historical_high_pct) for BE
            # Bug: pct used current price only — brief spikes in pos.high were ignored
            # Example: BCH high=$385 (above BE $384.38) but current=$378 → pct=0.8% → BE missed
            _high_pct = (pos.high - pos.avg_entry) / pos.avg_entry if pos.avg_entry > 0 else 0
            _eff_pct = max(pct, _high_pct)  # use highest price ever seen for BE check
            # v15.14: LEGACY BE LOCK DISABLED — superseded by 3-rung real-profit ladder.
            if False:  # disabled v15.14
                pass
                if getattr(self, 'native_sl', None):
                    try:
                        # v13.5.6 FIX: capture move() result. Previously this was
                        # fire-and-forget, so a failed re-attach left the position
                        # naked on the exchange side (software SL still protected,
                        # but the failsafe was silently gone). Alert if False.
                        _moved = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                        if _moved is False and self.tg:
                            try:
                                self.tg.send(f"⚠️ <b>NATIVE SL BE-MOVE FAILED</b> {pos.pair}\n"
                                             f"🛡️ Software SL still active at ${pos.sl:.4f}\n"
                                             f"⚠️ Exchange-side failsafe NOT in place — review")
                            except Exception:
                                pass
                        self.save_state()
                    except Exception as _e: log.warning(f'Native SL BE-move failed: {_e}')
                self.save_state()  # v11.2.20 FIX: persist BE lock — was lost on restart
            # v14.6.5 AUDIT FIX (H-1): gate legacy step-lock behind tp_floor_locked.
            # Once the v14.7 chase ladder takes over (TP hit → chase mode), legacy
            # step-lock must NOT run — its 1% trailing buffer would overwrite the
            # chase SL. Both systems used to write to pos.sl independently.
            if not getattr(pos, 'tp_floor_locked', False):
              if pct >= 0.03 and pos.sl < pos.avg_entry * 1.02 - 0.000001:
                old_sl = pos.sl; pos.sl = round(pos.avg_entry * 1.02, 8)
                log.info(f"\U0001f512 {pos.pair} SL+2% lock ${old_sl:.4f}→${pos.sl:.4f}")
                if self.tg: self.tg.send(f"🔒 <b>SL +2% LOCK</b> {pos.pair}\n📈 SL ${old_sl:.4f} → ${pos.sl:.4f}\n💰 +2% profit secured")
                if getattr(self, 'native_sl', None):
                    try:
                        _moved = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                        if _moved is False and self.tg:
                            try: self.tg.send(f"⚠️ NATIVE SL +2% MOVE FAILED {pos.pair} | SL ${pos.sl:.4f} | Exchange failsafe gone")
                            except Exception: pass
                        self.save_state()
                    except Exception as _e: log.warning(f'Native SL +2pct-move failed: {_e}')
              if pct >= 0.05 and pos.sl < pos.avg_entry * 1.035 - 0.000001:
                old_sl = pos.sl; pos.sl = round(pos.avg_entry * 1.035, 8)
                log.info(f"\U0001f512 {pos.pair} SL+3.5% lock ${old_sl:.4f}→${pos.sl:.4f}")
                if self.tg: self.tg.send(f"🔒 <b>SL +3.5% LOCK</b> {pos.pair}\n📈 SL ${old_sl:.4f} → ${pos.sl:.4f}\n💰 +3.5% profit secured")
                if getattr(self, 'native_sl', None):
                    try:
                        _moved = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                        if _moved is False and self.tg:
                            try: self.tg.send(f"⚠️ NATIVE SL +3.5% MOVE FAILED {pos.pair} | SL ${pos.sl:.4f} | Exchange failsafe gone")
                            except Exception: pass
                        self.save_state()
                    except Exception as _e: log.warning(f'Native SL +3.5pct-move failed: {_e}')
            # v9.4: Progressive profit lock — protect 50% of gains at 75% of TP
              tp_dist = pos.tp - pos.avg_entry
              if tp_dist > 0:
                progress = (price - pos.avg_entry) / tp_dist
                if progress >= 0.75:
                    lock_price = pos.avg_entry + tp_dist * 0.50
                    if pos.sl < lock_price:
                        old_sl = pos.sl
                        pos.sl = lock_price
                        log.info(f"🔐 {pos.pair} PROFIT LOCK 75% → SL=${pos.sl:.4f} (lock 50% gains)")
                        if self.tg: self.tg.send(f"🔐 <b>PROFIT LOCK 75%</b> {pos.pair}\n🛡️ SL = ${pos.sl:.4f}\n💰 50% of gains locked")
                        if getattr(self, 'native_sl', None):
                            try:
                                _moved = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                                if _moved is False and self.tg:
                                    try: self.tg.send(f"⚠️ NATIVE SL 75% LOCK MOVE FAILED {pos.pair} | SL ${pos.sl:.4f} | Exchange failsafe gone")
                                    except Exception: pass
                                self.save_state()
                            except Exception as _e: log.warning(f'Native SL PL75-move failed: {_e}')
                        # v14.6.4 AUDIT FIX: removed dead LEGACY PROFIT LOCKED commented send.
                        self.save_state()  # v11.2.20 FIX: persist profit lock

            # v14.6.2: Group D progressive profit locks
            if pos.group == 'D':
                _d_locks = [
                    (0.20, 0.15, 'D_LOCK_20%'),
                    (0.15, 0.10, 'D_LOCK_15%'),
                    (0.10, 0.05, 'D_LOCK_10%'),
                    (0.05, 0.005, 'D_LOCK_5%'),
                ]
                for _trig, _lock, _label in _d_locks:
                    if pct >= _trig:
                        _new_sl = round(pos.avg_entry * (1 + _lock), 8)
                        if pos.sl < _new_sl:
                            pos.sl = _new_sl
                            log.info(f"🔐 {pos.pair} {_label} → SL=${pos.sl:.6f}")
                            if self.tg:
                                try: self.tg.send(f"🔐 {_label} {pos.pair} | SL locked at ${pos.sl:.6f}")
                                except Exception: pass
                            if getattr(self, 'native_sl', None):
                                try:
                                    _m = await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                                    if _m is False and self.tg:
                                        try: self.tg.send(f"⚠️ {_label} native SL move failed {pos.pair}")
                                        except Exception: pass
                                except Exception as _e:
                                    log.warning(f"Group D SL move failed {pos.pair}: {_e}")
                            self.save_state()
                        break

            if False and price >= pos.tp and not pos.trailing_on:
                pos.trailing_on = True
                td = pos.avg_entry * 0.010  # Strict 1% trailing buffer
                pos.trail_stop = price - td
                
                log.info(f"📈 {pos.pair} TP HIT → Limitless Trail ON @${price:.4f} stop=${pos.trail_stop:.4f}")
                
                # Detach native TP limit order from Binance so it doesn't force a sell
                if getattr(self, "native_tp", None):
                    try: await asyncio.to_thread(self.native_tp.detach, pos)
                    except Exception: pass
                
                try:
                    _msg = f"""📈 <b>TP HIT — LETTING WINNER RUN</b>
🎯 Target Reached: ${price:.4f}
🔒 Ghost Stop Locked: ${pos.trail_stop:.4f}
🚀 1% Trail Activated!"""
                    if hasattr(self, 'tg') and self.tg: getattr(self, 'tg').send(_msg)
                    elif 'tg' in globals(): tg.send(_msg)
                except Exception: pass
                
                if hasattr(self, 'save_state'): self.save_state()

            # --- v15.6 ATR-ADAPTIVE GHOST TRAIL ---
            # Replaces v15.5 fixed 1% step-lock. Industry-standard 2.5x ATR multiplier
            # for 5-15m crypto. Auto-adapts per coin's volatility. Gated behind be_locked
            # (first ladder rung) so the trade has room to breathe before locking starts.
            # Trail computed from PEAK (high water mark), ratchets UP only.
            # v14.6.5 AUDIT FIX (H-1) preserved: only runs when chase NOT active.
            if not getattr(pos, 'tp_floor_locked', False) and getattr(pos, 'be_locked', False):
              _p_pct = (price - pos.avg_entry) / pos.avg_entry
              _opt_sl = pos.sl
              nl = '\n'
              
              # ATR-adaptive trail width (industry standard: ATR × 2.5 for 5-15m crypto)
              _atr_val = getattr(pos, 'atr', 0.0) or 0.0   # defensive: handle None/missing
              _atr_pct = (_atr_val / pos.avg_entry) if (pos.avg_entry > 0 and _atr_val > 0) else 0.012
              _trail_pct = _atr_pct * 2.5
              # Safety clamps: min 2% (no noise stops), max 5% (no extreme give-back)
              _trail_pct = max(0.020, min(0.050, _trail_pct))
              
              # Trail from PEAK (high water mark), not current price
              _peak = max(getattr(pos, 'high', price) or price, price)
              _dynamic_sl = _peak * (1.0 - _trail_pct)
              
              # Ratchet UP only — BE floor (entry+0.2%) is already in pos.sl from ladder fire
              _opt_sl = max(pos.sl, _dynamic_sl)
                
              if _opt_sl > pos.sl:
                pos.sl = _opt_sl
                log.info(f"🔒 {pos.pair} Step-Lock advanced to ${_opt_sl:.4f}")
                try:
                    if hasattr(self, 'native_sl'): await asyncio.to_thread(self.native_sl.move, pos, pos.sl)  # v15.4 FIX (P3)
                except Exception: pass
                _last_alert = getattr(pos, '_last_tg_sl', 0)
                if (_opt_sl - _last_alert) / max(_last_alert, 0.0001) >= 0.005:
                    pos._last_tg_sl = _opt_sl
                    try:
                        _msg = f"🪜 <b>{pos.pair} STEP-LOCK SECURED</b>{nl}Price Climbed: <b>+{_p_pct*100:.1f}%</b>{nl}New Stop-Loss: <b>${pos.sl:.4f}</b>"
                        if hasattr(self, 'tg') and self.tg: getattr(self, 'tg').send(_msg)
                        elif hasattr(self, 'notifier') and self.notifier: getattr(self, 'notifier').send(_msg)
                        elif 'tg' in globals(): tg.send(_msg)
                    except Exception: pass
                self.save_state()  # v15.6 FIX: moved inside if-block — was saving every cycle
                
            # 4. Trail management — v14.7 DISABLED (chase ladder above handles ratcheting)
            
                td = pos.avg_entry * 0.010  # Strict 1% trailing gap
                new_trail = price - td
                if new_trail > pos.trail_stop:
                    pos.trail_stop = new_trail
                    log.info(f"📈 {pos.pair} trail moved → ${pos.trail_stop:.4f}")
                    self.save_state()  # v11.2.20 FIX: persist trail stop advance
                if price <= pos.trail_stop:
                    # v14.4: Cancel native TP — bot is closing via trail (price went higher than TP)
                    if getattr(self, "native_tp", None):
                        try: await asyncio.to_thread(self.native_tp.detach, pos)
                        except Exception: pass
                    to_close.append((pos, price, "TRAIL")); continue

            # 5. Regime shift exit — v9.1: exit ANY profitable position in TREND_DOWN
            if ctx.regime == "TREND_DOWN" and pct > 0.020:
                to_close.append((pos,price,"REGIME")); continue

            # 6. Time exit — but only if in loss. If in profit, let it ride
            # v9.1: Near-TP exception + safe datetime parsing
            try:
                entry=datetime.fromisoformat(pos.entry_time)
            except Exception:
                continue  # Skip time check if entry_time is malformed
            mins=(datetime.now(timezone.utc)-entry).total_seconds()/60
            # v9.1: Don't time-exit if price is within 30% of TP distance
            tp_dist = pos.tp - pos.avg_entry
            near_tp = tp_dist > 0 and (pos.tp - price) < tp_dist * 0.30
            # v14.7: never time-exit a position in chase mode (TP already hit, let it run)
            if mins>self.cfg.MAX_HOLD_MIN and not near_tp and not getattr(pos, 'tp_floor_locked', False):
                if pct <= 0:
                    to_close.append((pos,price,"TIME"))  # In loss — cut it
                elif pct > 0 and mins > self.cfg.MAX_HOLD_MIN * 2:
                    to_close.append((pos,price,"TIME_MAX"))  # In profit but 2x max time — take it

        return to_close

    def _record_close(self, pos, price, reason, ctx, tg):
        """v8.4: Record PnL ONLY after sell is confirmed on Binance.
        Uses pos.qty which already reflects any scale-out reductions (FIX #3).
        v9.6 FIX: pro-rate entry_fee by remaining qty.
        B2-7: for synthesized closes (sell never confirmed), use avg_entry as
        exit price so realized PnL = -fees only. Prevents bot's running PnL
        from drifting against real wallet equity after sell-failures. Caller
        is still responsible for logging stuck coins separately.
        v13.5.3 audit Bug #42: synthesized closes were also charging a phantom
        EXIT fee (ef = price * qty * TAKER_FEE). But if the sell never confirmed,
        Binance never took an exit fee — only the entry fee was real. Net loss
        was 2× actual cost. Fix: skip ef on synthesized closes.

        v14.1 FIX (ISSUE B): preserve the actual market price BEFORE the
        synthesized-close overwrite, and pass it through to the journal as
        an additional `market_price` field. The journal `price` field still
        reflects avg_entry on synthesized closes (preserving the reconciliation
        property), but operators reading the journal can now distinguish
        "real exit at $X" from "synthesized close, actual market was at $Y"."""
        try:
            SYNTHESIZED_REASONS = {"CRASH_STUCK", "FORCE_CLOSE", "DUST", "GHOST"}  # RESTORED DUST/GHOST (Phantom Fee Fix) — no fake PnL
            is_synthesized = reason in SYNTHESIZED_REASONS
            # v14.1 ISSUE B: capture the real market price before any overwrite.
            actual_market_price = price
            if is_synthesized:
                price = pos.avg_entry
                log.info(f"📒 B2-7: synthesized close for {pos.pair} ({reason}) "
                         f"booked at avg_entry ${pos.avg_entry:.4f} "
                         f"(real market ${actual_market_price:.6f}, no real sale)")
            pnl=(price-pos.avg_entry)*pos.qty
            # v13.5.3 Bug #42: ef=0 on synthesized closes (no Binance fee was charged
            # because no real sell happened). Realized PnL on synthesized = -entry_fee
            # only, matching real wallet equity. Was: -entry_fee - phantom_exit_fee
            # (~2× the real cost), drifting bot's running PnL below wallet over time.
            ef = 0.0 if is_synthesized else (price * pos.qty * self.cfg.TAKER_FEE)
            # v9.6 FIX: only charge the unsold portion's share of entry fee
            remaining_pct = pos.qty / pos.total_qty if pos.total_qty > 0 else 1.0
            prorated_entry_fee = pos.entry_fee * remaining_pct
            tf = prorated_entry_fee + ef
            net = pnl - tf
            self.pnl+=net; self.daily_pnl+=net; self.fees+=ef
            self.last_close[pos.pair]=datetime.now(timezone.utc)
            # v9.3: Track last result per pair for expert cooldown
            # v11.2.8 FIX (May 4, 2026): TIME_MAX fires only when pct > 0 (slow profitable
            # exit at 2× MAX_HOLD_MIN). Was: classified as TIMEOUT → 24h cooldown for that
            # pair → 2 profitable TIME_MAX exits per day = pair blocked for 24h. Penalized
            # slow winners. Now: TIME_MAX with positive net is treated as WIN (30min cd),
            # only TIME (in-loss timeout) is TIMEOUT.
            if reason == "TP" or reason == "TRAIL" or reason == "REGIME":
                self.last_result[pos.pair] = "WIN"
            elif reason == "TIME_MAX" and net > 0:
                self.last_result[pos.pair] = "WIN"  # slow but profitable — don't penalize
            elif reason == "TIME" or reason == "TIME_MAX":
                self.last_result[pos.pair] = "TIMEOUT"
                # v11.2.19 FIX: TIME exits were counting as pair losses → 2 flat timeouts = 24h block
                # A consolidating coin is NOT a bad coin. Only SL hits count toward pair_losses_today.
            else:
                self.last_result[pos.pair] = "LOSS"
                self.pair_losses_today[pos.pair] = self.pair_losses_today.get(pos.pair, 0) + 1
            pct=(price-pos.avg_entry)/pos.avg_entry*100 if pos.avg_entry>0 else 0
        
            # v7: Record for Kelly
            self.kelly.record(net > 0, pct)
            # v7.2: Analytics — FIX #7
            if hasattr(self, 'analytics') and self.analytics is not None:
                self.analytics.record(pct)
        
            if net>0: self.wins+=1; self.closs=0
            else:
                self.losses+=1
                # v13.5.7 FIX #3 (May 21, 2026): DUST, GHOST, CRASH_STUCK are state-desync
                # artifacts, not real trading losses. PENDLE DUST (-$0.0121) and DOGE GHOST
                # (-$0.0030) on May 14-15 incremented closs to 2 and triggered the 0.70×
                # multiplier in can_trade(), pulling sizes below MIN_NOTIONAL. Now: only
                # real exit reasons count toward closs. Wins still reset closs (handled above).
                _DESYNC_REASONS = ("DUST", "GHOST", "CRASH_STUCK")
                if reason not in _DESYNC_REASONS:
                    self.closs+=1
                    if self.closs>=self.cfg.MAX_CONSEC_LOSSES:
                        self.pause_until=datetime.now(timezone.utc)+timedelta(minutes=self.cfg.LOSS_PAUSE_MIN)
                        log.warning(f"⛔ {self.closs} losses → pause {self.cfg.LOSS_PAUSE_MIN}min")
                else:
                    log.info(f"📉 {pos.pair} {reason} (-${abs(net):.4f}) — desync artifact, closs not incremented")
        
            ic="✅" if net>0 else "❌"
            so=f" DCA:{pos.safety_used}" if pos.safety_used>0 else ""
            sc=f" Sc:{len(pos.scale_done)}" if pos.scale_done else ""
            py=f" Pyr:{pos.pyramids}" if pos.pyramids and pos.pyramids > 0 else ""
            log.info(f"{ic} {pos.pair} {pos.strategy} | ${net:+.4f} ({pct:+.1f}%) WR:{self.wr*100:.0f}%{so}{sc}{py} | {reason} [{ctx.regime}]")
            self._log_trade(reason,pos,price,pos.qty,pos.size,tf,net,
                            synthesized=is_synthesized,
                            market_price=actual_market_price)  # v14.1 ISSUE B
            # v15.0: TCA exit capture — records full lifecycle including R-multiple, MAE/MFE
            if _TCA is not None:
                try: _TCA.record_exit(pos, exit_price=price, reason=reason, pnl=net,
                                      high_seen=getattr(pos, "high", price))
                except Exception: pass
            # v15.2 #2 FIX: audit log exit decision
            _audit = getattr(getattr(self, '_bot_ref', None), '_audit', None)
            if _audit is not None:
                try: _audit.log("EXIT", pair=pos.pair, reason=reason,
                                entry=pos.avg_entry, exit=price, pnl_usd=round(net, 4),
                                synthesized=is_synthesized,
                                tp_floor=getattr(pos, "tp_floor_locked", False))
                except Exception: pass
            # v11.2.8 FIX (May 4, 2026): guard fromisoformat against malformed entry_time.
            # check_exits() at line ~413 already wraps fromisoformat in try/except, but
            # _record_close didn't — a corrupted state file would crash mid-close: sell
            # already executed on Binance, but PnL not recorded, position not removed.
            try:
                hold_min = (datetime.now(timezone.utc) - datetime.fromisoformat(pos.entry_time)).total_seconds() / 60
            except Exception:
                hold_min = 0
            bal = self.cfg.TOTAL_CAPITAL + self.pnl
            # v15.4 TG UPGRADE: render chart for SELL alert (entry/SL/TP + actual exit overlay).
            # Fetches own candles via exchange.klines() — 1 extra REST call per SELL (~50ms).
            _chart_png = None
            if getattr(self.cfg, 'TG_CHARTS_ENABLED', True) and getattr(self.cfg, 'TG_ENABLED', False):
                try:
                    from telegram_charts import render_trade_chart
                    _exch = getattr(self, '_bot_ref', None)
                    _exch = _exch.ex if _exch else None
                    if _exch:
                        _candles = _exch.klines_sync(pos.pair, "5m", 60)  # v15.3 FIX: sync helper
                        if _candles and len(_candles) >= 5:
                            _chart_png = render_trade_chart(_candles, entry=pos.avg_entry, sl=pos.sl, tp=pos.tp,
                                                             pair=pos.pair, strategy=pos.strategy,
                                                             action=reason, exit_price=price)
                except Exception as _ce:
                    log.debug(f"SELL chart render skipped: {_ce}")
            # v14.6.4 AUDIT FIX (H4): guard against tg=None (DUST closes pass None
            # for tg). Previously this raised AttributeError, caught by outer
            # try/except, and the alert was silently lost (acceptable for DUST,
            # but unexplicit). Now: explicit `if tg:` so the intent is visible.
            if tg:
                tg.trade_alert(reason, pos.pair, price, pos.strategy, pnl=net,
                               qty=pos.qty, size=pos.size, entry=pos.avg_entry,
                               entry_fee=pos.entry_fee, exit_fee=ef, hold_min=hold_min,
                               balance=bal, dca=pos.safety_used, reason=reason,
                               chart_bytes=_chart_png)
        except Exception as _rc_gap_err:
            log.warning(f"_record_close JOURNAL_GAP_FALLBACK {pos.pair} ({reason}): {_rc_gap_err}")
            try:
                from journal_utils import append_jsonl
                _px = float(price) if price else getattr(pos, 'avg_entry', 0)
                _entry = getattr(pos, 'avg_entry', 0)
                append_jsonl("trades_v9.jsonl", {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "pair": pos.pair,
                    "action": reason,
                    "entry": _entry,
                    "exit": _px,
                    "qty": getattr(pos, "qty", 0),
                    "pnl": round((_px - _entry) * getattr(pos, "qty", 0), 6),
                    "strategy": getattr(pos, "strategy", "?"),
                    "grade": getattr(pos, "grade", "?"),
                    "synthesized": True,
                    "gap_fix": True,
                    "gap_reason": str(_rc_gap_err)[:120],
                })
            except Exception: pass


    def _log_trade(self, action, pos, price, qty, size, fee, net=0,
                   synthesized=False, market_price=None):
        # B2-5: include pnl_pct so walk_forward.py can compute meaningful Sharpe.
        # Before B2-5, walk_forward fell back to dollar `pnl` and got garbage.
        pnl_pct = ((price - pos.avg_entry) / pos.avg_entry * 100
                   if pos.avg_entry > 0 else 0.0)
        entry={"ts":datetime.now(timezone.utc).isoformat(),"action":action,
               "pair":pos.pair,"price":price,"qty":qty,"size":size,"fee":fee,
               "pnl":net,"pnl_pct":round(pnl_pct,4),"strategy":pos.strategy,
               "grade":pos.grade,"rr":pos.rr,"group":getattr(pos,"group","A")}
        # v14.1 FIX (ISSUE B): for synthesized closes (DUST/FORCE_CLOSE/GHOST/
        # CRASH_STUCK), the `price` field above is avg_entry (preserves the
        # bot-PnL ↔ wallet-equity reconciliation property). The two new fields
        # below let operators see the REAL market price at synthesis time.
        # Backward-compatible: existing readers ignore unknown JSON keys.
        if synthesized:
            entry["synthesized"] = True
            if market_price is not None:
                entry["market_price"] = market_price
        self.trades.append(entry)
        # v10.4 FIX: append to live TradeJournal so strategy_weight() sees fresh data
        # without restart. Bot.__init__ links self.risk.journal = self.journal.
        if hasattr(self, 'journal') and self.journal is not None:
            try: self.journal.history.append(entry)
            except Exception: pass
        # v9.0 FIX: Append-only JSONL — no corruption risk, no full file rewrite
        # v13.5.2 audit Fix #9: route through append_jsonl for size-bounded rotation.
        # Threshold raised to 50MB (vs default 5MB) because upgrade_engine.check_clean_days()
        # walks the FULL file to compute consecutive profitable days for tier gating —
        # rotating at 5MB would silently break the deposit gate after ~80 months at
        # current ~10 trades/day. 50MB ≈ 80 years of history, safer ceiling.
        try:
            from journal_utils import append_jsonl
            append_jsonl(self.cfg.LOG_FILE, entry, max_bytes=50*1024*1024)
        except Exception as e:
            # Fall back to raw append if helper import fails — never lose a trade record.
            log.warning(f"Trade log via journal_utils failed ({e}); falling back to raw append")
            try:
                with open(self.cfg.LOG_FILE, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e2:
                log.warning(f"Trade log write failed: {e2}")



    def status(self):
        self._reset(); p,r=self.paused()
        return {"cap":self.cfg.TOTAL_CAPITAL,"pnl":round(self.pnl,4),
                "pnl_pct":round(self.pnl/self.cfg.TOTAL_CAPITAL*100,2) if self.cfg.TOTAL_CAPITAL>0 else 0,
                "daily":round(self.daily_pnl,4),"dt":self.daily_t,
                "fees":round(self.fees,4),"pos":len(self.positions),
                "avail":round(self.available,2),"tt":len(self.trades),
                "wr":round(self.wr*100,1),"cl":self.closs,"paused":p,"pr":r,
                "heat":round(self.portfolio_heat*100,1)}

    # v13.5.7 FIX #5 (May 21, 2026): journal-gap defense.
    # On May 14 evening, SUI + DODO positions closed without writing to
    # trades_v9.jsonl. Root cause: position.remove() was called from one of
    # ~10 sites in bot.py without going through _record_close → _log_trade.
    # This wrapper logs a CRITICAL warning whenever a position is removed
    # without a journal entry in the last 10 seconds. Doesn't auto-fix the
    # underlying call sites — those need per-site refactor — but at least
    # we get a loud warning when it happens instead of silent data loss.
    def remove_position_safe(self, pos, expected_reason=None):
        """Wrapper around self.positions.remove(pos) that detects journal gaps.
        Use this instead of `self.positions.remove(pos)` everywhere that close
        path runs. Pass expected_reason ('TP','SL','TIME','GHOST', etc.) for
        better diagnostics. NOT YET WIRED at all call sites — call sites should
        be migrated one at a time after testing."""
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            pair = getattr(pos, 'pair', '?')
            recent_window = now - timedelta(seconds=10)
            recent_entries = [
                t for t in self.trades[-20:]
                if t.get('pair') == pair
                and t.get('action') in ('TP','SL','SL_NATIVE','TIME','TIME_MAX',
                                        'GHOST','CRASH','DUST','FORCE_CLOSE',
                                        'TRAIL','REGIME','CLOSE','CRASH_STUCK')
            ]
            # Best-effort timestamp parse
            had_recent = False
            for entry in recent_entries:
                try:
                    ts = datetime.fromisoformat(entry.get('ts','').replace('Z','+00:00'))
                    if ts >= recent_window:
                        had_recent = True; break
                except Exception: pass
            if not had_recent:
                log.error(f"🚨 JOURNAL GAP DETECTED: {pair} removed from positions "
                          f"without journal entry in last 10s "
                          f"(expected_reason={expected_reason}). "
                          f"This is a v13.5.7 FIX #5 diagnostic — investigate call site.")
        except Exception as _e:
            log.debug(f"remove_position_safe diagnostic suppressed: {_e}")
        try:
            self.positions.remove(pos)
        except ValueError:
            log.warning(f"remove_position_safe: {getattr(pos,'pair','?')} was not in positions list")


# v14.5.1 FIX (audit #2): DELETED _enforce_dynamic_tiers() — dead code that set
# RISK_PCT=1.0 (100% capital per trade). Never called, but a catastrophic landmine
# if invoked accidentally. Original location: lines 872-884.
# v14.5.1 FIX (audit #24): bare `except:` inside it also swallowed KeyboardInterrupt.
# v14.6.4 AUDIT FIX: removed dead module-level `USE_KELLY = True` — was never imported anywhere.


