"""cost_analyzer: the sample Python module the workshop converts into a remote MCP server.

This is plain, dependency-free Python: a small AWS sizing / pricing calculator.
It is intentionally the kind of "utility code every team has but nobody turns
into an MCP server because the conversion is tedious" (workshop hook, AGENTS.md §5).

Two things live here on purpose:

1. The pure functions (``estimate_ec2_monthly_cost`` ...). These are the sample.
2. A small **tool registry** (``TOOL_SPECS`` + ``dispatch``) that mirrors the MCP
   ``tools/list`` / ``tools/call`` shape. The registry is the *bridge*: the agents'
   job in Stage 2 is to wrap these handlers in a FastMCP server on AgentCore
   Runtime, and the deterministic grader (``grading/``) asserts against the same
   registry: in-process today, against the deployed endpoint once it exists.

IMPORTANT: the prices below are **illustrative, static, rounded** values for a
deterministic lab. They are NOT live AWS pricing. Never quote them as real.
"""

from __future__ import annotations

from typing import Any

# Illustrative hours in a month used for all monthly estimates.
HOURS_PER_MONTH = 730.0

# --- Illustrative price tables (us-west-2-ish, on-demand). NOT live pricing. ---

# EC2 on-demand hourly rate (USD) keyed by instance type.
EC2_HOURLY_USD: dict[str, float] = {
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "m5.large": 0.096,
    "m5.xlarge": 0.192,
    "c5.large": 0.085,
    "c5.xlarge": 0.17,
    "r5.large": 0.126,
    "r5.xlarge": 0.252,
}

# EC2 (vCPU, memory GiB) specs used by recommend_instance.
EC2_SPECS: dict[str, dict[str, int]] = {
    "t3.micro": {"vcpus": 2, "memory_gib": 1},
    "t3.small": {"vcpus": 2, "memory_gib": 2},
    "t3.medium": {"vcpus": 2, "memory_gib": 4},
    "m5.large": {"vcpus": 2, "memory_gib": 8},
    "m5.xlarge": {"vcpus": 4, "memory_gib": 16},
    "c5.large": {"vcpus": 2, "memory_gib": 4},
    "c5.xlarge": {"vcpus": 4, "memory_gib": 8},
    "r5.large": {"vcpus": 2, "memory_gib": 16},
    "r5.xlarge": {"vcpus": 4, "memory_gib": 32},
}

# EBS price per GB-month (USD) keyed by volume type.
EBS_GB_MONTH_USD: dict[str, float] = {
    "gp3": 0.08,
    "gp2": 0.10,
    "io1": 0.125,
    "st1": 0.045,
    "sc1": 0.015,
}

# S3 storage price per GB-month (USD) keyed by storage class.
S3_GB_MONTH_USD: dict[str, float] = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "GLACIER": 0.004,
}
# S3 request pricing (USD per 1,000 requests).
S3_GET_PER_1K_USD = 0.0004
S3_PUT_PER_1K_USD = 0.005

CURRENCY = "USD"


class UnknownResourceError(ValueError):
    """Raised when an instance type / volume type / storage class is unknown."""


def _round_money(value: float) -> float:
    return round(value, 2)


def estimate_ec2_monthly_cost(
    instance_type: str,
    count: int = 1,
    hours_per_month: float = HOURS_PER_MONTH,
    region: str = "us-west-2",
) -> dict[str, Any]:
    """Estimate the monthly on-demand cost of one or more EC2 instances.

    Returns a dict with the hourly rate, the resolved monthly cost, and the
    inputs echoed back so callers (and the chatbot UI) can render an explanation.
    """
    if instance_type not in EC2_HOURLY_USD:
        raise UnknownResourceError(f"Unknown EC2 instance type: {instance_type!r}")
    if count < 1:
        raise ValueError("count must be >= 1")
    if hours_per_month < 0:
        raise ValueError("hours_per_month must be >= 0")

    hourly = EC2_HOURLY_USD[instance_type]
    monthly = hourly * hours_per_month * count
    return {
        "service": "ec2",
        "instance_type": instance_type,
        "count": count,
        "hours_per_month": hours_per_month,
        "region": region,
        "hourly_rate": round(hourly, 4),
        "monthly_cost": _round_money(monthly),
        "currency": CURRENCY,
    }


def estimate_ebs_monthly_cost(
    volume_type: str,
    size_gb: float,
    count: int = 1,
) -> dict[str, Any]:
    """Estimate the monthly cost of one or more EBS volumes."""
    if volume_type not in EBS_GB_MONTH_USD:
        raise UnknownResourceError(f"Unknown EBS volume type: {volume_type!r}")
    if size_gb < 0:
        raise ValueError("size_gb must be >= 0")
    if count < 1:
        raise ValueError("count must be >= 1")

    rate = EBS_GB_MONTH_USD[volume_type]
    monthly = rate * size_gb * count
    return {
        "service": "ebs",
        "volume_type": volume_type,
        "size_gb": size_gb,
        "count": count,
        "gb_month_rate": round(rate, 4),
        "monthly_cost": _round_money(monthly),
        "currency": CURRENCY,
    }


def estimate_s3_monthly_cost(
    storage_gb: float,
    get_requests: int = 0,
    put_requests: int = 0,
    storage_class: str = "STANDARD",
) -> dict[str, Any]:
    """Estimate the monthly cost of S3 storage plus GET/PUT request charges."""
    if storage_class not in S3_GB_MONTH_USD:
        raise UnknownResourceError(f"Unknown S3 storage class: {storage_class!r}")
    if storage_gb < 0 or get_requests < 0 or put_requests < 0:
        raise ValueError("storage_gb, get_requests, put_requests must be >= 0")

    storage_cost = S3_GB_MONTH_USD[storage_class] * storage_gb
    get_cost = (get_requests / 1000.0) * S3_GET_PER_1K_USD
    put_cost = (put_requests / 1000.0) * S3_PUT_PER_1K_USD
    monthly = storage_cost + get_cost + put_cost
    return {
        "service": "s3",
        "storage_class": storage_class,
        "storage_gb": storage_gb,
        "get_requests": get_requests,
        "put_requests": put_requests,
        "storage_cost": _round_money(storage_cost),
        "request_cost": _round_money(get_cost + put_cost),
        "monthly_cost": _round_money(monthly),
        "currency": CURRENCY,
    }


