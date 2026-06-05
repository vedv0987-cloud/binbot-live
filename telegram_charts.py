"""BinBot v15.4 — Chart screenshot generator for Telegram alerts.

Renders a candle chart with entry/SL/TP horizontal lines overlaid, returns
raw PNG bytes ready for Telegram sendPhoto.

DESIGN:
- Lazy-imports matplotlib (heavy: ~100MB resident) only when first chart is rendered.
- Uses Agg backend (no display required, server-safe).
- Renders to in-memory PNG (no temp files, no disk I/O).
- Auto-fits Y-axis with 2% padding above/below entry/SL/TP markers.
- Catches all matplotlib errors — never crashes the bot.

USAGE:
    from telegram_charts import render_trade_chart
    png_bytes = render_trade_chart(candles, entry, sl, tp, pair="BTCUSDT", strategy="WYCKOFF_ACC")
    if png_bytes:
        tg.send_photo(png_bytes, caption="...")
"""
from __future__ import annotations
import io, logging
from typing import List, Optional

log = logging.getLogger("binbot")

_MPL_READY = False
_plt = None
_mpdates = None
_mpath = None
_mpatches = None


def _ensure_mpl():
    """Lazy-init matplotlib with Agg backend. Returns True if usable."""
    global _MPL_READY, _plt, _mpdates, _mpath, _mpatches
    if _MPL_READY:
        return True
    try:
        import matplotlib
        matplotlib.use("Agg")  # server-safe, no display
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib.patches as mpatches
        from matplotlib.path import Path
        _plt = plt
        _mpdates = mdates
        _mpath = Path
        _mpatches = mpatches
        _MPL_READY = True
        return True
    except Exception as e:
        log.warning(f"matplotlib unavailable, charts disabled: {e}")
        return False


def render_trade_chart(candles, entry: float, sl: float, tp: float,
                        pair: str = "", strategy: str = "",
                        action: str = "BUY",
                        exit_price: Optional[float] = None) -> Optional[bytes]:
    """Render an OHLC candle chart with entry/SL/TP overlay.

    Args:
        candles: list of Candle objects with .ts/.o/.h/.l/.c attributes
        entry: entry price (horizontal blue line)
        sl: stop-loss price (horizontal red line)
        tp: take-profit price (horizontal green line)
        pair: ticker symbol (e.g. "BTCUSDT")
        strategy: strategy name (shown in title)
        action: "BUY", "TP", "SL", "TRAIL", etc.
        exit_price: if provided, overlays a dashed line at exit (used on SELL alerts)

    Returns:
        Raw PNG bytes, or None if rendering failed.
    """
    if not _ensure_mpl():
        return None
    try:
        from datetime import datetime, timezone
        # Take the last 60 candles for the chart window
        window = list(candles)[-60:]
        if len(window) < 5:
            return None

        # Build x (datetimes) and OHLC arrays
        xs = [datetime.fromtimestamp(c.ts, tz=timezone.utc) for c in window]
        opens   = [c.o for c in window]
        highs   = [c.h for c in window]
        lows    = [c.l for c in window]
        closes  = [c.c for c in window]

        fig, ax = _plt.subplots(figsize=(8, 4.5), dpi=110)
        fig.patch.set_facecolor('#1a1a1a')
        ax.set_facecolor('#1a1a1a')

        # Plot candles
        for i, (x, o, h, l, c) in enumerate(zip(xs, opens, highs, lows, closes)):
            color = '#26a69a' if c >= o else '#ef5350'
            # Wick
            ax.plot([x, x], [l, h], color=color, linewidth=0.8, zorder=1)
            # Body — use rectangle for proper width
            body_low = min(o, c)
            body_height = max(o, c) - body_low
            if body_height == 0:
                body_height = (h - l) * 0.05  # doji safeguard
            # width in days for x-axis units
            width_days = 0.6 / (24 * 60)  # ~36 sec wide if 1m candles
            if len(xs) > 1:
                dx = (xs[1] - xs[0]).total_seconds() / 86400 * 0.7
                width_days = dx
            rect = _mpatches.Rectangle((_mpdates.date2num(x) - width_days/2, body_low),
                                       width_days, body_height,
                                       facecolor=color, edgecolor=color, zorder=2)
            ax.add_patch(rect)

        # Overlay entry / SL / TP horizontal lines
        ax.axhline(entry, color='#2196F3', linestyle='-',  linewidth=1.5,
                   label=f'Entry ${entry:.4f}', zorder=5)
        ax.axhline(sl, color='#ef5350', linestyle='--', linewidth=1.2,
                   label=f'SL ${sl:.4f}', zorder=5)
        ax.axhline(tp, color='#26a69a', linestyle='--', linewidth=1.2,
                   label=f'TP ${tp:.4f}', zorder=5)
        if exit_price is not None:
            ax.axhline(exit_price, color='#FFC107', linestyle=':', linewidth=1.5,
                       label=f'Exit ${exit_price:.4f}', zorder=6)

        # Y-axis auto-range with 1.5% padding around entry/SL/TP/exit
        all_levels = [entry, sl, tp] + ([exit_price] if exit_price else [])
        chart_min = min(min(lows), min(all_levels))
        chart_max = max(max(highs), max(all_levels))
        pad = (chart_max - chart_min) * 0.05
        ax.set_ylim(chart_min - pad, chart_max + pad)

        # Format x-axis
        ax.xaxis.set_major_formatter(_mpdates.DateFormatter('%H:%M', tz=timezone.utc))
        ax.tick_params(axis='x', colors='#bbb', labelsize=8)
        ax.tick_params(axis='y', colors='#bbb', labelsize=8)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_color('#444')
        ax.spines['bottom'].set_color('#444')
        ax.spines['left'].set_color('#444')
        ax.grid(True, alpha=0.15, linestyle='-', color='#666')

        # Title
        title_color = '#26a69a' if action == 'BUY' else ('#26a69a' if exit_price and exit_price > entry else '#ef5350')
        title = f"{pair}  {action}  {strategy}"
        ax.set_title(title, color=title_color, fontsize=11, fontweight='bold', pad=8)

        # Legend
        leg = ax.legend(loc='upper left', framealpha=0.85, facecolor='#2a2a2a',
                        edgecolor='#444', labelcolor='#ddd', fontsize=8)

        _plt.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=110, facecolor='#1a1a1a',
                    bbox_inches='tight', pad_inches=0.15)
        _plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"render_trade_chart failed: {e}")
        try:
            _plt.close('all')  # cleanup on error
        except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
        return None
