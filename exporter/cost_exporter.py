#!/usr/bin/env python3
"""
cost_exporter.py — a tiny companion Prometheus exporter for the LLM Observability
Stack.

WHY THIS EXISTS (read this before assuming it should be a pure Grafana expression):

Grafana's dashboard-native math (PromQL expressions, transforms, "Add field from
calculation") can only operate on data that is *already in a metric* or on values
already present in a query result. It cannot reach into an arbitrary environment
variable at query time and multiply it against a live Prometheus counter — PromQL has
no notion of a Grafana-side env var, and Grafana transforms operate on already-returned
query results, not on injecting external scalars into a running rate() calculation in a
generically reusable way across panels.

You *can* hardcode a dollar figure directly into a PromQL expression (e.g.
`sum(rate(rag_tokens_generated_total[5m])) * 0.00001`), but then the price lives
buried inside dashboard JSON, invisible and easy to forget about, and you have to hunt
down and hand-edit every panel that references it every time pricing changes.

So instead: this is a ~60-line Prometheus *exporter* — the standard pattern for
"derive a new metric from an existing one with logic Prometheus/PromQL can't express
cleanly." It polls Prometheus for the current value of `rag_tokens_generated_total`,
multiplies by COST_PER_1K_TOKENS / 1000, and re-exposes the result as
`estimated_cost_usd_total`, which Prometheus then scrapes like any other target (see
the `cost-exporter` job in prometheus/prometheus.yml). The dashboard panels just read
that metric — no cost math lives in dashboard JSON at all.

This keeps the $/1K rate in exactly one place (an env var you can change without
touching any JSON), and keeps the dashboard honest: if you don't run this exporter,
the cost panels show "No data" instead of a silently wrong number.

Usage (standalone):
    COST_PER_1K_TOKENS=0.01 PROMETHEUS_URL=http://localhost:9090 python3 cost_exporter.py

Usage (docker compose):
    docker compose --profile cost up -d
"""

import os
import sys
import time
import urllib.request
import urllib.parse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
COST_PER_1K_TOKENS = float(os.environ.get("COST_PER_1K_TOKENS", "0.01"))
SCRAPE_INTERVAL_SECONDS = float(os.environ.get("SCRAPE_INTERVAL_SECONDS", "15"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9105"))
TOKEN_METRIC = "rag_tokens_generated_total"

# In-memory cache of the last computed value, served to Prometheus on scrape.
_state = {
    "estimated_cost_usd_total": 0.0,
    "last_token_total": None,
    "last_success": False,
    "last_error": "",
}


def query_prometheus_instant(expr: str):
    """Run an instant PromQL query against Prometheus and return the scalar/sum
    result, or None if the query failed or returned no data."""
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    result = payload["data"]["result"]
    if not result:
        return None
    # sum(...) queries return a single vector element with the aggregated value.
    return float(result[0]["value"][1])


def refresh_estimate():
    """Pull the current total tokens generated from Prometheus, recompute the
    estimated cost, and update the in-memory state that /metrics serves."""
    try:
        total_tokens = query_prometheus_instant(f"sum({TOKEN_METRIC})")
        if total_tokens is None:
            # No data yet (e.g. rag-service hasn't emitted anything) — not an error,
            # just nothing to compute yet.
            _state["last_success"] = True
            _state["last_error"] = ""
            return
        estimated_cost = (total_tokens * COST_PER_1K_TOKENS) / 1000.0
        _state["estimated_cost_usd_total"] = estimated_cost
        _state["last_token_total"] = total_tokens
        _state["last_success"] = True
        _state["last_error"] = ""
    except Exception as exc:  # noqa: BLE001 — exporter must never crash the loop
        _state["last_success"] = False
        _state["last_error"] = str(exc)
        print(f"[cost_exporter] refresh failed: {exc}", file=sys.stderr)


class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet down default request logging
        pass

    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return

        lines = [
            "# HELP estimated_cost_usd_total Estimated cumulative USD cost, computed "
            "as total tokens generated * COST_PER_1K_TOKENS / 1000.",
            "# TYPE estimated_cost_usd_total counter",
            f"estimated_cost_usd_total {_state['estimated_cost_usd_total']}",
            "# HELP cost_exporter_cost_per_1k_tokens_usd The configured $/1K token "
            "rate this exporter is using (COST_PER_1K_TOKENS env var).",
            "# TYPE cost_exporter_cost_per_1k_tokens_usd gauge",
            f"cost_exporter_cost_per_1k_tokens_usd {COST_PER_1K_TOKENS}",
            "# HELP cost_exporter_up Whether the last Prometheus poll succeeded "
            "(1) or failed (0).",
            "# TYPE cost_exporter_up gauge",
            f"cost_exporter_up {1 if _state['last_success'] else 0}",
        ]
        body = ("\n".join(lines) + "\n").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    print(
        f"[cost_exporter] starting: PROMETHEUS_URL={PROMETHEUS_URL} "
        f"COST_PER_1K_TOKENS={COST_PER_1K_TOKENS} "
        f"SCRAPE_INTERVAL_SECONDS={SCRAPE_INTERVAL_SECONDS} "
        f"listening on :{EXPORTER_PORT}/metrics"
    )
    refresh_estimate()  # populate immediately so first scrape isn't empty

    server = ThreadingHTTPServer(("0.0.0.0", EXPORTER_PORT), MetricsHandler)

    # Refresh the estimate in the background on a timer, driven from the request
    # thread's own loop would block serving — instead do a simple poll-before-serve
    # by refreshing lazily each interval via a background thread.
    import threading

    def loop():
        while True:
            time.sleep(SCRAPE_INTERVAL_SECONDS)
            refresh_estimate()

    threading.Thread(target=loop, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
