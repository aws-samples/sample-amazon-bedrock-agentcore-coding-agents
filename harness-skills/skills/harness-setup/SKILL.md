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

## Role mapping (LOCKED, do not reassign)

This harness has a fixed division of labor. Echo it back to the user before you start so the
roles are unambiguous:

| Agent | Role | Identity model | Per-agent skill |
|---|---|---|---|
| **Claude Code** | **BACKEND**: implements the AgentCore MCP server (the deliverable under test) | Bedrock native, runtime IAM role has `bedrock:InvokeModel`, **no API key** | `configure-claude-code-backend` |
| **Kiro** | **VALIDATOR**: runs the pytest acceptance gate / acceptance checks; the definition-of-done | AgentCore Identity / Token Vault; `KIRO_API_KEY` fetched on-demand, in-memory only | `configure-kiro-validator` |
| **Codex** | **FRONTEND BUILDER**: builds the chatbot UI that talks to the backend MCP server | Runtime IAM role through the AWS SDK credential chain | `configure-codex-frontend` |

Framing: this is an **autonomous, fire-and-forget** pipeline. The orchestrator handles the
deterministic work (admission, context hydration, pre-flight, finalization); the three agents
are the single agentic step fanned into three roles and composed into ONE deliverable. There
is **no race, no winner, no fastest/cheapest ranking**: every agent has a job and does it.
The local frontend panel is an *observability* window into the run, not a race UI.

> Per-agent **model routing** is each agent's own concern (Sonnet for new tasks,
> Haiku for read-only review, Opus opt-in for complex repos). The umbrella skill only confirms
> region and which agents to configure; the per-agent skills own model selection. New agent
> types (cursor, hermes, open-code) extend the harness the same way: add a sibling skill, keep
> this dispatch table the contract.

## Step 1: Gather inputs (region + which agents)

Before running anything, confirm scope with an AskUserQuestion-style prompt. At a staffed
workshop event the shared infra and Gateway are usually **pre-provisioned**; ask so you can
skip Steps 2 to 3 instead of re-deploying.

Ask:

- **Region**: default `us-west-2`. All commands in the base repos assume it; Bedrock model
  access (Claude Opus/Sonnet/Haiku, plus GPT via OpenAI-on-Bedrock for Codex) must be enabled there.
  - Options: `us-west-2 (recommended)` / `other (specify)`
- **Shared infra + Gateway already provisioned?** (typical at an event)
  - Options: `Yes: skip to Step 4 (verify Gateway, then deploy agents)` / `No: I'm starting from scratch (run Steps 2-3)`
- **Which agents to configure?**
  - Options: `All three (backend + validator + frontend)` / `Backend only (Claude Code)` / `Validator only (Kiro)` / `Frontend only (Codex)` / `Custom subset`

Capture the answers; everything below keys off them. Export region once so later commands inherit it:

```bash
export AWS_REGION="us-west-2"   # or the region the user chose
aws sts get-caller-identity      # confirm you're in the intended account before deploying
```

## Step 2: Shared infrastructure (deploy ONCE)

> Skip if the user said infra is pre-provisioned. This stands up the shared VPC + S3 Files
> mount that every agent runtime attaches to. Run it exactly once per account/region.

```bash
cd coding-agents/infra
./setup.sh us-west-2          # shared VPC + S3 Files; idempotent-ish, but don't double-run needlessly
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
cd gateway_mcp
export GITHUB_APP_ID="123456"
export GITHUB_APP_PRIVATE_KEY_FILE="/path/to/your-app.private-key.pem"
export GITHUB_APP_INSTALLATION_ID="78901234"
export AWS_REGION="us-west-2"
./deploy-all.sh    # stores GitHub creds in Secrets Manager, builds+pushes the MCP container to ECR,
                   # creates the IAM role, AgentCore Runtime (MCP) + Gateway (IAM-auth)
```

Never commit the GitHub App private key, App ID, or installation ID; they are passed by
env/file only.

## Step 4: Verify the Gateway responds (`tools/list`)

Confirm the Gateway is live and brokering GitHub tools before you point agents at it. The URL
is saved to `.deployed-state.json`:

```bash
GATEWAY_URL=$(jq -r '.gateway_url' .deployed-state.json)
awscurl --service bedrock-agentcore --region "$AWS_REGION" -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'
```

Expect a JSON-RPC result listing GitHub tools (issues, code, etc.). If this fails, fix the
Gateway before deploying agents; a missing Gateway makes every backend run fail pre-flight.

## Step 5: Dispatch to the per-agent skills (per the Step 1 selection)

