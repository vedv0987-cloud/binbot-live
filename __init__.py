"""BinBot v13.5.3 — Institutional-Grade Crypto Trading Bot

v13.5.3 = v13.5.2 + post-deploy second-pass audit (May 10, 2026, evening).
A thorough line-by-line review of v13.5.2 found 9 critical/high bugs that
slipped past the first audit. This pass closes them. Bot was running clean
on v13.5.2 throughout — none of these were causing visible failures, but
they were either silently degrading safety systems or actively bleeding
small amounts of money. All fixes verified against live trade data.

  CRITICAL — Auto-upgrade safety system was broken
  • Bug #1 upgrade_engine.py log() TypeError: log() took 1 arg but lines
    270/273 passed 2. Crashed silently inside except blocks → operator
    never saw the real error during a tier rollback.
  • Bug #2 auto_upgrade.py soak crash detection: was case-sensitive grep
    for "Started binbot" but systemd emits "Started BinBot" → crash_count
    permanently 0 → auto-rollback-on-crash NEVER fired. Defeated the
    entire safety story of soak watchdog.
  • Bug #3 auto_upgrade.py tests run with system python: lacks sklearn/
    lightgbm/xgboost/catboost → tests always failed → patches always
    rolled back → no tier upgrade could ever commit on this VM.

  CRITICAL — Money/safety leaks already firing in production
  • Bug #21 analytics.strategy_performance counted BUY entries (pnl=0)
    as losses → every strategy's WR halved → strategy_weight() over-
    penalized winners. Validated against live data: SMC_OB true close-
    only WR is 90.9% (not the 45% the buggy method showed).
  • Bug #22 bot.py logger named "pro-v9.0" while 16 other modules use
    "binbot" → bot_v9.0.log captured ~33% of activity. Operators
    debugging from the file alone got a misleading picture.
  • Bug #25 pos._sell_fails was dynamic attr, dropped by asdict() on
    state.save → 3-strike force-close path could never accumulate
    strikes across reboots → stuck positions leaked indefinitely.
  • Bug #41 ML OutlierDetector state lost on every restart → safety
    system silently disabled for 6h post-restart on every reboot.
  • Bug #42 B2-7 synthesized closes double-charged fees: realized loss
    on every GHOST/CRASH_STUCK/FORCE_CLOSE/DUST event was 2× the real
    cost. Bleeding ~$0.04 per stuck-coin event; ~$0.15 lost so far.

  CRITICAL — Stale-flag migration safety
  • Bug #6 stale .feature_flags.json from a v13.5.1-era install can
    carry risk_pct=0.07 (the source-bug v13.5.2 Fix #1 fixed). Source
    is now correct but disk flag still overrides at boot. Added a hard
    cap in config.__post_init__: any flag-source RISK_PCT > 0.02 is
    clamped to 0.02 with a stderr migration notice.

  HIGH — Robustness improvements
  • Bug #7/#8 exchange.py partial-fill detection: was using follow-up
    get_asset_balance which had eventual-consistency races and crashed
    on None. Now uses executedQty from the order response itself
    (authoritative, atomic).
  • Bug #5/#51 main.py + bot.py argparse: --capital with no value or
    bad value now exits cleanly with stderr message instead of
    IndexError/ValueError traceback.
  • Bug #4 audit_wallet.py: added SCALE and CLOSE to close-action set
    (was missing → false ORPHAN flags if scale-out ever re-enabled).
  • Bug #10 auto_upgrade.py: 4× FD leaks in patch subprocess calls
    now use with-blocks.
  • Bug #29 coin_profile.py: non-atomic write replaced with tmp+
    os.replace (months of per-coin learning could vanish if killed
    mid-write).
  • Bug #30 ml.py: ml_models.pkl path anchored to module dir (was
    relative — broke for any side-script run from a different cwd).
  • Bug #15 systemd: Description bumped from v13.2 to v13.5.3
    (also fixes Bug #2's substring grep target).

  Operation B baked in
  • bot.py:411 the "QFL_PANIC, SQUEEZE_BREAK" exemption was sed-applied
    on the running VM only and would revert on every redeploy. Now
    SMC_OB / SMC_SWEEP / WYCKOFF_ACC are protected in source, in a
    named constant _PROTECTED_FROM_BACKTEST. Live data justifies
    protecting them: SMC_OB 90.9% WR / +$3.55, SMC_SWEEP 100% / +$1.12,
    WYCKOFF_ACC 50% / +$0.48 net positive.

Inherited from v13.5.2:
v13.5.2 = v13.5.1 + 13 audit fixes covering source-bugs, latent failures,
and tooling inconsistencies. SMC_OB+FVG hard-disabled (Fix #5), SL floor
synced at 3% (Fix #6), MAX_DAILY_LOSS_PCT tightened 10%->5% (Fix #7),
audit_wallet.py added (Fix #10).

Inherited from v13.5.1: 9 first-pass audit fixes (RISK_PCT 7->1%,
KELLY 1.0->0.25, telegram BE math, BE-lock SL +0.45%, regime_v2 remap).

Inherited from v13.5: mojibake repaired + ALL Batch 2 patches pre-applied.

Operator note: deploying v13.5.3 onto an existing v13.5.2 install is
straightforward (no schema changes, no model retrains needed). Run
audit_wallet.py before AND after deploy to confirm wallet state. The
.feature_flags.json migration in config.py is automatic and safe."""

