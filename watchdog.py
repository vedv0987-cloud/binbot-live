#!/usr/bin/env python3
"""BinBot v18.9.2 — EXTERNAL WATCHDOG (runs OUTSIDE the bot, via systemd timer ~every 60s).

Provides two safety nets the bot's IN-PROCESS guards cannot:
  1) HEARTBEAT STALENESS — the bot writes heartbeat.txt every cycle. If it goes stale,
     the bot process is hung or stuck in a logic loop. systemd's Restart=always does NOT
     catch this (the process is still "active"), so the watchdog restarts the service.
  2) ABSOLUTE EQUITY FLOOR — if the full spot-wallet value drops below WATCHDOG_FLOOR_USD
     (a hard catastrophic floor set BELOW your normal equity), the watchdog STOPS the bot
     and alerts. Open positions keep their exchange-side native SLs (we do NOT cancel them).
     Set WATCHDOG_FLATTEN=1 to also market-sell everything (nuclear option, OFF by default).

Fail-safe by design: any error just logs and exits — the watchdog NEVER opens a trade.
Defaults are conservative: with WATCHDOG_FLOOR_USD unset (0), only the heartbeat-restart
runs. Run as root (systemd system timer) so `systemctl` works without sudo.

Env (from .env / EnvironmentFile):
  WATCHDOG_FLOOR_USD   absolute USD equity floor; 0 or unset = floor check disabled
  WATCHDOG_STALE_SEC   heartbeat staleness threshold in seconds (default 300)
  WATCHDOG_FLATTEN     "1" to market-sell all on a floor breach (default: halt only)
  WATCHDOG_SERVICE     systemd unit name (default binance-bot-v11)
"""
import os, time, subprocess, urllib.request, urllib.parse
from pathlib import Path

BOT_DIR   = Path(os.path.dirname(os.path.abspath(__file__)))
SERVICE   = os.environ.get("WATCHDOG_SERVICE", "binance-bot-v11")
HEARTBEAT = BOT_DIR / "heartbeat.txt"
STALE_SEC = int(os.environ.get("WATCHDOG_STALE_SEC", "600") or 600)   # v18.9.4: 600s (was 300)
GRACE_SEC = int(os.environ.get("WATCHDOG_GRACE_SEC", "300") or 300)   # after a restart, wait this long before another
FLOOR_USD = float(os.environ.get("WATCHDOG_FLOOR_USD", "0") or 0)   # 0 = floor check OFF
STABLES   = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD")
RESTART_STATE = BOT_DIR / "watchdog_state.txt"   # last-restart ts, for the grace window


def _load_env():
    try:
        for line in (BOT_DIR / ".env").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")


def _tg(msg):
    tok, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not (tok and chat):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=10)
    except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")


def _systemctl(action):
    for cmd in (["systemctl", action, SERVICE], ["sudo", "-n", "systemctl", action, SERVICE]):
        try:
            if subprocess.run(cmd, timeout=30, check=False).returncode == 0:
                return True
        except Exception:
            continue
    print(f"systemctl {action} {SERVICE} failed")
    return False


def _equity_usdt():
    from binance.client import Client
    c = Client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])
    prices = {t["symbol"]: float(t["price"]) for t in c.get_all_tickers()}
    total = 0.0
    for b in c.get_account()["balances"]:
        amt = float(b["free"]) + float(b["locked"])
        if amt <= 0:
            continue
        a = b["asset"]
        total += amt if a in STABLES else amt * prices.get(a + "USDT", 0.0)
    return total


def _flatten():
    """Nuclear option (opt-in): cancel orders + market-sell every non-stable balance."""
    from binance.client import Client
    c = Client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])
    syms = {s["symbol"]: s for s in c.get_exchange_info()["symbols"]}
    for b in c.get_account()["balances"]:
        a = b["asset"]; amt = float(b["free"]) + float(b["locked"])
        sym = a + "USDT"
        if a in STABLES or amt <= 0 or sym not in syms:
            continue
        try:
            c.cancel_open_orders(symbol=sym)  # free the qty held by native SL orders
            step = next((float(f["stepSize"]) for f in syms[sym]["filters"] if f["filterType"] == "LOT_SIZE"), 0.0)
            free = float(c.get_asset_balance(asset=a)["free"])
            q = (int(free / step) * step) if step else free
            if q > 0:
                c.order_market_sell(symbol=sym, quantity=("%.8f" % q).rstrip("0").rstrip("."))
                print(f"flattened {sym}: sold {q}")
        except Exception as e:
            print(f"flatten {sym} failed: {e}")


def main():
    _load_env()

    # 1) Heartbeat staleness -> restart (recovers a hung/looping process). v18.9.4: after a
    # restart, hold off for GRACE_SEC so the bot can finish its (slow) startup + write the
    # first heartbeat — otherwise restarting mid-boot loops forever (the v18.9.2 bug).
    try:
        if HEARTBEAT.exists():
            stale = time.time() - float(HEARTBEAT.read_text().strip() or 0)
            since_restart = 1e9
            try:
                if RESTART_STATE.exists():
                    since_restart = time.time() - float(RESTART_STATE.read_text().strip() or 0)
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
            if stale > STALE_SEC and since_restart > GRACE_SEC:
                _tg(f"🐶 <b>WATCHDOG</b>: heartbeat stale {stale:.0f}s (&gt; {STALE_SEC}s) — restarting <code>{SERVICE}</code>")
                try: RESTART_STATE.write_text(str(int(time.time())))
                except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")
                _systemctl("restart")
                return
    except Exception as e:
        print(f"heartbeat check skipped: {e}")

    # 2) Absolute equity floor -> HALT (last-resort kill switch; opt-in via WATCHDOG_FLOOR_USD).
    if FLOOR_USD > 0:
        try:
            eq = _equity_usdt()
            if eq < FLOOR_USD:
                flat = os.environ.get("WATCHDOG_FLATTEN") == "1"
                _tg(f"🛑 <b>WATCHDOG KILL</b>: equity ${eq:.2f} &lt; floor ${FLOOR_USD:.2f} — STOPPING "
                    f"<code>{SERVICE}</code>" + (" + flattening" if flat else " (positions keep exchange SLs)"))
                _systemctl("stop")
                if flat:
                    _flatten()
                return
            print(f"watchdog OK: equity ${eq:.2f} >= floor ${FLOOR_USD:.2f}")
        except Exception as e:
            print(f"equity floor check skipped: {e}")


if __name__ == "__main__":
    main()
