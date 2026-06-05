"""BinBot v12.0 — coin_profile.py — Per-Coin Adaptive Learning
Learns each coin's unique behavior over time: volatility, best hours,
win rates per strategy, correlation to BTC. Uses this to dynamically
adjust SL/TP and filter signals for each individual coin.
"""
import json, time, logging, os
from pathlib import Path
from collections import defaultdict

log = logging.getLogger('binbot')

class CoinProfile:
    """Tracks a single coin's historical behavior for adaptive trading."""
    def __init__(self, symbol):
        self.symbol = symbol
        self.trades = []           # Recent trade outcomes [{pnl_pct, strategy, hour, hold_min}]
        self.avg_atr_pct = 1.5     # Running ATR% estimate
        self.atr_readings = []     # Rolling ATR samples
        self.win_rate = 0.50
        self.avg_hold_min = 30
        self.best_hours = []       # UTC hours with best performance
        self.worst_hours = []      # UTC hours with worst performance
        self.btc_corr = 0.0        # Correlation to BTC (-1 to 1)
        self.strategy_wr = {}      # {strategy: win_rate}

    def record_trade(self, pnl_pct, strategy, entry_hour_utc, hold_minutes):
        """Call after each closed trade for this coin."""
        self.trades.append({
            "pnl": pnl_pct, "strat": strategy,
            "hour": entry_hour_utc, "hold": hold_minutes,
            "ts": time.time()
        })
        if len(self.trades) > 200:
            self.trades = self.trades[-200:]
        self._recalculate()

    def record_atr(self, atr_pct):
        """Call periodically with current ATR% for this coin."""
        self.atr_readings.append(atr_pct)
        if len(self.atr_readings) > 100:
            self.atr_readings = self.atr_readings[-100:]
        self.avg_atr_pct = sum(self.atr_readings) / len(self.atr_readings)

    def _recalculate(self):
        if len(self.trades) < 3:
            return
        # Win rate
        wins = sum(1 for t in self.trades if t["pnl"] > 0)
        self.win_rate = wins / len(self.trades)
        # Average hold time
        holds = [t["hold"] for t in self.trades if t["hold"] > 0]
        self.avg_hold_min = sum(holds) / len(holds) if holds else 30
        # Per-strategy win rate
        strat_trades = defaultdict(list)
        for t in self.trades:
            strat_trades[t["strat"]].append(t["pnl"])
        self.strategy_wr = {}
        for s, pnls in strat_trades.items():
            if len(pnls) >= 3:
                self.strategy_wr[s] = sum(1 for p in pnls if p > 0) / len(pnls)
        # Best/worst hours
        hour_pnl = defaultdict(list)
        for t in self.trades:
            hour_pnl[t["hour"]].append(t["pnl"])
        hour_avg = {h: sum(ps)/len(ps) for h, ps in hour_pnl.items() if len(ps) >= 2}
        if hour_avg:
            sorted_hours = sorted(hour_avg.items(), key=lambda x: x[1], reverse=True)
            self.best_hours = [h for h, _ in sorted_hours[:3] if hour_avg[h] > 0]
            self.worst_hours = [h for h, _ in sorted_hours[-3:] if hour_avg[h] < 0]

    def optimal_sl_pct(self):
        """Dynamic SL as a decimal fraction (0.03 = 3%). Min 3% floor.

        v14.6.5 AUDIT FIX (F39): docstring previously said "Min 1.5%" while
        the v14.6.1 fix raised the floor to 0.03 (3%). Naming says `_pct` but
        the return value is a decimal fraction — consumers multiply by price
        directly (price * sl_pct), NOT by price/100.
        """
        return max(0.03, self.avg_atr_pct / 100 * 1.5)  # v14.6.1 FIX: 3% floor

    def optimal_tp_pct(self):
        """Dynamic TP as a decimal fraction (0.01 = 1%). Min 1% floor.

        Returns a decimal fraction, not a percentage — see optimal_sl_pct docstring.
        """
        return max(0.01, self.avg_atr_pct / 100 * 2.5)

    def strategy_boost(self, strategy):
        """Boost/penalize based on how well this strategy works for THIS coin."""
        wr = self.strategy_wr.get(strategy)
        if wr is None or len(self.trades) < 5:
            return 1.0  # Not enough data
        if wr >= 0.65: return 1.10   # +10% — strategy works great here
        if wr >= 0.55: return 1.05   # +5%
        if wr <= 0.30: return 0.75   # -25% — strategy is bad for this coin
        if wr <= 0.40: return 0.85   # -15%
        return 1.0

    def hour_boost(self, current_hour_utc):
        """Small boost/penalty based on historical hour performance."""
        if current_hour_utc in self.best_hours: return 1.05
        if current_hour_utc in self.worst_hours: return 0.90
        return 1.0

    def to_dict(self):
        return {
            "symbol": self.symbol, "trades": self.trades[-50:],
            "atr_readings": self.atr_readings[-20:],
            "avg_atr_pct": self.avg_atr_pct, "btc_corr": self.btc_corr
        }

    @classmethod
    def from_dict(cls, d):
        cp = cls(d["symbol"])
        cp.trades = d.get("trades", [])
        cp.atr_readings = d.get("atr_readings", [])
        cp.avg_atr_pct = d.get("avg_atr_pct", 1.5)
        cp.btc_corr = d.get("btc_corr", 0.0)
        cp._recalculate()
        return cp


