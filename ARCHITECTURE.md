# ARCHITECTURE.md — line-level index of the big files

> Companion to `CLAUDE.md`. Use these line ranges to `Read` only the section you need
> (`Read` with `offset`/`limit`) instead of loading whole files. Line numbers drift as code
> changes — re-grep `^class `/`    def ` if something looks off, and update this file when you
> move things materially.

## bot.py — `ProBotV11` (one ~3,260-line class, the orchestrator)
| Lines | Member | Purpose |
|---|---|---|
| 6 | `_heal_memory()` (module fn) | GC/memory helper |
| 169 | `class ProBotV11` | the bot |
| 170–365 | `__init__` | wires every subsystem (Exchange, Risk, ML, Intel, Strategies, monitors, telegram) |
| 366–400 | `_cache_*`, `_gate_reject` | disk cache + gate-telemetry helpers |
| 401–450 | `_detach_native_sl_before_sell` / `_restore_native_sl_after_failed_sell` | native-SL safety around sells |
| 451–561 | `_apply_hard_risk_blocks` | hard blocks before any entry (events, unlocks, regime…) |
| 562–617 | `_adopt_orphan` | adopt a wallet coin the bot isn't tracking |
| **618–1155** | `run()` | boot sequence + main async loop wiring |
| **1156–3047** | `_cycle()` | **the per-tick trading loop** (scan → score → gate → enter/exit). The hot path; biggest method by far |
| 3048–3147 | `_handle_hybrid_maker`, `_wait_for_limit_fill`, `_cancel_and_check`, `_build_filled_result` | maker-order execution helpers |
| 3148–3167 | `_async_retrain_ml`, `_async_retrain_hyperopt` | background retrains |
| 3168–3261 | `_stop`, `_async_shutdown`, `_summary` | shutdown + summary |

## risk.py — `Risk` (one ~1,715-line class)
| Lines | Member | Purpose |
|---|---|---|
| 17–116 | `__init__` | risk state, positions, counters |
| 117–162 | `save_state`, `get_and_clear_partials`, `record_equity` | persistence + equity tracking |
| 163–283 | `equity_size_mult`, `available`, `wr`, `portfolio_heat`, `paused`, `_reset`, `_us_winter_shift`, `_session_ok` | sizing/session/heat helpers |
| **284–567** | `can_trade(sig, fg)` | **entry gate + position sizing** (capital tier, exposure, session, heat) |
| 568–594 | `volatility_adjusted_size` | ATR-scaled sizing |
| 595–776 | `open_pos` | open a position (records, attaches SL/TP) |
| 777–812 | `check_pyramid` | pyramiding into winners |
| **813–1443** | `check_exits(tickers, ctx, ex, tg)` | **SL / TP / trailing / profit-ladder / scale-out** — exit logic |
| 1444–1606 | `_record_close` | close accounting (fees, PnL, journal) |
| 1607–1651 | `_log_trade` | trade logging |
| 1652–1715 | `status`, `remove_position_safe` | status + safe removal |

## ml.py — many classes (~1,890 lines)
Primary: **`MLPredictor` (1165–1757)** = the RF+GB+LGBM+XGB ensemble (predict + train); **`RLAgent` (1758+)**.
Data-source helpers (each small, self-contained): `DXYCorrelation` 77, `WhaleOnChain` 118,
`MultiExchangeFlow` 163, `OptionsSentiment` 240, `TransformerNLP` 291, `MetaLearner` 343,
`MonteCarloSim` 379, `CoinGeckoTrending` 426, `CoinGeckoMovers` 472, `SocialSentiment` 541,
`ExchangeFlowEstimator` 604, `LongShortRatio` 669, `OpenInterestTracker` 737, `HashRateMonitor` 818,
`ModelSelector` 900, `DashboardExporter` 939, `OutlierDetector` 967, `_LSTMNet`/`LSTMPredictor` 1022/1039 (LSTM off unless `BINBOT_ENABLE_LSTM=1`).

## intelligence.py — many classes (~1,410 lines)
Aggregator: **`Intel` (1206–1326)** — combines the trackers into a score/context.
Trackers: `FundingRateTracker` 19, `LiquidationDetector` 104, `SmartCoinDetector` 190,
`CryptoPanicNews` 306, `MomentumScanner` 427, `LiquidationCascadeTracker` 513,
`SpotPerpBasisTracker` 631, `StatArbSignal` 716, `FinBERTSentiment` 766, `VPINTracker` 832,
`KalmanPairsSpreader` 936, `TokenUnlockTracker` 1043, `EconomicCalendar` 1114.
`Context` 1195 (intel context), `AzureOpenAIIntelligence` 1327 (LLM news sentiment, optional).

## strategies.py (~620 lines)
`GridLevel` 11, `GridEngine` 14–136, `DCA` 137–181, **`Strategies` 182–455** (signal generation —
the main entry-signal source), `Backtester` 456+.

## exchange.py (~860 lines)
`BinanceAPIError` 17, **`Exchange` 26–743** (orders, balances, klines, account), `LivePrices` 744+ (WS price feed).

## exchange_native_sl.py (~490 lines)
`NativeSLManager` 51–377 (places/links stop orders on Binance), `NativeTPManager` 378+.

## analytics.py (~680 lines)
`SelfHealer` 8, `TradeJournal` 106, `DrawdownShield` 211 (drawdown shield / peak anchor),
`PairRotator` 435, `Analytics` 551.

## Smaller files (whole-file is cheap to read)
`config.py` (`Config`, all flags) · `state.py` (`StateManager`) · `models.py` (dataclasses) ·
`reconciler.py` (`PositionReconciler`) · `monitors.py` (Kelly/calendar/whale monitors) ·
`portfolio_alloc.py` (`ExposureGuard`) · `telegram*.py` · `watchdog.py` · `tca.py` ·
`orderflow.py`, `lob_imbalance.py`, `local_orderbook.py`, `micro_price.py`.

## Boot & ops scripts
`main.py` entry · `pre_start.sh` boot reconcile (ExecStartPre) · `deploy.sh` update+gate+restart ·
`bootstrap.sh` fresh-VM setup · `backup.sh` state snapshot · `*.service`/`*.timer` systemd units.
