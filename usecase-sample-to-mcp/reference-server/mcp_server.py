"""Reference MCP server: the BACKEND role's deliverable, as real working code.

This is what "convert the module to a remote MCP server" produces: every function in
``cost_analyzer.TOOL_SPECS`` exposed over MCP's JSON-RPC wire shape (``tools/list`` +
``tools/call``). Standard library only, so it runs anywhere instantly:

    python3 src/usecase-sample-to-mcp/reference-server/mcp_server.py --port 9000

Then the SAME grading contract that passed in-process passes over the wire:

    MCP_ENDPOINT_URL=http://127.0.0.1:9000 pytest src/usecase-sample-to-mcp/grading/

Three jobs in one file:
  * Stage 1: the reference solution for the by-hand conversion (compare yours to this).
  * Stage 2: the artifact the orchestrator engine boots as a subprocess, so the
    acceptance gate runs against a live local endpoint.
  * Stage 3: ``deploy/`` wraps the graded ``handle_rpc`` function in AgentCore's
    FastMCP transport and registers that Runtime behind Gateway.

Wire shape (mirrors the GitHub MCP gateway in the reference harness):
    -> {"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}
    <- {"jsonrpc":"2.0","id":1,"result":{"tools":[{name,description,inputSchema}...]}}
    -> {"jsonrpc":"2.0","method":"tools/call","id":2,
        "params":{"name":"estimate_ec2_monthly_cost","arguments":{"instance_type":"m5.large"}}}
    <- {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{...json...}"}],
        "isError":false}}
Invalid input (e.g. an unknown instance type) returns a JSON-RPC error object; the
client adapter raises, which is exactly what the ``input_validation`` check asserts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Make the sibling cost_analyzer importable regardless of caller CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cost_analyzer  # noqa: E402

SERVER_INFO = {"name": "cost-analyzer-mcp", "version": "1.0.0"}
PROTOCOL_VERSION = "2025-03-26"


def _rpc_result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_rpc(payload: dict) -> dict:
    """Dispatch one JSON-RPC request. Pure function: easy to test, easy to read."""
    req_id = payload.get("id")
    method = payload.get("method", "")
    params = payload.get("params") or {}

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
        })
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": cost_analyzer.list_tools()})
    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            out = cost_analyzer.dispatch(name, arguments)
        except KeyError as exc:
            return _rpc_error(req_id, -32601, f"unknown tool: {exc}")
        except (ValueError, TypeError) as exc:
            # Invalid input must be a hard error, never a silently wrong price.
            return _rpc_error(req_id, -32602, f"invalid arguments for {name}: {exc}")
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": json.dumps(out)}],
            "isError": False,
        })
    return _rpc_error(req_id, -32601, f"method not found: {method}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # quiet logs
        pass

    def do_GET(self) -> None:
        # Liveness probe for the engine / load balancers. MCP itself is POST-only.
        self._send(200, {"status": "ok", "server": SERVER_INFO["name"]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return self._send(400, _rpc_error(None, -32700, "parse error: invalid JSON"))
        self._send(200, handle_rpc(payload))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "9000")))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"cost-analyzer MCP server on http://{args.host}:{args.port}  "
          f"({len(cost_analyzer.TOOL_SPECS)} tools)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
