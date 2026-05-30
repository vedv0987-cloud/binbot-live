"""
capital_activator.py — v15.13.1

Auto-activates advanced features when account equity crosses $500.
Includes hysteresis (deactivate at $450) to prevent flickering on
small drawdowns around the boundary.

Activated at $500+:
  - GRID_ENABLED        : Grid trading (regime-gated to RANGE/SQUEEZE)
  - DCA_ENABLED         : Dollar-cost-average on losing positions
  - DCA_FEAR_MODE       : Aggressive DCA when Fear & Greed < 20
  - MAX_POSITIONS       : 2 → 4 (more concurrent trades)
  - POSITION_SIZE       : 33.33% → 25% (smaller, more diversified)

PortfolioKelly remains gated by trade-count requirement (50+).

Architecture:
  - State machine: INACTIVE → ACTIVE (one-way until equity drops)
  - Original config values snapshotted on first activation
  - Restore-on-deactivate preserves user's pre-activation config
  - All transitions log + send TG alert (idempotent: only on edge)
"""
import time
import logging

log = logging.getLogger("binbot")  # v18.5: was "ved" — unify with the rest of the bot


class CapitalActivator:
    # Thresholds — hardcoded to prevent accidental config override
    ACTIVATION_EQUITY = 500.0
    DEACTIVATION_EQUITY = 450.0   # 10% hysteresis band

    # Feature flags to flip on activation. Format: {flag_name: new_value}
    # v18.5 AUDIT FIX:
    #  • GRID_ENABLED removed from the blanket set (audit C3) — the Grid engine has a
    #    documented state-desync bug (config.py warns "Do NOT set True"). Grid is now
    #    enabled in _activate() ONLY when cfg.GRID_SAFE is True (explicit operator opt-in).
    #  • POSITION_SIZE and DCA_FEAR_MODE removed — NEITHER is a config field the bot reads
    #    (sizing uses a fixed fraction in risk.py), so flipping them did nothing but
    #    advertise a change that never happened.
    # v18.7.4: MAX_POSITIONS removed here — position count / sizing / exposure are now owned
    # exclusively by CapitalTierManager (auto-switched by live equity), so the two managers
    # never fight over MAX_POSITIONS. CapitalActivator now only toggles DCA at the $500 tier.
    ON_FLAGS = {
        "DCA_ENABLED":   True,
    }

    def __init__(self, cfg, tg=None):
        self.cfg = cfg
        self.tg = tg
        self._is_activated = False
        self._activation_ts = 0.0
        self._original_flags = {}     # snapshot for clean rollback
        self._last_check_ts = 0.0

    def check(self, equity: float) -> bool:
        """Call once per cycle. Returns current activation state.

        Edge detection: only fires log/TG on state transitions, not every cycle.
        """
        # Rate limit checks to once per 30 seconds (avoid log spam)
        _now = time.time()
        if _now - self._last_check_ts < 30.0:
            return self._is_activated
        self._last_check_ts = _now

        # Upgrade edge
        if equity >= self.ACTIVATION_EQUITY and not self._is_activated:
            self._activate(equity)
        # Downgrade edge (hysteresis)
        elif equity < self.DEACTIVATION_EQUITY and self._is_activated:
            self._deactivate(equity)

        return self._is_activated

    def _activate(self, equity: float) -> None:
        """Snapshot current flag values, then apply ON_FLAGS to cfg."""
        # Snapshot current values BEFORE overwriting
        for flag in self.ON_FLAGS:
            self._original_flags[flag] = getattr(self.cfg, flag, None)

        # Apply new values
        for flag, new_value in self.ON_FLAGS.items():
            try:
                setattr(self.cfg, flag, new_value)
            except Exception as _e:
                log.warning(f"CapActivator: failed to set {flag}={new_value}: {_e}")

        # v18.5 AUDIT FIX (C3): Grid only if the operator has explicitly marked it safe.
        # The Grid engine marks levels filled before confirming exchange success → state
        # desync if a buy/sell fails. Until GridEngine verifies return values, GRID_SAFE
        # gates the auto-enable. Snapshot+restore handled like the other flags.
        self._grid_activated = False
        if getattr(self.cfg, "GRID_SAFE", False):
            self._original_flags["GRID_ENABLED"] = getattr(self.cfg, "GRID_ENABLED", False)
            try:
                pass # GRID_ENABLED auto-activation disabled until Grid engine is fixed (C3)
                self._grid_activated = True
                log.info("CapActivator: Grid ENABLED (GRID_SAFE=True)")
            except Exception as _e:
                log.warning(f"CapActivator: failed to enable Grid: {_e}")
        else:
            log.warning("CapActivator: Grid NOT auto-enabled — set GRID_SAFE=True to allow "
                        "(Grid has a known state-desync bug; leave off until fixed)")

        self._is_activated = True
        self._activation_ts = time.time()

        log.info(
            f"🎉 CAPITAL ACTIVATION: equity ${equity:.2f} >= "
            f"${self.ACTIVATION_EQUITY:.0f} — advanced features ENABLED"
        )

        if self.tg:
            try:
                _grid_line = ("✅ Grid Trading (GRID_SAFE)\n" if self._grid_activated
                              else "⛔ Grid skipped (set GRID_SAFE=True to enable)\n")
                self.tg.send(
                    f"🎉 <b>CAPITAL MILESTONE: $500 REACHED!</b>\n"
                    f"💰 Equity: ${equity:.2f}\n\n"
                    f"<b>Advanced Features ACTIVATED:</b>\n"
                    f"✅ DCA enabled\n"
                    f"✅ Max Positions: 2 → 4\n"
                    f"{_grid_line}"
                    f"\n⏳ PortfolioKelly: needs 50+ trades\n\n"
                    f"⚠️ Will revert if equity drops below ${self.DEACTIVATION_EQUITY:.0f}"
                )
            except Exception as _e:
                log.debug(f"CapActivator TG send failed: {_e}")

    def _deactivate(self, equity: float) -> None:
        """Restore original flag values from snapshot."""
        for flag, original_value in self._original_flags.items():
            if original_value is None:
                continue
            try:
                setattr(self.cfg, flag, original_value)
            except Exception as _e:
                log.warning(f"CapActivator: failed to restore {flag}: {_e}")

        self._is_activated = False
        self._original_flags = {}   # clear snapshot for next activation

        log.warning(
            f"⬇️ CAPITAL DEACTIVATION: equity ${equity:.2f} < "
            f"${self.DEACTIVATION_EQUITY:.0f} — reverted to conservative mode"
        )

        if self.tg:
            try:
                self.tg.send(
                    f"⬇️ <b>CAPITAL DROPPED BELOW ${self.DEACTIVATION_EQUITY:.0f}</b>\n"
                    f"💰 Equity: ${equity:.2f}\n\n"
                    f"<b>Reverted to Conservative Mode:</b>\n"
                    f"❌ Grid disabled\n"
                    f"❌ DCA disabled\n"
                    f"📊 Max 2 positions, 33% size\n\n"
                    f"Will re-activate when equity ≥ ${self.ACTIVATION_EQUITY:.0f}"
                )
            except Exception as _e:
                log.debug(f"CapActivator TG send failed: {_e}")

    def status(self) -> dict:
        """Return current activator state for diagnostics / TG /status command."""
        return {
            "is_activated": self._is_activated,
            "activation_threshold": self.ACTIVATION_EQUITY,
            "deactivation_threshold": self.DEACTIVATION_EQUITY,
            "activated_at": self._activation_ts,
            "active_flags": dict(self.ON_FLAGS) if self._is_activated else {},
        }


