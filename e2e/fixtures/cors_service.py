#!/usr/bin/env python3
"""A hand-written FIXTURE service, not a sample and not a reference solution.

It exists for exactly one test: proving that a service which answers a browser's CORS
preflight really is reachable from a page on another origin. That is a BROWSER fact, so
it has to be pinned against something real rather than asserted in prose, and a live
traverse already hit the bug it guards (a service handling only GET and POST looks fine
from curl and is unreachable from a page).

Nothing in the workshop builds this, imports it, or is graded against it. What an agent
writes for a given request is the agent's business; this file only holds the wire
behaviour still under test.
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self) -> None:
        """The preflight a browser sends before a cross-origin JSON POST."""
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._json(200, {"status": "ok"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return self._json(400, {"error": "parse error"})
        return self._json(200, {"echo": payload})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "0")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
