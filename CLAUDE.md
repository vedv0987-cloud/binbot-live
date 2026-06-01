# CLAUDE.md — BinBot navigation map

> **Read this first, every session. Do NOT scan the whole codebase.** This file tells you
> exactly which file (and line) owns each concern, so you edit ~50 lines instead of reading
> 18,000. For line-level indexes of the big files, see `ARCHITECTURE.md`.

## What this is
A **live, real-money** Binance spot trading bot (single account). Runs on an Oracle VM under
systemd (`binance-bot-v11.service`), entry point `main.py` → `bot.ProBotV11.run()`. Version
**v18.9.x**. ~18.9k lines across flat modules (one concern per file — no packages).

## ⚠️ Operating rules (non-negotiable)
1. **Real money.** Changes can lose funds. Be conservative; never weaken risk controls without being asked.
2. **Use this map — don't re-read everything.** Open only the file(s) for the task. `ARCHITECTURE.md` has line ranges so you can `Read` with `offset`/`limit`.
3. **Test gate before every push:** `BINANCE_API_KEY=dummy BINANCE_API_SECRET=dummy python -m unittest test_core` (35 tests) + `python -m py_compile *.py`. This is the same gate `deploy.sh` enforces.
4. **Config is the single source of truth** for tunables — change flags/thresholds in `config.py`, not scattered in code.
5. **State is gitignored on purpose** (`.env`, `bot_state.json`, `*.jsonl`, `*.pkl`). A `git pull`/`reset --hard` only ever touches code, never live state. Don't commit those.
6. **Version bump convention:** user-facing behavior changes bump the version string + add a one-line note (see recent `git log` style: `v18.9.x: <what changed>`).

## File map — where each concern lives
| Concern | File | Key classes / entry |
|---|---|---|
| **Orchestrator / main loop** | `bot.py` | `ProBotV11` (one class). `run()` boot+loop, `_cycle()` is the per-tick trading loop, `_apply_hard_risk_blocks()`, `_adopt_orphan()` |
| **Risk / sizing / exits** | `risk.py` | `Risk`: `can_trade()` (entry gate+size), `open_pos()`, `check_exits()` (SL/TP/trail/ladder), `check_pyramid()`, `_record_close()` |
| **Strategies / signals** | `strategies.py` | `Strategies` (signal generation), `GridEngine`, `DCA`, `Backtester` |
| **ML / RL ensemble** | `ml.py` | `MLPredictor` (RF+GB+LGBM+XGB ensemble), `RLAgent`, `LSTMPredictor` (off by default), many data-source helpers |
| **Intelligence (scoring, funding, liq, news)** | `intelligence.py` | `Intel` (aggregator), `FundingRateTracker`, `LiquidationDetector`, `SmartCoinDetector`, `AzureOpenAIIntelligence`, `EconomicCalendar` |
| **Exchange I/O** | `exchange.py` | `Exchange` (orders/balances/klines), `LivePrices` (WS feed) |
| **Native SL/TP on Binance** | `exchange_native_sl.py` | `NativeSLManager`, `NativeTPManager` |
| **Indicators / TA** | `indicators.py` | `TA` (all indicators, numba-accelerated) |
| **Analytics / journal / drawdown** | `analytics.py` | `Analytics`, `TradeJournal`, `DrawdownShield`, `SelfHealer`, `PairRotator` |
| **Monitors (calendar, kelly, whales…)** | `monitors.py` | `KellySizer`, `EventCalendar`, `TokenUnlockMonitor`, `HyperOptimizer`, etc. |
| **State persistence** | `state.py` | `StateManager` (loads/saves `bot_state.json`) |
| **Data models** | `models.py` | `Candle`, `Position`, `Signal`, `Context`, `PendingBuy` |
| **Position reconcile (ghosts/orphans)** | `reconciler.py` | `PositionReconciler` |
| **Config (ALL flags/thresholds)** | `config.py` | `Config` |
| **Telegram** | `telegram.py`, `telegram_commands.py`, `telegram_charts.py` | `Telegram`, command handlers |
| **Portfolio / exposure** | `portfolio_alloc.py` | `ExposureGuard` |
| **Order flow / microstructure** | `orderflow.py`, `lob_imbalance.py`, `local_orderbook.py`, `micro_price.py` | flow/LOB trackers |
| **External watchdog** | `watchdog.py` | heartbeat-restart + equity-floor kill switch (systemd timer) |
| **Boot reconcile script** | `pre_start.sh` | DD anchor fix + ghost/orphan reconcile (runs as `ExecStartPre`) |

## "I want to change X → edit Y"
| Task | Where |
|---|---|
| Tune a threshold/flag (risk %, max positions, sessions, ML boost…) | `config.py` (`Config`) — first |
| Add/modify an **entry strategy** | `strategies.py` (`Strategies`), then ensure it's scored/consumed in `bot._cycle()` |
| Change **stop-loss / take-profit / trailing / scale-out ladder** | `risk.py` → `check_exits()` |
| Change **position sizing / entry gating** | `risk.py` → `can_trade()` |
| Add an **ML model** or change ensemble weighting | `ml.py` → `MLPredictor` |
| Change **signal scoring / intel weighting** | `intelligence.py` → `Intel` |
| Order placement / fills / API behavior | `exchange.py` → `Exchange` |
| SL/TP orders held on Binance | `exchange_native_sl.py` |
| Telegram alerts/commands | `telegram.py` / `telegram_commands.py` |
| Boot-time recovery of positions | `pre_start.sh` + `bot._adopt_orphan()` |

## Dev workflow
- **Branch:** work on `claude/binbot-continuation-Nob66` or `main` (both Claudes + the VM share `main` as source of truth). Always `git fetch origin main` before editing; push immediately after.
- **Gate:** run the unittest gate above before pushing. Don't push red.
- **Apply on VM:** code change → `bash deploy.sh` (fetch+gate+restart, auto-rollback on failure). Non-code change (docs/gitignore) → `git fetch origin main && git reset --hard origin/main` (no restart needed).
- **Fresh VM:** see `SETUP.md` (`bootstrap.sh` + `backup.sh`).

## Coordination (two Claudes + VM)
`main` is the single source of truth. One change at a time; whoever pushes reports the commit hash; the other pulls before starting. Never both edit the same file simultaneously.