def recommend_instance(vcpus: int, memory_gib: int) -> dict[str, Any]:
    """Right-size: return the cheapest instance meeting the vCPU + memory floor."""
    if vcpus < 1 or memory_gib < 1:
        raise ValueError("vcpus and memory_gib must be >= 1")

    candidates = [
        name
        for name, spec in EC2_SPECS.items()
        if spec["vcpus"] >= vcpus and spec["memory_gib"] >= memory_gib
    ]
    if not candidates:
        raise UnknownResourceError(
            f"No catalog instance satisfies vcpus>={vcpus}, memory_gib>={memory_gib}"
        )

    # Cheapest by monthly cost; tie-break by name for determinism.
    best = min(
        candidates,
        key=lambda name: (EC2_HOURLY_USD[name] * HOURS_PER_MONTH, name),
    )
    spec = EC2_SPECS[best]
    monthly = EC2_HOURLY_USD[best] * HOURS_PER_MONTH
    return {
        "recommended_instance_type": best,
        "vcpus": spec["vcpus"],
        "memory_gib": spec["memory_gib"],
        "monthly_cost": _round_money(monthly),
        "currency": CURRENCY,
        "requested": {"vcpus": vcpus, "memory_gib": memory_gib},
    }


def estimate_stack_monthly_cost(spec: dict[str, Any]) -> dict[str, Any]:
    """Aggregate a small architecture's monthly cost.

    ``spec`` may contain any of the keys ``ec2``, ``ebs``, ``s3`` whose values
    are lists of keyword-argument dicts for the matching estimator.
    """
    line_items: list[dict[str, Any]] = []
    for item in spec.get("ec2", []):
        line_items.append(estimate_ec2_monthly_cost(**item))
    for item in spec.get("ebs", []):
        line_items.append(estimate_ebs_monthly_cost(**item))
    for item in spec.get("s3", []):
        line_items.append(estimate_s3_monthly_cost(**item))

    total = sum(li["monthly_cost"] for li in line_items)
    return {
        "line_items": line_items,
        "total_monthly_cost": _round_money(total),
        "currency": CURRENCY,
    }


# ---------------------------------------------------------------------------
# Tool registry: the MCP bridge.
#
# This is the contract Stage 2's agents implement against and the grader checks.
# A tool spec is shaped like an MCP tool: {name, description, inputSchema}.
# ``dispatch(name, arguments)`` is the ``tools/call`` equivalent.
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "estimate_ec2_monthly_cost",
        "description": "Estimate the monthly on-demand cost of EC2 instances.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "instance_type": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "default": 1},
                "hours_per_month": {"type": "number", "default": HOURS_PER_MONTH},
                "region": {"type": "string", "default": "us-west-2"},
            },
            "required": ["instance_type"],
        },
    },
    {
        "name": "estimate_ebs_monthly_cost",
        "description": "Estimate the monthly cost of EBS volumes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "volume_type": {"type": "string"},
                "size_gb": {"type": "number", "minimum": 0},
                "count": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["volume_type", "size_gb"],
        },
    },
    {
        "name": "estimate_s3_monthly_cost",
        "description": "Estimate the monthly cost of S3 storage and requests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "storage_gb": {"type": "number", "minimum": 0},
                "get_requests": {"type": "integer", "minimum": 0, "default": 0},
                "put_requests": {"type": "integer", "minimum": 0, "default": 0},
                "storage_class": {"type": "string", "default": "STANDARD"},
            },
            "required": ["storage_gb"],
        },
    },
    {
        "name": "recommend_instance",
        "description": "Recommend the cheapest instance meeting a vCPU + memory floor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vcpus": {"type": "integer", "minimum": 1},
                "memory_gib": {"type": "integer", "minimum": 1},
            },
            "required": ["vcpus", "memory_gib"],
        },
    },
    {
        "name": "estimate_stack_monthly_cost",
        "description": "Aggregate the monthly cost of a small architecture.",
        "inputSchema": {
            "type": "object",
            "properties": {"spec": {"type": "object"}},
            "required": ["spec"],
        },
    },
]

_HANDLERS = {
    "estimate_ec2_monthly_cost": estimate_ec2_monthly_cost,
    "estimate_ebs_monthly_cost": estimate_ebs_monthly_cost,
    "estimate_s3_monthly_cost": estimate_s3_monthly_cost,
    "recommend_instance": recommend_instance,
    "estimate_stack_monthly_cost": estimate_stack_monthly_cost,
}


def list_tools() -> list[dict[str, Any]]:
    """Return the tool specs (the ``tools/list`` equivalent)."""
    return TOOL_SPECS


def dispatch(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool by name with keyword arguments (the ``tools/call`` equivalent)."""
    if name not in _HANDLERS:
        raise UnknownResourceError(f"Unknown tool: {name!r}")
    return _HANDLERS[name](**(arguments or {}))


if __name__ == "__main__":
    # Tiny manual smoke check: python cost_analyzer.py
    import json

    print(json.dumps(estimate_ec2_monthly_cost("m5.large", count=2), indent=2))
    print(json.dumps(recommend_instance(vcpus=2, memory_gib=8), indent=2))
