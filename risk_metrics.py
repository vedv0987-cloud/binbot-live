"""BinBot v15.1 — Advanced Risk Metrics

Standard institutional risk-adjusted return metrics. Drop-in replacement for
the limited Sharpe-only analytics in analytics.py.

All metrics computed from a return series (list of per-trade PnL percentages).

Metrics:
  - sharpe          : (mean_return / std_return) × √trades_per_year
  - sortino         : (mean_return / downside_std) × √trades_per_year
  - calmar          : annualized_return / max_drawdown
  - mar             : same as calmar but monthly basis
  - ulcer_index     : RMS of drawdown depths (penalizes both depth + duration)
  - tail_ratio      : 95th-percentile gain / |5th-percentile loss|
  - common_sense    : profit_factor × tail_ratio (cmpd robustness measure)
  - skewness        : 3rd moment — positive = right tail, negative = left tail
  - kurtosis        : 4th moment — high = fat tails
  - cvar_95         : v15.1 — expected loss in worst 5% of trades
  - cvar_99         : v15.1 — expected loss in worst 1% of trades
  - max_consec_loss : v15.1 — longest losing streak
  - recovery_factor : v15.1 — total return / max drawdown
"""
from __future__ import annotations
import math
import time
from typing import List, Tuple


def _std(xs: List[float], ddof: int = 0) -> float:
    if len(xs) <= ddof:
        return 0.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - ddof)
    return math.sqrt(var)


