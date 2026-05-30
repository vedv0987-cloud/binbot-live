# BinBot v11 — ml.py
# All ML models: MTF, LSTM, RL, Transformer, MetaLearner, DXY, Whale,
# MultiEx, Options, CoinGecko, Social, ExchangeFlow, ModelSelector, Dashboard
import time, json, os, logging, math, urllib.request
import requests as req
from datetime import datetime, timezone
from pathlib import Path
try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    # v11.2.10: train_test_split removed — replaced by TimeSeriesSplit (imported in train())
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    class _NpFallback:
        @staticmethod
        def array(x): return list(x) if not isinstance(x,list) else x
        @staticmethod
        def mean(x): return sum(x)/len(x) if x else 0
        @staticmethod
        def std(x):
            if not x: return 0
            m=sum(x)/len(x); return (sum((i-m)**2 for i in x)/len(x))**0.5
        @staticmethod
        def percentile(x,p): s=sorted(x); i=int(len(s)*p/100); return s[min(i,len(s)-1)] if s else 0
        @staticmethod
        def median(x): s=sorted(x); n=len(s); return s[n//2] if s else 0
        @staticmethod
        def sum(x): return sum(x)
        @staticmethod
        def clip(x,a,b): return max(a,min(b,x)) if not hasattr(x,'__iter__') else [max(a,min(b,v)) for v in x]
        @staticmethod
        def zeros(shape): return [[0]*shape[1] for _ in range(shape[0])] if len(shape)==2 else [0]*shape[0]
        @staticmethod
        def where(cond,a,b): return [a_v if c else b_v for c,a_v,b_v in zip(cond,a if hasattr(a,'__iter__') else [a]*len(cond),b if hasattr(b,'__iter__') else [b]*len(cond))]
        @staticmethod
        def diff(x): return [x[i]-x[i-1] for i in range(1,len(x))]
        random = __import__('random')
    np = _NpFallback()
try:
    from lightgbm import LGBMClassifier
    LGBM_AVAILABLE = True
except ImportError:
    LGBM_AVAILABLE = False
# v13.0: XGBoost + CatBoost for 5-model ensemble
try:
    from xgboost import XGBClassifier
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False
try:
    from catboost import CatBoostClassifier
    CATBOOST_AVAILABLE = True
except ImportError:
    CATBOOST_AVAILABLE = False
log = logging.getLogger('binbot')

# v11.2.10: MTFBrain + MultiTimeframeML REMOVED — dead code.
# MTFBrain._extract_features accessed .c on dicts (bot.py passed dicts, not Candle objects).
# Silently fell back to 0.5 on every call. ~100 lines removed.
# The working RF+GB+LGBM ensemble (MLPredictor) is preserved below.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 2: DXY Dollar Correlation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DXYCorrelation:
    def __init__(self):
        self.last_fetch=0; self.fetch_interval=900; self.dxy_current=None; self.dxy_previous=None
        self.dxy_change_pct=0.0; self.trend="NEUTRAL"; self._history=[]
    def update(self):
        if time.time()-self.last_fetch<self.fetch_interval: return True
        try:
            url="https://open.er-api.com/v6/latest/USD"
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            resp=urllib.request.urlopen(req,timeout=10)
            data=json.loads(resp.read().decode())
            eur=data.get("rates",{}).get("EUR",0)
            if eur>0:
                dxy=1/eur*100; now=time.time()
                self.dxy_previous=self.dxy_current; self.dxy_current=dxy
                # v11.1: 24h rolling baseline — compare to oldest reading in window
                self._history.append((now,dxy))
                self._history=[h for h in self._history if now-h[0]<90000]
                ref=self._history[0][1] if len(self._history)>1 else dxy
                self.dxy_change_pct=(dxy-ref)/max(ref,0.001)*100
                if self.dxy_change_pct>0.1: self.trend="RISING"
                elif self.dxy_change_pct<-0.1: self.trend="FALLING"
                else: self.trend="NEUTRAL"
                self.last_fetch=now
                log.info(f"💵 DXY: {self.dxy_current:.1f} ({self.dxy_change_pct:+.2f}%) {self.trend}")
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
        return True
    def get_boost(self):
        if self.trend=="FALLING": return 1.15
        elif self.trend=="RISING": return 0.80
        return 1.00
    def should_block(self): return self.dxy_change_pct>0.5
    def status(self):
        if self.dxy_current is None: return "DXY:?"
        a="↑" if self.trend=="RISING" else "↓" if self.trend=="FALLING" else "→"
        return f"DXY:{a}{self.dxy_change_pct:+.1f}%"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 3: Whale Tracking
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class WhaleOnChain:
    def __init__(self):
        from collections import deque
        self.last_fetch=0; self.fetch_interval=300; self.signal="NEUTRAL"
        self.net_flow=0; self.large_tx=0; self.history=deque(maxlen=12)
    def update(self):
        if time.time()-self.last_fetch<self.fetch_interval: return True
        try:
            url="https://blockchain.info/unconfirmed-transactions?format=json"
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            resp=urllib.request.urlopen(req,timeout=15)
            data=json.loads(resp.read().decode())
            txs=data.get("txs",[])
            lt=0; large_outflow=0; large_inflow=0
            for tx in txs[:50]:
                tv=sum(o.get("value",0) for o in tx.get("out",[]))/1e8
                if tv>10:
                    lt+=1
                    if tv>50: large_outflow+=1
                    else: large_inflow+=1
            self.large_tx=lt
            self.history.append(lt)
            baseline=sum(self.history)/len(self.history) if self.history else lt
            if lt>baseline*1.5 and large_outflow>large_inflow:
                sig="BEARISH"
            elif lt>baseline*1.5 and large_inflow>large_outflow:
                sig="BULLISH"
            else:
                sig="NEUTRAL"
            self.signal=sig
            self.last_fetch=time.time()
            log.info(f"🐋 Whale: {lt} large txs (baseline {baseline:.0f}) | {self.signal}")
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
        return True
    def get_boost(self):
        if self.signal=="BULLISH": return 1.20
        elif self.signal=="BEARISH": return 0.75
        return 1.00
    def should_block(self): return self.net_flow>50
    def status(self): return f"W:{self.large_tx}tx"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 4: Multi-Exchange Order Flow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MultiExchangeFlow:
    def __init__(self):
        self.last_fetch=0; self.fetch_interval=120; self.consensus="NEUTRAL"; self.boost_val=1.0
    def analyze(self,symbol="BTCUSDT"):
        if time.time()-self.last_fetch<self.fetch_interval: return self.boost_val
        # v14.6.3 FIX: run in background thread — prevents 30s freeze if APIs lag
        import threading as _thr
        if getattr(self, '_analyzing', False): return self.boost_val
        self._analyzing = True
        def _bg():
            try: self._analyze_sync(symbol)
            finally: self._analyzing = False
        _thr.Thread(target=_bg, daemon=True).start()
        return self.boost_val

    def _analyze_sync(self,symbol="BTCUSDT"):
        try:
            # Binance
            url=f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=20"
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            resp=urllib.request.urlopen(req,timeout=10)
            bn=json.loads(resp.read().decode())
            bn_bids=sum(float(b[1])*float(b[0]) for b in bn.get("bids",[]))
            bn_asks=sum(float(a[1])*float(a[0]) for a in bn.get("asks",[]))
            bn_total=bn_bids+bn_asks
            bn_imb=(bn_bids-bn_asks)/bn_total if bn_total>0 else 0
        except Exception: bn_imb=0
        try:
            # Bybit
            url=f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}&limit=20"
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            resp=urllib.request.urlopen(req,timeout=10)
            bb=json.loads(resp.read().decode())
            bids=bb.get("result",{}).get("b",[]); asks=bb.get("result",{}).get("a",[])
            bb_bids=sum(float(b[1])*float(b[0]) for b in bids)
            bb_asks=sum(float(a[1])*float(a[0]) for a in asks)
            bb_total=bb_bids+bb_asks
            bb_imb=(bb_bids-bb_asks)/bb_total if bb_total>0 else 0
        except Exception: bb_imb=0
        try:
            # OKX
            okx_sym=symbol.replace("USDT","-USDT")
            url=f"https://www.okx.com/api/v5/market/books?instId={okx_sym}&sz=20"
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            resp=urllib.request.urlopen(req,timeout=10)
            ox=json.loads(resp.read().decode())
            books=ox.get("data",[]); book=books[0] if books else {}
            ob=book.get("bids",[]); oa=book.get("asks",[])
            ox_bids=sum(float(b[1])*float(b[0]) for b in ob)
            ox_asks=sum(float(a[1])*float(a[0]) for a in oa)
            ox_total=ox_bids+ox_asks
            ox_imb=(ox_bids-ox_asks)/ox_total if ox_total>0 else 0
        except Exception: ox_imb=0
        signals=[]
        for imb in [bn_imb,bb_imb,ox_imb]:
            if imb>0.1: signals.append("BULL")
            elif imb<-0.1: signals.append("BEAR")
            else: signals.append("NEUTRAL")
        bulls=signals.count("BULL"); bears=signals.count("BEAR")
        if bulls>=2: self.consensus="BULLISH"; self.boost_val=1.15
        elif bears>=2: self.consensus="BEARISH"; self.boost_val=0.80
        elif bulls>=1 and bears>=1: self.consensus="DIVERGENCE"; self.boost_val=0.90
        else: self.consensus="NEUTRAL"; self.boost_val=1.00
        if bulls==3: self.boost_val=1.25
        if bears==3: self.boost_val=0.65
        self.last_fetch=time.time()
        log.info(f"📊 MultiEx: BN:{signals[0]} BB:{signals[1]} OKX:{signals[2]} → {self.consensus}")
        return self.boost_val
    def get_boost(self,symbol="BTCUSDT"):
        self.analyze(symbol); return self.boost_val
    def should_block(self): return self.consensus=="BEARISH" and self.boost_val<=0.65
    def status(self): return f"MX:{self.consensus[:4]}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 5: Options Sentiment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OptionsSentiment:
    def __init__(self):
        self.last_fetch=0; self.fetch_interval=900; self.put_call_ratio=1.0
        self.max_pain=0; self.total_oi=0; self.sentiment="NEUTRAL"
    def update(self):
        if time.time()-self.last_fetch<self.fetch_interval: return True
        try:
            url="https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option"
            req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"})
            resp=urllib.request.urlopen(req,timeout=15)
            data=json.loads(resp.read().decode())
            sums=data.get("result",[])
            tput=0; tcall=0
            for s in sums:
                nm=s.get("instrument_name",""); oi=s.get("open_interest",0)
                if "-P" in nm: tput+=oi
                elif "-C" in nm: tcall+=oi
            self.total_oi=tput+tcall
            self.put_call_ratio=tput/tcall if tcall>0 else 1.0
            if self.put_call_ratio>1.0: self.sentiment="BEARISH"
            elif self.put_call_ratio<0.7: self.sentiment="BULLISH"
            else: self.sentiment="NEUTRAL"
            self.last_fetch=time.time()
            log.info(f"📈 Options: P/C={self.put_call_ratio:.2f} OI={self.total_oi:.0f} {self.sentiment}")
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
        return True
    def get_boost(self):
        if self.sentiment=="BULLISH": return 1.15
        elif self.sentiment=="BEARISH": return 0.80
        return 1.00
    def should_block(self): return self.put_call_ratio>1.5
    def status(self): return f"Opt:P/C:{self.put_call_ratio:.1f}"




