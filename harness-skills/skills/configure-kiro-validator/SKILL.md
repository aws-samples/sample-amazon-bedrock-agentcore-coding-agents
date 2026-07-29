---
name: configure-kiro-validator
description: >-
  Configure the Kiro agent as the VALIDATOR in our 3-agent AgentCore harness:
  Kiro writes and runs tests and OWNS the acceptance gate. Use when the user
  says "set up Kiro", "configure the validator", "configure the validation
  agent", "set up the tests agent", "wire up Kiro for testing", "make Kiro run
  the acceptance gate", "point Kiro at the grading checks", or asks how to
  deploy Kiro with Token Vault / Identity credentials. LOCKED role mapping in
  this harness: Claude Code = BACKEND, Kiro = VALIDATOR (tests / acceptance
  gate), Codex = FRONTEND BUILDER. This skill configures ONLY the Kiro =
  VALIDATOR slot.
---

> **RESTORE PATH - NOT THE SERVED ROLE**
>
> Kiro requires a paid per-user Pro+ subscription and a `ksk_` API key issued
> through the Kiro console. Workshop Studio accounts cannot be issued these keys
> (no per-user subscription, no admin toggle available at event provisioning time).
> The served validator in the workshop is a second Claude Code (see
> `configure-claude-code-validator`), which is Bedrock-native with no key.
>
> Use this skill only if you are restoring the Kiro path in a context where you
> have a valid `ksk_` key and a Kiro Pro+ subscription. All Kiro assets
> (`coding-agents/kiro/`) remain in the repository as the restore path.

# Configure Kiro as the VALIDATOR

You are configuring the Kiro coding agent for the workshop's autonomous
3-agent harness. Kiro's LOCKED role is **VALIDATOR**: it writes and runs the
tests and owns the **acceptance gate**: the deterministic "definition of
done" for every autonomous run. Kiro does not build the backend (that is
Claude Code) and does not build the frontend (that is Codex). Stay in lane.

This is an autonomous, fire-and-forget pipeline. There is **no race, no
winner, no fastest/cheapest ranking**: the three agents each do their role
and the orchestrator validates their combined candidate before queuing the
builder PRs. Your job here is only to get the VALIDATOR slot deployed and
pointed at the gate.

## Why a spec-driven agent fits validation

Kiro is spec-driven (it classifies intent into chat / do / spec and works from
requirements). That maps cleanly onto validation: the acceptance gate is a
**fixed contract**, not an open-ended build. The validator's job is to take the
task and the builders' work, decide what "acceptable" means for this specific
deliverable, and assert the backend honors it. Keep Kiro anchored to the
deliverable at hand; do not let it drift into "improving" the backend.

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
  GitHub MCP Gateway is up (`coding-agents/gateway_mcp/deploy-all.sh`). If not,
  that is a separate setup step; flag it, do not silently skip it.

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

## Step 3: Point Kiro at the acceptance gate

The acceptance gate for this harness is **agentic**: the validator reads the
task (`WORKSHOP_TASK`) and the work the builder roles produced
(`WORKSHOP_WORK_DIR`), decides what "acceptable" means for that specific
deliverable, and authors one self-contained executable check. The engine runs
that executable and its real exit code decides.

Tell Kiro to:
1. Read the task and examine the builders' output.
2. Author ONE self-contained executable (shebang + any language in the
   container) that really exercises the deliverable.
3. Exit 0 to accept, non-zero to reject; print one line per check.

Kiro must NOT edit the backend or frontend it is checking. Maker is never
checker: a model that grades its own work grades it generously; the validator
runs in its own container with its own steering precisely to keep that
separation honest.

## Step 4: Run the gate (the authored check, not an LLM judgment)

**CRITICAL: real execution decides; a model's opinion never overrides the exit
code.** The gate is the validator's authored executable. Pass/fail is the real
exit code the engine reads after running it. If a model "thinks it looks
correct" but the check exits non-zero, the run is NOT done.

The engine invokes the validator role, which authors and runs the check.
Iteration is bounded (~2 rounds): if the gate is still failing after the
bounded retries, the run escalates to a human rather than looping forever.

## Step 5: Verify and report

Confirm the VALIDATOR slot is live and reporting correctly:

```bash
# Runtime registered?
python deploy.py            # idempotent; re-run shows current runtime state
```

Report back: Kiro deployed via Token Vault (key in vault, in-memory only),
model in use (`auto` or the pinned override), and runtime status READY. Do
not claim completion until you have observed the runtime state yourself:
verify, don't assume.

## Guardrails (stay in the VALIDATOR lane)

- Kiro = VALIDATOR ONLY. It authors/runs the check and owns the gate. It does
  NOT edit the backend (Claude Code's job) or the frontend (Codex's).
- Credential path is **Token Vault** (`KIRO_API_KEY=ksk_xxx ./setup.sh`).
  In-memory only, never on disk. No Codex Identity commands here.
- The gate is **the authored executable**. Real execution decides; a model's
  opinion never overrides the exit code.
- No race / no winner framing. The three agents are co-equal roles integrated
  through a gated role-PR queue. Any cost figures are illustrative
  orders of magnitude; use the workshop's own measured run metrics, never
  vendor "Nx cheaper" claims.
- Extensibility note: to validate a different deliverable, the validator reads
  a different task and writes a different check. Nothing in the harness
  pre-encodes what a correct answer looks like for any specific request.
