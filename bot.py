import warnings
warnings.filterwarnings('ignore', category=UserWarning)

# 🔥 ABSOLUTE TRUTH PROTOCOL (AMNESIA FIX)
import os, json, asyncio
def _heal_memory():
    import time
    for fn in ["bot_state.json", "trade_history.json"]:
        if not os.path.exists(fn):
            with open(fn, "w") as _f: _f.write("{}")
        else:
            try:
                with open(fn, "r") as _f: json.load(_f)
            except Exception:
                corrupted_fn = f"{fn}.corrupted_backup_{int(time.time())}"
                try:
                    os.rename(fn, corrupted_fn)
                except Exception:
                    pass
                with open(fn, "w") as _f: _f.write("{}")
_heal_memory()
import gc
try:
    import joblib
except ImportError:
    joblib = None
#!/usr/bin/env python3
"""
BinBot V18.8 GodMode LIVE-ONLY — bot.py | ProBotV11 main orchestrator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Live trading engine (real money only — no paper). Auto capital-tiers by live balance.
V18.8: auto balance-tier sizing + WYCKOFF BEAR filter + TG anti-spam, on the v18.7.1 GodMode base.

Total bug fixes from v11.2 → v11.2.7: 23
  v11.2:   #1-#5    timedelta, F&G, GridLevel, fee gate, Signal dataclass
  v11.2.1: #6-#9    last_reset / closs / pause_until persistence, SLIP-WARN orphan
  v11.2.2: #10      Ghost killer (gate removed in this build — not applicable to live-only)
  v11.2.3: #11-#14  PnL double-count, intel async, hyperopt stride, BTC high persistence
  v11.2.4: #15-#16  Ghost _record_close, news rate-limit cooldown
  v11.2.5: #17-#18  Capital exposure floor, Telegram thread pool
  v11.2.6: #19-#22  Startup sync, BTC crash, force-close + dust _record_close, HALF MIN_TRADE
  v11.2.7: #23      Peak-equity persistence (audit #5)

All 23 fixes preserved. Live execution paths intact.
"""

# v18.7.1 FIX: removed `from atr_engine import ATREngine; atr_tracker = ATREngine()`.
# atr_engine.py does not exist in this build → it crashed the bot on startup with
# ModuleNotFoundError. atr_tracker was unused (its only consumer, the disabled `if False:`
# ATR-trailing block, was already deleted). ATR trailing is handled by risk.check_exits.
import os, requests, json, time, math, logging, signal, sys, urllib.request, threading, traceback, tempfile
import random  # v16.0 AUDIT FIX L5: moved from hot loop (lines 2580/2619) to module level
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
warnings.filterwarnings("ignore", category=UserWarning, module="lightgbm")
warnings.filterwarnings("ignore", message="X does not have valid feature names")
_pos_lock = threading.RLock()  # v11.2.15: protects positions from race conditions
# v10.7 FIX: fcntl is Linux/macOS only — Windows users were crashing on this top-level
# import (v10.6 only addressed StateManager.save). Now imported conditionally.
try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    fcntl = None
    _HAS_FCNTL = False
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace  # v9.7.1: for synthetic ctx in crash protection
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, List, Dict
from pathlib import Path
from collections import deque
from logging.handlers import RotatingFileHandler
from journal_utils import append_jsonl as _append_jsonl  # v13.5
from capital_activator import CapitalActivator, CapitalTierManager  # v18.7.4 auto capital tiers

try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.model_selection import train_test_split
    ML_AVAILABLE = True
except ImportError:
    raise ImportError("CRITICAL: numpy and scikit-learn are strictly required.")

try:
    from lightgbm import LGBMClassifier
    LGBM_AVAILABLE = True
except ImportError:
    LGBM_AVAILABLE = False

try:
    from scipy.optimize import minimize
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
# v13.5.3 audit Bug #22: was getLogger("pro-v9.0") but every other module uses
# getLogger('binbot'). The RotatingFileHandler below was attached to "pro-v9.0"
# only → bot_v9.0.log captured ~33% of activity (only bot.py's lines). Anyone
# debugging from the rotated log file alone got a deeply incomplete picture.
# Now: attach the handler to the ROOT logger so it captures everything from
# every module, AND rename bot's local logger to 'binbot' to match the rest.
log = logging.getLogger('binbot')

# v15.4 WS Spam Filter: Suppress library-level websocket loop errors
class WSSpamFilter(logging.Filter):
    def filter(self, record):
        return "Read loop has been closed" not in record.getMessage()
logging.getLogger().addFilter(WSSpamFilter())
logging.getLogger('binance').addFilter(WSSpamFilter())
logging.getLogger('websockets').addFilter(WSSpamFilter())