# v11.2.10: LSTM_MODE + _LSTMMLP import REMOVED — LSTMPredictor was dead code.
# It imported non-existent `ta` module in _extract_features, always returned [].



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 8: Transformer NLP Sentiment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TRANSFORMER_OK = False
try:
    from transformers import pipeline as hf_pipeline
    TRANSFORMER_OK = True
except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")

class TransformerNLP:
    """Real NLP that understands context, not just keywords.
    'Bitcoin crash fears ease' = BULLISH (keyword counter says bearish)
    Falls back to keyword scoring if transformers not installed."""
    def __init__(self):
        self.model=None; self.last_fetch=0; self.fetch_interval=900
        self.sentiment_score=0.0; self.label="NEUTRAL"
        if TRANSFORMER_OK:
            try:
                self.model=hf_pipeline("sentiment-analysis",model="distilbert-base-uncased-finetuned-sst-2-english",device=-1)
                log.info("🤖 Transformer NLP loaded (DistilBERT)")
            except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
    def analyze(self, headlines):
        if not headlines: return 0.0
        if self.model:
            try:
                scores=[]
                for h in headlines[:20]:
                    r=self.model(h[:512])[0]
                    s=r['score'] if r['label']=='POSITIVE' else -r['score']
                    scores.append(s)
                self.sentiment_score=np.mean(scores) if scores else 0.0
            except Exception:
                self.sentiment_score=self._keyword_fallback(headlines)
        else:
            self.sentiment_score=self._keyword_fallback(headlines)
        if self.sentiment_score>0.2: self.label="BULLISH"
        elif self.sentiment_score<-0.2: self.label="BEARISH"
        else: self.label="NEUTRAL"
        return self.sentiment_score
    def _keyword_fallback(self,headlines):
        bull=["surge","rally","bullish","pump","soar","jump","gain","rise","up","buy","breakout","moon","record","high","ath"]
        bear=["crash","dump","bearish","plunge","drop","fall","sell","fear","panic","low","collapse","crisis","ban","hack"]
        sc=0; ct=0
        for h in headlines:
            hl=h.lower()
            for w in bull:
                if w in hl: sc+=1; ct+=1
            for w in bear:
                if w in hl: sc-=1; ct+=1
        return sc/max(ct,1)
    def get_boost(self):
        if self.label=="BULLISH": return 1.15
        elif self.label=="BEARISH": return 0.80
        return 1.00
    def should_block(self): return self.sentiment_score<-0.5
    def status(self): return f"NLP:{self.label[:4]}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 9: Dynamic Model Weighting (Meta-Learner)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MetaLearner:
    """v13.0: Dynamically shifts ML model weights based on recent accuracy.
    Now supports 5-model ensemble (RF+GB+LGBM+XGB+CatBoost)."""
    def __init__(self):
        from collections import deque
        self.weights={"rf":0.20,"gb":0.20,"lgbm":0.20,"xgb":0.20,"cat":0.20}
        self.model_scores={"rf":deque(maxlen=100),"gb":deque(maxlen=100),"lgbm":deque(maxlen=100),"xgb":deque(maxlen=100),"cat":deque(maxlen=100)}
        self.last_update=0; self.update_interval=86400  # Daily
    def record_prediction(self, model_name, predicted_up, actual_up):
        correct=1.0 if predicted_up==actual_up else 0.0
        if model_name in self.model_scores:
            self.model_scores[model_name].append(correct)
    def update_weights(self):
        if time.time()-self.last_update<self.update_interval: return
        accs={}
        for m,scores in self.model_scores.items():
            if len(scores)>=10:
                accs[m]=np.mean(scores[-50:])
            else:
                accs[m]=0.5  # Default
        total=sum(accs.values())
        if total>0:
            for m in self.weights:
                self.weights[m]=round(accs.get(m,0.20)/total,3)
        self.last_update=time.time()
        active = {k:v for k,v in self.weights.items() if v > 0.05}
        log.info(f"🧠 Meta-Learner weights: {' '.join(f'{k.upper()}:{v:.0%}' for k,v in active.items())}")
    def get_weights(self): return self.weights
    def status(self):
        top2 = sorted(self.weights.items(), key=lambda x: x[1], reverse=True)[:2]
        return f"W:{'|'.join(f'{k}{v:.0%}' for k,v in top2)}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 10: Monte Carlo Simulation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MonteCarloSim:
    """Runs 1000 random scenarios to stress-test strategy.
    Answers: What is worst month? Best month? Probability of ruin?"""
    def __init__(self, n_sims=1000):
        self.n_sims=n_sims; self.results=None; self.last_run=0
        self.worst_month=0; self.best_month=0; self.ruin_prob=0; self.median_month=0
    def run(self, trade_results, capital=53.0):
        if len(trade_results)<10: return
        pnls=np.array(trade_results)
        mean_pnl=np.mean(pnls); std_pnl=np.std(pnls)
        if std_pnl==0: std_pnl=0.001
        trades_per_month=len(pnls)/max(1,(time.time()-self.last_run)/86400)*30 if self.last_run>0 else 20
        trades_per_month=max(10,min(100,trades_per_month))
        # v14.6.5 AUDIT FIX (F22): crypto PnL is heavily fat-tailed (leptokurtic).
        # The original np.random.normal sampler underestimated ruin probability
        # because it doesn't reproduce the tail mass. With ≥30 actual trades we
        # now bootstrap from the empirical PnL distribution (no parametric
        # assumption). Falls back to normal only when sample is too small to
        # bootstrap meaningfully.
        _use_bootstrap = len(pnls) >= 30
        monthly_returns=[]
        ruin_count=0
        n_per_sim = int(trades_per_month)
        for _ in range(self.n_sims):
            if _use_bootstrap:
                sim_pnls = np.random.choice(pnls, size=n_per_sim, replace=True)
            else:
                sim_pnls = np.random.normal(mean_pnl, std_pnl, n_per_sim)
            month_pnl=np.sum(sim_pnls)
            monthly_returns.append(month_pnl)
            bal=capital+month_pnl
            if bal<capital*0.5: ruin_count+=1
        monthly_returns=np.array(monthly_returns)
        self.worst_month=float(np.percentile(monthly_returns,5))
        self.best_month=float(np.percentile(monthly_returns,95))
        self.median_month=float(np.median(monthly_returns))
        self.ruin_prob=ruin_count/self.n_sims*100
        self.last_run=time.time()
        _method = "bootstrap" if _use_bootstrap else "normal"
        log.info(f"🎲 Monte Carlo ({self.n_sims} sims, {_method}): Worst:{self.worst_month:+.2f} Med:{self.median_month:+.2f} Best:{self.best_month:+.2f} Ruin:{self.ruin_prob:.1f}%")
    def should_reduce_risk(self): return self.ruin_prob>20
    def status(self): return f"MC:R{self.ruin_prob:.0f}%"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.4 MODULE: CoinGecko Trending — boost trending coins
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CoinGeckoTrending:
    """Fetches top 7 trending coins from CoinGecko (free, no API key).
    If bot's signal matches a trending coin → confidence boost.
    Refresh every 10 minutes to avoid rate limits."""
    def __init__(self):
        self._trending = []  # list of coin symbols
        self._ts = 0
        self._cache_sec = 600  # 10 min
    def refresh(self):
        if time.time() - self._ts < self._cache_sec: return
        try:
            url = "https://api.coingecko.com/api/v3/search/trending"
            req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"BinBot/1.0"})
            resp = urllib.request.urlopen(req, timeout=8)
            data = json.loads(resp.read().decode('utf-8'))
            # v14.6.3 FIX: guard against rate-limit error dict response
            if isinstance(data, dict) and data.get("status", {}).get("error_code") == 429:
                log.warning("CoinGeckoTrending: Rate limited (429). Backing off for 1 hour.")
                self._cache_sec = 3600  # 1 hour backoff
                self._ts = time.time()
                return
            if not isinstance(data, dict) or "coins" not in data:
                log.debug(f"CoinGeckoTrending: unexpected response {type(data).__name__}")
                self._ts = time.time()  # throttle retry
                return
            self._cache_sec = 600  # reset to normal
            coins = data.get("coins", [])
            self._trending = [c.get("item",{}).get("symbol","").upper() for c in coins[:7]]
            self._ts = time.time()
            if self._trending:
                log.info(f"🔥 Trending: {', '.join(self._trending[:5])}")
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
    def get_boost(self, symbol):
        """Returns confidence boost if coin is trending."""
        coin = symbol.replace("USDT","")
        if coin in self._trending: return 1.10  # +10% boost for trending
        return 1.0
    def is_trending(self, symbol):
        coin = symbol.replace("USDT","")
        return coin in self._trending


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.4 MODULE: CoinGecko Top Movers — avoid FOMO, catch dips
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CoinGeckoMovers:
    """Fetches top gainers/losers from CoinGecko markets (free).
    Avoids buying coins that just pumped 15%+ (FOMO trap).
    Identifies coins that dropped 8%+ (potential reversal).
    Refresh every 10 minutes."""
    def __init__(self):
        self._gainers = {}   # {symbol: pct_change_24h}
        self._losers = {}
        self._ts = 0
        self._cache_sec = 600
    def refresh(self):
        if time.time() - self._ts < self._cache_sec: return
        try:
            url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&sparkline=false&price_change_percentage=24h"
            req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"BinBot/1.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode('utf-8'))
            # v10.7 FIX: validate response shape — rate-limit responses come back as
            # error dicts (e.g. {"status": {...}}) and would iterate as keys → AttributeError.
            if isinstance(data, dict) and data.get("status", {}).get("error_code") == 429:
                log.warning("CoinGeckoMovers: Rate limited (429). Backing off for 1 hour.")
                self._cache_sec = 3600  # 1 hour backoff
                self._ts = time.time()
                return
            if not isinstance(data, list):
                log.warning(f"CoinGeckoMovers: unexpected response type {type(data).__name__}, clearing stale data")
                self._gainers = {}
                self._losers = {}
                self._ts = time.time()
                return
            self._cache_sec = 600  # reset to normal
            self._gainers = {}
            self._losers = {}
            for coin in data:
                if not isinstance(coin, dict): continue
                sym = coin.get("symbol","").upper()
                pct = coin.get("price_change_percentage_24h", 0) or 0
                if pct >= 10: self._gainers[sym] = pct
                if pct <= -5: self._losers[sym] = pct
            self._ts = time.time()
            if self._gainers:
                top3 = sorted(self._gainers.items(), key=lambda x: x[1], reverse=True)[:3]
                log.info(f"📈 Top gainers: {', '.join(f'{s}+{p:.0f}%' for s,p in top3)}")
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
    def get_boost(self, symbol):
        """Penalize FOMO buys, neutral for dips (bot already has QFL)."""
        coin = symbol.replace("USDT","")
        pct = self._gainers.get(coin, 0)
        if pct >= 20: return 0.70  # -30% confidence — extreme pump, don't chase
        if pct >= 15: return 0.80  # -20% confidence — big pump, risky
        if pct >= 10: return 0.90  # -10% confidence — moderate pump
        # Losers get slight boost (reversal play)
        loss_pct = self._losers.get(coin, 0)
        if loss_pct <= -10: return 1.08  # +8% for deep dip reversal
        if loss_pct <= -5: return 1.04   # +4% for moderate dip
        return 1.0
    def is_pumped(self, symbol):
        coin = symbol.replace("USDT","")
        return self._gainers.get(coin, 0) >= 15
    def should_block(self, symbol):
        """Block if both the coin AND BTC are in distribution."""
        coin = symbol.replace("USDT","")
        return self._gainers.get(coin, 0) >= 25


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.4 MODULE: Social Sentiment — CryptoCompare social stats
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SocialSentiment:
    """Monitors social media activity spikes using CryptoCompare free API.
    High social volume = potential pump incoming (or dump if negative).
    Refresh every 15 minutes. No API key required for basic endpoints."""
    def __init__(self):
        self._scores = {}  # {coin: social_score}
        self._ts = 0
        self._cache_sec = 900  # 15 min
        self._avg_score = 0
    def refresh(self, coins=None):
        if time.time() - self._ts < self._cache_sec: return
        if not coins: coins = ["BTC","ETH","SOL","BNB","XRP","DOGE","ADA","ARB","OP","SUI"]
        try:
            for coin in coins[:10]:  # Limit to 10 to avoid rate limits
                url = f"https://min-api.cryptocompare.com/data/social/coin/latest?coinId={self._get_coin_id(coin)}"
                req = urllib.request.Request(url, headers={"User-Agent":"BinBot/1.0"})
                resp = urllib.request.urlopen(req, timeout=5)
                data = json.loads(resp.read().decode('utf-8'))
                # v10.7 FIX: CryptoCompare returns {"Data": None} for coins without
                # social tracking. data.get("Data", {}) only defaults on MISSING key,
                # not None VALUE. Coerce None→{} to prevent AttributeError on .get() chain.
                social = data.get("Data") or {}
                # Aggregate Twitter + Reddit activity
                twitter = social.get("Twitter",{}) or {}
                reddit = social.get("Reddit",{}) or {}
                tw_followers = twitter.get("followers",0)
                tw_posts = twitter.get("statuses",0)
                rd_subscribers = reddit.get("subscribers",0)
                rd_active = reddit.get("active_users",0)
                score = (tw_posts * 0.001) + (rd_active * 0.1)
                self._scores[coin] = score
                time.sleep(0.3)  # Rate limit: ~3 calls/sec
            self._ts = time.time()
            if self._scores:
                self._avg_score = sum(self._scores.values()) / len(self._scores) if self._scores else 0
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
    def _get_coin_id(self, coin):
        """CryptoCompare coin IDs for top coins."""
        ids = {"BTC":"1182","ETH":"7605","SOL":"934443","BNB":"204788",
               "DOGE":"4432","AVAX":"934156","LINK":"236131","ZEC":"24854","ONDO":"958188",
               "SUI":"953119","APT":"951233","NEAR":"505608","SEI":"953255","INJ":"937043",
               "HYPE":"960125","ENA":"957843",
               "FET":"831075","RENDER":"965650","TAO":"954078","PEPE":"953245",
               "JTO":"955891","WLD":"954120","PENDLE":"953721","JUP":"955432","BERA":"959875"}
        return ids.get(coin, "1182")
    def get_boost(self, symbol):
        """Boost if social activity is above average for this coin."""
        coin = symbol.replace("USDT","")
        score = self._scores.get(coin, 0)
        if self._avg_score <= 0: return 1.0
        ratio = score / self._avg_score if self._avg_score > 0 else 1.0
        if ratio >= 2.0: return 1.12  # 2x average social = +12%
        if ratio >= 1.5: return 1.06  # 1.5x = +6%
        if ratio <= 0.3: return 0.92  # Very low social = -8%
        return 1.0
    def should_block(self, symbol):
        return False  # Social alone shouldn't block


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.4 MODULE: Exchange Flow Estimator — Glassnode-style
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ExchangeFlowEstimator:
    """Estimates exchange inflow/outflow using Binance volume patterns.
    No external API needed — uses Binance's own 24h ticker data.
    High buy volume ratio = bullish (accumulation).
    High sell volume ratio = bearish (distribution).
    Refresh every 5 minutes."""
    def __init__(self):
        self._flows = {}  # {symbol: {"buy_ratio": float, "signal": str}}
        self._btc_flow = "NEUTRAL"
        self._ts = 0
        self._cache_sec = 300  # 5 min
    def refresh(self, tickers_24h=None):
        """Analyze volume patterns from Binance 24h tickers.
        takers_buy_volume / total_volume > 55% = accumulation (bullish)
        takers_buy_volume / total_volume < 45% = distribution (bearish)"""
        if time.time() - self._ts < self._cache_sec: return
        if not tickers_24h: return
        try:
            for t in tickers_24h:
                sym = t.get("symbol","")
                if not sym.endswith("USDT"): continue
                quote_vol = float(t.get("quoteVolume",0) or 0)
                if quote_vol <= 0: continue
                # Binance spot 24h ticker field names (python-binance)
                taker_buy_quote = float(t.get("takerBuyQuoteAssetVolume", 0) or
                                        t.get("takerBuyQuoteVol", 0) or 0)
                # If taker data unavailable (spot doesn't always provide it), use price direction as proxy
                if taker_buy_quote <= 0:
                    # Fallback: estimate from price change direction
                    pct_change = float(t.get("priceChangePercent", 0) or 0)
                    if pct_change > 2.0: signal = "ACCUMULATION"
                    elif pct_change < -2.0: signal = "DISTRIBUTION"
                    else: signal = "NEUTRAL"
                else:
                    buy_ratio = taker_buy_quote / quote_vol
                    if buy_ratio > 0.55: signal = "ACCUMULATION"
                    elif buy_ratio < 0.45: signal = "DISTRIBUTION"
                    else: signal = "NEUTRAL"
                self._flows[sym] = {"buy_ratio": taker_buy_quote / quote_vol if quote_vol > 0 and taker_buy_quote > 0 else 0.5, "signal": signal}
                if sym == "BTCUSDT": self._btc_flow = signal
            self._ts = time.time()
            acc = sum(1 for f in self._flows.values() if f["signal"]=="ACCUMULATION")
            dist = sum(1 for f in self._flows.values() if f["signal"]=="DISTRIBUTION")
            if acc > 0 or dist > 0:
                log.info(f"💎 Exchange Flow: {acc} accumulating, {dist} distributing | BTC: {self._btc_flow}")
            else:
                log.info(f"💎 Exchange Flow: NEUTRAL ({len(self._flows)} pairs) | BTC: {self._btc_flow}")
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
    def get_boost(self, symbol):
        """Boost/penalize based on exchange flow."""
        flow = self._flows.get(symbol, {})
        signal = flow.get("signal", "NEUTRAL")
        if signal == "ACCUMULATION": return 1.08   # +8% — smart money buying
        if signal == "DISTRIBUTION": return 0.88   # -12% — smart money selling
        return 1.0
    def should_block(self, symbol):
        """Block if both the coin AND BTC are in distribution."""
        flow = self._flows.get(symbol, {})
        return flow.get("signal") == "DISTRIBUTION" and self._btc_flow == "DISTRIBUTION"
    def btc_signal(self): return self._btc_flow

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v11.2.10 MODULE: Binance Long/Short Ratio (FREE)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LongShortRatio:
    """Contrarian indicator: when 67%+ of traders are long, market often dumps.
    Uses Binance Futures globalLongShortAccountRatio (free, no API key).
    Response: [{"longShortRatio":"0.78","longAccount":"0.44","shortAccount":"0.56",...}]
    Refreshes every 5 minutes."""
    def __init__(self):
        from collections import deque
        self._ratio = 1.0  # 1.0 = balanced
        self._long_pct = 0.50
        self._short_pct = 0.50
        self._signal = "NEUTRAL"
        self._ts = 0
        self._cache_sec = 300  # 5 min
        self._history = deque(maxlen=24)  # rolling history for trend detection

    def update(self):
        if time.time() - self._ts < self._cache_sec: return True
        try:
            url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=1"
            r = urllib.request.Request(url, headers={"User-Agent": "BinBot/11"})
            resp = urllib.request.urlopen(r, timeout=8)
            data = json.loads(resp.read().decode())
            if data and isinstance(data, list):
                entry = data[0]
                self._ratio = float(entry.get("longShortRatio", 1.0))
                self._long_pct = float(entry.get("longAccount", 0.5))
                self._short_pct = float(entry.get("shortAccount", 0.5))

                # Track history for trend
                self._history.append(self._ratio)

                # Contrarian logic
                if self._long_pct >= 0.67:      # 67%+ long → crowd overleveraged long
                    self._signal = "CROWD_LONG"
                elif self._short_pct >= 0.67:   # 67%+ short → crowd overleveraged short
                    self._signal = "CROWD_SHORT"
                elif self._long_pct >= 0.58:    # 58%+ long → mild long bias
                    self._signal = "LEAN_LONG"
                elif self._short_pct >= 0.58:   # 58%+ short → mild short bias
                    self._signal = "LEAN_SHORT"
                else:
                    self._signal = "BALANCED"

                self._ts = time.time()
                log.info(f"📊 L/S Ratio: {self._ratio:.2f} (L:{self._long_pct:.0%}/S:{self._short_pct:.0%}) → {self._signal}")
        except Exception as e: log.debug(f"LongShort update: {e}")
        return True

    def get_boost(self):
        """Contrarian: crowd wrong → trade against them."""
        if self._signal == "CROWD_LONG":   return 0.70  # -30% — don't buy with overleveraged longs
        if self._signal == "CROWD_SHORT":  return 1.25  # +25% — shorts will get squeezed
        if self._signal == "LEAN_LONG":    return 0.90  # -10% — mild caution
        if self._signal == "LEAN_SHORT":   return 1.10  # +10% — mild bullish
        return 1.0

    def should_block(self):
        """Block buys when extreme crowd-long (>70%)."""
        return self._long_pct >= 0.70

    def status(self):
        return f"LS:{self._ratio:.2f}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v11.2.10 MODULE: Binance Open Interest Tracker (FREE)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OpenInterestTracker:
    """Tracks Open Interest changes to validate trends.
    OI up + Price up = real trend (confidence boost).
    OI up + Price down = shorts piling in (squeeze risk).
    OI down = positions closing (trend exhaustion).
    Uses Binance Futures API (free, no key).
    Response: {"symbol":"BTCUSDT","openInterest":"100987.416","time":...}
    Refreshes every 10 minutes."""
    def __init__(self):
        self._current_oi = 0
        self._prev_oi = 0
        self._oi_change_pct = 0.0
        self._signal = "NEUTRAL"
        self._ts = 0
        self._cache_sec = 600  # 10 min
        self._price_at_prev = 0
        self._price_at_current = 0

    def update(self, btc_price=0):
        if time.time() - self._ts < self._cache_sec: return True
        try:
            url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
            r = urllib.request.Request(url, headers={"User-Agent": "BinBot/11"})
            resp = urllib.request.urlopen(r, timeout=8)
            data = json.loads(resp.read().decode())
            oi = float(data.get("openInterest", 0))

            if oi > 0:
                self._prev_oi = self._current_oi
                self._current_oi = oi
                self._price_at_prev = self._price_at_current
                self._price_at_current = btc_price

                if self._prev_oi > 0:
                    self._oi_change_pct = (self._current_oi - self._prev_oi) / self._prev_oi * 100
                    price_change = 0
                    if self._price_at_prev > 0 and btc_price > 0:
                        price_change = (btc_price - self._price_at_prev) / self._price_at_prev * 100

                    # OI + Price divergence analysis
                    oi_up = self._oi_change_pct > 1.0
                    oi_down = self._oi_change_pct < -1.0
                    price_up = price_change > 0.5
                    price_down = price_change < -0.5

                    if oi_up and price_up:
                        self._signal = "STRONG_TREND"      # Real buying pressure
                    elif oi_up and price_down:
                        self._signal = "SHORT_BUILDUP"      # Shorts piling in — squeeze risk
                    elif oi_down and price_down:
                        self._signal = "CAPITULATION"       # Positions closing — potential bottom
                    elif oi_down and price_up:
                        self._signal = "SHORT_SQUEEZE"      # Shorts closing — could continue up
                    else:
                        self._signal = "NEUTRAL"

                self._ts = time.time()
                log.info(f"📈 OI: {self._current_oi:.0f} ({self._oi_change_pct:+.1f}%) → {self._signal}")
        except Exception as e: log.debug(f"OpenInterest update: {e}")
        return True

    def get_boost(self):
        if self._signal == "STRONG_TREND":   return 1.15   # +15% — confirmed trend
        if self._signal == "SHORT_SQUEEZE":  return 1.10   # +10% — shorts covering
        if self._signal == "CAPITULATION":   return 1.08   # +8% — potential reversal
        if self._signal == "SHORT_BUILDUP":  return 0.85   # -15% — caution, squeeze could go either way
        return 1.0

    def should_block(self):
        """Block when OI is dropping fast (>5%) — market is deleveraging."""
        return self._oi_change_pct < -5.0

    def status(self):
        arrow = "↑" if self._oi_change_pct > 0 else "↓" if self._oi_change_pct < 0 else "→"
        return f"OI:{arrow}{self._oi_change_pct:+.1f}%"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v11.2.10 MODULE: Bitcoin Hash Rate Monitor (FREE)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HashRateMonitor:
    """Monitors Bitcoin network hash rate as a macro health indicator.
    Hash rate rising = miners confident = bullish.
    Hash rate dropping sharply = miners capitulating = bearish.
    Uses blockchain.info (free, no key).
    Response: plain integer (hashes/sec), e.g. 915345495087
    Refreshes every 30 minutes (slow-moving indicator)."""
    def __init__(self):
        self._current = 0
        self._previous = 0
        self._change_pct = 0.0
        self._signal = "NEUTRAL"
        self._ts = 0
        self._cache_sec = 1800  # 30 min — hash rate changes slowly
        self._history = []

    def update(self):
        if time.time() - self._ts < self._cache_sec: return True
        try:
            url = "https://blockchain.info/q/hashrate"
            r = urllib.request.Request(url, headers={"User-Agent": "BinBot/11"})
            resp = urllib.request.urlopen(r, timeout=10)
            raw = resp.read().decode().strip()
            # Blockchain.info doesn't return JSON for this endpoint, it returns a plain text float.
            # But if rate limited it might return HTML or JSON.
            if "error_code" in raw or "<html" in raw.lower() or "limit" in raw.lower():
                log.warning(f"HashRateMonitor: Rate limited or unexpected response. Backing off for 1 hour.")
                self._cache_sec = 3600
                self._ts = time.time()
                return
            hr = float(raw)
            self._cache_sec = 1800 # reset to normal

            if hr > 0:
                self._previous = self._current
                self._current = hr
                self._history.append(hr)
                if len(self._history) > 48: self._history = self._history[-48:]  # 24h at 30min

                if self._previous > 0:
                    self._change_pct = (self._current - self._previous) / self._previous * 100

                # Compare to 24h rolling average
                if len(self._history) >= 6:
                    avg = sum(self._history) / len(self._history)
                    ratio = self._current / avg if avg > 0 else 1.0

                    if ratio < 0.90:        # 10%+ below average
                        self._signal = "MINER_STRESS"
                    elif ratio < 0.95:      # 5% below average
                        self._signal = "DECLINING"
                    elif ratio > 1.05:      # 5%+ above average (ATH territory)
                        self._signal = "ATH_HASHRATE"
                    else:
                        self._signal = "HEALTHY"
                else:
                    self._signal = "HEALTHY"

                self._ts = time.time()
                hr_eh = self._current / 1e18  # Convert to EH/s for readability
                log.info(f"⛏ Hash Rate: {hr_eh:.0f} EH/s ({self._change_pct:+.1f}%) → {self._signal}")
        except Exception as e: log.debug(f"HashRate update: {e}")
        return True

    def get_boost(self):
        if self._signal == "ATH_HASHRATE":  return 1.05   # +5% — network strongest ever
        if self._signal == "MINER_STRESS":  return 0.85   # -15% — miners selling
        if self._signal == "DECLINING":     return 0.93   # -7% — mild concern
        return 1.0

    def should_block(self):
        """Only block on extreme miner capitulation (>15% drop)."""
        return self._change_pct < -15.0

    def status(self):
        hr_eh = self._current / 1e18 if self._current > 0 else 0
        return f"HR:{hr_eh:.0f}EH"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 11: Performance-Based Model Replacement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ModelSelector:
    """Tracks each model prediction accuracy over rolling window.
    Drops consistently underperforming models.
    Promotes consistently outperforming models."""
    def __init__(self, window=50):
        self.window=window
        # v11.2.10: removed lstm/mtf (dead code, see ml.py)
        self.predictions={"rf":[],"gb":[],"lgbm":[],"xgb":[],"cat":[]}
        self.active={"rf":True,"gb":True,"lgbm":True,"xgb":True,"cat":True}
        self.last_eval=0; self.eval_interval=21600  # 6 hours
    def record(self, model, predicted_up, actual_up):
        if model in self.predictions:
            self.predictions[model].append(1.0 if predicted_up==actual_up else 0.0)
            if len(self.predictions[model])>self.window*2:
                self.predictions[model]=self.predictions[model][-self.window*2:]
    def evaluate(self):
        if time.time()-self.last_eval<self.eval_interval: return
        for model in self.active:
            preds=self.predictions.get(model,[])
            if len(preds)<20: continue
            recent_acc=np.mean(preds[-self.window:])
            if recent_acc<0.40:
                self.active[model]=False
                log.warning(f"🔻 Model {model} disabled: {recent_acc:.1%} accuracy")
            elif recent_acc>0.55 and not self.active[model]:
                self.active[model]=True
                log.info(f"🔺 Model {model} re-enabled: {recent_acc:.1%} accuracy")
        self.last_eval=time.time()
        active_list=[m for m,a in self.active.items() if a]
        log.info(f"🧠 Active models: {active_list}")
    def is_active(self, model): return self.active.get(model,True)
    def status(self):
        active=sum(1 for a in self.active.values() if a)
        return f"Models:{active}/5"  # v13.0: rf+gb+lgbm+xgb+cat

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 12: Web Dashboard Data Exporter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DashboardExporter:
    """Writes bot state to JSON file every cycle for web dashboard to read.
    Dashboard (separate React app) reads this file and displays charts."""
    def __init__(self, export_path="dashboard_data.json"):
        self.path=export_path; self.last_export=0; self.export_interval=30
    def export(self, positions, pnl, wins, losses, regime, fg, heat, daily_pnl, trades_today, ml_acc, dxy_status="", options_status="", rl_status="", mc_status=""):
        if time.time()-self.last_export<self.export_interval: return
        try:
            data={
                "ts":datetime.now(timezone.utc).isoformat(),
                "positions":[{"pair":p.pair,"entry":p.avg_entry,"qty":p.qty,"strategy":p.strategy,"grade":p.grade} for p in positions],
                "pnl":round(pnl,4),"daily_pnl":round(daily_pnl,4),
                "wins":wins,"losses":losses,
                "wr":round(wins/(wins+losses)*100,1) if (wins+losses)>0 else 50,
                "regime":regime,"fg":fg,"heat":round(heat,2),
                "trades_today":trades_today,"ml_accuracy":round(ml_acc,3),
                "dxy":dxy_status,"options":options_status,
                "rl":rl_status,"monte_carlo":mc_status,
                "active_positions":len(positions),
            }
            Path(self.path).write_text(json.dumps(data,indent=2))
            self.last_export=time.time()
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v12.0 MODULE: Outlier Detection (Dissimilarity Index)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OutlierDetector:
    """Dissimilarity Index — measures how 'different' current features are
    from training data. Prevents ML from making confident predictions in
    market conditions it's never seen before (black swans, post-FOMC spikes).
    Inspired by FreqAI's DI outlier removal system."""
    def __init__(self, threshold=3.0):
        self._mean = None
        self._std = None
        self.threshold = threshold
        self.last_score = 0.0

    def fit(self, X_train):
        """Call after training — learns what 'normal' features look like."""
        if not ML_AVAILABLE: return
        self._mean = np.mean(X_train, axis=0)
        self._std = np.std(X_train, axis=0) + 1e-8

    def score(self, features):
        """Returns DI score. Higher = more dissimilar from training data."""
        if self._mean is None: return 0.0
        try:
            z = np.abs((np.array(features) - self._mean) / self._std)
            self.last_score = float(np.mean(z))
            return self.last_score
        except Exception: return 0.0

    def is_outlier(self, features):
        """True if current market is too different from training data."""
        return self.score(features) > self.threshold

    def status(self):
        return f"DI:{self.last_score:.1f}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v15.1: LSTM Deep Learning Model (Optional — requires PyTorch)
