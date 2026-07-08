"""Starter agent: DELIBERATELY INEFFICIENT. The Agentic Efficiency Lab's patient.

A minimal Bedrock `converse` tool-use agent that answers AWS cost questions with the
same `cost_analyzer` skill used everywhere else in this workshop. It works: and it
wastes money on purpose. Every numbered inefficiency below maps to one row of the
lab's enhancement table; you dispatch fixes through the Stage 2 orchestrator and the
validator proves each one with a test under tests/.

Run it (needs AWS credentials with Bedrock access, e.g. the workshop account):

    python3 src/starter-agent/agent.py "What does running 3 m5.large cost monthly?"

Built-in inefficiencies (the lab menu):
  1. No prompt caching       : nothing in the request is marked cacheable.
  2. (after #1 lands) fixed cache TTL rather than peak/off-peak.
  3. Single-region model id  : us.* inference profile, never the global.* one.
  4. Extended thinking ALWAYS: reasoning enabled on every call, even "2+2".
  5. No few-shot examples    : and (5b) a system prompt too short to cache.
  6. Weak tool specs         : one-line descriptions, no examples, loose schema.
  7. No structured output    : tools return prose, the model re-parses it.
  8. No sub-agent summarizer : the price-sheet tool dumps everything upstream.
  9. Noisy stdout            : debug logging is fed back into the model as text.
"""

from __future__ import annotations

import json
import os
import sys

import boto3

# (3) Pinned single-region inference profile. The fix is global cross-region
# inference (global.anthropic...), which routes around regional load.
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "us-west-2"

# (5b) Too short to ever reach the prompt-caching minimum token threshold -
# even after a cachePoint is added, this prompt alone won't cache.
SYSTEM_PROMPT = "You answer AWS cost questions using the tools."

# (6) Weak tool specs: terse descriptions, no usage examples, untyped output.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "usecase-sample-to-mcp"))
import cost_analyzer  # noqa: E402

TOOLS = [
    {"toolSpec": {
        "name": "estimate_ec2_monthly_cost",
        "description": "ec2 cost",  # one fix: real description + examples
        "inputSchema": {"json": {"type": "object", "properties": {
            "instance_type": {"type": "string"},
            "count": {"type": "integer"},
        }}},
    }},
    {"toolSpec": {
        "name": "list_price_sheet",
        "description": "prices",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }},
]


def _run_tool(name: str, args: dict) -> str:
    # (9) Debug noise printed AND returned: every byte of it becomes input
    # tokens on the next model call.
    print(f"[debug] tool={name} args={json.dumps(args)} pid={os.getpid()}")
    if name == "estimate_ec2_monthly_cost":
        out = cost_analyzer.dispatch(name, args)
        # (7) Prose, not structured output: the model has to re-parse this.
        return f"[debug] dispatch ok\nThe answer is: {json.dumps(out)}"
    if name == "list_price_sheet":
        # (8) The full price sheet, verbatim, into the parent context. The fix
        # is a sub-agent (or code) that summarizes before it reaches the model.
        rows = [cost_analyzer.dispatch("estimate_ec2_monthly_cost",
                                       {"instance_type": t, "count": 1})
                for t in cost_analyzer.EC2_HOURLY_USD]
        return "\n".join(json.dumps(r) for r in rows)
    return f"unknown tool {name}"


def ask(question: str) -> str:
    client = boto3.client("bedrock-runtime", region_name=REGION)
    messages = [{"role": "user", "content": [{"text": question}]}]
    while True:
        resp = client.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],   # (1) nothing marked cacheable
            messages=messages,
            toolConfig={"tools": TOOLS},
            # (4) Reasoning forced on for EVERY call, hard or trivial.
            additionalModelRequestFields={
                "thinking": {"type": "enabled", "budget_tokens": 4096}},
        )
        usage = resp.get("usage", {})
        print(f"[debug] usage={json.dumps(usage)}")  # (9) more stdout noise
        msg = resp["output"]["message"]
        messages.append(msg)
        if resp.get("stopReason") != "tool_use":
            return "".join(c.get("text", "") for c in msg["content"])
        results = []
        for block in msg["content"]:
            if "toolUse" in block:
                tu = block["toolUse"]
                results.append({"toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": _run_tool(tu["name"], tu.get("input") or {})}],
                }})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What does running 3 m5.large cost monthly?"
    print(ask(q))
