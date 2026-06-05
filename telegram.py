# BinBot — telegram.py
# v15.4 UPGRADE: daily/weekly summaries, heartbeat, photo support, critical alerts.
# Backward compatible: all existing send/trade_alert signatures preserved.

def _fmt(price):
    """Smart price formatter — shows enough decimals for any coin."""
    if price == 0: return "0"
    if price >= 100:   return f"{price:.2f}"
    if price >= 1:     return f"{price:.4f}"
    if price >= 0.01:  return f"{price:.5f}"
    if price >= 0.001: return f"{price:.6f}"
    return f"{price:.8f}"


import json, urllib.request, urllib.parse, logging, time, os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
log = logging.getLogger('binbot')


class Telegram:
    def __init__(self, cfg):
        self.cfg = cfg
        # v11.2.5 FIX: bounded thread pool instead of unlimited threading.Thread.
        self._pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="TGSend")
        self._shutdown = False  # v11.2.19 FIX
        # v15.4: heartbeat + summary state tracking
        self._last_heartbeat_ts = 0
        self._last_daily_summary_date = None  # ISO date string of last daily summary sent
        self._last_weekly_summary_date = None  # ISO date string of last weekly summary sent
        # v18.7.2: anti-spam backstop — remembers the last time each exact message text was
        # sent, to suppress repetitive condition alerts (exposure/heat/blocks) that fire every
        # scan cycle. Trade alerts are unaffected (each is unique per pair/price/balance).
        self._dedup = {}

    # ── core send (unchanged signature) ─────────────────────────────────
    def _send_blocking(self, msg):
        for _attempt in range(3):
            try:
                url = f"https://api.telegram.org/bot{self.cfg.TG_BOT_TOKEN}/sendMessage"
                data = json.dumps({"chat_id": self.cfg.TG_CHAT_ID, "text": msg,
                                   "parse_mode": "HTML",
                                   "disable_web_page_preview": True}).encode()
                req = urllib.request.Request(url, data=data,
                                              headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5)
                return
            except Exception as _e:
                if _attempt < 2:
                    time.sleep(2 ** _attempt)
                else:
                    log.warning(f"TG failed after 3 attempts: {_e}")

    def send(self, msg, dedup=True):
        if not self.cfg.TG_ENABLED: return
        if self._shutdown: return
        # v18.7.2 ANTI-SPAM BACKSTOP: drop a byte-identical message if it was already sent
        # within TG_DEDUP_WINDOW_SEC. Repetitive per-cycle condition alerts produce identical
        # text → suppressed. Trade alerts (BUY/SELL) carry unique pair/price/balance → never
        # identical within the window → never suppressed. Pass dedup=False to force-send.
        if dedup:
            try:
                _now = time.time()
                _win = getattr(self.cfg, 'TG_DEDUP_WINDOW_SEC', 120)
                if _now - self._dedup.get(msg, 0) < _win:
                    return  # duplicate within window — suppress
                self._dedup[msg] = _now
                if len(self._dedup) > 200:  # prune
                    self._dedup = {k: v for k, v in self._dedup.items() if _now - v < _win}
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        try:
            self._pool.submit(self._send_blocking, msg)
        except Exception as _e:
            log.debug(f"Suppressed [telegram.py]: {_e}")

    # v15.4: send_photo — for chart screenshots attached to alerts
    def _send_photo_blocking(self, img_bytes, caption=""):
        """Send a PNG photo with caption via Telegram. img_bytes is raw PNG."""
        try:
            import secrets
            boundary = secrets.token_hex(16)
            crlf = "\r\n"
            # Build multipart/form-data manually (no `requests` dep)
            parts = []
            for k, v in [("chat_id", self.cfg.TG_CHAT_ID),
                         ("caption", caption),
                         ("parse_mode", "HTML")]:
                parts.append(f"--{boundary}{crlf}"
                             f'Content-Disposition: form-data; name="{k}"{crlf}{crlf}'
                             f"{v}{crlf}".encode())
            parts.append(f"--{boundary}{crlf}"
                         f'Content-Disposition: form-data; name="photo"; filename="chart.png"{crlf}'
                         f"Content-Type: image/png{crlf}{crlf}".encode())
            parts.append(img_bytes)
            parts.append(f"{crlf}--{boundary}--{crlf}".encode())
            body = b"".join(parts)
            url = f"https://api.telegram.org/bot{self.cfg.TG_BOT_TOKEN}/sendPhoto"
            req = urllib.request.Request(url, data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            urllib.request.urlopen(req, timeout=15)  # photos need longer timeout
        except Exception as _e:
            log.warning(f"TG sendPhoto failed: {_e}")
            # Fall back to text-only message
            if caption:
                self._send_blocking(caption + "\n⚠️ (chart upload failed)")

    def send_photo(self, img_bytes, caption=""):
        """Send a chart screenshot. img_bytes = raw PNG. caption = HTML text."""
        if not self.cfg.TG_ENABLED: return
        if self._shutdown: return
        try:
            self._pool.submit(self._send_photo_blocking, img_bytes, caption)
        except Exception as _e:
            log.debug(f"Suppressed [telegram.py send_photo]: {_e}")

    # ── trade_alert (unchanged signature, can optionally include chart) ──
    def trade_alert(self, action, pair, price, strategy, pnl=None, conf=None,
                    qty=None, size=None, tp=None, sl=None, grade=None,
                    entry=None, entry_fee=None, exit_fee=None, hold_min=None,
                    balance=None, dca=0, reason=None, chart_bytes=None):
        if action == "BUY":
            msg = f"✅ <b>BUY</b> {pair}\n"
            msg += f"📊 {strategy}"
            if grade: msg += f" | Grade: {grade}"
            msg += f"\n💲 Entry: ${_fmt(price)}"
            if qty and size: msg += f"\n📦 Qty: {qty:.2f} | Size: ${size:.2f}"
            if tp:
                tp_pct = (tp - price) / price * 100 if price > 0 else 0
                tp_dollar = (tp - price) * qty if qty else 0
                msg += f"\n🎯 TP: ${_fmt(tp)} (+{tp_pct:.1f}%) → +${tp_dollar:.2f}"
            if sl:
                sl_pct = (sl - price) / price * 100 if price > 0 else 0
                sl_dollar = (sl - price) * qty if qty else 0
                msg += f"\n🛑 SL: ${_fmt(sl)} ({sl_pct:.1f}%) → ${sl_dollar:.2f}"
                be_trigger = round(price * 1.025, 6)
                be_new_sl  = round(price * 1.0045, 6)
                msg += (f"\n🔒 BE triggers at: ${_fmt(be_trigger)} (+2.5%) → "
                        f"New SL: ${_fmt(be_new_sl)} (entry+0.45%)")
            if conf: msg += f"\n📊 Conf: {conf:.0%}"
        else:
            is_win = pnl is not None and pnl > 0
            icon = "🤑" if is_win else "❌"
            r = reason if reason else action
            msg = f"{icon} <b>{r}</b> {pair}\n"
            msg += f"📊 {strategy}"
            if dca > 0: msg += f" | DCA:{dca}"
            if entry:
                msg += f"\n💲 Entry: ${_fmt(entry)} → Exit: ${_fmt(price)}"
            else:
                msg += f"\n💲 Exit: ${_fmt(price)}"
            if qty and size: msg += f"\n📦 Qty: {qty:.2f} | Size: ${size:.2f}"
            if pnl is not None:
                gross = pnl + (entry_fee or 0) + (exit_fee or 0)
                total_fee = (entry_fee or 0) + (exit_fee or 0)
                pct = (price - entry) / entry * 100 if entry and entry > 0 else 0
                msg += f"\n💰 Gross: ${gross:+.4f}"
                msg += f"\n💸 Fees: -${total_fee:.4f}"
                msg += f"\n{'✅' if is_win else '❌'} Net: ${pnl:+.4f} ({pct:+.1f}%)"
            if hold_min is not None:
                h, m = divmod(int(hold_min), 60)
                msg += f"\n⏱ Hold: {h}h {m}m"
            if balance is not None:
                msg += f"\n💼 Balance: ${balance:.2f}"
        # v15.4: optionally send as photo with caption if chart provided
        if chart_bytes:
            self.send_photo(chart_bytes, msg)
        else:
            self.send(msg)

    # ── v15.4 NEW: heartbeat + health snapshot every N hours ───────────
    def heartbeat(self, positions, daily_pnl, equity, dd_status, wr, closed_today,
                  interval_hours=4, force=False):
        """Send a periodic alive-check + health summary. Pass `force=True` to bypass timer."""
        now = time.time()
        if not force and (now - self._last_heartbeat_ts) < interval_hours * 3600:
            return  # not time yet
        self._last_heartbeat_ts = now

        pos_lines = []
        for p in positions[:5]:  # cap at 5 lines
            entry = getattr(p, 'avg_entry', None) or getattr(p, 'entry', 0)
            high = getattr(p, 'high', entry)
            be_marker = "🔒BE" if getattr(p, 'be_locked', False) else ""
            pos_lines.append(f"  {p.pair} entry=${_fmt(entry)} {be_marker}")
        if not pos_lines: pos_lines = ["  (none)"]

        msg = (f"🟢 <b>BinBot Heartbeat</b>\n"
               f"⏱ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
               f"💼 Equity: ${equity:.2f}\n"
               f"📊 Day PnL: ${daily_pnl:+.4f} | Closed: {closed_today} | WR: {wr:.0f}%\n"
               f"🛡 DD shield: {dd_status}\n"
               f"📦 Positions ({len(positions)}):\n" + "\n".join(pos_lines))
        self.send(msg)

    # ── v15.4 NEW: daily P&L summary at 23:55 UTC ──────────────────────
    def daily_summary(self, trades_jsonl_path="trades_v9.jsonl", equity=None):
        """Sends an end-of-day recap. Idempotent — only fires once per UTC date."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self._last_daily_summary_date == today:
            return  # already sent today
        if not os.path.exists(trades_jsonl_path):
            log.warning("daily_summary: trades_jsonl not found")
            return
        try:
            with open(trades_jsonl_path) as f:
                trades = [json.loads(l) for l in f if l.strip()]
        except Exception as _e:
            log.warning(f"daily_summary read failed: {_e}")
            return

        today_closes = [t for t in trades
                        if t.get('action') in ('TP', 'SL', 'TIME', 'TRAIL',
                                                 'CRASH', 'FORCE_CLOSE', 'REGIME', 'DUST')
                        and today in t.get('ts', '')]
        if not today_closes:
            self.send(f"📊 <b>Daily Recap — {today}</b>\nNo closed trades today.")
            self._last_daily_summary_date = today
            return

        total_pnl = sum(float(t.get('pnl', 0)) for t in today_closes)
        wins = [t for t in today_closes if float(t.get('pnl', 0)) > 0]
        losses = [t for t in today_closes if float(t.get('pnl', 0)) <= 0]
        wr = (len(wins) / len(today_closes) * 100) if today_closes else 0

        # Per-strategy aggregation
        by_strat = {}
        for t in today_closes:
            s = t.get('strategy', 'UNKNOWN')
            by_strat.setdefault(s, []).append(float(t.get('pnl', 0)))
        by_strat_sum = {s: (sum(v), len(v)) for s, v in by_strat.items()}
        best = max(by_strat_sum.items(), key=lambda x: x[1][0]) if by_strat_sum else None
        worst = min(by_strat_sum.items(), key=lambda x: x[1][0]) if by_strat_sum else None

        msg = (f"📊 <b>Daily Recap — {today}</b>\n"
               f"💰 PnL: ${total_pnl:+.4f}\n"
               f"📈 Trades: {len(wins)}W / {len(losses)}L  ({wr:.0f}% WR)\n")
        if best and best[1][0] > 0:
            msg += f"🏆 Best: {best[0]} (${best[1][0]:+.4f} on {best[1][1]} trades)\n"
        if worst and worst[1][0] < 0:
            msg += f"💸 Worst: {worst[0]} (${worst[1][0]:+.4f} on {worst[1][1]} trades)\n"
        if equity is not None:
            msg += f"💼 Equity: ${equity:.2f}"
        self.send(msg)
        self._last_daily_summary_date = today

    # ── v15.4 NEW: weekly summary on Sunday 23:55 UTC ──────────────────
    def weekly_summary(self, trades_jsonl_path="trades_v9.jsonl", equity=None):
        """Sends a 7-day rollup every Sunday. Idempotent per week."""
        now = datetime.now(timezone.utc)
        if now.weekday() != 6:  # 6 = Sunday
            return
        week_key = now.strftime('%Y-W%W')
        if self._last_weekly_summary_date == week_key:
            return
        if not os.path.exists(trades_jsonl_path):
            return
        try:
            with open(trades_jsonl_path) as f:
                trades = [json.loads(l) for l in f if l.strip()]
        except Exception:
            return

        week_ago = (now - timedelta(days=7)).isoformat()
        week_closes = [t for t in trades
                       if t.get('action') in ('TP', 'SL', 'TIME', 'TRAIL',
                                                'CRASH', 'FORCE_CLOSE', 'REGIME', 'DUST')
                       and t.get('ts', '') > week_ago]
        if not week_closes:
            self.send(f"📅 <b>Weekly Recap — {week_key}</b>\nNo closed trades this week.")
            self._last_weekly_summary_date = week_key
            return

        total_pnl = sum(float(t.get('pnl', 0)) for t in week_closes)
        wins = [t for t in week_closes if float(t.get('pnl', 0)) > 0]
        losses = [t for t in week_closes if float(t.get('pnl', 0)) <= 0]
        wr = (len(wins) / len(week_closes) * 100) if week_closes else 0
        avg_win = sum(float(t['pnl']) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(float(t['pnl']) for t in losses) / len(losses) if losses else 0
        expectancy = (wr/100) * avg_win + (1 - wr/100) * avg_loss

        msg = (f"📅 <b>Weekly Recap — {week_key}</b>\n"
               f"💰 7d PnL: ${total_pnl:+.4f}\n"
               f"📈 Trades: {len(week_closes)} ({len(wins)}W / {len(losses)}L)\n"
               f"📊 WR: {wr:.0f}% | Expectancy: ${expectancy:+.4f}/trade\n"
               f"🟢 Avg Win: ${avg_win:+.4f}\n"
               f"🔴 Avg Loss: ${avg_loss:+.4f}\n")
        if equity is not None:
            msg += f"💼 Equity: ${equity:.2f}"
        self.send(msg)
        self._last_weekly_summary_date = week_key

    # ── v15.4 NEW: critical event alerts with priority ─────────────────
    def critical_alert(self, event_type, details, priority="HIGH"):
        """Emit a critical event alert. Priority: HIGH/CRITICAL/INFO.
        Used by DD shield trip, API down, ML retrain failures, accuracy drops, etc."""
        icon = {"CRITICAL": "🚨", "HIGH": "⚠️", "INFO": "ℹ️"}.get(priority, "⚠️")
        msg = f"{icon} <b>{event_type}</b>\n{details}"
        self.send(msg)

    # ── v18.9.11 NEW: register the slash-command MENU (the blue "Menu" button) ──
    def set_commands(self):
        """Register the bot's command menu via Telegram setMyCommands, so the blue
        'Menu' button shows the command list with descriptions. Called once at startup."""
        if not self.cfg.TG_ENABLED:
            return
        cmds = [
            {"command": "status",      "description": "equity, day P&L, positions, paused"},
            {"command": "positions",   "description": "open positions with live P&L"},
            {"command": "pause",       "description": "stop opening new positions"},
            {"command": "resume",      "description": "resume normal operation"},
            {"command": "force_close", "description": "close ALL positions (2-step)"},
            {"command": "help",        "description": "command list"},
        ]
        try:
            url = f"https://api.telegram.org/bot{self.cfg.TG_BOT_TOKEN}/setMyCommands"
            data = json.dumps({"commands": cmds}).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
            log.info("📋 Telegram command menu registered (Menu button)")
        except Exception as e:
            log.warning(f"setMyCommands failed: {e}")

    def close(self):
        self._shutdown = True
        try:
            # v14.6.5 AUDIT FIX (L-5): was wait=False — queued messages (like
            # final "Bot stopped" alert) could be dropped. Now wait up to 3s
            # for in-flight sends to complete before killing the pool.
            self._pool.shutdown(wait=True, cancel_futures=False)
        except TypeError:
            # Python <3.9 doesn't have cancel_futures
            self._pool.shutdown(wait=True)
        except Exception as _e:
            log.debug(f"Suppressed [telegram.py]: {_e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NEWS SENTIMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