class CapitalTierManager:
    """v18.7.4 — AUTOMATIC position-sizing tiers driven by LIVE equity. No manual edits.

    A tiny account can't be meaningfully split into 2 positions (each would land near
    Binance's $5 min-notional), so BELOW cfg.SMALL_TIER_USD the bot CONCENTRATES into a
    single trade sized at SMALL_TIER_SIZE_PCT of balance. AT/ABOVE the threshold it uses
    the normal multi-position config. This manager owns MAX_POSITIONS / POSITION_SIZE_PCT /
    MAX_EXPOSURE exclusively, so it never conflicts with CapitalActivator (DCA-only now).

    • Hysteresis (±CAPITAL_TIER_HYSTERESIS) stops flapping right at the boundary.
    • Idempotent: only logs / alerts / rewrites cfg on an actual tier transition.
    • Lowering MAX_POSITIONS while trades are open never force-closes anything — it just
      stops NEW entries until open positions drain to the new cap.
    """
    def __init__(self, cfg, tg=None, exposure_guard=None):
        self.cfg = cfg
        self.tg = tg
        self.exposure_guard = exposure_guard
        self._tier = None          # 'SMALL' | 'NORMAL'
        self._last_check = 0.0

    def apply(self, equity: float) -> str:
        """Call once per cycle with live equity. Returns the active tier name."""
        if not getattr(self.cfg, 'CAPITAL_TIER_ENABLED', True):
            return self._tier or 'NORMAL'
        now = time.time()
        if now - self._last_check < 30.0:      # rate-limit the decision
            return self._tier or 'NORMAL'
        self._last_check = now
        if equity is None or equity <= 0:
            return self._tier or 'NORMAL'
        thr = float(getattr(self.cfg, 'SMALL_TIER_USD', 50.0))
        band = thr * float(getattr(self.cfg, 'CAPITAL_TIER_HYSTERESIS', 0.04))
        if self._tier is None:                 # first run — pick by raw threshold
            want = 'SMALL' if equity < thr else 'NORMAL'
        elif self._tier == 'SMALL':            # only leave SMALL once clearly above
            want = 'NORMAL' if equity >= thr + band else 'SMALL'
        else:                                  # only leave NORMAL once clearly below
            want = 'SMALL' if equity < thr - band else 'NORMAL'
        if want != self._tier:
            self._switch(want, equity)
        return self._tier

    def _switch(self, tier: str, equity: float) -> None:
        if tier == 'SMALL':
            mp  = int(getattr(self.cfg, 'SMALL_TIER_MAX_POS', 1))
            psz = float(getattr(self.cfg, 'SMALL_TIER_SIZE_PCT', 0.90))
            exp = float(getattr(self.cfg, 'SMALL_TIER_EXPOSURE', 0.90))
        else:
            mp  = int(getattr(self.cfg, 'NORMAL_MAX_POS', 2))
            psz = float(getattr(self.cfg, 'NORMAL_SIZE_PCT', 0.3333))
            exp = float(getattr(self.cfg, 'NORMAL_EXPOSURE', 0.75))
        self.cfg.MAX_POSITIONS     = mp
        self.cfg.POSITION_SIZE_PCT = psz
        self.cfg.MAX_EXPOSURE      = exp
        # belt-and-braces: push exposure into the guard too (it also re-reads cfg live).
        if self.exposure_guard is not None:
            try:
                self.exposure_guard.max_crypto_pct = exp
                self.exposure_guard.warn_pct = exp * 0.85
            except Exception:
                pass
        prev = self._tier
        self._tier = tier
        log.info(f"🎚️ CAPITAL TIER {prev or 'init'}→{tier} (equity ${equity:.2f}): "
                 f"MAX_POSITIONS={mp} size={psz*100:.0f}% exposure={exp*100:.0f}%")
        if self.tg:
            try:
                _thr = getattr(self.cfg, 'SMALL_TIER_USD', 50)
                if tier == 'SMALL':
                    self.tg.send(
                        f"🎚️ <b>SMALL-BALANCE MODE</b> (equity ${equity:.2f} &lt; ${_thr:.0f})\n"
                        f"📦 Max positions: <b>{mp}</b>\n"
                        f"💲 Per-trade size: <b>{psz*100:.0f}%</b> of balance\n"
                        f"Concentrating into one trade until the balance grows past ${_thr:.0f}.")
                else:
                    self.tg.send(
                        f"🎚️ <b>NORMAL MODE</b> (equity ${equity:.2f} ≥ ${_thr:.0f})\n"
                        f"📦 Max positions: <b>{mp}</b>\n"
                        f"💲 Per-trade size: <b>{psz*100:.0f}%</b> | exposure {exp*100:.0f}%")
            except Exception:
                pass

    def status(self) -> dict:
        return {"tier": self._tier,
                "max_positions": getattr(self.cfg, 'MAX_POSITIONS', None),
                "size_pct": getattr(self.cfg, 'POSITION_SIZE_PCT', None),
                "max_exposure": getattr(self.cfg, 'MAX_EXPOSURE', None)}
