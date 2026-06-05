# BinBot v13.6 — exchange_failover.py
"""Multi-exchange price-data failover.

PURPOSE:
When Binance is having issues (rate-limit storms, regional restrictions,
brief API outages), we want the bot's price-data view to keep working using
Bybit/OKX as fallback sources. This is READ-ONLY failover — orders ALWAYS
go to Binance because that's where your funds are. We never trade on a
different exchange than where the capital sits.

WHAT IT FAILS OVER:
  • get_ticker_price(symbol) — when Binance ticker errors persist
  • get_klines(symbol, interval, limit) — when Binance klines error persist

WHAT IT DOES NOT FAIL OVER:
  • create_order — orders ALWAYS go to Binance
  • get_account / get_asset_balance — always Binance (your funds are there)
  • get_open_orders / cancel_order — always Binance
  • WebSocket streams — Binance only (failover provider's WS not connected)

DESIGN:
  • Health monitor counts consecutive Binance errors
  • If error count exceeds EXCHANGE_FAILOVER_THRESHOLD (default 60s of failures),
    failover engages and price reads route to Bybit, then OKX as second backup
  • Once Binance recovers (1 successful call), failover automatically deactivates
  • Telegram alert on engage + disengage so user knows what's happening
  • Uses ccxt library (must be installed: `pip install ccxt`)
  • If ccxt not installed → module is a no-op, bot continues with Binance-only

FAILURE MODES:
  • Binance down + Bybit/OKX rate-limited → bot uses last known price (cached)
  • All three down → bot waits with exponential backoff, no panic
  • The bot never trades during failover ON — order endpoint goes to Binance,
    which is currently failing, so create_order will fail and risk.py's
    blind-order guards prevent any state corruption.
"""
from __future__ import annotations
import logging, time, threading
from typing import Optional, List

log = logging.getLogger("binbot")

# ccxt is optional; if missing this module is a no-op
try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False


class ExchangeFailover:
    """Health-monitored read-only failover layer for price data."""

    # Symbol mapping: Binance USDT pairs → ccxt unified ('XXX/USDT')
    @staticmethod
    def _to_ccxt(sym: str) -> str:
        if sym.endswith("USDT"):
            return f"{sym[:-4]}/USDT"
        return sym

    # Interval mapping: Binance → ccxt unified
    _INTERVAL_MAP = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "4h": "4h", "1d": "1d",
    }

    def __init__(self, cfg, tg=None):
        self.cfg = cfg
        self.tg = tg
        self._lock = threading.Lock()
        self._first_error_ts = 0.0  # When did Binance start failing?
        self._error_count = 0
        self._failover_active = False
        self._exchanges: List = []
        self._init_backups()

    def _init_backups(self):
        """Initialize ccxt backup exchanges (Bybit, OKX). Read-only — no API keys."""
        if not CCXT_AVAILABLE:
            log.info("ccxt not installed — exchange failover disabled (Binance-only)")
            return
        if not self.cfg.EXCHANGE_FAILOVER_ENABLED:
            return
        try:
            # Public-only (no API keys needed for ticker/klines)
            self._exchanges.append({
                "name": "bybit",
                "client": ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}}),
            })
            self._exchanges.append({
                "name": "okx",
                "client": ccxt.okx({"enableRateLimit": True}),
            })
            log.info(f"🔄 Exchange Failover initialized: {len(self._exchanges)} backup exchanges (read-only)")
        except Exception as e:
            log.warning(f"Failover backup init failed: {e}")
            self._exchanges = []

    # ── Public API ────────────────────────────────────────────────────────────

    def report_binance_ok(self):
        """Called by exchange.py wrapper after every successful Binance call."""
        with self._lock:
            if self._error_count > 0 or self._failover_active:
                if self._failover_active:
                    log.info("✅ Binance recovered — exchange failover DISENGAGED")
                    if self.tg:
                        try: self.tg.send("✅ <b>Failover Disengaged</b>\nBinance API recovered, primary route restored")
                        except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
                self._error_count = 0
                self._first_error_ts = 0
                self._failover_active = False

    def report_binance_error(self, err: Exception) -> bool:
        """Called by exchange.py wrapper after Binance call failure.
        Returns True if failover is now active and caller should try backup."""
        if not self.cfg.EXCHANGE_FAILOVER_ENABLED or not self._exchanges:
            return False
        with self._lock:
            now = time.time()
            if self._first_error_ts == 0:
                self._first_error_ts = now
            self._error_count += 1
            error_window_s = now - self._first_error_ts
            threshold = self.cfg.EXCHANGE_FAILOVER_THRESHOLD
            if error_window_s >= threshold and not self._failover_active:
                self._failover_active = True
                log.warning(
                    f"🔄 Binance API failing for {error_window_s:.0f}s "
                    f"({self._error_count} errors) — ENGAGING FAILOVER to backups"
                )
                if self.tg:
                    try:
                        self.tg.send(
                            f"🔄 <b>Failover Engaged</b>\n"
                            f"Binance API: {self._error_count} errors in {error_window_s:.0f}s\n"
                            f"Routing reads to backup exchanges (Bybit/OKX)\n"
                            f"⚠️ Trading paused until Binance recovers"
                        )
                    except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
            return self._failover_active

    @property
    def is_active(self) -> bool:
        return self._failover_active

    # ── Failover queries ──────────────────────────────────────────────────────

    def get_ticker_price(self, sym: str) -> Optional[float]:
        """Try each backup in order. Return first successful price, else None."""
        if not self._failover_active:
            return None
        ccxt_sym = self._to_ccxt(sym)
        for ex in self._exchanges:
            try:
                t = ex["client"].fetch_ticker(ccxt_sym)
                price = float(t.get("last") or t.get("close") or 0)
                if price > 0:
                    log.debug(f"Failover price {sym} from {ex['name']}: {price}")
                    return price
            except Exception as e:
                log.debug(f"Failover {ex['name']} ticker {sym} failed: {e}")
        return None

    def get_klines(self, sym: str, interval: str, limit: int = 100) -> Optional[List]:
        """Return klines via backup exchange. Format matches Binance:
        [open_time, open, high, low, close, volume, ...]"""
        if not self._failover_active:
            return None
        ccxt_sym = self._to_ccxt(sym)
        ccxt_tf = self._INTERVAL_MAP.get(interval, interval)
        for ex in self._exchanges:
            try:
                ohlcv = ex["client"].fetch_ohlcv(ccxt_sym, ccxt_tf, limit=limit)
                if ohlcv:
                    # Convert ccxt format [ts,o,h,l,c,v] to Binance-ish format
                    # (Binance kline arrays have 12 fields; we pad with zeros)
                    out = []
                    for k in ohlcv:
                        out.append([
                            int(k[0]),    # open time
                            str(k[1]),    # open
                            str(k[2]),    # high
                            str(k[3]),    # low
                            str(k[4]),    # close
                            str(k[5]),    # volume
                            int(k[0]),    # close time (use open time as approx)
                            "0", 0, "0", "0", "0",  # quote vol, trades, etc — zeros are fine
                        ])
                    log.debug(f"Failover klines {sym}/{interval} from {ex['name']}: {len(out)} candles")
                    return out
            except Exception as e:
                log.debug(f"Failover {ex['name']} klines {sym}/{interval} failed: {e}")
        return None
