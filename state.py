import threading
# BinBot v11 — state.py
import os, json, logging
from datetime import datetime, timezone
from models import Position
log = logging.getLogger('binbot')

# v18.5 AUDIT FIX (H4): a single process-wide lock that ACTUALLY serializes concurrent
# saves. The previous code flock()'d a per-call unique temp file (state.json.tmp.<pid>_
# <thread_id>), so two threads each locked a DIFFERENT file → no mutual exclusion at all,
# just last-writer-wins on os.replace. The bot is single-process (guarded by the exec
# lock), so an RLock is the correct, portable primitive here.
_SAVE_LOCK = threading.RLock()
try:
    import fcntl as _fcntl
    _has_fcntl = True
except ImportError:
    _fcntl = None
    _has_fcntl = False

class StateManager:
    _lock = __import__("threading").Lock()
    def __init__(self, fp): self.path = fp

    def save(self, positions, pnl, daily_pnl, daily_t, wins, losses, fees, grid_pnl=None, grid_trades=None, hyperopt_params=None, last_close=None, last_result=None, pair_losses_today=None, last_reset="", closs=0, pause_until=None, btc_24h_high=0, peak_equity=0, dd_peak=0, total_capital=None):
        # v18.8.5 FIX: removed `existing = self.load()` here — its result was never
        # used (the grid/capital back-fill below does its own json.load), yet it ran a
        # full positions-deserialize, rewrote the .bak, AND logged "📂 Loaded state" at
        # INFO on EVERY save. That made journalctl look like the bot restarted each
        # cycle (impossible to tell a real restart from a routine save) and doubled
        # state-file I/O. The .bak backup is still refreshed by load() on startup.
        # v9.7.2 FIX: when grid stats not passed, read existing from disk to preserve them.
        # Previously, save_state() with no args wrote grid_pnl=0, wiping accumulated grid PnL.
        # v18.5 AUDIT FIX (H1): also back-fill total_capital from disk when the caller
        # omits it, so a partial save() (the common case — most call sites don't pass it)
        # no longer DROPS the persisted capital. Same preservation pattern already used
        # for grid_pnl/grid_trades.
        if grid_pnl is None or grid_trades is None or total_capital is None:
            try:
                if os.path.exists(self.path):
                    with open(self.path) as _f:
                        _existing = json.load(_f)
                    if grid_pnl is None: grid_pnl = _existing.get("grid_pnl", 0)
                    if grid_trades is None: grid_trades = _existing.get("grid_trades", 0)
                    if total_capital is None: total_capital = _existing.get("total_capital")
            except Exception as _e: log.debug(f"Suppressed [state.py]: {_e}")
        if grid_pnl is None: grid_pnl = 0
        if grid_trades is None: grid_trades = 0
        state = {"saved_at":datetime.now(timezone.utc).isoformat(),
                 "positions":[p.to_dict() for p in positions],
                 "pnl":pnl,"daily_pnl":daily_pnl,"daily_trades":daily_t,
                 "wins":wins,"losses":losses,"fees":fees,
                 "grid_pnl":grid_pnl,"grid_trades":grid_trades}
        if hyperopt_params: state["hyperopt"] = hyperopt_params
        if total_capital is not None: state["total_capital"] = total_capital  # v11.2.20 FIX: atomic capital persist
        # v9.5 FIX: persist cooldown memory so restarts don't wipe re-entry blocks
        # v11.2.8 FIX (May 4, 2026): always persist these dicts (even empty) so that
        # clearing them at runtime propagates to disk. Was: `if last_close: ...` guard
        # meant emptying a dict at runtime (rare but possible) wouldn't update state,
        # leaving stale entries on disk. Now: write whatever the caller passed.
        if last_close is not None:
            state["last_close"] = {k: v.isoformat() for k, v in last_close.items()}
        if last_result is not None:
            state["last_result"] = last_result
        if pair_losses_today is not None:
            state["pair_losses_today"] = pair_losses_today
        # v11.2.1 FIX: persist daily-reset gate, consec-loss streak, and active pause timer
        # Without these, a restart wipes daily_pnl/daily_t (because last_reset="" forces _reset()),
        # clears the consecutive-loss counter (bypassing 3-loss pause), and cancels any active
        # pause window. All three are required for kill-switch persistence.
        # v11.2.8: same empty-guard fix applied
        if last_reset: state["last_reset"] = last_reset
        state["closs"] = int(closs)  # always persist (0 is meaningful — "no consec losses")
        if pause_until: state["pause_until"] = pause_until.isoformat() if hasattr(pause_until, 'isoformat') else pause_until
        # v11.2.3 FIX (May 3, 2026): persist BTC 24h-high for crash-protection across restarts.
        # Without this, restart mid-crash sets _btc_24h_high to current (already-dumped) price,
        # blinding the bot to the 5% drop trigger that started before reboot.
        if btc_24h_high and btc_24h_high > 0: state["btc_24h_high"] = btc_24h_high
        # v11.2.7 FIX (May 3, 2026): persist peak-equity DD anchor (audit #5, same class as #14).
        # Without this, every boot reset peak to TOTAL_CAPITAL, so a restart mid-drawdown
        # silently wiped the breaker reference and let the bot trade through actual drawdowns.
        if peak_equity and peak_equity > 0: state["peak_equity"] = peak_equity
        # v11.2.8 FIX (May 4, 2026): persist DrawdownShield.peak — third instance of the
        # same bug class (#14, #23). Audit #5 missed this sibling code path. Without
        # persistence, the gradual de-risking ladder (FULL/CAUTION/DEFENSIVE/SURVIVAL)
        # silently reset on every restart, even though the primary 10% breaker was fixed.
        if dd_peak and dd_peak > 0: state["dd_peak"] = dd_peak
        try:
            # v9.1: Atomic save — write to temp, then rename (prevents corruption)
            # v18.5 AUDIT FIX (H4): serialize the whole write+replace under a real
            # process-wide lock (the old per-temp-file flock was a no-op for threads),
            # and fsync before replace so a power loss can't leave a zero-length file.
            tmp = self.path + f".tmp.{os.getpid()}_{id(threading.current_thread())}"
            with _SAVE_LOCK:
                with open(tmp, 'w') as f:
                    json.dump(state, f, indent=2)
                    f.flush()
                    try:
                        os.fsync(f.fileno())  # durability — flush kernel buffers to disk
                    except Exception:
                        pass  # fsync unsupported on some FS/platforms — best effort
                os.replace(tmp, self.path)  # Atomic on Linux + Windows
        except Exception as e:
            log.error(f"State save: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)  # v18.5: don't leak temp files on failure
            except Exception:
                pass

    def load(self):
        if not os.path.exists(self.path): return None
        try:
            with open(self.path,'r') as f: state = json.load(f)
        except Exception:
            # v9.1: Try backup if main file corrupted
            bak = self.path + ".bak"
            if os.path.exists(bak):
                log.warning("⚠️ State corrupted, loading backup")
                try:
                    with open(bak,'r') as f: state = json.load(f)
                except Exception: return None
            else: return None
        try:
            positions = []
            for pd in state.get("positions",[]):
                try: positions.append(Position.from_dict(pd))
                except Exception as _e: log.debug(f"Suppressed [state.py]: {_e}")
            log.info(f"📂 Loaded state: {len(positions)} pos, PnL: ${state.get('pnl',0):+.4f}")
            # v9.1: Save backup after successful load
            # v11.2.8 FIX (May 4, 2026): atomic .bak write. Was: direct write of .bak —
            # if process crashed mid-write, BOTH main file (just successfully written
            # via os.replace) AND .bak got corrupted → next load returns None → bot
            # starts with empty state, losing peak_equity, daily counters, cooldowns.
            try:
                bak_tmp = self.path + ".bak.tmp"
                with open(bak_tmp, 'w') as f: json.dump(state, f)
                os.replace(bak_tmp, self.path + ".bak")
            except Exception as _e: log.debug(f"Suppressed [state.py]: {_e}")
            # v9.5 FIX: restore cooldown memory across restarts
            last_close_raw = state.get("last_close", {})
            last_close = {}
            for k, v in last_close_raw.items():
                try: last_close[k] = datetime.fromisoformat(v)
                except Exception as _e: log.debug(f"Suppressed [state.py]: {_e}")
            return {"positions":positions,"total_capital":state.get("total_capital",None),"pnl":state.get("pnl",0),"daily_pnl":state.get("daily_pnl",0),  # v11.2.21 FIX: total_capital was saved but never loaded
                    "daily_trades":state.get("daily_trades",0),"wins":state.get("wins",0),
                    "losses":state.get("losses",0),"fees":state.get("fees",0),
                    "grid_pnl":state.get("grid_pnl",0),"grid_trades":state.get("grid_trades",0),
                    "hyperopt":state.get("hyperopt",None),
                    "last_close":last_close,
                    "last_result":state.get("last_result", {}),
                    "pair_losses_today":state.get("pair_losses_today", {}),
                    # v11.2.1 FIX: restore daily-reset gate + loss streak + active pause
                    "last_reset":state.get("last_reset", ""),
                    "closs":state.get("closs", 0),
                    "pause_until":state.get("pause_until", None),
                    # v11.2.3 FIX: restore BTC 24h-high so crash-protection survives restart
                    "btc_24h_high":state.get("btc_24h_high", 0),
                    # v11.2.7 FIX: restore peak-equity DD anchor so circuit breaker survives restart
                    "peak_equity":state.get("peak_equity", 0),
                    # v11.2.8 FIX: restore DrawdownShield.peak so de-risking ladder survives restart
                    "dd_peak":state.get("dd_peak", 0)}
        except Exception: return None



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v7.2 AI INTELLIGENCE LAYERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

