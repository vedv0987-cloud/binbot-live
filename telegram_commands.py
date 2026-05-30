"""BinBot v15.4 — Telegram interactive command handler.

Runs a background long-polling thread that listens for commands from the
authorized chat_id ONLY. Commands trigger state changes on the bot.

SECURITY: hardcoded authorization to cfg.TG_CHAT_ID. Messages from any other
chat_id are silently ignored. Audit-logged via the bot's _audit hook.

Supported commands:
  /status        — current equity, positions, PnL, DD shield
  /pause         — stop opening NEW positions (existing positions managed normally)
  /resume        — resume normal operation
  /positions     — list open positions with live PnL
  /force_close   — close ALL positions (2-step confirmation)
  /confirm       — confirm pending /force_close within 60s
  /cancel        — cancel any pending confirmation
  /help          — list commands
"""
from __future__ import annotations
import json, logging, threading, time, urllib.request, urllib.parse, secrets
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("binbot")


class TelegramCommandHandler:
    """Long-polling Telegram command receiver. Runs as a daemon thread."""

    POLL_TIMEOUT_SEC = 25     # long-poll timeout — Telegram holds connection up to this long
    POLL_RETRY_DELAY = 5      # delay before retry on error
    CONFIRM_WINDOW_SEC = 60   # how long /force_close confirmation is valid

    def __init__(self, cfg, bot):
        """bot is the ProBotV11 instance — used to read state and trigger actions."""
        self.cfg = cfg
        self.bot = bot
        self._offset = 0
        self._stop_event = threading.Event()
        self._thread = None
        # Pending confirmations: {token: {"action": str, "expires": ts}}
        self._pending_confirm: Optional[dict] = None
        # Authorized chat_id (only this chat can issue commands)
        self._authorized_chat_id = str(cfg.TG_CHAT_ID) if cfg.TG_CHAT_ID else None

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self):
        if not self.cfg.TG_ENABLED or not self._authorized_chat_id:
            log.info("TelegramCommandHandler: disabled (TG_ENABLED=False or no TG_CHAT_ID)")
            return
        if not getattr(self.cfg, "TG_INTERACTIVE_ENABLED", True):
            log.info("TelegramCommandHandler: disabled by TG_INTERACTIVE_ENABLED=False")
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                          name="TGCommands")
        self._thread.start()
        log.info(f"  🎛  TG commands wired (authorized chat_id={self._authorized_chat_id[:4]}***)")

    def stop(self):
        self._stop_event.set()

    # ── long-polling loop ─────────────────────────────────────────────
    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log.debug(f"TG command poll error: {e}")
                if self._stop_event.wait(self.POLL_RETRY_DELAY):
                    break

    def _poll_once(self):
        url = (f"https://api.telegram.org/bot{self.cfg.TG_BOT_TOKEN}/getUpdates"
               f"?offset={self._offset}&timeout={self.POLL_TIMEOUT_SEC}"
               f"&allowed_updates=%5B%22message%22%5D")
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self.POLL_TIMEOUT_SEC + 10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            # Network glitch — wait then retry
            if self._stop_event.wait(self.POLL_RETRY_DELAY):
                return
            return
        if not data.get("ok"):
            return
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if not chat_id or not text:
                continue
            # AUTHORIZATION GATE — silently drop unauthorized
            if chat_id != self._authorized_chat_id:
                log.warning(f"TG command from UNAUTHORIZED chat_id={chat_id[:4]}*** — ignored")
                continue
            self._dispatch(text)

    # ── command dispatch ──────────────────────────────────────────────
    def _dispatch(self, text: str):
        cmd = text.split()[0].lower()
        try:
            if cmd == "/status":           self._cmd_status()
            elif cmd == "/positions":      self._cmd_positions()
            elif cmd == "/pause":          self._cmd_pause()
            elif cmd == "/resume":         self._cmd_resume()
            elif cmd == "/force_close":    self._cmd_force_close_prepare()
            elif cmd == "/confirm":        self._cmd_confirm(text)
            elif cmd == "/cancel":         self._cmd_cancel()
            elif cmd == "/help":           self._cmd_help()
            else:
                self.bot.tg.send(f"❓ Unknown command: <code>{cmd}</code>\nSend /help for list.", dedup=False)
        except Exception as e:
            log.warning(f"TG command {cmd} failed: {e}")
            self.bot.tg.send(f"⚠️ Command failed: {e}", dedup=False)

    # ── command implementations ───────────────────────────────────────
    def _cmd_help(self):
        msg = ("🎛 <b>BinBot Commands</b>\n"
               "/status      — equity, positions, day PnL, DD shield\n"
               "/positions   — open positions with live PnL\n"
               "/pause       — stop opening NEW positions\n"
               "/resume      — resume normal operation\n"
               "/force_close — close ALL positions (2-step)\n"
               "/help        — this list")
        self.bot.tg.send(msg, dedup=False)

    def _cmd_status(self):
        bot = self.bot
        try:
            # v15.4 FIX: equity = capital + unrealized PnL from positions
            # Was: getattr(bot.risk, 'free', 0) which is never set → always $0
            from bot import _pos_lock
            with _pos_lock:
                pos_value = sum(getattr(p, 'size', 0) for p in bot.risk.positions)
            equity = getattr(bot.cfg, 'TOTAL_CAPITAL', 0) + getattr(bot.risk, 'pnl', 0)
            if equity <= 0:
                equity = pos_value  # fallback
            day_pnl = getattr(bot.risk, 'daily_pnl', 0)
            wins = getattr(bot.risk, 'wins', 0)
            losses = getattr(bot.risk, 'losses', 0)
            total = wins + losses
            wr = (wins / total * 100) if total else 0
            paused = getattr(bot, 'paused', False)
            dd_status = "full" if not getattr(bot.risk.ddshield, 'kill_switch', False) else "TRIPPED"
            peak = getattr(bot.risk, 'peak_equity', 0) or getattr(bot.cfg, 'TOTAL_CAPITAL', 0)  # v15.4 FIX: fallback
            dd_pct = ((peak - equity) / peak * 100) if peak > 0 and equity < peak else 0
            msg = (f"📊 <b>Status</b>\n"
                   f"State: {'⏸ PAUSED' if paused else '▶️ RUNNING'}\n"
                   f"💼 Equity: ${equity:.2f} | Peak: ${peak:.2f} | DD: {dd_pct:.2f}%\n"
                   f"📈 Day PnL: ${day_pnl:+.4f}\n"
                   f"🏆 Lifetime: {wins}W / {losses}L ({wr:.0f}% WR)\n"
                   f"📦 Positions: {len(bot.risk.positions)}/{bot.cfg.MAX_POSITIONS}\n"
                   f"🛡 DD Shield: {dd_status}")
            self.bot.tg.send(msg, dedup=False)
        except Exception as e:
            self.bot.tg.send(f"⚠️ /status failed: {e}", dedup=False)

    def _cmd_positions(self):
        bot = self.bot
        from bot import _pos_lock
        with _pos_lock:
            positions = list(bot.risk.positions)
        if not positions:
            self.bot.tg.send("📦 No open positions.", dedup=False)
            return
        try:
            # Best-effort live price fetch
            live_px = {}
            # v15.2: use WebSocket live prices (sync-safe) instead of removed cl
            ws_prices = bot.ws.get_prices() if hasattr(bot, 'ws') else {}
            for p in positions:
                try:
                    px = ws_prices.get(p.pair) or getattr(bot, '_last_prices', {}).get(p.pair)
                    if px:
                        live_px[p.pair] = float(px)
                except Exception:
                    pass
            lines = [f"📦 <b>Positions ({len(positions)})</b>"]
            for p in positions:
                px = live_px.get(p.pair, p.entry)
                pnl_pct = (px - p.entry) / p.entry * 100 if p.entry > 0 else 0
                pnl_usd = (px - p.entry) * p.qty
                to_sl = (px - p.sl) / px * 100 if px > 0 else 0
                to_tp = (p.tp - px) / px * 100 if px > 0 else 0
                be = " 🔒BE" if getattr(p, 'be_locked', False) else ""
                lines.append(f"\n<b>{p.pair}</b>{be}\n"
                             f"  Entry: ${p.entry:.4f}  Live: ${px:.4f}\n"
                             f"  PnL: ${pnl_usd:+.4f} ({pnl_pct:+.2f}%)\n"
                             f"  SL: -{to_sl:.2f}% | TP: +{to_tp:.2f}%\n"
                             f"  Strat: {p.strategy}")
            self.bot.tg.send("".join(lines), dedup=False)
        except Exception as e:
            self.bot.tg.send(f"⚠️ /positions failed: {e}", dedup=False)

    def _cmd_pause(self):
        if getattr(self.bot, 'paused', False):
            self.bot.tg.send("⏸ Already paused.", dedup=False)
            return
        self.bot.paused = True
        msg = ("⏸ <b>BOT PAUSED</b>\n"
               "No NEW positions will open.\n"
               "Existing positions managed normally (SL/TP/trail).\n"
               "Send /resume to restart trading.")
        self.bot.tg.send(msg, dedup=False)
        log.warning("🎛  BOT PAUSED via Telegram command")
        # Audit log if available
        try:
            audit = getattr(self.bot, '_audit', None)
            if audit:
                audit.log("BOT_PAUSE", source="telegram_command")
        except Exception:
            pass

    def _cmd_resume(self):
        if not getattr(self.bot, 'paused', False):
            self.bot.tg.send("▶️ Already running.", dedup=False)
            return
        self.bot.paused = False
        self.bot.tg.send("▶️ <b>BOT RESUMED</b>\nNormal operation restored.", dedup=False)
        log.warning("🎛  BOT RESUMED via Telegram command")
        try:
            audit = getattr(self.bot, '_audit', None)
            if audit:
                audit.log("BOT_RESUME", source="telegram_command")
        except Exception:
            pass

    def _cmd_force_close_prepare(self):
        bot = self.bot
        from bot import _pos_lock
        with _pos_lock:
            n = len(bot.risk.positions)
        if n == 0:
            self.bot.tg.send("📦 No positions to close.", dedup=False)
            return
        token = secrets.token_hex(3)  # 6-char hex
        self._pending_confirm = {
            "action": "force_close",
            "token": token,
            "expires": time.time() + self.CONFIRM_WINDOW_SEC,
        }
        with _pos_lock:
            pairs = ", ".join(p.pair for p in bot.risk.positions)
        msg = (f"⚠️ <b>CONFIRM FORCE CLOSE</b>\n"
               f"This will close {n} position(s): {pairs}\n"
               f"Reply <code>/confirm {token}</code> within {self.CONFIRM_WINDOW_SEC}s.\n"
               f"Or /cancel to abort.")
        self.bot.tg.send(msg, dedup=False)

    def _cmd_confirm(self, text: str):
        if not self._pending_confirm:
            self.bot.tg.send("❌ Nothing to confirm.", dedup=False)
            return
        if time.time() > self._pending_confirm["expires"]:
            self._pending_confirm = None
            self.bot.tg.send("❌ Confirmation expired (60s window passed).", dedup=False)
            return
        parts = text.split()
        if len(parts) < 2:
            self.bot.tg.send("❌ Usage: <code>/confirm &lt;token&gt;</code>", dedup=False)
            return
        provided = parts[1].strip().lower()
        expected = self._pending_confirm["token"].lower()
        if provided != expected:
            self.bot.tg.send(f"❌ Invalid token. Use the exact one from the confirm prompt.", dedup=False)
            return
        action = self._pending_confirm["action"]
        self._pending_confirm = None
        if action == "force_close":
            self._execute_force_close()

    def _cmd_cancel(self):
        if not self._pending_confirm:
            self.bot.tg.send("✓ Nothing pending.", dedup=False)
            return
        self._pending_confirm = None
        self.bot.tg.send("✓ Confirmation cancelled.", dedup=False)

    def _execute_force_close(self):
        """Actually close all positions. Audit-logged."""
        bot = self.bot
        try:
            audit = getattr(bot, '_audit', None)
            if audit:
                audit.log("FORCE_CLOSE_ALL", source="telegram_command",
                          positions=[p.pair for p in bot.risk.positions])
        except Exception:
            pass
        # Set a flag that the cycle loop reads to do the actual closing
        # (closing inside this thread would race with the trading loop)
        bot._force_close_all_requested = True
        from bot import _pos_lock
        with _pos_lock:
            n = len(bot.risk.positions)
        self.bot.tg.send(f"✅ Force-close requested for {n} position(s). Executing in next cycle...", dedup=False)
        log.warning(f"🎛  FORCE CLOSE ALL requested via Telegram ({n} positions)")
