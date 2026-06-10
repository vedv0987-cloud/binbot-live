"""BinBot v18.5 — feature_health.py

Startup FEATURE-HEALTH table. The v18.5 audit found a large set of "advertised but
inert" subsystems (modules set to None then called inside silent try/except: pass).
This module prints a per-module ACTIVE / INACTIVE / STUB status at boot so an operator
can SEE — at a glance — exactly which intelligence and safety features are actually
running. Pure inspection: no side effects, never raises into the boot path.
"""
import logging

log = logging.getLogger("binbot")

# Class names that mean "intentional no-op placeholder", not a real module.
_STUB_CLASSES = {"_NullBlocker", "_DeadModule", "_NullModule"}


def _state(obj):
    """Render a module's liveness from the object itself."""
    if obj is None:
        return "INACTIVE", "❌ INACTIVE"
    if type(obj).__name__ in _STUB_CLASSES:
        return "STUB", "⚪ STUB (no-op)"
    return "ACTIVE", "✅ ACTIVE"


def _flag_state(enabled):
    return ("ACTIVE", "✅ ON") if enabled else ("INACTIVE", "➖ OFF")


def report(bot, cfg):
    """Log the feature-health table. `bot` is the ProBotV11 instance, `cfg` its Config."""
    try:
        g = lambda name: getattr(bot, name, None)

        # (label, kind, value)  — kind: "obj" inspects an instance, "flag" inspects a bool
        rows = [
            ("ML ensemble (MLPredictor)",     "obj",  g("ml")),
            ("Additive intel scoring",        "flag", getattr(cfg, "INTEL_SCORING_ENABLED", False)),
            ("Adaptive R:R",                  "flag", getattr(cfg, "ADAPTIVE_RR_ENABLED", False)),
            ("FOMC/CPI/NFP calendar block",   "obj",  g("econ_calendar")),
            ("Token-unlock guard",            "obj",  g("token_unlock")),
            ("Micro-price engine",            "obj",  g("micro_price")),
            ("Funding-rate tracker",          "obj",  g("funding_rate")),
            ("Liquidation detector",          "obj",  g("liquidation")),
            ("Liquidation-cascade",           "obj",  g("liq_cascade")),
            ("LOB imbalance",                 "obj",  g("lob")),
            ("VPIN toxic-flow",               "obj",  g("vpin")),
            ("Volume-delta",                  "obj",  g("vol_delta")),
            ("Aggressor-flow",                "obj",  g("aggressor_flow")),
            ("Smart-coin detector",           "obj",  g("smart_coin")),
            ("CryptoPanic news",              "obj",  g("crypto_news")),
            ("Global news (RSS)",             "obj",  g("news")),
            ("L/S ratio (Binance)",           "obj",  g("long_short")),
            ("Open interest (Binance)",       "obj",  g("open_interest")),
            ("Momentum scanner",              "obj",  g("momentum")),
            ("Coin profiles",                 "obj",  g("coin_profiles")),
            ("Native SL (exchange-side)",     "obj",  g("native_sl") if getattr(cfg, "NATIVE_SL_ENABLED", False) else None),
            ("Native TP (exchange-side)",     "obj",  g("native_tp")),
            ("Position reconciler",           "obj",  g("reconciler")),
            ("Drawdown shield",               "obj",  g("ddshield")),
            ("TriArb scanner (alert-only)",   "obj",  g("tri_arb")),
            ("Capital activator ($500)",      "obj",  g("cap_activator")),
            ("Telegram alerts",               "flag", getattr(cfg, "TG_ENABLED", False)),
            ("Telegram commands",             "obj",  g("_tg_cmd_handler")),
            ("Web dashboard",                 "obj",  g("_dashboard")),
            ("Prometheus :9090",              "obj",  g("_prom")),
            ("Audit log (hash-chain)",        "obj",  g("_audit")),
            ("Dead-man's switch",             "flag", getattr(cfg, "DEADMAN_ENABLED", False)),
            ("Gate telemetry",                "flag", getattr(cfg, "GATE_TELEMETRY_ENABLED", False)),
        ]

        active = stub = inactive = 0
        log.info("━" * 64)
        log.info("  📋 FEATURE HEALTH — v18.5 (what is ACTUALLY running)")
        for label, kind, val in rows:
            if kind == "flag":
                state, disp = _flag_state(bool(val))
            else:
                state, disp = _state(val)
            if state == "ACTIVE":
                active += 1
            elif state == "STUB":
                stub += 1
            else:
                inactive += 1
            log.info(f"     {label:<32} {disp}")

        # v19.0.4: the dead PyTorch/heavy placeholders (lstm, monte_carlo, meta_learner,
        # model_selector, transformer_nlp) and the noisy ones (social_sentiment, exchange_flow,
        # hash_rate) were REMOVED from the code. long_short/open_interest are now real (above).
        # This list only names real-but-optional modules that may legitimately be None.
        placeholder_names = [
            "dxy", "options", "multi_ex", "whale", "rl", "gecko_trending", "gecko_movers",
        ]
        dead = [n for n in placeholder_names if getattr(bot, n, None) is None]
        if dead:
            log.info(f"     {'placeholders (no impl, safe no-op)':<32} ⚪ {len(dead)}: {', '.join(dead)}")
        log.info(f"  📋 {active} ACTIVE · {stub} stub · {inactive} inactive · {len(dead)} placeholders")
        log.info("━" * 64)
        return {"active": active, "stub": stub, "inactive": inactive, "placeholders": len(dead)}
    except Exception as e:
        log.warning(f"feature_health.report failed (non-fatal): {e}")
        return {}
