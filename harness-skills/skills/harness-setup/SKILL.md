---
name: harness-setup
description: >-
  Umbrella entrypoint for standing up the 3-agent AgentCore coding-agent harness end to end.
  Use when the user says "set up the harness", "configure the agents", "first time setup",
  "deploy the coding agents", "bootstrap the harness", "get the agents running",
  "stand up the harness", "wire up the orchestrator", or "I just cloned this, what do I run".
  Drives the full bring-up: shared infra -> GitHub MCP Gateway -> the three per-agent skills
  (backend / validator / frontend) -> orchestrator run, with a confirm-region/agents gather step
  and a closing smoke-test checklist. Dispatches to configure-claude-code-backend, configure-kiro-validator,
  and configure-codex-frontend rather than duplicating their steps.
---

# Set up the AgentCore coding-agent harness

You are configuring OUR workshop harness: three autonomous coding agents behind a single
orchestrator (deterministic glue around one agentic step).
This is the **umbrella** skill: it sequences the shared infrastructure and then hands each
agent off to its own focused skill. Do not inline the per-agent deploy steps here; dispatch.

## Default role mapping

The role registry declares this default division of labor. `WORKSHOP_ROLES`
can select registered alternatives, including the opencode frontend and Claude
Code validator. Each selected role still has its own builder or checker boundary.

| Agent | Role | Identity model | Per-agent skill |
|---|---|---|---|
| **Claude Code** | **BACKEND**: implements the backend deliverable the task names and exposes it through the AgentCore Gateway | Bedrock native, runtime IAM role has `bedrock:InvokeModel`, **no API key** | `configure-claude-code-backend` |
| **Codex** | **FRONTEND BUILDER**: builds the interface the request needs | Bedrock Runtime Responses, Runtime IAM role through the AWS SDK credential chain | `configure-codex-frontend` |
| **Kiro** | **VALIDATOR**: reads the task and the builders' work, authors a self-contained executable check, the engine runs it, and its real exit code is the gate | Token Vault (AgentCore Identity): **your own `ksk_` key**, fetched at session start, in memory only, never on disk and never a runtime env var | `configure-kiro-validator` |

Framing: this is an **autonomous, fire-and-forget** pipeline. The orchestrator handles the
deterministic work (admission, context hydration, pre-flight, finalization); the three agents
are the agentic step fanned into isolated roles. Each builder gets ONE pull request of its own
against the default branch, checked and reviewed and merged on its own: no combined candidate,
no merge queue, no separate final PR. There is **no race, no winner, no fastest/cheapest
ranking**: every agent has a job and does it.

> Model defaults belong to the registry and per-role configuration. Codex uses
> `WORKSHOP_CODEX_MODEL`; Claude Code uses `WORKSHOP_CLAUDE_MODEL`. The Strands
> coordinator uses `ORCHESTRATOR_MODEL_ID` independently. Do not pass a GPT
> frontend model to the Claude coordinator or a Claude model to Codex.

## Step 1: Gather inputs (region + which agents)

Before running anything, confirm scope with an AskUserQuestion-style prompt. At a staffed
workshop event the shared infra and Gateway are usually **pre-provisioned**; ask so you can
skip Steps 2 to 3 instead of re-deploying.

Ask:

- **Region**: read `AWS_REGION`, `AWS_DEFAULT_REGION`, or the AWS CLI configuration.
  Resolve it before deployment. Claude and the Codex Runtime Responses model must
  be available to the account in that region.
- **Shared infra + Gateway already provisioned?** (typical at an event)
  - Options: `Yes: skip to Step 4 (verify Gateway, then deploy agents)` / `No: I'm starting from scratch (run Steps 2-3)`
- **Which agents to configure?**
  - Options: `Default roster` / `Backend only (Claude Code)` / `Validator only (Kiro)` / `Frontend only (Codex)` / `Custom subset`

Capture the answers; everything below keys off them. Export region once so later commands inherit it:

```bash
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region)}}"
: "${AWS_REGION:?Set the workshop deployment region first}"
aws sts get-caller-identity      # confirm you're in the intended account before deploying
```

## Step 2: Shared infrastructure (deploy ONCE)

> Skip if the user said infra is pre-provisioned. This stands up the shared VPC + S3 Files
> mount that every agent runtime attaches to. Run it exactly once per account/region.

```bash
cd coding-agents/infra
./setup.sh "$AWS_REGION"     # shared VPC + S3 Files
```

Prereqs if this is a truly fresh machine (event boxes already have these):

```bash
pip install -r coding-agents/requirements.txt
pip install awscurl              # used to verify the Gateway in Step 4
gh auth status                   # GitHub MCP server needs an authenticated gh / GitHub App
```

## Step 3: GitHub MCP Gateway (deploy FIRST among the moving parts)

> Skip if pre-provisioned. The Gateway is the single MCP endpoint the backend agent's runtime
> wires into `~/.mcp.json`. It must exist before the agents run, so deploy it before Step 5.

```bash
cd coding-agents/gateway_mcp
export GITHUB_APP_ID="123456"
export GITHUB_APP_PRIVATE_KEY_FILE="/path/to/your-app.private-key.pem"
export GITHUB_APP_INSTALLATION_ID="78901234"
./deploy-all.sh    # stores GitHub creds in Secrets Manager, builds+pushes the MCP container to ECR,
                   # creates the IAM role, AgentCore Runtime (MCP) + Gateway (IAM-auth)
```

