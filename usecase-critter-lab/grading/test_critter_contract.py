"""pytest entrypoint for the deterministic Critter Lab grading contract.

Run locally (pre-deploy, in-process):
    pytest src/usecase-critter-lab/grading/ -v

Run against a deployed endpoint (Stage 2, set the env var):
    MCP_ENDPOINT_URL=https://<gateway>/... pytest src/usecase-critter-lab/grading/ -v

This is the "no LLM judge" check the orchestrator shells out to per agent.
"""

from __future__ import annotations

import os

import pytest

# Loaded under unique module names by conftest.py so this grading folder never shadows
# the bare ``contract``/``adapters`` names the orchestrator engine imports at runtime.
from conftest import critter_adapters, critter_contract

InProcessClient = critter_adapters.InProcessClient
RemoteMCPClient = critter_adapters.RemoteMCPClient
CASES = critter_contract.CASES
CHECKS = critter_contract.CHECKS
REQUIRED_TOOLS = critter_contract.REQUIRED_TOOLS
grade = critter_contract.grade

import critter_lab


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
    assert result[key] == expected, f"{name}: {result[key]!r} != {expected!r}"


def test_overall_grade_passes(client):
    verdict = grade(client)
    assert verdict["passed"], verdict["checks"]


# --- Determinism is the whole premise of this module: assert it directly, in-process. ---


def test_generate_critter_is_deterministic():
    """Same name twice -> byte-for-byte identical critter (sha256-derived, no clock)."""
    first = critter_lab.generate_critter("sparky")
    second = critter_lab.generate_critter("sparky")
    assert first == second
    # Normalization: spacing/case change only the echoed `name`, never the derived
    # fields (species/element/stats/palette/rarity all come from the normalized hash).
    normalized = critter_lab.generate_critter("  SPARKY ")
    assert {k: v for k, v in normalized.items() if k != "name"} == {
        k: v for k, v in first.items() if k != "name"
    }


def test_different_names_give_different_stats():
    """Two different names must not collapse to the same stat block."""
    a = critter_lab.generate_critter("sparky")
    b = critter_lab.generate_critter("bubbles")
    assert a["stats"] != b["stats"]
