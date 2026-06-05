"""BinBot v15.0 — Prometheus-style metrics exporter

Exposes time-series metrics on http://127.0.0.1:9090/metrics for Prometheus
scrape. No Prometheus client lib needed — pure stdlib HTTP server.

Metrics exposed:
  - binbot_capital_usd
  - binbot_open_positions
  - binbot_daily_pnl_usd
  - binbot_total_pnl_usd
  - binbot_wins_total / binbot_losses_total
  - binbot_drawdown_pct
  - binbot_sharpe / binbot_sortino / binbot_calmar
  - binbot_consec_losses
  - binbot_ml_accuracy
  - binbot_position_value_usd{pair="BTCUSDT"} ...
  - binbot_cycle_count_total
  - binbot_signal_blocks_total{reason="vpin"} ...

Integration:
    from prom_metrics import PrometheusExporter
    self.prom = PrometheusExporter(port=9090)
    self.prom.start()
    # Then update metrics:
    self.prom.set("capital_usd", self.cfg.TOTAL_CAPITAL)
    self.prom.inc("signal_blocks_total", labels={"reason": "vpin"})
"""
from __future__ import annotations
import logging, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import defaultdict
from typing import Optional, Dict, Tuple

log = logging.getLogger("binbot")

_LOCK = threading.Lock()
_GAUGES: Dict[Tuple[str, frozenset], float] = {}
_COUNTERS: Dict[Tuple[str, frozenset], float] = defaultdict(float)


def _key(name: str, labels: Optional[Dict[str, str]] = None) -> Tuple[str, frozenset]:
    return (name, frozenset((labels or {}).items()))


def _format_labels(labels_set: frozenset) -> str:
    if not labels_set:
        return ""
    parts = [f'{k}="{v}"' for k, v in sorted(labels_set)]
    return "{" + ",".join(parts) + "}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw): pass  # silence

    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        lines = []
        with _LOCK:
            seen_names = set()
            for (name, lbls), val in _GAUGES.items():
                if name not in seen_names:
                    lines.append(f"# TYPE binbot_{name} gauge")
                    seen_names.add(name)
                lines.append(f"binbot_{name}{_format_labels(lbls)} {val}")
            seen_names = set()
            for (name, lbls), val in _COUNTERS.items():
                if name not in seen_names:
                    lines.append(f"# TYPE binbot_{name} counter")
                    seen_names.add(name)
                lines.append(f"binbot_{name}{_format_labels(lbls)} {val}")
            lines.append(f"# TYPE binbot_scrape_unix_ts gauge")
            lines.append(f"binbot_scrape_unix_ts {time.time()}")
        body = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PrometheusExporter:
    """Tiny Prometheus exporter — starts a thread, exposes :9090/metrics."""

    def __init__(self, port: int = 9090, bind: str = "127.0.0.1"):
        self.port = port
        self.bind = bind
        self._server = None
        self._thread = None

    def start(self):
        try:
            self._server = ThreadingHTTPServer((self.bind, self.port), _Handler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="PromExporter"
            )
            self._thread.start()
            log.info(f"📊 Prometheus exporter live at http://{self.bind}:{self.port}/metrics")
        except Exception as e:
            log.warning(f"Prometheus exporter start failed: {e}")

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as _e: __import__("logging").getLogger("binbot").warning(f"Ignored exception: {_e}")

    # ─── Public API (module-level locks) ──────────────────────────────────────

    @staticmethod
    def set(name: str, value: float, labels: Optional[Dict[str, str]] = None):
        with _LOCK:
            _GAUGES[_key(name, labels)] = float(value)

    @staticmethod
    def inc(name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None):
        with _LOCK:
            _COUNTERS[_key(name, labels)] += float(value)
