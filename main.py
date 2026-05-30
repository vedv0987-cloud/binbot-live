import gc

import logging

class _WSDedupFilter(logging.Filter):
    """Suppress duplicate WebSocket disconnect spam from python-binance."""
    def __init__(self):
        super().__init__()
        self._last = {}
    def filter(self, record):
        msg = record.getMessage()
        if 'Read loop has been closed' in msg or 'Error receiving message' in msg:
            import time
            now = time.time()
            if now - self._last.get('ws_err', 0) < 60:
                return False
            self._last['ws_err'] = now
            record.msg = 'WebSocket disconnected — self-healer reconnecting (50 streams)'
            record.args = ()
        return True

logging.getLogger().addFilter(_WSDedupFilter())
logging.getLogger('binance').addFilter(_WSDedupFilter())
logging.getLogger('websockets').addFilter(_WSDedupFilter())

# v15.4 FIX (P3-2): gc.disable() removed — Python GC pauses are µs,
# network latency is 10,000× longer. OOM risk far outweighs GC pause.

#!/usr/bin/env python3
"""BinBot v15.2 - main.py | Live Trading Entry Point (Async).

Usage:
  python main.py                  # start live trading with cfg.TOTAL_CAPITAL
  python main.py --capital 500    # override starting capital

v15.2 (May 24, 2026): Full async rewrite — aiohttp + asyncio.
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")

import sys, os, asyncio
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass  # uvloop doesn't work natively on Windows, so we fallback to asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from bot import ProBotV11

def _parse_capital_arg(argv):
    """v13.5.3 audit Bug #5: was raw argv.index + idx+1 lookup with no guard."""
    if "--capital" not in argv:
        return None
    try:
        idx = argv.index("--capital")
    except ValueError:
        return None
    if idx + 1 >= len(argv):
        sys.stderr.write("❌ --capital requires a numeric value (e.g. --capital 500)\n")
        sys.exit(2)
    try:
        cap = float(argv[idx + 1])
    except ValueError:
        sys.stderr.write(f"❌ --capital expects a number, got '{argv[idx + 1]}'\n")
        sys.exit(2)
    if cap <= 0:
        sys.stderr.write(f"❌ --capital must be positive, got {cap}\n")
        sys.exit(2)
    return cap

async def async_main():
    """v15.2: Async entry point."""
    cfg = Config()
    cfg.USE_TESTNET = False  # production
    cap_override = _parse_capital_arg(sys.argv)
    if cap_override is not None:
        cfg.TOTAL_CAPITAL = cap_override
    bot = ProBotV11(cfg)
    await bot.run()

def main():
    """Sync wrapper for backward compat — calls asyncio.run()."""
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
