"""Local REST server for the Execution Chart UI (owner spec 2026-09-15).

Implements the spec's section-5 API contract AND hosts the chart frontend,
for mobile dev environments (Termux/Acode) where the GitHub-Pages site can't
reach a local trades.db:

    python3 ui/serve_ui.py                 # http://localhost:8080
    # open: http://localhost:8080/docs/chart.html?raw=http://localhost:8080/

Routes:
    GET /api/trades?symbol=NEAR/USDT   exact section-5 payload (live from trades.db)
    GET /data/trades_ui.json            fresh export for the chart frontend
    everything else                    static files from the repo root
"""
import json
import os
import sys
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import ui_store  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=_REPO, **kw)

    def _json(self, code, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/trades":
            symbol = parse_qs(u.query).get("symbol", [""])[0] or "NEAR/USDT"
            self._json(200, ui_store.api_trades(symbol))
            return
        if u.path == "/data/trades_ui.json":
            with_open = {}
            for sym in ("NEARUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
                p = ui_store.api_trades(sym)
                if p["trades"]:
                    with_open[p["symbol"]] = p["trades"]
            self._json(200, {"updated": datetime.now(timezone.utc).isoformat(),
                             "symbols": with_open})
            return
        super().do_GET()

    def log_message(self, fmt, *args):  # quieter Termux output
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"Execution Chart UI — http://localhost:{port}/docs/chart.html?raw=http://localhost:{port}/")
    print(f"API contract     — http://localhost:{port}/api/trades?symbol=NEAR/USDT")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
