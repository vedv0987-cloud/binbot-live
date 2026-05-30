import time

class MicroPriceModel:
    """
    v16.0 Institutional Quant Upgrade: Micro-Price Fair Value
    Calculates the true 'fair value' based on L1 order book gravity.
    Runs in O(1) memory and CPU for 1GB RAM constraints.
    """
    def __init__(self, ema_alpha=0.1):
        self.bba_cache = {}  
        self.micro_prices = {}
        self.ema_micro_prices = {}
        self.ema_alpha = ema_alpha

    def update_bba(self, symbol, bid_price, bid_qty, ask_price, ask_qty):
        """Update Best Bid/Ask data from @bookTicker WS stream"""
        if bid_qty + ask_qty <= 0:
            return
            
        self.bba_cache[symbol] = {
            "b": bid_price,
            "B": bid_qty,
            "a": ask_price,
            "A": ask_qty,
            "ts": time.time()
        }
        
        micro_price = (ask_price * bid_qty + bid_price * ask_qty) / (bid_qty + ask_qty)
        self.micro_prices[symbol] = micro_price
        
        if symbol not in self.ema_micro_prices:
            self.ema_micro_prices[symbol] = micro_price
        else:
            self.ema_micro_prices[symbol] = (micro_price * self.ema_alpha) + (self.ema_micro_prices[symbol] * (1 - self.ema_alpha))

    def get_signal(self, symbol, current_trade_price):
        if symbol not in self.micro_prices:
            return 0.0, None
            
        mp = self.micro_prices[symbol]
        ema_mp = self.ema_micro_prices[symbol]
        bba = self.bba_cache[symbol]
        
        if time.time() - bba["ts"] > 30.0:
            return 0.0, None
            
        if current_trade_price <= 0:
            return 0.0, None
            
        deviation_pct = (mp - current_trade_price) / current_trade_price
        momentum_pct = (mp - ema_mp) / ema_mp
        
        if deviation_pct > 0.0015 and momentum_pct > 0.0005:
            conf = min(1.0, deviation_pct / 0.0030)
            return conf, {"mp": mp, "dev": deviation_pct, "imb": bba["B"] / (bba["B"] + bba["A"])}
            
        return 0.0, None
