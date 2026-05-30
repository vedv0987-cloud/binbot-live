# BinBot v13.6 — web_dashboard.py
"""Read-only web dashboard for BinBot.

PURPOSE:
A simple HTTP server that serves a single auto-refreshing HTML page showing
positions, PnL, regime, recent trades, and bot health. Read-only — no buttons,
no API, no auth required because it serves only what's already in
dashboard_data.json + bot_state.json (no secrets).

DESIGN:
- Uses Python's built-in http.server (no Flask dependency)
- Runs in a daemon thread, so a crash here can't kill the bot
- Bot writes dashboard_data.json every cycle; dashboard reads it on each request
- Auto-refresh via <meta http-equiv="refresh" content="10">
- Mobile-friendly viewport, dark theme

SECURITY:
- BIND='127.0.0.1' is localhost-only (default, safe)
- BIND='0.0.0.0' exposes to network — REQUIRES firewall rule and accepts the
  risk that a network observer can see your PnL/positions (NOT secrets)
- v14.5.1 FIX (audit #11): corrected contradictory comments above
- API keys, .env, bot_state.json are NEVER served
- Server only reads two specific files (dashboard_data.json, bot_state.json
  for derived metrics) — no path traversal possible

USAGE:
- Set cfg.WEB_DASHBOARD_ENABLED = True
- Set cfg.WEB_DASHBOARD_PORT = 8080 (or whatever)
- Set cfg.WEB_DASHBOARD_BIND = '0.0.0.0' for network access (firewall first!)
                            or '127.0.0.1' for localhost only (default)
- Bot starts the server in a daemon thread at boot
- Visit http://VM_IP:8080/ in a browser
"""
from __future__ import annotations
import json, logging, threading, time, html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("binbot")

# Files we're willing to read. Strict allowlist — anything else → 404.
ALLOWED_FILES = {"dashboard_data.json", "bot_state.json"}


