"""The deterministic grading contract, the single source of truth for "did the
agent succeed?" in Stage 2.

There is NO LLM judge here, and no contest; this is the autonomous orchestrator's
**acceptance gate**, the deterministic "definition of done" (AGENTS.md §4). The composed
result either satisfies these checks or it does not; if it does, the orchestrator opens a PR.

The contract is defined against an abstract ``MCPClient`` protocol so the SAME tests
run two ways:

  * **Local / pre-deploy**: an in-process adapter over ``cost_analyzer`` (so the
    grader is provably green before any AgentCore deployment exists).
  * **Deployed**: an adapter over the agent's remote MCP endpoint on AgentCore
    Runtime/Gateway (``tools/list`` + ``tools/call`` over the wire).

The orchestrator (``src/orchestrator/``) runs these as pytest in its finalization phase
against the composed result; green → open the PR autonomously, red → bounded retry then a
human. It records per-role wall-clock + token cost as run metrics (observability, not a
ranking).
"""

from __future__ import annotations

import math
from typing import Any, Protocol


# The five tools every valid submission must expose. This is the acceptance set.
REQUIRED_TOOLS: set[str] = {
    "estimate_ec2_monthly_cost",
    "estimate_ebs_monthly_cost",
    "estimate_s3_monthly_cost",
    "recommend_instance",
    "estimate_stack_monthly_cost",
}

# Numeric tolerance for money comparisons (cents).
MONEY_TOL = 0.01


class MCPClient(Protocol):
    """Minimal MCP surface the grader needs. Adapters implement this."""

    def list_tools(self) -> list[dict[str, Any]]:
        """Return tool specs, MCP ``tools/list`` shape ({name, description, inputSchema})."""
        ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool, MCP ``tools/call`` shape; return the structured result dict."""
        ...


# --- Fixture cases: (tool, arguments, key, expected), kept tiny + deterministic. ---
# These mirror values produced by the reference cost_analyzer so a correct port matches.

CASES: list[tuple[str, dict[str, Any], str, float]] = [
    ("estimate_ec2_monthly_cost", {"instance_type": "m5.large", "count": 2}, "monthly_cost", 140.16),
    ("estimate_ec2_monthly_cost", {"instance_type": "t3.micro"}, "monthly_cost", 7.59),
    ("estimate_ebs_monthly_cost", {"volume_type": "gp3", "size_gb": 100}, "monthly_cost", 8.0),
    ("estimate_s3_monthly_cost", {"storage_gb": 100}, "monthly_cost", 2.3),
    ("recommend_instance", {"vcpus": 2, "memory_gib": 8}, "recommended_instance_type", "m5.large"),
]


def check_tool_discovery(client: MCPClient) -> tuple[bool, str]:
    """Check 1: all REQUIRED_TOOLS are discoverable via tools/list."""
    names = {t.get("name") for t in client.list_tools()}
    missing = REQUIRED_TOOLS - names
    if missing:
        return False, f"missing tools: {sorted(missing)}"
    return True, "all required tools discoverable"


def check_tool_correctness(client: MCPClient) -> tuple[bool, str]:
    """Check 2: each fixture case returns the correct sizing/pricing result."""
    for name, args, key, expected in CASES:
        result = client.call_tool(name, args)
        if key not in result:
            return False, f"{name}{args}: result missing key {key!r} (got {sorted(result)})"
        actual = result[key]
        if isinstance(expected, float):
            if not (isinstance(actual, (int, float)) and math.isclose(actual, expected, abs_tol=MONEY_TOL)):
                return False, f"{name}{args}: {key}={actual!r}, expected ~{expected}"
        else:
            if actual != expected:
                return False, f"{name}{args}: {key}={actual!r}, expected {expected!r}"
    return True, f"{len(CASES)} fixture cases correct"


def check_input_validation(client: MCPClient) -> tuple[bool, str]:
    """Check 3: unknown inputs are rejected, not silently mispriced."""
    try:
        client.call_tool("estimate_ec2_monthly_cost", {"instance_type": "not-a-real-type"})
    except Exception:
        return True, "unknown instance type correctly rejected"
    return False, "unknown instance type was NOT rejected (should raise)"


# Ordered list of (check_id, callable). The orchestrator iterates this.
CHECKS = [
    ("tool_discovery", check_tool_discovery),
    ("tool_correctness", check_tool_correctness),
    ("input_validation", check_input_validation),
]


def grade(client: MCPClient) -> dict[str, Any]:
    """Run every check against a client. Returns a structured verdict.

    ``passed`` is True only if ALL checks pass. This is the boolean the
    orchestrator's finalization phase uses to decide "done"; green opens the PR.
    """
    results = []
    for check_id, fn in CHECKS:
        try:
            ok, detail = fn(client)
        except Exception as exc:  # a crashing check is a failed check, never a crash
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"check": check_id, "passed": ok, "detail": detail})
    return {"passed": all(r["passed"] for r in results), "checks": results}
