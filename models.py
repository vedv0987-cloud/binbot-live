# BinBot v11 — models.py
from dataclasses import dataclass, field, asdict
from typing import Optional, List

__all__ = ["Candle", "Context", "Position", "Signal", "PendingBuy"]  # v14.5.1 FIX (audit #23)

@dataclass
class Candle:
    ts:float; o:float; h:float; l:float; c:float; v:float

@dataclass
class Context:
    daily:str; h4:str; h1:str; btc_ok:bool
    fg:int; fg_label:str; vol:str; session:str
    active:bool; mode:str; adx:float
    news_score:float; news_label:str
    regime:str; killzone:str  # v7
    heat:float; squeeze:bool  # v7
    mtf_align:float=50.0  # v13.2: Multi-TF alignment score 0-100

@dataclass
class Position:
    pair:str; entry:float; qty:float; size:float
    entry_time:str; sl:float; tp:float; group:str
    high:float; strategy:str; atr:float
    trailing_on:bool=False; trail_stop:float=0.0
    safety_used:int=0; avg_entry:float=0.0
    total_qty:float=0.0; total_cost:float=0.0
    scale_done:list=field(default_factory=list)
    rr:float=0.0; grade:str=""; entry_fee:float=0.0; context:str=""; be_locked:bool=False
    tp_floor_locked:bool=False  # v14.7: True once TP touched and SL pinned at TP (chase mode active)
    pyramids:int=0  # v7: anti-martingale adds
    # v13.5.3 audit Bug #25: declare _sell_fails as a real field instead of
    # dynamic attribute. Was: bot.py:899 set pos._sell_fails dynamically and
    # asdict() (used by state.py:save) excludes non-declared attrs → strike
    # count was silently lost on every restart. A position that hit partial-
    # fill twice, restarted, hit it once more would NEVER reach the 3-strike
    # force-close path → stuck position leaks indefinitely. Now persists.
    sell_fails:int=0
    # v13.6 NEW: Native SL order ID. When NATIVE_SL_ENABLED=True, after every BUY
    # the bot places STOP_LOSS_LIMIT on Binance and stores its orderId here.
    # Used by: (a) native_sl.detach/move to cancel before SELL/TP exit,
    # (b) bot.py v8.3 sync guard to skip cancelling our own SL orders.
    # Without this field declared, asdict() drops it on save → field wiped every restart.
    native_sl_order_id: Optional[int] = None
    # v14.4: Native TP order ID — LIMIT SELL placed at pos.tp when trailing activates.
    # If bot dies during trailing, exchange fills this automatically at TP price.
    native_tp_order_id: Optional[int] = None
    # v18.5 AUDIT FIX (D5): daily trend captured at entry. The TIME_EXIT-in-BEAR
    # logic in risk.check_exits read pos.regime_at_entry, which was never a declared
    # field (always missing → fell back to an unset _last_ctx → _is_bear always False
    # → the BEAR stagnation exit could NEVER fire). Declaring it + setting it in
    # open_pos makes that safety exit work and survive restarts (asdict persists it).
    regime_at_entry: str = ""
    # Same issue with the ML-confidence-tracker BE/3pct/5pct lock attrs. We
    # already declared be_locked above. The _lock_3pct and _lock_5pct attrs
    # in risk.py are dead writes (audit Bug #25) — never read, can stay as
    # dynamic. If they're later read, declare them too.

    def to_dict(self): return asdict(self)
    @classmethod
    def from_dict(cls, d):
        valid = {k:v for k,v in d.items() if k in cls.__dataclass_fields__}
        # v13.5.3: legacy state files that pre-date sell_fails will hydrate
        # with default 0 because @dataclass defaults handle missing keys.
        return cls(**valid)

@dataclass
class Signal:
    pair:str; price:float; strategy:str; conf:float
    grade:str; reason:str; group:str; tier:int
    tp:float; sl:float; rr:float; atr:float
    filter_stage:str="PASS" 

@dataclass
class PendingBuy:
    pair:str; signal:Signal; lowest:float; created:float; triggered:bool=False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TELEGRAM
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

