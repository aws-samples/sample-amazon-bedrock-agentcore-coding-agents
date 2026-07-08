"""Governance & Metrics API (Stage 3, Connect layer).

The Stage 3 backend. Stdlib only, runs instantly:

    python3 metrics-api/metrics_api.py        # serves http://localhost:8092

API-first (Chandra's P0): every number the console shows comes from `metrics_lib`,
the co-equal Python library. This HTTP layer is a thin shell: each handler calls
a lib function and serializes the result. The lib and REST share ONE data path, so
a Python caller and a REST caller can never diverge.

metrics_lib aggregates the shared telemetry ledger (`.runs/telemetry.jsonl`) that
Stage 1 sessions and Stage 2 engine runs append to as they run on this machine.
No seed dataset; run nothing and the numbers are zero. Identity is the OS user
(the local stand-in for the on-behalf-of chain). The kill switch signals the
recorded process.

On AgentCore the rows come from the sdlc session-tracking DynamoDB +
CloudWatch/X-Ray instead of the local ledger, and identity comes from
AgentCore Identity. The HTTP shapes here do NOT change; contract-first.

Per-user attribution is DERIVED from session metadata until Runtime exposes it
natively (SIFT 5/26 gap). Costs are estimates (measured latency at published
Bedrock rates), never live billing, and the per-agent split is attribution only:
no race, no winner.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# When run as a script, ensure the file's own directory is importable so `metrics_lib`
# resolves regardless of the caller's CWD. (The lib is the single shared data path.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics_lib  # noqa: E402  (import after sys.path tweak, intentional)

HOST = "0.0.0.0"
PORT = 8092


def _first(qs: dict, key: str):
    """First value for a query param, or None."""
    vals = qs.get(key)
    return vals[0] if vals else None


def dispatch(method: str, path: str, query: str, body: dict | None) -> tuple[int, dict]:
    """Pure router for the Stage 3 API: (status, json-able dict).

    Shared by the standalone server (below) and the unified console server
    (`console/server.py`) so both read the SAME metrics_lib data path.
    `query` is the raw query string (e.g. "by=agent"); `body` is unused here
    except to keep the signature uniform across the three backends.
    """
    qs = parse_qs(query or "")

    if method == "GET":
        if path == "/api/health":
            return 200, {"status": "ok", "mode": "engine"}
        if path == "/api/sessions":
            filters = {}
            user_id = _first(qs, "user_id")
            assistant_type = _first(qs, "assistant_type")
            window = _first(qs, "window")
            if user_id:
                filters["user_id"] = user_id
            if assistant_type:
                filters["assistant_type"] = assistant_type
            if window is not None:
                filters["window"] = window
            return 200, metrics_lib.list_sessions(filters or None)
        if path == "/api/cost-breakdown":
            return 200, metrics_lib.get_cost_breakdown(by=_first(qs, "by") or "agent")
        if path == "/api/latency/p95":
            scope = {}
            assistant_type = _first(qs, "assistant_type")
            user_id = _first(qs, "user_id")
            if assistant_type:
                scope["assistant_type"] = assistant_type
            if user_id:
                scope["user_id"] = user_id
            return 200, metrics_lib.get_latency_p95(scope or None)
        if path == "/api/policies":
            return 200, metrics_lib.get_policies()
        if path == "/api/audit":
            try:
                limit = int(_first(qs, "limit") or 200)
            except ValueError:
                limit = 200
            return 200, metrics_lib.get_audit_trail(limit=limit)
        if path == "/api/dashboard":
            return 200, metrics_lib.get_dashboard()
        if path == "/api/runtimes":
            return 200, metrics_lib.list_runtimes()
        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "users" and parts[4] == "metrics":
            return 200, metrics_lib.get_user_metrics(parts[3], _first(qs, "time_range") or "24h")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "sessions" and parts[4] == "identity":
            identity = metrics_lib.get_identity(parts[3])
            if identity is None:
                return 404, {"error": "session not found", "session_id": parts[3]}
            return 200, identity
        return 404, {"error": "not found", "path": path}

    if method == "POST":
        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "sessions" and parts[4] == "stop":
            result = metrics_lib.stop_session(parts[3])
            if result is None:
                return 404, {"error": "session not found", "session_id": parts[3]}
            return 200, result
        # POST /api/runtimes/{role}/probe runs a tiny live job in the role's
        # deployed runtime to confirm the fleet is executing.
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "runtimes" and parts[4] == "probe":
            return 200, metrics_lib.dispatch_probe(parts[3])
        return 404, {"error": "not found", "path": path}

    return 404, {"error": "method not allowed", "method": method}


class Handler(BaseHTTPRequestHandler):
    # --- helpers ---
    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # This API exposes per-user cost, session identity, and the audit trail,
        # so a wildcard CORS header would let any website read another attendee's
        # governance data. Emit CORS only for a genuine same-origin call,
        # reflecting that exact origin; same-origin GETs and curl send no Origin.
        origin = self._same_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _same_origin(self) -> str | None:
        """Return the request Origin iff same-origin with the Host, else None.

        Missing/garbled Origin (same-origin GETs, curl) yields None, so no CORS
        headers, which is correct. Mirrors console/server.py's gate.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return None
        # Never reflect a CRLF-bearing Origin into the Access-Control-Allow-Origin
        # response header: `Origin: http://host/\r\n...` keeps netloc==host and would
        # otherwise pass the equality gate below, letting an obs-fold header ride the
        # reflection (py/http-response-splitting). A real origin never contains CRLF.
        if "\r" in origin or "\n" in origin:
            return None
        try:
            authority = urlparse(origin).netloc
        except ValueError:
            return None
        return origin if authority and authority.lower() == host.lower() else None

    def log_message(self, *args) -> None:  # quiet logs
        pass

    def do_OPTIONS(self) -> None:  # CORS preflight
        self._send(200, {})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        code, out = dispatch("GET", parsed.path.rstrip("/") or "/", parsed.query, None)
        self._send(code, out)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        # drain any request body (ignored) so the socket stays clean
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        code, out = dispatch("POST", parsed.path.rstrip("/") or "/", parsed.query, None)
        self._send(code, out)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Governance & Metrics API on http://localhost:{PORT}")
    print(
        "Endpoints: GET /api/health  GET /api/sessions  GET /api/users/{id}/metrics  "
        "GET /api/cost-breakdown  GET /api/latency/p95  GET /api/sessions/{id}/identity  "
        "POST /api/sessions/{id}/stop  GET /api/policies  GET /api/dashboard"
    )
    print("All data flows through metrics_lib (API-first) over the real run ledger "
          "(.runs/telemetry.jsonl).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