class DashboardHandler(BaseHTTPRequestHandler):
    """Renders a single page on GET /; static JSON on GET /data.json."""

    def log_message(self, format, *args):  # noqa: A002
        # Silence default per-request logging — we don't want it polluting bot logs
        pass

    def do_GET(self):  # noqa: N802
        try:
            if self.path in ("/", "/index.html"):
                self._send_html()
            elif self.path == "/data.json":
                self._send_json()
            elif self.path == "/healthz":
                self._send_simple(200, "ok\n", "text/plain")
            else:
                self._send_simple(404, "Not Found\n", "text/plain")
        except Exception as e:
            log.warning(f"Dashboard request error: {e}")
            try:
                self._send_simple(500, "Internal Server Error\n", "text/plain")
            except Exception:
                pass

    # ── Senders ───────────────────────────────────────────────────────────────

    def _send_simple(self, code: int, body: str, ctype: str):
        body_b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_b)

    def _send_json(self):
        data = self._load_dashboard_data()
        body = json.dumps(data, default=str)
        self._send_simple(200, body, "application/json")

    def _send_html(self):
        data = self._load_dashboard_data()
        body = self._render_html(data)
        self._send_simple(200, body, "text/html; charset=utf-8")

    # ── Data ──────────────────────────────────────────────────────────────────

    def _load_dashboard_data(self) -> dict:
        """Merge dashboard_data.json (live snapshot) + bot_state.json (totals)."""
        out = {}
        try:
            if Path("dashboard_data.json").exists():
                # v14.6.3 FIX: retry on JSONDecodeError (mid-write collision)
                for _retry in range(3):
                    try:
                        with open("dashboard_data.json") as f:
                            out.update(json.load(f))
                        break
                    except json.JSONDecodeError:
                        time.sleep(0.05)
        except Exception as e:
            out["_dashboard_load_err"] = str(e)
        try:
            if Path("bot_state.json").exists():
                for _retry in range(3):
                    try:
                        with open("bot_state.json") as f:
                            s = json.load(f)
                        break
                    except json.JSONDecodeError:
                        time.sleep(0.05)
                        s = {}
                # Pull only safe, derived totals — never API keys or secrets
                out["lifetime_pnl"]  = s.get("pnl", 0.0)
                out["lifetime_wins"] = s.get("wins", 0)
                out["lifetime_losses"] = s.get("losses", 0)
                out["dd_peak"]       = s.get("dd_peak", 0.0)
                out["peak_equity"]   = s.get("peak_equity", 0.0)
                out["fees_paid"]     = s.get("fees", 0.0)
                out["consec_losses"] = s.get("closs", 0)
                out["state_saved_at"] = s.get("saved_at", "")
                # Active positions with detail
                out["positions_detail"] = s.get("positions", [])
                # Cooldown summary
                lc = s.get("last_close", {})
                lr = s.get("last_result", {})
                cd_summary = []
                for pair, ts in lc.items():
                    cd_summary.append({"pair": pair, "result": lr.get(pair, "?"), "since": ts})
                out["cooldowns"] = cd_summary
        except Exception as e:
            out["_state_load_err"] = str(e)
        out["_rendered_at"] = datetime.now(timezone.utc).isoformat()
        return out

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_html(self, d: dict) -> str:
        """Hand-rolled HTML, dark theme, mobile-friendly, no JS dependencies."""
        # Auto-derived totals
        total_lifetime = d.get("lifetime_pnl", 0.0)
        wins = d.get("lifetime_wins", 0)
        losses = d.get("lifetime_losses", 0)
        wr = (wins / max(wins + losses, 1)) * 100 if (wins + losses) > 0 else 0
        day_pnl = d.get("daily_pnl", 0.0)
        day_t = d.get("trades_today", 0)
        positions = d.get("positions_detail", []) or d.get("positions", [])
        active_pos = len(positions) if isinstance(positions, list) else d.get("active_positions", 0)
        regime = d.get("regime", "?")
        fg = d.get("fg", 50)
        ml_acc = d.get("ml_accuracy", 0)
        wallet_bal = d.get("wallet_balance", d.get("bal", "?"))
        cap = d.get("capital", d.get("cap", "?"))
        cooldowns = d.get("cooldowns", [])
        ts = d.get("ts", d.get("_rendered_at", ""))
        dd_peak = d.get("dd_peak", 0)
        peak_eq = d.get("peak_equity", 0)
        fees = d.get("fees_paid", 0)
        consec = d.get("consec_losses", 0)

        # Color helpers
        pnl_color = "#3fb950" if total_lifetime >= 0 else "#f85149"
        day_color = "#3fb950" if day_pnl >= 0 else "#f85149"

        # Build position rows
        pos_rows = ""
        if positions:
            for p in positions:
                pair = html.escape(str(p.get("pair", "?")))
                entry = float(p.get("avg_entry", p.get("entry", 0)))
                qty = float(p.get("qty", 0))
                sl = float(p.get("sl", 0))
                tp = float(p.get("tp", 0))
                strat = html.escape(str(p.get("strategy", "?")))
                grade = html.escape(str(p.get("grade", "?")))
                size_usd = qty * entry
                sl_pct = ((sl - entry) / entry * 100) if entry > 0 else 0
                tp_pct = ((tp - entry) / entry * 100) if entry > 0 else 0
                be = "🔒" if p.get("be_locked") else ""
                native_sl = "🛡️" if p.get("native_sl_order_id") else ""
                pos_rows += f"""<tr>
                    <td><b>{pair}</b> {be}{native_sl}</td>
                    <td>{strat}<br><small>{grade}</small></td>
                    <td>${entry:.6f}<br><small>qty {qty:.4f}</small></td>
                    <td>${size_usd:.2f}</td>
                    <td style="color:#f85149">${sl:.6f}<br><small>{sl_pct:+.2f}%</small></td>
                    <td style="color:#3fb950">${tp:.6f}<br><small>{tp_pct:+.2f}%</small></td>
                </tr>"""
        else:
            pos_rows = """<tr><td colspan="6" style="text-align:center;color:#8b949e">No open positions</td></tr>"""

        # Cooldown rows
        cd_rows = ""
        if cooldowns:
            for c in cooldowns[:10]:
                pair = html.escape(str(c.get("pair", "?")))
                result = html.escape(str(c.get("result", "?")))
                since = html.escape(str(c.get("since", "")))[:19]  # trim micro
                cd_rows += f"<tr><td>{pair}</td><td>{result}</td><td><small>{since}</small></td></tr>"
        else:
            cd_rows = """<tr><td colspan="3" style="text-align:center;color:#8b949e">No active cooldowns</td></tr>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>BinBot v18.4 Dashboard</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
       background: #0d1117; color: #e6edf3; margin: 0; padding: 12px; }}
h1 {{ font-size: 18px; margin: 0 0 12px 0; color: #58a6ff; }}
h2 {{ font-size: 14px; margin: 16px 0 8px 0; color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 4px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 16px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 10px; }}
.card .label {{ font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
.card .value {{ font-size: 16px; font-weight: bold; margin-top: 2px; }}
.card .sub {{ font-size: 11px; color: #8b949e; margin-top: 2px; }}
table {{ width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d;
         border-radius: 6px; overflow: hidden; font-size: 13px; }}
th {{ text-align: left; padding: 8px; background: #21262d; font-size: 11px; text-transform: uppercase;
       color: #8b949e; border-bottom: 1px solid #30363d; }}
td {{ padding: 8px; border-bottom: 1px solid #21262d; }}
tr:last-child td {{ border-bottom: none; }}
small {{ color: #8b949e; }}
.muted {{ color: #8b949e; font-size: 11px; }}
.footer {{ margin-top: 24px; padding-top: 12px; border-top: 1px solid #30363d; font-size: 11px; color: #8b949e; text-align: center; }}
</style>
</head>
<body>
<h1>🚀 BinBot v18.4 — Live Dashboard</h1>

<div class="grid">
  <div class="card">
    <div class="label">Lifetime PnL</div>
    <div class="value" style="color:{pnl_color}">${total_lifetime:+.4f}</div>
    <div class="sub">{wins}W / {losses}L · {wr:.1f}% WR</div>
  </div>
  <div class="card">
    <div class="label">Today PnL</div>
    <div class="value" style="color:{day_color}">${day_pnl:+.4f}</div>
    <div class="sub">{day_t} trades today</div>
  </div>
  <div class="card">
    <div class="label">Open Positions</div>
    <div class="value">{active_pos} / 3</div>
    <div class="sub">{html.escape(str(regime))}</div>
  </div>
  <div class="card">
    <div class="label">Capital / Wallet</div>
    <div class="value">${cap}</div>
    <div class="sub">Bal: ${wallet_bal}</div>
  </div>
  <div class="card">
    <div class="label">Fear &amp; Greed</div>
    <div class="value">{fg}</div>
    <div class="sub">ML acc: {ml_acc:.1%}</div>
  </div>
  <div class="card">
    <div class="label">Drawdown Peak</div>
    <div class="value">${dd_peak:.2f}</div>
    <div class="sub">Peak Eq: ${peak_eq:.2f}</div>
  </div>
  <div class="card">
    <div class="label">Total Fees</div>
    <div class="value">${fees:.4f}</div>
    <div class="sub">Consec losses: {consec}</div>
  </div>
</div>

<h2>📦 Open Positions</h2>
<table>
  <tr><th>Pair</th><th>Strategy</th><th>Entry</th><th>Size</th><th>SL</th><th>TP</th></tr>
  {pos_rows}
</table>

<h2>⏳ Cooldowns</h2>
<table>
  <tr><th>Pair</th><th>Result</th><th>Since (UTC)</th></tr>
  {cd_rows}
</table>

<div class="footer">
  <div>Last update: {html.escape(str(ts))} · Auto-refresh every 10s</div>
  <div class="muted">v13.6 · read-only · no API keys exposed</div>
</div>
</body>
</html>"""


class DashboardServer:
    """Wraps ThreadingHTTPServer in a daemon thread; exposes start/stop."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._server = None
        self._thread = None

    def start(self):
        if not self.cfg.WEB_DASHBOARD_ENABLED:
            return
        try:
            bind = self.cfg.WEB_DASHBOARD_BIND
            port = int(self.cfg.WEB_DASHBOARD_PORT)
            self._server = ThreadingHTTPServer((bind, port), DashboardHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="WebDashboard",
            )
            self._thread.start()
            log.info(f"🌐 Web Dashboard live at http://{bind}:{port}/  (read-only, auto-refresh)")
        except Exception as e:
            log.warning(f"Web Dashboard failed to start: {e} — bot continues without dashboard")
            self._server = None

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
                log.info("🌐 Web Dashboard stopped")
            except Exception as e:
                log.debug(f"Dashboard stop error: {e}")
        self._server = None
