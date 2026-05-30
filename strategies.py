# BinBot v11 — strategies.py
# GridEngine, DCA, Strategies, Backtester
import time, logging
from typing import List, Optional, Dict
from dataclasses import dataclass
from models import Signal, Context, Position, Candle
from indicators import TA
log = logging.getLogger('binbot')

@dataclass
class GridLevel:
    price:float; side:str; filled:bool=False; fill_price:float=0.0; fill_qty:float=0.0; selling:bool=False; buying:bool=False  # v14.6.4 AUDIT FIX: removed duplicate `buying:bool=False`

class GridEngine:
    def __init__(self, cfg, ex):
        self.cfg=cfg; self.ex=ex
        self.grids: Dict[str, List[GridLevel]] = {}
        self.pnl:float=0.0; self.trades:int=0; self.exposure:float=0.0

    def setup(self, sym, price, sup, res, atr=0):
        if sym in self.grids: return
        n=self.cfg.GRID_LEVELS

        # v7: Dynamic ATR-based spacing
        if atr > 0:
            spacing = atr * 0.5  # Half ATR per grid level
        else:
            spacing = (res-sup)/(n+1)
        if spacing<=0: return

        if self.cfg.GRID_GEOMETRIC and price > 0:
            # v7: Geometric grid (equal %)
            pct = spacing / price
            lvls = []
            for i in range(1, n+1):
                bp = price * (1 - pct * i)  # Buy levels below
                sp = price * (1 + pct * i)  # Sell levels above
                if bp > sup * 0.95:
                    lvls.append(GridLevel(price=round(bp,8), side="BUY"))
                if sp < res * 1.05:
                    lvls.append(GridLevel(price=round(sp,8), side="SELL"))
        else:
            lvls=[GridLevel(price=round(sup+spacing*i,8),side="BUY" if sup+spacing*i<price else "SELL") for i in range(1,n+1)]

        self.grids[sym]=lvls
        log.info(f"📊 Grid {sym}: {len(lvls)} lvls | ATR-spaced ${spacing:.2f}")

    def check(self, sym, price, cap_per):
        if sym not in self.grids: return []
        acts=[]
        open_buys = sum(1 for l in self.grids[sym] if l.filled and l.side == "BUY")
        max_open = getattr(self.cfg, 'GRID_MAX_OPEN_LEVELS', 4)
        for lv in self.grids[sym]:
            if lv.filled: continue
            if lv.side=="BUY" and price<=lv.price*1.001 and not getattr(lv, 'buying', False):
                if open_buys >= max_open:
                    continue
                lv.buying = True
                q=cap_per/price
                # v11.2.8 FIX (May 4, 2026): do NOT mark filled here — caller (bot.py)
                # places the order and MUST call mark_filled() only on success. Was:
                # marking filled before placing the order → if exchange rejected (no
                # USDT, MIN_NOTIONAL, API limit), grid level was permanently dead until
                # restart. config.py warns GRID_ENABLED=False until this is fixed.
                acts.append({"a":"BUY","p":price,"q":q,"lv":lv,"cap_per":cap_per})
            elif lv.side=="SELL" and price>=lv.price*0.999:
                # v11.2.16 FIX: race condition — skip buy levels already being sold
                buys=[l for l in self.grids[sym] if l.filled and not l.selling and l.side=="BUY" and l.fill_price<price]
                if buys:
                    b=buys[0]; b.selling=True  # lock immediately to prevent double-sell
                    lv.selling=True  # v11.2.18 FIX: lock sell level to prevent endless sell loop
                    profit=(price-b.fill_price)*b.fill_qty
                    acts.append({"a":"SELL","p":price,"q":b.fill_qty,"profit":profit,"buy_lv":b,"sell_lv":lv})
        return acts

    def mark_buy_filled(self, lv, fill_price, fill_qty, cap_per):
        """v11.2.8: called by bot.py only after ex.buy() returns no error."""
        lv.filled = True
        lv.buying = False
        lv.fill_price = fill_price
        lv.fill_qty = fill_qty
        self.exposure += cap_per
        # v11.2.20 FIX: grid one-way death trap — reset associated sell level so grid cycles
        if hasattr(lv, 'sell_partner') and lv.sell_partner:
            lv.sell_partner.filled = False
            log.info(f"🔄 Grid: sell level reset for rebuy cycle")

    def mark_sell_filled(self, buy_lv, profit, sell_lv=None):
        """v11.2.8: called by bot.py only after ex.sell() returns no error.
        v11.2.18 FIX: accept sell_lv — mark filled to block endless re-trigger."""
        self.exposure -= buy_lv.fill_price * buy_lv.fill_qty
        buy_lv.filled = False
        buy_lv.selling = False
        if sell_lv:
            sell_lv.selling = False
            sell_lv.filled = True  # block re-trigger until price cycles back through buy
            buy_lv.sell_partner = sell_lv  # v11.2.20 FIX: link so rebuy can reset sell level
        self.pnl += profit
        self.trades += 1

    def sync_with_wallet(self, sym, actual_qty):
        if sym not in self.grids: return
        filled_buys = sorted([l for l in self.grids[sym] if l.filled and l.side == 'BUY'], key=lambda x: x.price)
        expected_qty = sum(l.fill_qty for l in filled_buys)
        if abs(expected_qty - actual_qty) > 1e-4:
            log.warning(f"Grid desync {sym}: expected {expected_qty} vs wallet {actual_qty}")
            # Untag filled levels if wallet has less
            if actual_qty < expected_qty:
                for l in reversed(filled_buys):
                    if actual_qty <= 1e-4:
                        l.filled = False
                        l.buying = False
                    else:
                        actual_qty -= l.fill_qty

    def recenter(self, sym, sup, res, price, atr=0):
        if sym not in self.grids: return
        ps=[l.price for l in self.grids[sym]]
        if not ps: return
        mid=(max(ps)+min(ps))/2
        if abs(price-mid)/mid*100>5:
            # v16.0 AUDIT FIX H8: preserve filled buy levels during recenter.
            # Previously deleted ALL levels, orphaning coins already purchased.
            # Now: keep filled buys (real coins held), only rebuild unfilled levels.
            _filled_buys = [l for l in self.grids[sym] if l.filled and l.side == "BUY"]
            del self.grids[sym]
            self.setup(sym, price, sup, res, atr)
            if sym in self.grids and _filled_buys:
                self.grids[sym].extend(_filled_buys)
                log.info(f"🔄 Grid recenter {sym}: preserved {len(_filled_buys)} filled buy levels")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DCA ENGINE (v7: Fear & Greed weighted)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DCA:
    def __init__(self, cfg): self.cfg=cfg

    def check(self, pos, price, fg=50):
        if not self.cfg.DCA_ENABLED or pos.safety_used>=len(self.cfg.DCA_STEPS): return None
        if not hasattr(pos, 'base_size') or pos.base_size <= 0:
            pos.base_size = pos.size  # v11.2.18 FIX: snapshot original entry size
        ref=pos.avg_entry if pos.avg_entry>0 else pos.entry
        drop=(ref-price)/ref*100

        # v7: F&G weighted DCA — buy more aggressively in fear
        step = self.cfg.DCA_STEPS[pos.safety_used]
        if fg < 20: step *= 0.7  # Trigger 30% earlier in extreme fear
        elif fg < 35: step *= 0.85

        if drop>=step:
            mult=self.cfg.DCA_MULT[pos.safety_used] if pos.safety_used<len(self.cfg.DCA_MULT) else 1.0
            # v7: Increase DCA size in fear
            if fg < 20: mult *= 1.3
            new_size = pos.base_size * mult
            max_mult = getattr(self.cfg, 'DCA_MAX_TOTAL_MULT', 4.0)
            if (pos.total_cost + new_size) > (pos.base_size * max_mult):
                return None
            return {"size":pos.base_size*mult,"price":price,"n":pos.safety_used+1}
        return None

    def apply(self, pos, price, qty, size):
        # v11.2.21 FIX: avg_entry used total_cost/total_qty (includes sold portions → corrupted)
        # Now: weighted avg of remaining qty + new fill. qty incremented not reset to total_qty.
        pos.avg_entry = ((pos.qty * pos.avg_entry) + size) / (pos.qty + qty) if (pos.qty + qty) > 0 else pos.avg_entry
        pos.total_cost+=size; pos.total_qty+=qty
        pos.safety_used+=1; pos.qty+=qty  # v11.2.21 FIX: was pos.total_qty — resurrected sold coins
        pos.size=pos.qty*pos.avg_entry
        pos.entry_fee+=price*qty*getattr(self.cfg,'TAKER_FEE',0.001)  # v11.2.22 FIX: DCA fee never tracked
        pos.tp=pos.avg_entry+pos.atr*1.5
        sl_dist = getattr(self.cfg, 'DCA_SL_ATR_MULT', 1.5)
        pos.sl = pos.avg_entry - (pos.atr * sl_dist)
        log.info(f"Moved SL to {pos.sl:.4f} after DCA")
        log.info(f"🔄 DCA#{pos.safety_used} {pos.pair} Avg:${pos.avg_entry:.4f} Size:${pos.size:.2f}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STRATEGIES (v7: divergence, squeeze breakout, stat arb)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Strategies:
    def __init__(self, cfg, ex, intel, micro_price_engine=None):
        self.cfg=cfg; self.ex=ex; self.intel=intel; self.micro_price_engine=micro_price_engine
        self._corr_cache = {}

    async def analyze(self, sym, name, group, tier, ctx:Context, c5, c15, hyperparams=None) -> List[Signal]:
        if self.cfg.SESSION_FILTER and not getattr(ctx, "active", True): return []
        if sym!="BTCUSDT" and not getattr(ctx, "btc_ok", True): return []
        if not c5 or len(c5) < 50: return []
        if not c15: c15 = []

        cc=[x.c for x in c5]; price=cc[-1]
        hp = hyperparams or {"rsi_buy":35,"rsi_sell":65,"bb_sd":2.0}
        # v9.1: Group-based R:R — bigger targets for volatile coins
        # v9.2: Adaptive R:R — lower TP in choppy/range for more wins
        # v18.5 AUDIT FIX (D7): the per-regime "adaptive R:R" was inert. base_rr of 0.5
        # for A/B/C produced sub-1.0 targets that _sig()'s 1.5x TP floor always overrode,
        # so every A/B/C trade ran a fixed 1.5:1 regardless of regime. Opt-in via
        # ADAPTIVE_RR_ENABLED: when on, sensible per-group base R:Rs let the regime
        # modulation below actually move the target. OFF (default) → byte-identical to v18.4.
        if getattr(self.cfg, 'ADAPTIVE_RR_ENABLED', False):
            base_rr = getattr(self.cfg, 'BASE_RR_BY_GROUP',
                              {"A": 2.0, "B": 2.0, "C": 1.8, "D": 5.0}).get(group, 2.0)
        else:
            base_rr = {"A": 0.5, "B": 0.5, "C": 0.5, "D": 5.0}.get(group, 0.5)  # v14.6.2 legacy: floored to 1.5 by _sig
        if ctx.regime in ("CHOPPY", "RANGE"):
            grr = round(base_rr * 0.85, 2)  # v9.8c: was 0.45 (BE WR 62%), now 0.85 (BE WR 42%)
        elif ctx.regime == "SQUEEZE":
            grr = round(base_rr * 0.65, 2)  # SQUEEZE = keep wide, breakouts go far
        elif ctx.regime == "TREND_DOWN":
            grr = round(base_rr * 0.50, 2)  # 50% lower TP in downtrend (quick scalp)
        else:
            grr = base_rr  # Full TP in TREND_UP
        rsi=TA.rsi(c5); atr=TA.atr(c5); adx=TA.adx(c5); vr=TA.vol_ratio(c5)

        sigs=[]  # v16.0 AUDIT FIX C1: moved above MICRO_PRICE block — was NameError crash

        # v16.0: Micro-Price High-Frequency Stat Arb (Spot Long Only)
        if getattr(self, "micro_price_engine", None):
            mp_conf, mp_data = self.micro_price_engine.get_signal(sym, price)
            if mp_conf > 0.0:
                # Add a high conviction standalone signal with tight ATR trail
                sigs.append(self._sig(sym, price, "MICRO_PRICE", mp_conf, group, tier, 
                    price + (atr*2.0), price - (atr*1.2), atr, 
                    f"MicroPrice Anomaly: {mp_data['mp']:.4f} (Dev: {mp_data['dev']*100:.2f}%)"))

        # v9.2: Dynamic SL — ATR-based, tighter in calm markets, wider in volatile
        atr_pct = atr / price if price > 0 else 0.02
        # FIX F3 (audit): was {1.8, 2.5, 2.5} — middle value was wrong.
        # Calm: 1.8 | Normal: 2.1 | Volatile: 2.5
        if atr_pct < 0.015:
            sl_mult = 3.0
        elif atr_pct > 0.035:
            sl_mult = 4.0
        else:
            sl_mult = 3.5  # v14.6: wider SL for choppy market
        ef,es,et=TA.ema(cc,9),TA.ema(cc,21),TA.ema(cc,50)
        ml,sl,hist=TA.macd(cc)
        bu,bm,bl,bw=TA.bb(cc,20,hp.get("bb_sd",2.0))
        vwap=TA.vwap(c5[-40:]); sup,res=TA.sup_res(c5)

        if self.cfg.ATH_PROTECT and TA.is_ath(c5) and ctx.regime not in ("TREND_UP",): return []

        htf_bull=True
        if c15 and len(c15)>25:
            t15,_=TA.trend(c15) if len(c15)>=50 else ("SIDE",0.3)
            htf_bull=t15!="BEAR" and TA.rsi(c15)<70

        ta_score=sum(1 for t in [ctx.daily,ctx.h4,ctx.h1] if t=="BULL")+(1 if htf_bull else 0)
        fm=0.15 if ctx.fg<20 else (0.08 if ctx.fg<35 else (-0.15 if ctx.fg>75 else (-0.05 if ctx.fg>60 else 0)))
        nm = ctx.news_score * self.cfg.NEWS_WEIGHT if self.cfg.NEWS_ENABLED else 0

        # v7: Killzone boost
        kz_boost = 0.08 if ctx.killzone in ("LDN_KZ","NY_KZ") else 0.04
        regime_boost = 0.10 if ctx.regime == "TREND_UP" and ctx.daily == "BULL" else 0

        # v7: Funding rate signal
        fr = self.intel.funding_rate(sym) if sym in ("BTCUSDT","ETHUSDT") else 0
        fr_mod = -0.05 if fr > 0.001 else (0.05 if fr < -0.001 else 0)

        # v7.2: Candlestick pattern boost
        candle_pat, candle_boost = TA.candle_pattern(c5)

        # sigs=[] — v16.0 AUDIT FIX C1: moved to line 174 (before MICRO_PRICE block)

        # ━━━ v7 NEW: RSI DIVERGENCE (74% WR in fear) ━━━
        div_type, div_str = TA.divergence(c5)
        if div_type == "BULL_DIV" and rsi < 40 and ctx.fg < 35:
            conf = 0.62 + div_str + fm + nm + kz_boost + fr_mod  # audit: raised 0.55→0.62
            slp = price - atr * sl_mult; sld = price - slp; tp = price + sld * 2.0  # v9.2: Dynamic SL
            sigs.append(self._sig(sym,price,"RSI_DIVERGENCE",conf,group,tier,tp,slp,atr,
                f"{name} Bull Div RSI={rsi:.0f} F&G={ctx.fg}"))

        # ━━━ v7 NEW: SQUEEZE BREAKOUT ━━━
        if ctx.squeeze and adx < 20 and vr > 1.2:
            # Squeeze about to fire — enter on volume confirmation
            direction = "BULL" if cc[-1] > bm else "BEAR"
            if direction == "BULL" and ta_score >= 2:
                conf = 0.60 + (0.10 if vr > 1.5 else 0) + fm + nm + kz_boost  # audit: raised 0.50→0.60
                # v10.4 FIX: bound slp so a wick below the lower BB band can't invert RR.
                # Was: slp = bl (could be > price → negative sld → inverted TP).
                slp = min(bl, price * 0.99); sld = price - slp; tp = price + sld * 2.5
                sigs.append(self._sig(sym,price,"SQUEEZE_BREAK",conf,group,tier,tp,slp,atr,
                    f"{name} Squeeze fire Vol={vr:.1f}x"))

        # ━━━ SMC ORDER BLOCK + FVG ━━━
        obl,obh,ob=TA.order_block(c5); _,_,fvg_found=TA.fvg(c5)
        if ob and obl and obh and obl<=price<=obh*1.005 and rsi<60:
            conf=0.60+(0.15 if fvg_found else 0)+0.05*ta_score+fm+nm+kz_boost+regime_boost+candle_boost+fr_mod  # audit: raised 0.50→0.60
            slp=obl-atr*1.5; sld=price-slp; tp=price+sld*grr
            tag="+FVG" if fvg_found else ""
            sigs.append(self._sig(sym,price,f"SMC_OB{tag}",conf,group,tier,tp,slp,atr,
                f"{name} OB{tag} RSI={rsi:.0f} TA={ta_score}/4"))

        # ━━━ SMC LIQUIDITY SWEEP ━━━
        swept,slvl=TA.liq_sweep(c5)
        if swept and rsi<55:
            conf=0.63+(0.10 if vr>1.2 else 0)+0.05*min(ta_score,2)+fm+nm+kz_boost+regime_boost+candle_boost  # audit: raised 0.55→0.63
            slp=slvl-atr*1.2; sld=price-slp; tp=price+sld*grr
            sigs.append(self._sig(sym,price,"SMC_SWEEP",conf,group,tier,tp,slp,atr,
                f"{name} Sweep@${slvl:.4f} RSI={rsi:.0f}"))

        # ━━━ TREND CONTINUATION ━━━
        if len(ml)>=2 and len(sl)>=2 and len(ef)>=1 and len(es)>=1 and len(et)>=1:
            if ml[-1]>sl[-1] and ml[-2]<=sl[-2] and ef[-1]>es[-1]>et[-1] and adx>25 and ta_score>=2:
                conf=0.50+(0.10 if adx>35 else 0)+(0.08 if vr>1.2 else 0)+0.05*min(ta_score,3)+fm+nm+kz_boost+regime_boost+candle_boost
                slp=price-atr*(sl_mult+0.5); sld=price-slp; tp=price+sld*max(grr,2.5)  # v9.2: Dynamic SL (QFL wider)
                sigs.append(self._sig(sym,price,"TREND",conf,group,tier,tp,slp,atr,
                    f"{name} MACD↑ EMA↑ ADX={adx:.0f}"))

        # ━━━ BB MEAN REVERSION (v7: uses hyperopt params) ━━━
        rsi_buy = hp.get("rsi_buy", 35)
        if ctx.regime in ("RANGE", "CHOPPY") and price<=bl*1.002 and rsi<40:  # v14.6.4 AUDIT FIX: was rsi<90 (typo, no RSI filter). Now matches backtester _bb() at rsi<40.
            conf=0.56+(0.12 if rsi<30 else 0)+fm+nm+kz_boost+regime_boost+candle_boost  # audit: raised 0.45→0.56
            slp=price-atr*sl_mult; tp=bm; sld=price-slp  # v9.2: Dynamic SL
            rr=(tp-price)/sld if sld>0 else 0
            if rr>=grr:
                sigs.append(self._sig(sym,price,"BB_BOUNCE",conf,group,tier,tp,slp,atr,
                    f"{name} BB_lo RSI={rsi:.0f}"))

        # ━━━ VWAP BOUNCE ━━━
        vd=abs(price-vwap)/vwap*100 if vwap>0 else 99
        # v14.6.5 AUDIT FIX (F15): rsi<80 was effectively no filter — in downtrends
        # price repeatedly retests VWAP from below and the strategy fired buy
        # signals all the way down. Compare BB_BOUNCE which uses rsi<40. Tightening
        # to rsi<55 keeps the "mean reversion" character but filters out high-RSI
        # bounces in a falling tape.
        if vd<0.5 and price<=vwap*1.001 and rsi<55:
            conf=0.56+(0.10 if vr>1.0 else 0)+0.05*min(ta_score,2)+fm+nm+kz_boost+regime_boost+candle_boost  # audit: raised 0.45→0.56
            slp=price-atr*sl_mult; sld=price-slp; tp=price+sld*grr  # v9.2: Dynamic SL
            sigs.append(self._sig(sym,price,"VWAP",conf,group,tier,tp,slp,atr,
                f"{name} VWAP=${vwap:.2f} RSI={rsi:.0f}"))

        # ━━━ QFL PANIC BUY ━━━
        panic,drop,pvr=TA.panic(c5,self.cfg.QFL_DROP,self.cfg.QFL_VOL)
        if panic:
            conf=0.65+(0.15 if drop>5 else 0.05)+fm+nm+regime_boost+candle_boost
            slp=price-atr*(sl_mult+0.8); sld=price-slp; tp=price+sld*max(grr,3.0)  # v9.2: Dynamic SL (Wyckoff wider)
            sigs.append(self._sig(sym,price,"QFL_PANIC",conf,group,tier,tp,slp,atr,
                f"{name} PANIC {drop:.1f}% Vol={pvr:.1f}x"))

        # ━━━ BREAKOUT + BOS ━━━
        bos_f,bos_d=TA.bos(c5)
        if price>res*0.998 and vr>=1.3 and bos_f and bos_d=="BULL" and adx>20 and ta_score>=2:
            conf=0.60+(0.15 if vr>1.8 else 0.05)+0.05*min(ta_score,2)+fm+nm+kz_boost+regime_boost+candle_boost  # audit: raised 0.50→0.60
            slp=res-atr*1.5; sld=price-slp; tp=price+sld*max(grr,3.0)
            sigs.append(self._sig(sym,price,"BREAKOUT",conf,group,tier,tp,slp,atr,
                f"{name} Break${res:.2f}+BOS Vol={vr:.1f}x"))

        # ━━━ v7 NEW: WYCKOFF SPRING ━━━
        wyckoff_phase, wy_conf = TA.wyckoff_phase(c5)
        # v18.7.3 FIX (small-loss bleed): a genuine Wyckoff spring prints a VOLUME-backed
        # reclaim. Without confirmation this fired on every quiet dip → the position just
        # drifted sideways for 4h and time-exited at a small loss + fees. Now, when
        # WYCKOFF_STRICT is on, also require (a) volume confirmation and (b) the higher
        # timeframe NOT bearish, so it only triggers on real accumulation.
        _wyckoff_ok = (wyckoff_phase == "ACCUMULATION" and rsi < 40)
        if _wyckoff_ok and getattr(self.cfg, 'WYCKOFF_STRICT', True):
            _wyckoff_ok = (vr >= getattr(self.cfg, 'WYCKOFF_MIN_VOL_RATIO', 1.1)) and htf_bull
        if _wyckoff_ok:
            conf = 0.60 + wy_conf + fm + nm  # audit: raised 0.50→0.60
            slp = price - atr * sl_mult; sld = price - slp; tp = price + sld * 3.0  # v9.2: Dynamic SL
            sigs.append(self._sig(sym,price,"WYCKOFF_ACC",conf,group,tier,tp,slp,atr,
                f"{name} Wyckoff accumulation RSI={rsi:.0f} Vol={vr:.1f}x"))

        # ━━━ NEW v14.6: EMA CROSS ━━━
        if len(ef)>=2 and len(es)>=2 and ef[-1]>es[-1] and ef[-2]<=es[-2] and adx>20 and ta_score>=1:
            conf=0.55+fm+nm+kz_boost+regime_boost
            slp=price-atr*(sl_mult-0.5); sld=price-slp; tp=price+sld*grr
            sigs.append(self._sig(sym,price,"EMA_CROSS",conf,group,tier,tp,slp,atr, f"{name} EMA 9/21 Cross"))

        # ━━━ NEW v14.6: MACD HISTOGRAM REVERSAL ━━━
        if len(hist)>=3 and hist[-1]>hist[-2] and hist[-2]<hist[-3] and hist[-1]<0 and rsi<50:
            conf=0.58+fm+nm+kz_boost+candle_boost
            slp=price-atr*sl_mult; sld=price-slp; tp=price+sld*grr
            sigs.append(self._sig(sym,price,"MACD_HIST",conf,group,tier,tp,slp,atr, f"{name} MACD Hist Turn"))

        # ━━━ NEW v14.6: KELTNER BOUNCE (lower-band mean reversion) ━━━
        # v14.6.4 AUDIT NOTE: Uses EMA(20) - 2×ATR for lower band. TA.keltner() in
        # indicators.py uses SMA-based KC (Chester Keltner original); this strategy
        # uses Linda Raschke's EMA variant. Both are valid KC implementations.
        # Backtester _keltner() mirrors this exact logic.
        kc_mid = TA.ema(cc,20)[-1] if len(cc)>20 else price
        kc_lower = kc_mid - atr * 2.0
        if price <= kc_lower * 1.002 and ctx.regime in ("RANGE","CHOPPY") and rsi<40:
            conf=0.56+fm+nm+kz_boost+candle_boost
            slp=price-atr*sl_mult; sld=price-slp; tp=price+sld*grr
            sigs.append(self._sig(sym,price,"KELTNER_BOUNCE",conf,group,tier,tp,slp,atr, f"{name} Keltner Bounce"))

        # ━━━ NEW v14.6: SUPERTREND-style trend-flip (EMA50 cross + volume confirmation) ━━━
        # v14.6.4 AUDIT NOTE: This is NOT the classical ATR-based Supertrend indicator.
        # It uses an EMA50 cross-up with a 1.5× volume gate as a trend-flip proxy.
        # The strategy NAME ("SUPERTREND") is preserved for journal/ML continuity in
        # trades_v9.jsonl. Backtester _supertrend() mirrors this exact logic.
        if len(et)>=2 and cc[-1]>et[-1] and cc[-2]<=et[-2] and vr>1.5:
            conf=0.60+fm+nm+kz_boost+regime_boost
            slp=price-atr*sl_mult; sld=price-slp; tp=price+sld*grr
            sigs.append(self._sig(sym,price,"SUPERTREND",conf,group,tier,tp,slp,atr, f"{name} Supertrend/EMA50 Break"))

        return sigs

    def _sig(self,sym,price,strat,conf,grp,tier,tp,sl,atr,reason):
        # v8.4: Minimum 3% SL distance — crypto noise easily wicks 2%
        min_sl = price * 0.97   # v14.6.1 FIX: 3% floor — must match risk.py can_trade()
        if sl > min_sl:
            sl = min_sl
        # Adjust TP to maintain R:R with new SL.
        # v18.5 (D7): floor is configurable (MIN_TP_RR_FLOOR, default 1.5). This floor is
        # load-bearing — it guarantees TP R:R >= rank()'s MIN_RR gate so signals aren't
        # all filtered. With ADAPTIVE_RR_ENABLED, grr can raise the target ABOVE this floor.
        sl_dist = price - sl
        _tp_floor = getattr(self.cfg, 'MIN_TP_RR_FLOOR', 1.5)
        if sl_dist > 0:
            tp = max(tp, price + sl_dist * _tp_floor)
        conf=round(max(0,min(conf,1.0)),2)
        rr=round((tp-price)/(price-sl),1) if price>sl else 0
        gr="A+" if conf>=0.75 else ("A" if conf>=0.60 else ("B" if conf>=0.50 else "C"))
        return Signal(sym,price,strat,conf,gr,reason,grp,tier,tp,sl,rr,atr)

    def rank(self,sigs):
        # v16.0 AUDIT NOTE M7: conf >= 0.65 here is the EFFECTIVE minimum confidence gate.
        # bot.py _cycle line 2507 has a softer 0.50 gate, but signals never reach it
        # because rank() filters first. This is intentional: 0.65 = quality gate,
        # 0.50 = safety net for any future rank() bypass paths.
        v=[s for s in sigs if s.conf>=0.65 and s.rr>=self.cfg.MIN_RR and s.grade in ("A+","A","B","C","D")]
        # v7.2: Deduplicate — keep best signal per pair
        seen = {}
        for s in v:
            if s.pair not in seen or s.conf > seen[s.pair].conf:
                seen[s.pair] = s
        v = list(seen.values())
        return sorted(v,key=lambda s:s.conf+(0.05 if s.tier==1 else 0),reverse=True)

    def check_correlation(self, sym, existing_positions, candle_cache):
        """v7: Check if new position is too correlated with existing."""
        if not existing_positions: return True
        sym_candles = candle_cache.get(sym)
        if not sym_candles: return True
        for pos in existing_positions:
            pos_candles = candle_cache.get(pos.pair)
            if not pos_candles: continue
            corr = TA.correlation(sym_candles, pos_candles)
            if abs(corr) > self.cfg.CORR_THRESHOLD:
                log.info(f"⚠️ Corr block: {sym}↔{pos.pair} = {corr:.2f}")
                return False
        return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RISK MANAGER (v7: Kelly, heat, anti-martingale)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━



class Backtester:
    def __init__(self, exchange, ta, config):
        self.exchange=exchange; self.ta=ta; self.config=config

    def run(self, symbol="BTCUSDT", days=7):
        log.info(f"📊 Backtesting {symbol} {days}d...")
        c5=self.exchange.klines_sync(symbol,"5m",min(days*288,1000))  # v15.3 FIX: sync helper
        if not c5 or len(c5)<200: return {"error":"insufficient_data"}
        time.sleep(0.5)
        c1h=self.exchange.klines_sync(symbol,"1h",min(days*24,500))  # v15.3 FIX: sync helper
        results={"symbol":symbol,"strategies":{}}

        tests=[("SMC_OB",self._ob),("SMC_SWEEP",self._sweep),("TREND",self._trend),
               ("BB_BOUNCE",self._bb),("VWAP",self._vwap),("QFL_PANIC",self._qfl),
               ("BREAKOUT",self._bk),("RSI_DIVERGENCE",self._div),("SQUEEZE_BREAK",self._sq),
               ("EMA_CROSS",self._ema_cross),("MACD_HIST",self._macd_hist),
               ("KELTNER_BOUNCE",self._keltner),("SUPERTREND",self._supertrend)]

        for name,fn in tests:
            try:
                p=fn(c5,c1h); results["strategies"][name]=p
                st="ON" if p["win_rate"]>38 and p["total_pnl_pct"]>-3 else "OFF"
                log.info(f"  {name}: {p['trades']}t WR:{p['win_rate']:.0f}% PnL:{p['total_pnl_pct']:+.2f}% {st}")
            except Exception: results["strategies"][name]={"trades":0,"wins":0,"win_rate":0,"total_pnl_pct":0}

        # v11.2.8 FIX (May 4, 2026): operator precedence. Was:
        #   if isinstance(p,dict) and p.get("win_rate",0)<38 or p.get("total_pnl_pct",0)<-3
        # which parses as `(isinstance and wr<38) or pnl<-3` → if `p` ever isn't a dict
        # (future refactor), the `.get()` on second branch crashes with AttributeError.
        # Now: parens make intent explicit.
        disabled=[n for n,p in results["strategies"].items() if isinstance(p,dict) and (p.get("win_rate",0)<38 or p.get("total_pnl_pct",0)<-3)]
        results["disabled_strategies"]=disabled
        # v14.6.1 FIX: removed disabled=[] that silenced the warning below
        if disabled: log.warning(f"⚠️ Disable: {', '.join(disabled)}")
        total_t=sum(s.get("trades",0) for s in results["strategies"].values() if isinstance(s,dict))
        log.info(f"📊 Backtest done: {total_t} trades")
        return results

    def _sim(self, entry, after, atr, rr=1.5):
        sl=entry-atr; tp=entry+atr*rr
        # B2-4: round-trip fee — 0.2% taker on Binance Spot retail (entry+exit).
        # Was fee-blind; reported WR was biased ~5pp upward, and the disable
        # threshold (WR<38%) at line 366 thus rarely actually disabled losing
        # strategies post-fee.
        RT_FEE_PCT = 0.2
        # v14.6.5 AUDIT FIX (F32): add a spread/slippage assumption. Altcoins on
        # Binance Spot routinely show 0.05–0.30% bid-ask spread (see lob_imbalance
        # measurements). Without modeling this, backtest PnL was overstated by
        # ~0.10–0.60% per round-trip. Using 0.10% as a conservative midpoint.
        RT_SPREAD_PCT = 0.10
        RT_COST_PCT = RT_FEE_PCT + RT_SPREAD_PCT
        for c in after:
            if c.l<=sl: return {"win":False,"pnl_pct":(sl-entry)/entry*100 - RT_COST_PCT}
            if c.h>=tp: return {"win":True,"pnl_pct":(tp-entry)/entry*100 - RT_COST_PCT}
        ep=after[-1].c if after else entry
        net_pct = (ep-entry)/entry*100 - RT_COST_PCT
        return {"win":net_pct>0,"pnl_pct":net_pct}

    def _run_strat(self, candles, check_fn, cooldown=12, rr=1.5):
        trades=wins=0; pnl=0.0; cd=0
        for i in range(50,len(candles)-36):
            if cd>0: cd-=1; continue
            w=candles[max(0,i-60):i+1]
            if check_fn(w,candles[i]):
                atr=self.ta.atr(w); r=self._sim(candles[i].c,candles[i+1:i+36],atr,rr)
                trades+=1; pnl+=r["pnl_pct"]
                if r["win"]: wins+=1
                cd=cooldown
        return {"trades":trades,"wins":wins,"win_rate":round(wins/trades*100,1) if trades>0 else 0,
                "total_pnl_pct":round(pnl,2)}

    def _ob(self,c,c1h): return self._run_strat(c,lambda w,_:TA.order_block(w)[2] and TA.rsi(w)<60)
    def _sweep(self,c,c1h): return self._run_strat(c,lambda w,_:TA.liq_sweep(w)[0] and TA.rsi(w)<55, rr=2.0)
    def _trend(self,c,c1h):
        def check(w,_):
            cc=[x.c for x in w]; ml,sl,_=TA.macd(cc); ef=TA.ema(cc,9); es=TA.ema(cc,21); et=TA.ema(cc,50)
            return len(ml)>=2 and len(sl)>=2 and len(ef)>=1 and len(es)>=1 and len(et)>=1 and ml[-1]>sl[-1] and ml[-2]<=sl[-2] and ef[-1]>es[-1]>et[-1] and TA.adx(w)>25
        return self._run_strat(c,check,rr=2.5)
    def _bb(self,c,c1h):
        def check(w,_):
            cc=[c.c for c in w]; _,_,bl,_=TA.bb(cc)
            return cc[-1]<=bl*1.002 and TA.rsi(w)<40
        return self._run_strat(c,check)
    def _vwap(self,c,c1h):
        def check(w,_):
            vwap=TA.vwap(w[-40:]); vd=abs(w[-1].c-vwap)/vwap*100 if vwap>0 else 99
            return vd<0.5 and w[-1].c<=vwap*1.001 and TA.rsi(w)<55  # v16.0 AUDIT FIX H6: was <45, live uses <55
        return self._run_strat(c,check)
    def _qfl(self,c,c1h): return self._run_strat(c,lambda w,_:TA.panic(w,3.0,2.0)[0],cooldown=24,rr=3.0)
    def _bk(self,c,c1h):
        def check(w,_):
            _,res=TA.sup_res(w); bos_f,bos_d=TA.bos(w)
            return w[-1].c>res*0.998 and TA.vol_ratio(w)>=1.3 and bos_f and bos_d=="BULL"
        return self._run_strat(c,check,rr=3.0)
    def _div(self,c,c1h):
        def check(w,_):
            dt,ds=TA.divergence(w); return dt=="BULL_DIV" and TA.rsi(w)<40
        return self._run_strat(c,check,rr=2.0)
    def _sq(self,c,c1h):
        def check(w,_):
            sq,sl=TA.bb_squeeze(w); return sq and sl>=5 and TA.vol_ratio(w)>1.2
        return self._run_strat(c,check,rr=2.5)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # v14.6.4 AUDIT FIX: 4 missing backtester methods added.
    # Previously the tests list referenced self._ema_cross / self._macd_hist /
    # self._keltner / self._supertrend which DID NOT EXIST. Backtester.run() silently
    # caught AttributeError via bare except, logging 0 trades / 0% WR for all 4 new
    # v14.6 strategies — making backtest output for them completely fake.
    # Each method below mirrors the EXACT condition used by the live analyze() method.
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _ema_cross(self, c, c1h):
        """v14.6.4 AUDIT FIX: backtester for EMA_CROSS strategy.
        Mirrors analyze(): ef[-1]>es[-1] and ef[-2]<=es[-2] and adx>20."""
        def check(w, _):
            cc = [x.c for x in w]
            ef = TA.ema(cc, 9); es = TA.ema(cc, 21)
            if len(ef) < 2 or len(es) < 2: return False
            return ef[-1] > es[-1] and ef[-2] <= es[-2] and TA.adx(w) > 20
        return self._run_strat(c, check, rr=1.5)

    def _macd_hist(self, c, c1h):
        """v14.6.4 AUDIT FIX: backtester for MACD_HIST strategy.
        Mirrors analyze(): hist[-1]>hist[-2] and hist[-2]<hist[-3] and hist[-1]<0 and rsi<50."""
        def check(w, _):
            cc = [x.c for x in w]
            _, _, hist = TA.macd(cc)
            if len(hist) < 3: return False
            return (hist[-1] > hist[-2] and hist[-2] < hist[-3]
                    and hist[-1] < 0 and TA.rsi(w) < 50)
        return self._run_strat(c, check, rr=1.5)

    def _keltner(self, c, c1h):
        """v14.6.4 AUDIT FIX: backtester for KELTNER_BOUNCE strategy.
        Mirrors analyze(): price <= EMA20 - 2*ATR (lower band) and rsi<40.
        Note: backtest can't check ctx.regime (RANGE/CHOPPY); we approximate via
        low-ADX filter so we only count bounces in non-trending regimes."""
        def check(w, _):
            cc = [x.c for x in w]
            if len(cc) <= 20: return False
            kc_mid = TA.ema(cc, 20)[-1]
            atr = TA.atr(w)
            kc_lower = kc_mid - atr * 2.0
            # Low ADX approximates RANGE/CHOPPY regime for backtest purposes.
            return (cc[-1] <= kc_lower * 1.002
                    and TA.rsi(w) < 40
                    and TA.adx(w) < 25)
        return self._run_strat(c, check, rr=1.5)

    def _supertrend(self, c, c1h):
        """v14.6.4 AUDIT FIX: backtester for SUPERTREND strategy (EMA50 cross + volume).
        Mirrors analyze(): cc[-1]>et[-1] and cc[-2]<=et[-2] and vr>1.5."""
        def check(w, _):
            cc = [x.c for x in w]
            et = TA.ema(cc, 50)
            if len(et) < 2 or len(cc) < 2: return False
            return (cc[-1] > et[-1] and cc[-2] <= et[-2]
                    and TA.vol_ratio(w) > 1.5)
        return self._run_strat(c, check, rr=1.5)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN BOT v7
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

