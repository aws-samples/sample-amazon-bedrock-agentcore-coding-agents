---
name: configure-claude-code-validator
description: >-
  Configure a second Claude Code agent as the VALIDATOR in our AgentCore harness:
  it reads the task and the builders' work, authors a self-contained executable check,
  the engine runs that check, and its real exit code is the gate. Use when the user
  says "set up the validator", "configure the validation agent", "set up the tests
  agent", "deploy the Claude Code validator", or asks how to configure the acceptance
  gate. LOCKED role mapping in this harness: Claude Code = BACKEND (implements the
  backend), Claude Code validator = VALIDATOR (authors the check, exit code decides),
  opencode = FRONTEND BUILDER. This skill configures ONLY the validator slot. It is
  Bedrock-native (CLAUDE_CODE_USE_BEDROCK=1, IAM bedrock:InvokeModel, NO API key),
  so there is no Token Vault or vendor key step.
---

# Configure Claude Code as the VALIDATOR

You are configuring a second Claude Code coding agent for the workshop's
autonomous harness. Its LOCKED role is **VALIDATOR**: it reads the task and the
work the builder roles produced, authors an executable acceptance check, and the
engine runs that check. The check's real exit code is the gate. The validator does
not build the backend (that is the backend Claude Code) and does not build the
frontend (that is opencode). Stay in lane.

This is an autonomous, fire-and-forget pipeline. There is **no race, no winner,
no fastest/cheapest ranking**: the three roles each do their job and the
orchestrator validates their combined candidate, queues the builder PRs, and opens
the final integration PR.

## Why a second Claude Code fits validation

The validator is steered by its own acceptance-contract `CLAUDE.md`, not an
open-ended build. Claude Code follows a `CLAUDE.md` steering file precisely, which
maps cleanly onto validation: take the task and the builders' work, decide what
"acceptable" means for THIS specific deliverable, and write ONE executable that
probes it. The validator never touches the work it grades (maker is never checker:
that separation is what makes the gate honest).

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

## Step 3: The validator's job - author the check, not answer the task

The validator receives these env vars the orchestrator sets. They are FACTS about the
request and the environment; none of them says what a correct answer looks like.

| Env var | Contents |
|---|---|
| `WORKSHOP_TASK` | the original request (what the user typed) |
| `WORKSHOP_WORK_DIR` | path to the tree the builder roles wrote |
| `DELIVERABLE_URL` | a live URL, when one exists (may be empty) |
| `WORKSHOP_GATE_TIMEOUT_S` | the wall clock the check gets before it is killed |

Size any readiness poll against that budget, and **never below 60 seconds**. The
workspace is a network file mount: a deliverable's first start, which usually installs
dependencies, measures ~47s there against 7s on local disk. Checks that allowed 15-20s
produced red gates on services that were merely still starting. Reject work that
ANSWERS WRONGLY, never work you did not wait for.

With those inputs the validator's job is:

1. Read the task and examine the work. Understand what "acceptable" means for THIS
   specific deliverable - there is no pre-encoded answer anywhere in the repo.
2. Author ONE self-contained executable: a shebang line at the top, then any
   language available in the container. The check must:
   - Really EXERCISE the deliverable (start it if it needs to be running, because
     only the check knows what "running" means for this specific artifact).
   - Exit 0 to accept, non-zero to reject.
   - Print one line per check so the output is readable in the run log.
3. Write that executable as `acceptance_check` at the root of the workspace. The
   engine picks it up by that name; there is nothing to declare (`run.json` belongs to
   the BUILDERS, who use it to say how their deliverable starts).

The engine runs the authored executable and reads its real exit code. That exit
code IS the gate. Nothing else decides.

**CRITICAL: real execution decides; a model's opinion never overrides it.**
The validator authors the check; the ENGINE runs it; the EXIT CODE is the verdict.
If the check exits non-zero, the run is NOT done, regardless of what any model
"thinks". This is "put the LLM in a box": the creative work (building the backend,
building the frontend) is wrapped in a gate that gives the same verdict for the same
behavior every time. The validator may never edit the work it is checking (maker is
never checker: a model that grades its own work grades it generously; a separate
agent with its own container and steering is the only honest judge).

## Step 4: Run the gate

The engine calls the validator role, which AUTHORS the check and stops there. It does
not run it and does not declare the deliverable's entrypoint: `run.json` (with `start`,
and optional `port_env` / `health`) is written by the BUILDER roles, since only they
know how their work starts. The engine then starts what it was told to start, polls
what it was told to poll, runs the authored check, and reads its real exit code.

Keeping those apart is the whole point. A validator that ran its own check would be
grading and reporting in one breath, and the exit code the engine reads is the only
verdict that counts.

To observe the validator's output during development or to test the deployment:

```bash
# Verify the runtime is registered and READY.
python deploy.py            # idempotent; re-run shows current runtime state

# Open an interactive shell into the validator runtime to inspect or debug.
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn coding-agents/claude-code-validator/runtime_config.json)" \
  --region us-west-2
```

**The gate is the validator's authored check, not a repo-side test suite.** There
is no pytest contract, no fixture file, no reference implementation in this repo
that grades an agent's work. The validator decides what to check based on the task
and the deliverable in front of it. A red exit code can never become a pass, and
nothing is ever fabricated.

Iteration is bounded (~2 rounds): if the gate exits non-zero after the bounded
retries, the run escalates to a human rather than looping forever.

## Step 5: Verify and report

Confirm the VALIDATOR slot is live:

```bash
# Runtime registered and READY?
python deploy.py            # idempotent; shows current runtime state
```

Report back: the validator deployed Bedrock-native (no key), model in use, and
runtime status READY. Do not claim completion until you have observed the runtime
state yourself: verify, don't assume.

## Guardrails (stay in the VALIDATOR lane)

- The validator = VALIDATOR ONLY. It authors the check and runs it. It does NOT
  edit the backend (the backend Claude Code's job) or the frontend (opencode's).
  Maker is never checker: this separation is the reason the gate is honest.
- Credential path is **Bedrock-native** (IAM `bedrock:InvokeModel`, no API key,
  nothing on disk). No Token Vault / credential-provider commands here.
- The gate is **the validator's authored executable**. Real execution decides.
  A model's opinion never overrides the exit code.
- No race / no winner framing. The three roles are co-equal, composed into one
  deliverable by the orchestrator. Any cost figures are illustrative; use the
  workshop's own measured run metrics, never vendor "Nx cheaper" claims.
- Extensibility note: to validate a different kind of deliverable, the validator
  reads a different task and writes a different check. Nothing in the harness
  encodes what a correct answer looks like for any specific request.