# Captures sequential patterns that tree-based models miss
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# v18.8 MEMORY: torch is only used by the (disabled) LSTM. Importing it loads ~250MB at
# startup — exactly the RAM the 5-model ensemble (xgboost+catboost) needs on a ~1GB VM.
# So we DON'T import torch unless BINBOT_ENABLE_LSTM=1 is set. LSTM stays off regardless
# (bot.py sets self.lstm=None); this just reclaims the memory. The _LSTMNet stub below
# keeps ml.py importable when torch isn't loaded.
import os as _os_ml
if _os_ml.getenv('BINBOT_ENABLE_LSTM', '0') == '1':
    try:
        import torch
        import torch.nn as nn
        TORCH_AVAILABLE = True
    except ImportError:
        TORCH_AVAILABLE = False
else:
    TORCH_AVAILABLE = False

class _LSTMNet:
    """v15.1: Simple 1-layer LSTM for price direction prediction.
    Only instantiated if PyTorch is available."""
    pass

if TORCH_AVAILABLE:
    class _LSTMNet(nn.Module):
        def __init__(self, input_size=5, hidden_size=32, num_layers=1):
            super().__init__()
            self.lstm = nn.LSTM(input_size, hidden_size, num_layers, batch_first=True)
            self.fc = nn.Linear(hidden_size, 2)

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.fc(out[:, -1, :])


