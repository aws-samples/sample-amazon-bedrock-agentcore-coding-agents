---
name: configure-kiro-validator
description: >-
  Configure the Kiro agent as the VALIDATOR in our AgentCore harness: Kiro reads
  the task and the builders' work, AUTHORS a self-contained executable check, the
  engine runs that check, and its real exit code is the gate. Use when the user
  says "set up Kiro", "configure the validator", "configure the validation
  agent", "deploy the Kiro validator", "wire up Kiro for testing", or asks how to
  deploy Kiro with Token Vault / Identity credentials. LOCKED role mapping in
  this harness: Claude Code = BACKEND (implements the service side), Kiro =
  VALIDATOR (authors the check, exit code decides), opencode = FRONTEND BUILDER.
  This skill configures ONLY the Kiro = VALIDATOR slot. Kiro authenticates with
  the attendee's own ksk_ key through AgentCore Identity / Token Vault.
---

# Configure Kiro as the VALIDATOR

You are configuring the Kiro coding agent for the workshop's autonomous harness.
Kiro's LOCKED role is **VALIDATOR**: it reads the task and the work the builder
roles produced, AUTHORS an executable acceptance check, and the engine runs that
check. The check's real exit code is the gate. Kiro does not build the backend
(that is the backend Claude Code) and does not build the frontend (that is
opencode). Stay in lane.

This is an autonomous, fire-and-forget pipeline. There is **no race, no winner,
no fastest/cheapest ranking**: each role does its job, and each builder gets ONE
pull request of its own against the repository's default branch, which is
checked, reviewed, and merged on its own. There is no combined candidate, no
merge queue, and no separate final PR.

## Why a spec-driven agent fits validation

Kiro is spec-driven (it classifies intent into chat / do / spec and works from
requirements), and it reads `.kiro/steering/*.md` with `inclusion: always` every
turn. That maps cleanly onto validation: take the task and the builders' work,
decide what "acceptable" means for THIS specific deliverable, and write ONE
executable that probes it. Nothing in the repository pre-encodes what a correct
answer looks like, because nobody knew what would be asked. Keep Kiro anchored to
the deliverable in front of it; do not let it drift into "improving" the work it
is grading.

## Step 1: Gather inputs (AskUserQuestion)

Before running anything, confirm with the user (ask only for what is missing):

- **Kiro API key**: the user's OWN key, in the form `ksk_...`. Required for the
  Identity path. The event provisions a per-team Kiro / Amazon Q Developer Pro
  subscription through IAM Identity Center, and the attendee then mints their own
  key. If they do not have one yet, point them at the Prerequisites page **"Get
  Your Kiro API Key"**: sign in at kiro.dev with the event's Identity Center
  start URL and their per-team username and one-time password, then open
  app.kiro.dev -> **Settings > API Keys** -> create a key. Ask: "What is your
  Kiro API key (`ksk_...`)? It is fetched on demand at session start and held in
  memory only, never written to disk."
