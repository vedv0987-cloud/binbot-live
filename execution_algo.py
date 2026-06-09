import asyncio
import random
import logging
import time

log = logging.getLogger('binbot')


def plan_twap_chunks(size_usd, min_usd, chunk_usd, max_chunks=10):
    """v19.0: decide how many TWAP chunks an order should be sliced into.

    Returns 1 (-> a single immediate market order) for any order below `min_usd`:
    at small notional the market impact is ~zero, so slicing only adds latency
    (a 5-15s sleep PER chunk) and slippage. Above the floor, target ~`chunk_usd`
    per chunk (the caller passes max(TWAP_CHUNK_USD, the v18.9.11 C3 per-pair
    min-notional), so each chunk still clears MIN_NOTIONAL), capped at `max_chunks`.

    Fixes the live pathology where int(size/_min_chunk) sliced a $23 order into
    ~4 chunks -> ~23s latency + 0.295% slip. Pure function — unit-tested in
    test_core.py.
    """
    try:
        size_usd = float(size_usd)
        min_usd = float(min_usd)
        chunk_usd = float(chunk_usd)
    except (TypeError, ValueError):
        return 1
    if size_usd < min_usd or chunk_usd <= 0:
        return 1
    return max(1, min(int(max_chunks), int(size_usd / chunk_usd)))


class ExecutionAlgo:
    """
    v16: Smart Execution Engine (TWAP / Iceberg Slicer)
    Breaks down large orders into micro-orders over time to minimize slippage
    and avoid detection by High-Frequency Traders.
    """
    def __init__(self, exchange):
        self.ex = exchange

    async def twap_buy(self, sym, total_qty, n_chunks=10):
        """
        Executes a Time-Weighted Average Price (TWAP) buy order.
        Yields progress updates.
        """
        chunk_qty = self.ex.rnd(sym, total_qty / n_chunks)
        if chunk_qty <= 0:
            log.warning(f"TWAP chunk qty too small for {sym}, executing market order")
            return await self.ex.buy(sym, total_qty)

        total_filled = 0.0
        total_cost = 0.0
        fills = []

        log.info(f"🧊 Iceberg/TWAP slicing {total_qty} {sym} into {n_chunks} chunks")

        for i in range(n_chunks):
            remaining = total_qty - total_filled
            if remaining <= 0:
                break
                
            this_chunk = min(chunk_qty, remaining)
            this_chunk = self.ex.rnd(sym, this_chunk)
            
            if this_chunk <= 0:
                break

            # Execute chunk
            result = await self.ex.buy(sym, this_chunk)
            
            if "error" in result:
                log.warning(f"TWAP {sym} error on chunk {i+1}: {result['error']}")
                if total_filled > 0:
                    break
                return result

            # Parse fills
            try:
                filled_qty = float(result.get("executedQty", this_chunk))
                _chunk_fills = result.get("fills", [])
                if _chunk_fills:
                    _fq_sum = sum(float(f.get("qty", 0)) for f in _chunk_fills)
                    _fc_sum = sum(float(f.get("qty", 0)) * float(f.get("price", 0)) for f in _chunk_fills)
                    avg_price = (_fc_sum / _fq_sum) if _fq_sum > 0 else float(result.get("price", 0))
                else:
                    avg_price = float(result.get("price", 0))
                
                if avg_price <= 0:
                    avg_price = float(result.get("price", 0))

                total_filled += filled_qty
                total_cost += filled_qty * avg_price
                fills.append({"chunk": i+1, "qty": filled_qty, "price": avg_price})
            except Exception as e:
                log.debug(f"TWAP parsing error: {e}")
                total_filled += this_chunk
                fills.append({"chunk": i+1, "qty": this_chunk, "price": 0})

            # Random jitter to hide flow (5 to 15 seconds)
            if i < n_chunks - 1:
                delay = random.uniform(5, 15)
                await asyncio.sleep(delay)

        vwap = total_cost / total_filled if total_filled > 0 else 0
        log.info(f"🧊 TWAP {sym} complete: {total_filled} filled at VWAP ${vwap:.4f}")
        
        return {
            "orderId": f"TWAP_{int(time.time())}", 
            "executedQty": str(total_filled),
            "status": "FILLED", 
            "twap_vwap": vwap, 
            "twap_chunks": len(fills),
            "fills": [{"price": str(vwap), "qty": str(total_filled)}]
        }
