"""BinBot v15.0 — Multi-Regime Backtest Engine

Wraps strategies.Backtester to split historical data by regime (TREND_UP,
TREND_DOWN, RANGE, CHOPPY, VOLATILE, SQUEEZE) and report per-regime stats.

Identifies which strategies have edge in WHICH market conditions, so the
auto-killer can be regime-aware instead of global-WR based.

Output: per_strategy × per_regime PnL/WR matrix.

Usage:
    from regime_backtest import RegimeAwareBacktester
    rbt = RegimeAwareBacktester(exchange, TA, config)
    matrix = rbt.run("BTCUSDT", days=30)
    rbt.print_matrix(matrix)
"""
from __future__ import annotations
import logging
from collections import defaultdict
from typing import Dict, List

log = logging.getLogger("binbot")


def _classify_regime(window_candles, ta_module) -> str:
    """Re-implements indicators.TA.regime_detect on a window of candles."""
    if len(window_candles) < 60:
        return "UNKNOWN"
    cc = [c.c for c in window_candles]
    try:
        adx = ta_module.adx(window_candles)
        atr = ta_module.atr(window_candles)
        atr_pct = atr / cc[-1] * 100 if cc[-1] > 0 else 0
        e200 = ta_module.ema(cc, min(50, len(cc) - 1))
        squeeze, sq_len = ta_module.bb_squeeze(window_candles)
        if adx > 30 and e200 and cc[-1] > e200[-1]: return "TREND_UP"
        if adx > 30 and e200 and cc[-1] < e200[-1]: return "TREND_DOWN"
        if atr_pct > 3.0: return "VOLATILE"
        if squeeze and sq_len >= 5: return "SQUEEZE"
        if adx < 20: return "RANGE"
        return "CHOPPY"
    except Exception:
        return "UNKNOWN"


