# BinBot — Telegram Alert System

Reference for BinBot's Telegram integration: the core sender, every alert type
(BUY / SELL / EXIT / locks / heartbeat / summaries / critical events), where each
fires in the code, and how to call it.

- **Core class:** `telegram.py` → `Telegram`
- **Incoming commands:** `telegram_commands.py`
- **Charts:** `telegram_charts.py`

---

## 1. Configuration

Set in `.env` (loaded by systemd). Telegram is enabled only when both token + chat are present.

```ini
TG_BOT_TOKEN=123456:ABC...      # from @BotFather
TG_CHAT_ID=123456789            # your chat/user id
```

Relevant `config.py` flags:

| Flag | Default | Purpose |
|---|---|---|
| `TG_ENABLED` | derived (true if token+chat set) | master on/off |
| `TG_DEDUP_WINDOW_SEC` | `120` | suppress byte-identical messages within this window (anti-spam) |

---

## 2. Core API — `Telegram` (telegram.py)

```python
tg = Telegram(cfg)

tg.send(msg, dedup=True)              # fire-and-forget text (HTML), 3x retry, thread-pooled
tg.send_photo(png_bytes, caption="") # attach a chart PNG
tg.trade_alert(action, pair, price, strategy, ...)   # formats BUY + EXIT cards
tg.heartbeat(positions, daily_pnl, equity, dd_status, wr, closed_today, interval_hours=4)
tg.daily_summary(trades_jsonl_path, equity=None)     # once per UTC day
tg.weekly_summary(trades_jsonl_path, equity=None)    # Sundays
tg.critical_alert(event_type, details, priority="HIGH")  # HIGH/CRITICAL/INFO
tg.close()                            # flush + shutdown the send pool
```

**Design notes**
- Non-blocking: every send runs on a bounded `ThreadPoolExecutor(max_workers=5)`.
- Resilient: `_send_blocking` retries 3× with exponential backoff (1s, 2s).
- Anti-spam: `send(dedup=True)` drops a byte-identical message seen within
  `TG_DEDUP_WINDOW_SEC`. Trade alerts carry unique pair/price/balance, so they are
  **never** suppressed; only repetitive condition alerts (exposure/heat) are.
- HTML formatting (`<b>…</b>`), web-page preview disabled.

---

## 3. Alert catalog — what fires, and from where

| Alert | Trigger | Source | Formatter |
|---|---|---|---|
| 🟢 **BUY** | position opened | `risk.py` `open_pos` → `trade_alert("BUY", …)` | `telegram.py` `trade_alert` (lines 122–140) |
| 🤑/❌ **EXIT** (TP/SL/TRAIL/TIME/CRASH/FORCE_CLOSE/REGIME/DUST) | position closed | `risk.py` `_record_close` → `trade_alert(reason, …, pnl=…)` | `telegram.py` `trade_alert` (lines 141–164) |
| 🔒 **SL LOCK / BE / CHASE / PROFIT LOCK** | trailing/breakeven/profit-ladder ratchets | `risk.py` `check_exits` (~1126–1344) | inline `tg.send` |
| 📤 **SCALE OUT** | partial profit-ladder sell | `bot.py` (~2043) | inline `tg.send` |
| 📥 **ADOPTED POSITION** | orphan coin adopted at boot | `bot.py` (~637) | inline `tg.send` |
| 🚀 **LIVE** / 🛑 **stopped** | bot start / shutdown | `bot.py` (~846 / ~3290) | inline `tg.send` |
| 🟢 **Heartbeat** | every `interval_hours` (4h) | `bot.py` (~1289) | `heartbeat` |
| 📊 **Daily Recap** | 23:55 UTC, once/day | `bot.py` (~1297) | `daily_summary` |
| 📅 **Weekly Recap** | Sundays | `weekly_summary` | `weekly_summary` |
| 🎚️ **SMALL-BALANCE / NORMAL MODE** | capital-tier switch | `risk.py` / `bot.py` | inline `tg.send` |
| 🚨 **Critical** (DD shield trip/recover, BTC crash, dead-man switch, WS down, strategy killed) | risk/health events | `bot.py` (~1436/1461/1656/1965) | `critical_alert` |
| ⚠️ **NATIVE SL** attach/move/re-attach failures | exchange-stop issues | `risk.py` / `bot.py` | inline `tg.send` |

---

## 4. Message formats

### BUY card (`trade_alert` action="BUY")
```
✅ BUY  SOLUSDT
📊 SMC_OB+FVG | Grade: A+
💲 Entry: $145.20
📦 Qty: 0.34 | Size: $49.50
🎯 TP: $151.80 (+4.5%) → +$2.24
🛑 SL: $140.80 (-3.0%) → $-1.49
🔒 BE triggers at: $148.83 (+2.5%) → New SL: $145.85 (entry+0.45%)
📊 Conf: 82%
```

### EXIT card (`trade_alert` action=reason, with `pnl`)
```
🤑 TP  SOLUSDT
📊 SMC_OB+FVG
💲 Entry: $145.20 → Exit: $151.80
📦 Qty: 0.34 | Size: $49.50
💰 Gross: $+2.24
💸 Fees: -$0.10
✅ Net: $+2.14 (+4.5%)
⏱ Hold: 3h 12m
💼 Balance: $52.14
```
Win → 🤑, loss → ❌. `reason` is the close type (TP/SL/TRAIL/TIME/CRASH/FORCE_CLOSE/REGIME/DUST).

### Heartbeat
```
🟢 BinBot Heartbeat
⏱ 2026-06-02 14:31 UTC
💼 Equity: $52.14
📊 Day PnL: $+2.14 | Closed: 1 | WR: 100%
🛡 DD shield: full
📦 Positions (1):
  SOLUSDT entry=$145.20 🔒BE
```

---

## 5. Minimal integration example

```python
from config import Config
from telegram import Telegram

cfg = Config()                 # needs TG_BOT_TOKEN + TG_CHAT_ID in env/.env
tg = Telegram(cfg)

# simple message
tg.send("✅ <b>Hello</b> from my app")

# a trade card
tg.trade_alert("BUY", "BTCUSDT", 65000.0, "BREAKOUT",
               conf=0.82, qty=0.001, size=65.0, tp=68000.0, sl=63000.0, grade="A")

# a close card
tg.trade_alert("TP", "BTCUSDT", 68000.0, "BREAKOUT",
               pnl=2.85, qty=0.001, size=65.0, entry=65000.0,
               entry_fee=0.065, exit_fee=0.068, hold_min=190, balance=67.85)

# critical event
tg.critical_alert("DD SHIELD TRIPPED", "Drawdown 12%+ — new entries blocked", priority="CRITICAL")

tg.close()                     # on shutdown, flushes queued sends
```

---

## 6. Incoming commands — `telegram_commands.py`

The bot also polls for commands you send it (status, force-close, etc.). See that
module for the handler table. (Optional `/stop` and `/start` controls can be added there.)