def _percentile(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _trades_per_year(timestamps: List[float], default: int = 252) -> float:
    """Annualize based on actual frequency."""
    if len(timestamps) < 2:
        return float(default)
    span_days = (timestamps[-1] - timestamps[0]) / 86400
    if span_days <= 0:
        return float(default)
    return len(timestamps) / span_days * 365


def sharpe(returns: List[float], timestamps: List[float] = None) -> float:
    if len(returns) < 10:
        return 0.0
    m = sum(returns) / len(returns)
    s = _std(returns)
    if s <= 0:
        return 0.0
    tpy = _trades_per_year(timestamps) if timestamps else 252
    return round(m / s * math.sqrt(tpy), 3)


def sortino(returns: List[float], timestamps: List[float] = None,
            target: float = 0.0) -> float:
    """Sortino — like Sharpe but only penalizes downside variance.
    More accurate measure of pain-adjusted return."""
    if len(returns) < 10:
        return 0.0
    m = sum(returns) / len(returns)
    downside = [r - target for r in returns if r < target]
    if not downside:
        return 999.0  # no losses ever — undefined but practically infinite
    d_std = math.sqrt(sum(x ** 2 for x in downside) / len(returns))
    if d_std <= 0:
        return 0.0
    tpy = _trades_per_year(timestamps) if timestamps else 252
    return round((m - target) / d_std * math.sqrt(tpy), 3)


def max_drawdown(returns: List[float]) -> Tuple[float, int]:
    """Returns (max_dd_pct, duration_in_trades)."""
    if not returns:
        return 0.0, 0
    equity = [0.0]
    for r in returns:
        equity.append(equity[-1] + r)
    peak = equity[0]
    max_dd = 0.0
    peak_idx = 0
    max_dd_duration = 0
    for i, e in enumerate(equity):
        if e > peak:
            peak = e
            peak_idx = i
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
            max_dd_duration = i - peak_idx
    return round(max_dd, 4), max_dd_duration


def calmar(returns: List[float], timestamps: List[float] = None) -> float:
    """Calmar — annualized return / max drawdown. Higher = better."""
    if len(returns) < 10:
        return 0.0
    total_return = sum(returns)
    tpy = _trades_per_year(timestamps) if timestamps else 252
    span_years = len(returns) / tpy if tpy > 0 else 1
    annual_return = total_return / span_years if span_years > 0 else 0
    max_dd, _ = max_drawdown(returns)
    if max_dd <= 0:
        return 999.0
    return round(annual_return / max_dd, 3)


def mar(returns: List[float], timestamps: List[float] = None) -> float:
    """MAR ratio — Calmar variant, more common monthly basis."""
    return calmar(returns, timestamps)


def ulcer_index(returns: List[float]) -> float:
    """Ulcer Index — RMS of drawdown depths across the equity curve.
    Penalizes both depth AND duration of drawdowns. Lower = smoother."""
    if not returns:
        return 0.0
    equity = [0.0]
    for r in returns:
        equity.append(equity[-1] + r)
    peak = equity[0]
    sq_dds = []
    for e in equity:
        if e > peak:
            peak = e
        dd_pct = ((peak - e) / abs(peak) * 100) if peak != 0 else (peak - e)
        sq_dds.append(dd_pct ** 2)
    return round(math.sqrt(sum(sq_dds) / len(sq_dds)), 3)


def tail_ratio(returns: List[float]) -> float:
    """Tail Ratio — P95 gain / |P5 loss|. > 1 means right tail dominates."""
    if len(returns) < 20:
        return 0.0
    p95 = _percentile(returns, 95)
    p5 = abs(_percentile(returns, 5))
    if p5 <= 0:
        return 999.0
    return round(p95 / p5, 3)


def profit_factor(returns: List[float]) -> float:
    wins = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    if losses <= 0:
        return 999.0
    return round(wins / losses, 3)


def common_sense_ratio(returns: List[float]) -> float:
    """Profit Factor × Tail Ratio. >1.5 = robust strategy."""
    pf = profit_factor(returns)
    tr = tail_ratio(returns)
    if pf >= 999 or tr >= 999:
        return 999.0
    return round(pf * tr, 3)


def skewness(returns: List[float]) -> float:
    """3rd standardized moment. Positive = more big wins than big losses."""
    n = len(returns)
    if n < 10:
        return 0.0
    m = sum(returns) / n
    s = _std(returns)
    if s <= 0:
        return 0.0
    return round(sum(((r - m) / s) ** 3 for r in returns) / n, 3)


def kurtosis(returns: List[float]) -> float:
    """4th moment, excess kurtosis (normal = 0). High = fat tails / black swans."""
    n = len(returns)
    if n < 10:
        return 0.0
    m = sum(returns) / n
    s = _std(returns)
    if s <= 0:
        return 0.0
    k4 = sum(((r - m) / s) ** 4 for r in returns) / n
    return round(k4 - 3, 3)


def cvar_95(returns: List[float]) -> float:
    """v15.1: Conditional Value at Risk at 95% confidence.
    Average loss in the worst 5% of trades. More informative than VaR
    because it measures the EXPECTED loss in tail scenarios."""
    if not returns or len(returns) < 20: return 0.0
    sorted_r = sorted(returns)
    cutoff = max(1, int(len(sorted_r) * 0.05))
    tail = sorted_r[:cutoff]
    return round(sum(tail) / len(tail), 4) if tail else 0.0

def cvar_99(returns: List[float]) -> float:
    """v15.1: CVaR at 99% — extreme tail risk."""
    if not returns or len(returns) < 100: return 0.0
    sorted_r = sorted(returns)
    cutoff = max(1, int(len(sorted_r) * 0.01))
    tail = sorted_r[:cutoff]
    return round(sum(tail) / len(tail), 4) if tail else 0.0

def max_consecutive_losses(returns: List[float]) -> int:
    """v15.1: Longest losing streak. Important for psychological resilience."""
    if not returns: return 0
    max_streak = current = 0
    for r in returns:
        if r < 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak

def recovery_factor(returns: List[float]) -> float:
    """v15.1: Total return / max drawdown. Higher = faster recovery."""
    if not returns: return 0.0
    total_return = sum(returns)
    peak = 0.0; max_dd = 0.0; running = 0.0
    for r in returns:
        running += r
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
    return round(total_return / max_dd, 2) if max_dd > 0 else 999.0


def full_report(returns: List[float], timestamps: List[float] = None) -> dict:
    """All metrics in one shot. Use for periodic reporting."""
    max_dd, dd_dur = max_drawdown(returns)
    return {
        "n_trades": len(returns),
        "total_return": round(sum(returns), 4),
        "mean_return": round(sum(returns) / max(len(returns), 1), 4),
        "sharpe": sharpe(returns, timestamps),
        "sortino": sortino(returns, timestamps),
        "calmar": calmar(returns, timestamps),
        "max_drawdown": max_dd,
        "max_dd_duration": dd_dur,
        "ulcer_index": ulcer_index(returns),
        "tail_ratio": tail_ratio(returns),
        "profit_factor": profit_factor(returns),
        "common_sense_ratio": common_sense_ratio(returns),
        "skewness": skewness(returns),
        "kurtosis": kurtosis(returns),
        "cvar_95": cvar_95(returns),
        "cvar_99": cvar_99(returns),
        "max_consec_losses": max_consecutive_losses(returns),
        "recovery_factor": recovery_factor(returns),
    }


def print_report(returns: List[float], timestamps: List[float] = None):
    """Pretty-print full risk metrics report."""
    r = full_report(returns, timestamps)
    print("\n" + "=" * 60)
    print("  RISK METRICS")
    print("=" * 60)
    for k, v in r.items():
        print(f"  {k:<22} {v}")
    print("=" * 60 + "\n")

