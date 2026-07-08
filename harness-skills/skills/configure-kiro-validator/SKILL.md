---
name: configure-kiro-validator
description: >-
  Configure the Kiro agent as the VALIDATOR in our 3-agent AgentCore harness:
  Kiro writes and runs tests and OWNS the acceptance gate. Use when the user
  says "set up Kiro", "configure the validator", "configure the validation
  agent", "set up the tests agent", "wire up Kiro for testing", "make Kiro run
  the acceptance gate", "point Kiro at the grading checks", or asks how to
  deploy Kiro with Token Vault / Identity credentials. LOCKED role mapping in
  this harness: Claude Code = BACKEND (AgentCore MCP server), Kiro = VALIDATOR
  (tests / acceptance gate), Codex = FRONTEND BUILDER (chatbot UI). This skill
  configures ONLY the Kiro = VALIDATOR slot.
---

# Configure Kiro as the VALIDATOR

You are configuring the Kiro coding agent for the workshop's autonomous
3-agent harness. Kiro's LOCKED role is **VALIDATOR**: it writes and runs the
tests and owns the **acceptance gate**: the deterministic "definition of
done" for every autonomous run. Kiro does not build the backend (that is
Claude Code) and does not build the chatbot UI (that is Codex). Stay in lane.

This is an autonomous, fire-and-forget pipeline. There is **no race, no
winner, no fastest/cheapest ranking**: the three agents each do their role
and the orchestrator composes one deliverable. Your job here is only to get
the VALIDATOR slot deployed and pointed at the gate.

## Why a spec-driven agent fits validation

Kiro is spec-driven (it classifies intent into chat / do / spec and works from
requirements). That maps cleanly onto validation: the acceptance gate is a
**fixed contract**, not an open-ended build. The validator's job is to take a
spec (the required tools + their expected behavior) and assert the backend
honors it, exactly the deterministic, requirement-anchored work Kiro is good
at. Keep Kiro anchored to the contract in `usecase-sample-to-mcp/grading/`;
do not let it drift into "improving" the backend.

## Step 1: Gather inputs (AskUserQuestion)

Before running anything, confirm with the user (ask only for what is missing):

- **Kiro API key**: a Token Vault key in the form `ksk_xxx`. Required for the
  Identity path. Ask: "What is your Kiro API key (`ksk_...`)? It is fetched
  on-demand at session start and held in memory only, never written to disk."
- **Region**: default `us-west-2` (all workshop examples use this).
- **Model**: default `auto` (Kiro's router, 1.0x cost baseline). Offer the
  pinned alternatives `claude-opus-4.6`, `claude-sonnet-4.6`,
  `claude-haiku-4.5` if the user wants to override.
- **Prerequisites already met?**: confirm shared infra is deployed
  (`coding-agents/infra/setup.sh us-west-2` runs ONCE for all agents) and the
  GitHub MCP Gateway is up (`gateway_mcp/deploy-all.sh`). If not, that is a
  separate setup step; flag it, do not silently skip it.

If the user has no `ksk_` key yet, do NOT invent one and do NOT fall back to
writing a key to disk. Stop and ask.

## Step 2: Deploy Kiro via the Token Vault (Identity) credential path

Kiro authenticates through AgentCore **Identity / Token Vault**. The key is
provided once to `setup.sh`, stored in the vault, then fetched **on-demand at
session start and held in memory only, never persisted to disk** in the
runtime. This is the security-by-default posture: the agent never sees a
long-lived secret on its filesystem.

```bash
cd coding-agents/kiro

# Provide the Token Vault key inline; setup.sh registers it with Identity,
# builds the arm64 image, and pushes to ECR. The key lives in the vault,
# not on disk in the runtime.
KIRO_API_KEY=ksk_xxx ./setup.sh

# Register / update the AgentCore Runtime (VPC, S3 Files mount, IAM).
python deploy.py
```

Alternatives for `setup.sh`:

```bash
./setup.sh                 # interactive prompt for the key (no inline secret)
./setup.sh --skip-identity # only if Identity was already provisioned out-of-band
```

Default model is `auto`. To pin a model for this validator deployment, pass it
through Kiro's normal model override (router accepts `claude-opus-4.6`,
`claude-sonnet-4.6`, `claude-haiku-4.5`). Validation is read-and-assert work,
so a mid-tier model (`auto` / sonnet) is usually the right cost/quality
trade-off; reserve opus for genuinely tricky contract reasoning.

Do NOT run Codex's credential steps here. `create-workload-identity` and
`create-api-key-credential-provider` belong to the Codex (FRONTEND BUILDER)
slot, not Kiro. Kiro's credential path is Token Vault only.

## Step 3: Point Kiro at the deterministic acceptance gate

The acceptance gate already exists and is **deterministic**: it is the source
of truth Kiro validates against. It lives at:

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

Tell Kiro to **align its tests to this contract**: same tool names, same
expected behaviors, same three dimensions. Kiro's spec-driven output should
mirror `REQUIRED_TOOLS` and the three checks, not introduce a parallel,
divergent test suite. The contract is the spec; Kiro fills it in and exercises
it against the live backend Claude Code produced.

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
# imports the server module directly. Use this for the cheap left-shifted
# check before anything is deployed.
pytest usecase-sample-to-mcp/grading/
```

**CRITICAL: the gate is pytest, not an LLM judge.** Pass/fail is decided by
deterministic assertions in `test_mcp_contract.py`, not by a model's opinion.
This is "put the LLM in a box": the creative loops (Claude builds the backend,
Codex builds the UI, Kiro authors tests) are wrapped in a deterministic gate
that gives the same verdict every time. Never substitute a model's judgment
for `10 passed`. If a model "thinks it looks correct" but pytest is red, the
run is NOT done.

`10 passed` is the autonomous **definition of done**. Iteration is bounded
(~2 rounds): if the gate is still red after the bounded retries, the run
escalates to a human rather than looping forever; long-running agents WILL
fail, and the platform must drive every task to a terminal state regardless.

## Step 5: Verify and report

Confirm the VALIDATOR slot is live and reporting correctly:

```bash
# Runtime registered?
python deploy.py            # idempotent; re-run shows current runtime state

# Gate green over the wire?
MCP_ENDPOINT_URL="https://<deployed-mcp-endpoint>" \
  pytest usecase-sample-to-mcp/grading/ -q
```

Report back: Kiro deployed via Token Vault (key in vault, in-memory only),
model in use (`auto` or the pinned override), and the gate result
(`10 passed` = green, definition of done met). Do not claim completion until
you have observed the pytest result yourself: verify, don't assume.

## Guardrails (stay in the VALIDATOR lane)

- Kiro = VALIDATOR ONLY. It authors/runs tests and owns the gate. It does NOT
  edit the backend MCP server (Claude Code's job) or the chatbot UI (Codex's).
- Credential path is **Token Vault** (`KIRO_API_KEY=ksk_xxx ./setup.sh`).
  In-memory only, never on disk. No Codex Identity commands here.
- The gate is **pytest**, deterministic, no LLM judge. `10 passed` is the only
  green.
- No race / no winner framing. The three agents are co-equal roles composed
  into one deliverable by the orchestrator. Any cost figures are illustrative
  orders of magnitude; use the workshop's own measured run metrics, never
  vendor "Nx cheaper" claims.
- Extensibility note: the contract in `grading/` is the swappable interface:
  to validate a different backend, change `REQUIRED_TOOLS` and the three check
  bodies, not the harness. Extend behind the interface; don't fork the core.
