import asyncio
import json
import logging
import urllib.request
import websockets
from collections import defaultdict
import time

log = logging.getLogger('binbot')

class LocalOrderBook:
    """
    v16: Asynchronous Local Order Book (LOB) manager.
    Downloads initial snapshot via REST, then applies @depth WS updates
    for zero-latency liquidity queries.
    """
    def __init__(self, symbols):
        self.symbols = [s.lower() for s in symbols]
        self.bids = defaultdict(dict)
        self.asks = defaultdict(dict)
        self.last_update_id = {}
        self._running = False
        self._task = None

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_manager())
        log.info(f"📚 LocalOrderBook started for {len(self.symbols)} symbols")

    async def _fetch_snapshot(self, sym):
        try:
            url = f"https://api.binance.com/api/v3/depth?symbol={sym.upper()}&limit=1000"
            # In a true async environment, use aiohttp. Using urllib for minimal deps compatibility.
            loop = asyncio.get_event_loop()
            req = urllib.request.Request(url, headers={"User-Agent": "BinBot/16"})
            resp = await loop.run_in_executor(None, urllib.request.urlopen, req, 5)
            data = json.loads(resp.read().decode())
            
            self.last_update_id[sym] = data['lastUpdateId']
            
            for p, q in data['bids']:
                if float(q) > 0: self.bids[sym][float(p)] = float(q)
            for p, q in data['asks']:
                if float(q) > 0: self.asks[sym][float(p)] = float(q)
                
        except Exception as e:
            log.warning(f"LOB Snapshot failed for {sym}: {e}")

    async def _run_manager(self):
        # Fetch snapshots first
        for sym in self.symbols:
            await self._fetch_snapshot(sym)
            await asyncio.sleep(0.1) # rate limit mitigation
            
        streams = [f"{s}@depth" for s in self.symbols]
        # Max 1024 streams. Chunk if needed, but assuming < 100 symbols here.
        stream_str = "/".join(streams)
        uri = f"wss://stream.binance.com:9443/stream?streams={stream_str}"
        
        while self._running:
            try:
                async with websockets.connect(uri) as ws:
                    log.info("🔌 LOB WebSocket connected")
                    while self._running:
                        try:
                            msg_str = await ws.recv()
                            msg = json.loads(msg_str)
                            await self._apply_update(msg)
                        except asyncio.TimeoutError:
                            continue
            except Exception as e:
                if self._running:
                    log.warning(f"LOB WS error: {e}")
                await asyncio.sleep(5)

    async def _apply_update(self, msg):
        data = msg.get("data", {})
        if not data or "s" not in data: return
        sym = data["s"].lower()
        
        # Check sequence
        u = data["u"]
        U = data["U"]
        last_id = self.last_update_id.get(sym, 0)
        
        if u <= last_id:
            return  # Drop stale updates
            
        # v18 Fix: Check for gaps to prevent LOB desync
        if last_id > 0 and U != last_id + 1:
            log.warning(f"⚠️ LOB Desync for {sym}: expected {last_id + 1}, got {U}. Resyncing...")
            self.bids[sym].clear()
            self.asks[sym].clear()
            await self._fetch_snapshot(sym)
            return
            
        # Apply bids
        for p, q in data.get("b", []):
            price, qty = float(p), float(q)
            if qty == 0.0:
                self.bids[sym].pop(price, None)
            else:
                self.bids[sym][price] = qty
                
        # Apply asks
        for p, q in data.get("a", []):
            price, qty = float(p), float(q)
            if qty == 0.0:
                self.asks[sym].pop(price, None)
            else:
                self.asks[sym][price] = qty
                
        self.last_update_id[sym] = u

    def get_liquidity(self, sym, levels=10):
        """Returns top N bids and asks"""
        sym = sym.lower()
        sorted_bids = sorted(self.bids[sym].items(), key=lambda x: x[0], reverse=True)[:levels]
        sorted_asks = sorted(self.asks[sym].items(), key=lambda x: x[0], reverse=False)[:levels]
        return sorted_bids, sorted_asks

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