class LSTMPredictor:
    """v15.1: LSTM-based sequential pattern detector.

    Unlike tree-based models (RF/GB/LGBM) which treat each feature snapshot
    independently, LSTM learns patterns ACROSS time steps — e.g. "RSI falling
    for 5 candles then volume spikes" as a sequence, not isolated features.

    Architecture: 1-layer LSTM, 32 hidden units, 60-candle lookback.
    Input: normalized OHLCV sequences (% change from first candle).
    Output: P(price_up) probability.

    Optional: only runs if PyTorch is installed. Falls back to 0.5 (neutral)."""

    def __init__(self):
        self._model = None
        self._ready = False
        self._accuracy = 0.0
        self._seq_len = 60
        if not TORCH_AVAILABLE:
            log.info("🧠 LSTM: PyTorch not installed — LSTM model disabled")

    def train(self, candles, epochs=20, lr=0.001):
        """Train LSTM on OHLCV sequences."""
        if not TORCH_AVAILABLE or len(candles) < 200:
            return False
        try:
            X_seqs, y_labels = [], []
            for i in range(self._seq_len, len(candles) - 10):
                window = candles[i - self._seq_len:i]
                base_c = window[0].c
                if base_c <= 0: continue
                seq = []
                for c in window:
                    seq.append([
                        (c.o - base_c) / base_c,
                        (c.h - base_c) / base_c,
                        (c.l - base_c) / base_c,
                        (c.c - base_c) / base_c,
                        min(c.v / (window[0].v + 1e-8), 5.0)
                    ])
                X_seqs.append(seq)
                future_high = max(c.h for c in candles[i:i+10])
                y_labels.append(1 if future_high >= candles[i].c * 1.005 else 0)

            if len(X_seqs) < 100: return False

            X = torch.FloatTensor(X_seqs)
            y = torch.LongTensor(y_labels)

            # Time-series split (80/20, no shuffle)
            split = int(len(X) * 0.8)
            X_train, X_val = X[:split], X[split:]
            y_train, y_val = y[:split], y[split:]

            self._model = _LSTMNet(input_size=5, hidden_size=32)
            optimizer = torch.optim.Adam(self._model.parameters(), lr=lr)
            criterion = nn.CrossEntropyLoss()

            self._model.train()
            for epoch in range(epochs):
                optimizer.zero_grad()
                outputs = self._model(X_train)
                loss = criterion(outputs, y_train)
                loss.backward()
                optimizer.step()

            # Validation accuracy
            self._model.eval()
            with torch.no_grad():
                val_out = self._model(X_val)
                preds = val_out.argmax(dim=1)
                self._accuracy = float((preds == y_val).float().mean())

            self._ready = True
            log.info(f"🧠 LSTM trained: {len(X_train)} samples | Val acc: {self._accuracy:.1%} | Loss: {loss.item():.4f}")

            if self._accuracy > 0.85:
                log.warning(f"⚠️ LSTM accuracy {self._accuracy:.1%} > 85% — likely overfit, disabling")
                self._ready = False
            elif self._accuracy < 0.45:
                log.warning(f"⚠️ LSTM accuracy {self._accuracy:.1%} < 45% — no edge, disabling")
                self._ready = False

            return True
        except Exception as e:
            log.warning(f"LSTM train failed: {e}")
            return False

    def predict(self, candles):
        """Predict P(up) from last 60 candles. Returns 0.5 if unavailable."""
        if not TORCH_AVAILABLE or not self._ready or self._model is None:
            return 0.5
        try:
            if len(candles) < self._seq_len:
                return 0.5
            window = candles[-self._seq_len:]
            base_c = window[0].c
            if base_c <= 0: return 0.5
            seq = []
            for c in window:
                seq.append([
                    (c.o - base_c) / base_c,
                    (c.h - base_c) / base_c,
                    (c.l - base_c) / base_c,
                    (c.c - base_c) / base_c,
                    min(c.v / (window[0].v + 1e-8), 5.0)
                ])
            X = torch.FloatTensor([seq])
            self._model.eval()
            with torch.no_grad():
                out = self._model(X)
                probs = torch.softmax(out, dim=1)
                return float(probs[0][1])
        except Exception:
            return 0.5

    def confidence_boost(self):
        if not self._ready: return 0.0
        return 0.08 if self._accuracy > 0.58 else 0.04 if self._accuracy > 0.52 else 0.0

    def status(self):
        if not TORCH_AVAILABLE: return "LSTM:off"
        if not self._ready: return "LSTM:untrained"
        return f"LSTM:{self._accuracy:.0%}"


