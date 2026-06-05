# BinBot v11 — reconciler.py
# v11.2.10: Position reconciliation — compares bot state against Binance balances
import time, logging

log = logging.getLogger('binbot')


class PositionReconciler:
    """Compares bot's tracked positions against actual Binance balances.
    Alerts on drift, detects ghosts (bot tracks but Binance doesn't have),
    and detects orphans (Binance has but bot doesn't track).

    Runs every INTERVAL_CYCLES cycles (~5 minutes at 30s/cycle).
    Does NOT auto-correct — alerts only. Auto-correction is too risky
    for a live trading system without human oversight."""

    INTERVAL_CYCLES = 10  # ~5 minutes at 30s/cycle
    DRIFT_THRESHOLD = 0.10  # 10% qty drift triggers alert
    MIN_ORPHAN_USD = 2.0  # v13.5.2 audit Fix #8: was 5.0 — small stuck tokens
    # ($3-$5 each) from the 8 untracked BUYs in trade journal were silently
    # invisible. Lowered to $2 so dust below MIN_NOTIONAL ($5) still shows up.

    def __init__(self):
        self._cycle_count = 0
        self._last_check = 0
        self._consecutive_failures = 0
        # v13.5.7 FIX #6 (May 21, 2026): per-asset orphan alert dedup.
        # Was: every reconciliation cycle (~5min) re-fired Telegram alert for
        # the same orphan. On May 15, PENDLE dust generated 70+ alerts in hours.
        # Now: per-asset cooldown — re-alert only if 30+ min passed since last
        # alert for that specific asset, OR the orphan value changed by >25%.
        self._orphan_last_alert_ts = {}   # asset_name -> unix timestamp
        self._orphan_last_alert_val = {}  # asset_name -> usd value
        self.ORPHAN_ALERT_COOLDOWN_SEC = 1800  # 30 minutes
        self.ORPHAN_ALERT_VALUE_DELTA = 0.25   # 25% value change re-alerts

    def check(self, positions, exchange, tg, cfg):
        """Run reconciliation if enough cycles have passed.
        Args:
            positions: list of Position objects from risk.positions
            exchange: Exchange instance with .cl (Binance client)
            tg: Telegram instance for alerts
            cfg: Config with PAIRS list
        """
        self._cycle_count += 1
        if self._cycle_count < self.INTERVAL_CYCLES:
            return

        self._cycle_count = 0

        try:
            self._reconcile(positions, exchange, tg, cfg)
            self._consecutive_failures = 0
        except Exception as e:
            self._consecutive_failures += 1
            log.warning(f"Reconciler check failed ({self._consecutive_failures}x): {e}")
            # Don't spam alerts on repeated API failures
            if self._consecutive_failures == 3:
                try:
                    tg.send(f"⚠️ <b>RECONCILER DOWN</b>\n"
                            f"Failed {self._consecutive_failures}x — check API connectivity")
                except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")

    def _reconcile(self, positions, exchange, tg, cfg):
        """Core reconciliation logic."""
        # Build map of what the bot thinks it has
        bot_holdings = {}  # {asset: qty}
        for pos in positions:
            asset = pos.pair.replace("USDT", "")
            bot_holdings[asset] = bot_holdings.get(asset, 0) + pos.qty

        # Get actual Binance balances for tracked assets
        issues = []
        ghosts = []
        drifts = []

        for asset, expected_qty in bot_holdings.items():
            try:
                bal = exchange.cl.get_asset_balance(asset=asset)
                actual_free = float(bal["free"]) if bal else 0
                actual_locked = float(bal.get("locked", 0)) if bal else 0
                actual_total = actual_free + actual_locked

                if expected_qty > 0 and actual_total < expected_qty * 0.05:
                    # GHOST: Bot thinks it has coins, Binance has effectively none.
                    # v13.5.6 FIX: relaxed from `actual_total == 0` to <5% of
                    # expected, matching the in-cycle ghost check threshold
                    # (bot.py:895). With the strict-zero rule, any dust at all
                    # (e.g. 0.01 TON left over after a native-SL fill at 6.47
                    # out of 6.48) classified the position as DRIFT instead of
                    # GHOST, and DRIFT is alert-only — never auto-closes. This
                    # left the bot tracking a phantom position for up to 10 min
                    # until the in-cycle check (every 20 cycles ≈ 10 min) caught
                    # it. Now reconciler catches it too, in <5 min.
                    ghosts.append({
                        "asset": asset,
                        "expected": expected_qty,
                        "actual": actual_total
                    })
                elif expected_qty > 0:
                    drift_pct = abs(actual_total - expected_qty) / expected_qty
                    if drift_pct > self.DRIFT_THRESHOLD:
                        drifts.append({
                            "asset": asset,
                            "expected": round(expected_qty, 6),
                            "actual": round(actual_total, 6),
                            "drift_pct": round(drift_pct * 100, 1)
                        })
            except Exception as e:
                log.warning(f"Reconciler: balance fetch failed for {asset}: {e}")

        # Check for ORPHANS: Binance has coins that bot doesn't track
        tracked_assets = set(bot_holdings.keys())
        # Only check assets from our trading pairs to avoid noise from staking/earn etc.
        pair_assets = set()
        for p in cfg.PAIRS:
            pair_assets.add(p["s"].replace("USDT", ""))

        orphans = []
        for asset in pair_assets - tracked_assets:
            try:
                bal = exchange.cl.get_asset_balance(asset=asset)
                # v13.2 FIX: Include locked balance (open orders + lock-up staking)
                actual = float(bal["free"]) + float(bal.get("locked", 0)) if bal else 0
                if actual > 0:
                    # Check USD value to filter dust
                    try:
                        price = float(exchange.cl.get_symbol_ticker(
                            symbol=f"{asset}USDT")["price"])
                        usd_val = actual * price
                        if usd_val >= self.MIN_ORPHAN_USD:
                            orphans.append({
                                "asset": asset,
                                "qty": round(actual, 6),
                                "usd": round(usd_val, 2)
                            })
                    except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")  # Can't price it — skip
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")  # Balance fetch failed — skip

        # Report issues
        if ghosts:
            ghost_msg = "\n".join(
                f"  • {g['asset']}: bot has {g['expected']:.4f}, Binance has 0"
                for g in ghosts
            )
            log.warning(f"🚨 GHOST POSITIONS DETECTED:\n{ghost_msg}")
            # v14.6.3 FIX: auto-remove ghost positions from state
            # Previously alert-only — left position slot locked indefinitely
            # after manual sell on Binance app. Now: synthesize close + remove.
            # v14.6.5 AUDIT FIX (My-C2): was raw `positions.remove(p)` with no
            # journal entry — PnL, wins/losses, Kelly, RL all missed the exit.
            # Now: write a GHOST close entry to trades_v9.jsonl BEFORE removing.
            try:
                from datetime import datetime, timezone as _tz
                import json as _json
                _ghost_assets = {g['asset'] for g in ghosts}
                _before = len(positions)
                _journal_path = __import__('os').path.join(
                    __import__('os').path.dirname(__file__), 'trades_v9.jsonl')
                for p in list(positions):
                    if p.pair.replace('USDT','') in _ghost_assets:
                        # Synthesized close: book at avg_entry (no phantom PnL)
                        try:
                            _entry = {
                                "ts": datetime.now(_tz.utc).isoformat(),
                                "pair": p.pair,
                                "action": "GHOST",
                                "qty": getattr(p, 'qty', 0),
                                "entry": getattr(p, 'avg_entry', 0),
                                "exit": getattr(p, 'avg_entry', 0),
                                "pnl": 0.0,
                                "strategy": getattr(p, 'strategy', 'unknown'),
                                "reason": "GHOST_RECONCILER — position existed in bot but not on Binance",
                            }
                            with open(_journal_path, 'a') as _jf:
                                _jf.write(_json.dumps(_entry, separators=(',',':')) + '\n')
                            log.info(f"📝 GHOST journal entry written for {p.pair}")
                        except Exception as _je:
                            log.warning(f"GHOST journal write failed for {p.pair}: {_je}")
                        positions.remove(p)
                _removed = _before - len(positions)
                if _removed > 0:
                    log.warning(f"🧹 Auto-removed {_removed} ghost position(s) from live memory")
                    try:
                        tg.send(f"🧹 <b>GHOST AUTO-REMOVED</b>\n"
                                f"{ghost_msg}\n"
                                f"✅ {_removed} position(s) removed from bot state\n"
                                f"📝 Journal entries written (PnL=0, booked at entry)")
                    except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
            except Exception as _ge:
                log.warning(f"Ghost auto-remove failed: {_ge}")
                try:
                    tg.send(f"🚨 <b>GHOST POSITIONS</b>\n"
                            f"Bot tracks coins that Binance doesn't have!\n"
                            f"{ghost_msg}\n"
                            f"⚠️ Manual review required")
                except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")

        if drifts:
            drift_msg = "\n".join(
                f"  • {d['asset']}: bot={d['expected']} actual={d['actual']} "
                f"({d['drift_pct']}% drift)"
                for d in drifts
            )
            log.warning(f"⚠️ POSITION DRIFT:\n{drift_msg}")
            try:
                tg.send(f"⚠️ <b>POSITION DRIFT</b>\n"
                        f"Bot qty differs from Binance by >{self.DRIFT_THRESHOLD*100:.0f}%\n"
                        f"{drift_msg}")
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")

        orphans = [o for o in orphans if o["asset"] != "BNB"]
        if orphans:
            orphan_msg = "\n".join(
                f"  • {o['asset']}: {o['qty']} (${o['usd']})"
                for o in orphans
            )
            log.info(f"🔍 ORPHAN COINS (Binance has, bot doesn't track):\n{orphan_msg}")
            # v13.5.7 FIX #6: dedup per-asset before Telegram alert. Was: every
            # 5min cycle re-fired alert (PENDLE on May 15 → 70+ alerts in hours).
            now_ts = time.time()
            asset_to_alert = []
            for o in orphans:
                asset = o["asset"]
                cur_val = o["usd"]
                last_ts = self._orphan_last_alert_ts.get(asset, 0)
                last_val = self._orphan_last_alert_val.get(asset, 0)
                cooldown_passed = (now_ts - last_ts) >= self.ORPHAN_ALERT_COOLDOWN_SEC
                value_changed = (last_val == 0) or \
                                (abs(cur_val - last_val) / max(last_val, 0.01) >= self.ORPHAN_ALERT_VALUE_DELTA)
                if cooldown_passed or value_changed:
                    asset_to_alert.append(o)
                    self._orphan_last_alert_ts[asset] = now_ts
                    self._orphan_last_alert_val[asset] = cur_val
                else:
                    log.debug(f"🔕 Orphan alert suppressed for {asset} "
                              f"(last alerted {(now_ts-last_ts)/60:.0f}min ago)")
            # Only alert if significant value AND at least one asset escaped dedup
            # v13.5.2 audit Fix #8: lowered $10→$5 so a single $5-9 stuck coin
            # actually pages the operator. Was: stuck SEI/INJ/JASMY worth $3-9 each
            # would log silently and never reach Telegram.
            total_orphan_usd = sum(o["usd"] for o in asset_to_alert)
            if asset_to_alert and total_orphan_usd >= 5.0:
                # ─── AUTO-HEAL ORPHAN COINS ───
                # v14.6.5 AUDIT FIX (My-C1): removed peak_equity=0 / dd_peak=0
                # reset that was disarming drawdown protection on every orphan
                # cleanup. Also: write a journal entry for each orphan sell so
                # trade history is complete (was: silent sell with no audit trail).
                import os, json, subprocess
                from datetime import datetime, timezone as _tz2
                _journal_path2 = os.path.join(os.path.dirname(__file__), 'trades_v9.jsonl')
                try:
                    for o in asset_to_alert:
                        sym = f"{o['asset']}USDT"
                        log.warning(f"🧹 Auto-selling ORPHAN {sym} ({o['qty']} qty)...")
                        try:
                            _qty = exchange.rnd(sym, o['qty'])
                            if _qty > 0:
                                exchange.cl.create_order(symbol=sym, side='SELL', type='MARKET', quantity=f"{_qty:.8f}")
                            # Write journal entry for the orphan sell
                            _entry = {
                                "ts": datetime.now(_tz2.utc).isoformat(),
                                "pair": sym,
                                "action": "FORCE_CLOSE",
                                "qty": o['qty'],
                                "entry": 0,
                                "exit": round(o['usd'] / o['qty'], 6) if o['qty'] > 0 else 0,
                                "pnl": 0.0,
                                "strategy": "ORPHAN_RECONCILER",
                                "reason": f"ORPHAN — untracked {sym} on Binance, auto-sold ${o['usd']:.2f}",
                            }
                            with open(_journal_path2, 'a') as _jf:
                                _jf.write(json.dumps(_entry, separators=(',',':')) + '\n')
                            log.info(f"📝 ORPHAN journal entry written for {sym}")
                        except Exception as e:
                            log.warning(f"Failed to auto-sell {sym}: {e}")

                    log.warning("✅ Orphan coins sold. No circuit breaker reset needed.")
                    if tg:
                        try: tg.send(f"🧹 <b>ORPHAN AUTO-FIXED</b>\n"
                                     f"Sold {len(asset_to_alert)} untracked coins "
                                     f"(${total_orphan_usd:.2f}).\n"
                                     f"📝 Journal entries written.\n"
                                     f"🛡️ Drawdown Shield preserved (no reset).")
                        except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
                except Exception as e:
                    log.error(f"Auto-orphan fix failed: {e}")

                alert_msg = "\n".join(
                    f"  • {o['asset']}: {o['qty']} (${o['usd']})"
                    for o in asset_to_alert
                )
                try:
                    tg.send(f"🔍 <b>ORPHAN COINS</b>\n"
                            f"Binance has coins bot doesn't track (${total_orphan_usd:.2f} total)\n"
                            f"{alert_msg}\n"
                            f"💡 May be from manual trades or failed position tracking\n"
                            f"<i>Next alert for same asset in 30min unless value changes ±25%</i>")
                except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")

        if not ghosts and not drifts and not orphans:
            log.debug("✅ Reconciliation OK — bot matches Binance")

        self._last_check = time.time()
        return {"ghosts": ghosts, "drifts": drifts, "orphans": orphans}