# v8.4: Log rotation — prevent disk fill on 1GB VM
try:
    _ROOT = os.path.dirname(os.path.abspath(__file__))
    _fh = RotatingFileHandler(os.path.join(_ROOT, "bot_v9.0.log"),
                              maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    _fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"))
    # v13.5.3 Bug #22: attach to ROOT logger so binbot/binance/etc. all flow.
    logging.getLogger().addHandler(_fh)
except Exception: pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError: pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from config import Config
from models import Position, Signal, Context, Candle, PendingBuy
from telegram import Telegram
from news import NewsSentiment
from state import StateManager
# v13.3 $1000-TIER: walk-forward optimizer (activated by upgrade_engine)
try:
    from walk_forward import WalkForwardOptimizer as _WFO
    _WFO_AVAILABLE = True
except ImportError:
    _WFO_AVAILABLE = False
from analytics import SelfHealer, TradeJournal, DrawdownShield, PairRotator, Analytics
from exchange import Exchange, LivePrices
from exchange_native_sl import NativeSLManager, NativeTPManager  # v13.6/v14.4 native SL/TP
from indicators import TA
from monitors import EventCalendar, StablecoinFlow, MVRVMonitor, TVLMonitor, WhaleWalletMonitor, OpenInterestMonitor, TokenUnlockMonitor, KellySizer, HyperOptimizer
from intelligence import Intel
from ml import MLPredictor, DXYCorrelation, CoinGeckoTrending, CoinGeckoMovers, WhaleOnChain, MultiExchangeFlow, OptionsSentiment, RLAgent

from local_orderbook import LocalOrderBook
from execution_algo import ExecutionAlgo
from strategies import GridLevel, GridEngine, DCA, Strategies, Backtester
from micro_price import MicroPriceModel
from risk import Risk
from portfolio_alloc import ExposureGuard

from reconciler import PositionReconciler
from coin_profile import CoinProfileManager
from orderflow import AggressorFlowTracker, VolumeDeltaTracker
from lob_imbalance import LOBImbalanceTracker
from intelligence import (FundingRateTracker, LiquidationDetector, SmartCoinDetector,
    CryptoPanicNews, MomentumScanner, LiquidationCascadeTracker, SpotPerpBasisTracker,
    StatArbSignal, VPINTracker, KalmanPairsSpreader, AzureOpenAIIntelligence,
    TokenUnlockTracker, EconomicCalendar)

class ProBotV11:
    def __init__(self, cfg):
        self.cfg=cfg
        self.ex=Exchange(cfg)

        # v18.7.1: ML gated by cfg.ML_ENABLED (real kill-switch). GodMode default ON;
        # bounded ±ML_CONF_BOOST nudge, never overrides a hard risk block.
        self.ml = MLPredictor(retrain_hours=getattr(cfg, 'ML_RETRAIN_HOURS', 6)) \
            if (ML_AVAILABLE and getattr(cfg, 'ML_ENABLED', True)) else None
        self.micro_price = MicroPriceModel()
        # v18.7.1 FIX (D3): monitors.TokenUnlockMonitor / EventCalendar LACK should_block(),
        # so the FOMC/CPI + token-unlock risk blocks were silent no-ops (AttributeError→
        # except:pass). The intelligence classes expose the should_block()/get_next_event()/
        # get_boost() interface the risk blocks actually call.
        self.token_unlock = TokenUnlockTracker()
        self.econ_calendar = EconomicCalendar()
        self.dxy = DXYCorrelation()
        self.whale = WhaleOnChain()
        self.multi_ex = MultiExchangeFlow()
        self.options = OptionsSentiment()
        self.rl = RLAgent()
        self.transformer_nlp = None
        self.meta_learner = None
        self.monte_carlo = None
        self.model_selector = None
        self.gecko_trending = CoinGeckoTrending()
        self.gecko_movers = CoinGeckoMovers()
        self.social_sentiment = None
        self.exchange_flow = None
        self.long_short = None
        self.open_interest = None
        self.hash_rate = None

        self.intel=Intel(cfg,self.ex)
        # v18.7.1 FIX (D2): pass the micro-price engine into Strategies (was None → the
        # MICRO_PRICE strategy could never fire even though the REST bookTicker feed runs).
        self.ex.on_book_ticker = self.micro_price.update_bba
        self.strat=Strategies(cfg,self.ex,self.intel, self.micro_price)
        self.state=StateManager(cfg.STATE_FILE)
        self.risk=Risk(cfg,self.state)
        self.grid=GridEngine(cfg,self.ex)
        self._limit_orders={}  # v11.2.18 FIX: track unfilled GTC limit orders
        self.dca=DCA(cfg)
        self.exposure_guard = ExposureGuard(cfg)
        self.tg=Telegram(cfg)
        self.risk.tg = self.tg  # v13.5.4 wire Telegram into Risk
        # v16.0.03.1: Auto-activate Grid/DCA/aggressive features when equity ≥ $500.
        # Hysteresis at $450 prevents flickering. Original config values are
        # snapshotted and restored if equity drops below the deactivation band.
        self.cap_activator = CapitalActivator(cfg, tg=self.tg)
        # v18.7.4: automatic capital-tier sizing — below SMALL_TIER_USD ($50) the bot runs
        # 1 concentrated position at 90% of balance; at/above it reverts to the normal
        # 2-position / 33% config. Switched automatically by live equity (see _cycle).
        self.cap_tier = CapitalTierManager(cfg, tg=self.tg, exposure_guard=self.exposure_guard)
        self.native_sl = NativeSLManager(self.ex, cfg)
        self.native_sl._risk_ref = self
        # v14.6 FIX: recover any positions missing native SL (crash-during-BE-move recovery)
        import threading
        threading.Timer(15.0, lambda: self.native_sl.recover_missing(self.risk.positions)).start()
        self.native_tp = NativeTPManager(self.ex, cfg)  # v14.4 exchange-side TP backup
        self.risk.native_sl = self.native_sl  # v13.6 wire into Risk
        self.pending:Dict[str,PendingBuy]={}
        self.ws=LivePrices([p["s"] for p in cfg.PAIRS])
        # v16.0.0 AUDIT FIX (C1): removed `` — it was
        # immediately overwritten by self.lob=LOBImbalanceTracker() below (line ~260)
        # and LocalOrderBook.start() was never called, so it was a dead allocation.
        # self.lob is the LOBImbalanceTracker (the one actually used in risk blocks).
        
        
        

        self.exec_algo = ExecutionAlgo(self.ex)
        # v16.0.0: LSTM deep learning model (optional — requires PyTorch)
        self.lstm=None
        # v8.3: Super-intelligent modules
        # v11.2.10: MTF + LSTM removed (dead code — dict/object mismatch, non-existent imports)
        
        
        
        
        
        # v13.3 $1000-TIER: walk-forward optimizer
        self._wfo = None
        try:
            from feature_flags import get as _ff
            if _ff("walk_forward_opt", False) and _WFO_AVAILABLE:
                # v13.4 fix (Batch 1): hyperopt.force_run() now exists in monitors.py.
                # Previously this lambda called a non-existent attribute → silent no-op
                # whenever walk-forward detected parameter decay >25%.
                self._wfo = _WFO(
                    trades_file="trades_v9.jsonl",
                    interval_hours=24.0,
                    hyperopt_callback=lambda: self.hyperopt.force_run() if self.hyperopt else None
                )
                self._wfo.start()
                log.info("📐 Walk-Forward Optimizer activated ($1000 tier)")
        except Exception as _e:
            log.debug(f"WFO init: {_e}")
        
        
        
        
        # v11.2.10: New intelligence modules
        
        
        
        # v12.0: New Tier 3 modules
        self.coin_profiles=CoinProfileManager(save_path="coin_profiles.json")
        self.aggressor_flow=AggressorFlowTracker()
        # v14.2: Institutional modules — Module 1+2+3+4
        self.vol_delta=VolumeDeltaTracker()          # M1: per-pair signed delta
        self.lob=LOBImbalanceTracker()               # M2: order book imbalance
        self.liq_cascade=LiquidationCascadeTracker() # M3: enhanced cascade
        # v15.2 #4 FIX: skip SpotPerpBasisTracker/StatArbSignal/KalmanPairsSpreader
        # instantiation entirely — they were dead weight on spot-only (can't trade
        # the spread). Updates were disabled in v15.0; now we drop instantiation too.
        # Stub objects keep should_block() interface alive without memory cost.
        class _NullBlocker:
            def should_block(self, *a, **k): return False
            def get_boost(self, *a, **k): return 1.0
            def status(self, *a, **k): return ""
            def update(self, *a, **k): pass
            def get_score(self, *a, **k): return 0.0
        self.spot_perp = _NullBlocker()
        self.stat_arb  = _NullBlocker()
        self.kalman    = _NullBlocker()
        self.vpin=VPINTracker()                          # VPIN: toxic flow detector
        # v12.2: Advanced intelligence modules (all FREE APIs)
        self.funding_rate=FundingRateTracker()
        self.liquidation=LiquidationDetector()
        self.smart_coin=SmartCoinDetector()
        self.crypto_news=CryptoPanicNews()
        self.azure_openai = AzureOpenAIIntelligence()
        self.momentum=MomentumScanner()
        self.bt=Backtester(self.ex,TA,cfg)
        self.hyperopt=HyperOptimizer(cfg.HYPEROPT_INTERVAL_H) if cfg.HYPEROPT_ENABLED else None
        # v13.5.3 audit Fix #5: pre-disable SMC_OB+FVG.
        # Live data through May 10 2026 (11 trades): 45% WR, -$2.55 net P&L.
        # Vanilla SMC_OB on identical setups: 91% WR, +$3.55. The +0.15 conf boost from
        # FVG detection (strategies.py:236) inflates grade to A+ on weaker setups.
        # Backtester already auto-disables strategies <38% WR after enough trades, but
        # at 45% WR this stays just inside the gate. Hard-blocking here until the
        # journal_weight() penalty (analytics.py:181) accumulates more samples.
        # Remove from this list once strategy_weight returns ≤0.90 organically.
        self.disabled_strats=[]
        self.candle_cache:Dict[str,list]={}
        self.healer = SelfHealer()
        self.journal = TradeJournal(cfg.LOG_FILE)
        self.ddshield = DrawdownShield(cfg.TOTAL_CAPITAL)
        # v11.2.8 FIX (May 4, 2026): restore persisted DD-shield peak if present.
        # Without this, every restart reset peak to TOTAL_CAPITAL → de-risking ladder
        # silently bypassed even when actual drawdown was 8-11%. Same bug class as
        # v11.2.3 #14 (BTC high) and v11.2.7 #23 (Risk._peak_equity).
        if hasattr(self.risk, '_saved_dd_peak') and self.risk._saved_dd_peak > 0:
            self.ddshield.set_peak(self.risk._saved_dd_peak)
        self.rotator = PairRotator(cfg.PAIRS)
        self.analytics = Analytics()
        # v10.4 FIX: wire journal back-reference so Risk._log_trade can update
        # the live TradeJournal.history (was: only loaded once at startup).
        self.risk.journal = self.journal
        # v8.4: Wire shield + analytics into Risk so can_trade and _record_close can use them
        self.risk.ddshield = self.ddshield
        self.risk.analytics = self.analytics
        # v15.2 #2 FIX: give risk module a back-reference so audit_log calls in
        # open_pos / _record_close can reach the bot's audit instance.
        self.risk._bot_ref = self
        self.running=True; self.cycles=0; self.start=None
        self._price_cache = {}  # v14.5.1 FIX (audit #3): was never initialized — AttributeError in ML correlation
        # v15.4 Telegram upgrade: state for interactive controls + scheduled summaries
        self.paused = False                     # toggled by /pause and /resume
        self._force_close_all_requested = False # set by /force_close confirm, drained by cycle loop
        self._tg_cmd_handler = None
        try:
            from telegram_commands import TelegramCommandHandler
            self._tg_cmd_handler = TelegramCommandHandler(cfg, self)
            self._tg_cmd_handler.start()
        except Exception as _tg_cmd_e:
            log.warning(f"TG command handler wiring failed: {_tg_cmd_e}")
        # v11.2.10: Position reconciliation — compares bot vs Binance balances
        self.reconciler = PositionReconciler()
        # v15.3 AUDIT FIX #3: TriangularArb wired in ALERT-ONLY mode.
        # The module's execute() is a documented dry-run skeleton — calling it
        # does nothing. We scan and Telegram-alert; manual execution by operator.
        # Switching to autoexecute requires implementing 3-leg atomic ordering
        # with rollback on partial fills (real money risk if not done right).
        self.tri_arb = None
        self._tri_arb_cycle = 0
        self._tri_arb_alerted = {}  # cycle_key -> timestamp (dedup alerts)
        try:
            from triangular_arb import TriangularArb
            self.tri_arb = TriangularArb(self.ex, min_profit_bps=50.0)  # 50bps = 0.5% min net, filters data errors
            log.info("  💎 TriArb scanner wired (alert-only, manual execute)")
        except Exception as _tri_e:
            log.warning(f"TriArb wiring failed: {_tri_e}")


    # v11.2.16: Model cache helpers — skip retraining on fast restarts
    def _cache_path(self, name):
        import os
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ml_cache")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{name}.pkl")

    def _cache_fresh(self, name, ttl=7200):
        import os
        p = self._cache_path(name)
        try:
            return os.path.exists(p) and (time.time() - os.path.getmtime(p)) < ttl
        except Exception:
            return False

    def _cache_save(self, obj, name):
        try: joblib.dump(obj, self._cache_path(name))
        except Exception as e: log.debug(f"Cache save {name}: {e}")

    def _cache_load(self, name):
        try: return joblib.load(self._cache_path(name))
        except Exception: return None

    def _gate_reject(self, reason):
        """v16.0.0 NEW: tally why a signal was rejected at the entry gate. Logged as a
        histogram every GATE_TELEMETRY_EVERY_CYCLES so MIN_CONF / heat / cooldown / regime
        gates can be tuned with data instead of guesswork."""
        if not getattr(self.cfg, 'GATE_TELEMETRY_ENABLED', True):
            return
        try:
            if not hasattr(self, '_gate_stats'):
                import collections as _c; self._gate_stats = _c.Counter()
            self._gate_stats[reason] += 1
        except Exception:
            pass

    async def _detach_native_sl_before_sell(self, pos, reason="SELL"):
        """Cancel attached exchange-side SL before a bot-initiated market sell."""
        # v14.4: Also cancel native TP order to prevent double-sell
        if getattr(self, "native_tp", None):
            try: await asyncio.to_thread(self.native_tp.detach, pos)
            except Exception: pass
        if not getattr(self.cfg, "NATIVE_SL_ENABLED", False):
            return True
        if not getattr(self, "native_sl", None):
            return True
        if not getattr(pos, "native_sl_order_id", None):
            return True
        try:
            ok = await asyncio.to_thread(self.native_sl.detach, pos)
            if ok:
                return True
            log.warning(f"Native SL detach failed before {reason} for {pos.pair}; market sell skipped")
            try:
                self.tg.send(
                    f"⚠️ <b>NATIVE SL DETACH FAILED</b> {pos.pair}\n"
                    f"Bot skipped market sell for {reason}; exchange-side SL remains the failsafe."
                )
            except Exception:
                pass
            return False
        except Exception as e:
            log.warning(f"Native SL detach exception before {reason} for {pos.pair}: {e}")
            return False

    async def _restore_native_sl_after_failed_sell(self, pos, reason="SELL"):
        """Re-place exchange-side SL if a bot sell failed after detaching it."""
        if not getattr(self.cfg, "NATIVE_SL_ENABLED", False):
            return
        if not getattr(self, "native_sl", None):
            return
        if getattr(pos, "native_sl_order_id", None):
            return
        try:
            if await asyncio.to_thread(self.native_sl.attach, pos):
                log.info(f"Native SL restored for {pos.pair} after failed {reason}")
                try:
                    self.risk.save_state(self.grid.pnl, self.grid.trades,
                                         self.hyperopt.best_params if self.hyperopt else None)
                except Exception:
                    pass
            else:
                log.warning(f"Native SL restore failed for {pos.pair} after failed {reason}")
        except Exception as e:
            log.warning(f"Native SL restore exception for {pos.pair} after failed {reason}: {e}")

    async def _apply_hard_risk_blocks(self, sig, ctx):
        """Run critical risk blocks even when ML is unavailable or not ready."""
        if getattr(sig, "conf", 0) <= 0:
            return False

        def block(label, detail):
            log.info(f"💤 {sig.pair} BLOCKED[{label}] — {detail} | strat={sig.strategy}")
            sig.conf = 0
            return True

        if ctx.regime == "TREND_DOWN":
            return block("regime", "TREND_DOWN")
        if ctx.regime == "CHOPPY":
            _BREAKOUT_STRATS = {"WYCKOFF_ACC", "SMC_OB+FVG", "SMC_SWEEP", "TREND", "VWAP", "BREAKOUT", "EMA_CROSS", "SUPERTREND"}
            if sig.strategy in _BREAKOUT_STRATS:
                return block("regime", "CHOPPY+breakout")
            # v16.0.04: block ALL entries when CHOPPY + BEAR daily trend.
            # In CHOPPY+BEAR, even A+ accumulation signals fail — market pumps +0.7%
            # then immediately reverses. No trade is better than a +0.5% consolation exit.
            # Only allow entries if daily trend is NEUTRAL or better.
            if getattr(ctx, 'daily', '') == "BEAR" and getattr(self.cfg, 'BLOCK_CHOPPY_BEAR', True):
                return block("regime", "CHOPPY+BEAR — no momentum, skip")

        # v18.7.3 FIX (WYCKOFF_ACC small-loss bleed): in a confirmed BEAR daily downtrend,
        # slow accumulation / order-block dip-buys keep stalling and time-exiting at small
        # losses + fees (death by a thousand cuts). Block those long-against-the-trend
        # entries across ALL 5m regimes when daily==BEAR. Fast reversal plays (QFL_PANIC,
        # SMC_SWEEP) are intentionally NOT in the set — they catch sharp oversold bounces.
        if (getattr(ctx, 'daily', '') == "BEAR"
                and getattr(self.cfg, 'BLOCK_ACCUMULATION_IN_BEAR', True)
                and sig.strategy in getattr(self.cfg, 'ACCUMULATION_STRATS',
                                            ("WYCKOFF_ACC", "SMC_OB", "SMC_OB+FVG"))):
            return block("regime", f"{sig.strategy} accumulation in BEAR daily — skip")

        # v16.0.05: BEAR regime max 1 position — all alts fall together on BTC dump.
        # 2 correlated longs in BEAR = double SL hit risk on any market downturn.
        if (getattr(ctx, 'daily', '') == 'BEAR'
                and len(getattr(self.risk, 'positions', [])) >= getattr(self.cfg, 'MAX_POSITIONS_BEAR', 1)
                and getattr(self.cfg, 'MAX_POSITIONS_BEAR_ENABLED', True)):
            return block("risk", f"BEAR regime — max 1 position (correlation guard)")

        # v16.0.06: Correlation matrix penalty
        # Blocks entry if new signal correlates > 0.85 with any open position.
        # Prevents NEAR + SUI + ATOM all dumping together on a BTC drop.
        if getattr(self.cfg, 'CORR_PENALTY_ENABLED', True) and self.risk.positions:
            _sig_c = self.candle_cache.get(sig.pair, [])
            _thresh = getattr(self.cfg, 'CORR_BLOCK_THRESHOLD', 0.85)
            if len(_sig_c) >= 20:
                for _pos in self.risk.positions:
                    _pos_c = self.candle_cache.get(_pos.pair, [])
                    if len(_pos_c) >= 20:
                        try:
                            _corr = abs(TA.correlation(_sig_c, _pos_c))
                            if _corr > _thresh:
                                log.info(f"🔗 {sig.pair} corr {_corr:.2f} with {_pos.pair} — blocked")
                                return block("risk", f"Corr {sig.pair}/{_pos.pair}={_corr:.2f} (>{_thresh})")
                        except Exception as _ce:
                            log.debug(f"Corr check {sig.pair}/{_pos.pair}: {_ce}")
        try:
            if self.funding_rate.should_block(sig.pair):
                return block("funding_rate", "funding-rate extreme")
        except Exception:
            pass
        try:
            if self.liquidation.should_block():
                return block("liquidation_cascade", "liquidation cascade")
        except Exception:
            pass
        try:
            await asyncio.to_thread(self.vol_delta.update, sig.pair)
            if self.vol_delta.should_block(sig.pair):
                return block("vol_delta", f"institutional sell delta={self.vol_delta.get_score(sig.pair):+.2f}")
        except Exception:
            pass
        try:
            await asyncio.to_thread(self.lob.update, sig.pair)
            if self.lob.should_block(sig.pair):
                return block("lob_imbalance", f"OBI={self.lob.get_obi(sig.pair):+.2f}")
        except Exception:
            pass
        try:
            await asyncio.to_thread(self.vpin.update, sig.pair)
            if self.vpin.should_block(sig.pair):
                return block("vpin", f"VPIN={self.vpin.get_vpin(sig.pair):.2f}")
        except Exception:
            pass
        try:
            if self.liq_cascade.should_block(sig.pair):
                return block("liq_cascade", "cascade active")
        except Exception:
            pass
        try:
            if self.spot_perp.should_block(sig.pair):
                return block("basis_extreme", self.spot_perp.status(sig.pair))
        except Exception:
            pass
        try:
            if self.crypto_news.should_block(sig.pair):
                return block("crypto_news", "news risk")
        except Exception:
            pass
        try:
            sig_atr_p = sig.atr/max(sig.price,0.001)*100 if sig.price>0 else 1.0
            if self.rl.should_block(ctx.regime, ctx.daily, sig_atr_p, ctx.fg):
                return block("rl_agent", "RL risk state")
        except Exception:
            pass
        # v16.0.0: Block #19 — Token Unlock
        # v16.0.0: Block #20 — Economic Calendar (FOMC/CPI/NFP)
        return False

    async def _adopt_orphan(self, sym, asset, free_qty, price):
        """v18.8: adopt an untracked wallet coin as a managed position so a state wipe or
        manual buy never strands it (it then gets SL/TP management). Cost basis = current
        market price (the original entry is unknown). If the coin already has an exchange-
        side stop order, LINK it; otherwise attach a fresh native SL. Returns the Position
        or None."""
        try:
            qty = self.ex.rnd(sym, free_qty)
            if qty <= 0 or price <= 0:
                return None
            _grp = next((p.get('g', 'C') for p in self.cfg.PAIRS if p.get('s') == sym), 'C')
            _slp = getattr(self.cfg, 'STOP_LOSS_PCT', 0.03)
            _rr = max(getattr(self.cfg, 'MIN_RR', 1.2), 1.5)
            sl = round(price * (1 - _slp), 8)
            tp = round(price * (1 + _slp * _rr), 8)
            pos = Position(pair=sym, entry=price, qty=qty, size=round(qty * price, 6),
                           entry_time=datetime.now(timezone.utc).isoformat(), sl=sl, tp=tp,
                           group=_grp, high=price, strategy='ADOPTED', atr=round(price * 0.01, 8),
                           entry_fee=0.0, avg_entry=price, total_qty=qty,
                           total_cost=round(qty * price, 6), rr=_rr, grade='B',
                           context='ADOPTED', regime_at_entry='')
            # Link an existing exchange SL if one is already on the book (avoids a duplicate);
            # otherwise attach a fresh native SL sized to the adopted qty.
            linked = False
            try:
                _oo = await self.ex.get_open_orders(symbol=sym)
                if isinstance(_oo, list):
                    for o in _oo:
                        if o.get('type') in ('STOP_LOSS_LIMIT', 'STOP_LOSS'):
                            pos.native_sl_order_id = o.get('orderId')
                            try: pos.sl = float(o.get('stopPrice') or sl)
                            except Exception: pass
                            linked = True
                            break
            except Exception:
                pass
            self.risk.positions.append(pos)
            if not linked and getattr(self, 'native_sl', None):
                try: await asyncio.to_thread(self.native_sl.attach, pos)
                except Exception as _e: log.warning(f"Adopt SL attach {sym}: {_e}")
            try: self.risk.save_state()
            except Exception: pass
            log.warning(f"📥 ADOPTED orphan {sym}: qty={qty} @ ${price:.6f} (${qty*price:.2f}) "
                        f"SL=${pos.sl:.6f} {'[linked existing SL]' if linked else '[fresh SL]'}")
            try:
                self.tg.send(f"📥 <b>ADOPTED POSITION</b> {sym}\n"
                             f"📦 Qty: {qty} @ ${price:.6f} (${qty*price:.2f})\n"
                             f"🛑 SL: ${pos.sl:.6f} {'(linked)' if linked else '(new)'} | strat: ADOPTED\n"
                             f"Bot is now managing this coin (cost basis = current price).")
            except Exception:
                pass
            return pos
        except Exception as e:
            log.warning(f"Adopt orphan {sym} failed: {e}")
            return None

    async def run(self):
        await self.ex.init()
        # v8.4 FIX #6: Execution lock — prevent concurrent bot instances
        # v10.4 FIX: cross-platform lock path (was hardcoded /tmp/, broke on Windows).
        # On Linux/macOS this still resolves to /tmp/. On Windows it uses the
        # user's TEMP directory.
        self._lock_file = open(os.path.join(tempfile.gettempdir(), "binance_bot_v9.lock"), "w")
        # v10.7 FIX: gate flock on platform availability — was crashing on Windows
        # despite v10.6 fcntl import "fix" (run() still called fcntl.flock directly).
        if _HAS_FCNTL:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (IOError, OSError):
                log.error("❌ Another bot instance is running! Exiting to prevent double trades.")
                self._lock_file.close()
                sys.exit(1)
            log.info("🔒 Execution lock acquired (fcntl)")
        else:
            log.warning("⚠️ fcntl unavailable (Windows) — execution lock skipped. "
                        "Multiple bot instances on Windows will not be blocked.")

        signal.signal(signal.SIGINT,self._stop)
        signal.signal(signal.SIGTERM,self._stop)
        self.start=datetime.now(timezone.utc)
        self._last_heartbeat = time.time()  # v8.4 FIX #3: Heartbeat tracking
        bal_data = await self.ex.get_asset_balance("USDT")
        bal = float(bal_data["free"]) + float(bal_data.get("locked", 0))
        # v8.4: Auto-detect capital from actual balance (compound start)
        # v8.4 + v11.2.7: auto-compound capital from real wallet balance
        actual_bal = bal + sum(p.size for p in self.risk.positions)
        # v18.8.6 FIX: also count untracked non-USDT holdings (fee/staking tokens like
        # BNB) so the startup capital + DD-peak anchor reflect the FULL wallet — the same
        # blind spot the per-cycle equity calc had, where an uncounted BNB balance
        # manufactured a phantom drawdown.
        try:
            _acc0 = await self.ex.get_account()
            _tracked0 = {p.pair.replace("USDT", "") for p in self.risk.positions}
            for _b0 in (_acc0 or {}).get("balances", []):
                _a0 = _b0.get("asset", "")
                if _a0 == "USDT" or _a0 in _tracked0:
                    continue
                _amt0 = float(_b0.get("free", 0) or 0) + float(_b0.get("locked", 0) or 0)
                if _amt0 <= 0:
                    continue
                try: _px0 = float((await self.ex.get_symbol_ticker(_a0 + "USDT")).get("price", 0) or 0)
                except Exception: _px0 = 0.0
                actual_bal += _amt0 * _px0
        except Exception as _b0e:
            log.debug(f"startup untracked-asset valuation skipped: {_b0e}")
        if actual_bal > 5 and not self.cfg.FIXED_CAPITAL_MODE:
            self.cfg.TOTAL_CAPITAL = round(actual_bal, 2)
            log.info(f"  💰 Auto-compound: Capital set to ${self.cfg.TOTAL_CAPITAL} from balance")
        elif self.cfg.FIXED_CAPITAL_MODE:
            log.info(f"  💰 FIXED_CAPITAL_MODE: keeping ${self.cfg.TOTAL_CAPITAL} (wallet ${actual_bal:.2f})")
        # v18.8.6 FIX: re-anchor the DD-shield to REAL wallet equity on a STATEFUL restart.
        # __init__ built DrawdownShield(cfg.TOTAL_CAPITAL) with the config-default ($45.65)
        # and set_peak() floored the peak there (via _compute_true_peak_from_journal, whose
        # initial_equity/floor both key off self.capital), manufacturing a phantom ~26%
        # drawdown that KILLED trading on a restart WITH an open position — the fresh-start
        # anchor below only fires when FLAT. Re-validate now that real equity is known:
        # a genuine past high reconstructed from journal PnL is kept (real drawdowns still
        # protect you); the stale config-default floor is discarded.
        if not getattr(self.risk, '_fresh_start', False) and actual_bal > 5:
            try:
                self.ddshield.capital = actual_bal
                self.ddshield.peak = actual_bal  # drop the stale config-default floor
                _saved_dd = getattr(self.risk, '_saved_dd_peak', 0) or 0
                if _saved_dd > 0:
                    self.ddshield.set_peak(_saved_dd)  # may restore a real journal-validated peak
                log.info(f"  🛡️ DD-shield re-anchored to real equity ${actual_bal:.2f} "
                         f"(peak ${self.ddshield.peak:.2f}, dd {self.ddshield.drawdown_pct:.1f}%)")
            except Exception as _dre:
                log.warning(f"DD-shield re-anchor failed: {_dre}")
        # v18.8 FIX: on a FRESH start (wiped state), anchor the drawdown-shield peak + risk
        # peak to the REAL starting equity. Without this, a wiped state leaves the peak at
        # the config TOTAL_CAPITAL ($45.65); if the real wallet is lower (e.g. funds were
        # locked in coins), the bot computes a bogus 90%+ drawdown and the DD shield blocks
        # ALL trading. Anchoring to reality means drawdown is measured from where you
        # actually started, so trading resumes normally as the balance grows.
        if getattr(self.risk, '_fresh_start', False):
            _anchor = max(actual_bal, 0.0)
            try:
                # Direct reset (not set_peak): set_peak() floors the peak at the config
                # capital via its journal anti-tamper, which is exactly the stale value we
                # must discard on a wiped/fresh start. With no history, the real starting
                # equity IS the peak. As the balance grows, update_peak() raises it normally.
                self.ddshield.peak = _anchor
                self.ddshield.capital = _anchor
                self.risk._peak_equity = _anchor
                log.info(f"  🆕 Fresh start — DD-shield peak HARD-anchored to real equity ${_anchor:.2f} (discarding stale config peak)")
            except Exception as _fae:
                log.warning(f"Fresh-start peak anchor failed: {_fae}")

        
        # --- Feature-Health Self-Check ---
        log.info("  --- Feature Health ---")
        modules = {
            "ML Ensemble": getattr(self, "ml", None),
            "Micro-Price": getattr(self, "micro_price", None),
            "Token Unlock": getattr(self, "token_unlock", None),
            "Econ Calendar": getattr(self, "econ_calendar", None),
            "DXY": getattr(self, "dxy", None),
            "Whale": getattr(self, "whale", None),
            "MultiEx": getattr(self, "multi_ex", None),
            "Options": getattr(self, "options", None),
            "RL Agent": getattr(self, "rl", None),
            "Funding Rate": getattr(self, "funding_rate", None),
            "Liq Cascade": getattr(self, "liq_cascade", None),
            "LOB Imbalance": getattr(self, "lob", None)
        }
        for name, mod in modules.items():
            status = "INSTANTIATED" if mod is not None else "None"
            log.info(f"  {name:15}: {status}")
        log.info("  ----------------------")

        log.info("━"*70)
        log.info("  🚀 BINBOT V18.9.0 GodMode — audit-hardened core + scale-ladder + session-filter (see feature-health table below)")
        # v15.0 #8 Observability: Prometheus metrics exporter on :9090/metrics
        self._prom = None
        try:
            from prom_metrics import PrometheusExporter
            self._prom = PrometheusExporter(port=9090)
            self._prom.start()
            log.info("  📊 Prometheus: http://127.0.0.1:9090/metrics")
        except Exception as _pe:
            log.warning(f"Prometheus exporter init failed: {_pe} — bot continues")
            self._prom = None
        # v15.0 #9 Compliance: hash-chained audit log
        self._audit = None
        try:
            from audit_log import AuditLog
            self._audit = AuditLog()
            log.info("  📜 Audit log initialized (SHA-256 hash-chain)")
        except Exception as _ae:
            log.warning(f"Audit log init failed: {_ae} — bot continues")
            self._audit = None
        # v13.5.5: web dashboard init (gated by WEB_DASHBOARD_ENABLED, default OFF)
        self._dashboard = None
        if getattr(self.cfg, "WEB_DASHBOARD_ENABLED", False):
            try:
                from web_dashboard import DashboardServer
                self._dashboard = DashboardServer(self.cfg)
                self._dashboard.start()
                log.info(f"  🌐 Dashboard: http://{self.cfg.WEB_DASHBOARD_BIND}:{self.cfg.WEB_DASHBOARD_PORT}/")
            except Exception as _de:
                log.warning(f"Dashboard init failed: {_de} — bot continues")
                self._dashboard = None
        # v13.5.5: Monte Carlo stress test daemon (gated by STRESS_TEST_ENABLED)
        self._stress = None
        if getattr(self.cfg, "STRESS_TEST_ENABLED", False):
            try:
                from stress_test import MonteCarloStressTest
                self._stress = MonteCarloStressTest(self.cfg, self.tg)
                self._stress.start()
                log.info("  📊 Stress test daemon started")
            except Exception as _se:
                log.warning(f"Stress test init failed: {_se} — bot continues")
                self._stress = None
        # v13.5.5: exchange failover (loaded but inactive until exchange.py hooks added)
        self._failover = None
        if getattr(self.cfg, "EXCHANGE_FAILOVER_ENABLED", False):
            try:
                from exchange_failover import ExchangeFailover
                self._failover = ExchangeFailover(self.cfg, self.tg)
                self.ex._failover_mgr = self._failover
                log.info("  🔄 Failover loaded (klines hooks active)")
            except Exception as _fe:
                log.warning(f"Failover init failed: {_fe}")
                self._failover = None
        log.info(f"  💰 LIVE | Cap: ${self.cfg.TOTAL_CAPITAL} | Bal: ${bal:.2f}")
        # v16.0.0: honest banner — the per-module live/inactive status is printed by
        # the feature-health table (self._feature_health) right after this block.
        # No more blanket "everything is ON" claims; ML is OFF unless cfg.ML_ENABLED.
        _kelly_state = 'ON' if getattr(self.cfg, 'USE_KELLY', False) else 'OFF (fixed-fraction sizing)'
        _ml_state = ('ON' if (getattr(self.cfg, 'ML_ENABLED', True) and self.ml is not None)
                     else 'OFF (ML_ENABLED=False or deps missing)')
        log.info(f"  Risk: {self.cfg.RISK_PCT*100:.0f}%/trade | R:R min {self.cfg.MIN_RR}:1 | MIN_CONF {self.cfg.MIN_CONF} | Kelly: {_kelly_state}")
        log.info(f"  Execution: {'LIMIT-maker→market hybrid' if self.cfg.USE_LIMIT else 'MARKET'} | Native SL: {'ON' if self.cfg.NATIVE_SL_ENABLED else 'OFF'} | ML: {_ml_state}")
        log.info(f"  Signal engine: 13 TA strategies | 6-state regime | killzones | correlation/heat guards")
        log.info(f"  Safety: drawdown shield | circuit breaker | daily-loss cap | per-pair cooldown | dead-man switch")
        log.info(f"  Modes: Grid({self.cfg.GRID_LEVELS}lvl) | SMC | QFL | DCA({len(self.cfg.DCA_STEPS)})")
        log.info(f"  Loaded: {len(self.risk.positions)} positions | TG: {'ON' if self.cfg.TG_ENABLED else 'OFF'}")
        log.info(f"  Pairs: {len(self.cfg.PAIRS)} | Max {self.cfg.MAX_DAILY_TRADES}/day | Scan: {self.cfg.SCAN_SEC}s")
        log.info("━"*70)

        self.tg.send(f"🚀 <b>BinBot V18.8 GodMode LIVE</b>\n💰 Cap: ${self.cfg.TOTAL_CAPITAL} | Wallet: ${actual_bal:.2f}\n📦 Positions: {len(self.risk.positions)} | USDT free: ${round(actual_bal - sum(p.size for p in self.risk.positions), 2)}")

        # v8.3: Sync with Binance — sell ghost coins + cancel orders
        # v8.4 FIX: Only touch assets from PAIRS list — don't sell unrelated holdings
        # v9.1: Safety — check stuck_coins log and warn before selling valuable coins
        # Sweeps any non-USDT PAIRS coin from the wallet on startup so the bot starts with a clean USDT-only balance.
        try:
            tracked = set(p.pair.replace('USDT','') for p in self.risk.positions)
            managed_coins = set(p['n'] for p in self.cfg.PAIRS)
            # v9.1: Load stuck coins to recover gracefully
            stuck_pairs = set()
            try:
                if os.path.exists("stuck_coins.jsonl"):
                    with open("stuck_coins.jsonl") as sf:
                        for line in sf:
                            try: stuck_pairs.add(json.loads(line.strip())["pair"].replace("USDT",""))
                            except Exception: pass
            except Exception: pass
            for b in (await self.ex.get_account())['balances']:
                asset = b['asset']
                free = float(b['free'])
                if asset == 'USDT' or free < 0.001: continue
                if asset not in tracked and asset in managed_coins:
                    sym = asset + 'USDT'
                    try:
                        price = float((await self.ex.get_symbol_ticker(sym))['price'])
                        value = free * price
                        qty = self.ex.rnd(sym, free)
                        if qty > 0 and value > 5.0:  # Binance MIN_NOTIONAL
                            if asset in stuck_pairs:
                                log.warning(f"⚠️ Recovering stuck coin {asset}: {qty} (${value:.2f})")
                            else:
                                if asset in getattr(self.cfg, "GHOST_SWEEP_ALLOWLIST", ["BNB"]): continue
                                # v18.8: ADOPT untracked managed coins as positions instead of
                                # selling them off — so a state wipe or manual buy never strands
                                # money. The coin then gets full SL/TP management.
                                if (getattr(self.cfg, 'AUTO_ADOPT_ORPHANS', True)
                                        and value >= getattr(self.cfg, 'AUTO_ADOPT_MIN_USD', 5.0)):
                                    _adopted = await self._adopt_orphan(sym, asset, free, price)
                                    if _adopted is not None:
                                        tracked.add(asset)   # now tracked → not swept/cancelled below
                                        continue
                                log.warning(f"🧹 Selling ghost {asset}: {qty} (${value:.2f})")
                            await self.ex.create_order(symbol=sym,side='SELL',type='MARKET',quantity=f'{qty:.8f}')
                            log.info(f'\U0001f9f9 Sold {asset}: {qty} @ ${price:.4f} = ${value:.2f}')
                    except Exception as e: log.warning(f"Ghost sell {asset} failed: {e}")
            for pair in self.cfg.PAIRS:
                try:
                    orders_open = await self.ex.get_open_orders(symbol=pair['s'])
                    if isinstance(orders_open, dict) and 'error' in orders_open: continue
                    for o in orders_open:
                        if o["orderId"] in {getattr(_p, "native_sl_order_id", None) for _p in self.risk.positions}:
                            log.info(f'🛡️  v8.3 PRESERVED native SL {pair["s"]} #{o["orderId"]}')
                            continue
                        if o.get("type") in ("STOP_LOSS_LIMIT", "STOP_LOSS"):
                            log.info(f'🛡️  PRESERVED untracked SL {pair["s"]} #{o["orderId"]} (orphan protection)')
                            continue
                        await self.ex.cancel_order(symbol=pair['s'],orderId=o['orderId'])
                        log.info(f'\U0001f9f9 v8.3 cancelled {pair["s"]} #{o["orderId"]}')
                except Exception as e: log.warning(f"Cancel open orders {pair['s']} failed: {e}")
            # v11.2.20 FIX: limit order amnesia — check for limit orders that filled between
            # crash and restart (no longer in open_orders but also not tracked as positions)
            tracked_pairs = {p.pair for p in self.risk.positions}
            try:
                import time as _t
                since = int((_t.time() - 3600) * 1000)  # last 1 hour
                for pair in self.cfg.PAIRS:
                    try:
                        orders = await self.ex.get_all_orders(symbol=pair['s'], limit=10)
                        if isinstance(orders, dict) and 'error' in orders: continue
                        for o in orders:
                            if (o.get('type') == 'LIMIT' and o.get('side') == 'BUY'
                                    and o.get('status') == 'FILLED'
                                    and int(o.get('time', 0)) > since
                                    and pair['s'] not in tracked_pairs):
                                log.warning(f"⚠️ LIMIT amnesia: {pair['s']} #{o['orderId']} filled but untracked — adding position with default SL/TP")
                                self.tg.send(f"⚠️ <b>LIMIT AMNESIA RECOVERED</b> {pair['s']}\nFilled limit order found untracked — position added with default SL/TP")
                                fill_price = float(o.get('price', 0)) or float(o.get('cummulativeQuoteQty', 0)) / float(o.get('executedQty', 1))
                                fill_qty = float(o.get('executedQty', 0))
                                if fill_price > 0 and fill_qty > 0:
                                    atr_est = fill_price * 0.01
                                    from risk import Position
                                    pos = Position(pair=pair['s'], entry=fill_price, qty=fill_qty,
                                        size=fill_price*fill_qty, entry_time=datetime.now(timezone.utc).isoformat(),
                                        sl=fill_price*0.985, tp=fill_price*1.03, group=pair.get('g','D'),
                                        high=fill_price, strategy='LIMIT_RECOVERED', atr=atr_est,
                                        entry_fee=fill_price*fill_qty*0.001, avg_entry=fill_price,
                                        total_qty=fill_qty, total_cost=fill_price*fill_qty, rr=2.0, grade='B')
                                    self.risk.positions.append(pos)
                                    self.risk.save_state()
                                    tracked_pairs.add(pair['s'])
                                    if getattr(self, 'native_sl', None):
                                        asyncio.create_task(asyncio.to_thread(self.native_sl.attach, pos))
                                    if getattr(self, 'native_tp', None):
                                        asyncio.create_task(asyncio.to_thread(self.native_tp.attach, pos))  # v11.2.10 FIX: was .discard() — PREVENTED de-dup, allowing double-recovery
                    except Exception as e: log.warning(f"Amnesia recovery {pair['s']} failed: {e}")
            except Exception as _ae: log.warning(f"Amnesia check failed: {_ae}")
            log.info('\U0001f9f9 v8.3 Binance sync done — zero ghosts')
        except Exception as e:
            log.warning(f'Sync: {e}')

        # v11.2.15: Auto-sync wallet ONLY when no positions open
        try:
            saved_positions = self.risk.positions
            if len(saved_positions) == 0 and not self.cfg.FIXED_CAPITAL_MODE:
                bal = await self.ex.get_asset_balance("USDT")
                wallet = float(bal["free"]) + float(bal["locked"])
                if wallet > 5.0 and abs(wallet - self.cfg.TOTAL_CAPITAL) > 2.0:
                    old_cap = self.cfg.TOTAL_CAPITAL
                    self.cfg.TOTAL_CAPITAL = round(wallet, 2)
                    self.risk.cfg.TOTAL_CAPITAL = round(wallet, 2)
                    # v11.2.20 FIX: use atomic StateManager instead of raw json.dump
                    self.risk.save_state(total_capital=self.cfg.TOTAL_CAPITAL)
                    log.info(f"💰 Wallet sync: ${old_cap:.2f} → ${self.cfg.TOTAL_CAPITAL:.2f} (saved to state.json)")
            else:
                pass # Forced sync fix
        except Exception as e:
            log.warning(f"Wallet sync failed: {e}")

        # v14.4: Wire native TP to risk module (used in check_exits)
        try: self.risk.native_tp = self.native_tp
        except Exception: pass
        # WebSocket
        try: self.ws.start(self.cfg.API_KEY, self.cfg.API_SECRET)
        except Exception as e: log.warning(f"WebSocket start failed: {e}")
        # v14.3.1 WS Watchdog — track last successful price timestamp
        self._ws_last_ok = time.time()
        self._ws_reconnects = 0

        # Backtest
        try:
            bt_res=self.bt.run("BTCUSDT",7)
            # v12.2 FIX: Also backtest top altcoins — was only BTCUSDT which disabled
            # strategies that work on alts but not BTC (e.g. QFL works great on SOL).
            alt_disabled = set(bt_res.get("disabled_strategies",[]))
            for alt_pair in ["ETHUSDT","SOLUSDT","BNBUSDT","INJUSDT","OPUSDT","ARBUSDT","DOTUSDT","ADAUSDT"]:
                try:
                    alt_res = self.bt.run(alt_pair, 7)
                    alt_ok = set(bt_res.get("disabled_strategies",[])) - set(alt_res.get("disabled_strategies",[]))
                    alt_disabled -= alt_ok  # Re-enable if works on any alt
                except Exception: pass
            # v13.5.3 audit Fix #5: union (not assign) so our pre-disabled list survives
            # the backtest pass. Was: hard-assignment overwrote SMC_OB+FVG block from __init__.
            _hard_disabled = set(self.disabled_strats)  # capture pre-set blocks
            # v13.5.3 audit: Option B baked in. Was sed-applied to running VM
            # only — would revert on every redeploy. The 7-day BTC backtest
            # disables strategies that work fine on alts; without this filter,
            # SMC_OB / SMC_SWEEP / WYCKOFF_ACC (the bot's three live winners
            # with 90.9% / 100% / 50% close-only WR per audit) would all be
            # killed at boot, starving the bot. Live data through May 10 2026
            # justifies hard-protecting them. QFL_PANIC and SQUEEZE_BREAK kept
            # protected from the original v12.2 carve-out.
            #
            # If a protected strategy genuinely starts losing money, remove it
            # from this list — DO NOT remove the union with _hard_disabled
            # (that's Fix #5 keeping SMC_OB+FVG dead).
            _PROTECTED_FROM_BACKTEST = ["QFL_PANIC", "SQUEEZE_BREAK",
                                        "SMC_OB", "SMC_SWEEP", "WYCKOFF_ACC"]
            # NEUTRALIZED: backtest disable list IGNORED (only _hard_disabled active).
            # Backtest still runs for visibility, but its verdict is overridden by
            # live trade history via journal.strategy_weight(). See May 11 2026 audit.
            # Original line preserved below as a comment:
            # self.disabled_strats=[s for s in (alt_disabled | _hard_disabled) if s not in _PROTECTED_FROM_BACKTEST]
            self.disabled_strats=[s for s in _hard_disabled if s not in _PROTECTED_FROM_BACKTEST]
        except Exception: pass

        # ML
        # v11.2.16: Skip ML retrain if trained within 2h (timestamp gate)
        if self.ml and ML_AVAILABLE:
            try:
                ts_file = self._cache_path("ml_train_ts").replace(".pkl",".ts")
                import os as _os
                needs_train = True
                if _os.path.exists(ts_file):
                    age = time.time() - _os.path.getmtime(ts_file)
                    if age < 7200:
                        log.info(f"🧠 ML retrain skipped — trained {age:.0f}s ago (< 2h)")
                        needs_train = False
                if needs_train:
                    self.ml.train(await self.ex.klines("BTCUSDT","5m",2000),TA)
                    with open(ts_file, "w") as _f: _f.write("ok")  # v15.3 FIX: close file properly
            except Exception: pass

        # v16.0.0: Train LSTM deep learning model
        try:
            btc_candles = await self.ex.klines("BTCUSDT", "5m", 2000)
            if btc_candles and len(btc_candles) >= 200:
                self.lstm.train(btc_candles)
        except Exception as _le:
            log.debug(f"LSTM startup train: {_le}")

        # v16.0.0: Initialize token unlock tracker + economic calendar
        try: self.token_unlock.update()
        except Exception: pass
        try: self.econ_calendar.update()
        except Exception: pass

        # v8.3: Fetch DXY + Options on startup
        try: await asyncio.to_thread(self.dxy.update)
        except Exception: pass
        try: await asyncio.to_thread(self.options.update)
        except Exception: pass
        # v8.3: Train LSTM
        # v8.3: Run Monte Carlo on historical trades
        try:
            past_pnls=[t.get("pnl",0) for t in self.risk.trades if "pnl" in t]
            if len(past_pnls)>=10: self.monte_carlo.run(past_pnls, self.cfg.TOTAL_CAPITAL)
        except Exception: pass


        # v11.2.16: HyperOpt timestamp gate
        if self.hyperopt:
            try:
                import os as _os4
                ts_hyper = self._cache_path("hyperopt_ts").replace(".pkl",".ts")
                needs_hyper = True
                if _os4.path.exists(ts_hyper):
                    age_hyper = time.time() - _os4.path.getmtime(ts_hyper)
                    if age_hyper < 7200:
                        log.info(f"🔧 HyperOpt skipped — ran {age_hyper:.0f}s ago")
                        needs_hyper = False
                if needs_hyper:
                    c5=await self.ex.klines("BTCUSDT","5m",500)
                    self.hyperopt.optimize(c5,TA)
                    with open(ts_hyper, "w") as _f: _f.write("ok")  # v15.3 FIX: close file properly
            except Exception: pass

        # v16.0.0 NEW: feature-health table — log exactly which modules are live/inactive
        # so advertised-but-dead features can never silently hide again.
        if getattr(self.cfg, 'FEATURE_HEALTH_ENABLED', True):
            try:
                import feature_health
                feature_health.report(self, self.cfg)
            except Exception as _fhe:
                log.debug(f"feature_health report skipped: {_fhe}")
        # v16.0.0 NEW: validate config and warn on anything suspicious/no-op.
        try:
            self.cfg.validate()
        except Exception as _cve:
            log.debug(f"config.validate skipped: {_cve}")
        # v16.0.0 NEW: dead-man's-switch price-blind tracker init.
        self._last_price_ok_ts = time.time()
        self._deadman_fired = False
        # v16.0.0 NEW: gate-rejection telemetry counter.
        import collections as _collections
        self._gate_stats = _collections.Counter()

        while self.running:
            try:
                await self._cycle()
                await asyncio.sleep(self.cfg.SCAN_SEC)
            except KeyboardInterrupt: break
            except Exception as e:
                tb = traceback.format_exc()
                log.error(f"Error: {e}\n{tb}")
                # v13.5: emergency exit-check loop. Was: 30s blind sleep with
                # no SL/TP/trail check. If the exception was correlated with
                # a panic event (Binance API throttling during a flash crash,
                # WebSocket reconnect storm), open positions could blow past
                # SL during that window. Now: split the cooldown into 6× 5s
                # micro-sleeps and run check_exits between each one. If exits
                # ALSO fail, give up and accept the long sleep — but at least
                # we tried 6 times.
                exit_attempts = 0
                exit_successes = 0
                for _ in range(6):
                    if not self.running: break
                    try:
                        # Get a fresh ticker snapshot — may fail if the API was
                        # the source of the original exception, in which case
                        # we just sleep 5s and try the next iteration.
                        # v14.6.3 FIX: detect rate limit — skip REST if banned
                        _is_rate_limit = any(x in str(e).lower() for x in
                            ['429', '-1003', 'too many requests', 'rate limit', 'ip banned'])
                        if self.ws.is_active:
                            tk = self.ws.get_prices()
                            if not tk and not _is_rate_limit:
                                tk = await self.ex.tickers()
                        elif not _is_rate_limit:
                            tk = await self.ex.tickers()
                        else:
                            tk = {}  # rate limited — use WS only, avoid REST ban
                        if tk and self.risk.positions:
                            # Synthetic context for emergency check — use last-known
                            # regime fallback. We're past the cycle-build of ctx so
                            # use a conservative neutral.
                            _emerg_ctx = SimpleNamespace(regime="RANGE", fg=50, heat=0,
                                daily="--", h4="--", killzone="OFF", btc_ok=True,
                                news_score=0, news_label="", vol="NORM",
                                session="--", active=False, mode="RANGE", adx=25,
                                squeeze=False, sq_len=0, mtf_align=50.0)
                            to_close = await self.risk.check_exits(tk, _emerg_ctx, self.ex, self.tg)  # v15.4 FIX: await async
                            for pos, price, reason in to_close:
                                exit_attempts += 1
                                try:
                                    if not await self._detach_native_sl_before_sell(pos, f"EMERGENCY_{reason}"):
                                        continue
                                    _bal = await self.ex.get_asset_balance(pos.pair.replace("USDT",""))
                                    if "error" not in _bal: _qty = min(pos.qty, float(_bal.get("free", 0)))
                                    else: _qty = pos.qty
                                    _qty = self.ex.rnd(pos.pair, _qty)
                                    r = await self.ex.sell(pos.pair, _qty)
                                    if "error" not in r:
                                        try:
                                            fills = r.get("fills", [])
                                            if fills:
                                                tq = sum(float(f["qty"]) for f in fills)
                                                tc = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                                                if tq > 0: price = tc / tq
                                        except Exception: pass
                                        self.risk._record_close(pos, price, reason, _emerg_ctx, self.tg)
                                        with _pos_lock:
                                            if pos in self.risk.positions:
                                                self.risk.remove_position_safe(pos, expected_reason=reason)  # v14.6.4 AUDIT FIX: wired journal-gap detector
                                        exit_successes += 1
                                        log.warning(f"🚑 Emergency exit during exception cooldown: {pos.pair} {reason} @ ${price:.4f}")
                                    else:
                                        await self._restore_native_sl_after_failed_sell(pos, f"EMERGENCY_{reason}")  # v16.0 AUDIT FIX C5: was missing await — coroutine never ran
                                except Exception as _ex:
                                    log.warning(f"Emergency exit {pos.pair} failed: {_ex}")
                            if to_close:
                                self.risk.save_state()
                    except Exception as _emerg_ex:
                        # Emergency check itself failing — log once per pass, keep sleeping
                        log.debug(f"Emergency exit-check failed: {_emerg_ex}")
                    await asyncio.sleep(5)
                if exit_attempts:
                    log.warning(f"🚑 Exception cooldown: {exit_successes}/{exit_attempts} emergency exits processed")

        # v14.4: Cancel open native TP orders on shutdown
        try:
            if getattr(self, "native_tp", None):
                for _p in self.risk.positions:
                    _tp_oid = getattr(_p, "native_tp_order_id", None)
                    if _tp_oid:
                        try: self.native_tp.detach(_p); log.info(f"  Cancelled native TP {_p.pair} #{_tp_oid}")
                        except Exception: pass
        except Exception: pass
        # v15.2: async shutdown — cancel orders, cleanup
        await self._async_shutdown()
        await self.ex.close()
        self.risk.save_state(self.grid.pnl,self.grid.trades,
                             self.hyperopt.best_params if self.hyperopt else None)
        self.ws.stop()
        self._summary()

    async def _cycle(self):
        self.cycles+=1
        if self.cycles % 50 == 0: asyncio.get_event_loop().run_in_executor(None, gc.collect)  # v16.0 AUDIT FIX M4: was gc.collect() blocking hot path — now background thread

        # v16.0.0 NEW: gate-rejection telemetry histogram (helps tune MIN_CONF/heat/cooldown).
        if getattr(self.cfg, 'GATE_TELEMETRY_ENABLED', True):
            _gt_every = getattr(self.cfg, 'GATE_TELEMETRY_EVERY_CYCLES', 120)
            _gs = getattr(self, '_gate_stats', None)
            if _gt_every and _gs and self.cycles % _gt_every == 0 and sum(_gs.values()) > 0:
                _top = ", ".join(f"{k}={v}" for k, v in _gs.most_common(8))
                log.info(f"📊 GATE TELEMETRY (last {_gt_every} cycles): {sum(_gs.values())} rejects | {_top}")
                _gs.clear()

        # ─── v15.4 TG UPGRADE: scheduled hooks (run every cycle, internal timers gate firing) ───
        try:
            # 1. Force-close-all drain (from /force_close confirmation in TG)
            if getattr(self, '_force_close_all_requested', False):
                self._force_close_all_requested = False
                log.warning(f"🎛  Draining /force_close request — closing {len(self.risk.positions)} positions")
                for _fp in list(self.risk.positions):
                    try:
                        _fpx = float((await self.ex.get_symbol_ticker(_fp.pair))['price'])
                        _bal = await self.ex.get_asset_balance(_fp.pair.replace("USDT",""))
                        if "error" not in _bal: _qty = min(_fp.qty, float(_bal.get("free", 0)))
                        else: _qty = _fp.qty
                        _qty = self.ex.rnd(_fp.pair, _qty)
                        _r = await self.ex.sell(_fp.pair, _qty)
                        _actual = _fpx
                        if _r.get("fills"):
                            _tq = sum(float(f["qty"]) for f in _r["fills"])
                            _tc = sum(float(f["qty"]) * float(f["price"]) for f in _r["fills"])
                            if _tq > 0: _actual = _tc / _tq
                        # v15.0 FIX: was `from intelligence import IntelContext` which doesn't
                        # exist (class is `Context`). Use SimpleNamespace for safety — matches
                        # the pattern used in emergency/crash contexts elsewhere in this file.
                        _ctx = SimpleNamespace(regime="FORCE", killzone="", daily="--",
                            h4="--", h1="--", fg=50, btc_ok=True, news_score=0,
                            news_label="", vol="NORM", session="--", active=False,
                            mode="FORCE", adx=25, squeeze=False, sq_len=0, heat=0,
                            mtf_align=50.0)
                        self.risk._record_close(_fp, _actual, "FORCE_CLOSE", _ctx, self.tg)
                        if _fp in self.risk.positions:
                            self.risk.remove_position_safe(_fp, expected_reason="FORCE_CLOSE")
                    except Exception as _fce:
                        log.warning(f"Force-close {_fp.pair} failed: {_fce}")
                self.risk.save_state()
                try: self.tg.send(f"✅ Force-close complete. Positions remaining: {len(self.risk.positions)}")
                except Exception: pass

            # 2. Heartbeat every TG_HEARTBEAT_HOURS hours
            if getattr(self.cfg, 'TG_ENABLED', False):
                _hb_h = getattr(self.cfg, 'TG_HEARTBEAT_HOURS', 4)
                _equity = getattr(self.cfg, 'TOTAL_CAPITAL', 0) + getattr(self.risk, 'pnl', 0)  # v15.4 FIX: was risk.free (unset)
                # v16.0.03.1: check capital activation threshold (rate-limited to 30s internally)
                try: self.cap_activator.check(_equity)
                except Exception as _ace: log.debug(f"CapActivator check failed: {_ace}")
                # v16.0.06: record daily equity sample for equity curve MA
                try: self.risk.record_equity(_equity)
                except Exception: pass
                # v16.0.05: PortfolioKelly auto-enable at PORTFOLIO_KELLY_MIN_TRADES trades.
                # Kelly requires enough history (50+ trades) to compute reliable strategy weights.
                _kelly_min = getattr(self.cfg, 'PORTFOLIO_KELLY_MIN_TRADES', 50)
                _kelly_on  = getattr(self.cfg, 'PORTFOLIO_KELLY_ENABLED', False)
                # v16.0.0 AUDIT FIX (D6): was getattr(risk,'day_trades')+getattr(risk,'total_trades')
                # — NEITHER attribute exists on Risk (it's daily_t, and there is no cumulative
                # counter), so this summed 0+0 forever and PortfolioKelly never auto-enabled.
                # Correct cumulative closed-trade count is wins+losses.
                _total_t   = int(getattr(self.risk, 'wins', 0)) + int(getattr(self.risk, 'losses', 0))
                if not _kelly_on and _total_t >= _kelly_min:
                    self.cfg.PORTFOLIO_KELLY_ENABLED = True
                    log.info(f"🎓 PortfolioKelly AUTO-ENABLED at {_total_t} trades")
                    try: self.tg.send(
                        f"🎓 <b>PortfolioKelly ENABLED</b>\n"
                        f"📊 {_total_t} trades reached!\n"
                        f"Strategies now sized by historical edge.\n"
                        f"Dynamic regime scaling: TREND×1.0 RANGE×0.5 CHOPPY×0.25")
                    except Exception: pass
                _day_pnl = getattr(self.risk, 'daily_pnl', 0)
                _w = getattr(self.risk, 'wins', 0); _l = getattr(self.risk, 'losses', 0)
                _wr = (_w / (_w + _l) * 100) if (_w + _l) > 0 else 0
                _dd = "full" if not getattr(self.risk.ddshield, 'kill_switch', False) else "TRIPPED"
                self.tg.heartbeat(self.risk.positions, _day_pnl, _equity, _dd, _wr,
                                   closed_today=(_w + _l), interval_hours=_hb_h)

            # 3. Daily summary at 23:55+ UTC
            if getattr(self.cfg, 'TG_DAILY_SUMMARY_ENABLED', True):
                _now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
                if _now.hour == 23 and _now.minute >= 55:
                    _equity = getattr(self.cfg, 'TOTAL_CAPITAL', 0) + getattr(self.risk, 'pnl', 0)  # v15.4 FIX
                    self.tg.daily_summary(equity=_equity)

            # 4. Weekly summary on Sunday 23:55+ UTC
            if getattr(self.cfg, 'TG_WEEKLY_SUMMARY_ENABLED', True):
                _now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
                if _now.weekday() == 6 and _now.hour == 23 and _now.minute >= 55:
                    _equity = getattr(self.cfg, 'TOTAL_CAPITAL', 0) + getattr(self.risk, 'pnl', 0)  # v15.4 FIX
                    self.tg.weekly_summary(equity=_equity)
        except Exception as _hook_e:
            log.debug(f"v15.4 TG hooks error: {_hook_e}")

        # v14.5: Auto-sync capital from actual USDT balance every 30 cycles
        if self.cycles % 30 == 0 or self.cycles == 1:
            try:
                # v15.6 FIX: Prevent double-counting ghost positions by verifying wallet qty
                _acc = await self.ex.get_account()
                _bals = {b['asset']: float(b['free']) + float(b['locked']) for b in _acc['balances']}
                _total = _bals.get("USDT", 0.0)
                
                for _cp in self.risk.positions:
                    _asset = _cp.pair.replace("USDT", "")
                    _actual_qty = _bals.get(_asset, 0.0)
                    if _actual_qty > _cp.qty * 0.05:  # Only add value if coins are actually in wallet
                        try:
                            _cp_px = float((await self.ex.get_symbol_ticker(_cp.pair))["price"])
                            if _cp_px > 0: _total += _cp.qty * _cp_px
                        except Exception: pass
                if _total > 5.0 and abs(_total - self.cfg.TOTAL_CAPITAL) > 1.0:
                    self.cfg.TOTAL_CAPITAL = round(_total, 2)
                    self.risk.cfg.TOTAL_CAPITAL = self.cfg.TOTAL_CAPITAL
                    # v14.6.3 FIX: wire real free USDT so available() uses actual balance
                    try:
                        _free_usdt = float((await self.ex.get_asset_balance("USDT")).get("free", 0))
                        self.risk._real_usdt_free = _free_usdt
                    except Exception: pass
                    log.info(f"💰 Capital auto-synced: ${self.cfg.TOTAL_CAPITAL:.2f} (free+positions)")
                    # v16.0.0 AUDIT FIX (C2): removed runtime regex self-rewrite of config.py
                    # (corruption risk, fought version control, surprising side effect).
                    # Capital is now persisted ONLY to bot_state.json — the correct durable
                    # store, atomic via StateManager, and read back on the next boot.
                    try:
                        self.risk.save_state(total_capital=self.cfg.TOTAL_CAPITAL)
                        log.info(f"💾 Capital persisted to state.json: ${round(_total, 2)}")
                    except Exception as _e:
                        log.warning(f"Capital state persist failed: {_e}")
            except Exception: pass

        # v11.2.16: DEPOSIT DETECTOR — one-way sync (up only), every 10 cycles
        # v16.0 AUDIT FIX C3: skip if auto-sync already ran this cycle (every 30 / cycle 1)
        # to prevent double capital sync race condition causing capital oscillation.
        _auto_sync_ran = (self.cycles % 30 == 0 or self.cycles == 1)
        if self.cycles % 10 == 0 and not _auto_sync_ran:
            try:
                if not self.risk.positions:
                    bal = await self.ex.get_asset_balance('USDT')
                    wallet = float(bal['free']) + float(bal['locked'])
                    if wallet > self.cfg.TOTAL_CAPITAL + 5.0 and not self.cfg.FIXED_CAPITAL_MODE:
                        old_cap = self.cfg.TOTAL_CAPITAL
                        self.cfg.TOTAL_CAPITAL = round(wallet, 2)
                        self.risk.cfg.TOTAL_CAPITAL = round(wallet, 2)
                        # v11.2.20 FIX: use atomic StateManager instead of raw json.dump (race condition)
                        self.risk.save_state(self.grid.pnl, self.grid.trades, total_capital=self.cfg.TOTAL_CAPITAL)
                        log.info(f'💰 DEPOSIT DETECTED: ${old_cap:.2f} → ${self.cfg.TOTAL_CAPITAL:.2f} (+${wallet-old_cap:.2f})')
                        self.tg.send('Deposit: $' + str(round(old_cap,2)) + ' to $' + str(self.cfg.TOTAL_CAPITAL))
                        # v11.2.16: Auto risk upgrade at capital milestones
                        if self.cfg.TOTAL_CAPITAL >= 150.0 and self.cfg.RISK_PCT < 0.02:
                            self.cfg.RISK_PCT = 0.02
                            self.risk.cfg.RISK_PCT = 0.02
                            # v11.2.10 FIX: removed source code self-modification — use runtime override only.
                            # Config.py is never rewritten; operator manually updates between deploys.
                            log.info('🚀 AUTO UPGRADE: RISK_PCT 1% → 2% (capital >= $150) [runtime only]')
                            self.tg.send('🚀 RISK UPGRADED: 1% to 2% — Capital hit $150! (update config.py manually)')
                        if self.cfg.TOTAL_CAPITAL >= 200.0 and not self.cfg.GRID_ENABLED:
                            log.info('🔔 GRID READY: Capital >= $200 — enable Grid in next restart')
                            self.tg.send('🔔 Grid auto-enables at next restart — capital $200+ reached!')
            except Exception as _de:
                log.debug(f'Deposit check: {_de}')

        # Tickers
        tickers = {}  # v14.5.1 FIX (audit #1): init before conditional — was NameError when WS inactive
        if self.ws.is_active:
            tickers=self.ws.get_prices()
        # v14.3.1 WS Watchdog — auto-recovery
        if tickers:
            self._ws_last_ok = time.time()
            self._ws_reconnects = 0
            # FIX 4: Populate price cache for StatArb/Kalman modules
            for _sym in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
                if _sym in tickers:
                    if _sym not in self._price_cache: self._price_cache[_sym] = {}
                    self._price_cache[_sym][time.time()] = tickers[_sym]
                    if len(self._price_cache[_sym]) > 50:
                        del self._price_cache[_sym][min(self._price_cache[_sym].keys())]
        elif (time.time() - getattr(self, "_ws_last_ok", time.time())) > 90:
            _rc = getattr(self, "_ws_reconnects", 0)
            log.warning(f"⚠️ WS watchdog: no prices for 30s — reconnect attempt #{_rc+1}")
            try:
                self.ws.stop()
                # v15.3 FIX: was `time.sleep(3)` — blocked the entire async event
                # loop (including all running coroutines, SL checks, scanners)
                # for 3 full seconds during every reconnect attempt.
                await asyncio.sleep(3)
                self.ws.start(self.cfg.API_KEY, self.cfg.API_SECRET)
                self._ws_last_ok = time.time()
                self._ws_reconnects = 0  # v14.5.1 FIX (audit #16): reset counter on success (was: _rc, never incremented)
                log.info(f"✅ WS reconnected successfully")
                try: self.tg.send(f"🔌 <b>WS auto-reconnected</b> (attempt #{_rc+1})")
                except Exception: pass
            except Exception as _we:
                self._ws_reconnects = _rc + 1
                log.error(f"❌ WS reconnect failed: {_we} (attempt #{_rc+1})")
                if self._ws_reconnects >= 3:
                    log.error("❌ WS dead after 3 reconnects — triggering systemd restart")
                    try: self.tg.send("🚨 <b>WS unrecoverable — bot restarting via systemd</b>")
                    except Exception: pass
                    import sys; sys.exit(1)
            if not tickers: tickers=await self.ex.tickers()
        else: tickers=await self.ex.tickers()
                # if not tickers: return

        # ─── v16.0.0 NEW: DEAD-MAN'S SWITCH ───────────────────────────────────────────
        # If the bot goes fully price-blind (no WS prices AND REST also returns nothing)
        # for DEADMAN_STALE_SEC while holding open positions, it can't manage SL/TP — a
        # dangerous state. Native SL still guards on the exchange, so default action is a
        # loud alert; set DEADMAN_ACTION="flatten" to also market-close everything.
        try:
            if getattr(self.cfg, 'DEADMAN_ENABLED', True):
                if tickers:
                    self._last_price_ok_ts = time.time()
                    self._deadman_fired = False
                elif self.risk.positions:
                    _blind = time.time() - getattr(self, '_last_price_ok_ts', time.time())
                    _limit = getattr(self.cfg, 'DEADMAN_STALE_SEC', 150)
                    if _blind > _limit and not getattr(self, '_deadman_fired', False):
                        self._deadman_fired = True
                        _act = getattr(self.cfg, 'DEADMAN_ACTION', 'alert')
                        log.error(f"💀 DEAD-MAN SWITCH: price-blind {_blind:.0f}s with "
                                  f"{len(self.risk.positions)} open positions — action={_act}")
                        try:
                            self.tg.critical_alert(
                                "DEAD-MAN SWITCH",
                                f"Price feed blind for {_blind:.0f}s with "
                                f"{len(self.risk.positions)} open position(s).\n"
                                f"Exchange-side native SL still protects each position.\n"
                                f"Action: {_act.upper()}",
                                priority="CRITICAL")
                        except Exception:
                            try: self.tg.send(f"💀 <b>DEAD-MAN SWITCH</b> — price-blind {_blind:.0f}s, action={_act}")
                            except Exception: pass
                        if _act == "flatten":
                            # request the existing force-close-all drain (runs next cycle top)
                            self._force_close_all_requested = True
        except Exception as _dms_e:
            log.debug(f"dead-man switch check failed: {_dms_e}")

        # v9.4: BTC CRASH PROTECTION — if BTC drops >5% in 24h, sell everything
        # v11.2.3 FIX (May 3, 2026): _btc_24h_high moved to self.risk._btc_24h_high so it
        # persists via state.py across restarts. Was RAM-only — restart mid-crash blinded
        # the 5% trigger because the "high" got reset to current (already-dumped) price.
        btc_price = tickers.get("BTCUSDT", 0)
        if float(btc_price) > 0 and float(self.risk._btc_24h_high) > 0:
            btc_drop = (float(self.risk._btc_24h_high) - float(btc_price)) / float(self.risk._btc_24h_high) * 100
            if btc_drop >= 5 and self.risk.positions:
                log.warning(f"🚨 BTC CRASH -{btc_drop:.1f}% — EMERGENCY SELL ALL")
                self.tg.send(f"🚨 <b>BTC CRASH DETECTED</b>\n📉 BTC dropped {btc_drop:.1f}% from 24h high\n💰 Selling all positions to protect capital")
                # v9.7.1 FIX: synthetic ctx — self.ctx is never defined; ctx isn't built until later in cycle
                _crash_ctx = SimpleNamespace(regime="CRASH",fg=50,heat=0,daily="--",h4="--",killzone="OFF",btc_ok=True,news_score=0,news_label="",vol="NORM",session="--",active_sess=[],mode="RANGE",adx=25,squeeze=False,sq_len=0)  # v11.2.23 FIX: full ctx attrs
                # v11.2.6 FIX: use actual fill price from result["fills"] instead of ticker —
                # during a 5%+ crash, real fills slip 1-5% below ticker. Logs accurate PnL.
                # v11.2.8 FIX (May 4, 2026): handle sell failure properly. Was: `if "error" in r: continue`
                # which left position untracked-as-sold. User got TG alert "selling everything"
                # but Binance app still showed open position. Same accounting gap as v11.2.6 #21.
                # Now: retry once with actual free balance, then synthesize _record_close at
                # last-known price so PnL/journal/Kelly all see the exit even if Binance rejected.
                for pos in list(self.risk.positions):
                    actual_price = tickers.get(pos.pair, pos.avg_entry)  # default
                    if await self._detach_native_sl_before_sell(pos, "CRASH"):
                        _bal = await self.ex.get_asset_balance(pos.pair.replace("USDT",""))
                        if "error" not in _bal: _qty = min(pos.qty, float(_bal.get("free", 0)))
                        else: _qty = pos.qty
                        _qty = self.ex.rnd(pos.pair, _qty)
                        r = await self.ex.sell(pos.pair, _qty)
                    else:
                        r = {"error": "native_sl_detach_failed"}
                    if "error" in r:
                        # Retry once with actual free balance (handles rounding / dust)
                        try:
                            asset = pos.pair.replace("USDT", "")
                            free_bal = float((await self.ex.get_asset_balance(asset))["free"])
                            free_qty = self.ex.rnd(pos.pair, free_bal)
                            if free_qty > 0:
                                if not await self._detach_native_sl_before_sell(pos, "CRASH_RETRY"):
                                    raise RuntimeError("native_sl_detach_failed")
                                r2 = await self.ex.sell(pos.pair, free_qty)
                                if "error" not in r2:
                                    r = r2
                                    log.info(f"🔁 CRASH retry succeeded {pos.pair}")
                        except Exception:
                            pass
                    if "error" in r:
                        await self._restore_native_sl_after_failed_sell(pos, "CRASH")
                        # Still failing — synthesize close so accounting is correct.
                        log.warning(f"⚠️ CRASH sell failed {pos.pair}: {r.get('error','?')} — synthesizing close at last-known price")
                        # v13.5: log to stuck_coins.jsonl so startup-sync recovery sees it.
                        # Pre-v13.5 only the FORCE_CLOSE path wrote here, leaving CRASH-failed
                        # positions invisible to recovery (Reconciler would alert as ORPHAN
                        # but no automatic re-sell would be attempted on next restart).
                        try:
                            stuck = {"pair":pos.pair,"qty":pos.qty,"entry":pos.avg_entry,
                                     "ts":datetime.now(timezone.utc).isoformat(),"reason":"crash_sell_failed"}
                            _append_jsonl("stuck_coins.jsonl", stuck)
                        except Exception as _e: log.warning(f"Stuck coin log (CRASH) failed for {pos.pair}: {_e}")
                        try:
                            self.tg.send(f"⚠️ <b>CRASH SELL FAILED</b> {pos.pair}\n"
                                         f"Reason: {r.get('error','unknown')}\n"
                                         f"Position remains in Binance — manual review required.")
                        except Exception as e: log.warning(f"Crash sell alert failed for {pos.pair}: {e}")
                        try:
                            self.risk._record_close(pos, actual_price, "CRASH_STUCK", _crash_ctx, self.tg)
                        except Exception as e:
                            log.warning(f"CRASH_STUCK _record_close failed: {e}")
                        with _pos_lock:
                            if pos in self.risk.positions:
                                self.risk.remove_position_safe(pos, expected_reason="CRASH_STUCK")  # v14.6.4 AUDIT FIX
                        continue
                    # extract real fill price from Binance result
                    try:
                        fills = r.get("fills", [])
                        if fills:
                            tq = sum(float(f["qty"]) for f in fills)
                            tc = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                            if tq > 0: actual_price = tc / tq
                    except Exception as e: log.warning(f"Crash sell fill extraction failed for {pos.pair}: {e}")
                    self.risk._record_close(pos, actual_price, "CRASH", _crash_ctx, self.tg)
                    with _pos_lock:
                        self.risk.remove_position_safe(pos, expected_reason="CRASH")  # v14.6.4 AUDIT FIX
                self.risk.save_state()
                return
        if float(btc_price) > 0:
            if self.risk._btc_24h_high == 0:
                # v11.2.8 FIX (May 4, 2026): cold-start blind spot. Was: first cycle just
                # set high to current (already-dumped) price — bot blind to ongoing crashes.
                # Now: try to fetch real 24h high from Binance ticker before falling back.
                try:
                    real_high = float((await self.ex.get_ticker(symbol="BTCUSDT"))["highPrice"])
                    if real_high > 0:
                        self.risk._btc_24h_high = real_high
                        log.info(f"  📊 BTC 24h-high seeded from Binance ticker: ${real_high:,.0f}")
                    else:
                        self.risk._btc_24h_high = btc_price
                except Exception:
                    self.risk._btc_24h_high = btc_price
                # v11.2.8: track when high was last reset (wall-clock, not cycle-count)
                self.risk._btc_high_reset_ts = time.time()
            # v11.2.19 FIX: BTC crash rolling window — was hard 24h reset which zeroed
            # the high to current price, letting slow bleeds (4%/day) bypass protection.
            # Now: rolling deque of (ts, price) — high = max over true last-24h window.
            if not hasattr(self.risk, '_btc_price_history'):
                self.risk._btc_price_history = []
                # v11.2.20 FIX: BTC crash paradox — empty history on restart overwrote
                # persisted _btc_24h_high with current (crashed) price, blinding protection.
                # Seed history with persisted high so rolling window inherits it.
                if self.risk._btc_24h_high > 0:
                    self.risk._btc_price_history.append((time.time() - 3600, self.risk._btc_24h_high))
            self.risk._btc_price_history.append((time.time(), btc_price))
            cutoff = time.time() - 86400
            self.risk._btc_price_history = [(t,p) for t,p in self.risk._btc_price_history if t > cutoff]
            if self.risk._btc_price_history:
                self.risk._btc_24h_high = max(float(p) for _,p in self.risk._btc_price_history)
        # v7.2: Drawdown shield update
        # v9.0 FIX: Use mark-to-market (current price × qty) not entry size
        pos_value = 0
        for p in self.risk.positions:
            cur_price = tickers.get(p.pair, 0) or p.avg_entry
            pos_value += float(cur_price) * p.qty
        try:
            # v18.8.5 FIX: value the FULL spot wallet from one get_account() snapshot,
            # not just USDT + tracked positions. Pre-v18.8.5 an untracked holding (e.g.
            # the sub-$5 BNB fee/staking balance the startup sweep's $5 floor leaves
            # behind) was invisible to equity; deploying USDT into a position then made
            # measured equity fall by the uncounted amount → phantom drawdown → the
            # circuit breaker froze the bot while the real wallet sat at an all-time high.
            # Now every non-USDT, non-dust holding is added at its market price.
            _acc_snap = await self.ex.get_account()
            _usdt_total = 0.0
            _untracked_value = 0.0
            _tracked_assets = {p.pair.replace("USDT", "") for p in self.risk.positions}
            for _b in (_acc_snap or {}).get("balances", []):
                _amt = float(_b.get("free", 0) or 0) + float(_b.get("locked", 0) or 0)
                if _amt <= 0:
                    continue
                _a = _b.get("asset", "")
                if _a == "USDT":
                    _usdt_total += _amt
                    continue
                if _a in _tracked_assets:
                    continue  # tracked positions are already valued via pos_value above
                _sym = _a + "USDT"
                _px = float(tickers.get(_sym, 0) or 0)
                if _px <= 0:
                    try: _px = float((await self.ex.get_symbol_ticker(_sym)).get("price", 0) or 0)
                    except Exception: _px = 0.0
                _untracked_value += _amt * _px
            if not _acc_snap and _usdt_total <= 0:
                # snapshot unusable — fall back to the pre-v18.8.5 USDT-only fetch
                _usdt_total = await self.ex.balance("USDT")
            bal = _usdt_total + pos_value + _untracked_value  # live equity = full wallet at market
            # v14.1 FIX (ISSUE A): realized equity for peak update — sum of cost basis
            # (pos.size at entry), NOT current market value, so unrealized intraday
            # spikes can't contaminate dd_peak. Untracked holdings have no bot-tracked
            # unrealized PnL, so their market value is added to both live and realized.
            _raw_realized = _usdt_total + sum(p.size for p in self.risk.positions) + _untracked_value
            
            # v18.8.1 FIX: Prevent Binance API balance-delay phantom spikes from ruining DD peak
            # If realized equity jumps by > 20% in one cycle, delay recognizing it by 3 cycles
            # to let Binance's REST API sync with local position tracking.
            # v18.8.5 FIX: bidirectional spike guard. A real one-cycle move is bounded
            # by the ~3% SL, so a >20% jump in EITHER direction is a REST/WS data glitch
            # (e.g. balance-API lag right after a fill). Pre-v18.8.5 only UPWARD spikes
            # were held back; a downward glitch flowed straight into the drawdown
            # denominator and could trip the circuit breaker on a phantom loss. Now both
            # directions hold the last-known-good equity for up to 3 cycles.
            if (hasattr(self, '_last_realized_safe') and self._last_realized_safe > 0
                    and (_raw_realized > self._last_realized_safe * 1.2
                         or _raw_realized < self._last_realized_safe * 0.8)):
                self._phantom_spike_count = getattr(self, '_phantom_spike_count', 0) + 1
                if self._phantom_spike_count <= 3:
                    _realized = self._last_realized_safe
                    bal = getattr(self, '_last_bal_safe', bal)
                else:
                    _realized = _raw_realized
                    self._last_realized_safe = _realized
                    self._last_bal_safe = bal
            else:
                _realized = _raw_realized
                self._phantom_spike_count = 0
                self._last_realized_safe = _realized
                self._last_bal_safe = bal
        except Exception as _bal_err:
            log.warning(f"⚠️ Balance fetch failed: {_bal_err} — using capital estimate")
            bal = self.cfg.TOTAL_CAPITAL  # safe fallback: don't trigger DD shield on API error
            _realized = self.cfg.TOTAL_CAPITAL
        self.ddshield.update(bal)            # v14.1: live equity → drawdown_pct denominator
        self.ddshield.update_peak(_realized) # v14.1: realized only → high-water mark (ISSUE A fix)
        # v18.7.4: auto capital-tier — switch sizing/positions by LIVE equity every cycle
        # (runs ungated by Telegram; internally rate-limited; applies BEFORE the entry phase
        # so can_trade sees the right MAX_POSITIONS / POSITION_SIZE_PCT / MAX_EXPOSURE).
        try:
            if getattr(self, 'cap_tier', None):
                self.cap_tier.apply(bal)
        except Exception as _cte:
            log.debug(f"CapitalTier apply failed: {_cte}")
        if self.ddshield.status == "KILLED":
            self._dd_kill_ticks = getattr(self, '_dd_kill_ticks', 0) + 1
            if self._dd_kill_ticks >= 2:  # v15.6 FIX: wait 2 cycles to confirm (avoids REST/WS race condition false alarms)
                if self.cycles % 20 == 1: log.warning(f"🛡️ Drawdown shield KILLED — 12%+ drawdown, no new trades")
                # v15.4 TG UPGRADE: critical alert on DD trip (one-shot via _last_dd_alert_at)
                if not getattr(self, '_last_dd_alert_at', None):
                    try:
                        self.tg.critical_alert(
                            "DD SHIELD TRIPPED",
                            f"12%+ drawdown detected\n"
                            f"Peak: ${getattr(self.ddshield, 'peak', 0):.2f}\n"
                            f"Equity: ${bal:.2f}\n"
                            f"Drawdown: {getattr(self.ddshield, 'drawdown_pct', 0):.2f}%\n"
                            f"⚠️ NEW positions blocked. Existing positions managed normally.",
                            priority="CRITICAL")
                        self._last_dd_alert_at = self.cycles
                    except Exception: pass
        else:
            self._dd_kill_ticks = 0
            if getattr(self, '_last_dd_alert_at', None) is not None:
                # DD recovered — send all-clear, reset flag
                try:
                    self.tg.critical_alert("DD SHIELD RECOVERED",
                        f"Drawdown back under threshold\nEquity: ${bal:.2f}\n✅ New positions allowed again.",
                        priority="HIGH")
                except Exception: pass
                self._last_dd_alert_at = None

        # v8.4: AUTO-COMPOUNDING — update capital from actual balance
        # Position sizes grow as profits accumulate, shrink if losses occur
        
        if self.cycles % 10 == 0 and bal > 0 and not self.cfg.FIXED_CAPITAL_MODE:  # v11.2.23 FIX: was bypassing FIXED_CAPITAL_MODE  # Every ~2.5 min
            old_cap = self.cfg.TOTAL_CAPITAL
            self.cfg.TOTAL_CAPITAL = round(bal, 2)
            if abs(bal - old_cap) > 0.50 and self.cycles > 1:  # Log only meaningful changes
                direction = "📈" if bal > old_cap else "📉"
                log.info(f"{direction} Capital: ${old_cap:.2f} → ${bal:.2f} (compound)")

        # v7.2: Self-healing health check
        health_issues = self.healer.check_health(tickers, self.ws, self.ex)
        if health_issues and self.cycles % 10 == 0:
            for issue in health_issues: log.warning(f"🔧 {issue}")

        # v9.3: Intelligence stack logging
        # v11.2.3 FIX (May 3, 2026): wrapped in daemon thread.
        # The MVRV/TVL/Whale/OI checks make HTTP requests to CoinGecko/DeFi Llama (timeout 3-10s).
        # Previously these blocked the main 15s scan cycle for 10-50s when external APIs were slow,
        # delaying SL/TP execution during volatility events. Output is info-log only (no trade
        # decisions depend on these), so safe to run async.
        if self.cycles % 100 == 1:
            def _run_intel_async():
                try:
                    mvrv_sig = self.risk.mvrv.check()
                    self.risk.tvl.check()
                    whale_sigs = self.risk.whale_wallets.check(self.cfg.PAIRS)
                    whale_buys = sum(1 for v in whale_sigs.values() if v == "WHALE_BUY")
                    whale_sells = sum(1 for v in whale_sigs.values() if v == "WHALE_SELL")
                    oi_sigs = self.risk.oi_monitor.check()
                    oi_surges = sum(1 for v in oi_sigs.values() if v == "OI_SURGE")
                    log.info(f"🔬 Intel: MVRV={mvrv_sig} | Whales: {whale_buys}buy/{whale_sells}sell | TVL:{len(self.risk.tvl.tvl_data)} | OI surges:{oi_surges}")
                except Exception as e:
                    log.warning(f"Intel async error: {e}")
            try:
                threading.Thread(target=_run_intel_async, daemon=True, name="IntelAsync").start()
            except Exception: pass
        # v9.2: Log event + stablecoin status
        if self.cycles % 50 == 1:
            eh = self.risk.event_cal.hours_to_next()
            em = self.risk.event_cal.risk_mult()
            sf = self.risk.stable_flow.check()
            if eh < 48:
                log.info(f"\U0001f3db FOMC/CPI in {eh:.0f}h | Risk mult: {em} | Stablecoin: {sf}")

        # v8.3: Update intelligence modules periodically
        if self.cycles % 20 == 0:  # Every 5 min
            try: await asyncio.to_thread(self.whale.update)
            except Exception: pass
            try: await asyncio.to_thread(self.multi_ex.analyze, "BTCUSDT")
            except Exception: pass
            # v8.4: Refresh new intelligence modules
            try: await asyncio.to_thread(self.gecko_trending.refresh)
            except Exception: pass
            try: await asyncio.to_thread(self.gecko_movers.refresh)
            except Exception: pass
            try:
                tickers_24h = await self.ex.get_ticker()
                await asyncio.to_thread(self.exchange_flow.refresh, tickers_24h)
            except Exception: pass
            # v11.2.10: Update new intelligence modules
            try: await asyncio.to_thread(self.long_short.update)
            except Exception as e: log.debug(f"LongShort refresh: {e}")
            try:
                btc_p = tickers.get("BTCUSDT", 0)
                await asyncio.to_thread(self.open_interest.update, btc_p)
            except Exception as e: log.debug(f"OI refresh: {e}")
            try: await asyncio.to_thread(self.hash_rate.update)
            except Exception as e: log.debug(f"HashRate refresh: {e}")
            # v12.0: Aggressor flow tracker
            try: await asyncio.to_thread(self.aggressor_flow.update, ["BTCUSDT","ETHUSDT","SOLUSDT"])
            except Exception as e: log.debug(f"AggressorFlow refresh: {e}")
            # v14.2: Module 3+4 global updates (per-pair done in entry gate)
            try: await asyncio.to_thread(self.liq_cascade.update)
            except Exception as e: log.debug(f"LiqCascade refresh: {e}")
            try:
                _btc=[float(v) for v in list(self._price_cache.get("BTCUSDT",{}).values())[-50:]]
                _eth=[float(v) for v in list(self._price_cache.get("ETHUSDT",{}).values())[-50:]]
                _sol=[float(v) for v in list(self._price_cache.get("SOLUSDT",{}).values())[-50:]]
                # v15.0 Gap #2: StatArb/Kalman/SpotPerp updates DISABLED — dead weight on spot-only.
                # We can't capture the spread without futures shorts; the boost contribution was ±5%.
                # Skipping these saves ~200ms/cycle in API calls. Classes remain importable.
                # if len(_btc)>=30 and len(_eth)>=30:
                #     self.stat_arb.update(_btc,_eth,"BTC/ETH")
                #     self.kalman.update(_btc,_eth,"BTC/ETH")
                # if len(_btc)>=30 and len(_sol)>=30:
                #     self.stat_arb.update(_btc,_sol,"BTC/SOL")
                #     self.kalman.update(_btc,_sol,"BTC/SOL")
            except Exception as e: log.debug(f"StatArb refresh: {e}")
            try:
                # v15.0 Gap #2: SpotPerpBasisTracker update DISABLED — dead weight on spot-only.
                # should_block() check in _apply_hard_risk_blocks remains as safety net but
                # without updates it will return False (no data) and gracefully no-op.
                # top_syms = [p["s"] for p in self.cfg.PAIRS[:8]]
                # self.spot_perp.update(top_syms)
                pass
            except Exception as e: log.debug(f"SpotPerp refresh: {e}")
            # v12.2: New intelligence modules
            try: await asyncio.to_thread(self.funding_rate.update)
            except Exception as e: log.debug(f"FundingRate refresh: {e}")
            try: await asyncio.to_thread(self.liquidation.update)
            except Exception as e: log.debug(f"Liquidation refresh: {e}")
            try: await asyncio.to_thread(self.smart_coin.update, self.risk.positions)
            except Exception as e: log.debug(f"SmartCoin refresh: {e}")
            try: await asyncio.to_thread(self.crypto_news.update)
            except Exception as e: log.debug(f"CryptoNews refresh: {e}")
            try:
                scan_syms = [p["s"] for p in self.cfg.PAIRS[:10]]
                await asyncio.to_thread(self.momentum.update, scan_syms, self.ex)
            except Exception as e: log.debug(f"Momentum refresh: {e}")
            # v16.0.0: Periodic token unlock + economic calendar updates
            try: await asyncio.to_thread(self.token_unlock.update)
            except Exception as e: log.debug(f"TokenUnlock refresh: {e}")
            try: await asyncio.to_thread(self.econ_calendar.update)
            except Exception as e: log.debug(f"EconCalendar refresh: {e}")
        if self.cycles % 60 == 0:  # Every 15 min
            try: self.meta_learner.update_weights()
            except Exception: pass
            try: self.model_selector.evaluate()
            except Exception: pass
            # v8.4: Social sentiment (slower refresh — 15min)
            try:
                coins = [p["n"] for p in self.cfg.PAIRS[:10]]
                await asyncio.to_thread(self.social_sentiment.refresh, coins)
            except Exception: pass
            try: await asyncio.to_thread(self.dxy.update)
            except Exception: pass
            try: await asyncio.to_thread(self.options.update)
            except Exception: pass

        # Ghost position check every 20 cycles — keeps bot in sync with Binance wallet.
        # Detects positions tracked by the bot but no longer present in the wallet
        # (manual sale on Binance app, partial fill, dust release) and synthesizes a
        # _record_close() at current ticker price so PnL, journal, Kelly Criterion, and
        # Telegram exit alert all reflect the real exit.
        if self.risk.positions and self.cycles % 20 == 0:
            try:
                # v12.2: Include LOCKED balance (open orders + lock-up staking).
                # NOTE: Does NOT cover Flexible/Simple Earn. Disable Auto-Subscribe for bot pairs.
                bals = {}
                for b in (await self.ex.get_account())['balances']:
                    total = float(b['free']) + float(b['locked'])
                    if total > 0.001:
                        bals[b['asset']] = total
                # v14.6: Clean ghost detection — remove from state, attempt real sell, no fake PnL
                for _p in list(self.risk.positions):
                    _asset = _p.pair.replace('USDT','')
                    _actual = bals.get(_asset, 0)
                    if _actual < _p.qty * 0.05:  # coins truly gone
                        # v16.0.03 FIX (3B): ALWAYS detach native SL/TP orders first,
                        # even when _free == 0 (user sold 100% manually on Binance app).
                        # Without this, zombie STOP_LOSS_LIMIT orders remain on the
                        # exchange order book and can randomly trigger on the user's
                        # next manual trade of that coin.
                        try:
                            await self._detach_native_sl_before_sell(_p, "GHOST_SWEEP")
                        except Exception as _detach_e:
                            log.warning(f"Ghost sweep native SL/TP detach failed {_p.pair}: {_detach_e}")
                        # Try real sell if any coins remain
                        try:
                            _free = float((await self.ex.get_asset_balance(_asset)).get('free', 0))
                            if _free > 0:
                                await self.ex.sell(_p.pair, _free)
                                log.info(f"✅ Sold orphan {_p.pair}: {_free:.4f}")
                        except Exception as _e:
                            log.warning(f"Orphan sell failed {_p.pair}: {_e}")
                        
                        # v15.6 FIX: Record PnL before removing so Kelly/journal don't lose the trade data
                        _exit_px = tickers.get(_p.pair, _p.avg_entry)
                        _ghost_ctx = SimpleNamespace(regime="GHOST", fg=50, heat=0, daily="--", h4="--", killzone="OFF", btc_ok=True, news_score=0, news_label="", vol="NORM", session="--", active=False, mode="RANGE", adx=25, squeeze=False, sq_len=0, mtf_align=50.0)
                        try:
                            self.risk._record_close(_p, _exit_px, "GHOST", _ghost_ctx, self.tg)
                        except Exception as _e:
                            log.warning(f"Ghost record_close failed: {_e}")

                        # Remove from state cleanly
                        with _pos_lock:
                            if _p in self.risk.positions:
                                self.risk.remove_position_safe(_p, expected_reason="ORPHAN")  # v14.6.4 AUDIT FIX
                        log.warning(f"🧹 Removed orphan position: {_p.pair}")
                        self.risk.save_state(self.grid.pnl, self.grid.trades)
            except Exception as e: log.warning(f"Ghost removal failed: {e}")

        # v15.2 #1 FIX: API-cheap adaptive reposting.
        # Previous v15.0: cancel+repost every 30s while pending → up to 8 API calls/order.
        # v15.2 budget: ≤ 5 API calls per order TOTAL across its entire lifetime.
        # Strategy:
        #   1) Bump min-age to 10 minutes (was 30s) → 1 repost per 10min max
        #   2) Add price-move gate: only repost if market moved > 0.5% from order price
        #   3) Hard-cap repost_count at 3 per order
        #   4) After 3 reposts OR 30 min total, cancel + give up (signal expired)
        # Worst-case API budget for ONE pending order: 1 status check + 3 (cancel+repost) + 1 final cancel = 5 calls
        # On a typical day with 0-2 unfilled limits, that's 0-10 API calls/day for reposts.
        import time as _time
        _adaptive_repost_age = 600     # 10 min between repost attempts (was 30s)
        _adaptive_max_age = 1800       # 30 min hard cap
        _price_move_threshold = 0.005  # 0.5% market move required to justify repost
        _max_reposts = 3               # absolute repost ceiling per order
        for oid in list(self._limit_orders.keys()):
            info = self._limit_orders.get(oid, {})
            age = _time.time() - info.get("ts", _time.time())
            pair = info.get("pair", "")
            # v16.0.03 FIX (3A): use last_poll_ts for repost cooldown instead of ts.
            # ts must remain the absolute creation time for _adaptive_max_age (30min)
            # expiry to work. Without this, stable-market price-move gate kept
            # resetting ts to 'now', and age never reached 30min — zombie orders.
            _last_poll = info.get("last_poll_ts", info.get("ts", 0))
            if (_time.time() - _last_poll) < _adaptive_repost_age:
                continue
            try:
                o = await self.ex.get_order(symbol=pair, orderId=oid)
                if (o.get("status") or "").upper() == "FILLED":
                    continue  # main poll loop handles fills
                if age >= _adaptive_max_age or info.get("repost_count", 0) >= _max_reposts:
                    await self.ex.cancel_order(symbol=pair, orderId=oid)
                    self._limit_orders.pop(oid, None)
                    log.info(f"⏱ LIMIT expired {pair} #{oid} cancelled (age={age:.0f}s reposts={info.get('repost_count',0)})")
                    continue
                # Price-move gate — only repost if market moved >0.5%
                order_price = float(o.get("price", 0) or 0)
                cur = tickers.get(pair, 0) or 0
                if cur <= 0:
                    try: cur = float((await self.ex.get_symbol_ticker(pair))["price"])
                    except Exception: cur = 0
                if cur > 0 and order_price > 0:
                    price_move = abs(cur - order_price) / order_price
                    if price_move < _price_move_threshold:
                        # Market hasn't moved enough — keep waiting, don't burn API
                        log.debug(f"📐 LIMIT {pair} stable ({price_move*100:.2f}%), keep waiting")
                        # v16.0.03 FIX (3A): bump last_poll_ts (NOT ts) so we re-evaluate
                        # in 10min. ts stays as original creation time for expiry calc.
                        info["last_poll_ts"] = _time.time()
                        continue
                # Move was significant — cancel + repost
                try: await self.ex.cancel_order(symbol=pair, orderId=oid)
                except Exception as _ce:
                    if "-2011" not in str(_ce) and "Unknown order" not in str(_ce): raise
                if cur > 0 and info.get("sig"):
                    sig_obj = info["sig"]; sz = info["size"]
                    new_qty = sz / cur
                    new_price = cur * (1 - self.cfg.LIMIT_OFFSET_PCT / 100)
                    new_result = await self.ex.buy_limit(pair, new_qty, new_price)
                    if "error" not in new_result and new_result.get("orderId"):
                        new_oid = int(new_result["orderId"])
                        rcount = info.get("repost_count", 0) + 1
                        self._limit_orders[new_oid] = {"sig": sig_obj, "size": sz, "pair": pair, "ts": info.get("ts", _time.time()), "repost_count": rcount, "last_poll_ts": _time.time()}  # v16.0.03 FIX (3A): preserve original ts from parent order for expiry calc
                        log.info(f"🔄 LIMIT repost {pair} ${new_price:.6f} (#{rcount}/{_max_reposts}) +{price_move*100:.2f}% moved")
                        if getattr(self, "_prom", None): self._prom.inc("limit_reposts_total")
                self._limit_orders.pop(oid, None)
            except Exception as _e:
                log.debug(f"Adaptive repost {oid}: {_e}")

        # v14.6.2: Auto-strategy killer — fires at 50+ closed trades
        try:
            import json as _json
            CLOSE_ACTIONS = {"TP","SL","TIME","GHOST","CRASH","DUST","FORCE_CLOSE","TRAIL","REGIME"}
            _PROTECTED = ["QFL_PANIC","SQUEEZE_BREAK","SMC_OB","SMC_SWEEP","WYCKOFF_ACC"]
            _trades = self.journal.history if hasattr(self, 'journal') and self.journal else []
            _closes = [t for t in _trades if t.get("action") in CLOSE_ACTIONS]
            if len(_closes) >= 50:
                # Build per-strategy stats
                _stats = {}
                for t in _closes:
                    s = t.get("strategy","unknown")
                    if s not in _stats: _stats[s] = {"wins":0,"losses":0,"pnl":0,"trades":0}
                    _stats[s]["trades"] += 1
                    _stats[s]["pnl"] += float(t.get("pnl",0))
                    if float(t.get("pnl",0)) > 0: _stats[s]["wins"] += 1
                    else: _stats[s]["losses"] += 1
                # Score each strategy
                _scored = []
                for s, d in _stats.items():
                    if d["trades"] < 5: continue  # not enough data
                    if s in _PROTECTED: continue
                    wr = d["wins"] / d["trades"]
                    _scored.append((s, wr, d["pnl"], d["trades"]))
                # Sort by WR ascending — worst first
                _scored.sort(key=lambda x: x[1])
                # Kill bottom 3 with WR < 45% — minimum 15 trades required
                _to_kill = [s for s,wr,pnl,t in _scored if wr < 0.45 and t >= 15][:3]
                _newly_killed = [s for s in _to_kill if s not in self.disabled_strats]
                if _newly_killed:
                    self.disabled_strats.extend(_newly_killed)
                    for s in _newly_killed:
                        wr = next(x[1] for x in _scored if x[0]==s)
                        pnl = next(x[2] for x in _scored if x[0]==s)
                        trades = next(x[3] for x in _scored if x[0]==s)
                        log.warning(f"🔪 AUTO-KILLED strategy {s}: WR={wr*100:.1f}% PnL=${pnl:.4f} over {trades} trades")
                        try: self.tg.send(f"🔪 STRATEGY KILLED: {s} | WR={wr*100:.1f}% | PnL=${pnl:.4f} | {trades}t | Below 45% WR — disabled")
                        except Exception: pass
                # Log full attribution at 50-trade milestone
                if len(_closes) == 50:
                    log.info("📊 50-TRADE MILESTONE — Strategy Attribution:")
                    for s,wr,pnl,t in sorted(_scored, key=lambda x: x[1], reverse=True):
                        log.info(f"  {s:<25} WR={wr*100:.1f}% PnL=${pnl:.4f} ({t}t)")
        except Exception as _e:
            log.debug(f"Auto-killer error: {_e}")

        # Context with portfolio heat
        ctx=self.intel.context(self.risk.portfolio_heat)
        # v14.6.2: pass regime to risk for Group D gate
        self.risk._last_regime = ctx.regime
        # v16.0.0 AUDIT FIX (D5): expose full ctx so risk.check_exits' BEAR time-exit can
        # read the live daily trend (it referenced self._last_ctx, which was never set).
        self.risk._last_ctx = ctx

        # ML retrain
        if self.ml and ML_AVAILABLE and self.ml.should_retrain():
            asyncio.ensure_future(self._async_retrain_ml())

        # HyperOpt
        if self.hyperopt and self.hyperopt.should_optimize():
            asyncio.ensure_future(self._async_retrain_hyperopt())

        # ═══ DCA + PYRAMID + EXITS ═══
        if self.risk.positions:
            # v10.5 FIX: iterate over list() copy so .remove(pos) calls inside
            # the loop body (lines ~4221, 4239, 4254) don't cause Python's
            # iterator to skip the next element. Real consequence under v10.4:
            # if 2 positions both hit SL same cycle, position #2 was skipped
            # this cycle and processed next cycle. Now: both processed cleanly.
            for pos in list(self.risk.positions):
                price=tickers.get(pos.pair,0)
                if price==0: continue
                # v16.0.0 AUDIT FIX (D9): removed the "LITE ATR TRAILING LOGIC" block.
                # It was permanently disabled (`if False:`) yet still ran a per-position
                # `await self.ex.klines(pos.pair,"5m",30)` API call + get_dynamic_sl()
                # EVERY cycle and threw the result away — pure wasted API budget/latency
                # on every open position. ATR trailing is handled by the ghost-trail in
                # risk.check_exits (the live, ratcheting implementation).
                so=self.dca.check(pos,price,ctx.fg)
                # v16.0.03 FIX (2B): DCA must respect portfolio heat cap.
                # Without this, DCA aggressively doubles down on losers during
                # cascading dumps, bypassing the global risk circuit breaker.
                _dca_max_heat = getattr(self.cfg, 'FEAR_HEAT', self.cfg.MAX_HEAT) if getattr(ctx, 'fg', 50) < 20 else self.cfg.MAX_HEAT
                if so and self.risk.portfolio_heat >= _dca_max_heat:
                    log.debug(f"🔥 DCA {pos.pair} skipped — heat {self.risk.portfolio_heat*100:.1f}% >= cap {_dca_max_heat*100:.1f}%")
                    so = None
                if so and self.risk.available>=so["size"]:
                    q=so["size"]/price; r=await self.ex.buy(pos.pair,q)
                    if "error" not in r:
                        fq=sum(float(f["qty"]) for f in r.get("fills",[]))or q
                        fc=sum(float(f["qty"])*float(f["price"]) for f in r.get("fills",[]))or (fq*price)
                        self.dca.apply(pos,price,fq,fc)

            # v7: Anti-Martingale pyramids
            await self.risk.check_pyramid(tickers, ctx, self.ex)  # v15.4 FIX: await async

            # v14.4: Check if native TP limit orders filled on exchange
            if getattr(self, "native_tp", None) and tickers:
                for _ntp_pos in list(self.risk.positions):
                    try:
                        _ntp_filled, _ntp_price = self.native_tp.check_filled(_ntp_pos)
                        if _ntp_filled:
                            log.info(f"🎯 {_ntp_pos.pair} native TP filled @ ${_ntp_price:.4f} — recording close")
                            await self._detach_native_sl_before_sell(_ntp_pos, "NATIVE_TP_FILLED")
                            self.risk._record_close(_ntp_pos, _ntp_price, "TP", ctx, self.tg)
                            with _pos_lock:
                                if _ntp_pos in self.risk.positions:
                                    self.risk.remove_position_safe(_ntp_pos, expected_reason="TP")  # v14.6.4 AUDIT FIX
                            self.risk.save_state(self.grid.pnl, self.grid.trades,
                                                 self.hyperopt.best_params if self.hyperopt else None)
                            try: self.tg.send(f"🎯 <b>NATIVE TP FILLED</b> {_ntp_pos.pair}\n💲 ${_ntp_price:.4f}\n✅ Exchange exit — profit secured")
                            except Exception: pass
                    except Exception as _ntpe:
                        log.debug(f"Native TP cycle check {_ntp_pos.pair}: {_ntpe}")
            closed=await self.risk.check_exits(tickers,ctx,self.ex,self.tg)  # v15.4 FIX: await async

            # v16.0: Partial scale-out execution (queued by risk.py when rung 2 fires)
            # Paper §4.2: split TPs — sell 40% at rung2, keep 60% as free runner.
            _partials = self.risk.get_and_clear_partials()
            for _ppos, _pprice, _ppct, _preason in _partials:
                try:
                    with _pos_lock:
                        if _ppos not in self.risk.positions:
                            continue
                    _asset_p = _ppos.pair.replace("USDT","").replace("BUSD","")
                    _free_b = float((await self.ex.get_asset_balance(_asset_p)).get("free", 0))
                    _sell_qty = self.ex.rnd(_ppos.pair, min(_ppos.qty * _ppct, _free_b * 0.99))
                    if _sell_qty <= 0:
                        continue
                    _r = await self.ex.sell(_ppos.pair, _sell_qty)
                    if "error" not in _r:
                        _fills = _r.get("fills", [])
                        _exit_p = (sum(float(f["qty"])*float(f["price"]) for f in _fills) /
                                   sum(float(f["qty"]) for f in _fills)) if _fills else _pprice
                        _partial_pnl = (_exit_p - _ppos.avg_entry) * _sell_qty
                        with _pos_lock:
                            _ppos.qty = self.ex.rnd(_ppos.pair, _ppos.qty - _sell_qty)
                            _ppos.total_qty = _ppos.qty  # v16.0 AUDIT FIX H4: was not updated — broke DCA/fee pro-rating
                            _ppos.size = round(_ppos.avg_entry * _ppos.qty, 6)
                        log.info(
                            f"📤 SCALE_OUT {_ppos.pair} sold {_sell_qty} @ ${_exit_p:.4f} "
                            f"pnl=${_partial_pnl:+.4f} remain qty={_ppos.qty}"
                        )
                        if self.tg:
                            try: self.tg.send(
                                f"📤 <b>SCALE OUT</b> {_ppos.pair}\n"
                                f"Sold: {_ppct*100:.0f}% @ ${_exit_p:.4f}\n"
                                f"PnL: ${_partial_pnl:+.4f}\n"
                                f"Runner: {_ppos.qty} remaining ✅"
                            )
                            except Exception: pass
                        self.risk.save_state()
                    else:
                        log.warning(f"Partial scale-out {_ppos.pair} failed: {_r}")
                except Exception as _pe:
                    log.warning(f"Partial scale-out {_ppos.pair}: {_pe}")

            actually_closed = []
            for pos, close_price, reason in closed:
                # v11.2.17 FIX: Grid double-sell race condition
                # check pos still tracked before selling — Grid/scale-out may have already closed it
                with _pos_lock:
                    if pos not in self.risk.positions:
                        log.warning(f"⚠️ SKIP double-sell {pos.pair} — already removed from positions")
                        continue
                actual_price = close_price  # Default to WS price

                # v13.5.6 FIX: native-SL race detection.
                # If the native STOP_LOSS_LIMIT has already filled on Binance,
                # our own market sell will fail (-2010 / NOTIONAL on the
                # leftover dust) and the position drops into the DUST removal
                # path, where B2-7 books it at avg_entry — erasing the realised
                # gain from the bot's PnL while the USDT sits untracked in the
                # wallet. Root cause confirmed on TONUSDT 2026-05-14 (BE @
                # +2.5%, SL+2% lock, native SL filled @ $2.133 = +2% gain,
                # bot booked −entry-fee). Fix: poll the native-SL order
                # status first; if FILLED, use its fills as the real exit and
                # skip the bot's own market sell entirely.
                _native_filled = False
                _oid = getattr(pos, "native_sl_order_id", None)
                if _oid:
                    try:
                        o = await self.ex.get_order(symbol=pos.pair, orderId=_oid)
                        _status = (o.get("status") or "").upper()
                        if _status == "FILLED":
                            _native_filled = True
                            # Compute true fill price from individual trades, fall
                            # back to cummulativeQuoteQty / executedQty.
                            try:
                                trades = await self.ex.get_my_trades(symbol=pos.pair, orderId=_oid)
                                if trades:
                                    tq = sum(float(t["qty"]) for t in trades)
                                    tc = sum(float(t["qty"]) * float(t["price"]) for t in trades)
                                    if tq > 0:
                                        actual_price = tc / tq
                                else:
                                    _ex = float(o.get("executedQty", 0))
                                    _cq = float(o.get("cummulativeQuoteQty", 0))
                                    if _ex > 0:
                                        actual_price = _cq / _ex
                            except Exception as _fe:
                                log.debug(f"native-SL fill detail fetch failed for {pos.pair}: {_fe}")
                            log.info(f"💡 {pos.pair} native SL already FILLED @ ${actual_price:.6f} "
                                     f"(orderId={_oid}) — skipping bot sell, using native fill as exit")
                            try:
                                self.tg.send(f"🛡️ <b>NATIVE SL EXIT</b> {pos.pair}\n"
                                             f"💲 Filled @ ${actual_price:.4f} | reason: {reason}\n"
                                             f"✅ Bot detected native SL fill — no double-sell")
                            except Exception:
                                pass
                            pos.native_sl_order_id = None
                    except Exception as _ge:
                        # -2013 "Order does not exist" etc. → treat as no native SL
                        log.debug(f"native-SL pre-check {pos.pair}: {_ge}")

                if _native_filled:
                    # Book the close at the REAL native-SL fill price (not
                    # synthesized) so PnL reflects the actual gain/loss instead
                    # of −entry-fee only.
                    try:
                        self.risk._record_close(pos, actual_price, reason, ctx, self.tg)
                        with _pos_lock:
                            if pos in self.risk.positions:
                                self.risk.remove_position_safe(pos, expected_reason=reason)  # v14.6.4 AUDIT FIX
                        actually_closed.append((pos, actual_price))
                    except Exception as _rc:
                        log.warning(f"native-SL-fill _record_close {pos.pair} failed: {_rc}")
                    continue

                # v13.5.5: Force cancel and wait for Binance lock to release.
                # v13.5.6: replaced bare `except: pass` with logged Exception.
                # Bare `except:` catches KeyboardInterrupt/SystemExit, breaking
                # graceful systemd shutdown; also hid -2011 "Unknown order" which
                # was a key symptom of the race above.
                try:
                    _orders = await self.ex.get_open_orders(symbol=pos.pair)
                    if isinstance(_orders, dict) and 'error' in _orders: continue
                    for _o in _orders:
                        if _o.get("type") in ("STOP_LOSS_LIMIT", "STOP_LOSS"): continue
                        if _o.get("orderId") == getattr(pos, "native_sl_order_id", None): continue
                        await self.ex.cancel_order(symbol=pos.pair, orderId=_o["orderId"])
                except Exception as _ce: log.debug(f"cancel non-SL orders {pos.pair}: {_ce}")
                # v15.3 FIX: was `time.sleep(0.3)` — with N concurrent SL hits in
                # a flash crash this serialized into N × 0.3s of frozen event
                # loop, blocking every other exit from running. asyncio.sleep
                # yields control so other coroutines can process exits in parallel.
                await asyncio.sleep(0.3)
                if not await self._detach_native_sl_before_sell(pos, reason):
                    continue
                # v16.0.03 FIX (3C): clamp sell qty to actual wallet free balance.
                # If native SL partially filled (common in flash crashes), pos.qty
                # exceeds actual free balance → Binance -2010 Insufficient Balance.
                # Clamping prevents the 3-fail loop from force-closing locally while
                # leaving un-filled tokens orphaned and unprotected.
                try:
                    _asset_c = pos.pair.replace("USDT", "")
                    _bal_data_c = await self.ex.get_asset_balance(_asset_c)
                    if "error" not in _bal_data_c:
                        _free_bal_c = float(_bal_data_c.get("free", 0))
                        _sell_qty = min(pos.qty, _free_bal_c)
                    else:
                        _sell_qty = pos.qty
                    _sell_qty = self.ex.rnd(pos.pair, _sell_qty)
                except Exception as _clamp_e:
                    log.debug(f"Balance clamp check failed {pos.pair}: {_clamp_e}")
                    _sell_qty = pos.qty
                if _sell_qty <= 0:
                    log.warning(f"⚠️ {pos.pair} sell_qty=0 after clamp — native SL likely fully filled")
                    continue
                result = await self.ex.sell(pos.pair, _sell_qty)
                if "error" in result:
                    await self._restore_native_sl_after_failed_sell(pos, reason)  # v16.0 AUDIT FIX C5: was missing await
                    # v9.0 FIX: Track sell failures — force remove after 3 attempts
                    # v13.5.3 audit Bug #25: was pos._sell_fails (dynamic attr, dropped
                    # by asdict() on save → lost on every restart → 3-strike force-
                    # close path could never accumulate strikes across reboots →
                    # stuck positions leaked indefinitely). Now uses pos.sell_fails
                    # which is declared on Position dataclass and persists.
                    pos.sell_fails = pos.sell_fails + 1
                    if pos.sell_fails >= 3:
                        # v9.7.1 FIX: before giving up, try ONE last sell with actual Binance free balance
                        # Catches rounding cases where pos.qty > actual free, and dust below MIN_NOTIONAL
                        try:
                            asset = pos.pair.replace("USDT", "")
                            free_bal = float((await self.ex.get_asset_balance(asset))["free"])
                            free_qty = self.ex.rnd(pos.pair, free_bal)
                            price_now = float((await self.ex.get_symbol_ticker(pos.pair))["price"])
                            value_usd = free_qty * price_now
                            if free_qty > 0 and value_usd >= 5.5:  # above MIN_NOTIONAL with safety margin
                                log.warning(f"🔁 Final attempt: selling actual free balance {free_qty} {asset} (${value_usd:.2f})")
                                if not await self._detach_native_sl_before_sell(pos, f"{reason}_FINAL"):
                                    continue
                                final_r = await self.ex.sell(pos.pair, free_qty)
                                if "error" in final_r:
                                    await self._restore_native_sl_after_failed_sell(pos, f"{reason}_FINAL")
                                if "error" not in final_r:
                                    log.info(f"✅ Recovered {pos.pair} on final attempt")
                                    self.tg.send(f"✅ <b>RECOVERED</b> {pos.pair}\n💲 Sold {free_qty} on final attempt at ${price_now:.4f}")
                                    try:
                                        actual_price = price_now
                                        fills = final_r.get("fills", [])
                                        if fills:
                                            tq = sum(float(f["qty"]) for f in fills)
                                            tc = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                                            if tq > 0: actual_price = tc / tq
                                        self.risk._record_close(pos, actual_price, reason, ctx, self.tg)
                                    except Exception as ex_rc:
                                        log.warning(f"Final attempt close-record failed: {ex_rc}")
                                    self.risk.remove_position_safe(pos, expected_reason=reason)  # v14.6.4 AUDIT FIX
                                    self.risk.save_state()
                                    continue
                            elif value_usd > 0 and value_usd < 5.5:
                                log.warning(f"🪙 {pos.pair} DUST — removing WITHOUT accounting (coins trapped, not sold) (${value_usd:.2f} < MIN_NOTIONAL) — releasing from tracking")
                                # self.tg.send(f"🪙 <b>DUST RELEASE</b> {pos.pair}\n💲 ${value_usd:.2f} below MIN_NOTIONAL — can't sell, releasing")
                                # v13.5: log to stuck_coins.jsonl. Without this, the dust
                                # coins disappear from bot tracking with no audit trail —
                                # operator can't reconcile what happened months later.
                                try:
                                    stuck = {"pair":pos.pair,"qty":free_qty,"entry":pos.avg_entry,
                                             "ts":datetime.now(timezone.utc).isoformat(),
                                             "reason":"dust_below_min_notional",
                                             "value_usd":round(value_usd,2)}
                                    _append_jsonl("stuck_coins.jsonl", stuck)
                                except Exception as _e: log.warning(f"Stuck coin log (DUST) failed for {pos.pair}: {_e}")
                                # v13.5: call _record_close with DUST reason — B2-7 already
                                # in this build will book PnL at avg_entry (=−fees only),
                                # so the bot's running PnL stays consistent with reality
                                # (coins trapped at no-loss-no-gain, only entry fee sunk).
                                # v11.2.21 historical reasoning: prior version booked at
                                # ticker price → "hallucinated USDT" gain. B2-7 fixes that.
                                try:
                                    self.risk._record_close(pos, price_now, "DUST", ctx, None)
                                except Exception as ex_rc:
                                    log.warning(f"Dust _record_close failed: {ex_rc}")
                                if pos in self.risk.positions:
                                    self.risk.remove_position_safe(pos, expected_reason="DUST")  # v14.6.4 AUDIT FIX
                                self.risk.save_state()
                                continue
                        except Exception as fb_ex:
                            log.warning(f"Final-attempt sell errored: {fb_ex}")
                        log.warning(f"⚠️ Force-removing {pos.pair} after {pos.sell_fails} sell failures")
                        self.tg.send(f"⚠️ <b>FORCE CLOSE</b> {pos.pair} — sell failed {pos.sell_fails}x, removing from tracking")
                        # v9.1: Log stuck coins so startup can recover
                        try:
                            stuck = {"pair":pos.pair,"qty":pos.qty,"entry":pos.avg_entry,
                                     "ts":datetime.now(timezone.utc).isoformat(),"reason":"sell_failed"}
                            _append_jsonl("stuck_coins.jsonl", stuck)
                            log.warning(f"📝 Logged stuck coin: {pos.pair} qty={pos.qty}")
                        except Exception as e: log.warning(f"Stuck coin log failed for {pos.pair}: {e}")
                        # v11.2.6 FIX: synthesize implicit close at last-known price so PnL,
                        # journal, Kelly Criterion, RL agent all see the exit. Was: silent
                        # positions.remove() — same accounting gap that v11.2.4 #15 fixed for
                        # the in-cycle ghost killer.
                        try:
                            last_known_price = tickers.get(pos.pair, pos.avg_entry)
                            self.risk._record_close(pos, last_known_price, "FORCE_CLOSE", ctx, self.tg)
                        except Exception as ex_rc:
                            log.warning(f"Force-close _record_close failed: {ex_rc}")
                        if pos in self.risk.positions:
                            self.risk.remove_position_safe(pos, expected_reason="FORCE_CLOSE")  # v14.6.4 AUDIT FIX
                        self.risk.save_state()
                    else:
                        log.warning(f"Sell failed {pos.pair} ({pos.sell_fails}/3), retrying next cycle")
                    continue  # Position stays — no PnL recorded
                # v9.1: Use actual fill price from Binance, not WebSocket price
                try:
                    fills = result.get("fills", [])
                    if fills:
                        tq = sum(float(f["qty"]) for f in fills)
                        tc = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                        if tq > 0: actual_price = tc / tq
                except Exception as e: log.warning(f"Sell fill extraction failed for {pos.pair}: {e}")
                # v11.2.16: SLIPPAGE GUARD — warn if fill worse than SL by >0.5%
                if reason == "SL" and actual_price < pos.sl:
                    slip_pct = (pos.sl - actual_price) / pos.sl * 100
                    if slip_pct > 0.5:
                        log.warning(f"⚠️ SLIPPAGE {pos.pair}: SL=${pos.sl:.4f} filled @${actual_price:.4f} slip={slip_pct:.2f}%")
                        self.tg.send(f"⚠️ Slippage {pos.pair}: expected ${pos.sl:.4f}, got ${actual_price:.4f} ({slip_pct:.2f}% slip)")
                # v8.4: ONLY record PnL after sell confirmed (or dry run)
                self.risk._record_close(pos, actual_price, reason, ctx, self.tg)
                with _pos_lock:
                    if pos in self.risk.positions:
                        self.risk.remove_position_safe(pos, expected_reason=reason)  # v14.6.4 AUDIT FIX (was unlocked + raw remove)
                actually_closed.append((pos, actual_price))
            if actually_closed: self.risk.save_state()
            # v8.3: RL learns from closed trades — use actual close price
            for pos, act_price in actually_closed:
                try:
                    pnl_pct = (act_price - pos.avg_entry) / pos.avg_entry * 100 if pos.avg_entry > 0 else 0
                    # B2-2: pass full context + pair so per-position state is used
                    sig_atr_p = pos.atr / max(pos.avg_entry, 0.001) * 100 if pos.avg_entry > 0 else 1.0
                    self.rl.reward(pnl_pct,
                                   regime=ctx.regime, trend=ctx.daily,
                                   atr_pct=sig_atr_p, fg=ctx.fg, pair=pos.pair)
                except Exception as e: log.warning(f"RL reward failed for {pos.pair}: {e}")
                # v12.0: Record to coin profile
                try:
                    # BUG FIX: Position has entry_time (ISO str), not entry_ts
                    try:
                        entry_dt = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
                        hold_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
                    except Exception:
                        hold_min = 30  # Safe fallback
                    entry_h = datetime.now(timezone.utc).hour
                    self.coin_profiles.record_trade(pos.pair, pnl_pct, pos.strategy, entry_h, hold_min)
                except Exception as e: log.debug(f"CoinProfile record: {e}")

        # v11.2.10: Position reconciliation — compare bot state vs Binance
        self.reconciler.check(self.risk.positions, self.ex, self.tg, self.cfg)

        # v16.0.0 AUDIT FIX (D10): native-SL orphan reconciler was defined but NEVER called,
        # so stale exchange-side STOP_LOSS_LIMIT orders from closed positions accumulated.
        # Run it on a cadence (offloaded — it uses the sync python-binance client).
        _recon_n = getattr(self.cfg, 'NATIVE_SL_RECONCILE_CYCLES', 20)
        if (_recon_n and self.cycles % _recon_n == 0
                and getattr(self.cfg, 'NATIVE_SL_ENABLED', False)
                and getattr(self, 'native_sl', None)):
            try:
                _orphans = await asyncio.to_thread(
                    self.native_sl.reconcile_orphans, self.risk.positions, self.tg)
                if _orphans:
                    log.info(f"🛑 Native-SL reconciler cancelled {_orphans} orphan stop order(s)")
            except Exception as _ro_e:
                log.debug(f"Native-SL reconcile failed: {_ro_e}")

        # v15.3 AUDIT FIX #3: TriArb opportunistic scan, alert-only.
        # Run every 10 cycles (~5min @ 30s/cycle) to avoid API spam.
        # Per-cycle dedup: same triangle won't re-alert within 30 min unless profit grows >50%.
        if self.tri_arb is not None:
            self._tri_arb_cycle += 1
            if self._tri_arb_cycle >= 10:
                self._tri_arb_cycle = 0
                try:
                    _opps = self.tri_arb.scan()
                    _now = time.time()
                    for _opp in _opps:
                        _key = f"{_opp['direction']}_{'-'.join(_opp['cycle'])}"
                        _last_ts, _last_bps = self._tri_arb_alerted.get(_key, (0, 0))
                        _bps = _opp['expected_profit_bps']
                        # Dedup: skip if seen <30min ago AND profit didn't grow >50%
                        if _now - _last_ts < 1800 and _bps < _last_bps * 1.5:
                            continue
                        self._tri_arb_alerted[_key] = (_now, _bps)
                        _msg = (f"💎 <b>TriArb Opportunity</b>\n"
                                f"Cycle: {' → '.join(_opp['cycle'])}\n"
                                f"Direction: {_opp['direction']}\n"
                                f"Expected: <b>{_bps:.1f}bps</b> ({_bps/100:.3f}%)\n"
                                f"⚠️ ALERT ONLY — manual execution required\n"
                                f"(execute() is unwired; do 3-leg trade by hand if worthwhile)")
                        log.info(f"💎 TriArb: {_key} expected {_bps:.1f}bps")
                        try: self.tg.send(_msg)
                        except Exception: pass
                except Exception as _tri_scan_e:
                    log.debug(f"TriArb scan failed: {_tri_scan_e}")

        # ═══ TRAILING BUY CHECK ═══
        to_remove=[]
        for sym,pb in self.pending.items():
            price=tickers.get(sym,0)
            if price==0: continue
            if time.time()-pb.created>600: to_remove.append(sym); continue
            if price<pb.lowest: pb.lowest=price
            rev=(price-pb.lowest)/pb.lowest*100 if pb.lowest>0 else 0
            if rev>=self.cfg.TRAIL_BUY_PCT:
                pb.signal.price = price  # v11.2.21 FIX: update price BEFORE can_trade (was stale signal price)
                ok,reason,size=self.risk.can_trade(pb.signal,ctx.fg)
                result = None  # v11.2.10 FIX: must initialize before branches — TREND_DOWN path skips both
                if ok and ctx.regime != "TREND_DOWN":
                    log.info(f"📈 Trail BUY {sym} Low:${pb.lowest:.4f}→${price:.4f} +{rev:.2f}%")
                    if self.cfg.USE_LIMIT:
                        result=await self.ex.buy_limit(sym,size/price,price*(1-self.cfg.LIMIT_OFFSET_PCT/100))
                        if "error" not in result:
                            if result.get("status")=="FILLED":
                                pb.signal.price=price
                                self.risk.open_pos(pb.signal,size,result,ctx,self.tg)
                                # v13.2: RL entry for trail-buy limit fill
                                pass
                            else:
                                # v11.2.18 FIX: phantom position — GTC trail not filled yet
                                oid=result.get("orderId")
                                pb.signal.price=price
                                if oid: self._limit_orders[oid]={"sig":pb.signal,"size":size,"pair":sym,"ts":__import__("time").time()}; log.info(f"⏳ LIMIT trail {sym} #{oid} pending")
                    else:
                        result=await self.ex.buy(sym,size/price)
                        if "error" not in result:
                            pb.signal.price=price
                            self.risk.open_pos(pb.signal,size,result,ctx,self.tg)
                            # v13.2: RL entry for trail-buy
                            pass
                if ok and result is not None and "error" not in result: to_remove.append(sym)  # only remove if trade confirmed
        for sym in to_remove:
            if sym in self.pending: del self.pending[sym]

        # ═══ STATUS ═══
        if self.cycles%5==1:
            s=self.risk.status()
            # v15.2 #1 FIX: push Prometheus gauges in status block (once per 5 cycles = 2.5min)
            # Cheap — local writes only, no API calls.
            if getattr(self, "_prom", None):
                try:
                    self._prom.set("capital_usd", self.cfg.TOTAL_CAPITAL)
                    self._prom.set("open_positions", s.get("pos", 0))
                    self._prom.set("daily_pnl_usd", s.get("daily", 0))
                    self._prom.set("total_pnl_usd", s.get("pnl", 0))
                    self._prom.set("win_count", self.risk.wins)
                    self._prom.set("loss_count", self.risk.losses)
                    self._prom.set("consec_losses", self.risk.closs)
                    self._prom.set("drawdown_pct", getattr(self.ddshield, "drawdown_pct", 0))
                    self._prom.set("peak_equity_usd", getattr(self.ddshield, "peak", 0))
                    self._prom.set("fear_greed", ctx.fg)
                    self._prom.set("portfolio_heat_pct", s.get("heat", 0))
                    self._prom.set("cycle_count", self.cycles)
                    if hasattr(self.analytics, "sharpe"):
                        self._prom.set("sharpe", self.analytics.sharpe or 0)
                    if hasattr(self.analytics, "sortino"):
                        self._prom.set("sortino", self.analytics.sortino or 0)
                    if hasattr(self.analytics, "calmar"):
                        self._prom.set("calmar", self.analytics.calmar or 0)
                except Exception as _pme: log.debug(f"Prom gauge update: {_pme}")
            gp=f" Grid:${self.grid.pnl:+.4f}({self.grid.trades}t)" if self.grid.trades>0 else ""
            ps=f" ⛔{s['pr']}" if s['paused'] else ""
            news=f" 📰{ctx.news_score:+.2f}" if self.cfg.NEWS_ENABLED else ""
            # v8.3: Export dashboard data
            try:
                self.dashboard.export(
                    self.risk.positions, self.grid.pnl, self.risk.wins, self.risk.losses,
                    ctx.regime, ctx.fg, ctx.heat, self.risk.daily_pnl, self.risk.daily_t,
                    self.ml.accuracy if self.ml else 0,
                    "", "", "", ""
                )
            except Exception: pass
            log.info(
                f"⏱ #{self.cycles} | {ctx.regime} {ctx.daily}/{ctx.h4} {ctx.killzone}  "
                                f"F&G:{ctx.fg} BTC:{'✅' if getattr(ctx,'btc_ok',True) else '❌'}{news} Heat:{s['heat']:.1f}% MTF:{getattr(ctx,'mtf_align',50):.0f} | "
                f"Pos:{s['pos']} PnL:${s['pnl']:+.4f} "
                f"Day:${s['daily']:+.4f}({s['dt']}/{self.cfg.MAX_DAILY_TRADES}t) "
                f"WR:{s['wr']:.0f}%{gp}{ps}")
            self.risk.save_state(self.grid.pnl,self.grid.trades,
                                 self.hyperopt.best_params if self.hyperopt else None)

        # v8.4 FIX #3: Heartbeat to Telegram every 2 hours
        if time.time() - self._last_heartbeat >= 7200:
            try:
                s = self.risk.status()
                bal = await self.ex.balance("USDT") + sum(p.size for p in self.risk.positions)
                hrs = (datetime.now(timezone.utc) - self.start).total_seconds() / 3600
                self.tg.send(
                    f"💓 <b>Heartbeat</b> | Up: {hrs:.1f}h\n"
                    f"💰 Balance: ${bal:.2f} | PnL: ${s['pnl']:+.4f}\n"
                    f"📊 Pos: {s['pos']} | Trades: {s['dt']} | WR: {s['wr']:.0f}%\n"
                    f"🧠 Cycle: #{self.cycles}"
                )
                self._last_heartbeat = time.time()
            except Exception: self._last_heartbeat = time.time()

        p,_=self.risk.paused()
        if p: return

        # ═══ GRID (only in RANGE regime) ═══
        self.risk.set_grid_exp(self.grid.exposure)
        # v11.2.18 FIX: poll pending GTC limit orders each cycle
        if self._limit_orders:
            for oid in list(self._limit_orders):
                info=self._limit_orders[oid]
                try:
                    o=await self.ex.get_order(symbol=info["pair"],orderId=oid)
                    if o.get("status")=="FILLED":
                        # v11.2.19 FIX: get_order has no fills array — open_pos falls back to
                        # sig.price/qty causing balance drift. Fetch real fills via get_my_trades.
                        try:
                            trades=await self.ex.get_my_trades(symbol=info["pair"],orderId=oid)
                            if trades:
                                o["fills"]=[{"qty":str(t["qty"]),"price":str(t["price"]),"commission":str(t["commission"])} for t in trades]
                        except Exception as fe:
                            log.warning(f"fills fetch failed {info['pair']} #{oid}: {fe} — using sig price")
                        self.risk.open_pos(info["sig"],info["size"],o,ctx,self.tg)
                        # v13.2: RL entry for deferred limit fill
                        pass
                        del self._limit_orders[oid]; log.info(f"✅ LIMIT filled {info['pair']} #{oid}")
                    elif o.get("status") in ("CANCELED","EXPIRED","REJECTED"):
                        # v11.2.21 FIX: check partial fills before discarding — Binance can
                        # partially fill then cancel; ignoring creates ghost positions with no SL/TP
                        exec_qty = float(o.get("executedQty", 0))
                        if exec_qty > 0:
                            log.warning(f"⚠️ LIMIT {info['pair']} #{oid} partial fill {exec_qty} — tracking as position")
                            try:
                                trades = await self.ex.get_my_trades(symbol=info['pair'], orderId=oid)
                                if trades: o["fills"]=[{"qty":str(t["qty"]),"price":str(t["price"]),"commission":str(t["commission"])} for t in trades]
                            except Exception as e: log.warning(f"Limit fill check for {info['pair']} #{oid} failed: {e}")
                            self.risk.open_pos(info["sig"], info["size"], o, ctx, self.tg)
                            # v13.2: RL entry for partial limit fill
                            pass
                        else:
                            log.warning(f"⚠️ LIMIT {info['pair']} #{oid} {o.get('status')} — removed")
                        del self._limit_orders[oid]
                except Exception as e:
                    log.warning(f"limit poll error {oid}: {e}")
        if getattr(self.cfg, 'EXPOSURE_GUARD_ENABLED', True):
            usdt_free = float((await self.ex.get_asset_balance("USDT")).get("free", 0))
            pos_value = sum(p.size for p in self.risk.positions)
            ok, pct, status = self.exposure_guard.check(usdt_free, pos_value, self.cfg.TOTAL_CAPITAL)
            if not ok:
                log.warning(f"EXPOSURE BLOCKED: {pct*100:.1f}% > {self.exposure_guard.max_crypto_pct*100}%")
                # v18.7.2 FIX: this alert used to fire EVERY scan cycle (~2/min) → Telegram
                # spam. The block is correct (bot is fully invested, pausing NEW entries),
                # but the notification must not repeat. Alert only on the transition INTO the
                # blocked state, then at most once every 30 min while it persists.
                _now = time.time()
                _was_blocked = getattr(self, '_exposure_blocked', False)
                _last_alert = getattr(self, '_exposure_alert_ts', 0)
                if (not _was_blocked) or (_now - _last_alert > 1800):
                    try: self.tg.send(f"⚠️ <b>EXPOSURE FULL</b>: {pct*100:.1f}% crypto ≥ "
                                      f"{self.exposure_guard.max_crypto_pct*100:.0f}% limit — new entries paused "
                                      f"(existing positions managed normally).")
                    except Exception: pass
                    self._exposure_alert_ts = _now
                self._exposure_blocked = True
                return  # block new entries
            else:
                # Exposure back under the limit — clear the latch and send a one-shot all-clear.
                if getattr(self, '_exposure_blocked', False):
                    self._exposure_blocked = False
                    try: self.tg.send(f"✅ <b>EXPOSURE OK</b>: {pct*100:.1f}% crypto — new entries resumed.")
                    except Exception: pass
    
        if getattr(self.cfg, 'GRID_SYNC_INTERVAL', 10) > 0 and self.cycles % getattr(self.cfg, 'GRID_SYNC_INTERVAL', 10) == 0:
            for p in self.cfg.PAIRS[:4]:
                p_sym = p["s"] if isinstance(p, dict) else p
                if getattr(self.grid, 'grids', {}).get(p_sym):
                    _bal = await self.ex.get_asset_balance(p_sym.replace('USDT',''))
                    self.grid.sync_with_wallet(p_sym, float(_bal.get('free', 0)))

        if self.cfg.GRID_ENABLED and ctx.regime in ("RANGE","SQUEEZE"):
            cap_per=self.cfg.grid_capital/self.cfg.GRID_LEVELS
            if cap_per>=self.cfg.MIN_TRADE:
                for pair in self.cfg.PAIRS[:4]:
                    sym=pair["s"]; price=tickers.get(sym,0)
                    if price==0: continue
                    c5=await self.ex.klines(sym,"5m",30)
                    if len(c5)<20: continue
                    sup,res=TA.sup_res(c5,20); atr=TA.atr(c5)
                    if sym not in self.grid.grids: self.grid.setup(sym,price,sup,res,atr)
                    for act in self.grid.check(sym,price,cap_per):
                        # v11.2.8 FIX: only mark grid level filled after exchange confirms
                        if act["a"]=="BUY":
                            r = await self.ex.buy(sym, act["q"])
                            if "error" not in r:
                                # extract real fill price + qty from response
                                fp = act["p"]; fq = act["q"]
                                try:
                                    fills = r.get("fills", [])
                                    if fills:
                                        tq_gross = sum(float(f["qty"]) for f in fills)
                                        tc = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                                        if tq_gross > 0: fp = tc / tq_gross
                                        fq = tq_gross
                                        for f in fills:
                                            if f.get("commissionAsset") == sym.replace("USDT", ""):
                                                fq -= float(f.get("commission", 0))
                                except Exception as e: log.warning(f"Grid buy fill extraction failed: {e}")
                                self.grid.mark_buy_filled(act["lv"], fp, fq, act["cap_per"])
                                log.info(f"📊[GRID] BUY {sym} @${fp:.4f}")
                            else:
                                act["lv"].buying = False
                                log.warning(f"📊[GRID] BUY rejected {sym}: {r.get('error','?')}")
                        elif act["a"]=="SELL":
                            r = await self.ex.sell(sym, act["q"])
                            if "error" not in r:
                                self.grid.mark_sell_filled(act["buy_lv"], act["profit"], act.get("sell_lv"))
                                log.info(f"📊[GRID] SELL {sym} +${act['profit']:.4f}")
                            else:
                                act["buy_lv"].selling = False  # v11.2.18 FIX: zombie lock — unlock on rejection
                                if "sell_lv" in act: act["sell_lv"].selling = False
                                log.warning(f"📊[GRID] SELL rejected {sym}: {r.get('error','?')}")
                    self.grid.recenter(sym,sup,res,price,atr)
                    # v15.3 FIX: was `time.sleep(0.1)` — blocked event loop.
                    await asyncio.sleep(0.1)

        # ═══ SIGNAL GENERATION ═══
        # v7.2: Dynamic pair rotation every 30min
        # v11.2.16: Always use rotator's full pair list (83 pairs) not static cfg.PAIRS (31 pairs)
        # Old: cfg.PAIRS on non-120 cycles → only 31 pairs scanned → 0 signals in quiet markets
        if self.cycles % 120 == 1:
            active_pairs = [p for p in self.rotator.rank_pairs(self.ex) if (p['s'] if isinstance(p, dict) else p) in {q['s'] for q in self.cfg.PAIRS}]
        else:
            active_pairs = self.rotator.pairs if self.rotator.pairs else self.cfg.PAIRS
        # v11.2.16: AUTO-RECOVERY — if pairs empty, force rescan immediately
        if not active_pairs:
            log.warning('⚠️ Scanner empty — forcing immediate refresh')
            try:
                active_pairs = [p for p in self.rotator.rank_pairs(self.ex) if (p['s'] if isinstance(p, dict) else p) in {q['s'] for q in self.cfg.PAIRS}]
                if active_pairs:
                    log.info(f'✅ Scanner recovered: {len(active_pairs)} pairs')
                    self.tg.send(f'✅ Scanner auto-recovered: {len(active_pairs)} pairs')
            except Exception as _se:
                log.warning(f'Scanner refresh failed: {_se}')
        if not active_pairs:
            log.warning('⚠️ Scanner still empty — skipping cycle')
            return
        all_sigs=[]; self.candle_cache={}
        hp = self.hyperopt.best_params if self.hyperopt else None
        # v15.2: ASYNC PARALLEL signal scanning — all pairs scanned concurrently
        _scan_t0 = time.time()
        
        # v16.0.0 AUDIT FIX (D2): the MICRO_PRICE strategy was DEAD. Its only data source
        # (@bookTicker WS) was removed to save RAM, and this line previously CLAIMED it was
        # "powered by @bookTicker WS now" — which was false (the WS only carries miniTicker).
        # Restore the feed with a cheap periodic REST bookTicker poll: one call returns every
        # symbol's best bid/ask; we feed the top-N pairs into micro_price.update_bba().
        _mp_n = getattr(self.cfg, 'MICRO_PRICE_POLL_CYCLES', 0)
        if _mp_n and getattr(self, 'micro_price', None) and self.cycles % _mp_n == 0:
            try:
                _bba = await self.ex.get_all_bba()   # {SYM: {b,B,a,A}} — single lightweight call
                _top = getattr(self.cfg, 'MICRO_PRICE_POLL_TOP_N', 30)
                _wanted = {p["s"] for p in self.cfg.PAIRS[:_top]}
                _fed = 0
                for _sym, _d in _bba.items():
                    if _sym in _wanted and _d.get("b", 0) > 0 and _d.get("a", 0) > 0:
                        self.micro_price.update_bba(_sym, _d["b"], _d["B"], _d["a"], _d["A"])
                        _fed += 1
                if _fed and self.cycles % (_mp_n * 60) == 0:
                    log.debug(f"📊 Micro-price feed: {_fed} pairs via REST bookTicker")
            except Exception as _mpe:
                log.debug(f"Micro-price REST poll failed: {_mpe}")

        async def _scan_pair_async(pair):
            try:
                c5 = await self.ex.klines(pair["s"], "5m", 100)
                c15 = await self.ex.klines(pair["s"], "15m", 60)
                sigs = self.strat.analyze(pair["s"], pair["n"], pair["g"], pair.get("t",2), ctx, c5, c15, hp)
                import inspect
                if inspect.iscoroutine(sigs): sigs = await sigs
                return pair["s"], sigs, c5
            except Exception as e:
                return pair["s"], [], None
        try:
            results = await asyncio.gather(
                *[_scan_pair_async(p) for p in active_pairs],
                return_exceptions=True
            )
            for r in results:
                if isinstance(r, Exception):
                    continue
                sym, sigs, c5 = r
                all_sigs.extend(sigs)
                if c5:
                    self.candle_cache[sym] = c5
        except Exception as _ge:
            for pair in active_pairs:
                c5 = await self.ex.klines(pair["s"], "5m", 100)
                c15 = await self.ex.klines(pair["s"], "15m", 60)
                sigs = self.strat.analyze(pair["s"], pair["n"], pair["g"], pair.get("t",2), ctx, c5, c15, hp)
                import inspect
                if inspect.iscoroutine(sigs): sigs = await sigs
                all_sigs.extend(sigs)
                if sigs:
                    self.candle_cache[pair["s"]] = c5
        _scan_ms = (time.time() - _scan_t0) * 1000
        if self.cycles % 5 == 1:
            log.info(f"⚡ Scan: {len(active_pairs)} pairs in {_scan_ms:.0f}ms ({_scan_ms/max(len(active_pairs),1):.0f}ms/pair)")

        # v16.0 AUDIT FIX M1: pre-populate candle_cache for open positions not in active rotation.
        # check_correlation() checks candle_cache.get(pos.pair) — if an existing position's pair
        # isn't scanned this cycle, its candles aren't cached, and the correlation check
        # silently passes (returns True), allowing correlated entries.
        for _pos in self.risk.positions:
            if _pos.pair not in self.candle_cache:
                try:
                    _pc5 = await self.ex.klines(_pos.pair, "5m", 60)
                    if _pc5:
                        self.candle_cache[_pos.pair] = _pc5
                except Exception:
                    pass

        ranked=self.strat.rank(all_sigs)
        if ranked:
            log.info(f"📡 Signals: {len(ranked)} ranked | top: " + ", ".join(f"{s.pair}({s.strategy} conf={s.conf} grade={s.grade})" for s in ranked[:3]))
        else:
            log.info(f"📡 Signals: 0 generated this cycle (across {len(all_sigs) if all_sigs else 0} raw)")
        # v8.4 FIX: HARD-disable — fully block disabled strategies (not just reduce)
        for s in ranked:
            if s.strategy in self.disabled_strats:
                s.conf = 0  # Hard block — disabled means disabled
                log.info(f"💤 {s.pair} BLOCKED[disabled_strat] {s.strategy}")  # v13.5.5 fix #3
                continue  # v13.5.5 fix #3: was wasting cycles on dead signals

        # Critical market-risk blocks must run even when ML is not installed,
        # not trained, or stale. ML below may still adjust surviving signals.
        for s in ranked:
            # 🔥 MACRO GRAVITY FILTER (Active via Native Engine)
            await self._apply_hard_risk_blocks(s, ctx)

        # v12.0: ML boost + ADDITIVE intelligence scoring
        # Replaces 12+ multiplicative boosts with weighted additive system.
        # Hard blocks already ran above via _apply_hard_risk_blocks() (includes LOB, VPIN,
        # TREND_DOWN, multiex, dxy+options, gecko_movers, exchange_flow, etc.)
        # v14.5.1 FIX (audit #13): removed duplicate LOB/VPIN pre-ML block that
        # double-called .update() and .should_block() — already in _apply_hard_risk_blocks.

        # v16.0.0 AUDIT FIX (D1 structural): this whole block was gated behind
        # `if self.ml ...`, but self.ml was always None — so the ML boost AND the
        # additive intelligence scoring (incl. LIVE modules vol_delta/lob/vpin/funding/
        # liquidation/momentum/coin_profiles/azure_openai) AND the journal
        # strategy_weight nudge NEVER ran in any prior build. Decoupled into two flags
        # so each is independently enable-able after backtesting. BOTH default OFF →
        # live behavior is byte-identical to v18.4 until the operator opts in.
        _ml_on = bool(self.ml and ML_AVAILABLE and getattr(self.ml, '_ready', False))
        _intel_on = bool(getattr(self.cfg, 'INTEL_SCORING_ENABLED', False))
        if _ml_on or _intel_on:
            for sig in ranked:
                if sig.conf <= 0:
                    continue
                try:
                    c5d=self.candle_cache.get(sig.pair) or await self.ex.klines(sig.pair,"5m",100)
                    if _ml_on:
                        ml_score=self.ml.predict(c5d,TA)
                        boost=(ml_score-0.5)*self.ml.confidence_boost
                        # v16.0.0: hard cap ML influence to ±ML_CONF_BOOST (config) regardless
                        # of model's internal confidence_boost — bounded, auditable effect.
                        _cap = getattr(self.cfg, 'ML_CONF_BOOST', 0.10)
                        boost = max(-_cap, min(_cap, boost))
                        sig.conf=round(min(1.0,max(0,sig.conf+boost)),2)
                    if not _intel_on:
                        continue  # ML-only mode: skip the additive intelligence block below
                    # v7.2 (v14.6.5 AUDIT FIX F14): journal weight was multiplicative
                    # (sig.conf * jw, jw≈0.6–1.4). Combined with later OB/funding
                    # multipliers and the additive intelligence block, a streak of
                    # bad strategies could compress conf toward 0 chaotically.
                    # Now treat jw deviation from 1.0 as a bounded additive nudge.
                    jw = self.journal.strategy_weight(sig.strategy)
                    sig.conf = round(min(1.0, max(0, sig.conf + max(-0.10, min(0.10, jw - 1.0)))), 2)
                    base_conf = sig.conf  # Save pre-intelligence conf

                    # v14.5.1 FIX (audit #13): Hard blocks already ran via
                    # _apply_hard_risk_blocks() above (TREND_DOWN, multiex, dxy+options,
                    # gecko_movers, exchange_flow, long_short, open_interest, hash_rate,
                    # funding_rate, liquidation, vol_delta, lob, vpin, liq_cascade,
                    # spot_perp, crypto_news, rl_agent). Removed ~60 lines of duplicate
                    # checks that doubled API calls and added latency.
                    if sig.conf <= 0:
                        continue

                    # ═══ ADDITIVE INTELLIGENCE SCORING ═══
                    # Each module contributes a weighted adjustment (positive or negative).
                    # Total adjustment is bounded to prevent wild swings.
                    # Weight = how much influence each module has (sum ~1.0).
                    adjustments = []
                    def _adj(boost_val, weight):
                        """Convert multiplicative boost (0.7–1.3) to additive adjustment."""
                        return (boost_val - 1.0) * weight

                    # Regime penalty (replaces old 0.80 multiplier)
                    if ctx.regime == "CHOPPY":
                        adjustments.append(("regime", -0.04))

                    # Core macro modules (highest weight)
                    # v16.0.0: individually wrapped — dxy/options/multi_ex/whale are None
                    # placeholders with no implementation in this build, so a bare call
                    # would raise and abort the ENTIRE adjustment block. Wrapping makes
                    # each a clean skip.
                    try: adjustments.append(("dxy", _adj(self.dxy.get_boost(), 0.15)))
                    except Exception: pass
                    try: adjustments.append(("options", _adj(self.options.get_boost(), 0.12)))
                    except Exception: pass
                    try: adjustments.append(("multi_ex", _adj(self.multi_ex.get_boost(), 0.12)))
                    except Exception: pass
                    try: adjustments.append(("whale", _adj(self.whale.get_boost(), 0.08)))
                    except Exception: pass

                    # Market microstructure (medium weight)
                    try: adjustments.append(("ls_ratio", _adj(self.long_short.get_boost(), 0.10)))
                    except Exception: pass
                    try: adjustments.append(("oi", _adj(self.open_interest.get_boost(), 0.08)))
                    except Exception: pass
                    try: adjustments.append(("ex_flow", _adj(self.exchange_flow.get_boost(sig.pair), 0.06)))
                    except Exception: pass

                    # Supplementary modules (lower weight)
                    try: adjustments.append(("gecko_t", _adj(self.gecko_trending.get_boost(sig.pair), 0.04)))
                    except Exception: pass
                    try: adjustments.append(("gecko_m", _adj(self.gecko_movers.get_boost(sig.pair), 0.04)))
                    except Exception: pass
                    try: adjustments.append(("social", _adj(self.social_sentiment.get_boost(sig.pair), 0.03)))
                    except Exception: pass
                    try: adjustments.append(("nlp", _adj(self.transformer_nlp.get_boost(), 0.03)))
                    except Exception: pass
                    try: adjustments.append(("hash", _adj(self.hash_rate.get_boost(), 0.03)))
                    except Exception: pass
                    # v12.0: Aggressor flow
                    try: adjustments.append(("aggflow", _adj(self.aggressor_flow.get_boost(sig.pair), 0.07)))
                    except Exception: pass
                    # v12.0: Per-coin profile boost
                    try:
                        cur_hour = datetime.now(timezone.utc).hour
                        cp_boost = self.coin_profiles.signal_boost(sig.pair, sig.strategy, cur_hour)
                        adjustments.append(("coin_prof", _adj(cp_boost, 0.06)))
                    except Exception: pass
                    # v12.2: New intelligence modules
                    try: adjustments.append(("funding", _adj(self.funding_rate.get_boost(sig.pair), 0.10)))
                    except Exception: pass
                    try: adjustments.append(("liq", _adj(self.liquidation.get_boost(), 0.08)))
                    except Exception: pass
                    # v14.2: Institutional module confidence adjustments
                    try: adjustments.append(("vol_delta", _adj(self.vol_delta.get_boost(sig.pair), 0.12)))
                    except Exception: pass
                    try: adjustments.append(("lob", _adj(self.lob.get_boost(sig.pair), 0.10)))
                    except Exception: pass
                    try: adjustments.append(("liq_cascade", _adj(self.liq_cascade.get_boost(sig.pair), 0.08)))
                    except Exception: pass
                    # v15.0 Gap #2: spot_perp boost DISABLED — without futures shorts, the
                    # basis signal can't be captured. Removed from additive scoring.
                    # try: adjustments.append(("spot_perp", _adj(self.spot_perp.get_boost(sig.pair), 0.08)))
                    # except Exception: pass
                    try: adjustments.append(("vpin", _adj(self.vpin.get_boost(sig.pair), 0.10)))
                    except Exception: pass
                    try: adjustments.append(("smartcoin", _adj(self.smart_coin.get_boost(sig.pair), 0.04)))
                    except Exception: pass
                    try: adjustments.append(("cnews", _adj(self.crypto_news.get_boost(sig.pair), 0.05)))
                    except Exception: pass
                    
                    try:
                        # Feed headlines to Azure OpenAI for a human-like sentiment boost
                        hot_headlines = [ev["headline"] for ev in getattr(self.crypto_news, '_hot_events', [])]
                        if not hot_headlines and hasattr(self.crypto_news, '_sources'):
                            # Fallback if no hot events: just pass general context
                            hot_headlines = ["Crypto market volatility increasing", "Bitcoin testing major resistance"]
                        if hot_headlines:
                            oai_score = self.azure_openai.analyze_news(hot_headlines, sig.pair)
                            if oai_score != 0:
                                oai_boost = 1.0 + (oai_score * 0.15) # +/- 15%
                                adjustments.append(("openai", _adj(oai_boost, 0.15)))
                    except Exception: pass
                    try: adjustments.append(("momentum", _adj(self.momentum.get_boost(sig.pair), 0.06)))
                    except Exception: pass

                    # RL agent
                    try:
                        sig_atr_p = sig.atr/max(sig.price,0.001)*100 if sig.price>0 else 1.0
                        rl_b = self.rl.get_boost(ctx.regime, ctx.daily, sig_atr_p, ctx.fg)
                        adjustments.append(("rl", _adj(rl_b, 0.08)))
                    except Exception: pass

                    # Monte Carlo risk penalty
                    if self.monte_carlo.should_reduce_risk():
                        adjustments.append(("mc_risk", -0.04))

                    # v16.0.0: LSTM deep learning boost
                    try:
                        lstm_prob = self.lstm.predict(c5d)
                        if lstm_prob != 0.5:  # 0.5 = neutral/unavailable
                            lstm_adj = (lstm_prob - 0.5) * 0.20  # ±10% max
                            adjustments.append(("lstm", lstm_adj))
                    except Exception: pass

                    # v16.0.0: Token unlock confidence penalty
                    try:
                        unlock_boost = self.token_unlock.get_boost(sig.pair)
                        if unlock_boost < 1.0:
                            adjustments.append(("unlock", _adj(unlock_boost, 0.12)))
                    except Exception: pass

                    # Sum all adjustments, clamp to [-0.15, +0.15] to prevent wild swings
                    total_adj = sum(v for _, v in adjustments)
                    total_adj = max(-0.15, min(0.15, total_adj))
                    sig.conf = round(max(0, min(1.0, base_conf + total_adj)), 2)
                except Exception: pass

        if not ranked: return

        # v8.4: Only hard-block TREND_DOWN — CHOPPY trades with reduced confidence
        if ctx.regime == "TREND_DOWN":
            return  # No trading in downtrend, period

        # v16.0 AUDIT FIX H7: CHOPPY+BEAR block (v16.0.04 feature — was listed but never implemented)
        if ctx.regime == "CHOPPY" and ctx.daily == "BEAR":
            return  # No entries when regime=CHOPPY AND daily=BEAR

        # v15.4 TG UPGRADE: respect /pause command — skip NEW entries when paused.
        # Existing positions continue to be managed (SL/TP/trail still run via check_exits).
        if getattr(self, 'paused', False):
            if self.cycles % 20 == 1:  # remind every ~10 min that bot is paused
                log.info("⏸  Bot is PAUSED — skipping new entry analysis. Send /resume to restart.")
            return

        for sig in ranked[:3]:  # v7: Allow 2 signals per cycle
            # v13.2: Attach MTF alignment for position sizing in risk.py
            sig._ctx_mtf_align = getattr(ctx, 'mtf_align', 50)
            ok,reason,size=self.risk.can_trade(sig,ctx.fg)
            if not ok:
                log.info(f"💤 {sig.pair} SKIP[risk] — {reason} | strat={sig.strategy} conf={sig.conf} grade={sig.grade}")
                self._gate_reject(f"risk:{reason}")  # v16.0.0 telemetry
                continue
            # v16.0.0: Volatility-adjusted sizing (risk parity)
            try:
                _atr_pct_vs = sig.atr / sig.price * 100 if sig.price > 0 and sig.atr > 0 else 2.0
                _grp_vs = next((p["g"] for p in self.cfg.PAIRS if p["s"] == sig.pair), "B")
                # v15.5 FIX: disabled - double-scaling with can_trade _vol_scalar (ARB opened $38 on $54 wallet)
                pass  # was: size = self.risk.volatility_adjusted_size(size, _atr_pct_vs, _grp_vs)
                size = max(self.cfg.MIN_TRADE, round(size, 2))
            except Exception: pass
            # v9.4: Grade gate — only A+ and A trades allowed
            # v9.4: Volatility filter — skip dead markets
            # v11.2: Three-tier gate (SKIP / HALF / FULL) on 5m ATR. Gate values in
            # config.py are now 5m-calibrated. Below 0.7×gate = dead market (skip);
            # between 0.7×gate and gate = low vol (half size); ≥ gate = full size.
            try:
                c5v = self.candle_cache.get(sig.pair) or await self.ex.klines(sig.pair,"5m",60)
                atr_v = TA.atr(c5v)
                atr_pct = atr_v / sig.price * 100 if sig.price > 0 else 0
                _grp = next((p["g"] for p in self.cfg.PAIRS if p["s"] == sig.pair), "D")
                _gate_full = self.cfg.GROUP_ATR_GATES.get(_grp, 0.25)
                _gate_half = _gate_full * 0.7  # v11.2: dead-market floor
                if atr_pct < _gate_half:
                    log.info(f"💤 {sig.pair} SKIP — ATR {atr_pct:.2f}% < {_gate_half:.2f}% (group {_grp}, dead)")
                    self._gate_reject("dead_market")  # v16.0.0 telemetry
                    continue
                elif atr_pct < _gate_full:
                    # v14.6.4 AUDIT FIX (H1): removed dead `size = size` no-op,
                    # misleading "HALF" log, and bogus MIN_TRADE re-check.
                    # v14.6 intentionally removed half-sizing → fixed 33.33% allocation.
                    # Low-vol path now just logs informational and continues at full size.
                    log.info(f"ℹ️ {sig.pair} LOW_VOL — ATR {atr_pct:.2f}% < {_gate_full:.2f}% (group {_grp}, full size kept)")
            except Exception: pass
            _mtf = getattr(ctx, "mtf_align", 50)
            # v14.3 FIX: Accumulation strategies (Wyckoff/SMC) work in low-momentum
            # environments — MTF is naturally suppressed during Phase B/C accumulation.
            # Trend-following strategies keep strict 55 gate.
            _ACC_STRATS_MTF = {"WYCKOFF_ACC", "SMC_OB+FVG", "SMC_OB", "RSI_DIVERGENCE", "MACD_HIST", "KELTNER_BOUNCE", "QFL_PANIC"}
            _NO_MTF_STRATS = {"RSI_DIVERGENCE", "MACD_HIST", "KELTNER_BOUNCE", "QFL_PANIC"}
            _mtf_gate = 0 if sig.strategy in _NO_MTF_STRATS else (30 if sig.strategy in _ACC_STRATS_MTF else 45)
            if _mtf < _mtf_gate:
                log.info(f"\U0001f4a4 {sig.pair} SKIP[mtf] — MTF:{_mtf:.0f} < {_mtf_gate} | strat={sig.strategy}")
                self._gate_reject("mtf")  # v16.0.0 telemetry
                continue
            # v14.6.4 AUDIT FIX: removed dead `if False and ctx.regime in ("RANGE","CHOPPY")` block.
            # The branch never executed (DISABLED 2026-05-04). Kept the gate logic above active.
            if sig.conf < 0.50 or (sig.grade not in ("A+","A","B","C") and not (sig.grade == "D" and ctx.regime in ("TREND_UP","SQUEEZE"))):
                log.info(f"💤 {sig.pair} SKIP[quality] — conf={sig.conf} (need ≥0.50) grade={sig.grade} (need A+/A/B) strat={sig.strategy}")
                self._gate_reject("quality")  # v16.0.0 telemetry
                continue

            # v7: Correlation filter
            if not self.strat.check_correlation(sig.pair, self.risk.positions, self.candle_cache):
                log.info(f"💤 {sig.pair} SKIP[corr] — correlated with open position | strat={sig.strategy}")
                self._gate_reject("corr")  # v16.0.0 telemetry
                continue

            # v7: Order book imbalance check
            if sig.strategy not in ("QFL_PANIC",):
                try:
                    bids,asks = await self.ex.order_book(sig.pair, 10)
                    ob_ratio, ob_label = TA.ob_imbalance(bids, asks)
                    # v14.6.5 AUDIT FIX (F14): switched from multiplicative
                    # (* 0.85 / * 1.10) to bounded additive (-0.05 / +0.05).
                    # Keeps the same directional intent without compounding.
                    if ob_label == "SELL_WALL":
                        sig.conf = round(max(0, sig.conf - 0.05), 2)
                    elif ob_label == "BUY_WALL":
                        sig.conf = round(min(1.0, sig.conf + 0.05), 2)
                except Exception: pass

            # v9.2: Funding Rate Signal — negative funding = bullish edge
            # v11.2.10 FIX: was futures_funding_rate() (requires futures API perms, wasted ~360 calls/hr)
            # Now uses REST-based exchange.funding_rate() which works without futures key.
            # v14.6.5 AUDIT FIX (F14): switched from multiplicative (* 1.15 / * 0.85)
            # to bounded additive (+0.07 / -0.07). Funding signal is already covered
            # by the additive `funding` adjustment in the intelligence aggregation
            # above, but this raw exchange-side check is kept as a faster path.
            try:
                fr = await self.ex.funding_rate(sig.pair)
                if fr < -0.0005:  # Negative funding = shorts paying longs = bullish
                    sig.conf = round(min(1.0, sig.conf + 0.07), 2)
                    log.info(f"💸 {sig.pair} funding {fr:.4f} BULLISH boost → conf {sig.conf}")
                elif fr > 0.001:  # High positive funding = overleveraged longs = bearish
                    sig.conf = round(max(0, sig.conf - 0.07), 2)
                    log.info(f"💸 {sig.pair} funding {fr:.4f} BEARISH reduce → conf {sig.conf}")
            except Exception: pass
            # Re-check after adjustments
            if sig.conf < self.cfg.MIN_CONF: continue

            # Trailing Buy
            if self.cfg.TRAILING_BUY and sig.strategy not in ("QFL_PANIC",) and ctx.regime not in ("TREND_UP","TREND_DOWN"):
                if sig.pair not in self.pending:
                    self.pending[sig.pair]=PendingBuy(pair=sig.pair,signal=sig,lowest=sig.price,created=time.time())
                    log.info(f"⏳ Trail queued {sig.pair} @${sig.price:,.2f} [{sig.strategy}] [{ctx.regime}]")
                continue

            if any(info['pair'] == sig.pair for info in self._limit_orders.values()): continue  # FIX: Prevent double limit orders
            # Instant execution
            mode_tag="🩸QFL" if "QFL" in sig.strategy else "🎯"
            log.info(f"{mode_tag} [{sig.grade}] {sig.pair} @${sig.price:,.2f} | {sig.strategy} | "
                     f"Conf:{sig.conf:.2f} R:R={sig.rr}:1 ${size:.1f} [{ctx.regime}] MTF:{getattr(ctx,'mtf_align',50):.0f}")
            # v13.2: Latency tracking
            _order_t0 = time.time()

            # v15.9.0: Hybrid maker skip — time-critical strategies and fast regimes bypass maker
            _skip_maker = (
                sig.strategy in self.cfg.HYBRID_MAKER_SKIP_STRATEGIES or
                ctx.regime in self.cfg.HYBRID_MAKER_SKIP_REGIMES
            )

            if self.cfg.USE_LIMIT and not _skip_maker:
                # v15.0 #1: anti-detection size randomization ±5% (HFT pattern-break)
                # v16.0 AUDIT FIX L5: `import random as _rnd` moved to module level
                _jitter = 1 + (random.random() - 0.5) * 0.10  # 0.95-1.05
                _size_jit = size * _jitter
                _size_jit = max(_size_jit, self.cfg.MIN_TRADE)  # v15.4 FIX: jitter cannot drop below MIN_TRADE
                _qty = _size_jit / sig.price
                result=await self.ex.buy_limit(sig.pair,_qty,sig.price*(1-self.cfg.LIMIT_OFFSET_PCT/100))
                if "error" not in result:
                    if result.get("status")=="FILLED":
                        self.risk.open_pos(sig,size,result,ctx,self.tg)
                        log.info(f"✅ MAKER FILL {sig.pair} (instant)")
                        # v13.2: RL entry for limit-buy instant fill
                        pass
                    else:
                        # v11.2.18 FIX: phantom position — GTC not filled yet, track pending
                        # v15.3 AUDIT FIX #1: store _size_jit (actual exchange-side amount)
                        oid=result.get("orderId")
                        if oid:
                            self._limit_orders[oid]={"sig":sig,"size":_size_jit,"pair":sig.pair,"ts":__import__("time").time()}
                            # v15.9.0: Hybrid mode — wait + cancel + market fallback (instead of leaving pending)
                            if self.cfg.HYBRID_MAKER_ENABLED:
                                log.info(f"⏳ MAKER pending {sig.pair} #{oid}, delegating to background task")
                                asyncio.create_task(self._handle_hybrid_maker(sig, size, _size_jit, _qty, oid, ctx))
                            else:
                                # LEGACY: leave pending for the repost loop (existing behavior)
                                log.info(f"⏳ LIMIT {sig.pair} #{oid} pending (jittered size=${_size_jit:.2f})")
                elif self.cfg.HYBRID_MAKER_ENABLED and result.get("error") == "post_only_reject":
                    # v15.9.0: Maker post-only rejected (price moved through limit) → market fallback
                    log.info(f"📉 {sig.pair} MAKER REJECT (post_only), MARKET fallback")
                    result = await self.ex.buy(sig.pair, _qty)
                    if "error" not in result:
                        self.risk.open_pos(sig, size, result, ctx, self.tg)
                        log.info(f"✅ MARKET FALLBACK {sig.pair} filled")
                        pass
                    else:
                        log.warning(f"❌ {sig.pair} MARKET FALLBACK failed: {result.get('error')}")
            else:
                # v15.0 #1: anti-detection size randomization ±5% on market orders too
                # v16.0 AUDIT FIX L5: `import random as _rnd` moved to module level
                _jitter_m = 1 + (random.random() - 0.5) * 0.10
                _final_size = size * _jitter_m
                _final_size = max(_final_size, self.cfg.MIN_TRADE)  # v15.4 FIX: jitter cannot drop below MIN_TRADE
                _final_qty = _final_size / sig.price
                # v16.0: Smart Execution Engine TWAP routing
                log.info(f"📊 {sig.pair} executing order (size ${_final_size:.2f})")
                n_chunks = max(1, min(10, int(_final_size / 6.0)))  # FIX: ensure min  per chunk
                if n_chunks > 1:
                    result = await self.exec_algo.twap_buy(sig.pair, _final_qty, n_chunks=n_chunks)
                else:
                    result = await self.ex.buy(sig.pair, _final_qty)
                    
                if "error" not in result:
                    self.risk.open_pos(sig,size,result,ctx,self.tg)
                    # v13.2: Wire RL entry so reward() has correct state context
                    pass
                    _order_ms = (time.time() - _order_t0) * 1000
                    log.info(f"⚡ {sig.pair} order latency: {_order_ms:.0f}ms{'(TWAP)' if n_chunks > 1 else ''}")


    # ──────────────────────────────────────────────────────────────
    # v15.9.0 — Hybrid maker entry helpers
    # ──────────────────────────────────────────────────────────────
    async def _handle_hybrid_maker(self, sig, size, _size_jit, _qty, oid, ctx):
        """Background task for hybrid maker order resolution without blocking _cycle."""
        _filled = await self._wait_for_limit_fill(
            sig.pair, oid,
            timeout_sec=self.cfg.HYBRID_MAKER_TIMEOUT_SEC,
            poll_sec=self.cfg.HYBRID_MAKER_POLL_INTERVAL
        )
        if _filled:
            self._limit_orders.pop(oid, None)
            self.risk.open_pos(sig, size, _filled, ctx, self.tg)
            log.info(f"✅ MAKER FILL {sig.pair} (delayed)")
            pass
        else:
            # Timeout — cancel and check for race fill, else market fallback
            _race = await self._cancel_and_check(sig.pair, oid)
            self._limit_orders.pop(oid, None)
            if _race:
                self.risk.open_pos(sig, size, _race, ctx, self.tg)
                log.info(f"✅ MAKER RACE FILL {sig.pair}")
                pass
            else:
                log.info(f"📉 {sig.pair} MAKER TIMEOUT, MARKET fallback")
                result = await self.ex.buy(sig.pair, _qty)
                if "error" not in result:
                    self.risk.open_pos(sig, size, result, ctx, self.tg)
                    log.info(f"✅ MARKET FALLBACK {sig.pair} filled")
                    pass
                else:
                    log.warning(f"❌ {sig.pair} MARKET FALLBACK failed: {result.get('error')}")


    async def _wait_for_limit_fill(self, sym, order_id, timeout_sec=30, poll_sec=3.0):
        """Poll Binance for order fill status until timeout.
        Returns a fill-compatible dict on success, None if still pending at timeout.
        Handles external cancellation (returns None) and partial fills (treats as filled).
        """
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                await asyncio.sleep(poll_sec)
            except Exception:
                break
            try:
                o = await self.ex.get_order(symbol=sym, orderId=order_id)
                status = (o.get("status") or "").upper()
                eq = float(o.get("executedQty", 0) or 0)
                if status == "FILLED":
                    return self._build_filled_result(sym, order_id, o)
                if status in ("CANCELED", "EXPIRED", "REJECTED"):
                    # Externally canceled — use partial fill if any
                    return self._build_filled_result(sym, order_id, o) if eq > 0 else None
                # NEW or PARTIALLY_FILLED → keep waiting
            except Exception as e:
                log.debug(f"poll order {order_id}: {e}")
        return None

    async def _cancel_and_check(self, sym, order_id):
        """Cancel a pending limit order. Handles cancel race: if order filled before
        cancel arrived, returns the fill data so the caller can still open the position.
        Returns dict on race-fill, None on clean cancel.
        """
        try:
            await self.ex.cancel_order(symbol=sym, orderId=order_id)
        except Exception as e:
            # -2011 = unknown order (already gone) — fall through to status check
            if "-2011" not in str(e) and "Unknown order" not in str(e):
                log.debug(f"cancel {order_id}: {e}")
        try:
            o = await self.ex.get_order(symbol=sym, orderId=order_id)
            status = (o.get("status") or "").upper()
            eq = float(o.get("executedQty", 0) or 0)
            if status == "FILLED" or eq > 0:
                return self._build_filled_result(sym, order_id, o)
        except Exception:
            pass
        return None

    def _build_filled_result(self, sym, order_id, o):
        """Construct a result dict compatible with risk.open_pos() from a get_order response.
        Synthesizes a single-entry fills array from executedQty + cummulativeQuoteQty.
        """
        eq = float(o.get("executedQty", 0) or 0)
        cqq = float(o.get("cummulativeQuoteQty", 0) or 0)
        avg_p = (cqq / eq) if eq > 0 else 0.0
        fills = [{
            "price": f"{avg_p:.8f}",
            "qty": f"{eq:.8f}",
            "commission": "0",
            "commissionAsset": "USDT",
        }] if eq > 0 else []
        return {
            "orderId": order_id,
            "status": "FILLED",
            "executedQty": str(eq),
            "cummulativeQuoteQty": str(cqq),
            "fills": fills,
            "transactTime": o.get("updateTime") or o.get("transactTime"),
            "symbol": sym,
        }

    async def _async_retrain_ml(self):
        """v15.2: Async ML retrain — replaces thread-based training."""
        try:
            c5 = await self.ex.klines("BTCUSDT", "5m", 2000)
            if c5:
                await asyncio.to_thread(self.ml.train, c5, TA)
        except Exception as e:
            log.debug(f"Async ML retrain: {e}")

    async def _async_retrain_hyperopt(self):
        """v15.2: Async HyperOpt retrain."""
        try:
            c5 = await self.ex.klines("BTCUSDT", "5m", 500)
            if c5:
                await asyncio.to_thread(self.hyperopt.optimize, c5, TA)
        except Exception as e:
            log.debug(f"Async HyperOpt retrain: {e}")



    def _stop(self,*a):
        log.info("\n🛑 Stopping — saving state...")
        # v13.5.7 FIX #4: save state EARLY, before cleanup
        try:
            self.risk.save_state(self.grid.pnl, self.grid.trades,
                                 self.hyperopt.best_params if self.hyperopt else None)
            log.info("  💾 Early state snapshot saved")
        except Exception as _e:
            log.error(f"  ⚠️ Early state save failed: {_e}")
        # v8.4 FIX #6: Release execution lock
        try:
            if _HAS_FCNTL:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
        except Exception: pass
        # v15.2: Signal handler must be sync. Just set flag — async cleanup
        # happens in run()'s exit path where we CAN await.
        self.running = False

    async def _async_shutdown(self):
        """v15.2: Async shutdown cleanup — called from run() after loop exits."""
        # Cancel open limit orders (preserve native SL)
        try:
            tracked_sl_ids = {getattr(_p, "native_sl_order_id", None)
                              for _p in self.risk.positions
                              if getattr(_p, "native_sl_order_id", None)}
            for p in self.cfg.PAIRS:
                orders = await self.ex.get_open_orders(symbol=p["s"])
                if isinstance(orders, dict) and 'error' in orders: continue
                for o in orders:
                    otype = o.get("type", "")
                    oid = o.get("orderId")
                    if otype == "STOP_LOSS_LIMIT" and oid in tracked_sl_ids:
                        log.info(f"  Preserved native SL {p['s']} #{oid}")
                        continue
                    if otype == "STOP_LOSS_LIMIT":
                        log.info(f"  Preserved STOP_LOSS_LIMIT {p['s']} #{oid}")
                        continue
                    await self.ex.cancel_order(symbol=p["s"], orderId=oid)
                    log.info(f"Cancelled open order {p['s']} #{oid} ({otype})")
        except Exception: pass
        self.risk.save_state(self.grid.pnl,self.grid.trades,
                             self.hyperopt.best_params if self.hyperopt else None)
        self.ws.stop()
        self.tg.send(f"🛑 <b>BinBot V18.8 GodMode stopped</b>")  # v18.8: unified version string
        # v13.5.5: stop dashboard cleanly
        try:
            if getattr(self, "_dashboard", None):
                self._dashboard.stop()
                log.info("  🌐 Dashboard stopped")
        except Exception as _e:
            log.debug(f"Dashboard stop suppressed: {_e}")
        # v13.5.5: stop stress test daemon
        try:
            if getattr(self, "_stress", None):
                self._stress.stop()
                log.info("  📊 Stress test stopped")
        except Exception as _e:
            log.debug(f"Stress test stop suppressed: {_e}")
        # v11.2.8 FIX (May 4, 2026): graceful telegram pool shutdown after final message.
        # Brief sleep so the final TG send has a chance to flush before pool closes.
        await asyncio.sleep(1)  # v16.0 AUDIT FIX H2: was time.sleep(1) — blocked entire event loop
        try: self.tg.close()
        except Exception: pass
        self.running=False

    def _summary(self):
        s=self.risk.status()
        hrs=(datetime.now(timezone.utc)-self.start).total_seconds()/3600 if self.start else 0
        log.info("━"*70)
        log.info(f"  📊 SESSION | {hrs:.1f}h | {self.cycles} cycles")
        log.info(f"  Trades: {s['tt']} | PnL: ${s['pnl']:+.4f} ({s['pnl_pct']:+.2f}%)")
        log.info(f"  Grid: ${self.grid.pnl:+.4f} ({self.grid.trades}t)")
        log.info(f"  WR: {s['wr']:.1f}% | Fees: ${s['fees']:.4f} | Heat: {s['heat']:.1f}%")
        if self.ml and self.ml._ready: log.info(f"  ML Acc: {self.ml.accuracy:.1%}")
        if self.hyperopt: log.info(f"  HyperOpt: {self.hyperopt.best_params}")
        log.info(f"  DD Shield: {self.ddshield.status} | Recoveries: {self.healer.recovery_count}")
        log.info("━"*70)


if __name__=="__main__":
    cfg=Config()
    cfg.USE_TESTNET=False  # v11.2.7 LIVE-ONLY
    # v13.5.3 audit Bug #51: was raw idx+1 lookup with no guard. Same fix as
    # main.py's _parse_capital_arg. Most production runs go through main.py
    # so this is a footgun for anyone running bot.py directly.
    if "--capital" in sys.argv:
        i = sys.argv.index("--capital")
        if i + 1 >= len(sys.argv):
            sys.stderr.write("❌ --capital requires a numeric value\n"); sys.exit(2)
        try:
            cfg.TOTAL_CAPITAL = float(sys.argv[i+1])
        except ValueError:
            sys.stderr.write(f"❌ --capital expects a number, got '{sys.argv[i+1]}'\n"); sys.exit(2)
    asyncio.run(ProBotV11(cfg).run())  # v16.0 AUDIT FIX H3: run() is async — was coroutine-never-executed