class MLPredictor:
    def __init__(self, retrain_hours=6):
        self.retrain_hours=retrain_hours; self.model=None; self._gb=None; self._lgbm=None; self.last_train=0
        self._xgb=None; self._cat=None
        self._scaler=None
        self.accuracy=0.0; self._ready=False
        self._outlier = OutlierDetector(threshold=3.0)
        self._train_lock = __import__('threading').Lock()
        self._pca = None
        # v14.6.5 AUDIT FIX (F4+F5): refs to MetaLearner / ModelSelector for
        # ensemble weighting + per-model gating during prediction. Set by bot.py
        # after construction (e.g. `self.ml.meta_learner = self.meta_learner`).
        # When None (early boot / standalone use), falls back to equal weights
        # and all-active models — preserves prior behavior.
        self.meta_learner = None
        self.model_selector = None
        try:
            import pickle
            # v13.5.3 audit Bug #30: anchor ml_models.pkl path to this module's
            # directory. Was: relative "ml_models.pkl" — only worked when systemd
            # set WorkingDirectory. A side-script run from / or /tmp would silently
            # fail to load, retraining models from scratch.
            _ml_dir = os.path.dirname(os.path.abspath(__file__))
            _ml_path = os.path.join(_ml_dir, "ml_models.pkl")
            if os.path.exists(_ml_path):
                with open(_ml_path, "rb") as f:
                    saved = pickle.load(f)
                self.model = saved["rf"]; self._gb = saved["gb"]
                self._lgbm = saved.get("lgbm"); self.accuracy = saved["acc"]
                self._pca = saved.get("pca")
                self._xgb = saved.get("xgb")
                self._cat = saved.get("cat")
                self._scaler = saved.get("scaler")
                # v13.5.3 audit Bug #41: restore outlier detector if pickled.
                # Old (pre-v13.5.3) saves didn't include it → fall back to fresh
                # instance, same as before. New saves preserve fitted _mean/_std.
                _saved_outlier = saved.get("outlier")
                if _saved_outlier is not None:
                    self._outlier = _saved_outlier
                self._ready = True; self.last_train = time.time()
                n_models = sum(1 for m in [self.model, self._gb, self._lgbm, self._xgb, self._cat] if m is not None)
                _outlier_status = "fitted" if (getattr(self._outlier, "_mean", None) is not None) else "fresh"
                log.info(f"🧠 ML loaded from disk | Acc:{self.accuracy:.1%} | Models:{n_models}/5 | PCA:{'yes' if self._pca else 'no'} | Scaler:{'yes' if self._scaler else 'no'} | Outlier:{_outlier_status}")
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")

    def should_retrain(self): return time.time()-self.last_train>self.retrain_hours*3600

    def staleness_factor(self):
        """Returns 0.0-1.0. Fresh model=1.0, >24h old=0.5, >48h=0.25."""
        if self.last_train == 0: return 0.5
        age_h = (time.time() - self.last_train) / 3600
        if age_h < 8: return 1.0
        if age_h < 16: return 0.85
        if age_h < 24: return 0.65
        if age_h < 48: return 0.50
        return 0.25


    @staticmethod
    def _gmadl_weights(candles_window, label_arr):
        """GMADL: Generalized Mean Absolute Directional Loss weighting.
        
        Assigns higher sample weights to candles with CLEAR directional moves.
        Forces the ML ensemble to learn patterns that precede strong, unambiguous
        price action — not marginal moves that barely clear the threshold.
        
        Weight tiers:
        - Crystal clear move (>2% range + >1.5% close change): 3.0x
        - Clear directional (>1% range + >0.75% close change): 2.0x  
        - Moderate signal (>0.5% range): 1.5x
        - Ambiguous / small move: 1.0x (base)
        """
        import numpy as np
        weights = np.ones(len(label_arr))
        for i, lbl in enumerate(label_arr):
            if i >= len(candles_window):
                break
            try:
                c = candles_window[i]
                # Range as % of close
                rng_pct = (c.h - c.l) / c.c * 100 if c.c > 0 else 0
                # Close momentum (c vs open)
                mom_pct = abs(c.c - c.o) / c.o * 100 if c.o > 0 else 0
                # Direction agreement: label 1 (win) should align with bullish candle
                direction_ok = (lbl == 1 and c.c > c.o) or (lbl == 0 and c.c < c.o)
                if rng_pct > 2.0 and mom_pct > 1.5 and direction_ok:
                    weights[i] = 3.0    # crystal clear directional move
                elif rng_pct > 1.0 and mom_pct > 0.75:
                    weights[i] = 2.0    # clear direction
                elif rng_pct > 0.5:
                    weights[i] = 1.5    # moderate signal
                # else 1.0 base weight
            except Exception:
                pass
        return weights

    def train(self, candles, ta):
        if not self._train_lock.acquire(blocking=False):
            log.info("🧠 ML train skipped — already training on another thread")
            return False
        try:
            if len(candles)<500: return False
            features, labels, sample_candle_idx = [], [], []
            for i in range(100,len(candles)-10):
                w=candles[max(0,i-100):i+1]; f=self._feat(w,ta)
                if f is None: continue
                entry_price = candles[i].c
                # v13.2 FIX: Symmetric thresholds for balanced labels
                # Old: +1% TP / -2% SL → 90%+ wins (trivially predictable)
                # New: +0.5% TP / -0.5% SL → ~50/50 balance (forces real learning)
                # If neither threshold hit → skip sample (flat market, no signal)
                win = -1  # sentinel: neither threshold hit
                # v13.3 $200-TIER: ATR-relative labels when flag active
                try:
                    from feature_flags import get as _ff
                    _use_atr = _ff("atr_labels", False)
                except Exception:
                    _use_atr = False
                if _use_atr:
                    _atr_vals = [c.h - c.l for c in candles[max(0,i-14):i]]
                    _atr = sum(_atr_vals)/len(_atr_vals) if _atr_vals else entry_price*0.01
                    _win_t  = entry_price + max(_atr * 1.0,  entry_price * 0.005)
                    _loss_t = entry_price - max(_atr * 0.5,  entry_price * 0.003)
                else:
                    _win_t  = entry_price * 1.005   # fixed +0.5%
                    _loss_t = entry_price * 0.995   # fixed -0.5%
                for future_c in candles[i+1:i+11]:
                    if future_c.l <= _loss_t:  # loss threshold hit first
                        win = 0
                        break
                    if future_c.h >= _win_t:   # win threshold hit first
                        win = 1
                        break
                if win < 0: continue  # flat market — skip, don't bias as loss
                labels.append(win)
                features.append(f)
                # v14.6.5 AUDIT FIX (F6): track which candle produced each feature/label
                # so GMADL weights can be aligned correctly below (skipped samples
                # break the previous "len(gmadl_w) == n_samples" assumption).
                sample_candle_idx.append(i)
            if len(features)<100: return False
            X_raw, y = np.array(features), np.array(labels)
            n_samples = len(X_raw)

            # v14.6.5 AUDIT FIX (F6): build GMADL weights aligned to actual sample candles.
            base_weights = np.linspace(1.0, 3.0, n_samples)  # recency bias
            try:
                # Compute GMADL weight per sample by inspecting the candle at sample_candle_idx[i]
                gmadl_w = np.ones(n_samples)
                for k, ci in enumerate(sample_candle_idx):
                    try:
                        c = candles[ci]
                        rng_pct = (c.h - c.l) / c.c * 100 if c.c > 0 else 0
                        chg_pct = abs(c.c - c.o) / c.o * 100 if c.o > 0 else 0
                        if rng_pct > 2.0 and chg_pct > 1.5:
                            gmadl_w[k] = 3.0
                        elif rng_pct > 1.0 and chg_pct > 0.75:
                            gmadl_w[k] = 2.0
                        elif rng_pct > 0.5:
                            gmadl_w[k] = 1.5
                    except Exception:
                        pass
                sample_weights = base_weights * gmadl_w
                log.info(f"🧠 GMADL weights applied (aligned) — mean={gmadl_w.mean():.2f} "
                         f"max={gmadl_w.max():.1f} high_conf={int((gmadl_w>=2.0).sum())}/{n_samples}")
            except Exception as _ge:
                log.debug(f"GMADL fallback: {_ge}")
                sample_weights = base_weights

            # v14.6.5 AUDIT FIX (F1+F2): walk-forward CV with per-fold scaler+PCA.
            # Previously: scaler and PCA were fit on the FULL X before TimeSeriesSplit,
            # leaking validation-fold variance structure into training and inflating
            # reported accuracy by 2-8%. Now: each fold fits its own transforms on
            # training data only; production model is fit on full data AFTER CV.
            from sklearn.model_selection import TimeSeriesSplit
            try:
                from sklearn.preprocessing import StandardScaler
                _SCALER_AVAILABLE = True
            except ImportError:
                _SCALER_AVAILABLE = False
            try:
                from sklearn.decomposition import PCA
                _PCA_AVAILABLE = True
            except ImportError:
                _PCA_AVAILABLE = False

            n_avail = n_samples
            if n_avail >= 600:   N_FOLDS, PURGE_GAP = 5, 50
            elif n_avail >= 300: N_FOLDS, PURGE_GAP = 4, 30
            elif n_avail >= 150: N_FOLDS, PURGE_GAP = 3, 20
            else:                N_FOLDS, PURGE_GAP = 2, 10
            tscv = TimeSeriesSplit(n_splits=N_FOLDS, gap=PURGE_GAP)
            fold_accs = {"rf": [], "gb": [], "lgbm": [], "xgb": [], "cat": []}

            for train_idx, val_idx in tscv.split(X_raw):
                Xt_raw, Xv_raw = X_raw[train_idx], X_raw[val_idx]
                yt, yv = y[train_idx], y[val_idx]
                wt = sample_weights[train_idx]
                if len(set(yt)) < 2: continue

                # Fit transformations on TRAINING fold only — F1+F2 fix
                if _SCALER_AVAILABLE:
                    _fold_scaler = StandardScaler()
                    Xt = _fold_scaler.fit_transform(Xt_raw)
                    Xv = _fold_scaler.transform(Xv_raw)
                else:
                    Xt, Xv = Xt_raw, Xv_raw
                if _PCA_AVAILABLE and Xt.shape[1] > 10:
                    try:
                        _fold_pca = PCA(n_components=0.95)
                        Xt_p = _fold_pca.fit_transform(Xt)
                        if Xt_p.shape[1] < 12:
                            _fold_pca = PCA(n_components=min(12, Xt.shape[1]))
                            Xt_p = _fold_pca.fit_transform(Xt)
                        if Xt_p.shape[1] < Xt.shape[1]:
                            Xt = Xt_p
                            Xv = _fold_pca.transform(Xv)
                    except Exception as _pe:
                        log.debug(f"Fold PCA skipped: {_pe}")

                # v13.1: Regularized — max_depth 5, min_samples_leaf 10
                rf = RandomForestClassifier(n_estimators=100,max_depth=5,min_samples_split=15,
                                            min_samples_leaf=10,max_features='sqrt',
                                            random_state=42,n_jobs=-1)
                rf.fit(Xt,yt,sample_weight=wt)
                gb = GradientBoostingClassifier(n_estimators=80,max_depth=4,learning_rate=0.05,
                                                min_samples_leaf=10,subsample=0.8,random_state=42)
                gb.fit(Xt,yt,sample_weight=wt)
                fold_accs["rf"].append(rf.score(Xv,yv))
                fold_accs["gb"].append(gb.score(Xv,yv))
                if LGBM_AVAILABLE:
                    try:
                        lgbm = LGBMClassifier(n_estimators=100,max_depth=5,learning_rate=0.05,
                                               num_leaves=20,min_child_samples=15,
                                               subsample=0.8,colsample_bytree=0.8,
                                               random_state=42,verbose=-1)
                        lgbm.fit(Xt,yt,sample_weight=wt)
                        fold_accs["lgbm"].append(lgbm.score(Xv,yv))
                    except Exception as e: log.warning(f"LGBM train: {e}")
                if XGB_AVAILABLE:
                    try:
                        # v13.4 fix (Batch 1): removed use_label_encoder=False — deprecated
                        # in xgboost ≥1.6 and removed entirely in ≥2.0. Kept the bare except
                        # below would have silently dropped XGB from the ensemble on a fresh
                        # install (5-model → 4-model with no operator-visible warning).
                        xgb = XGBClassifier(n_estimators=100,max_depth=5,learning_rate=0.05,
                                            subsample=0.8,colsample_bytree=0.8,min_child_weight=10,
                                            reg_alpha=0.1,reg_lambda=1.0,
                                            eval_metric='logloss',random_state=42,verbosity=0)
                        xgb.fit(Xt,yt,sample_weight=wt)
                        fold_accs["xgb"].append(xgb.score(Xv,yv))
                    except Exception as e: log.warning(f"XGB train: {e}")
                if CATBOOST_AVAILABLE:
                    try:
                        cat = CatBoostClassifier(iterations=100,depth=5,learning_rate=0.05,
                                                 l2_leaf_reg=5.0,subsample=0.8,random_state=42,verbose=0)
                        cat.fit(Xt,yt,sample_weight=wt)
                        fold_accs["cat"].append(cat.score(Xv,yv))
                    except Exception as e: log.warning(f"CatBoost train: {e}")

            # v14.6.5 AUDIT FIX (F1+F2+F12): now fit PRODUCTION transforms + models
            # on the FULL training data. CV gave honest accuracy; production model
            # benefits from all available samples (subsumes the "last-fold-only" bias
            # the audit flagged as F12 — best-fold-cherry-pick is also eliminated).
            if _SCALER_AVAILABLE:
                self._scaler = StandardScaler()
                X_full = self._scaler.fit_transform(X_raw)
                log.info(f"📊 Feature normalization: {X_full.shape[1]} features Z-scored")
            else:
                self._scaler = None
                X_full = X_raw

            self._pca = None
            if _PCA_AVAILABLE and X_full.shape[1] > 10:
                try:
                    pca = PCA(n_components=0.95)
                    X_pca = pca.fit_transform(X_full)
                    n_orig = X_full.shape[1]; n_reduced = X_pca.shape[1]
                    if n_reduced < 12:
                        pca = PCA(n_components=min(12, n_orig))
                        X_pca = pca.fit_transform(X_full)
                        n_reduced = X_pca.shape[1]
                    if n_reduced < n_orig:
                        self._pca = pca
                        X_full = X_pca
                        log.info(f"🔬 PCA: {n_orig} → {n_reduced} features (min 12 enforced)")
                except Exception as e: log.debug(f"Production PCA skipped: {e}")

            # Train production models on full data with full sample_weights
            self.model = RandomForestClassifier(n_estimators=100,max_depth=5,min_samples_split=15,
                                                min_samples_leaf=10,max_features='sqrt',
                                                random_state=42,n_jobs=-1)
            self.model.fit(X_full, y, sample_weight=sample_weights)
            self._gb = GradientBoostingClassifier(n_estimators=80,max_depth=4,learning_rate=0.05,
                                                  min_samples_leaf=10,subsample=0.8,random_state=42)
            self._gb.fit(X_full, y, sample_weight=sample_weights)
            self._lgbm = None
            if LGBM_AVAILABLE:
                try:
                    self._lgbm = LGBMClassifier(n_estimators=100,max_depth=5,learning_rate=0.05,
                                                num_leaves=20,min_child_samples=15,
                                                subsample=0.8,colsample_bytree=0.8,
                                                random_state=42,verbose=-1)
                    self._lgbm.fit(X_full, y, sample_weight=sample_weights)
                except Exception as e:
                    log.warning(f"LGBM production train: {e}"); self._lgbm = None
            self._xgb = None
            if XGB_AVAILABLE:
                try:
                    self._xgb = XGBClassifier(n_estimators=100,max_depth=5,learning_rate=0.05,
                                              subsample=0.8,colsample_bytree=0.8,min_child_weight=10,
                                              reg_alpha=0.1,reg_lambda=1.0,
                                              eval_metric='logloss',random_state=42,verbosity=0)
                    self._xgb.fit(X_full, y, sample_weight=sample_weights)
                except Exception as e:
                    log.warning(f"XGB production train: {e}"); self._xgb = None
            self._cat = None
            if CATBOOST_AVAILABLE:
                try:
                    self._cat = CatBoostClassifier(iterations=100,depth=5,learning_rate=0.05,
                                                   l2_leaf_reg=5.0,subsample=0.8,random_state=42,verbose=0)
                    self._cat.fit(X_full, y, sample_weight=sample_weights)
                except Exception as e:
                    log.warning(f"CatBoost production train: {e}"); self._cat = None

            # v13.1: HONEST accuracy — mean across folds (NOT inflated by leakage anymore)
            mean_accs = {}
            for name, accs in fold_accs.items():
                if accs: mean_accs[name] = np.mean(accs)
            all_means = [v for v in mean_accs.values() if v > 0]
            self.accuracy = np.mean(all_means) if all_means else 0
            n_trained = sum(1 for m in [self.model, self._gb, self._lgbm, self._xgb, self._cat] if m is not None)
            acc_parts = []
            for name in ["rf","gb","lgbm","xgb","cat"]:
                accs = fold_accs[name]
                if accs: acc_parts.append(f"{name.upper()}:{np.mean(accs):.1%}±{np.std(accs):.1%}")
                else: acc_parts.append(f"{name.upper()}:N/A")
            log.info(f"  {' '.join(acc_parts)} ({n_trained}/5, {N_FOLDS}-fold purged WF, gap={PURGE_GAP})")
            for name, accs in fold_accs.items():
                if accs and np.mean(accs) > 0.90:
                    log.warning(f"⚠️ {name.upper()} mean {np.mean(accs):.1%} suspiciously high — possible overfit")
                if accs and np.std(accs) > 0.15:
                    log.warning(f"⚠️ {name.upper()} fold variance {np.std(accs):.1%} — unstable")
            # v14.6.5 AUDIT FIX (F13): auto-demote models with mean CV accuracy
            # > 85% — strong overfit signal in binary classification on noisy crypto
            # data. Demote via ModelSelector (don't delete — they may recover after
            # next retrain on different data window). Skips if selector not wired.
            if self.model_selector is not None:
                for name, accs in fold_accs.items():
                    if accs and np.mean(accs) > 0.85:
                        try:
                            self.model_selector.active[name] = False
                            log.warning(f"🔻 {name.upper()} auto-demoted (mean acc {np.mean(accs):.1%} > 85%, likely overfit)")
                        except Exception: pass
            self.last_train=time.time(); self._ready=True
            self._outlier.fit(np.array(features))
            log.info(f"🛡️ Outlier detector fitted on {len(features)} samples")
            # Feature importance
            if self.model is not None:
                imp = self.model.feature_importances_
                if self._pca is not None:
                    # v13.2 FIX: After PCA, importances map to components not features
                    top_pc = sorted(enumerate(imp), key=lambda x: x[1], reverse=True)[:5]
                    log.info(f"Top PCA components: {', '.join(f'PC{i}:{v:.2f}' for i,v in top_pc)}")
                else:
                    names=['rsi','rsi_slope','macd_h','macd_x','bb_pos','bb_w','adx','vol_r',
                           'e9d','e21d','e50d','pc5','pc10','pc20','atr_pct',
                           'body_r','wick_r','consec_g','hh','vol_spk',
                           'rsi_t3','rsi_t10','macd_t3','bb_t5','atr_sl','sqz','vwap_d',
                           'rng_exp','engulf','buy_press','price_accel','close_pos','trend_con','vol_trend','dist_20h']
                    top=sorted(zip(names[:len(imp)],imp),key=lambda x:x[1],reverse=True)[:5]
                    weak = [n for n,v in zip(names[:len(imp)],imp) if v < 0.02]
                    if weak: log.info(f"Weak features (<2%): {', '.join(weak)}")
                    log.info(f"Top: {', '.join(f'{n}:{v:.2f}' for n,v in top)}")
            # Save models
            try:
                import pickle
                from datetime import datetime
                # v13.5.3 audit Bug #41: was missing "outlier" → outlier detector
                # state lost on restart. is_outlier() then returned 0 (since _mean
                # is None on fresh init) → never flagged outliers → DI safety
                # system silently disabled for the 6h between restart and next
                # retrain. Now: save the OutlierDetector instance itself; load
                # path restores it.
                save_data = {"rf": self.model, "gb": self._gb, "lgbm": self._lgbm,
                             "xgb": self._xgb, "cat": self._cat,
                             "scaler": self._scaler, "acc": self.accuracy, "pca": self._pca,
                             "outlier": self._outlier}
                # Anchor model file path to module dir (Bug #30: same fix v13.5.2
                # applied to BOTDIR for engine scripts; propagated here).
                _ml_dir = os.path.dirname(os.path.abspath(__file__))
                with open(os.path.join(_ml_dir, "ml_models.pkl"), "wb") as f:
                    pickle.dump(save_data, f)
                # v13.5.3: timestamp in UTC to match every other ts in the codebase.
                from datetime import timezone as _tz
                ts = datetime.now(_tz.utc).strftime("%Y%m%d_%H%M")
                ver_path = os.path.join(_ml_dir, f"ml_models_{ts}.pkl")
                with open(ver_path, "wb") as f:
                    pickle.dump(save_data, f)
                import glob
                old = sorted(glob.glob(os.path.join(_ml_dir, "ml_models_*.pkl")))[:-5]
                for fp in old:
                    try: os.remove(fp)
                    except Exception: pass
                log.info(f"💾 Models saved: ml_models.pkl + ml_models_{ts}.pkl")
            except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
            log.info(f"🧠 ML trained | Acc:{self.accuracy:.1%} (MEAN) | Feats:{X_full.shape[1]} | Samples:{len(X_full)} | Stale:{self.staleness_factor():.0%}")
            return True
        except Exception as e: log.error(f"ML train: {e}"); return False
        finally:
            self._train_lock.release()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v8.3 MODULE 7: Reinforcement Learning Agent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _feat(self, candles, ta):
        """v12.0: Expanded feature set (27 features).
        Original 15 + 12 new: body_ratio, wick_ratio, consec_green, higher_high,
        vol_spike, rsi_t3, rsi_t10, macd_h_t3, bb_pos_t5, atr_slope, squeeze, vwap_dist."""
        if len(candles)<60: return None
        try:
            cc=[c.c for c in candles]; p=cc[-1]
            highs=[c.h for c in candles]; lows=[c.l for c in candles]
            opens=[c.o for c in candles]; vols=[c.v for c in candles]
            rsi=ta.rsi(candles); rsi_p=ta.rsi(candles[:-5]) if len(candles)>55 else rsi
            ml,sl,hist=ta.macd(cc); bu,bm,bl,bw=ta.bb(cc)
            e9,e21,e50=ta.ema(cc,9),ta.ema(cc,21),ta.ema(cc,50)
            atr_val=ta.atr(candles)
            # === Original 15 features ===
            feat = [rsi,(rsi-rsi_p)/5,hist[-1] if hist else 0,
                    1 if len(ml)>=2 and len(sl)>=2 and ml[-1]>sl[-1] and ml[-2]<=sl[-2] else 0,
                    (p-bl)/(bu-bl) if bu>bl else 0.5, bw, ta.adx(candles), ta.vol_ratio(candles),
                    (p-e9[-1])/p*100 if e9 else 0, (p-e21[-1])/p*100 if e21 else 0,
                    (p-e50[-1])/p*100 if e50 else 0,
                    (cc[-1]-cc[-6])/cc[-6]*100 if len(cc)>6 else 0,
                    (cc[-1]-cc[-11])/cc[-11]*100 if len(cc)>11 else 0,
                    (cc[-1]-cc[-21])/cc[-21]*100 if len(cc)>21 else 0,
                    atr_val/p*100 if p>0 else 0]
            # === v12.0: New features ===
            c_last=candles[-1]
            body=abs(c_last.c-c_last.o); rng=c_last.h-c_last.l+1e-10
            feat.append(body/rng)  # body_ratio: how much of candle is body vs wick
            upper_wick=c_last.h-max(c_last.o,c_last.c); lower_wick=min(c_last.o,c_last.c)-c_last.l
            feat.append((upper_wick-lower_wick)/rng)  # wick_ratio: +1=upper wick, -1=lower wick
            # Consecutive green candles (momentum)
            cg=0
            for i in range(len(candles)-1,max(len(candles)-11,-1),-1):
                if candles[i].c>candles[i].o: cg+=1
                else: break
            feat.append(min(cg,10))  # consec_green: capped at 10
            # Higher high vs previous swing
            feat.append(1 if len(highs)>10 and highs[-1]>max(highs[-11:-1]) else 0)  # higher_high
            # Volume spike (current vs 20-period avg)
            avg_vol=sum(vols[-20:])/20 if len(vols)>=20 else sum(vols)/max(len(vols),1)
            feat.append(vols[-1]/max(avg_vol,1e-10))  # vol_spike
            # Lagged RSI (gives model momentum-of-momentum)
            rsi_t3=ta.rsi(candles[:-3]) if len(candles)>58 else rsi
            rsi_t10=ta.rsi(candles[:-10]) if len(candles)>65 else rsi
            feat.append(rsi-rsi_t3)   # rsi_t3: RSI change over 3 candles
            feat.append(rsi-rsi_t10)  # rsi_t10: RSI change over 10 candles
            # Lagged MACD histogram
            feat.append(hist[-1]-hist[-4] if hist and len(hist)>4 else 0)  # macd_h_t3
            # Lagged BB position (5 candles ago)
            p5=cc[-6] if len(cc)>6 else p
            feat.append((p5-bl)/(bu-bl) if bu>bl else 0.5)  # bb_pos_t5
            # ATR slope (volatility trend)
            if len(candles)>20:
                atr_prev=ta.atr(candles[:-5])
                feat.append((atr_val-atr_prev)/max(atr_prev,1e-10))  # atr_slope
            else:
                feat.append(0)
            # BB squeeze detection
            try:
                sq_flag, sq_len = ta.bb_squeeze(candles)
                feat.append(1 if sq_flag else 0)  # squeeze: 1=squeezed
            except Exception: feat.append(0)
            # VWAP distance
            try:
                vw=ta.vwap(candles)
                feat.append((p-vw)/p*100 if vw>0 and p>0 else 0)  # vwap_dist
            except Exception: feat.append(0)
            # === v13.2: Cross-Market & Structure Features (8 new → 35 total) ===
            # 28. Range expansion: current range vs avg range (breakout detection)
            ranges = [c.h - c.l for c in candles[-20:]]
            avg_range = sum(ranges) / len(ranges) if ranges else 1e-10
            feat.append((candles[-1].h - candles[-1].l) / max(avg_range, 1e-10))  # range_exp
            # 29. Engulfing strength: body size vs previous body
            prev_body = abs(candles[-2].c - candles[-2].o) if len(candles) > 1 else 1e-10
            feat.append(body / max(prev_body, 1e-10))  # engulf_ratio
            # 30. Buy pressure proxy: (close - low) / (high - low)
            feat.append((candles[-1].c - candles[-1].l) / rng)  # buy_pressure
            # 31. Price acceleration: rate of change of rate of change
            if len(cc) > 21:
                roc10 = (cc[-1] - cc[-11]) / cc[-11] * 100 if cc[-11] != 0 else 0
                roc10_prev = (cc[-6] - cc[-16]) / cc[-16] * 100 if cc[-16] != 0 else 0
                feat.append(roc10 - roc10_prev)  # price_accel
            else:
                feat.append(0)
            # 32. Close position in daily range: 1=top, 0=bottom
            h20 = max(highs[-20:]); l20 = min(lows[-20:])
            feat.append((p - l20) / max(h20 - l20, 1e-10))  # close_pos_20
            # 33. Trend consistency: % of last 10 candles that are green
            green_pct = sum(1 for c in candles[-10:] if c.c > c.o) / 10
            feat.append(green_pct)  # trend_consist
            # 34. Volume trend: vol change over 10 candles
            vol_early = sum(vols[-20:-10]) / 10 if len(vols) >= 20 else sum(vols) / max(len(vols), 1)
            vol_late = sum(vols[-10:]) / 10 if len(vols) >= 10 else vol_early
            feat.append(vol_late / max(vol_early, 1e-10))  # vol_trend
            # 35. Distance from 20-period high (overhead resistance proxy)
            feat.append((h20 - p) / max(p, 1e-10) * 100)  # dist_20h
            return feat
        except Exception: return None

    @property
    def confidence_boost(self):
        if not self._ready: return 0.0
        # v12.0: Apply staleness degradation to confidence boost
        base = 0.15 if self.accuracy>0.65 else 0.10 if self.accuracy>0.55 else 0.05
        return base * self.staleness_factor()

    def predict(self, candles, ta):
        if not self._ready or self.model is None: return 0.5
        try:
            f=self._feat(candles,ta)
            if f is None: return 0.5
            # v12.0: Outlier check — suppress prediction in unknown markets
            if self._outlier.is_outlier(f):
                log.debug(f"🛡️ ML outlier detected (DI={self._outlier.last_score:.1f}) — returning neutral 0.5")
                return 0.5
            f_arr = np.array([f])
            # v13.0: Apply StandardScaler if available (must match training normalization)
            if self._scaler is not None:
                try: f_arr = self._scaler.transform(f_arr)
                except Exception: pass
            # v12.1: Apply PCA transform if available
            if self._pca is not None:
                try: f_arr = self._pca.transform(f_arr)
                except Exception: pass
            rf_p = self.model.predict_proba(f_arr)[0]
            rf_score = float(rf_p[1]) if len(rf_p)>=2 else 0.5
            # v14.6.5 AUDIT FIX (F4+F5): use MetaLearner weights and ModelSelector
            # active flags when available. Previously: hardcoded 0.20 equal weights
            # (MetaLearner.get_weights() was never called), and all models always
            # contributed regardless of ModelSelector.is_active(). Now: dynamic
            # weighting + per-model gating. Falls back to equal weights / all-active
            # when refs aren't wired up.
            def _w(model_name, default=0.20):
                if self.meta_learner is not None:
                    try: return float(self.meta_learner.get_weights().get(model_name, default))
                    except Exception: return default
                return default
            def _active(model_name):
                if self.model_selector is not None:
                    try: return bool(self.model_selector.is_active(model_name))
                    except Exception: return True
                return True

            scores, weights, names_used = [], [], []
            if _active("rf"):
                scores.append(rf_score); weights.append(_w("rf")); names_used.append("rf")
            try:
                if self._gb is not None and _active("gb"):
                    gb_p = self._gb.predict_proba(f_arr)[0]
                    scores.append(float(gb_p[1]) if len(gb_p)>=2 else 0.5)
                    weights.append(_w("gb")); names_used.append("gb")
            except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
            if self._lgbm is not None and _active("lgbm"):
                try:
                    lgbm_p = self._lgbm.predict_proba(f_arr)[0]
                    scores.append(float(lgbm_p[1]) if len(lgbm_p)>=2 else 0.5)
                    weights.append(_w("lgbm")); names_used.append("lgbm")
                except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
            # v13.0: XGBoost prediction
            if self._xgb is not None and _active("xgb"):
                try:
                    xgb_p = self._xgb.predict_proba(f_arr)[0]
                    scores.append(float(xgb_p[1]) if len(xgb_p)>=2 else 0.5)
                    weights.append(_w("xgb")); names_used.append("xgb")
                except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
            # v13.0: CatBoost prediction
            if self._cat is not None and _active("cat"):
                try:
                    cat_p = self._cat.predict_proba(f_arr)[0]
                    scores.append(float(cat_p[1]) if len(cat_p)>=2 else 0.5)
                    weights.append(_w("cat")); names_used.append("cat")
                except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
            tw = sum(weights)
            return sum(s*w for s,w in zip(scores, weights))/tw if tw>0 else 0.5
        except Exception: return 0.5



