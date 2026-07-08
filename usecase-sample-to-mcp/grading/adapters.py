"""MCPClient adapters: one in-process (pre-deploy), one over the wire (deployed).

The grading contract (``contract.py``) is written against the ``MCPClient`` protocol
so the identical checks run before AND after deployment. Pre-deploy keeps the lab
green and teaches the contract; the deployed adapter is what the orchestrator points
at each agent's real AgentCore Runtime/Gateway endpoint in Stage 2.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Make the sibling cost_analyzer importable when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cost_analyzer  # noqa: E402


class InProcessClient:
    """Adapter over the reference cost_analyzer. No network, fully deterministic.

    This is what proves the grading contract is satisfiable before any agent runs,
    and what the workshop uses to demo the contract locally.
    """

    def list_tools(self) -> list[dict[str, Any]]:
        return cost_analyzer.list_tools()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return cost_analyzer.dispatch(name, arguments)


class MCPRemoteError(RuntimeError):
    """A JSON-RPC error returned by the remote MCP server (e.g. invalid input)."""


class RemoteMCPClient:
    """Adapter over a deployed MCP endpoint (JSON-RPC over HTTP).

    Stage 2 wires this at the orchestrator's finalization phase so the SAME contract
    grades the real, deployed server. The transport is plain JSON-RPC POSTs:

        tools/list -> {"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}
        tools/call -> {"jsonrpc":"2.0","method":"tools/call","id":2,
                       "params":{"name": <tool>, "arguments": {...}}}

    Locally this points at the reference server
    (``reference-server/mcp_server.py``); on AgentCore it points at the Gateway.
    Gateway access is IAM-authenticated, so when ``MCP_SIGV4=1`` is set the request
    is SigV4-signed with botocore (service ``bedrock-agentcore``): same wire shape,
    signed transport. A JSON-RPC ``error`` raises ``MCPRemoteError``, which is exactly
    what the ``input_validation`` check expects for bad input.
    """

    def __init__(self, endpoint_url: str, region: str = "us-west-2") -> None:
        self.endpoint_url = endpoint_url
        self.region = region
        self._id = 0
        self._sign = os.environ.get("MCP_SIGV4") == "1"

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        import urllib.request

        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "method": method, "id": self._id, "params": params}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._sign:  # Gateway path: SigV4-sign the same payload.
            headers.update(self._sigv4_headers(body))
        req = urllib.request.Request(self.endpoint_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if "error" in payload:
            err = payload["error"]
            raise MCPRemoteError(f"{err.get('code')}: {err.get('message')}")
        return payload.get("result", {})

    def _sigv4_headers(self, body: bytes) -> dict[str, str]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        import botocore.session

        creds = botocore.session.get_session().get_credentials().get_frozen_credentials()
        aws_req = AWSRequest(method="POST", url=self.endpoint_url, data=body,
                             headers={"Content-Type": "application/json"})
        SigV4Auth(creds, "bedrock-agentcore", self.region).add_auth(aws_req)
        return dict(aws_req.headers)

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._post("tools/list", {})
        return result.get("tools", result)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        # MCP wraps tool output in content blocks; unwrap the structured JSON.
        if isinstance(result, dict) and "content" in result:
            for block in result["content"]:
                if block.get("type") == "text":
                    return json.loads(block["text"])
        return result