- **Region**: default `us-west-2` (all workshop examples use this).
- **Model**: default `auto` (Kiro's own router). Note that `kiro-cli` takes no
  model flag: the model is written as `chat.defaultModel` into
  `~/.kiro/settings/cli.json` by the container's `run.sh`, so there is no Bedrock
  model id to select here.
- **Prerequisites already met?**: confirm shared infra is deployed
  (`coding-agents/infra/setup.sh us-west-2` runs ONCE for all agents) and the
  GitHub MCP Gateway is up (`coding-agents/gateway_mcp/deploy-all.sh`). If not,
  that is a separate setup step; flag it, do not silently skip it.

If the user has no `ksk_` key yet, do NOT invent one and do NOT fall back to
writing a key to disk. Stop and ask.

## Step 2: Deploy Kiro via the Token Vault (Identity) credential path

Kiro authenticates through AgentCore **Identity / Token Vault**. The key is
provided once to `setup.sh`, stored in the vault, then fetched **on demand at
session start and held in memory only, never persisted to disk** in the runtime.
`deploy.py` deliberately does NOT inject the key as a runtime environment
variable, because a plaintext env var is readable by anyone who can
`GetAgentRuntime`.

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
./setup.sh --skip-identity # build the image with NO key yet; add the key later
```

`--skip-identity` is the normal event path when the image is built before the
attendee has minted a key: the image and Runtime come up keyless, and the key is
added afterwards on the wired Kiro instance in console **Settings**. A
credential provider is per-account, so even a centrally prebuilt image needs this
step run in the attendee's own account.

`deploy.py` refuses a split-brain region: if the S3 Files access point in
`coding-agents/infra.config` was created in a different region than this deploy
targets, it exits `REGION_MISMATCH` rather than producing a Runtime that cannot
reach `/mnt/s3files`.

Do NOT run Codex's credential steps here. Kiro's credential path is Token Vault
only.

## Step 3: The validator's job - author the check, not answer the task

The validator receives these env vars the orchestrator sets. They are FACTS about
the request and the environment; none of them says what a correct answer looks
like.

| Env var | Contents |
|---|---|
| `WORKSHOP_TASK` | the original request (what the user typed) |
| `WORKSHOP_WORK_DIR` | path to the tree the builder roles wrote |
| `DELIVERABLE_URL` | a live URL, when one exists (may be empty) |
| `WORKSHOP_GATE_TIMEOUT_S` | the wall clock the check gets before it is killed |

Size any readiness poll against that budget, and **never below 60 seconds**. A
deliverable's first start may install declared dependencies and take much longer
than a warm restart. Checks that allowed 15-20s produced red gates on services
that were merely still starting. Reject work that ANSWERS WRONGLY, never work you
did not wait for.

With those inputs the validator's job is:

1. Read the task and examine the work. Understand what "acceptable" means for
   THIS specific deliverable; there is no pre-encoded answer anywhere in the repo.
2. Author ONE self-contained executable: a shebang line at the top, then any
   language available in the container. The check must:
   - Really EXERCISE the deliverable (start it if it needs to be running, because
     only the check knows what "running" means for this specific artifact).
   - Exit 0 to accept, non-zero to reject.
   - Print one line per check so the output is readable in the run log.
3. Write that executable as `acceptance_check` at the root of the workspace. The
   engine picks it up by that name; there is nothing to declare (`run.json`
   belongs to the BUILDERS, who use it to say how their deliverable starts).

**CRITICAL: real execution decides; a model's opinion never overrides it.** Kiro
AUTHORS the check; the ENGINE runs it; the EXIT CODE is the verdict. If the check
exits non-zero, the run is NOT done, regardless of what any model "thinks". Kiro
may never edit the work it is checking (maker is never checker: a model that
grades its own work grades it generously; a separate agent with its own container
and steering is the only honest judge).

## Step 4: Run the gate

The engine calls the validator role, which AUTHORS the check and stops there. It
does not run it and does not declare the deliverable's entrypoint: `run.json`
(with `start`, and optional `port_env` / `health`) is written by the BUILDER
roles, since only they know how their work starts. The engine then starts what it
was told to start, polls what it was told to poll, runs the authored check, and
reads its real exit code.

Keeping those apart is the whole point. A validator that ran its own check would
be grading and reporting in one breath, and the exit code the engine reads is the
only verdict that counts. Kiro may run a parse-only syntax check (`bash -n`,
`python -m py_compile`, `node --check`) before handing the file off.

To observe the validator's output during development or to test the deployment:

```bash
# Verify the runtime is registered and READY.
python deploy.py            # idempotent; re-run shows current runtime state

# Open an interactive shell into the validator runtime to inspect or debug.
agentcore exec --it \
  --runtime "$(jq -r .runtime_arn coding-agents/kiro/runtime_config.json)" \
  --region us-west-2
```

**The gate is the validator's authored check, not a repo-side test suite.** There
is no pytest contract, no fixture file, and no reference implementation in this
repo that grades an agent's work. A red exit code can never become a pass, and
nothing is ever fabricated.

Iteration is bounded (ONE repair round PER PULL REQUEST): if a pull request's gate
is still red after that, it escalates to a human rather than looping forever, and
a red pull request never blocks a green sibling.

## Step 5: Verify and report

Confirm the VALIDATOR slot is live and reporting correctly:

```bash
# Runtime registered and READY?
python deploy.py            # idempotent; re-run shows current runtime state
```

Report back: Kiro deployed with its Token Vault credential provider (key in the
vault, in memory only, never on the ARN), the model in use (`auto` unless
overridden), and runtime status READY. Do not claim completion until you have
observed the runtime state yourself: verify, don't assume.

## Guardrails (stay in the VALIDATOR lane)

- Kiro = VALIDATOR ONLY. It AUTHORS the check and owns the gate. It does NOT edit
  the backend (the backend Claude Code's job) or the frontend (opencode's). Maker
  is never checker: this separation is the reason the gate is honest.
- Credential path is **Token Vault** (`KIRO_API_KEY=ksk_xxx ./setup.sh`). In
  memory only, never on disk, never a runtime env var. Never commit a `ksk_` key
  and never echo one into a terminal transcript or a log.
- The gate is **the authored executable**. Real execution decides; a model's
  opinion never overrides the exit code.
- No race / no winner framing. The roles are co-equal, each with its own pull
  request that is checked, reviewed, and merged on its own. Any cost figures are
  illustrative orders of magnitude; use the workshop's own measured run metrics,
  never vendor "Nx cheaper" claims.
- Extensibility note: to validate a different deliverable, the validator reads a
  different task and writes a different check. Nothing in the harness pre-encodes
  what a correct answer looks like for any specific request.
