# BinBot v13.6 — exchange_native_sl.py
"""Native Stop-Loss-Limit orders on Binance Spot.

PURPOSE:
Place a STOP_LOSS_LIMIT order on Binance after every BUY fills, so the
exchange itself enforces the SL even if the bot crashes, the VM reboots,
the Binance WebSocket disconnects, or systemd kills the process.

WITHOUT THIS MODULE (v13.5.1 default behavior):
  • Bot tracks SL in software (in-memory + bot_state.json)
  • Every cycle, bot checks current price vs each position's pos.sl
  • If price <= pos.sl, bot places a MARKET SELL
  • If bot is DOWN when SL would have triggered → user takes full unlimited loss
    until bot recovers or user manually intervenes

WITH THIS MODULE (v13.6 with NATIVE_SL_ENABLED=True):
  • Bot still tracks SL in software (unchanged — this is the primary path)
  • IN ADDITION, bot places a STOP_LOSS_LIMIT order on Binance after each BUY
  • If bot is DOWN when SL hits, exchange auto-fills the limit order
  • Worst case: 0.1% slippage between stop trigger and limit price (configurable)
  • When bot recovers, reconciler detects the closed position and reconciles state

CRITICAL CONSTRAINTS:
  1. STOP_LOSS_LIMIT requires `stopPrice` AND `price`. Layout:
       SELL: triggers when last_price <= stopPrice; places limit SELL at `price`
     Set `price` slightly BELOW `stopPrice` to ensure fill in fast-falling markets.
     We use price = stopPrice × (1 - NATIVE_SL_BUFFER_PCT).
  2. Binance Spot does NOT support OCO + SL trailing simultaneously. We use plain
     STOP_LOSS_LIMIT (NOT OCO) so the bot keeps full control over moving the SL
     when BE-lock fires (the bot CANCELS the old SL and places a new one).
  3. When the bot decides to exit (TP hit, time stop, manual close, BE-lock move),
     it MUST cancel the pending native SL order BEFORE placing the closing order,
     or the position will get sold twice.
  4. When the bot opens a position, it MUST track the order_id of the native SL
     so it can cancel it later. We store it on Position as `native_sl_order_id`.
  5. If native SL placement FAILS (rate limit, MIN_NOTIONAL, exchange filter),
     we LOG and CONTINUE — software SL still protects the position. Fail-soft.

SAFE FALLBACK:
If anything in this module raises, the bot continues without native SL — the
software SL in risk.py remains the primary safety mechanism. This module is
ADDITIONAL protection, not REPLACEMENT.
"""
from __future__ import annotations
import logging, time, threading
from typing import Optional

log = logging.getLogger("binbot")