Do NOT inline agent deploys here. Invoke the focused skill for each selected agent so the
identity model and model routing stay owned in one place. Suggested order: backend first
(it's the deliverable under test), then validator (it gates that deliverable), then frontend:

1. **BACKEND: Claude Code** → run skill `configure-claude-code-backend`
   - Bedrock native (`CLAUDE_CODE_USE_BEDROCK=1`), no API key. Roughly:
     ```bash
     cd coding-agents/claude-code && ./setup.sh && python deploy.py
     ```
2. **VALIDATOR: Kiro** → run skill `configure-kiro-validator`
   - Token Vault; key fetched on demand, in-memory only. Roughly:
     ```bash
     cd coding-agents/kiro
     KIRO_API_KEY=ksk_xxx ./setup.sh      # or interactive ./setup.sh ; or --skip-identity
     python deploy.py
     ```
3. **FRONTEND: Codex** → run skill `configure-codex-frontend`
   - Runtime IAM role; no OpenAI key or Token Vault provider. Roughly:
     ```bash
     cd coding-agents/codex && ./setup.sh && python deploy.py
     ```

The snippets above are orientation only; the per-agent skill is the source of truth for flags,
model overrides, and identity. If the user picked "All three", you can also fan out the bare
deploys with the repo's batch script, then still run each skill for verification:

```bash
cd coding-agents && ./deploy-all.sh        # builds+deploys all agents; per-agent skills then verify each
```

## Step 6: Point at the orchestrator run

Once the selected agents are deployed and verified, the harness is ready for an autonomous run.
Hand off to the orchestrator (single-chat, fire-and-forget): submit ONE task and the blueprint
runs admission → context hydration → pre-flight → agent execution (the three roles) →
finalization → acceptance gate → PR. See `orchestrator/` in this repo.

Optionally bring up the local observability panel to *watch* the agents work (not a race UI):

```bash
cd coding-agents/frontend && pip install -r requirements.txt && python app.py
# http://127.0.0.1:5050 is multi-pane; reads runtime_config.json from each agent folder
```

The acceptance gate (Kiro's validator role) is:

```bash
MCP_ENDPOINT_URL="<deployed-backend-mcp-endpoint>" pytest usecase-sample-to-mcp/grading/
# 10 tests; in-process pre-deploy, over-the-wire when deployed. No LLM judge; this is the
# autonomous "definition of done", with bounded iteration (~2 rounds, then escalate to a human).
```

## Step 7: Smoke-test checklist

Walk this before declaring the harness ready. Each item is a concrete, observable check:

- [ ] **Identity/account**: `aws sts get-caller-identity` shows the intended account, and
      `AWS_REGION` is the region from Step 1.
- [ ] **Shared infra**: `coding-agents/infra/setup.sh` completed (or confirmed pre-provisioned);
      VPC + S3 Files exist.
- [ ] **Gateway live**: Step 4 `tools/list` returns a non-empty tool list with no JSON-RPC error.
- [ ] **Backend (Claude Code)**: `python deploy.py` succeeded; runtime registered; an interactive
      `python connect.py` session opens (verified by `configure-claude-code-backend`).
- [ ] **Validator (Kiro)**: deployed with Token Vault identity; the acceptance gate
      `pytest usecase-sample-to-mcp/grading/` runs (verified by `configure-kiro-validator`).
- [ ] **Frontend (Codex)**: deployed AND `create-workload-identity` + `create-api-key-credential-provider`
      ran (verified by `configure-codex-frontend`); the chatbot UI reaches the backend MCP endpoint.
- [ ] **Acceptance gate wired**: `MCP_ENDPOINT_URL` points at the deployed backend; the 10
      grading tests are collectable.
- [ ] **Orchestrator**: a single test task submitted to `orchestrator/` reaches a terminal
      state (PR opened or a clear fail-closed reason like `GITHUB_UNREACHABLE` / `REPO_NOT_FOUND_OR_NO_ACCESS`).
- [ ] **No secrets committed**: GitHub App key/IDs, `KIRO_API_KEY`, `OPENAI_API_KEY`, account ids,
      and tokens were passed by env/file only and are absent from the working tree.

If any item fails, fix it (or re-run the owning per-agent skill) before handing off. Cost is a
first-class concern but illustrative here; a small autonomous run is dollars of Bedrock
inference + compute, dominated by tokens, not by Lambda/DynamoDB. Quote the workshop's own
measured per-agent metrics from the run, never vendor "Nx cheaper" claims.

## Teardown (when the user is done)

```bash
cd coding-agents && ./cleanup_all.sh
cd coding-agents/infra && ./cleanup.sh      # removes VPC + S3 Files (keeps the S3 bucket)
cd gateway_mcp && ./delete-all.sh
```
