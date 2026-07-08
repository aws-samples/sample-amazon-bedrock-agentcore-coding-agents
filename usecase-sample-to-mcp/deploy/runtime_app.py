"""AgentCore MCP transport adapter for the graded PR deliverable.

The orchestrator produces ``mcp_server.py`` with a pure ``handle_rpc`` function.
This adapter exposes that exact implementation through FastMCP's streamable HTTP
transport, which satisfies the AgentCore Runtime MCP service contract.
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

import mcp_server


mcp = FastMCP("CostAnalyzerMCP")


def _call(name: str, arguments: dict) -> dict:
    response = mcp_server.handle_rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    if "error" in response:
        raise ValueError(response["error"]["message"])
    result = response["result"]
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and "text" in content[0]:
        return json.loads(content[0]["text"])
    return result


@mcp.tool()
def estimate_ec2_monthly_cost(instance_type: str, count: int = 1,
                              hours_per_month: float = 730.0,
                              region: str = "us-west-2") -> dict:
    """Estimate the monthly on-demand cost of EC2 instances."""
    return _call("estimate_ec2_monthly_cost", locals())


@mcp.tool()
def estimate_ebs_monthly_cost(volume_type: str, size_gb: float,
                              count: int = 1) -> dict:
    """Estimate the monthly cost of EBS volumes."""
    return _call("estimate_ebs_monthly_cost", locals())


@mcp.tool()
def estimate_s3_monthly_cost(storage_gb: float, get_requests: int = 0,
                             put_requests: int = 0,
                             storage_class: str = "STANDARD") -> dict:
    """Estimate the monthly cost of S3 storage and requests."""
    return _call("estimate_s3_monthly_cost", locals())


@mcp.tool()
def recommend_instance(vcpus: int, memory_gib: int) -> dict:
    """Recommend the cheapest instance meeting a vCPU and memory floor."""
    return _call("recommend_instance", locals())


@mcp.tool()
def estimate_stack_monthly_cost(spec: dict) -> dict:
    """Aggregate the monthly cost of a small architecture."""
    return _call("estimate_stack_monthly_cost", locals())


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", stateless_http=True)
