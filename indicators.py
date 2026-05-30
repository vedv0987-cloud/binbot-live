"""BinBot v14.2 — indicators.py — TA-Lib accelerated (187x faster)
Core indicators migrated to TA-Lib C library where available.
Custom SMC/Wyckoff/pattern indicators kept as pure Python.
Falls back to pure Python if TA-Lib unavailable.
"""
import math
import numpy as np

# TA-Lib acceleration — transparent fallback to pure Python
try:
    import talib as _tl
    _TALIB = True
except ImportError:
    _TALIB = False

from numba import jit

@jit(nopython=True)
def _fast_vwap_math(h, l, c, v):
    cv = 0.0
    cp = 0.0
    for i in range(len(h)):
        t = (h[i] + l[i] + c[i]) / 3.0
        cp += t * v[i]
        cv += v[i]
    return cp / cv if cv > 0 else c[-1]

def _c(candles):  return np.array([x.c for x in candles], dtype=float)
def _h(candles):  return np.array([x.h for x in candles], dtype=float)
def _l(candles):  return np.array([x.l for x in candles], dtype=float)
def _o(candles):  return np.array([x.o for x in candles], dtype=float)
def _v(candles):  return np.array([x.v for x in candles], dtype=float)


class TA:

    # ─── EMA ──────────────────────────────────────────────────────
    @staticmethod
    def ema(v, p):
        # v18.5 AUDIT NOTE (H5): return LENGTH differs by path — TA-Lib returns full
        # input length (NaN→raw backfilled); the pure-Python branch returns len(v)-p+1.
        # This is intentionally left unchanged: every caller indexes from the END
        # (ema(..)[-1], [-2]) and macd() keeps each path internally consistent, so the
        # difference never affects results. DO NOT assume ema() length == len(v); always
        # use negative indices or len() guards when consuming this.
        if len(v) < p:
            return [sum(v)/len(v)] * len(v) if v else []
        if _TALIB:
            try:
                arr = np.array(v, dtype=float)
                res = _tl.EMA(arr, timeperiod=p)
                return [float(x) if not np.isnan(x) else v[i] for i, x in enumerate(res)]
            except Exception: pass
        e = [sum(v[:p])/p]; m = 2/(p+1)
        for x in v[p:]: e.append(x*m + e[-1]*(1-m))
        return e

    # ─── RSI ──────────────────────────────────────────────────────
    @staticmethod
    def rsi(candles, p=14):
        if len(candles) < p+1: return 50.0
        if _TALIB:
            try:
                res = _tl.RSI(_c(candles), timeperiod=p)
                v = res[~np.isnan(res)]
                return float(v[-1]) if len(v) > 0 else 50.0
            except Exception: pass
        c = [x.c for x in candles]; g = []; l = []
        for i in range(1, len(c)):
            d = c[i]-c[i-1]; g.append(max(d,0)); l.append(max(-d,0))
        ag = sum(g[:p])/p; al = sum(l[:p])/p
        for i in range(p, len(g)): ag = (ag*(p-1)+g[i])/p; al = (al*(p-1)+l[i])/p
        return 100-(100/(1+ag/al)) if al > 0 else 100.0

    @staticmethod
    def rsi_series(candles, p=14):
        if len(candles) < p+10: return []
        if _TALIB:
            try:
                res = _tl.RSI(_c(candles), timeperiod=p)
                return [float(x) for x in res if not np.isnan(x)]
            except Exception: pass
        # v14.6.5 AUDIT FIX (F35): previous pure-Python path was O(N²) — it called
        # TA.rsi(candles[:i+1], p) for each i, re-computing from scratch every time.
        # On 100 candles that's ~80×100 = 8000 ops per call, multiplied by every
        # strategy on every pair every cycle. Now: O(N) incremental Wilder's
        # smoothing — produces identical values to TA.rsi() at each step.
        cc = [c.c for c in candles]
        if len(cc) < p + 1:
            return []
        gains, losses = 0.0, 0.0
        for i in range(1, p + 1):
            d = cc[i] - cc[i-1]
            if d >= 0: gains += d
            else: losses -= d
        avg_g = gains / p
        avg_l = losses / p
        series = []
        # First RSI value lands at index p
        def _rsi(g, l):
            if l == 0: return 100.0
            rs = g / l
            return 100.0 - 100.0 / (1.0 + rs)
        # Skip the first 5 values to match the original `range(p+5, ...)` behavior
        start_idx = p + 5
        for i in range(p + 1, len(cc)):
            d = cc[i] - cc[i-1]
            g = d if d > 0 else 0.0
            l = -d if d < 0 else 0.0
            avg_g = (avg_g * (p - 1) + g) / p
            avg_l = (avg_l * (p - 1) + l) / p
            if i >= start_idx:
                series.append(_rsi(avg_g, avg_l))
        return series

    # ─── MACD ─────────────────────────────────────────────────────
    @staticmethod
    def macd(c, f=12, s=26, sg=9):
        if _TALIB:
            try:
                arr = np.array(c, dtype=float)
                ml_arr, sl_arr, hist_arr = _tl.MACD(arr, f, s, sg)
                ml   = [float(x) if not np.isnan(x) else 0 for x in ml_arr]
                sl   = [float(x) if not np.isnan(x) else 0 for x in sl_arr]
                hist = [float(x) if not np.isnan(x) else 0 for x in hist_arr]
                return ml, sl, hist
            except Exception: pass
        ef = TA.ema(c,f); es = TA.ema(c,s); d = len(ef)-len(es)
        if d > 0: ef = ef[d:]
        ml = [a-b for a,b in zip(ef,es)]; sl = TA.ema(ml,sg); d2 = len(ml)-len(sl)
        ml_trim = ml[d2:] if d2 > 0 else ml
        return ml, sl, [a-b for a,b in zip(ml_trim,sl)]

    # ─── BOLLINGER BANDS ──────────────────────────────────────────
    @staticmethod
    def bb(c, p=20, sd=2.0):
        if not c: return 0,0,0,0
        if len(c) < p: return c[-1],c[-1],c[-1],0
        if _TALIB:
            try:
                arr = np.array(c, dtype=float)
                upper, mid, lower = _tl.BBANDS(arr, timeperiod=p, nbdevup=sd, nbdevdn=sd)
                u, m, l = float(upper[-1]), float(mid[-1]), float(lower[-1])
                if np.isnan(u): raise ValueError
                # v14.6.4 AUDIT FIX (M4): previous formula `2*sd*(u-m)/m*100` produced
                # 2x the correct value when sd=2 (extra `sd` factor). Standard BB
                # bandwidth is (upper - lower) / middle, expressed as percent.
                # Pure-Python path below already used the correct formula — now they match.
                bw = (u - l) / m * 100 if m > 0 else 0
                return u, m, l, bw
            except Exception: pass
        s = sum(c[-p:])/p; v = sum((x-s)**2 for x in c[-p:])/p; d = v**0.5
        return s+sd*d, s, s-sd*d, 2*sd*d/s*100 if s > 0 else 0

    # ─── ATR ──────────────────────────────────────────────────────
    @staticmethod
    def atr(candles, p=14):
        if len(candles) < p+1:
            return candles[-1].c * 0.005 if candles else 0
        if _TALIB:
            try:
                res = _tl.ATR(_h(candles), _l(candles), _c(candles), timeperiod=p)
                v = res[~np.isnan(res)]
                return float(v[-1]) if len(v) > 0 else candles[-1].c * 0.005
            except Exception: pass
        tr = [max(candles[i].h-candles[i].l,
                  abs(candles[i].h-candles[i-1].c),
                  abs(candles[i].l-candles[i-1].c)) for i in range(1, len(candles))]
        a = sum(tr[:p])/p
        for t in tr[p:]: a = (a*(p-1)+t)/p
        return a if a > 0 else candles[-1].c * 0.005

    # ─── ADX ──────────────────────────────────────────────────────
    @staticmethod
    def adx(candles, p=14):
        if len(candles) < p*2: return 25.0
        if _TALIB:
            try:
                res = _tl.ADX(_h(candles), _l(candles), _c(candles), timeperiod=p)
                v = res[~np.isnan(res)]
                return float(v[-1]) if len(v) > 0 else 25.0
            except Exception: pass
        pd, md, tr = [], [], []
        for i in range(1, len(candles)):
            hd = candles[i].h-candles[i-1].h; ld = candles[i-1].l-candles[i].l
            pd.append(hd if hd>ld and hd>0 else 0)
            md.append(ld if ld>hd and ld>0 else 0)
            tr.append(max(candles[i].h-candles[i].l,
                         abs(candles[i].h-candles[i-1].c),
                         abs(candles[i].l-candles[i-1].c)))
        def sm(v, p):
            s = [sum(v[:p])]
            for x in v[p:]: s.append(s[-1]-s[-1]/p+x)
            return s
        st, sp, sn = sm(tr,p), sm(pd,p), sm(md,p)
        n = min(len(st), len(sp), len(sn)); dx = []
        for i in range(n):
            if st[i] == 0: continue
            pi, mi = 100*sp[i]/st[i], 100*sn[i]/st[i]
            if pi+mi > 0: dx.append(100*abs(pi-mi)/(pi+mi))
        return sum(dx[-p:])/min(len(dx),p) if dx else 25.0

    # ─── STOCHASTIC ───────────────────────────────────────────────
    @staticmethod
    def stoch(candles, k=14, d=3):
        """NEW: Stochastic oscillator via TA-Lib."""
        if len(candles) < k+d+1: return 50.0, 50.0
        if _TALIB:
            try:
                sk, sd = _tl.STOCH(_h(candles), _l(candles), _c(candles),
                                   fastk_period=k, slowk_period=d, slowd_period=d)
                sk_v = sk[~np.isnan(sk)]; sd_v = sd[~np.isnan(sd)]
                return (float(sk_v[-1]), float(sd_v[-1])) if len(sk_v) > 0 else (50.0, 50.0)
            except Exception: pass
        return 50.0, 50.0

    # ─── CCI ──────────────────────────────────────────────────────
    @staticmethod
    def cci(candles, p=20):
        """NEW: Commodity Channel Index via TA-Lib."""
        if len(candles) < p: return 0.0
        if _TALIB:
            try:
                res = _tl.CCI(_h(candles), _l(candles), _c(candles), timeperiod=p)
                v = res[~np.isnan(res)]
                return float(v[-1]) if len(v) > 0 else 0.0
            except Exception: pass
        return 0.0

    # ─── MFI ──────────────────────────────────────────────────────
    @staticmethod
    def mfi(candles, p=14):
        """NEW: Money Flow Index via TA-Lib (volume-weighted RSI)."""
        if len(candles) < p+1: return 50.0
        if _TALIB:
            try:
                res = _tl.MFI(_h(candles), _l(candles), _c(candles), _v(candles), timeperiod=p)
                v = res[~np.isnan(res)]
                return float(v[-1]) if len(v) > 0 else 50.0
            except Exception: pass
        return 50.0

    # ─── OBV ──────────────────────────────────────────────────────
    @staticmethod
    def obv(candles):
        """NEW: On-Balance Volume via TA-Lib."""
        if len(candles) < 10: return 0.0
        if _TALIB:
            try:
                res = _tl.OBV(_c(candles), _v(candles))
                return float(res[-1]) if not np.isnan(res[-1]) else 0.0
            except Exception: pass
        return 0.0

    # ─── CANDLESTICK PATTERNS (TA-Lib has 61 patterns) ────────────
    @staticmethod
    def candle_pattern(candles):
        if len(candles) < 5: return "NONE", 0.0
        if _TALIB:
            try:
                o = _o(candles); h = _h(candles)
                l = _l(candles); c = _c(candles)
                # Check key bullish patterns
                patterns = [
                    (_tl.CDLHAMMER(o,h,l,c),        "HAMMER",        0.12),
                    (_tl.CDLMORNINGSTAR(o,h,l,c),    "MORNING_STAR",  0.18),
                    (_tl.CDLPIERCING(o,h,l,c),       "PIERCING",      0.13),
                    (_tl.CDL3WHITESOLDIERS(o,h,l,c), "3_SOLDIERS",    0.20),
                    (_tl.CDLDRAGONFLYDOJI(o,h,l,c),  "DOJI_BULL",     0.08),
                    (_tl.CDLINVERTEDHAMMER(o,h,l,c), "INV_HAMMER",    0.10),
                    (_tl.CDLHARAMI(o,h,l,c),         "HARAMI_BULL",   0.09),
                    # Bearish
                    (_tl.CDLSHOOTINGSTAR(o,h,l,c),   "SHOOTING_STAR", -0.10),
                    (_tl.CDLEVENINGSTAR(o,h,l,c),    "EVENING_STAR",  -0.18),
                    (_tl.CDL3BLACKCROWS(o,h,l,c),    "3_CROWS",       -0.20),
                    (_tl.CDLDOJI(o,h,l,c),           "DOJI",           0.05),
                ]
                # v14.6.5 AUDIT FIX (H-3): engulfing is direction-ambiguous (+100/-100).
                # Previously two CDLENGULFING entries (ENGULF_BULL, ENGULF_BEAR) both
                # called the same function. The first entry matched both bull AND bear
                # results (res[-1] != 0), labeling bearish engulfing as ENGULF_BULL.
                # Fix: check CDLENGULFING once and use sign to pick correct name.
                _eng = _tl.CDLENGULFING(o,h,l,c)
                if _eng[-1] > 0:
                    return "ENGULF_BULL", 0.15
                elif _eng[-1] < 0:
                    return "ENGULF_BEAR", -0.15
                for res, name, score in patterns:
                    if res[-1] != 0:
                        # Positive result = bullish, negative = bearish
                        actual_score = abs(score) if res[-1] > 0 else -abs(score)
                        return name, actual_score
                return "NONE", 0.0
            except Exception: pass
        # Pure Python fallback
        c = candles[-1]; p = candles[-2]; pp = candles[-3]
        body = abs(c.c-c.o); upper = c.h-max(c.c,c.o); lower = min(c.c,c.o)-c.l
        p_body = abs(p.c-p.o); atr = TA.atr(candles)
        if atr == 0: return "NONE", 0.0
        if lower > body*2 and upper < body*0.5 and c.c > c.o: return "HAMMER", 0.12
        if p.c < p.o and c.c > c.o and c.c > p.o and c.o < p.c and body > p_body: return "ENGULF_BULL", 0.15
        if pp.c < pp.o and abs(p.c-p.o) < atr*0.3 and c.c > c.o and c.c > (pp.o+pp.c)/2: return "MORNING_STAR", 0.18
        if body < atr*0.1: return "DOJI", 0.05
        if upper > body*2 and lower < body*0.5 and c.c < c.o: return "SHOOTING_STAR", -0.10
        if p.c > p.o and c.c < c.o and c.o > p.c and c.c < p.o and body > p_body: return "ENGULF_BEAR", -0.15
        return "NONE", 0.0

    # ─── ALL REMAINING PURE PYTHON (no talib equivalent) ──────────
    @staticmethod
    def keltner(candles, p=20, mult=1.5):
        if len(candles) < p: return 0,0,0
        cc = [c.c for c in candles]; mid = sum(cc[-p:])/p
        atr = TA.atr(candles, p)
        return mid+mult*atr, mid, mid-mult*atr

    @staticmethod
    def bb_squeeze(candles, p=20):
        if len(candles) < p+5: return False, 0
        cc = [c.c for c in candles]
        bu,_,bl,bw = TA.bb(cc, p); ku,_,kl = TA.keltner(candles, p)
        is_squeeze = bu < ku and bl > kl
        squeeze_len = 0
        for i in range(len(candles)-1, max(len(candles)-20, p), -1):
            window = candles[:i+1]; cc_w = [c.c for c in window]
            bu_w,_,bl_w,_ = TA.bb(cc_w, p); ku_w,_,kl_w = TA.keltner(window, p)
            if bu_w < ku_w and bl_w > kl_w: squeeze_len += 1
            else: break
        return is_squeeze, squeeze_len

    @staticmethod
    def vwap(candles):
        if not candles: return 0.0
        return float(_fast_vwap_math(_h(candles), _l(candles), _c(candles), _v(candles)))

    @staticmethod
    def vol_ratio(candles, p=20):
        if len(candles) < p+1: return 1.0
        avg = sum(c.v for c in candles[-(p+1):-1])/p
        return candles[-1].v/avg if avg > 0 else 1.0

    @staticmethod
    def trend(candles):
        if len(candles) < 50: return "SIDE", 0.3
        c = [x.c for x in candles]
        e9, e21, e50 = TA.ema(c,9), TA.ema(c,21), TA.ema(c,50)
        if not e9 or not e21 or not e50: return "SIDE", 0.3
        if e9[-1] > e21[-1] > e50[-1]: return "BULL", min((e9[-1]-e50[-1])/c[-1]*100, 1.0)
        if e9[-1] < e21[-1] < e50[-1]: return "BEAR", min((e50[-1]-e9[-1])/c[-1]*100, 1.0)
        return "SIDE", 0.3

    @staticmethod
    def sup_res(candles, lb=20):
        if len(candles) < lb: return candles[-1].l, candles[-1].h
        r = candles[-lb:]
        return min(c.l for c in r), max(c.h for c in r)

    @staticmethod
    def order_block(candles):
        if len(candles) < 10: return None, None, False
        for i in range(len(candles)-2, max(len(candles)-8, 1), -1):
            c, n = candles[i], candles[i+1]
            if c.c < c.o and n.c > n.o and (n.c-n.o) > (c.o-c.c)*1.5:
                return c.l, c.o, True
        return None, None, False

    @staticmethod
    def fvg(candles):
        if len(candles) < 5: return None, None, False
        for i in range(len(candles)-1, max(len(candles)-6, 2), -1):
            if candles[i].l > candles[i-2].h and candles[i-1].c > candles[i-1].o:
                return candles[i-2].h, candles[i].l, True
        return None, None, False

    @staticmethod
    def liq_sweep(candles, lb=20):
        if len(candles) < lb+3: return False, 0.0
        sup = min(c.l for c in candles[-(lb+3):-3]); r = candles[-3:]
        if any(c.l < sup for c in r) and r[-1].c > sup and r[-1].c > r[-1].o:
            return True, sup
        return False, 0.0

    @staticmethod
    def panic(candles, drop=3.0, vol_spike=2.0):
        if len(candles) < 30: return False, 0.0, 0.0
        rh = max(c.h for c in candles[-30:-3]); cur = candles[-1].c
        d = (rh-cur)/rh*100; vr = TA.vol_ratio(candles, 20)
        return (True, d, vr) if d >= drop and vr >= vol_spike else (False, 0.0, 0.0)

    @staticmethod
    def bos(candles, lb=30):
        if len(candles) < lb: return False, "NONE"
        highs = []; lows = []
        for i in range(2, len(candles)-2):
            if candles[i].h > candles[i-1].h and candles[i].h > candles[i+1].h:
                highs.append(candles[i].h)
            if candles[i].l < candles[i-1].l and candles[i].l < candles[i+1].l:
                lows.append(candles[i].l)
        p = candles[-1].c
        if highs and p > highs[-1]: return True, "BULL"
        if lows and p < lows[-1]:  return True, "BEAR"
        return False, "NONE"

    @staticmethod
    def is_ath(candles, lookback=100):
        if len(candles) < lookback: return False
        return candles[-1].c >= max(c.h for c in candles[-lookback:]) * 0.98

    @staticmethod
    def divergence(candles, lookback=30):
        if len(candles) < lookback+10: return "NONE", 0.0
        cc = [c.c for c in candles]; rsi_vals = TA.rsi_series(candles)
        if len(rsi_vals) < lookback: return "NONE", 0.0
        price_lows = []; rsi_at_lows = []
        for i in range(len(cc)-3, max(len(cc)-lookback, 2), -1):
            if cc[i] < cc[i-1] and cc[i] < cc[i+1]:
                price_lows.append((i, cc[i]))
                ri = i-(len(cc)-len(rsi_vals))
                if 0 <= ri < len(rsi_vals): rsi_at_lows.append((i, rsi_vals[ri]))
                if len(price_lows) >= 2: break
        if len(price_lows) < 2 or len(rsi_at_lows) < 2: return "NONE", 0.0
        if price_lows[0][1] < price_lows[1][1] and rsi_at_lows[0][1] > rsi_at_lows[1][1]:
            return "BULL_DIV", min((rsi_at_lows[0][1]-rsi_at_lows[1][1])/10.0, 0.3)
        price_highs = []; rsi_at_highs = []
        for i in range(len(cc)-3, max(len(cc)-lookback, 2), -1):
            if cc[i] > cc[i-1] and cc[i] > cc[i+1]:
                price_highs.append((i, cc[i]))
                ri = i-(len(cc)-len(rsi_vals))
                if 0 <= ri < len(rsi_vals): rsi_at_highs.append((i, rsi_vals[ri]))
                if len(price_highs) >= 2: break
        if len(price_highs) >= 2 and len(rsi_at_highs) >= 2:
            if price_highs[0][1] > price_highs[1][1] and rsi_at_highs[0][1] < rsi_at_highs[1][1]:
                return "BEAR_DIV", min((rsi_at_highs[1][1]-rsi_at_highs[0][1])/10.0, 0.3)
        return "NONE", 0.0

    @staticmethod
    def ob_imbalance(bids, asks, depth=10):
        if not bids or not asks: return 0.0, "NEUTRAL"
        bid_vol = sum(b[1] for b in bids[:depth])
        ask_vol = sum(a[1] for a in asks[:depth])
        total = bid_vol + ask_vol
        if total == 0: return 0.0, "NEUTRAL"
        ratio = (bid_vol-ask_vol)/total
        label = "BUY_WALL" if ratio > 0.3 else ("SELL_WALL" if ratio < -0.3 else "NEUTRAL")
        return round(ratio, 2), label

    @staticmethod
    def correlation(candles_a, candles_b, period=50):
        n = min(len(candles_a), len(candles_b), period)
        if n < 20: return 0.0
        a = [c.c for c in candles_a[-n:]]; b = [c.c for c in candles_b[-n:]]
        ra = [(a[i]-a[i-1])/a[i-1] for i in range(1, len(a))]
        rb = [(b[i]-b[i-1])/b[i-1] for i in range(1, len(b))]
        n = min(len(ra), len(rb))
        if n < 10: return 0.0
        ra, rb = ra[-n:], rb[-n:]; ma, mb = sum(ra)/n, sum(rb)/n
        cov = sum((ra[i]-ma)*(rb[i]-mb) for i in range(n))/n
        sa = (sum((x-ma)**2 for x in ra)/n)**0.5
        sb = (sum((x-mb)**2 for x in rb)/n)**0.5
        return round(cov/(sa*sb), 3) if sa*sb > 0 else 0.0

    @staticmethod
    def wyckoff_phase(candles, lookback=100):
        if len(candles) < lookback: return "UNKNOWN", 0.0
        window = candles[-lookback:]
        cc = [c.c for c in window]; vols = [c.v for c in window]
        half = lookback//2
        vol_first = sum(vols[:half]); vol_second = sum(vols[half:])
        p_range = (max(cc)-min(cc))/max(cc)*100 if max(cc) > 0 else 0
        price_near_low = cc[-1] < (min(cc)+(max(cc)-min(cc))*0.35)
        vol_declining = vol_second < vol_first*0.85
        range_narrow = p_range < 15
        if price_near_low and vol_declining and range_narrow:
            conf = 0.15+(0.10 if p_range < 10 else 0)
            return "ACCUMULATION", conf
        price_near_high = cc[-1] > (min(cc)+(max(cc)-min(cc))*0.65)
        if price_near_high and vol_declining and range_narrow:
            return "DISTRIBUTION", 0.15
        return "RANGING", 0.0

    @staticmethod
    def regime_detect(candles, candles_1h=None):
        if len(candles) < 60: return "UNKNOWN"
        cc = [c.c for c in candles]; adx = TA.adx(candles)
        e200 = TA.ema(cc, min(50, len(cc)-1)); atr = TA.atr(candles)
        atr_pct = atr/cc[-1]*100 if cc[-1] > 0 else 0
        _, _, _, bw = TA.bb(cc); squeeze, sq_len = TA.bb_squeeze(candles)
        if adx > 30 and e200 and cc[-1] > e200[-1]: return "TREND_UP"
        if adx > 30 and e200 and cc[-1] < e200[-1]: return "TREND_DOWN"
        if atr_pct > 3.0: return "VOLATILE"
        if squeeze and sq_len >= 5: return "SQUEEZE"
        if adx < 20: return "RANGE"
        return "CHOPPY"

# v18 Fix: Warm up Numba JIT compiler
try:
    _fast_vwap_math(np.zeros(10), np.zeros(10), np.zeros(10), np.zeros(10))
except Exception:
    pass
