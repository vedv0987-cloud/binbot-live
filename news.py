# BinBot v11 — news.py
import urllib.request, xml.etree.ElementTree as ET, time, logging
log = logging.getLogger('binbot')

class NewsSentiment:
    BULL = ['surge','soar','rally','pump','bullish','breakout','high','record','gain','rise',
            'moon','buy','adopt','approve','etf','launch','partner','upgrade','institutional',
            'accumulate','whale','support','recover','optimis','momentum','spike','profit','boom']
    BEAR = ['crash','plunge','dump','bearish','hack','ban','scam','fraud','sell','drop','fall',
            'fear','crisis','warning','risk','regulat','fine','lawsuit','collapse','bubble','ponzi',
            'rug','exploit','vulnerability','panic','liquidat','decline','loss','tank','tumble','concern']
    # B2-6: tokens that flip sentiment of a nearby BULL/BEAR keyword
    NEGATIONS = {'no','not','never','wont',"won't",'isnt',"isn't",'dont',"don't",
                 'cant',"can't",'hasnt',"hasn't",'hardly','rarely','unlikely',
                 'avoid','without','despite','reject','denies','denied'}

    def __init__(self):
        self._cache = {"score":0.0,"label":"Neutral","ts":0,"headlines":[]}
        # v11.2.8: track consecutive failures so prolonged outage degrades stale signal
        self._fail_count = 0
        self._first_fail_ts = 0

    def get_sentiment(self):
        now = time.time()
        if now - self._cache["ts"] < 300:  # 5min cache
            return self._cache["score"], self._cache["label"], self._cache["headlines"]
        try:
            url = "https://news.google.com/rss/search?q=bitcoin+OR+crypto+OR+ethereum&hl=en-US&gl=US&ceid=US:en"
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                root = ET.fromstring(resp.read().decode())
                items = root.findall('.//item')
                headlines, total, count = [], 0.0, 0
                for item in items[:30]:
                    title = item.find('title')
                    if title is None or title.text is None: continue
                    h = title.text.lower(); headlines.append(title.text[:80])
                    # B2-6: negation-aware token counting. Tokenize, then check 3
                    # tokens before each keyword match for a negation. "Bitcoin
                    # won't crash" no longer counts as bear; "no scam" doesn't
                    # count as bear either.
                    toks = h.split()
                    def _hit_count(words):
                        n = 0
                        for i, t in enumerate(toks):
                            tt = t.strip('.,!?":;()[]{}\'')
                            matched = False
                            for w in words:
                                # whole-token match for short words (avoid e.g. 'no' inside 'now')
                                # substring match for longer keywords
                                if (len(w) <= 4 and w == tt) or (len(w) > 4 and w in tt):
                                    matched = True; break
                            if not matched:
                                continue
                            prev_window = ' '.join(toks[max(0,i-3):i]).lower()
                            prev_toks = [p.strip('.,!?":;()[]{}\'') for p in prev_window.split()]
                            if any(neg in prev_toks for neg in NewsSentiment.NEGATIONS):
                                continue  # negated → skip
                            n += 1
                        return n
                    bull = _hit_count(self.BULL)
                    bear = _hit_count(self.BEAR)
                    if bull + bear > 0: total += (bull - bear) / (bull + bear); count += 1
                score = max(-1.0, min(1.0, total / max(count, 1)))
                label = "📈Bull" if score>0.3 else ("↗️SlBull" if score>0.1 else ("📉Bear" if score<-0.3 else ("↘️SlBear" if score<-0.1 else "➡️Neutral")))
                self._cache = {"score":round(score,2),"label":label,"ts":now,"headlines":headlines[:5]}
                # v11.2.8: success → reset failure tracking
                self._fail_count = 0
                self._first_fail_ts = 0
                log.info(f"📰 News: {label} ({score:+.2f}) | {len(items)} articles")
                return score, label, headlines[:5]
        except Exception as e:
            # v11.2.4 FIX (May 3, 2026): update cache ts on failure to prevent rate-limit spam.
            # v11.2.8 FIX (May 4, 2026): degrade stale signal toward neutral after 30+ minutes
            # of consecutive failures. Was: kept returning the last known sentiment forever
            # if RSS feed went down — biased trade decisions on hours-stale data.
            self._cache["ts"] = now
            self._fail_count += 1
            if self._first_fail_ts == 0:
                self._first_fail_ts = now
                # v11.2.19 FIX: snapshot original score at outage start — never decay from
                # already-decayed value (was double-exponential collapse, zeroed in ~60min)
                self._orig_score = self._cache["score"]
            outage_min = (now - self._first_fail_ts) / 60
            if outage_min > 30:
                # Decay from original score — smooth halving every 30min of continued outage
                decay_factor = 0.5 ** ((outage_min - 30) / 30)
                decayed = round(self._orig_score * decay_factor, 2)  # always from original
                self._cache["score"] = 0.0 if abs(decayed) < 0.05 else decayed
                if abs(self._cache["score"]) < 0.05:
                    self._cache["label"] = "➡️Stale"
                if self._fail_count % 12 == 1:  # log roughly every hour
                    log.warning(f"📰 News outage {outage_min:.0f}min — decaying score to {self._cache['score']:+.2f}")
            return self._cache["score"], self._cache["label"], self._cache["headlines"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STATE PERSISTENCE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