class RLAgent:
    """v12.1: Upgraded RL with experience replay + MLP DQN.
    Q-table is kept as base. MLP learns from experience buffer and can
    generalize to unseen states (unlike discrete Q-table lookup).
    Falls back gracefully to Q-table if MLP unavailable."""
    def __init__(self, save_path="rl_qtable.json"):
        self.save_path=save_path; self.n_actions=3; self.q_table={}
        self.alpha=0.1; self.gamma=0.95; self.epsilon=0.15; self.min_epsilon=0.05; self.epsilon_decay=0.995
        self.current_state=None; self.current_action=None; self.total_trades=0; self.total_reward=0
        # v13.3 $500-TIER: per-position state tracking (replaces single global state)
        self._pos_states: dict = {}   # pair → state_key
        # v12.1: Experience replay buffer for DQN
        self._experience = []  # [(state_vec, action, reward)]
        self._mlp = None
        self._mlp_ready = False
        self._retrain_every = 20  # Retrain MLP every 20 trades
        self._load()
    def _state_key(self,regime,trend,atr_pct,fg):
        r=regime if regime in ["TREND_UP","TREND_DOWN","VOLATILE","SQUEEZE","RANGE","CHOPPY"] else "RANGE"
        t=trend if trend in ["BULL","BEAR"] else "NEUTRAL"
        v="LOW" if atr_pct<1.0 else "MED" if atr_pct<3.0 else "HIGH"
        f="EXTREME_FEAR" if fg<20 else "FEAR" if fg<40 else "NEUTRAL" if fg<60 else "GREED"
        return f"{r}_{t}_{v}_{f}"
    def _state_vec(self, regime, trend, atr_pct, fg):
        """v12.1: Convert state to numeric vector for MLP."""
        regimes = {"TREND_UP":1,"TREND_DOWN":-1,"VOLATILE":0.5,"SQUEEZE":0.3,"RANGE":0,"CHOPPY":-0.3}
        trends = {"BULL":1,"BEAR":-1,"NEUTRAL":0}
        return [regimes.get(regime,0), trends.get(trend,0), min(atr_pct/5.0,1.0), fg/100.0]
    def _get_q(self,state):
        if state not in self.q_table: self.q_table[state]=[0.0,0.0,0.0]
        return self.q_table[state]
    def get_boost(self,regime,trend,atr_pct,fg):
        # v12.1: Try MLP first, fall back to Q-table
        if self._mlp_ready and self._mlp is not None:
            try:
                sv = self._state_vec(regime, trend, atr_pct, fg)
                pred = self._mlp.predict([sv])[0]
                if pred == 0: return 0.75   # SKIP
                if pred == 2: return 1.20   # STRONG BUY
                return 1.05                 # NORMAL BUY
            except Exception: pass
        # Q-table fallback
        state=self._state_key(regime,trend,atr_pct,fg)
        q=self._get_q(state); best=int(np.argmax(q)); mx=max(q)
        if mx==0: return 1.0
        if best==0: return 0.75
        elif best==2: return 1.20
        return 1.05
    def should_block(self,regime,trend,atr_pct,fg):
        state=self._state_key(regime,trend,atr_pct,fg); q=self._get_q(state)
        return q[0]>0.5 and q[0]>q[1]*2
    def reward(self,pnl_pct,regime="RANGE",trend="NEUTRAL",atr_pct=1.0,fg=50,pair=None):
        state=self._state_key(regime,trend,atr_pct,fg)
        # v13.3 $500-TIER: use per-position state if available
        try:
            from feature_flags import get as _ff
            if _ff("rl_per_position", False) and pair and pair in self._pos_states:
                state = self._pos_states.pop(pair)
            elif self.current_state:
                state = self.current_state
        except Exception:
            if self.current_state: state = self.current_state
        action=self.current_action if self.current_action is not None else 1
        r=pnl_pct*1.0 if pnl_pct>=0 else pnl_pct*1.5
        q=self._get_q(state); q[action]=q[action]+self.alpha*(r-q[action])
        self.q_table[state]=q; self.total_trades+=1; self.total_reward+=r
        self.epsilon=max(self.min_epsilon,self.epsilon*self.epsilon_decay)
        # v12.1: Store experience for DQN
        sv = self._state_vec(regime, trend, atr_pct, fg)
        # Map PnL to action label: loss=0(skip), small_win=1(buy), big_win=2(strong_buy)
        label = 0 if pnl_pct < -0.5 else 2 if pnl_pct > 1.0 else 1
        self._experience.append((sv, label))
        if len(self._experience) > 2000:
            self._experience = self._experience[-2000:]
        # Retrain MLP periodically
        if self.total_trades % self._retrain_every == 0 and len(self._experience) >= 50:
            self._train_mlp()
        if self.total_trades%5==0: self._save()
        self.current_state=None; self.current_action=None
    def _train_mlp(self):
        """v12.1: Train MLP DQN from experience replay buffer."""
        try:
            from sklearn.neural_network import MLPClassifier
            X = [e[0] for e in self._experience]
            y = [e[1] for e in self._experience]
            if len(set(y)) < 2: return  # Need at least 2 classes
            mlp = MLPClassifier(hidden_layer_sizes=(32, 16), max_iter=100,
                                random_state=42, warm_start=True)
            mlp.fit(X, y)
            self._mlp = mlp
            self._mlp_ready = True
            log.info(f"🤖 RL DQN trained on {len(X)} experiences | classes={sorted(set(y))}")
        except ImportError:
            pass  # No sklearn neural_network
        except Exception as e:
            log.debug(f"RL MLP train: {e}")
    def record_entry(self,regime,trend,atr_pct,fg,pair=None):
        state=self._state_key(regime,trend,atr_pct,fg)
        self.current_state=state; self.current_action=1
        # v13.3 $500-TIER: also track per-position when flag active
        try:
            from feature_flags import get as _ff
            if _ff("rl_per_position", False) and pair:
                self._pos_states[pair] = state
        except Exception:
            pass
    def _save(self):
        try: Path(self.save_path).write_text(json.dumps({
            "q_table":self.q_table,"epsilon":self.epsilon,
            "total_trades":self.total_trades,"total_reward":self.total_reward,
            "experience": self._experience[-500:]  # v12.1: persist last 500 experiences
        },indent=2))
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
    def _load(self):
        try:
            if Path(self.save_path).exists():
                d=json.loads(Path(self.save_path).read_text())
                self.q_table=d.get("q_table",{}); self.epsilon=d.get("epsilon",0.15)
                self.total_trades=d.get("total_trades",0); self.total_reward=d.get("total_reward",0)
                self._experience=d.get("experience",[])  # v12.1: restore experience
                if len(self._experience) >= 50:
                    self._train_mlp()  # v12.1: rebuild MLP from saved experience
        except Exception as _e: log.debug(f"Suppressed [ml]: {_e}")
    def status(self):
        mlp_tag = "DQN" if self._mlp_ready else "QT"
        return f"RL:{self.total_trades}t/{mlp_tag}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKTESTER (from v6)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

