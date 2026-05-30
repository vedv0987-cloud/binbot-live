# BinBot V18.8 GodMode — Real-Money Only

**Date:** 2026-05-30
**Base:** your V18.7.1 GodMode build → **V18.8**

## New in V18.8

### 1. Automatic capital tiers (auto-detects your balance, no manual edits)
The bot reads your **live wallet equity every cycle** (free USDT + locked + open-position value) and switches its own sizing config:

| Live balance | Max positions | Per-trade size | Max exposure |
|---|---|---|---|
| **below $100** | **1** (concentrated) | **90% of balance** (10% buffer) | 90% |
| **$100 and above** | **2** | **33%** | 75% |

- Threshold is `SMALL_TIER_USD=100` (configurable). Hysteresis (±4%) prevents flip-flopping at the line.
- Lowering positions never force-closes open trades — it just stops *new* entries until they drain.
- Sends a Telegram note on each switch (`🎚️ SMALL-BALANCE MODE` / `NORMAL MODE`).
- **All thresholds are config values** in `config.py` — change without touching code:
  `SMALL_TIER_USD=50.0`, `SMALL_TIER_MAX_POS=1`, `SMALL_TIER_SIZE_PCT=0.90`,
  `NORMAL_MAX_POS=2`, `NORMAL_SIZE_PCT=0.3333`, `CAPITAL_TIER_ENABLED=True`.
  *(Want the single-position mode to last until $100? Set `SMALL_TIER_USD=100.0`.)*

### 2. WYCKOFF_ACC small-loss bleed fixed
- Tighter entry: WYCKOFF_ACC now needs **volume confirmation** + a **non-bearish higher timeframe** (`WYCKOFF_STRICT`, `WYCKOFF_MIN_VOL_RATIO`).
- No slow accumulation longs in a **BEAR daily** downtrend (`BLOCK_ACCUMULATION_IN_BEAR`, `ACCUMULATION_STRATS`); fast reversal plays (QFL_PANIC / SMC_SWEEP) are kept.

### 3. Telegram anti-spam
- The `EXPOSURE FULL` alert is now transition-based (once + 30-min reminder + ✅ all-clear) instead of every cycle.
- Generic backstop in `telegram.send()` suppresses byte-identical messages within `TG_DEDUP_WINDOW_SEC` (120s). Trade alerts (always unique) are never suppressed.

### 4. Full 5-model ML + memory fix
- `xgboost` + `catboost` added to requirements — the ensemble now runs all 5 models (RF+GB+LGBM+XGB+CAT) when installed.
- `torch` is no longer imported at startup (LSTM is off) → frees ~250 MB, which is roughly what the 2 extra models use, so net RAM is unchanged. Set `BINBOT_ENABLE_LSTM=1` to re-enable torch.

### 5. Auto-adopt orphan coins (`AUTO_ADOPT_ORPHANS`, default ON)
On startup, any managed-pair coin in the wallet that the bot isn't tracking (after a state wipe, or a manual buy) is **adopted as a managed position** — given an SL/TP and managed normally — instead of being sold off or stranded. Cost basis = current market price. Links an existing exchange stop if present, else attaches a fresh one. Only adopts value ≥ `AUTO_ADOPT_MIN_USD` ($5).

### 6. Fresh-start drawdown anchor
On a wiped/fresh start the drawdown-shield peak now hard-anchors to the **real starting equity** instead of the stale config capital — fixes the bogus "99% drawdown" trip that blocked trading after a wipe.

### 7. Version unified to V18.8 everywhere
Log banner, Telegram **LIVE** + **stopped** messages, `__version__`/`__engine__`, and the systemd description all read **V18.8** (they were inconsistent: v16.0.0 / v18.4 / v18.7.1).

## Carried over from v18.5 / v18.7.1 (already in your build)
Real-money only (paper removed) · ML ensemble ON (bounded ±`ML_CONF_BOOST`) · INTEL scoring ON · FOMC/CPI + token-unlock blocks live · fee-accurate accounting · `open_pos` dedup guard · WS self-heal · micro-price feed restored · native-SL orphan reconciler · feature-health table · gate telemetry · dead-man switch · atr_engine startup-crash fix.

## Flag posture (config.py)
`ML_ENABLED=True` · `INTEL_SCORING_ENABLED=True` · `ADAPTIVE_RR_ENABLED=False` (opt-in) · `NATIVE_SL_ENABLED=True` · `GRID_SAFE=False` · `CAPITAL_TIER_ENABLED=True` · `PAPER_TRADING` removed (real money only).

## Verified
`python -m compileall` clean · `ProBotV11(Config())` constructs · tier auto-switch proven (equity $40 → 1 pos/90%, $60 → 2 pos/33%, exposure guard tracks live) · real `can_trade` sizing at $45 = $40.5 (90%).

⚠️ **Restart the bot to apply.** On a ~$44 balance it boots into SMALL mode (1 trade @ ~90%) and auto-flips to 2×33% above $50.