__version__ = "19.0.0"
__engine__ = ("V19.0 GodMode — REAL-MONEY ONLY (no paper). Auto capital-tiers by live "
              "balance (<$50 → 1 pos/90%, ≥$50 → 2 pos/33%), WYCKOFF BEAR filter, Telegram "
              "anti-spam dedup. On the v18.7.1 audit-hardened base: ML + intel scoring ON, "
              "FOMC/unlock blocks live, fee-accurate accounting, WS self-heal, feature-health.")
# ── v18.5 (2026-05-29) full audit remediation ────────────────────────────────
# A line-by-line institutional audit found a large set of "advertised but inert"
# subsystems plus several money-path correctness bugs. v18.5 fixes them one by one:
#   CORRECTNESS
#     • open_pos fee rate now follows the ACTUAL fill type (maker vs taker market
#       fallback) instead of the USE_LIMIT flag — stops PnL over-statement.
#     • open_pos same-pair duplicate guard (defends async double-open race).
#     • bot.py no longer rewrites its own config.py at runtime (corruption risk).
#     • self.lob double-assignment removed (LocalOrderBook vs LOBImbalanceTracker).
#   DEAD-FEATURE REVIVAL (made real)
#     • Micro-price stat-arb fed again (REST bookTicker poll — @bookTicker WS was
#       removed for RAM; poll restores the data without the spam).
#     • FOMC/CPI EconomicCalendar + TokenUnlock risk blocks instantiated & live.
#     • PortfolioKelly auto-enable counter fixed (wins+losses, was reading two
#       attributes that never existed → always 0).
#     • Native-SL orphan reconciler wired on a timer.
#     • WebSocket self-heals internally (reconnect loop) instead of dying.
#     • strategies adaptive R:R is honored instead of being pinned to 1.5:1.
#   SAFELY-DEFERRED (wired, default OFF — backtest before enabling)
#     • Full ML ensemble behind cfg.ML_ENABLED.
#     • Grid no longer auto-enables at $500 (known desync bug) — needs GRID_SAFE.
#   NEW
#     • feature_health.py startup status table (no more silent None modules).
#     • Gate-rejection telemetry, dead-man's-switch.
#   v18.7.1: paper/shadow mode REMOVED — real-money trading only.
#   CLEANUP
#     • Deleted patch-script cruft (fix_coroutine.py, auto_sync.py, temp_ws.py).
# v15.2 fixes (May 23, 2026):
#   #1 Adaptive limit reposting tightened — 10min interval + 0.5% price-move gate
#      + 3-repost cap → MAX 5 API calls per order across its lifetime
#   #1 Prometheus gauges updated every 5 cycles in _cycle status block
#   #2 Audit log calls wired in risk.open_pos (ENTRY) + risk._record_close (EXIT)
#   #3 Stale .bak / patch_*.py files removed from build
#   #4 StatArb/Kalman/SpotPerp replaced by _NullBlocker stub (saves 50KB init)
#
# v15.0/15.1 modules (still present and wired):
# v15.0 modules:
#   tca.py            — Transaction Cost Analysis (Gap #3 — Slip/R/MFE/MAE)
#   risk_metrics.py   — Sortino, Calmar, Ulcer, Tail Ratio, Common Sense
#   audit_log.py      — SHA-256 hash-chained audit (auto-wired in bot.run)
#   prom_metrics.py   — Prometheus :9090/metrics (auto-wired in bot.run)
#   bayesian_opt.py   — GP-UCB Bayesian hyperopt
#   regime_backtest.py — Per-strategy × per-regime matrix
#   v15_report.py     — Weekly report CLI
#
# Wired in bot.py:
#   #1 Execution: adaptive limit reposting (30s repost-while-pending, 5min cap)
#                 size jitter ±5% on every entry (anti-detection)
#   #2 Microstructure: LOB depth raised 20 → 100 levels (lob_imbalance.py)
#   #8 Observability: Prometheus exporter on :9090
#   #9 Compliance: hash-chained audit log
#   Gap #2 fixed: StatArb/Kalman/SpotPerp dead-weight disabled
#   Gap #3 fixed: TCA wired into open_pos + _record_close
#
# Not implementable on retail spot-only Binance:
#   #3 Cash yield: Simple Earn (region-locked for user)
#
# v15.1 NEW modules (implemented per user request):
#   triangular_arb.py  — Spot-only 3-leg arb scanner (USDT→BTC→ETH→USDT).
#                        scan() returns profit opportunities. execute() is
#                        DRY-RUN skeleton — wire fully before live use.
#                        Profit threshold: 10bps net after 3× taker fees.
#   portfolio_alloc.py — Three institutional allocators:
#                        • PortfolioKelly  — strategy-weighted Kelly using TCA history
#                        • ERCSizing       — Equal Risk Contribution per-position sizes
#                        • MVOPairSelector — Mean-variance pair selection (non-correlated)
#                        All opt-in; instantiate where needed in bot.py.
#
# Run weekly report: python3 v15_report.py [days] [regime]
# v15.0 New modules:
#   tca.py            — Transaction Cost Analysis (slip/fee/R-multiple/MFE/MAE per trade)
#   risk_metrics.py   — Sortino, Calmar, Ulcer, Tail Ratio, Common Sense Ratio
#   audit_log.py      — SHA-256 hash-chained audit log (tamper-evident)
#   prom_metrics.py   — Prometheus-style metrics exporter on :9090
#   bayesian_opt.py   — GP-UCB Bayesian hyperparameter optimization
#   regime_backtest.py — Per-strategy × per-regime backtest matrix
#   v15_report.py     — Weekly performance + audit-chain verify + regime matrix
#
# Wired into:
#   risk.py:open_pos     → TCA entry capture
#   risk.py:_record_close → TCA exit capture (R-multiple, MAE/MFE)
#   analytics.py:Analytics → 7 new institutional metrics
#
# Optional opt-in (not wired by default to avoid touching live execution paths):
#   audit_log     → manually instantiate in bot.py if compliance needed
#   prom_metrics  → manually start exporter; scrape with Prometheus
#   bayesian_opt  → swap into monitors.HyperOptimizer when ready
#   regime_backtest → run from CLI: python3 v15_report.py 30 regime
#
# v15.0 Gap #2 fix: StatArb/Kalman/SpotPerp dead-weight modules disabled.
#   These classes computed Z-scores/basis but couldn't capture the spread on
#   spot-only Binance (need futures shorts). Their .update() calls and the
#   spot_perp additive boost are commented out in bot.py — saves ~200ms/cycle
#   and ~50 API calls/hour. Classes remain importable; SpotPerpBasisTracker
#   .should_block() stays wired as a safety net (no-op without fresh data).