class NativeSLManager:
    """Manages Binance STOP_LOSS_LIMIT orders attached to open positions.

    Lifecycle:
      after_buy(pos)       → place native SL on exchange, store order_id on pos
      before_sell(pos)     → cancel native SL on exchange before bot's own SELL
      on_sl_move(pos, ...) → cancel old native SL, place new one at moved price
      reconcile()          → detect orphan native SL orders (e.g. position closed
                             on exchange but bot doesn't know yet)
    """

    def __init__(self, exchange, cfg):
        self.ex = exchange
        self.cfg = cfg
        self._fail_count = 0
        self._last_fail_warn = 0
        # v15.2: NativeSL runs in threads (can't await). It needs its own
        # sync python-binance Client for STOP_LOSS_LIMIT order management.
        # The main Exchange class is fully async now.
        try:
            from binance.client import Client as _SyncClient
            self._cl = _SyncClient(cfg.API_KEY, cfg.API_SECRET,
                                   testnet=getattr(cfg, 'USE_TESTNET', False))
            # Patch self.ex.cl reference so all existing self.ex.cl calls work
            self.ex.cl = self._cl
            log.info("NativeSL: sync client initialized")
        except Exception as _e:
            log.warning(f"NativeSL: sync client init failed: {_e} — native SL disabled")
            self._cl = None
        self._moving_pairs = set()
        self._moving_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def attach(self, pos) -> bool:
        """Place a native STOP_LOSS_LIMIT for this position. Returns True if
        successfully placed, False on any failure (caller continues regardless).

        v13.5.5 FIX: retry-on-2010 - retries up to 3x with 2s, 4s backoff
        on insufficient-balance errors (typical post-BUY settlement delay)."""
        if not self.cfg.NATIVE_SL_ENABLED:
            return False
        import time
        last_err = None
        for attempt in range(3):
            try:
                if self._place_stop_loss_limit(pos):
                    if attempt > 0:
                        log.info(f"Native SL attached for {pos.pair} on retry attempt {attempt+1}")
                    return True
                return False
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if "-2010" in err_str or "insufficient balance" in err_str:
                    if attempt < 2:
                        backoff = (attempt + 1) * 2
                        log.debug(f"Native SL {pos.pair}: -2010 attempt {attempt+1}, sleeping {backoff}s")
                        time.sleep(backoff)
                        continue
                self._note_failure(f"attach({pos.pair}): {type(e).__name__}: {e}")
                return False
        self._note_failure(f"attach({pos.pair}): exhausted 3 retries; final: {last_err}")
        return False

    def detach(self, pos) -> bool:
        """Cancel the native SL for this position. Always called before bot
        executes its own SELL (TP hit, manual close, etc.). Idempotent — safe
        to call even if no native SL exists."""
        if not self.cfg.NATIVE_SL_ENABLED:
            return True
        order_id = getattr(pos, "native_sl_order_id", None)
        if not order_id:
            return True  # Nothing to cancel
        try:
            self.ex.cl.cancel_order(symbol=pos.pair, orderId=order_id)
            log.info(f"🛑 Native SL cancelled for {pos.pair} (orderId={order_id})")
            pos.native_sl_order_id = None
            return True
        except Exception as e:
            # Common: order already filled or cancelled (reconciler handles drift)
            err_str = str(e)
            if "Unknown order" in err_str or "-2011" in err_str:
                log.debug(f"Native SL already gone for {pos.pair}: {e}")
                pos.native_sl_order_id = None
                return True
            self._note_failure(f"detach({pos.pair}): {type(e).__name__}: {e}")
            return False

    def move(self, pos, new_sl_price: float) -> bool:
        """Move the native SL to a new price (called by BE-lock and trailing).
        Cancels old order, places new one. Returns True on success.

        v14.1 FIX (ISSUE E): pos.pair is added to _moving_pairs for the
        duration of detach + attach. reconcile_orphans() will skip any
        symbol in this set, preventing the sub-second race where it
        would otherwise see the freshly-placed new SL as an orphan
        (because pos.native_sl_order_id hasn't been updated yet).

        v14.6.4 AUDIT FIX (C3): previously called detach() BEFORE the
        docstring and BEFORE entering the _moving_lock, then called
        detach() AGAIN inside the lock. The first call ran outside lock
        protection, completely defeating the v14.1 race-condition guard.
        Now: single detach() inside the lock-protected region."""
        if not self.cfg.NATIVE_SL_ENABLED:
            return False
        with self._moving_lock:
            self._moving_pairs.add(pos.pair)
        try:
            # Cancel old, place new — both inside _moving_pairs guard
            if not self.detach(pos):
                return False
            # Update pos.sl to the new value before re-placing (caller already did this)
            if self.attach(pos):
                return True
            # v18.9.6: detach succeeded but re-attach FAILED → the position now has NO
            # exchange-side stop. Make it loud (was silent) so the naked window is visible;
            # the software SL covers it live, and recover_missing()/caller-retry re-place it.
            self._note_failure(f"move({pos.pair}): re-attach failed after detach — NO native SL until recovery")
            log.warning(f"⚠️ Native SL MOVE left {pos.pair} without an exchange stop "
                        f"(old cancelled, new re-attach failed) — software SL active, will retry/recover")
            return False
        finally:
            with self._moving_lock:
                self._moving_pairs.discard(pos.pair)

    def recover_missing(self, positions) -> int:
        """On startup: re-attach native SL for any open position missing it.
        Handles crash-during-BE-lock-move scenario where detach succeeded but
        attach never ran. Called once at bot startup after state is loaded."""
        if not self.cfg.NATIVE_SL_ENABLED:
            return 0
        recovered = 0
        for pos in positions:
            if not getattr(pos, 'native_sl_order_id', None):
                try:
                    ok = self.attach(pos)
                    if ok:
                        log.info(f'🛡️  Recovered missing native SL for {pos.pair} at startup')
                        recovered += 1
                    else:
                        log.warning(f'⚠️  Failed to recover native SL for {pos.pair} at startup — software SL active')
                except Exception as e:
                    log.warning(f'recover_missing {pos.pair}: {e}')
        return recovered

    def reconcile_orphans(self, positions, tg=None) -> int:
        """Walk all open Binance STOP_LOSS_LIMIT orders. If any reference a
        symbol the bot doesn't track as open, cancel + alert. Returns count of
        orphans found. Run periodically (e.g. every 10 cycles like reconciler).

        v14.1 FIX (ISSUE E): symbols currently in _moving_pairs are skipped.
        Without this, a move() in progress (post-detach, pre-attach) creates
        a window where the freshly-placed new SL exists on Binance but is
        not yet stored on pos.native_sl_order_id — we would otherwise cancel
        our own valid SL."""
        if not self.cfg.NATIVE_SL_ENABLED:
            return 0
        # Snapshot moving set under lock — release immediately so we don't
        # hold the lock during network I/O on the next line.
        with self._moving_lock:
            _moving_now = set(self._moving_pairs)
        try:
            tracked_pairs = {p.pair for p in positions}
            tracked_ids = {getattr(p, "native_sl_order_id", None) for p in positions
                           if getattr(p, "native_sl_order_id", None)}
            open_orders = self.ex.cl.get_open_orders()
            orphan_count = 0
            for o in open_orders:
                if o.get("type") != "STOP_LOSS_LIMIT":
                    continue
                sym = o.get("symbol")
                oid = o.get("orderId")
                # v14.1 FIX (ISSUE E): if this symbol is being moved RIGHT NOW,
                # skip — pos.native_sl_order_id is transiently None during the
                # move() detach→attach window.
                if sym in _moving_now:
                    log.debug(f"Native SL reconcile: skipping {sym} (move in progress)")
                    continue
                # Orphan if: symbol not tracked, OR order_id not on any position
                if sym not in tracked_pairs or oid not in tracked_ids:
                    log.warning(f"🛑 Orphan native SL on {sym} (orderId={oid}) — cancelling")
                    try:
                        self.ex.cl.cancel_order(symbol=sym, orderId=oid)
                        orphan_count += 1
                    except Exception as e:
                        log.warning(f"Orphan SL cancel failed: {e}")
            if orphan_count > 0 and tg:
                try:
                    tg.send(f"🛑 <b>Native SL Reconciler</b>\nCancelled {orphan_count} orphan stop-loss order(s)")
                except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
            return orphan_count
        except Exception as e:
            self._note_failure(f"reconcile: {type(e).__name__}: {e}")
            return 0

    # ── Internal ──────────────────────────────────────────────────────────────

    def _place_stop_loss_limit(self, pos) -> bool:
        # v15.13.1 ZOMBIE MAKER GUARD
        if hasattr(self, "_risk_ref") and self._risk_ref:
            positions = getattr(self._risk_ref, "positions", getattr(getattr(self._risk_ref, "risk", None), "positions", []))
            if pos not in positions:
                import logging
                logging.getLogger("bot").warning(f"Native SL abort: {pos.pair} already closed (Zombie Guard)")
                return False
        """Place a STOP_LOSS_LIMIT SELL order on Binance.

        Order config:
          symbol     = pos.pair
          side       = SELL
          type       = STOP_LOSS_LIMIT
          stopPrice  = pos.sl                                  (trigger price)
          price      = pos.sl × (1 - NATIVE_SL_BUFFER_PCT)     (limit fill price)
          quantity   = pos.qty (rounded to symbol's stepSize)
          timeInForce= GTC
        """
        # Detach any pre-existing SL first (BE-move case)
        if getattr(pos, "native_sl_order_id", None):
            self.detach(pos)

        # Symbol filters: stepSize, minQty, minNotional
        try:
            info = self.ex.cl.get_symbol_info(pos.pair)
            step_size, min_qty, min_notional, tick_size = NativeSLManager._extract_filters(info)
        except Exception as e:
            log.warning(f"Native SL: symbol_info failed for {pos.pair}: {e}")
            return False

        # Round quantity DOWN to stepSize (Binance rejects over-precision)
        # v13.5.5 P2: use wallet qty instead of pos.qty. Fees deduct from wallet
        # (e.g. 77 JUP bought, fees take 0.1 JUP, wallet has 76.9, state has 77).
        # If we ask Binance to sell 77 when wallet has 76.9, error -2010 fires.
        # Fix: query actual wallet, take min(state, wallet), subtract one step
        # for safety. Falls back to pos.qty if wallet check fails.
        try:
            asset = pos.pair.removesuffix("USDT")  # v14.5.1 FIX (audit #20): safer than .replace()
            wallet_bal = self.ex.cl.get_asset_balance(asset=asset)
            wallet_qty = float(wallet_bal.get("free", 0))
            target_qty = min(pos.qty, wallet_qty)
            # v13.5.6 FIX: choose the margin so the leftover dust is either
            # zero or above MIN_NOTIONAL. The previous flat 0.1% margin (Fix
            # #5 in v13.5.5) GUARANTEED sub-MIN_NOTIONAL dust on this bot's
            # actual position sizes (~$13-17), creating the orphan path:
            # native SL fills 6.47 of 6.48 TON, bot can't sell the 0.01
            # remainder ($0.02 < $5 NOTIONAL), position force-closes as DUST.
            #
            # Strategy: subtract just one step_size (sells effectively all
            # of target_qty). The attach() wrapper around this method already
            # has 3x retry-on-2010 with 2s/4s backoff to handle the rare case
            # where Binance settlement is still in flight. If qty_full
            # somehow falls below min_qty, fall back to the 0.1% margin so
            # we still get SOME native SL protection.
            qty_full = self._floor_to_step(max(target_qty - step_size, 0.0), step_size)
            qty = qty_full if qty_full >= min_qty else self._floor_to_step(target_qty * 0.999, step_size)
        except Exception as _e:
            log.debug(f"Native SL wallet check failed for {pos.pair}: {_e}; using state qty")
            qty = self._floor_to_step(pos.qty, step_size)
        if qty < min_qty:
            log.info(f"Native SL skipped for {pos.pair}: qty {qty} < minQty {min_qty}")
            return False

        stop_price = self._round_to_tick(pos.sl, tick_size)
        limit_price = self._round_to_tick(stop_price * (1 - self.cfg.NATIVE_SL_BUFFER_PCT), tick_size)

        # Notional check (Binance min trade size)
        notional = qty * limit_price
        if notional < min_notional:
            log.info(f"Native SL skipped for {pos.pair}: notional ${notional:.4f} < min ${min_notional}")
            return False

        try:
            order = self.ex.cl.create_order(
                symbol=pos.pair,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                quantity=f"{qty:.8f}",
                stopPrice=f"{stop_price:.8f}",
                price=f"{limit_price:.8f}",
                timeInForce="GTC",
            )
            pos.native_sl_order_id = order.get("orderId")
            log.info(f"🛡️  Native SL attached to {pos.pair}: stop=${stop_price:.6f} limit=${limit_price:.6f} qty={qty} orderId={pos.native_sl_order_id}")
            self._fail_count = 0
            return True
        except Exception as e:
            self._note_failure(f"create_order({pos.pair}): {type(e).__name__}: {e}")
            return False

    @staticmethod
    def _extract_filters(info: dict):
        """Extract LOT_SIZE, MIN_NOTIONAL, PRICE_FILTER from symbol info."""
        step_size = 0.00000001
        min_qty = 0.0
        min_notional = 5.0  # Binance default
        tick_size = 0.00000001
        for f in info.get("filters", []):
            ft = f.get("filterType")
            if ft == "LOT_SIZE":
                step_size = float(f.get("stepSize", step_size))
                min_qty = float(f.get("minQty", min_qty))
            elif ft == "PRICE_FILTER":
                tick_size = float(f.get("tickSize", tick_size))
            elif ft in ("NOTIONAL", "MIN_NOTIONAL"):
                # Binance uses NOTIONAL on newer markets, MIN_NOTIONAL on older
                min_notional = float(f.get("minNotional", f.get("notional", min_notional)))
        return step_size, min_qty, min_notional, tick_size

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        # Floor to multiple of step
        n = int(value / step)
        return round(n * step, 8)

    @staticmethod
    def _round_to_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return price
        n = round(price / tick)
        return round(n * tick, 8)

    def _note_failure(self, msg: str):
        self._fail_count += 1
        now = time.time()
        # Rate-limit warnings to once per minute to avoid log spam on persistent issues
        if now - self._last_fail_warn > 60:
            log.warning(f"Native SL failure #{self._fail_count}: {msg}")
            self._last_fail_warn = now
        else:
            log.debug(f"Native SL failure (suppressed): {msg}")