Never commit the GitHub App private key, App ID, or installation ID; they are passed by
env/file only.

## Step 4: Verify the Gateway responds (`tools/list`)

Confirm the Gateway is live and brokering GitHub tools before you point agents at it. The URL
is saved to `.deployed-state.json`:

```bash
GATEWAY_URL=$(jq -r '.gateway_url' coding-agents/gateway_mcp/.deployed-state.json)
awscurl --service bedrock-agentcore --region "$AWS_REGION" -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'
```

Expect a JSON-RPC result listing GitHub tools (issues, code, etc.). If this fails, fix the
Gateway before deploying agents; a missing Gateway makes every backend run fail pre-flight.

## Step 5: Dispatch to the per-agent skills (per the Step 1 selection)

Do NOT inline agent deploys here. Invoke the focused skill for each selected agent so the
identity model and model routing stay owned in one place. Use the default roster's
order: backend, frontend, then checker.

1. **BACKEND: Claude Code** -> run skill `configure-claude-code-backend`
   - Bedrock native (`CLAUDE_CODE_USE_BEDROCK=1`), no API key. Roughly:
     ```bash
     cd coding-agents/claude-code && ./setup.sh && python deploy.py
     ```
2. **FRONTEND: Codex** -> run skill `configure-codex-frontend`
   - Bedrock Runtime Responses with the Runtime IAM role; no vendor API key:
     ```bash
     cd coding-agents/codex && ./setup.sh && python3 deploy.py
     ```
3. **VALIDATOR: Kiro** -> run skill `configure-kiro-validator`
   - Token Vault (AgentCore Identity) with the attendee's own `ksk_` key. Roughly:
     ```bash
     cd coding-agents/kiro && KIRO_API_KEY=ksk_xxx ./setup.sh && python deploy.py
     ```
   - No key minted yet? `./setup.sh --skip-identity` builds and deploys keyless, and
     the key is added later on the wired instance in console Settings.

The snippets above are orientation only; the per-agent skill is the source of truth for flags,
model overrides, and identity. If the user picked "All three", you can also fan out the bare
deploys with the repo's batch script, then still run each skill for verification:

```bash
cd coding-agents && ./deploy-all.sh        # builds+deploys all agents; per-agent skills then verify each
```

## Step 6: Point at the orchestrator run

Once the selected agents are deployed and verified, the harness is ready for an autonomous run.
Hand off to the orchestrator (single-chat, fire-and-forget): submit ONE task and the blueprint
runs admission -> context hydration -> pre-flight -> agent execution (the three roles) ->
finalization -> acceptance gate -> PR. See `orchestrator/` in this repo.

The acceptance gate works as follows: after the builder roles complete, the validator role
reads the task (`WORKSHOP_TASK`) and the work (`WORKSHOP_WORK_DIR`), authors one
self-contained executable, and the engine runs it. The check's real exit code decides. A
non-zero exit is ONE bounded retry PER PULL REQUEST, then escalate to a human. Nothing in the repo
pre-encodes what a correct answer looks like; the validator decides based on the deliverable
in front of it.

## Step 7: Smoke-test checklist

Walk this before declaring the harness ready. Each item is a concrete, observable check:

- [ ] **Identity/account**: `aws sts get-caller-identity` shows the intended account, and
      `AWS_REGION` is the region from Step 1.
- [ ] **Shared infra**: `coding-agents/infra/setup.sh` completed (or confirmed pre-provisioned);
      VPC + S3 Files exist.
- [ ] **Gateway live**: Step 4 `tools/list` returns a non-empty tool list with no JSON-RPC error.
- [ ] **Backend (Claude Code)**: `python deploy.py` succeeded; runtime registered; an interactive
      `python connect.py` session opens (verified by `configure-claude-code-backend`).
- [ ] **Validator (Kiro)**: deployed with its Token Vault credential provider (the key in the
      vault, never on the ARN); runtime READY (verified by `configure-kiro-validator`).
- [ ] **Frontend (Codex)**: deployed AND an actual CLI turn verified
      (verified by `configure-codex-frontend`); exercise backend connectivity if
      the request requires it.
- [ ] **End-to-end run**: submit one request to the orchestrator and confirm it reaches a terminal
      state where the validator authored a check, the engine ran it, and the real exit code
      decided (PR opened, or a clear fail-closed reason like `GITHUB_UNREACHABLE` /
      `REPO_NOT_FOUND_OR_NO_ACCESS`).
- [ ] **No secrets committed**: GitHub App key/IDs, account ids, and tokens were passed by env/file
      only and are absent from the working tree. A `ksk_` Kiro key is NEVER committed and never
      echoed into a terminal transcript: it lives in the Token Vault and is read at session start.

If any item fails, fix it (or re-run the owning per-agent skill) before handing off. Cost is a
first-class concern but illustrative here; a small autonomous run is dollars of Bedrock
inference + compute, dominated by tokens, not by Lambda/DynamoDB. Quote the workshop's own
measured per-agent metrics from the run, never vendor "Nx cheaper" claims.

## Teardown (when the user is done)

```bash
python coding-agents/claude-code/cleanup.py
python coding-agents/codex/cleanup.py
python coding-agents/kiro/cleanup.py
cd coding-agents/infra && ./cleanup.sh      # removes VPC + S3 Files (keeps the S3 bucket)
cd coding-agents/gateway_mcp && ./delete-all.sh
```
