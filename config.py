# BinBot v11 — config.py
import os, sys
from dataclasses import dataclass, field
try:
    from dotenv import load_dotenv
    # v11.2.9 FIX (May 4, 2026): resolve .env relative to this file instead of
    # hardcoded ~/binbot_v11/. Was: only worked when bot was at exactly that path,
    # silently failed on Windows / different install layouts. systemd EnvironmentFile=
    # is still the primary credential source, this is the dev/test fallback.
    # v13.4 (Batch 1): legacy fallback path updated to match v13 install dir.
    # systemd EnvironmentFile= remains the primary credential source in production.
    _here = os.path.dirname(os.path.abspath(__file__))
    _env_paths = [
        os.path.join(_here, '.env'),                      # same dir as config.py (preferred)
        os.path.expanduser('~/binbot_live/.env'),          # v13 install path
        os.path.expanduser('~/binbot/.env'),              # generic install path
    ]
    for _p in _env_paths:
        if os.path.exists(_p):
            load_dotenv(dotenv_path=_p)
            break
except ImportError: pass

@dataclass
class Config:
    MIN_POS_SIZE: float = 5.0
    AUTO_DISABLE_STRATS: bool = False
    MIN_RISK_USD: float = 1.0
    API_KEY: str = ""  # loaded from .env
    API_SECRET: str = ""  # loaded from .env
    USE_TESTNET: bool = False
    TOTAL_CAPITAL: float = 0.00  # v13.5.5 fresh start May 12 2026
    RISK_PCT: float = 0.02  # v14.6.5 AUDIT FIX: Option C — 2% risk (keeps existing per-trade risk level)
    # v18.9.6: per-trade SL ceiling + risk-normalized sizing. With RISK_NORMALIZE_SIZE on,
    # a wider/slipped stop SHRINKS the position so dollar risk never exceeds RISK_PCT of
    # capital (was: size fixed at ~33% regardless of SL width → real risk floated to ~3.3%).
    MAX_SL_PCT: float = 0.07
    RISK_NORMALIZE_SIZE: bool = True
    # v18.9.9: signed-request recv window (ms) + per-entry liquidity floor (audit fixes)
    RECV_WINDOW_MS: int = 10000
    MIN_24H_VOL_USD: float = 5_000_000.0  # skip entries on coins thinner than this (live 24h quote vol)
    # v18.9.9 (audit H1): drop the still-forming candle so strategies evaluate only CLOSED
    # bars — kills the repaint where signals fire on an intra-bar wick that vanishes on close.
    DROP_UNCLOSED_CANDLE: bool = True
    # v18.9.9 (audit H3): max amount the OB/funding micro-nudges may carry a signal toward
    # MIN_CONF. The pre-nudge conviction must be >= MIN_CONF - this, so a quirk can't gate it in.
    CONF_NUDGE_TOLERANCE: float = 0.03
    # v18.9.9 (audit H2): also block a long when the COIN's own 5m regime is TREND_DOWN
    # (the global regime is BTC-derived). Strictly additive — only ever blocks more.
    PER_COIN_REGIME_BLOCK: bool = True
    MAX_POSITIONS: int = 2  # v14.6.5 AUDIT FIX: Option C — 2 positions (spreads risk vs old SNIPER_90 single trade)
    MAX_EXPOSURE: float = 0.75   # v14.6.5 AUDIT FIX: Option C — 75% max exposure (down from 90% for safety)
    POSITION_SIZE_PCT: float = 0.3333  # v18.7.4: base fraction of capital per trade (was hardcoded
    #                                    0.3333 in risk.py). The capital-tier auto-switcher rewrites
    #                                    this live by equity (see CAPITAL_TIER_* below).

    # ── v18.7.4: AUTOMATIC CAPITAL TIERS (no manual code — bot switches by live equity) ──
    # Small balances can't be meaningfully split into 2 positions (each would sit near the
    # Binance $5 min-notional), so below SMALL_TIER_USD the bot concentrates into ONE trade
    # sized at SMALL_TIER_SIZE_PCT of balance. At/above the threshold it reverts to the
    # normal multi-position config. Switching is automatic, hysteresis-guarded, and only
    # acts on a transition. Tune any number here — no code editing required.
    CAPITAL_TIER_ENABLED: bool = True
    SMALL_TIER_USD: float = 100.0       # v18.8: below $100 → 1 concentrated position (was $50)
    SMALL_TIER_MAX_POS: int = 1         # below $100: max 1 position
    SMALL_TIER_SIZE_PCT: float = 0.95   # v18.9.18: user choice — 95% per trade (small acct, 1 position, concentrate to grow)
    SMALL_TIER_EXPOSURE: float = 0.95   # v18.9.18: match 95% per-trade size
    # v18.9.10: small-account per-trade risk budget. Because small balances run only ONE
    # position, they may concentrate up to SMALL_TIER_SIZE_PCT — but risk-normalize uses THIS
    # (slightly above the normal 2%) as the hard ceiling on a single trade's loss, so even at
    # 85% size the worst-case stop-out is ~2.5% of equity (and the 5% SL ceiling bounds slip).
    SMALL_TIER_RISK_PCT: float = 0.05   # v18.9.18: raised so 95% actually deploys at the wider stop (worst-case ~5% loss/trade = daily cap)
    NORMAL_MAX_POS: int = 2             # max positions at/above SMALL_TIER_USD (current setting)
    NORMAL_SIZE_PCT: float = 0.45      # v18.9.19: 45% per trade at/above $100 (2 positions = ~90% deployed)
    NORMAL_EXPOSURE: float = 0.95      # v18.9.19: 95% max exposure across 2 positions
    NORMAL_RISK_PCT: float = 0.025     # v18.9.19: per-trade risk budget at/above $100 so 45% deploys at the wider stop
    CAPITAL_TIER_HYSTERESIS: float = 0.04  # ±4% dead-band around the threshold (anti-flap)

    # v18.8: AUTO-ADOPT ORPHAN COINS. On startup, any managed-pair coin sitting in the
    # wallet that the bot isn't tracking (e.g. after a state wipe, or a manual buy) is
    # ADOPTED as a managed position instead of being sold off — so it gets SL/TP management
    # and is never stranded. Cost basis = current market price (the real entry is unknown).
    # Only adopts coins worth >= AUTO_ADOPT_MIN_USD. Set False to revert to sell-on-sight.
    AUTO_ADOPT_ORPHANS: bool = False  # v18.9.13: user holds coins manually in this account — bot must NOT adopt them
    AUTO_ADOPT_MIN_USD: float = 5.0     # ignore dust below this (also Binance min-notional)
    # v18.9.12: the periodic reconciler auto-SELLS any untracked non-USDT coin to USDT (so a
    # USDT bot can use it). Set False to only ALERT and leave the coin for you to convert
    # manually. (Only ever sells coins that actually fill — see reconciler.py.)
    ORPHAN_AUTO_SELL: bool = False    # v18.9.13: user trades manually here — only ALERT on untracked coins, never sell them
    # v18.9.13: BTC crash guard. The old logic panic-SOLD everything at a 5% BTC drop, which
    # churned (sell-all -> rebuy -> sell-all). This version only PAUSES new entries when BTC is
    # down >= BTC_CRASH_PCT in 24h (no panic-sell -> no churn); existing positions ride their stops.
    BTC_CRASH_GUARD: bool = True
    BTC_CRASH_PCT: float = 0.15

    MIN_TRADE: float = 5.5  # v15.4 FIX: 10% buffer above Binance $5 MIN_NOTIONAL
    # v19.0: TWAP execution gating. Below TWAP_MIN_USD an order is sent as a single
    # immediate market fill (no slicing) — at retail notional market impact is ~zero, so
    # slicing only added 10-30s latency + slippage (a $23 order -> ~4 chunks -> ~23s).
    # Above the floor we target ~TWAP_CHUNK_USD per chunk (never below the v18.9.11 C3
    # per-pair min-notional), capped at 10 chunks.
    TWAP_MIN_USD: float = 150.0
    TWAP_CHUNK_USD: float = 50.0
    TAKER_FEE: float = 0.00075  # v14.6.2: BNB discount 0.075%
    MAKER_FEE: float = 0.0     # v14.6.2: LIMIT_MAKER = 0% maker fee

    # ── Group D (mid-cap high-momentum) settings ──────────────────
    GROUP_D_ENABLED: bool = False
    GROUP_D_MAX_POS: int = 1           # max 1 Group D position simultaneously
    GROUP_D_SIZE_PCT: float = 0.15     # 15% of capital per trade
    GROUP_D_SL_FLOOR: float = 0.04    # 4% SL minimum (wider than Group A 3%)
    GROUP_D_SL_CEIL: float = 0.10     # 10% SL maximum
    GROUP_D_RR: float = 5.0           # 5:1 R:R → 4% SL = 20% TP
    GROUP_D_DAILY_LOSS_PCT: float = 0.03  # 3% daily loss limit for Group D
    GROUP_D_MIN_VOL: float = 10_000_000   # 0M min 24h volume
    GROUP_D_BTC_PUMP_PCT: float = 0.015  # BTC +1.5% in 1h = alt pump trigger

    PAIRS: list = field(default_factory=lambda: [
        {"s":"BTCUSDT", "n":"BTC", "g":"A","t":1},
        {"s":"ETHUSDT", "n":"ETH", "g":"A","t":1},
        {"s":"BNBUSDT", "n":"BNB", "g":"A","t":1},
        {"s":"SOLUSDT", "n":"SOL", "g":"A","t":1},
        {"s":"XRPUSDT", "n":"XRP", "g":"A","t":1},
        {"s":"ADAUSDT", "n":"ADA", "g":"A","t":1},
        {"s":"DOTUSDT", "n":"DOT", "g":"A","t":1},
        {"s":"AVAXUSDT", "n":"AVAX", "g":"A","t":1},
        {"s":"NEARUSDT", "n":"NEAR", "g":"A","t":1},
        {"s":"SUIUSDT", "n":"SUI", "g":"A","t":1},
        {"s":"TONUSDT", "n":"TON", "g":"A","t":1},
        {"s":"TRXUSDT", "n":"TRX", "g":"A","t":1},
        {"s":"ATOMUSDT", "n":"ATOM", "g":"A","t":1},
        {"s":"ICPUSDT", "n":"ICP", "g":"A","t":1},
        {"s":"APTUSDT", "n":"APT", "g":"A","t":1},
        {"s":"STXUSDT", "n":"STX", "g":"A","t":1},
        {"s":"HBARUSDT", "n":"HBAR", "g":"A","t":1},
        {"s":"ALGOUSDT", "n":"ALGO", "g":"A","t":1},
        {"s":"EGLDUSDT", "n":"EGLD", "g":"A","t":1},
        {"s":"VETUSDT", "n":"VET", "g":"A","t":1},
        {"s":"INJUSDT", "n":"INJ", "g":"A","t":1},
        {"s":"TIAUSDT", "n":"TIA", "g":"A","t":1},
        {"s":"SEIUSDT", "n":"SEI", "g":"A","t":1},
        {"s":"MINAUSDT", "n":"MINA", "g":"A","t":1},
        {"s":"POLUSDT", "n":"POL", "g":"A","t":1},
        {"s":"ARBUSDT", "n":"ARB", "g":"A","t":1},
        {"s":"OPUSDT", "n":"OP", "g":"A","t":1},
        {"s":"STRKUSDT", "n":"STRK", "g":"A","t":1},
        {"s":"METISUSDT", "n":"METIS", "g":"A","t":1},
        {"s":"MANTAUSDT", "n":"MANTA", "g":"A","t":1},
        {"s":"SKLUSDT", "n":"SKL", "g":"A","t":1},
        {"s":"TAOUSDT", "n":"TAO", "g":"A","t":1},
        {"s":"RENDERUSDT", "n":"RENDER", "g":"A","t":1},
        {"s":"GRTUSDT", "n":"GRT", "g":"A","t":1},
        {"s":"FILUSDT", "n":"FIL", "g":"A","t":1},
        {"s":"ARUSDT", "n":"AR", "g":"A","t":1},
        {"s":"THETAUSDT", "n":"THETA", "g":"A","t":1},
        {"s":"LPTUSDT", "n":"LPT", "g":"A","t":1},
        {"s":"FETUSDT", "n":"FET", "g":"A","t":1},
        {"s":"WLDUSDT", "n":"WLD", "g":"A","t":1},
        {"s":"JASMYUSDT", "n":"JASMY", "g":"A","t":1},
        {"s":"ENSUSDT", "n":"ENS", "g":"A","t":1},
        {"s":"IOTXUSDT", "n":"IOTX", "g":"A","t":1},
        {"s":"ANKRUSDT", "n":"ANKR", "g":"A","t":1},
        {"s":"STORJUSDT", "n":"STORJ", "g":"A","t":1},
        {"s":"UNIUSDT", "n":"UNI", "g":"A","t":1},
        {"s":"AAVEUSDT", "n":"AAVE", "g":"A","t":1},
        {"s":"MKRUSDT", "n":"MKR", "g":"A","t":1},
        {"s":"RUNEUSDT", "n":"RUNE", "g":"A","t":1},
        {"s":"LDOUSDT", "n":"LDO", "g":"A","t":1},
        {"s":"CRVUSDT", "n":"CRV", "g":"A","t":1},
        {"s":"COMPUSDT", "n":"COMP", "g":"A","t":1},
        {"s":"CAKEUSDT", "n":"CAKE", "g":"A","t":1},
        {"s":"PENDLEUSDT", "n":"PENDLE", "g":"A","t":1},
        {"s":"JTOUSDT", "n":"JTO", "g":"A","t":1},
        {"s":"JUPUSDT", "n":"JUP", "g":"A","t":1},
        {"s":"ENAUSDT", "n":"ENA", "g":"A","t":1},
        {"s":"YFIUSDT", "n":"YFI", "g":"A","t":1},
        {"s":"SNXUSDT", "n":"SNX", "g":"A","t":1},
        {"s":"1INCHUSDT", "n":"1INCH", "g":"A","t":1},
        {"s":"PYTHUSDT", "n":"PYTH", "g":"A","t":1},
        {"s":"RAYUSDT", "n":"RAY", "g":"A","t":1},
        {"s":"DYDXUSDT", "n":"DYDX", "g":"A","t":1},
        {"s":"SUSHIUSDT", "n":"SUSHI", "g":"A","t":1},
        {"s":"TRBUSDT", "n":"TRB", "g":"A","t":1},
        {"s":"LTCUSDT", "n":"LTC", "g":"A","t":1},
        {"s":"BCHUSDT", "n":"BCH", "g":"A","t":1},
        {"s":"XLMUSDT", "n":"XLM", "g":"A","t":1},
        {"s":"CELOUSDT", "n":"CELO", "g":"A","t":1},
        {"s":"ETCUSDT", "n":"ETC", "g":"A","t":1},
        {"s":"NEOUSDT", "n":"NEO", "g":"A","t":1},
        {"s":"QTUMUSDT", "n":"QTUM", "g":"A","t":1},
        {"s":"IOTAUSDT", "n":"IOTA", "g":"A","t":1},
        {"s":"ZECUSDT", "n":"ZEC", "g":"A","t":1},
        {"s":"DASHUSDT", "n":"DASH", "g":"A","t":1},
        {"s":"IMXUSDT", "n":"IMX", "g":"A","t":1},
        {"s":"GALAUSDT", "n":"GALA", "g":"A","t":1},
        {"s":"CHZUSDT", "n":"CHZ", "g":"A","t":1},
        {"s":"SANDUSDT", "n":"SAND", "g":"A","t":1},
        {"s":"MANAUSDT", "n":"MANA", "g":"A","t":1},
        {"s":"AXSUSDT", "n":"AXS", "g":"A","t":1},
        {"s":"FLOWUSDT", "n":"FLOW", "g":"A","t":1},
        {"s":"ENJUSDT", "n":"ENJ", "g":"A","t":1},
        {"s":"SUPERUSDT", "n":"SUPER", "g":"A","t":1},
        {"s":"BEAMXUSDT", "n":"BEAMX", "g":"A","t":1},
        {"s":"GMTUSDT", "n":"GMT", "g":"A","t":1},
        {"s":"AUDIOUSDT", "n":"AUDIO", "g":"A","t":1},
        {"s":"ONEUSDT", "n":"ONE", "g":"A","t":1},
        {"s":"HOTUSDT", "n":"HOT", "g":"A","t":1},
        {"s":"RVNUSDT", "n":"RVN", "g":"A","t":1},
        {"s":"BATUSDT", "n":"BAT", "g":"A","t":1},
        {"s":"ILVUSDT", "n":"ILV", "g":"A","t":1},
        {"s":"DOGEUSDT", "n":"DOGE", "g":"A","t":1},
        {"s":"SHIBUSDT", "n":"SHIB", "g":"A","t":1},
        {"s":"PEPEUSDT", "n":"PEPE", "g":"A","t":1},
        {"s":"WIFUSDT", "n":"WIF", "g":"A","t":1},
        {"s":"FLOKIUSDT", "n":"FLOKI", "g":"A","t":1},
        {"s":"BONKUSDT", "n":"BONK", "g":"A","t":1},
        {"s":"WUSDT", "n":"W", "g":"A","t":1},
        {"s":"TNSRUSDT", "n":"TNSR", "g":"A","t":1},
        {"s":"IOUSDT", "n":"IO", "g":"A","t":1},
        {"s":"NOTUSDT", "n":"NOT", "g":"A","t":1},
        {"s":"ZKUSDT", "n":"ZK", "g":"A","t":1},
        {"s":"ARKMUSDT", "n":"ARKM", "g":"A","t":1},
        {"s":"WOOUSDT", "n":"WOO", "g":"A","t":1},
        {"s":"ORDIUSDT", "n":"ORDI", "g":"A","t":1},
        {"s":"1000SATSUSDT", "n":"1000SATS", "g":"A","t":1},
        {"s":"GASUSDT", "n":"GAS", "g":"A","t":1},
    ])

    # Grid
    GRID_ENABLED: bool = False  # v8.4: needs $500+ to be meaningful. v11.2.4 WARN: keep False.
    # GridEngine.check() in strategies.py marks lv.filled=True BEFORE bot.py confirms
    # exchange success. If self.ex.buy() / self.ex.sell() fails (insufficient USDT, API
    # limit), grid state desyncs from real holdings and grid breaks permanently for that
    # symbol. Do NOT set True until grid execution properly checks return values.
    # (Audit May 3, 2026.)
    GRID_LEVELS: int = 8  # v7: more levels
    GRID_CAPITAL_PCT: float = 0.50
    GRID_GEOMETRIC: bool = True  # v7: geometric spacing

    # DCA
    DCA_ENABLED: bool = False  # v8.4: don't average down with $53
    DCA_STEPS: list = field(default_factory=lambda: [1.0, 2.5, 5.0])
    DCA_MULT: list = field(default_factory=lambda: [1.0, 1.5, 2.0])

    # QFL
    QFL_DROP: float = 3.0
    QFL_VOL: float = 2.0
    # v18.9.6: don't catch falling knives in a confirmed BEAR daily downtrend. QFL_PANIC
    # buys a 3-5% high-volume drop with NO trend/HTF confirmation; in a BEAR daily it just
    # rides the dump to its stop. Still allowed in NEUTRAL/BULL (oversold-bounce reversal).
    BLOCK_QFL_IN_BEAR: bool = True

    # Trailing Buy
    TRAILING_BUY: bool = False  # v9.7 FIX: was silently expiring most signals on flat days
    TRAIL_BUY_PCT: float = 0.30  # v8.4: wait for stronger reversal

    # Scale-out
    SCALE_OUT: bool = False   # v9.2: $76 positions big enough to split (50%/30%/20%)
    SCALE_LEVELS: list = field(default_factory=lambda: [
        {"pct":0.50,"rr":1.0}, {"pct":0.30,"rr":2.0}, {"pct":0.20,"rr":0}
    ])

    # v7: Limit orders
    USE_LIMIT: bool = True   # v14.6.2: enabled — maker fees save 0.2% per round trip
    # v14.6.4 AUDIT NOTE: previous warning about phantom-position bug was resolved by
    # v11.2.18 — _limit_orders dict tracks unfilled GTC orders and reconciles them
    # in the cycle loop (see bot.py _check_limit_orders around line 1701). The old
    # warning is preserved below as history but is NO LONGER an active concern.
    # OLD WARNING (resolved):
    #  (a) phantom-position bug — RESOLVED v11.2.18 (_limit_orders tracking)
    #  (b) failure fallback to market order — current code does NOT fall back; pending order is tracked
    LIMIT_OFFSET_PCT: float = 0.10  # v14.6.2: 0.10% for better maker probability

    # v15.9.0: Hybrid maker entry — addresses the "(b) failure fallback to market" note above
    # When enabled, the main entry path tries LIMIT_MAKER first (saves spread), waits
    # HYBRID_MAKER_TIMEOUT_SEC for fill, then cancels + falls back to MARKET if needed.
    # Time-critical strategies (front-running whales / panic dips) skip maker entirely.
    # Fast regimes (TREND_UP) skip maker because price runs away from limit orders.
    # Set HYBRID_MAKER_ENABLED=False to revert to legacy "place + leave pending" behavior.
    HYBRID_MAKER_ENABLED: bool = True
    HYBRID_MAKER_TIMEOUT_SEC: int = 30          # wait up to N seconds for maker fill
    HYBRID_MAKER_POLL_INTERVAL: float = 3.0     # poll get_order every N seconds while waiting
    HYBRID_MAKER_SKIP_STRATEGIES: tuple = ("MICRO_PRICE", "AGGRESSOR_FLOW", "QFL_PANIC")
    HYBRID_MAKER_SKIP_REGIMES: tuple = ("TREND_UP",)

    # ── v19.1.0 PROFITABILITY FEATURES (research-driven; small-account-focused) ──────
    # Feature 1: BNB fee discount + net-edge gate. For a $50-250 account, fee drag is the
    # #1 P&L leak. Pay fees in BNB (0.1%→0.075%) and refuse trades that can't clear cost.
    FEE_BNB_AUTO_ENABLE: bool = True     # call /sapi/v1/bnb/burn at boot so TAKER_FEE 0.075% is real
    NET_EDGE_GATE_ENABLED: bool = True   # reject entries whose TP can't clear round-trip cost + margin
    SLIPPAGE_BUF: float = 0.0010         # 0.10%/side assumed slippage (conservative)
    MIN_NET_EDGE: float = 0.0030         # require ≥0.30% NET edge to TP after 2×fee + 2×slippage
    # Feature 3: chop veto — "not trading" is the highest-EV action in range-bound chop.
    CHOP_VETO_ENABLED: bool = True       # block new entries in CHOPPY regime unless top-grade
    CHOP_VETO_ALLOW_GRADE: str = "A+"    # grade still allowed to enter during chop
    # Feature 4: volatility-target sizing (exposes the previously-hardcoded values; defaults
    # are the exact prior constants, so this is behaviour-neutral until tuned).
    VOL_TARGET: float = 0.010            # 1.0% target ATR% — calm→size up, volatile→size down
    VOL_SCALAR_MIN: float = 0.50
    VOL_SCALAR_MAX: float = 1.50
    # Feature 5: session-bias — documented crypto seasonality (Asia-open Mon momentum;
    # dead US-overnight window). Bounded SIZE multiplier, re-clamped to exposure caps.
    SESSION_BIAS_ENABLED: bool = True

    # v15.14: 3-rung real-profit ladder.
    # Removed the +0.68% early lock — was generating +0.5% consolation exits in CHOPPY.
    # Philosophy: real profits or clean SL. No tiny consolation prizes.
    # Entry $100, SL $97, TP $104.50 example:
    #   Peak +2.00% -> lock +1.50% -> effective exit ~+1.80%
    #   Peak +3.00% -> lock +2.50% -> effective exit ~+2.80%
    #   Peak +3.50% -> lock +3.00% -> effective exit ~+3.30%
    # After +3.50%: ATR ghost trail. TP hit: CHASE mode.
    # Set False to revert to v15.6 single-rung.
    USE_MULTI_TIER_LADDER: bool = True

    # v16.0: Progressive SL tightening (Paper §3.4).
    # After 2h without lock, SL tightens -3% → -1.5% over next 2 hours.
    PROGRESSIVE_SL_ENABLED: bool = True
    PROGRESSIVE_SL_FLOOR: float = 0.015   # tightest SL = -1.5% at hour 4

    # v16.0: Partial scale-out at rung 2 (Paper §4.2 split TPs).
    # When 2nd lock fires (+2.5%), sell 40% and keep 60% as free runner.
    PARTIAL_SCALEOUT_ENABLED: bool = True
    PARTIAL_SCALEOUT_PCT: float = 0.40    # sell 40% at rung 2 lock

    # v16.0: Daily volatility harvesting (Paper §5.3).
    # Reduces new position size when today is profitable (bank the gains).
    # Day > 1%: size ×0.85 | Day > 2%: size ×0.75 | Losing day: size ×0.80
    DAILY_HARVEST_ENABLED: bool = True
    DAILY_HARVEST_THRESHOLD: float = 0.01  # 1% daily gain triggers harvesting

    # v15.16: Correlation matrix penalty.
    # Blocks new entry if Pearson correlation with any open position > threshold.
    # Prevents NEAR+SUI+ATOM triple exposure to a single BTC dump.
    CORR_PENALTY_ENABLED: bool = True
    CORR_BLOCK_THRESHOLD: float = 0.85   # block if |correlation| > 0.85

    # v15.16: Equity curve meta-risk.
    # Tracks daily equity vs 10-day MA. Reduces position size automatically
    # when bot is in drawdown (equity below MA = strategies out of sync).
    # Above 10d MA=100% size | Below=50% | Below 20d MA=25%
    EQUITY_CURVE_ENABLED: bool = True

    # v15.16: Dynamic regime-aware Kelly (activates at 50+ trades with Kelly).
    # TREND_UP=full Kelly | RANGE=half | CHOPPY=quarter
    KELLY_REGIME_AWARE: bool = True

    # v15.14: Block ALL entries when regime=CHOPPY AND daily=BEAR.
    BLOCK_CHOPPY_BEAR: bool = True

    # v18.7.3: Stop the WYCKOFF_ACC / accumulation small-loss bleed. In a BEAR daily
    # downtrend, slow "buy the dip" accumulation longs stall and time-exit at small losses.
    # Block them across all 5m regimes when daily=BEAR (fast reversal plays QFL_PANIC /
    # SMC_SWEEP are deliberately excluded). Add strategy names to ACCUMULATION_STRATS if
    # you see another dip-buy strategy bleeding in downtrends.
    BLOCK_ACCUMULATION_IN_BEAR: bool = True
    ACCUMULATION_STRATS: tuple = ("WYCKOFF_ACC", "SMC_OB", "SMC_OB+FVG")
    # WYCKOFF entry quality gate (strategies.analyze): require volume confirmation + a
    # non-bearish higher timeframe so it only fires on a real volume-backed spring.
    WYCKOFF_STRICT: bool = True
    WYCKOFF_MIN_VOL_RATIO: float = 1.1

    # v15.15: BEAR regime max 1 position (correlation guard).
    # All altcoins fall together on BTC dump — 2 longs = 2× SL hit risk.
    # Set MAX_POSITIONS_BEAR=2 to allow 2 positions in BEAR (not recommended).
    MAX_POSITIONS_BEAR_ENABLED: bool = True
    MAX_POSITIONS_BEAR: int = 1

    # v15.15: Time-based exit — close stagnant trades in BEAR regime.
    # If position has been open 4h with <0.5% progress, free the capital.
    # Set TIME_EXIT_ENABLED=False to disable (not recommended in BEAR).
    TIME_EXIT_ENABLED: bool = True
    TIME_EXIT_HOURS_BEAR: float = 4.0      # hours before time-stop in BEAR
    TIME_EXIT_PROGRESS_MIN: float = 0.005  # exit if < +0.5% progress

    # v15.15: PortfolioKelly auto-enable at 50+ trades.
    # Kelly needs real historical data to weight strategies by edge.
    # Auto-enables when enough trades are logged; set manually to override.
    PORTFOLIO_KELLY_MIN_TRADES: int = 50
    PORTFOLIO_KELLY_ENABLED: bool = False   # auto-enables at 50 trades

    # v15.12.0: Native SL retry — fixes MOVE FAILED cascade (seen on NEAR trade).
    # Instead of one-shot API call, retries up to SL_MOVE_RETRIES times with delay.
    # Raises native SL success rate from ~85% to ~99%.
    SL_MOVE_RETRIES: int = 3           # max retry attempts per SL move
    SL_MOVE_RETRY_DELAY: float = 2.0   # seconds between retries

    # v15.11.0: Velocity exit — exit early when price crashes fast toward SL.
    # When price is: (a) below entry, (b) above SL, (c) falling fast consecutively,
    # the bot exits at current price instead of waiting for the SL level.
    # Better fill than riding a momentum crash into SL slippage.
    # VELOCITY_EXIT_THRESHOLD: minimum decline rate (%/min) to trigger. 0.4%/min = fast crash.
    # VELOCITY_EXIT_PROXIMITY: only fires when within this % of SL. 1.5% = $97-$98.45 range.
    VELOCITY_EXIT_ENABLED: bool = True
    VELOCITY_EXIT_THRESHOLD: float = 0.004   # 0.4%/min — fast crash threshold
    VELOCITY_EXIT_PROXIMITY: float = 0.015   # within 1.5% of SL

    # v7: Kelly Criterion
    USE_KELLY: bool = False

    # v15.3 AUDIT FIX #4: ERC (Equal Risk Contribution) sizing — opt-in.
    # When TRUE: replaces the 33.33%-of-capital sizing formula in risk.can_trade()
    # with portfolio_alloc.ERCSizing — sizes each position so it contributes equal
    # risk to the portfolio. Better risk distribution but UNTESTED on this account.
    # KEEP FALSE until backtested + paper-traded for at least 50 trades.
    # To enable: set this to True. To revert: set back to False. No restart fights.
    USE_ERC_SIZING: bool = False
    KELLY_FRACTION: float = 0.25  # v14.0: Quarter-Kelly (institutional safe default). Reverted from rogue 1.0 patch (May 13).

    # v7: Regime Detection
    REGIME_ENABLED: bool = True

    # v7: Portfolio Heat
    MAX_HEAT: float = 0.07  # v19.0.2: was 0.05 — raised so both NORMAL slots (2×45% @ up to
                            # MAX_SL_PCT 7%) can fill; 2×0.45×0.07≈0.063 needs headroom. Per-trade
                            # dollar-risk is still bounded by risk-normalize (≤2.5% NORMAL each).
    # v9.8 PER-GROUP GATES: replaces flat 0.8% with per-group thresholds
    # v11.2: Re-calibrated for 5m candles (gates are applied to 5m ATR in bot.py).
    # Old values (0.60–0.80) were 15m-scale and filtered ~all signals on 5m timeframe.
    # New values reflect the natural 5m ATR% range observed in bot_v9.0.log (0.06–0.41%
    # during dead chop, ~0.30–0.60% in normal vol). Used together with FULL/HALF tiering
    # in bot.py: atr_pct < gate*0.7 → SKIP, < gate → HALF size, ≥ gate → FULL size.
    GROUP_ATR_GATES: dict = field(default_factory=lambda: {
        "A": 0.20,  # v11.2: Anchors (BTC/ETH/BNB/SOL) — was 0.60 (15m-scale)
        "B": 0.22,  # v11.2: Large caps — was 0.65
        "C": 0.25,  # v11.2: Mid caps — was 0.70
        "D": 0.30,  # v11.2: DeFi/AI/Memes — was 0.80
    })
    FEAR_HEAT: float = 0.045  # v19.0.2: was 0.03 — let two NORMAL positions at the floor stop
                              # (2×0.45×0.045≈0.0405) still fill during extreme fear, while staying
                              # tighter than MAX_HEAT so wide-stop entries are throttled when scared.

    # v7: Anti-Martingale
    PYRAMID_ENABLED: bool = False  # v8.4: don't add with $53
    PYRAMID_THRESHOLD: float = 0.005  # Add to winners after 0.5%

    # v7: Correlation filter
    CORR_THRESHOLD: float = 0.80  # v11.2.10: lowered from 0.95 — catches real correlation (BTC/ETH ~0.85)

    # v7: Hyperopt
    HYPEROPT_ENABLED: bool = True
    HYPEROPT_INTERVAL_H: int = 168  # Weekly

    # News
    NEWS_ENABLED: bool = True
    NEWS_WEIGHT: float = 0.15
    # v18.9.5: Binance OFFICIAL announcements gate (delisting / halt). Blocks NEW
    # entries only on a managed coin that's being delisted or isn't actively
    # TRADING. FAIL-OPEN — a feed/API outage never blocks trading.
    BINANCE_ANNOUNCE_ENABLED: bool = True
    ANNOUNCE_REFRESH_SEC: int = 900

    # v19.0.3: FOMC/CPI/NFP macro-event hard block (audit: should_block() was computed but
    # NEVER called — bot traded straight through Fed prints). Now wired in _apply_hard_risk_blocks.
    # Blocks NEW entries only, in the EconomicCalendar window (2h before → 30min after). FAIL-OPEN.
    ECON_CALENDAR_BLOCK: bool = True
    # v19.0.3: Token-unlock cliff block (audit: should_block() was never called). Wired now.
    # Only fires when a coin has a KNOWN_UNLOCKS entry within 14 days and ≥5% supply. FAIL-OPEN.
    TOKEN_UNLOCK_BLOCK: bool = True

    # v13.5: pre-event lead time (hours BEFORE event-day start to begin blocking).
    # Default 0 keeps original v13.4 behavior (only blocks last 12h before
    # event-day end). Set to e.g. 6 to block from 6h before event-day midnight
    # UTC, i.e. cover the typical Asia/early-London pre-positioning window
    # ahead of US-time announcements (CPI 12:30 UTC, FOMC 18:00 UTC).
    PRE_EVENT_HOURS: float = 6.0

    # Telegram
    TG_ENABLED: bool = False
    TG_BOT_TOKEN: str = ""
    TG_CHAT_ID: str = ""
    # v15.4 TG upgrades — all opt-in via flags
    TG_INTERACTIVE_ENABLED: bool = True   # /status /pause /resume /positions /force_close
    TG_HEARTBEAT_HOURS: int = 4           # heartbeat interval in hours
    TG_DAILY_SUMMARY_ENABLED: bool = True # 23:55 UTC daily P&L recap
    TG_WEEKLY_SUMMARY_ENABLED: bool = True# Sunday 23:55 UTC weekly rollup
    TG_CHARTS_ENABLED: bool = True        # attach chart PNG to BUY/SELL alerts
    TG_DEDUP_WINDOW_SEC: int = 120        # v18.7.2: suppress byte-identical TG messages within
    #                                       this window (anti-spam). Trade alerts are unique so
    #                                       they're never suppressed. Set 0 to disable dedup.

    ATH_PROTECT: bool = True
    SPLIT_MODE: bool = True
    FIXED_CAPITAL_MODE: bool = False  # v11.2.15: never auto-sync capital from wallet
    MIN_RR: float = 1.35  # v18.9.9 (audit): 1.35 — keeps net expectancy positive when hybrid-maker falls back to taker (was 1.2)
    MIN_CONF: float = 0.80  # audit fix: raised from 0.55 — only high-conviction trades
    SESSION_FILTER: bool = True  # v9.2: trade only in active kill zones (Asia/London/NY)
    MAX_DAILY_TRADES: int = 20  # v11.2.15: unlimited wins, only loss % stops bot
    MAX_DAILY_LOSS_PCT: float = 0.05  # v13.5.5 audit fix: actually set to 5% (was 0.15 with misleading comment)
    # Live expectancy through May 10 2026 is ~$0.097/trade — at 10% daily loss
    # ($7.30 on $73 cap) the bot would burn ~75 trades' worth of edge in a single
    # bad day. May 7 already showed -2.2% in one session (-$1.57). 5% gives ~38 losing
    # trades worth of headroom, which is plenty for normal volatility but caps a
    # genuine bad day before it eats a week of wins.
    SL_TRIGGER_BUFFER: float = 0.003  # v11.2.16: trigger SL 0.3% early — market order fills at intended SL, not 1-2% below
    CIRCUIT_BREAKER_PCT: float = 0.10  # v11.2.2 LIVE: 10% peak DD circuit-breaker for first live week (was 0.15). Will raise back to 0.15 after 7 clean days.
    MAX_SLIP_PCT: float = 0.002  # v10.0 NEW: 0.2% slippage abort threshold (calibrate from telemetry post-deploy)
    MAX_CONSEC_LOSSES: int = 3  # v9.1: was 2, too aggressive with 4 trades/day
    LOSS_PAUSE_MIN: int = 60
    SCAN_SEC: int = 30  # v11.2.11: raised 15→30s — more coins to analyze (~65-70), need extra time for API calls
    MAX_HOLD_MIN: int = 720  # v9.1: 12hrs — Group C/D need more time at 2.0-2.5 R:R
    LOG_FILE: str = "trades_v9.jsonl"  # v9.0: append-only JSONL
    STATE_FILE: str = "bot_state.json"

    def __post_init__(self):
        # v13.3 $200-TIER: override RISK_PCT from feature_flags when tier active.
        # v13.5.3 audit Bug #6: stale .feature_flags.json from a v13.5.1-era
        # install can carry risk_pct=0.07 (the bug Fix #1 fixed in source). On
        # upgrade-to-v13.5.2, the source is correct but the on-disk flag still
        # overrides RISK_PCT. Add a HARD CAP here: even if the flag says 7%,
        # we cap at 2% (the v13.5.2 documented Tier-2 limit). Logs the override
        # so an operator sees the migration happen.
        try:
            from feature_flags import get as _ff
            _risk = _ff("risk_pct", None)
            if _risk is not None:
                try:
                    _risk_f = float(_risk)
                except (TypeError, ValueError):
                    sys.stderr.write(
                        f"\nWARNING: Ignoring invalid feature_flags risk_pct={_risk!r}; "
                        f"using config default {self.RISK_PCT:.4f}.\n\n"
                    )
                else:
                    if _risk_f <= 0:
                        sys.stderr.write(
                            f"\nWARNING: Ignoring non-positive feature_flags risk_pct={_risk_f}; "
                            f"using config default {self.RISK_PCT:.4f}.\n\n"
                        )
                    else:
                        # Feature flags may raise the documented tier risk to 2%,
                        # but must never be able to override live risk to unsafe values.
                        _risk_cap = 0.02
                        if _risk_f > _risk_cap:
                            sys.stderr.write(
                                f"\nWARNING: feature_flags risk_pct={_risk_f:.4f} exceeds "
                                f"safe cap {_risk_cap:.4f}; clamping.\n\n"
                            )
                            _risk_f = _risk_cap
                        object.__setattr__(self, "RISK_PCT", _risk_f)
        except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        self.API_KEY = os.getenv("BINANCE_API_KEY", "")
        self.API_SECRET = os.getenv("BINANCE_API_SECRET", "")
        self.TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
        self.TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
        if self.TG_BOT_TOKEN and self.TG_CHAT_ID: self.TG_ENABLED = True
        # v18.5 AUDIT FIX (D11): every pair was hard-coded group "A", so the per-group
        # GROUP_ATR_GATES (B/C/D) and the Group-D risk controls were dead. Reclassify
        # by liquidity/volatility tier WITHOUT touching the symbol list (zero risk of
        # dropping a pair). Anything not listed below defaults to C (mid-cap).
        # NOTE: Group-D special handling (TREND_UP-only, 15% size) only activates when
        # GROUP_D_ENABLED=True (see risk.can_trade); otherwise D coins trade like C but
        # with a stricter ATR gate (0.30) — strictly safer, never locks coins out.
        _GROUP_A = {"BTC","ETH","BNB","SOL","XRP","ADA","DOT","AVAX","TON","TRX","LTC","BCH","XLM"}
        _GROUP_B = {"NEAR","SUI","ATOM","ICP","APT","HBAR","ALGO","EGLD","VET","INJ","TIA","SEI",
                    "FIL","ETC","NEO","IOTA","ZEC","DASH","QTUM","UNI","AAVE","MKR","RUNE","LDO",
                    "AR","THETA","TAO","RENDER","FET","WLD","IMX"}
        _GROUP_D = {"DOGE","SHIB","PEPE","WIF","FLOKI","BONK","NOT","ORDI","1000SATS","HOT","BEAMX"}
        try:
            for _p in self.PAIRS:
                _n = _p.get("n", "")
                if _n in _GROUP_A:   _p["g"] = "A"
                elif _n in _GROUP_B: _p["g"] = "B"
                elif _n in _GROUP_D: _p["g"] = "D"
                else:                _p["g"] = "C"
        except Exception as _ge:
            sys.stderr.write(f"⚠️  CONFIG: pair group remap failed: {_ge}\n")
        # v10.6 FIX: fail-fast on missing API credentials.
        # Was: empty keys booted silently, WebSocket connected, then first authenticated
        # REST call failed with obscure Binance error. Now: clear error at startup.
        if not self.API_KEY or not self.API_SECRET:
            sys.stderr.write(
                "\n❌ FATAL: BINANCE_API_KEY or BINANCE_API_SECRET not set.\n"
                "   Either:\n"
                "     (a) systemd: ensure /etc/systemd/system/binance-bot-v11.service has\n"
                "         EnvironmentFile=/home/ubuntu/binbot_live/.env and reload daemon\n"
                "     (b) Create .env next to config.py with:\n"
                "           BINANCE_API_KEY=your_key_here\n"
                "           BINANCE_API_SECRET=your_secret_here\n"
                "   Ensure key is mainnet (USE_TESTNET=False) and has trading enabled.\n\n"
            )
            sys.exit(2)


    # v13.6 Feature Flags (default OFF)
    NATIVE_SL_ENABLED: bool = True
    NATIVE_SL_BUFFER_PCT: float = 0.005
    # v13.5.5: Web dashboard (read-only HTTP on :8080) — OFF by default
    WEB_DASHBOARD_ENABLED: bool = False
    WEB_DASHBOARD_PORT: int = 8080
    WEB_DASHBOARD_BIND: str = "127.0.0.1"  # v14.5: localhost only — prevents public exposure
    STRESS_TEST_ENABLED: bool = False
    STRESS_TEST_INTERVAL_H: int = 24
    EXCHANGE_FAILOVER_ENABLED: bool = True
    EXCHANGE_FAILOVER_THRESHOLD: int = 60
    # v18.8.9 SCALE LADDER: at fixed profit levels, SELL a chunk (bank real cash) AND
    # ratchet the SL up. Replaces the v18.8.7 ATR micro-steps that exited for tiny profit.
    # The SL lock at each level keeps profit (so a touched level can't reverse into a loss);
    # the scale-out auto-skips when the slice OR remainder is under the exchange min-notional,
    # so it degrades to a pure profit-lock on a small account. Chase mode still rides past TP.
    # Set PROFIT_LADDER_ENABLED=False to revert to the fixed-% group ladder.
    PROFIT_LADDER_ENABLED: bool = True
    # (trigger %, SL-lock %) above entry. Default: at +1.5% lock BREAKEVEN (entry) so the
    # runner holds through dips above entry and can't lose, then +1.0/+2.0 at +2.5/+3.5.
    # Tighter profit-lock: ((0.015,0.010),(0.025,0.020),(0.035,0.030)). Looser 3% trail:
    # ((0.015,-0.015),(0.025,-0.005),(0.035,0.005)).
    PROFIT_LADDER_LEVELS: tuple = ((0.015, 0.000), (0.025, 0.010), (0.035, 0.020))
    PROFIT_LADDER_SCALE_PCT: float = 0.30     # sell this fraction of remaining qty at each level
    PROFIT_LADDER_MIN_SLICE_USD: float = 5.0  # skip the sell if slice OR remainder < this (Binance min-notional)
    # v18.9.1 SESSION FILTER: only ENTER a coin during its active IST liquidity window.
    # Golden window (peak global volume, validated 13:00-17:00 UTC) is open to ALL coins;
    # coins not in any window may enter ONLY in the golden window. Times are minutes-since-
    # IST-midnight (IST=UTC+5:30); a window whose end < start crosses midnight. 5th field
    # dst=True marks US-anchored windows that auto-shift +60 min during US winter (EST) so
    # they track real US flows year-round (SESSION_DST_AUTOSHIFT). Exits are NEVER gated.
    SESSION_FILTER_ENABLED: bool = True
    SESSION_DST_AUTOSHIFT: bool = True      # shift US-anchored windows +1h during US winter (EST)
    SESSION_GOLDEN: tuple = (1110, 1350)    # 18:30-22:30 IST - open to every coin (US-anchored)
    # (name, start_IST_min, end_IST_min, frozenset(bases), dst_anchored)
    SESSION_WINDOWS: tuple = (
        ("ASIAN",   330,  810,  frozenset({"NEO","QTUM","XRP","ADA","TON","TRX","XLM","ALGO","HBAR","VET","EOS","ONE","ZIL","JASMY","CHZ"}), False),
        ("EUROPE",  810,  1110, frozenset({"DOT","ATOM","APT","SUI","TIA","SEI","POL","ARB","OP","MNT","STRK","IMX","BCH","LTC","ETC","ZEC","XMR","DASH","EGLD","XTZ","FLOW","KAVA","ICP","FTM","MINA","IOTA","METIS","MANTA","ZK","STX","GAS","BTC","ETH","SOL","BNB","AVAX"}), False),
        ("GOLDEN",  1110, 1350, frozenset({"BTC","ETH","SOL","BNB","AVAX"}), True),
        ("US_TECH", 1140, 60,   frozenset({"RENDER","TAO","NEAR","LINK","INJ","IO","FIL","AR","GRT","LPT","THETA","ENS","VANA","HEI","CYBER","MASK","QNT","UNI","CAKE","SUSHI","RAY","ORCA","1INCH","CRV","BAL","BNT","PYTH","ZRO","W","AAVE","MKR","COMP","PENDLE","ONDO","YGG","EUL","CVX","STG","LDO","JTO","RPL","SNX","RUNE","DYDX","FET","WLD","ARKM","ENA","JUP","IOTX","ANKR","STORJ","AUDIO","TRB","WOO","SKL","YFI","TNSR","CELO","BTC","ETH","SOL","BNB","AVAX"}), True),
        ("MEME",    1350, 210,  frozenset({"DOGE","SHIB","PEPE","WIF","FLOKI","BONK","1000CAT","1MBABYDOGE","TRUMP","BOME","MEME","ORDI","1000SATS","SAND","MANA","APE","ALICE","AXS","GALA","ENJ","ILV","PORTAL","BEAM","BEAMX","PIXEL","DAR","CHR","MAGIC","BLUR","SUPER","TWT","GT","CRO","NOT","HOT","GMT","RVN","BAT"}), True),
    )

    @property
    def risk_amount(self): return self.TOTAL_CAPITAL * self.RISK_PCT
    @property
    def max_daily_loss(self): return self.TOTAL_CAPITAL * self.MAX_DAILY_LOSS_PCT
    @property
    def grid_capital(self): return self.TOTAL_CAPITAL * self.GRID_CAPITAL_PCT

