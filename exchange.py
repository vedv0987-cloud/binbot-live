# BinBot v15.2 — exchange.py (Full Async)
# Replaces python-binance sync Client with native aiohttp + HMAC-SHA256.
# All API methods are `async def`. WebSocket stays threaded (python-binance).
import math, time, json, logging, threading, hashlib, hmac, urllib.parse, asyncio
from typing import Dict, Optional, List, Tuple
from models import Candle
log = logging.getLogger('binbot')

try:
    import aiohttp
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False
    log.error("❌ aiohttp not installed — run: pip install aiohttp")


class BinanceAPIError(Exception):
    """Drop-in replacement for python-binance BinanceAPIException."""
    def __init__(self, code=-1, message="Unknown error"):
        self.code = code
        self.message = message
        self.status_code = 400
        super().__init__(f"Binance [{code}]: {message}")


class Exchange:
    """v15.2: Fully async Exchange — aiohttp + Binance REST API.

    All public and signed endpoints use aiohttp for non-blocking I/O.
    HMAC-SHA256 signing done inline (no python-binance dependency for REST).

    Usage:
        ex = Exchange(cfg)
        await ex.init()           # must call before any API method
        candles = await ex.klines("BTCUSDT", "5m", 100)
        await ex.close()          # cleanup on shutdown
    """

    BASE = "https://api.binance.com"
    FAPI = "https://fapi.binance.com"

    def __init__(self, cfg):
        self.cfg = cfg
        self._api_key = cfg.API_KEY
        self._secret = cfg.API_SECRET.encode()
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(10)   # max 10 concurrent API calls
        self._time_offset = 0
        self.info: Dict = {}
        self._last_prices: Dict[str, float] = {}
        self._last_price_ts: Dict[str, float] = {}  # v16.0 AUDIT FIX M5: track price update time for staleness check
        self._last_time_sync: float = 0  # v16.0 AUDIT FIX L3: track last time-sync for periodic resync
        self.Err = BinanceAPIError  # backward compat: except self.Err
        # v18.7.1: paper/shadow trading removed — this build places REAL orders only.
        if getattr(cfg, 'USE_TESTNET', False):
            self.BASE = "https://testnet.binance.vision"
        try:
            from binance.client import Client
            self.cl = Client(self._api_key, cfg.API_SECRET, testnet=getattr(cfg, 'USE_TESTNET', False))  # v16.0 AUDIT FIX C6: was self._secret (bytes) — Client expects str, double-encoded broke signatures
        except ImportError:
            self.cl = None
            log.warning("python-binance not installed; sync klines unavailable.")
        log.info(f"🔥 Binance {'TEST' if getattr(cfg,'USE_TESTNET',False) else 'LIVE'} (async/aiohttp)")

    # ──────────────────────────────────────────────────────────────
    # Session + Signing
    # ──────────────────────────────────────────────────────────────

    async def init(self):
        """Async initialization — call once after construction."""
        await self._ensure_session()
        await self._sync_time()
        await self._load_exchange_info()

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self._api_key},
                timeout=aiohttp.ClientTimeout(total=10)
            )

    def _sign(self, params: dict) -> dict:
        """Add timestamp + HMAC-SHA256 signature to params."""
        # v16.0 AUDIT FIX L3: re-sync clock every 6 hours to prevent drift.
        # v18.5 AUDIT FIX (M1): previously every _sign() call in the 6h+ window
        # scheduled ANOTHER _sync_time() because _last_time_sync is only updated
        # after the await completes — producing a burst of duplicate syncs. Now we
        # optimistically stamp _last_time_sync BEFORE scheduling so only one resync
        # is in flight, and use get_running_loop() (no deprecated get_event_loop()).
        if time.time() - self._last_time_sync > 21600:  # 6h
            try:
                import asyncio as _aio
                loop = _aio.get_running_loop()
                self._last_time_sync = time.time()  # claim the slot up front
                loop.create_task(self._sync_time())
            except RuntimeError:
                pass  # no running loop (called from a plain thread) — re-syncs at next init
            except Exception:
                pass
        params['timestamp'] = int(time.time() * 1000) + self._time_offset
        # v18.9.9 (audit H2): widen the recv window (default 5s) so minor VM clock drift
        # doesn't make Binance reject signed orders (-1021) — which would silently block exits.
        params.setdefault('recvWindow', int(getattr(self.cfg, 'RECV_WINDOW_MS', 10000)))
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(self._secret, qs.encode(), hashlib.sha256).hexdigest()
        params['signature'] = sig
        return params

    async def close(self):
        """Cleanup aiohttp session on shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ──────────────────────────────────────────────────────────────
    # Low-level HTTP
    # ──────────────────────────────────────────────────────────────

    async def _public_get(self, endpoint, params=None, base=None):
        """Unsigned GET request."""
        await self._ensure_session()
        url = (base or self.BASE) + endpoint
        async with self._semaphore:
            async with self._session.get(url, params=params or {}) as r:
                data = await r.json()
                if isinstance(data, dict) and data.get('code', 0) < 0:
                    raise BinanceAPIError(data['code'], data.get('msg', ''))
                return data

    async def _signed_get(self, endpoint, params=None):
        """HMAC-signed GET request."""
        await self._ensure_session()
        params = self._sign(params or {})
        async with self._semaphore:
            async with self._session.get(f"{self.BASE}{endpoint}", params=params) as r:
                data = await r.json()
                if isinstance(data, dict) and data.get('code', 0) < 0:
                    raise BinanceAPIError(data['code'], data.get('msg', ''))
                return data

    async def _signed_post(self, endpoint, params=None):
        """HMAC-signed POST request."""
        await self._ensure_session()
        params = self._sign(params or {})
        async with self._semaphore:
            async with self._session.post(f"{self.BASE}{endpoint}", params=params) as r:
                data = await r.json()
                if isinstance(data, dict) and data.get('code', 0) < 0:
                    raise BinanceAPIError(data['code'], data.get('msg', ''))
                return data

    async def _signed_delete(self, endpoint, params=None):
        """HMAC-signed DELETE request."""
        await self._ensure_session()
        params = self._sign(params or {})
        async with self._semaphore:
            async with self._session.delete(f"{self.BASE}{endpoint}", params=params) as r:
                data = await r.json()
                if isinstance(data, dict) and data.get('code', 0) < 0:
                    raise BinanceAPIError(data['code'], data.get('msg', ''))
                return data

    async def _retry(self, coro_fn, max_retries=3):
        """Async retry with exponential backoff."""
        for attempt in range(max_retries):
            try:
                return await coro_fn()
            except BinanceAPIError as e:
                if attempt == max_retries - 1:
                    log.error(f"API failed after {max_retries} retries: {e.message}")
                    return {"error": e.message}
                wait = min(0.5 * (attempt + 1), 1.0)
                log.warning(f"⚠️ API error: {e.message} — retry {attempt+1}/{max_retries} in {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                if attempt == max_retries - 1:
                    return {"error": str(e)}
                await asyncio.sleep(min(0.5 * (attempt + 1), 1.0))
        return {"error": "max_retries_exceeded"}

    # ──────────────────────────────────────────────────────────────
    # Init helpers
    # ──────────────────────────────────────────────────────────────

    async def _sync_time(self):
        """Sync clock with Binance server to prevent recvWindow errors."""
        try:
            data = await self._public_get("/api/v3/time")
            local_ms = int(time.time() * 1000)
            srv_ms = data['serverTime']
            drift = abs(local_ms - srv_ms)
            if drift > 3000:
                self._time_offset = srv_ms - local_ms
                log.warning(f"⚠️ Clock drift: {drift}ms — applying offset "
                            f"(run `sudo timedatectl set-ntp true` to fix)")
            else:
                log.info(f"⏰ Time sync OK: {drift}ms drift")
            self._last_time_sync = time.time()  # v16.0 AUDIT FIX L3
        except Exception as e:
            log.debug(f"Time sync failed: {e}")

    async def _load_exchange_info(self):
        """Load per-symbol filters (lot size, tick size, min notional)."""
        try:
            data = await self._public_get("/api/v3/exchangeInfo")
            for s in data.get("symbols", []):
                f = {x["filterType"]: x for x in s.get("filters", [])}
                self.info[s["symbol"]] = {
                    "lot": f.get("LOT_SIZE", {}),
                    "price": f.get("PRICE_FILTER", {}),
                    "notional": f.get("NOTIONAL", {}),
                    "min_notional": f.get("MIN_NOTIONAL", {})
                }
            log.info(f"Loaded {len(self.info)} pairs")
        except Exception as e:
            log.error(f"Exchange info load failed: {e}")

    # ──────────────────────────────────────────────────────────────
    # Sync utility methods (no I/O — pure math)
    # ──────────────────────────────────────────────────────────────

    def get_min_notional(self, sym):
        """v8.4 FIX #5: Get per-pair minimum notional from Binance."""
        info = self.info.get(sym, {})
        mn = info.get("notional", {}).get("minNotional")
        if not mn:
            mn = info.get("min_notional", {}).get("minNotional")
        try:
            return float(mn) if mn else 6.0
        except Exception:
            return 6.0

    def price_sane(self, sym, price):
        """v8.4 FIX #2: Flash crash protection — reject if price deviates >15%."""
        last = self._last_prices.get(sym)
        if last and last > 0:
            # v16.0 AUDIT FIX M5: skip sanity check if last price is stale (>5min)
            # After bot restart during a crash, stored price is from pre-crash.
            # Recovery rally price would be falsely flagged as "flash crash".
            _last_ts = self._last_price_ts.get(sym, 0)
            _age = time.time() - _last_ts if _last_ts > 0 else 999
            if _age < 300:  # only check if last price is <5 minutes old
                deviation = abs(price - last) / last
                if deviation > 0.15:
                    log.warning(f"🚨 FLASH CRASH? {sym} price ${price:.4f} vs "
                                f"last ${last:.4f} ({deviation*100:.1f}% move)")
                    return False
        self._last_prices[sym] = price
        self._last_price_ts[sym] = time.time()  # v16.0 AUDIT FIX M5
        return True

    @staticmethod
    def _decimals_of(step_str):
        """Decimals implied by a stepSize/tickSize: '0.001'->3, '1'->0."""
        s = format(float(step_str), 'f').rstrip('0')
        return len(s.split('.')[1]) if '.' in s else 0

    def _qty_decimals(self, sym):
        step = self.info.get(sym, {}).get("lot", {}).get("stepSize")
        return None if not step else self._decimals_of(step)

    def _price_decimals(self, sym):
        tick = self.info.get(sym, {}).get("price", {}).get("tickSize")
        return None if not tick else self._decimals_of(tick)

    def rnd(self, sym, q):
        """Round quantity to valid lot step size (precision-safe)."""
        d = self._qty_decimals(sym)
        if d is None:
            log.warning(f"⚠️ {sym}: lot filters not loaded — skipping order (qty=0)")
            return 0.0
        step = float(self.info[sym]["lot"]["stepSize"])
        minq = float(self.info[sym]["lot"].get("minQty", "0") or "0")
        r = round(math.floor(q / step) * step, d) if step > 0 else round(q, d)
        return r if r >= minq else 0.0

    def rnd_price(self, sym, price):
        """v7: Round price to valid tick size (precision-safe)."""
        d = self._price_decimals(sym)
        if d is None:
            return round(price, 8)
        tick = float(self.info[sym]["price"]["tickSize"])
        return round(math.floor(price / tick) * tick, d) if tick > 0 else round(price, d)

    def _fmt_qty(self, sym, qty):
        """Format quantity to the symbol's exact lot precision."""
        d = self._qty_decimals(sym)
        return f"{qty:.8f}" if d is None else f"{qty:.{d}f}"

    def _fmt_price(self, sym, price):
        """Format price to the symbol's exact tick precision."""
        d = self._price_decimals(sym)
        return f"{price:.8f}" if d is None else f"{price:.{d}f}"

    @staticmethod
    def _validate_candles(candles):
        """v15.1: Reject corrupt/anomalous candles before strategies see them."""
        if not candles:
            return candles
        valid = []
        for c in candles:
            try:
                if c.c <= 0 or c.o <= 0 or c.h <= 0 or c.l <= 0: continue
                if c.h < c.l: continue
                if c.l > c.c or c.l > c.o: continue
                if c.h < c.c or c.h < c.o: continue
                if c.c > 0 and (c.h - c.l) / c.c > 0.25:
                    c.h = max(c.o, c.c)
                    c.l = min(c.o, c.c)
                if c.v < 0: continue
                valid.append(c)
            except Exception:
                continue
        if len(valid) < len(candles):
            rejected = len(candles) - len(valid)
            log.warning(f"⚠️ Candle validation: {rejected}/{len(candles)} rejected")
        return valid

    # ──────────────────────────────────────────────────────────────
    # Async API — Market Data (public, unsigned)
    # ──────────────────────────────────────────────────────────────

    async def klines(self, sym, iv="5m", lim=100):
        """Fetch OHLCV candles."""
        try:
            from feature_flags import get as _ff
            _batched = _ff("batched_klines", False)
        except Exception:
            _batched = False
        if _batched and lim > 1000:
            return await self._klines_batched(sym, iv, lim)
        # v18.9.9 (audit H1): fetch one extra bar and drop the still-forming last candle so
        # all TA runs on CLOSED bars only (anti-repaint). Flag-gated for easy revert.
        _drop = bool(getattr(self.cfg, "DROP_UNCLOSED_CANDLE", True))
        _req_lim = lim + 1 if _drop else lim
        try:
            raw = await self._public_get("/api/v3/klines",
                                         {"symbol": sym, "interval": iv, "limit": _req_lim})
            try:
                _fo = getattr(self, "_failover_mgr", None)
                if _fo is not None:
                    _fo.report_binance_ok()
            except Exception:
                pass
            _cs = [Candle(k[0]/1000, float(k[1]), float(k[2]),
                          float(k[3]), float(k[4]), float(k[5])) for k in raw]
            if _drop and len(_cs) > 1:
                _cs = _cs[:-1]  # drop the forming bar
            return self._validate_candles(_cs)
        except Exception as e:
            log.warning(f"klines {sym} {iv} failed: {e}")
            # Failover
            try:
                _fo = getattr(self, "_failover_mgr", None)
                if _fo is not None and _fo.report_binance_error(e):
                    _backup = _fo.get_klines(sym, iv, lim)
                    if _backup:
                        log.info(f"  Failover: {len(_backup)} candles for {sym} {iv}")
                        return self._validate_candles(
                            [Candle(k[0]/1000, float(k[1]), float(k[2]),
                                    float(k[3]), float(k[4]), float(k[5])) for k in _backup])
            except Exception as _fe:
                log.debug(f"Failover klines fallback failed: {_fe}")
            return []

    async def _klines_batched(self, sym, iv="5m", lim=2000):
        """Fetch >1000 candles via paginated endTime requests."""
        all_candles = []
        end_time = None
        per_req = 1000
        remaining = lim
        try:
            while remaining > 0:
                fetch = min(per_req, remaining)
                params = {"symbol": sym, "interval": iv, "limit": fetch}
                if end_time:
                    params["endTime"] = end_time
                raw = await self._public_get("/api/v3/klines", params)
                if not raw:
                    break
                batch = [Candle(k[0]/1000, float(k[1]), float(k[2]),
                                float(k[3]), float(k[4]), float(k[5])) for k in raw]
                all_candles = batch + all_candles
                remaining -= len(raw)
                if len(raw) < fetch:
                    break
                end_time = int(raw[0][0]) - 1
                await asyncio.sleep(0.1)
            log.info(f"📡 Batched klines {sym} {iv}: {len(all_candles)} candles")
            return all_candles
        except Exception as e:
            log.warning(f"batched klines {sym} failed: {e} — falling back")
            try:
                raw = await self._public_get("/api/v3/klines",
                                             {"symbol": sym, "interval": iv, "limit": 1000})
                return [Candle(k[0]/1000, float(k[1]), float(k[2]),
                               float(k[3]), float(k[4]), float(k[5])) for k in raw]
            except Exception:
                return []

    async def tickers(self):
        """Fetch all ticker prices."""
        try:
            data = await self._public_get("/api/v3/ticker/price")
            return {t["symbol"]: float(t["price"]) for t in data}
        except Exception as e:
            log.warning(f"tickers failed: {e}")
            return {}

    async def order_book(self, sym, limit=20):
        """v7: Get order book for imbalance detection."""
        try:
            ob = await self._public_get("/api/v3/depth",
                                        {"symbol": sym, "limit": limit})
            bids = [(float(b[0]), float(b[1])) for b in ob.get("bids", [])]
            asks = [(float(a[0]), float(a[1])) for a in ob.get("asks", [])]
            return bids, asks
        except Exception as e:
            log.warning(f"orderbook {sym} failed: {e}")
            return [], []

    async def funding_rate(self, sym):
        """v7: Get futures funding rate (sentiment indicator)."""
        try:
            data = await self._public_get("/fapi/v1/fundingRate",
                                          {"symbol": sym, "limit": 1},
                                          base=self.FAPI)
            if data and isinstance(data, list):
                return float(data[0].get("fundingRate", 0))
        except Exception as _e:
            log.debug(f"Funding rate {sym}: {_e}")
        return 0.0

    # ──────────────────────────────────────────────────────────────
    # Async API — Account + Trading (signed)
    # ──────────────────────────────────────────────────────────────

    async def balance(self, a="USDT"):
        """Get free balance for a single asset."""
        try:
            data = await self._signed_get("/api/v3/account")
            for b in data.get("balances", []):
                if b["asset"] == a:
                    return float(b["free"])
            return 0.0
        except Exception as e:
            log.warning(f"balance {a} failed: {e}")
            return 0.0

    async def get_account(self):
        """Full account info (balances, permissions, etc.)."""
        return await self._signed_get("/api/v3/account")

    async def get_asset_balance(self, asset="USDT"):
        """Returns {"asset": X, "free": Y, "locked": Z} dict."""
        try:
            data = await self._signed_get("/api/v3/account")
            for b in data.get("balances", []):
                if b["asset"] == asset:
                    return b
            return {"asset": asset, "free": "0", "locked": "0"}
        except Exception as e:
            log.warning(f"get_asset_balance {asset} failed: {e}")
            return {"asset": asset, "free": "0", "locked": "0"}

    async def get_symbol_ticker(self, symbol):
        """Get current price for a single symbol."""
        data = await self._public_get("/api/v3/ticker/price", {"symbol": symbol})
        return data

    async def get_24h_ticker(self):
        """Get 24h ticker stats for all symbols (replaces cl.get_ticker())."""
        return await self._public_get("/api/v3/ticker/24hr")

    async def create_order(self, **kwargs):
        """Raw order creation — used by bot.py startup cleanup."""
        async def _do():
            return await self._signed_post("/api/v3/order", kwargs)
        return await self._retry(_do)

    async def cancel_order(self, symbol, orderId):
        """Cancel an open order."""
        async def _do():
            return await self._signed_delete("/api/v3/order",
                                             {"symbol": symbol, "orderId": orderId})
        return await self._retry(_do)

    async def get_open_orders(self, symbol):
        """Get all open orders for a symbol."""
        async def _do():
            return await self._signed_get("/api/v3/openOrders", {"symbol": symbol})
        return await self._retry(_do)

    async def get_all_orders(self, symbol, limit=10):
        """Get recent orders for a symbol."""
        async def _do():
            return await self._signed_get("/api/v3/allOrders",
                                          {"symbol": symbol, "limit": limit})
        return await self._retry(_do)

    # ──────────────────────────────────────────────────────────────
    # Trading — Buy / Sell / TWAP
    # ──────────────────────────────────────────────────────────────



    async def buy(self, sym, qty):
        """Market buy with pre-checks (min notional, balance, flash crash)."""
        qty = self.rnd(sym, qty)
        if qty <= 0:
            return {"error": "qty_small"}
        # Price + min notional check
        try:
            ticker = await self.get_symbol_ticker(sym)
            price = float(ticker['price'])
            min_not = self.get_min_notional(sym)
            if qty * price < min_not:
                log.warning(f"⚠️ Below min notional {sym}: ${qty*price:.2f} < ${min_not}")
                return {"error": "below_min_notional"}
        except Exception as e:
            log.warning(f"⚠️ Price fetch failed {sym}: {e} — aborting buy")
            return {"error": "data_fetch_failed"}
        # Balance + flash crash check
        try:
            bal = await self.balance("USDT")
            if bal < qty * price * 1.01:
                log.warning(f"⚠️ No USDT for {sym}: ${bal:.2f}")
                return {"error": "no_balance"}
            if not self.price_sane(sym, price):
                return {"error": "price_insane"}
        except Exception as e:
            log.warning(f"⚠️ Balance check failed {sym}: {e} — aborting buy")
            return {"error": "data_fetch_failed"}

        async def _do_buy():
            o = await self._signed_post("/api/v3/order", {
                "symbol": sym, "side": "BUY", "type": "MARKET",
                "quantity": self._fmt_qty(sym, qty)
            })
            log.info(f"✅ BUY {sym} Qty:{qty} ID:{o.get('orderId')}")
            return o
        return await self._retry(_do_buy)

    async def buy_limit(self, sym, qty, price, post_only=True):
        """v14.6.2: post-only LIMIT_MAKER — guarantees maker fee."""
        qty = self.rnd(sym, qty)
        price = self.rnd_price(sym, price)
        if qty <= 0:
            return {"error": "qty_small"}
        order_type = "LIMIT_MAKER" if post_only else "LIMIT"
        try:
            params = {"symbol": sym, "side": "BUY", "type": order_type,
                      "quantity": self._fmt_qty(sym, qty), "price": self._fmt_price(sym, price)}
            if order_type == "LIMIT":
                params["timeInForce"] = "GTC"
            o = await self._signed_post("/api/v3/order", params)
            log.info(f"✅ {order_type} BUY {sym} qty={qty} @${price} ID:{o.get('orderId')}")
            return o
        except BinanceAPIError as e:
            if "-2010" in str(e) or "would immediately match" in str(e).lower():
                log.info(f"📐 {order_type} {sym} skipped — price moved through ${price:.6f}")
                return {"error": "post_only_reject"}
            log.warning(f"{order_type} {sym} failed: {e.message}")
            return {"error": e.message}
        except Exception as e:
            log.warning(f"{order_type} {sym} exception: {e}")
            return {"error": str(e)}

    async def buy_twap(self, sym, qty, n_chunks=4, interval_sec=8):
        """v15.1: Time-Weighted Average Price execution.
        Splits large orders into n_chunks smaller orders spaced interval_sec apart."""
        qty = self.rnd(sym, qty)
        if qty <= 0:
            return {"error": "qty_small"}
        # Check if TWAP is needed
        try:
            bids, asks = await self.order_book(sym, limit=5)
            if asks:
                top_ask_depth = sum(a[1] * a[0] for a in asks[:3])
                order_value = qty * asks[0][0]
                if order_value < top_ask_depth * 0.3:
                    log.info(f"📐 TWAP skip {sym}: order ${order_value:.2f} < 30% depth")
                    return await self.buy(sym, qty)
        except Exception:
            pass
        chunk_qty = qty / n_chunks
        total_filled = 0.0
        total_cost = 0.0
        fills = []
        for i in range(n_chunks):
            remaining = qty - total_filled
            this_chunk = min(chunk_qty, remaining)
            this_chunk = self.rnd(sym, this_chunk)
            if this_chunk <= 0:
                break
            result = await self.buy(sym, this_chunk)
            if "error" in result:
                if total_filled > 0:
                    log.warning(f"📐 TWAP {sym} partial: {i}/{n_chunks} chunks")
                    break
                return result
            try:
                filled_qty = float(result.get("executedQty", this_chunk))
                # v16.0 AUDIT FIX M6: compute chunk VWAP from ALL fills, not just first.
                # Thinly-traded alts fill across multiple price levels per chunk.
                _chunk_fills = result.get("fills", [])
                if _chunk_fills:
                    _fq_sum = sum(float(f.get("qty", 0)) for f in _chunk_fills)
                    _fc_sum = sum(float(f.get("qty", 0)) * float(f.get("price", 0)) for f in _chunk_fills)
                    avg_price = (_fc_sum / _fq_sum) if _fq_sum > 0 else 0
                else:
                    avg_price = float(result.get("price", 0)) or 0
                if avg_price <= 0:
                    avg_price = float(result.get("price", 0)) or 0
                total_filled += filled_qty
                total_cost += filled_qty * avg_price
                fills.append({"chunk": i+1, "qty": filled_qty, "price": avg_price})
            except Exception:
                total_filled += this_chunk
                fills.append({"chunk": i+1, "qty": this_chunk, "price": 0})
            if i < n_chunks - 1:
                await asyncio.sleep(interval_sec)
        vwap = total_cost / total_filled if total_filled > 0 else 0
        log.info(f"📐 TWAP {sym} done: {len(fills)}/{n_chunks} chunks | "
                 f"Qty:{total_filled:.6f} | VWAP:${vwap:.4f}")
        return {"orderId": f"TWAP_{int(time.time())}", "executedQty": str(total_filled),
                "status": "FILLED", "twap_vwap": vwap, "twap_chunks": len(fills),
                "fills": [{"price": str(vwap), "qty": str(total_filled)}]}

    async def sell(self, sym, qty):
        """Market sell with partial-fill detection."""
        # Auto-adjust to actual balance
        try:
            asset = sym.replace("USDT", "")
            bal_data = await self.get_asset_balance(asset)
            actual = float(bal_data.get("free", 0))
            if actual > 0 and actual < qty:
                qty = actual
        except Exception as _e:
            log.debug(f"Sell balance check: {_e}")
        qty = self.rnd(sym, qty)
        if qty <= 0:
            return {"error": "qty_small"}
        # Flash crash check (still sell — but log)
        try:
            ticker = await self.get_symbol_ticker(sym)
            price = float(ticker['price'])
            if not self.price_sane(sym, price):
                log.warning(f"🚨 Price insane for {sym}, selling anyway as safety exit")
        except Exception as _e:
            log.debug(f"Sell price check: {_e}")

        async def _do_sell():
            o = await self._signed_post("/api/v3/order", {
                "symbol": sym, "side": "SELL", "type": "MARKET",
                "quantity": self._fmt_qty(sym, qty)
            })
            log.info(f"✅ SELL {sym} Qty:{qty} ID:{o.get('orderId')}")
            return o
        result = await self._retry(_do_sell)

        # Partial fill detection (from v13.5.3 audit Bug #7/#8)
        if "error" not in result:
            try:
                executed = float(result.get("executedQty", 0))
                orig = float(result.get("origQty", qty))
                status = (result.get("status") or "").upper()
                fully_filled = (status == "FILLED") or (
                    orig > 0 and executed >= orig * 0.99)
                if not fully_filled:
                    log.warning(f"⚠️ PARTIAL SELL {sym}: executed={executed} of "
                                f"orig={orig} status={status}")
                    return {"error": "partial_fill", "executed": executed,
                            "orig": orig, "status": status, "requested": qty}
            except Exception as _e:
                log.debug(f"executedQty parse failed: {_e}")
                try:
                    asset = sym.replace("USDT", "")
                    bal_data = await self.get_asset_balance(asset)
                    remaining = float(bal_data.get("free", 0))
                    if remaining > qty * 0.01:
                        log.warning(f"⚠️ PARTIAL SELL {sym} (fallback): "
                                    f"{remaining} remaining")
                        return {"error": "partial_fill", "remaining": remaining,
                                "requested": qty}
                except Exception as _e2:
                    log.debug(f"balance fallback failed: {_e2}")
        return result

    # ──────────────────────────────────────────────────────────────
    # Convenience wrappers (for bot.py startup code that used cl.xxx)
    # ──────────────────────────────────────────────────────────────

    async def get_exchange_info(self):
        """Raw exchange info response."""
        return await self._public_get("/api/v3/exchangeInfo")

    async def get_order(self, symbol, orderId):
        """Get a specific order by orderId."""
        return await self._signed_get("/api/v3/order",
                                      {"symbol": symbol, "orderId": orderId})

    async def get_my_trades(self, symbol, orderId=None):
        """Get trade fills for a symbol (optionally filtered by orderId)."""
        params = {"symbol": symbol}
        if orderId:
            params["orderId"] = orderId
        return await self._signed_get("/api/v3/myTrades", params)

    async def get_ticker(self, symbol=None):
        """Get 24h ticker for one or all symbols."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._public_get("/api/v3/ticker/24hr", params)
        
    async def get_all_bba(self):
        """v16.0 REST fallback for @bookTicker to avoid 1GB RAM WS crash"""
        try:
            data = await self._public_get("/api/v3/ticker/bookTicker")
            if isinstance(data, list):
                return {item["symbol"].upper(): {"b": float(item.get("bidPrice", 0)), "B": float(item.get("bidQty", 0)), "a": float(item.get("askPrice", 0)), "A": float(item.get("askQty", 0))} for item in data}
        except Exception: pass
        return {}

    # ──────────────────────────────────────────────────────────────
    # Sync klines helper — for non-async callers (risk.py, analytics, etc.)
    # ──────────────────────────────────────────────────────────────

    def klines_sync(self, sym, iv="5m", lim=100):
        """v15.3 FIX: Blocking sync klines for threads/sync code that can't await.
        Uses python-binance sync Client (self.cl, set by NativeSLManager).
        Returns list of Candle objects, same format as async klines()."""
        _cl = getattr(self, "cl", None)
        if _cl is None:
            log.debug(f"klines_sync({sym}): no sync client — returning []")
            return []
        try:
            raw = _cl.get_klines(symbol=sym, interval=iv, limit=lim)
            candles = [Candle(k[0]/1000, float(k[1]), float(k[2]),
                              float(k[3]), float(k[4]), float(k[5])) for k in raw]
            return self._validate_candles(candles)
        except Exception as e:
            log.warning(f"klines_sync {sym} {iv} failed: {e}")
            return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WebSocket — stays threaded (python-binance ThreadedWebsocketManager)
# v15.3 FIX: spawn WS init in a CLEAN thread to avoid
# "This event loop is already running" conflict with asyncio.run().
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LivePrices:
    def __init__(self, symbols):
        self.symbols = [s.lower() for s in symbols]
        self.prices: Dict[str, float] = {}
        self.last_update: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._running = False
        self._task = None
        self._api_key = None
        self._api_secret = None
        self.on_book_ticker = None

    def start(self, api_key, api_secret):
        """v16: Pure asyncio websockets implementation (Phase 1)"""
        self._api_key = api_key
        self._api_secret = api_secret
        self.stop()
        
        self._running = True
        self._task = asyncio.create_task(self._listen_to_websockets())
        log.info("  WS boot dispatched — connecting natively in background")

    async def _listen_to_websockets(self):
        # v18.5 AUDIT FIX (H2): self-healing reconnect loop. Previously this coroutine
        # exited and set _running=False on the FIRST disconnect — the price feed died
        # permanently until the cycle watchdog (90s later) restarted it, leaving a long
        # price-blind window. Now it reconnects internally with exponential backoff and
        # only stops when stop() flips _running / cancels the task.
        import aiohttp
        streams = [f"{s}@miniTicker" for s in self.symbols]  # @bookTicker intentionally
        # excluded (RAM/spam); micro-price is fed by a REST poll in bot.py instead.
        stream_str = "/".join(streams)
        uri = f"wss://stream.binance.com:9443/stream?streams={stream_str}"
        backoff = 1.0
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(uri, heartbeat=60, receive_timeout=60) as ws:
                        log.info(f"🔌 WebSocket: {len(self.symbols)} streams connected (aiohttp)")
                        backoff = 1.0  # reset backoff after a successful connect
                        while self._running:
                            try:
                                msg = await ws.receive_json()
                                self._on_msg(msg)
                            except asyncio.TimeoutError:
                                continue
                            except TypeError:
                                # non-JSON frame (e.g. close/ping) — if socket closed, break to reconnect
                                if ws.closed:
                                    break
            except asyncio.CancelledError:
                raise  # stop() cancelled us — propagate so the task ends cleanly
            except Exception as e:
                if not self._running:
                    break
                log.warning(f"⚠️ WS disconnect: {e} — self-healing reconnect in {backoff:.0f}s")
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, 30.0)  # exponential backoff, capped at 30s
        log.info("🔌 WebSocket listener stopped")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    def _on_msg(self, msg):
        if not self._running:
            return
            
        stream = msg.get("stream", "")
        data = msg.get("data", {})
        if not data:
            return
            
        sym = data.get("s", "")
        if not sym:
            return

        # Handle @bookTicker
        if "@bookTicker" in stream:
            try:
                b = float(data["b"]); B = float(data["B"])
                a = float(data["a"]); A = float(data["A"])
                if self.on_book_ticker:
                    self.on_book_ticker(sym, b, B, a, A)
            except Exception: pass
            return
            
        # Handle @miniTicker (prices)
        price = float(data.get("c", 0))
            
        if price <= 0:
            return
            
        with self._lock:
            self.prices[sym] = price
            self.last_update[sym] = time.time()

    def get_prices(self):
        with self._lock:
            return dict(self.prices)

    @property
    def is_active(self):
        if not self._running:
            return False
        now = time.time()
        with self._lock:
            if not self.last_update:
                return False
            return (now - max(self.last_update.values())) < 30


