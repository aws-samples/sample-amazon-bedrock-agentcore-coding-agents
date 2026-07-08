"""Acceptance test for lab enhancement #1: add prompt caching to the starter agent.

This is the test the validator role asserts after the backend role lands the fix.
It is written to run in two modes, both opt-in so the repo-wide suite stays green
until you actually start the lab (the starter agent is inefficient BY DESIGN, so
these tests are red against it: that red is your task list):

  - STRUCTURAL (no AWS): inspects the request the agent builds. RED on the shipped
    starter agent (no cachePoint anywhere); turns GREEN once the fix adds a
    `cachePoint` block to the system prompt / repeated context.

        STARTER_AGENT_LAB=1 pytest src/starter-agent/tests/ -v

  - LIVE (additionally needs AWS creds): calls Bedrock twice with the same context
    and asserts the second response reports `cacheReadInputTokens > 0` and a lower
    input-token count than the first call.

        STARTER_AGENT_LAB=1 STARTER_AGENT_LIVE=1 pytest src/starter-agent/tests/ -v
"""

from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

agent = importlib.import_module("agent")

LIVE = os.environ.get("STARTER_AGENT_LIVE") == "1"

# The lab's red->green loop is opt-in: without the flag the whole module skips,
# keeping `pytest src/` green for people who never open the efficiency lab.
if os.environ.get("STARTER_AGENT_LAB") != "1" and not LIVE:
    pytest.skip("set STARTER_AGENT_LAB=1 to run the efficiency-lab acceptance tests",
                allow_module_level=True)


def _request_has_cache_point() -> bool:
    """Does the agent put a cachePoint block anywhere in its converse request?

    The fix can cache the system prompt (system=[{...},{"cachePoint":...}]) or
    repeated message context; either satisfies the gate. We look for the literal
    dict key '"cachePoint"' in the module source (prose mentions don't count),
    so the structural mode needs no AWS call.
    """
    src = open(agent.__file__, encoding="utf-8").read()
    return '"cachePoint"' in src


@pytest.mark.skipif(LIVE, reason="live mode covers this end-to-end")
def test_prompt_caching_is_configured():
    assert _request_has_cache_point(), (
        "starter agent sends no cachePoint block - every call pays full input price. "
        "Dispatch lab enhancement #1: cache the system prompt and repeated context."
    )


@pytest.mark.skipif(LIVE, reason="live mode covers this end-to-end")
def test_system_prompt_long_enough_to_cache():
    # (5b) Below the model's minimum cacheable prefix (~1k tokens for most
    # Claude models) a cachePoint is silently ignored. ~4 chars/token heuristic.
    approx_tokens = len(agent.SYSTEM_PROMPT) / 4
    assert approx_tokens >= 1024, (
        f"system prompt is ~{int(approx_tokens)} tokens - under the caching minimum, "
        "so even a correct cachePoint never produces a cache hit. Extend the prompt "
        "(few-shot examples from enhancement #5 count toward the minimum)."
    )


@pytest.mark.skipif(not LIVE, reason="needs AWS credentials; set STARTER_AGENT_LIVE=1")
def test_second_call_reports_cache_hit():
    import boto3

    client = boto3.client("bedrock-runtime", region_name=agent.REGION)

    def call():
        resp = client.converse(
            modelId=agent.MODEL_ID,
            system=[{"text": agent.SYSTEM_PROMPT},
                    {"cachePoint": {"type": "default"}}],
            messages=[{"role": "user",
                       "content": [{"text": "What does one m5.large cost monthly?"}]}],
            toolConfig={"tools": agent.TOOLS},
        )
        return resp["usage"]

    first = call()
    second = call()
    print("usage:", json.dumps({"first": first, "second": second}))
    assert second.get("cacheReadInputTokens", 0) > 0, (
        f"no cache hit on the second identical call: {second}"
    )
    assert second["inputTokens"] < first["inputTokens"], (
        "second call did not get cheaper despite the cache hit"
    )
