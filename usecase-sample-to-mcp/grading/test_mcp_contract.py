"""pytest entrypoint for the deterministic grading contract.

Run locally (pre-deploy, in-process):
    pytest src/usecase-sample-to-mcp/grading/ -v

Run against a deployed endpoint (Stage 2, set the env var):
    MCP_ENDPOINT_URL=https://<gateway>/... pytest src/usecase-sample-to-mcp/grading/ -v

This is the "no LLM judge" check the orchestrator shells out to per agent.
"""

from __future__ import annotations

import os

import pytest

from adapters import InProcessClient, RemoteMCPClient
from contract import CASES, CHECKS, REQUIRED_TOOLS, grade


def _client():
    url = os.environ.get("MCP_ENDPOINT_URL")
    if url:
        return RemoteMCPClient(url, region=os.environ.get("AWS_REGION", "us-west-2"))
    return InProcessClient()


@pytest.fixture
def client():
    return _client()


@pytest.mark.parametrize("check_id,check_fn", CHECKS, ids=[c[0] for c in CHECKS])
def test_individual_check(client, check_id, check_fn):
    ok, detail = check_fn(client)
    assert ok, f"[{check_id}] {detail}"


def test_all_required_tools_present(client):
    names = {t["name"] for t in client.list_tools()}
    assert REQUIRED_TOOLS.issubset(names), f"missing: {REQUIRED_TOOLS - names}"


@pytest.mark.parametrize(
    "name,args,key,expected",
    CASES,
    ids=[f"{c[0]}-{c[2]}" for c in CASES],
)
def test_case_value(client, name, args, key, expected):
    result = client.call_tool(name, args)
    assert key in result, f"{name}: missing {key}"
    actual = result[key]
    if isinstance(expected, float):
        assert abs(actual - expected) <= 0.01, f"{name}: {actual} != {expected}"
    else:
        assert actual == expected


def test_overall_grade_passes(client):
    verdict = grade(client)
    assert verdict["passed"], verdict["checks"]
