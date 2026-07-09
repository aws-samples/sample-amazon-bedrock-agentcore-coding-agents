---
name: configure-claude-code-validator
description: >-
  Configure a second Claude Code agent as the VALIDATOR in our AgentCore harness:
  it runs the acceptance gate (the deterministic pytest contract) that OWNS the
  "definition of done". Use when the user says "set up the validator", "configure
  the validation agent", "set up the tests agent", "deploy the Claude Code
  validator", "point the validator at the grading checks", or asks how to run the
  acceptance gate. LOCKED role mapping in this harness: Claude Code = BACKEND
  (AgentCore MCP server), Claude Code validator = VALIDATOR (acceptance gate),
  opencode = FRONTEND BUILDER (chatbot UI). This skill configures ONLY the
  validator slot. It is Bedrock-native (CLAUDE_CODE_USE_BEDROCK=1, IAM
  bedrock:InvokeModel, NO API key), so there is no Token Vault or vendor key step.
---

# Configure Claude Code as the VALIDATOR

You are configuring a second Claude Code coding agent for the workshop's
autonomous harness. Its LOCKED role is **VALIDATOR**: it runs the acceptance
gate, the deterministic "definition of done" for every autonomous run. It does
not build the backend (that is the backend Claude Code) and does not build the
chatbot UI (that is opencode). Stay in lane.

This is an autonomous, fire-and-forget pipeline. There is **no race, no winner,
no fastest/cheapest ranking**: the three roles each do their job and the
orchestrator composes one deliverable. Your job here is only to get the VALIDATOR
slot deployed and pointed at the gate.

## Why a second Claude Code fits validation

The validator is steered by a fixed acceptance contract (the required tools +
their expected behavior), not an open-ended build. Claude Code follows a
CLAUDE.md steering file precisely, which maps cleanly onto validation: take the
contract as the spec and assert the backend honors it. Keep it anchored to the
contract in `usecase-sample-to-mcp/grading/`; do not let it drift into
"improving" the backend.

## Step 1: Gather inputs (AskUserQuestion)

Before running anything, confirm with the user (ask only for what is missing):

- **Region**: default `us-west-2` (all workshop examples use this).
- **Model**: default `us.anthropic.claude-opus-4-6-v1`. Offer the pinned
  alternatives `claude-sonnet-4.6` / `claude-haiku-4.5` if the user wants a
  cheaper validator (validation is read-and-assert work, so a mid-tier model is
  often the right trade-off).
- **Prerequisites already met?**: confirm shared infra is deployed
  (`coding-agents/infra/setup.sh us-west-2` runs ONCE for all agents). No vendor
  key is needed: the validator is Bedrock-native, exactly like the backend.

There is NO API key to gather. The validator authenticates to Bedrock with its
Runtime IAM role; nothing is written to disk.

## Step 2: Deploy the validator (Bedrock-native, no key)

The validator is a second Claude Code container, so it deploys exactly like the
backend, with its own name/ECR repo:

```bash
cd coding-agents/claude-code-validator

# Build the arm64 image and push to ECR (no API key: Bedrock-native).
./setup.sh

# Register / update the AgentCore Runtime (VPC, S3 Files mount, IAM).
python deploy.py
```

Default model is `us.anthropic.claude-opus-4-6-v1`. To pin a cheaper validator,
pass `WORKSHOP_MODEL=...` to `deploy.py`. Do NOT run any Token Vault /
credential-provider steps here: the validator has no vendor key.

## Step 3: Point the validator at the deterministic acceptance gate

The acceptance gate already exists and is **deterministic**: it is the source of
truth the validator runs. It lives at:

```
usecase-sample-to-mcp/grading/
  contract.py            # REQUIRED_TOOLS + the three checks (the spec)
  adapters.py            # in-process vs over-the-wire MCP client
  test_mcp_contract.py   # the pytest harness (10 tests)
```

The contract enforces exactly **three checks** (`CHECKS` in `contract.py`):

| check id            | what it asserts                                                        |
|---------------------|------------------------------------------------------------------------|
| `tool_discovery`    | every tool in `REQUIRED_TOOLS` is discoverable via `tools/list`        |
| `tool_correctness`  | invoking each tool with valid args returns the expected value          |
| `input_validation`  | the server rejects bad / malformed input instead of accepting it       |

The validator's steering (`CLAUDE.md`, carrying the `harness:gate` block) pins it
to this contract. Do not introduce a parallel, divergent test suite: the contract
is the spec; the validator runs it against the live backend the backend Claude
Code produced.

## Step 4: Run the gate (this is the gate, NOT an LLM judge)

The acceptance gate is plain `pytest`. Run it pre-deploy in-process, and again
over the wire against the deployed MCP endpoint.

```bash
# Over-the-wire: point at the deployed backend MCP endpoint.
export MCP_ENDPOINT_URL="https://<deployed-mcp-endpoint>"
pytest usecase-sample-to-mcp/grading/
# expected: 10 passed
```

```bash
# Pre-deploy (in-process): same suite, no live endpoint needed; the adapter
# imports the server module directly.
pytest usecase-sample-to-mcp/grading/
```

**CRITICAL: the gate is pytest, not an LLM judge.** Pass/fail is decided by
deterministic assertions in `test_mcp_contract.py`, not by a model's opinion.
This is "put the LLM in a box": the creative loops (the backend builds the
server, opencode builds the UI, the validator runs the gate) are wrapped in a
deterministic gate that gives the same verdict every time. Never substitute a
model's judgment for `10 passed`. If a model "thinks it looks correct" but pytest
is red, the run is NOT done.

`10 passed` is the autonomous **definition of done**. Iteration is bounded
(~2 rounds): if the gate is still red after the bounded retries, the run escalates
to a human rather than looping forever.

## Step 5: Verify and report

Confirm the VALIDATOR slot is live and reporting correctly:

```bash
# Runtime registered?
python deploy.py            # idempotent; re-run shows current runtime state

# Gate green over the wire?
MCP_ENDPOINT_URL="https://<deployed-mcp-endpoint>" \
  pytest usecase-sample-to-mcp/grading/ -q
```

Report back: the validator deployed Bedrock-native (no key), model in use, and
the gate result (`10 passed` = green, definition of done met). Do not claim
completion until you have observed the pytest result yourself: verify, don't
assume.

## Guardrails (stay in the VALIDATOR lane)

- The validator = VALIDATOR ONLY. It runs the gate. It does NOT edit the backend
  MCP server (the backend Claude Code's job) or the chatbot UI (opencode's).
- Credential path is **Bedrock-native** (IAM `bedrock:InvokeModel`, no API key,
  nothing on disk). No Token Vault / credential-provider commands here.
- The gate is **pytest**, deterministic, no LLM judge. `10 passed` is the only
  green.
- No race / no winner framing. The three roles are co-equal, composed into one
  deliverable by the orchestrator. Any cost figures are illustrative; use the
  workshop's own measured run metrics, never vendor "Nx cheaper" claims.
- Extensibility note: the contract in `grading/` is the swappable interface: to
  validate a different backend, change `REQUIRED_TOOLS` and the three check
  bodies, not the harness. Extend behind the interface; don't fork the core.