class CoinProfileManager:
    """Manages profiles for all traded coins. Persists to disk."""

    def __init__(self, save_path="coin_profiles.json"):
        self._profiles = {}  # {symbol: CoinProfile}
        self._save_path = save_path
        self._last_save = 0
        self._save_interval = 300  # Save every 5 min
        self._load()

    def get(self, symbol):
        """Get or create profile for a coin."""
        if symbol not in self._profiles:
            self._profiles[symbol] = CoinProfile(symbol)
        return self._profiles[symbol]

    def record_trade(self, symbol, pnl_pct, strategy, entry_hour_utc, hold_minutes):
        """Record a trade result for a coin."""
        profile = self.get(symbol)
        profile.record_trade(pnl_pct, strategy, entry_hour_utc, hold_minutes)
        self._auto_save()

    def record_atr(self, symbol, atr_pct):
        """Record current ATR% for a coin."""
        self.get(symbol).record_atr(atr_pct)

    def signal_boost(self, symbol, strategy, current_hour_utc):
        """Combined boost from coin profile (strategy fit + hour)."""
        p = self.get(symbol)
        sb = p.strategy_boost(strategy)
        hb = p.hour_boost(current_hour_utc)
        # Multiplicative here is fine — only 2 small factors
        return round(sb * hb, 3)

    def _auto_save(self):
        if time.time() - self._last_save < self._save_interval:
            return
        self.save()

    def save(self):
        # v13.5.3 audit Bug #29: was non-atomic write — if bot was killed mid-write
        # the file would be half-written → next boot's _load() swallowed the
        # JSONDecodeError and started fresh, losing months of per-coin learning.
        # Now: write to .tmp (with pid suffix to avoid races) then os.replace,
        # which is atomic on POSIX. Same pattern state.py uses (line 76).
        try:
            data = {s: p.to_dict() for s, p in self._profiles.items()}
            tmp = f"{self._save_path}.tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                try: os.fsync(f.fileno())
                except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
            os.replace(tmp, self._save_path)
            self._last_save = time.time()
        except Exception as e:
            log.debug(f"CoinProfile save: {e}")
            # cleanup tmp if rename failed
            try: os.unlink(tmp)
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")

    def _load(self):
        try:
            if Path(self._save_path).exists():
                data = json.loads(Path(self._save_path).read_text())
                for sym, d in data.items():
                    self._profiles[sym] = CoinProfile.from_dict(d)
                log.info(f"📊 Loaded {len(self._profiles)} coin profiles")
        except Exception as e:
            log.debug(f"CoinProfile load: {e}")

    def status(self):
        tracked = len([p for p in self._profiles.values() if len(p.trades) >= 3])
        return f"Coins:{tracked}p"