# v14.5.1 FIX (audit #9): removed orphan module-level MIN_TRADE = 6.0
# The Config dataclass already has MIN_TRADE as a proper field.


    # v15.14 Upgrades
    GRID_MAX_DRAWDOWN_PCT: float = 0.05
    GRID_MAX_OPEN_LEVELS: int = 4
    GRID_SYNC_INTERVAL: int = 10
    
    DCA_MAX_TOTAL_MULT: float = 4.0
    DCA_MAX_AGE_HOURS: float = 72.0
    DCA_SL_ATR_MULT: float = 1.5
    
    EXPOSURE_GUARD_ENABLED: bool = True
    EXPOSURE_WARN_PCT: float = 0.85

    STOP_LOSS_PCT: float = 0.045  # v18.9.16: widened 3%->4.5% so normal dips don't stop you out before a recovery (risk-normalize keeps $ loss the same)

    # ══════════════════════════════════════════════════════════════════════════
    # v18.5 AUDIT REMEDIATION FLAGS
    # ══════════════════════════════════════════════════════════════════════════
    # ML ensemble (ml.py / MLPredictor). v18.7.1 GodMode: ENABLED by default. bot.py now
    # gates instantiation on this flag, so ML_ENABLED is a real kill-switch — set False to
    # run the pure-TA core. ML only NUDGES signal confidence by at most ±ML_CONF_BOOST and
    # never overrides a hard risk block, so its influence is bounded.
    ML_ENABLED: bool = True
    ML_CONF_BOOST: float = 0.10        # max ± confidence the ML score can shift a signal
    ML_RETRAIN_HOURS: float = 6.0      # min hours between ML retrains

    # Additive intelligence scoring (vol_delta/LOB/VPIN/funding/liquidation/momentum/
    # coin-profile/news/long-short/open-interest confidence nudges + journal strategy_weight). AUDIT
    # found this entire block was dead — nested behind the never-instantiated ML gate.
    # It is now reachable but kept OFF by default: enabling it changes signal
    # confidence on every trade and MUST be validated on history first. The hard
    # safety blocks (should_block) from these modules run regardless of this flag.
    INTEL_SCORING_ENABLED: bool = True

    # Adaptive R:R (audit D7). The per-regime R:R logic in strategies.analyze was inert:
    # base_rr of 0.5 for A/B/C produced sub-1.0 targets always overridden by the TP floor
    # in _sig(), so every A/B/C trade used a fixed ~1.5:1 regardless of regime. When this
    # is True, sensible per-group base R:Rs are used so the regime modulation actually
    # moves the target (TREND_UP→full, RANGE/CHOPPY→tighter). OFF by default → byte-
    # identical to v18.4 (fixed 1.5:1). Backtest before enabling — it changes every TP.
    ADAPTIVE_RR_ENABLED: bool = False
    BASE_RR_BY_GROUP: dict = field(default_factory=lambda: {"A": 2.0, "B": 2.0, "C": 1.8, "D": 5.0})
    MIN_TP_RR_FLOOR: float = 1.5   # hard floor on TP R:R in _sig() (keeps rank() MIN_RR satisfied)

    # v18.7.1: PAPER_TRADING removed entirely — this build is REAL-MONEY ONLY.

    # Feature-health table: print a per-module ACTIVE/INACTIVE status at startup so
    # an operator can SEE which intelligence modules are actually running.
    FEATURE_HEALTH_ENABLED: bool = True

    # Gate-rejection telemetry: count WHY signals are rejected (conf/rr/corr/heat/
    # cooldown/regime) and log a periodic histogram. Helps tune MIN_CONF etc.
    GATE_TELEMETRY_ENABLED: bool = True
    GATE_TELEMETRY_EVERY_CYCLES: int = 120

    # Dead-man's-switch: if the bot goes price-blind (no WS + no REST prices) for
    # DEADMAN_STALE_SEC while holding open positions, take DEADMAN_ACTION.
    #   "alert"   → loud Telegram + log only (default, safest — native SL still guards)
    #   "flatten" → also attempt to market-close all positions
    DEADMAN_ENABLED: bool = True
    DEADMAN_STALE_SEC: int = 150
    DEADMAN_ACTION: str = "alert"

    # Grid safety gate (audit C3): the Grid engine has a documented state-desync bug
    # (see GRID_ENABLED note above). CapitalActivator used to auto-enable Grid at
    # $500 regardless. v18.5 requires this explicit opt-in before Grid can ever be
    # auto-enabled. Leave False until GridEngine.check() verifies exchange success.
    GRID_SAFE: bool = False

    # Micro-price feed: @bookTicker was removed from the WS to save RAM, which left
    # the MICRO_PRICE strategy with no data. v18.5 restores the feed via a periodic
    # REST bookTicker poll instead (no WS spam). Set 0 to disable the strategy.
    MICRO_PRICE_POLL_CYCLES: int = 2      # poll best bid/ask every N cycles (0=off)
    MICRO_PRICE_POLL_TOP_N: int = 30      # only poll the top-N pairs (API budget)

    # Native-SL orphan reconciler cadence (audit D10 — was never called).
    NATIVE_SL_RECONCILE_CYCLES: int = 20  # run reconcile_orphans every N cycles (0=off)

    def validate(self):
        """v18.5: lightweight self-check. Logs (does not raise) on suspicious or
        no-op configuration so an operator notices misconfiguration at boot."""
        import sys as _sys
        warns = []
        if self.RISK_PCT > 0.02:
            warns.append(f"RISK_PCT={self.RISK_PCT} > 2% safe cap")
        if not (0 < self.MAX_EXPOSURE <= 1.0):
            warns.append(f"MAX_EXPOSURE={self.MAX_EXPOSURE} out of (0,1]")
        if self.MIN_RR < 1.0:
            warns.append(f"MIN_RR={self.MIN_RR} < 1.0 (negative expectancy after fees)")
        if self.MIN_CONF > 0.85:
            warns.append(f"MIN_CONF={self.MIN_CONF} very high — may starve the bot of trades")
        # v18.9.10: small-tier risk budget should stay a controlled notch above the normal cap
        if not (0 < self.SMALL_TIER_RISK_PCT <= 0.05):
            warns.append(f"SMALL_TIER_RISK_PCT={self.SMALL_TIER_RISK_PCT} out of (0, 0.05] — single-trade loss could be excessive")
        if self.MAX_POSITIONS < 1:
            warns.append(f"MAX_POSITIONS={self.MAX_POSITIONS} < 1 — bot cannot open trades")
        if self.GRID_ENABLED and not self.GRID_SAFE:
            warns.append("GRID_ENABLED=True but GRID_SAFE=False — Grid has a known desync bug")
        if self.DEADMAN_ACTION not in ("alert", "flatten"):
            warns.append(f"DEADMAN_ACTION={self.DEADMAN_ACTION!r} invalid (use 'alert'|'flatten')")
        # v18.9.9 (audit): cover the values the capital-tier manager mutates live + size/SL sanity
        if not (0 < self.POSITION_SIZE_PCT <= 1.0):
            warns.append(f"POSITION_SIZE_PCT={self.POSITION_SIZE_PCT} out of (0,1]")
        if not (0 < self.SMALL_TIER_SIZE_PCT <= 1.0):
            warns.append(f"SMALL_TIER_SIZE_PCT={self.SMALL_TIER_SIZE_PCT} out of (0,1]")
        if not (0 < self.SMALL_TIER_EXPOSURE <= 1.0):
            warns.append(f"SMALL_TIER_EXPOSURE={self.SMALL_TIER_EXPOSURE} out of (0,1]")
        # v19.0.2 (audit LOW-1): the NORMAL_* fields the tier manager mutates live had no bounds.
        # _switch() clamps size/exposure to [0,1], but NORMAL_RISK_PCT is unclamped anywhere — a
        # fat-finger (e.g. 0.25) would silently 10× the per-trade dollar-risk ceiling in can_trade.
        if not (0 < self.NORMAL_RISK_PCT <= 0.05):
            warns.append(f"NORMAL_RISK_PCT={self.NORMAL_RISK_PCT} out of (0, 0.05] — single-trade loss could be excessive")
        if not (0 < self.NORMAL_SIZE_PCT <= 1.0):
            warns.append(f"NORMAL_SIZE_PCT={self.NORMAL_SIZE_PCT} out of (0,1]")
        if not (0 < self.NORMAL_EXPOSURE <= 1.0):
            warns.append(f"NORMAL_EXPOSURE={self.NORMAL_EXPOSURE} out of (0,1]")
        # v19.1.0 profitability-feature bounds
        if not (0 <= self.MIN_NET_EDGE <= 0.05):
            warns.append(f"MIN_NET_EDGE={self.MIN_NET_EDGE} out of [0, 0.05] — too high will starve entries")
        if not (0 <= self.SLIPPAGE_BUF <= 0.02):
            warns.append(f"SLIPPAGE_BUF={self.SLIPPAGE_BUF} out of [0, 0.02]")
        if not (0 < self.VOL_TARGET <= 0.10):
            warns.append(f"VOL_TARGET={self.VOL_TARGET} out of (0, 0.10]")
        if self.VOL_SCALAR_MAX < self.VOL_SCALAR_MIN:
            warns.append(f"VOL_SCALAR_MAX={self.VOL_SCALAR_MAX} < VOL_SCALAR_MIN={self.VOL_SCALAR_MIN}")
        if self.MAX_SL_PCT < self.STOP_LOSS_PCT:
            warns.append(f"MAX_SL_PCT={self.MAX_SL_PCT} < STOP_LOSS_PCT={self.STOP_LOSS_PCT} (ceiling below floor)")
        for w in warns:
            _sys.stderr.write(f"⚠️  CONFIG: {w}\n")
        return warns
