import asyncio
import random
import logging
import time

log = logging.getLogger('binbot')

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