class RegimeAwareBacktester:
    """Splits backtest by detected regime at signal time."""

    def __init__(self, exchange, ta_module, cfg):
        self.exchange = exchange
        self.ta = ta_module
        self.cfg = cfg

    def run(self, symbol: str = "BTCUSDT", days: int = 30) -> Dict:
        """Walk historical candles, detect regime per window, run all strategies,
        record outcomes bucketed by regime."""
        c5 = self.exchange.klines_sync(symbol, "5m", min(days * 288, 10000))  # v15.3 FIX: sync helper
        if not c5 or len(c5) < 200:
            return {"error": "insufficient_data"}

        # Lazy import to avoid circular dependency
        from strategies import Backtester
        bt = Backtester(self.exchange, self.ta, self.cfg)

        # Per-strategy per-regime stats
        # stats[strategy][regime] = {"trades": N, "wins": M, "total_pnl": X}
        stats: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: {"trades": 0, "wins": 0, "total_pnl": 0.0})
        )

        # Walk candle window, classify regime, simulate each strategy
        for i in range(60, len(c5) - 36, 5):
            window = c5[max(0, i - 60):i + 1]
            regime = _classify_regime(window, self.ta)
            entry_price = c5[i].c
            atr = self.ta.atr(window) if hasattr(self.ta, "atr") else entry_price * 0.01

            tests = [
                ("SMC_OB",         lambda w: self.ta.order_block(w)[2] and self.ta.rsi(w) < 60),
                ("SMC_SWEEP",      lambda w: self.ta.liq_sweep(w)[0] and self.ta.rsi(w) < 55),
                ("BB_BOUNCE",      lambda w: self._bb_check(w)),
                ("VWAP",           lambda w: self._vwap_check(w)),
                ("QFL_PANIC",      lambda w: self.ta.panic(w, 3.0, 2.0)[0]),
                ("RSI_DIVERGENCE", lambda w: self._div_check(w)),
                ("SQUEEZE_BREAK",  lambda w: self._sq_check(w)),
                # v14.6.5 AUDIT FIX (F30): 6 strategies previously missing from
                # regime-aware backtest. Without these, the auto-regime-killer
                # couldn't suggest per-regime whitelists for any of them.
                ("TREND",          lambda w: self._trend_check(w)),
                ("BREAKOUT",       lambda w: self._breakout_check(w)),
                ("EMA_CROSS",      lambda w: self._ema_cross_check(w)),
                ("MACD_HIST",      lambda w: self._macd_hist_check(w)),
                ("KELTNER_BOUNCE", lambda w: self._keltner_check(w)),
                ("SUPERTREND",     lambda w: self._supertrend_check(w)),
            ]
            for name, check in tests:
                try:
                    if not check(window):
                        continue
                    sim = bt._sim(entry_price, c5[i + 1:i + 36], atr, rr=1.5)
                    stats[name][regime]["trades"] += 1
                    stats[name][regime]["total_pnl"] += sim["pnl_pct"]
                    if sim["win"]:
                        stats[name][regime]["wins"] += 1
                except Exception:
                    pass

        # Compute derived metrics per (strategy, regime)
        matrix = {}
        for strat, regimes in stats.items():
            matrix[strat] = {}
            for regime, d in regimes.items():
                t = d["trades"]
                if t == 0:
                    continue
                matrix[strat][regime] = {
                    "trades": t,
                    "wins": d["wins"],
                    "win_rate": round(d["wins"] / t * 100, 1),
                    "total_pnl_pct": round(d["total_pnl"], 2),
                    "avg_pnl_pct": round(d["total_pnl"] / t, 3),
                }
        return matrix

    def _bb_check(self, w):
        cc = [c.c for c in w]
        _, _, bl, _ = self.ta.bb(cc)
        return cc[-1] <= bl * 1.002 and self.ta.rsi(w) < 40

    def _vwap_check(self, w):
        vwap = self.ta.vwap(w[-40:])
        if vwap <= 0:
            return False
        return abs(w[-1].c - vwap) / vwap < 0.005 and self.ta.rsi(w) < 45

    def _div_check(self, w):
        dt, _ = self.ta.divergence(w)
        return dt == "BULL_DIV" and self.ta.rsi(w) < 40

    def _sq_check(self, w):
        sq, sl = self.ta.bb_squeeze(w)
        return sq and sl >= 5 and self.ta.vol_ratio(w) > 1.2

    # v14.6.5 AUDIT FIX (F30): backtest checks for the 6 strategies that were
    # missing from the regime matrix. Conditions mirror strategies.py exactly.
    def _trend_check(self, w):
        try:
            cc = [c.c for c in w]
            if len(cc) < 50: return False
            ml, sl, hist = self.ta.macd(cc)
            ef = self.ta.ema(cc, 9); es = self.ta.ema(cc, 21); et = self.ta.ema(cc, 50)
            adx = self.ta.adx(w); vr = self.ta.vol_ratio(w)
            return (ml[-1] > sl[-1] and ml[-2] <= sl[-2]
                    and ef[-1] > es[-1] > et[-1] and adx > 25)
        except Exception:
            return False

    def _breakout_check(self, w):
        try:
            cc = [c.c for c in w]
            if len(cc) < 50: return False
            highs = [c.h for c in w[-50:-1]]
            if not highs: return False
            res = max(highs)
            vr = self.ta.vol_ratio(w); adx = self.ta.adx(w)
            bos_f, bos_d = self.ta.bos(w)
            price = cc[-1]
            return (price > res * 0.998 and vr >= 1.3
                    and bos_f and bos_d == "BULL" and adx > 20)
        except Exception:
            return False

    def _ema_cross_check(self, w):
        try:
            cc = [c.c for c in w]
            if len(cc) < 25: return False
            ef = self.ta.ema(cc, 9); es = self.ta.ema(cc, 21)
            adx = self.ta.adx(w)
            return (len(ef) >= 2 and len(es) >= 2
                    and ef[-1] > es[-1] and ef[-2] <= es[-2] and adx > 20)
        except Exception:
            return False

    def _macd_hist_check(self, w):
        try:
            cc = [c.c for c in w]
            if len(cc) < 35: return False
            _, _, hist = self.ta.macd(cc)
            rsi = self.ta.rsi(w)
            return (len(hist) >= 3 and hist[-1] > hist[-2]
                    and hist[-2] < hist[-3] and hist[-1] < 0 and rsi < 50)
        except Exception:
            return False

    def _keltner_check(self, w):
        try:
            cc = [c.c for c in w]
            if len(cc) < 25: return False
            atr = self.ta.atr(w) if hasattr(self.ta, 'atr') else cc[-1] * 0.01
            kc_mid = self.ta.ema(cc, 20)[-1]
            kc_lower = kc_mid - atr * 2.0
            rsi = self.ta.rsi(w)
            price = cc[-1]
            return price <= kc_lower * 1.002 and rsi < 40
        except Exception:
            return False

    def _supertrend_check(self, w):
        try:
            cc = [c.c for c in w]
            if len(cc) < 55: return False
            et = self.ta.ema(cc, 50)
            vr = self.ta.vol_ratio(w)
            return (len(et) >= 2 and cc[-1] > et[-1]
                    and cc[-2] <= et[-2] and vr > 1.5)
        except Exception:
            return False

    def print_matrix(self, matrix: Dict):
        print("\n" + "=" * 90)
        print(f"  REGIME-AWARE BACKTEST — Per-Strategy / Per-Regime")
        print("=" * 90)
        if "error" in matrix:
            print(f"  ERROR: {matrix['error']}")
            return
        regimes = ["TREND_UP", "TREND_DOWN", "RANGE", "CHOPPY", "VOLATILE", "SQUEEZE", "UNKNOWN"]
        print(f"  {'Strategy':<18}", end="")
        for r in regimes:
            print(f"{r:>11}", end="")
        print()
        print("  " + "-" * 88)
        for strat, regime_data in sorted(matrix.items()):
            print(f"  {strat:<18}", end="")
            for r in regimes:
                d = regime_data.get(r)
                if d is None:
                    print(f"{'—':>11}", end="")
                else:
                    cell = f"{d['win_rate']:.0f}%/{d['trades']}"
                    print(f"{cell:>11}", end="")
            print()
        print("=" * 90 + "\n")
        # Recommend which strategies to ENABLE per regime (WR > 50%)
        print("  RECOMMENDED PER-REGIME WHITELIST (WR ≥ 50%, ≥5 trades):")
        per_regime_winners = defaultdict(list)
        for strat, rdata in matrix.items():
            for r, d in rdata.items():
                if d["trades"] >= 5 and d["win_rate"] >= 50:
                    per_regime_winners[r].append((strat, d["win_rate"]))
        for r in regimes:
            winners = sorted(per_regime_winners.get(r, []), key=lambda x: -x[1])
            if winners:
                ws = ", ".join(f"{s}({wr:.0f}%)" for s, wr in winners)
                print(f"    {r:<14} → {ws}")
        print()
