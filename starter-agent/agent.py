"""Starter agent: DELIBERATELY INEFFICIENT. The Agentic Efficiency Lab's patient.

A minimal Bedrock `converse` tool-use agent that answers cost questions from a tiny
lookup table it owns (see RATES_PER_HOUR). Self-contained on purpose: the lab is about
this agent's own efficiency, not about any deliverable the workshop builds. It works,
and it wastes money on purpose. Every numbered inefficiency below maps to one row of
the lab's enhancement table; you dispatch fixes through the orchestrator and the
validator proves each one with a test under tests/.

Run it (needs AWS credentials with Bedrock access, e.g. the workshop account):

    python3 starter-agent/agent.py "What do 3 large instances cost monthly?"

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
# WORKSHOP_MODEL is honored so accounts without access to the default model
# (Marketplace subscription gaps) can point the lab at any enabled Claude
# model without editing this file. The default matches the models the
# workshop already requires (Sonnet 4.6 is enabled for opencode/validator).
MODEL_ID = os.environ.get("WORKSHOP_MODEL", "us.anthropic.claude-sonnet-4-6")
REGION = "us-west-2"

# (5b) Too short to ever reach the prompt-caching minimum token threshold -
# even after a cachePoint is added, this prompt alone won't cache.
SYSTEM_PROMPT = "You answer AWS cost questions using the tools."

# The tiny lookup table this agent answers from. Self-contained ON PURPOSE: the lab
# is about the AGENT's efficiency (caching, reasoning, tool specs, context bloat), so
# it must not depend on anything else in the repository and it is not a sample the
# workshop asks anyone to build. Values are illustrative, not real prices.
RATES_PER_HOUR = {
    "small": 0.05,
    "medium": 0.10,
    "large": 0.20,
    "xlarge": 0.40,
}
HOURS_PER_MONTH = 730.0


def monthly_cost(size: str, count: int = 1) -> dict:
    """The one real computation behind both tools. Raises on an unknown size, so a
    bad question can never return a plausible-but-wrong number."""
    if size not in RATES_PER_HOUR:
        raise ValueError(f"unknown size {size!r}; known: {sorted(RATES_PER_HOUR)}")
    rate = RATES_PER_HOUR[size]
    return {"size": size, "count": count, "hourly_rate": rate,
            "monthly_cost": round(rate * count * HOURS_PER_MONTH, 2)}


# (6) Weak tool specs: terse descriptions, no usage examples, untyped output.

TOOLS = [
    {"toolSpec": {
        "name": "estimate_monthly_cost",
        "description": "cost",  # one fix: real description + examples
        "inputSchema": {"json": {"type": "object", "properties": {
            "size": {"type": "string"},
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
    if name == "estimate_monthly_cost":
        out = monthly_cost(args.get("size", ""), int(args.get("count", 1) or 1))
        # (7) Prose, not structured output: the model has to re-parse this.
        return f"[debug] dispatch ok\nThe answer is: {json.dumps(out)}"
    if name == "list_price_sheet":
        # (8) The full price sheet, verbatim, into the parent context. The fix
        # is a sub-agent (or code) that summarizes before it reaches the model.
        rows = [monthly_cost(size) for size in RATES_PER_HOUR]
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
    q = " ".join(sys.argv[1:]) or "What do 3 large instances cost monthly?"
    print(ask(q))