class NativeTPManager:
    """v14.4 — Manages Binance LIMIT SELL orders at the TP price.

    Placed when price hits TP and trailing activates in TREND_UP/SQUEEZE regime.
    Acts as exchange-side backup: if bot dies during trailing, limit sells at TP.

    Lifecycle:
      attach(pos)       → place LIMIT SELL at pos.tp, store orderId on pos
      detach(pos)       → cancel the limit order (trail closed higher or SL hit)
      check_filled(pos) → returns (True, fill_price) if order filled on exchange
    """

    def __init__(self, exchange, cfg):
        self.ex = exchange
        self.cfg = cfg

    def attach(self, pos) -> bool:
        if getattr(pos, 'native_tp_order_id', None):
            return True  # already placed
        # v14.7.4 FIX: retry-on-2010 with wallet-qty fallback. Same pattern as
        # NativeSLManager.attach. Root cause: right after BUY fills, Binance
        # deducts trading fee in the base asset (e.g. LTC), so wallet free qty
        # is ~0.1% below pos.qty. Asking to sell pos.qty → -2010 insufficient.
        # Fix: query wallet, take min(state, wallet), subtract one step_size for
        # safety. Three retries with 2s/4s backoff for settlement delays.
        import time as _t
        for _attempt in range(3):
            try:
                if self._place_tp_limit(pos):
                    if _attempt > 0:
                        log.info(f"Native TP attached for {pos.pair} on retry {_attempt+1}")
                    return True
                return False
            except Exception as _e:
                _err = str(_e).lower()
                if ("-2010" in _err or "insufficient balance" in _err) and _attempt < 2:
                    _backoff = (_attempt + 1) * 2
                    log.debug(f"Native TP {pos.pair}: -2010 attempt {_attempt+1}, sleeping {_backoff}s")
                    _t.sleep(_backoff)
                    continue
                log.warning(f"Native TP attach failed {pos.pair}: {_e} — software TP still active")
                return False
        log.warning(f"Native TP attach failed {pos.pair}: exhausted 3 retries — software TP still active")
        return False

    def _place_tp_limit(self, pos) -> bool:
        """Wallet-aware LIMIT SELL at pos.tp. Raises on -2010 so attach() can retry."""
        try:
            info = self.ex.cl.get_symbol_info(pos.pair)
            step_size, min_qty, min_notional, tick_size = NativeSLManager._extract_filters(info)
        except Exception as e:
            log.warning(f"Native TP symbol_info failed for {pos.pair}: {e}")
            return False
        try:
            asset = pos.pair.removesuffix("USDT")
            wallet_bal = self.ex.cl.get_asset_balance(asset=asset)
            wallet_qty = float(wallet_bal.get("free", 0))
            target_qty = min(pos.qty, wallet_qty)
            qty_full = NativeSLManager._floor_to_step(max(target_qty - step_size, 0.0), step_size)
            qty = qty_full if qty_full >= min_qty else NativeSLManager._floor_to_step(target_qty * 0.999, step_size)
        except Exception as _e:
            log.debug(f"Native TP wallet check failed for {pos.pair}: {_e}; using state qty")
            qty = NativeSLManager._floor_to_step(pos.qty, step_size)
        if qty < min_qty:
            log.info(f"Native TP skipped for {pos.pair}: qty {qty} < minQty {min_qty}")
            return False
        tp_price = NativeSLManager._round_to_tick(pos.tp, tick_size)
        try:
            _ts = f"{tick_size:.10f}".rstrip("0")
            _dec = len(_ts.split(".")[-1]) if "." in _ts else 2
        except Exception: _dec = 2
        if qty * tp_price < min_notional:
            log.info(f"Native TP skipped for {pos.pair}: notional ${qty*tp_price:.4f} < min ${min_notional}")
            return False
        order = self.ex.cl.order_limit_sell(
            symbol=pos.pair, quantity=qty, price=f"{tp_price:.{_dec}f}"
        )
        pos.native_tp_order_id = int(order['orderId'])
        log.info(f"🎯 Native TP attached {pos.pair}: limit {qty} @ ${tp_price:.6f} orderId={pos.native_tp_order_id}")
        return True

    def detach(self, pos) -> bool:
        order_id = getattr(pos, 'native_tp_order_id', None)
        if not order_id:
            return True
        try:
            self.ex.cl.cancel_order(symbol=pos.pair, orderId=order_id)
            pos.native_tp_order_id = None
            log.info(f"🎯 Native TP detached {pos.pair} (orderId={order_id})")
            return True
        except Exception as e:
            if any(c in str(e) for c in ["-2011", "Unknown order", "-2013"]):
                pos.native_tp_order_id = None
                return True
            log.warning(f"Native TP detach failed {pos.pair}: {e}")
            return False

    def check_filled(self, pos) -> tuple:
        order_id = getattr(pos, 'native_tp_order_id', None)
        if not order_id:
            return False, 0.0
        try:
            o = self.ex.cl.get_order(symbol=pos.pair, orderId=order_id)
            if (o.get('status') or '').upper() == 'FILLED':
                eq = float(o.get('executedQty') or 0)
                cq = float(o.get('cummulativeQuoteQty') or 0)
                fill_price = (cq / eq) if eq > 0 else pos.tp
                pos.native_tp_order_id = None
                log.info(f"🎯 Native TP filled {pos.pair} @ ${fill_price:.4f}")
                return True, fill_price
        except Exception as e:
            log.debug(f"Native TP check {pos.pair}: {e}")
        return False, 0.0
