---
name: configure-claude-code-backend
description: >-
  Configure the Claude Code agent as the BACKEND builder in the 3-agent AgentCore
  coding harness: the agent that implements whatever backend the task's contract names
  and exposes it through the AgentCore Gateway. Use this when the user says "set up
  Claude Code", "configure the backend agent", "configure the backend MCP agent",
  "build the backend", "deploy claude-code", "point Claude Code at the task",
  or asks which agent owns the server/tools side.
  Claude Code runs Bedrock-native (CLAUDE_CODE_USE_BEDROCK=1, IAM bedrock:InvokeModel,
  NO API key) on default model us.anthropic.claude-opus-4-6-v1. Opus suits the
  multi-file backend work. Do NOT use this for the Claude Code validator or opencode
  (frontend builder); those have their own configure skills.
---

# Configure Claude Code: Backend Builder

This skill configures **Claude Code** for its LOCKED role in our 3-agent autonomous
harness. Roles do not rotate:

| Agent | Role | Owns |
|---|---|---|
| **Claude Code** (this skill) | **BACKEND** | implements the backend deliverable the task names and exposes it behind the Gateway |
| Claude Code (validator) | VALIDATOR | authors the acceptance check; its exit code decides "done" |
| opencode | FRONTEND BUILDER | builds the interface that calls the backend |

This is **not** a race and there is **no winner**. The three agents are the single
agentic step of the orchestration blueprint, fanned into three roles and composed into ONE
deliverable in finalization (admission -> context hydration -> pre-flight -> agent
execution -> finalization). Claude Code's job is to produce the backend so the
validator's authored check can exit 0.

Why Claude Code is the backend: this role is multi-file, contract-driven server work.
Per per-task model routing, the most capable model is the right call for complex/critical
work; Opus recognizes rabbit holes and self-corrects, where mid-tier models persist in
unproductive loops. That is why the default model here is `us.anthropic.claude-opus-4-6-v1`
and why Claude Code, not the Claude Code validator or opencode, owns the server.

---

## Step 1: Gather inputs (AskUserQuestion)

Confirm before touching AWS. Ask the user (AskUserQuestion-style); accept defaults if
they say "use defaults":

1. **AWS region**: default `us-west-2` (all base-repo examples assume this).
2. **Model id**: default `us.anthropic.claude-opus-4-6-v1` (the cross-region id seen
   in the repo). Override only if the user wants e.g. `global.anthropic.claude-opus-4-6-v1`.
   Do NOT downgrade to Sonnet/Haiku for this role; backend work is the Opus opt-in case.
3. **Has shared infra + Gateway been deployed yet?** This skill assumes:
   - `coding-agents/infra` (shared VPC + S3 Files) is up, and
   - `coding-agents/gateway_mcp/deploy-all.sh` has produced a Gateway URL.
   If not, point them at those steps (Step 2) before deploying this agent.

Then state the locked role back to the user: "Claude Code = BACKEND. It will implement
whatever backend the task's contract names and expose it through the Gateway."

---

## Step 2: Verify prerequisites are deployed

The backend agent cannot reach its tools without shared infra and the Gateway. Confirm
both first.

```bash
# Shared infra (VPC + S3 Files); deploy ONCE for all agents, not per-agent.
cd coding-agents/infra
./setup.sh us-west-2

# Gateway (the single IAM-auth MCP endpoint the agent is pointed at); deploy FIRST.
cd coding-agents/gateway_mcp
export AWS_REGION="us-west-2"
./deploy-all.sh
# Gateway URL is written to coding-agents/gateway_mcp/.deployed-state.json:
GATEWAY_URL=$(jq -r '.gateway_url' coding-agents/gateway_mcp/.deployed-state.json)
echo "$GATEWAY_URL"
```

This is the Bedrock-native, **no-API-key** path. The runtime IAM role carries
`bedrock:InvokeModel`; there is NO key in env, no Token Vault, no credential provider.
(The Claude Code validator uses the same Bedrock-native path: `CLAUDE_CODE_USE_BEDROCK=1`,
runtime IAM role, no API key. opencode likewise uses its Runtime IAM role for Bedrock.)

---

## Step 3: Build and deploy the Claude Code agent

```bash
cd coding-agents/claude-code
./setup.sh        # builds the arm64 image, pushes to ECR
python deploy.py  # registers/updates the AgentCore Runtime (VPC, S3 Files mount, IAM role)
```

What `deploy.py` wires up (do not re-create it by hand):
- The runtime IAM role gets `bedrock:InvokeModel`; this is the credential path.
- `run.sh` inside the microVM generates `~/.mcp.json` (pointing at the Gateway MCP
  endpoint), sets `CLAUDE_CODE_USE_BEDROCK=1`, and launches
  `claude --dangerously-skip-permissions --model us.anthropic.claude-opus-4-6-v1`.
- Persistent `/mnt/s3files` is the S3 Files / managed session storage mount.

Sanity-check the Bedrock-native config that makes this the no-key path:

```bash
# These are set by run.sh inside the runtime; confirm the intent in the agent folder.
grep -n "CLAUDE_CODE_USE_BEDROCK" coding-agents/claude-code/run.sh
grep -n "InvokeModel" coding-agents/claude-code/deploy.py
# Expect: CLAUDE_CODE_USE_BEDROCK=1 and an IAM statement granting bedrock:InvokeModel.
# Expect: NO OPENAI_API_KEY / api-key-credential-provider anywhere here.
```

---

## Step 4: Point Claude Code at the task

The task comes from `WORKSHOP_TASK` (set by the orchestrator when dispatching). The backend's
job is to implement whatever the task's contract names - the specific tools, API surface,
or service described in the request - and expose it behind the AgentCore Gateway so the
validator can probe it.

Drive the agent interactively for development or to hand it a specific task:

```bash
cd coding-agents/claude-code
python connect.py --prompt "Apply the task: implement the backend as specified, expose it behind the Gateway, and confirm it is reachable."
# or resume a session:
python connect.py --session <session-id>
```

The agent decides the files, language, and structure. The only hard requirement on the
backend lane is: before handing off, the backend must confirm its own work starts and
is reachable at the documented endpoint. It does NOT write the validator's check and
does NOT build the frontend UI.

---

## Step 5: Backend self-check (pre-handoff)

The acceptance gate is **owned by the Claude Code validator**, which authors its own
executable check after examining the deliverable. The backend agent should do a
lightweight self-check first so it does not hand a non-starting or unreachable service
to the gate.

```bash
GATEWAY_URL=$(jq -r '.gateway_url' coding-agents/gateway_mcp/.deployed-state.json)

# Confirm the Gateway responds (tools/list is the minimal liveness probe).
awscurl --service bedrock-agentcore --region us-west-2 -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'
```

A non-empty JSON-RPC result means the backend is reachable. The validator then exercises
the actual behavior. If `tools/list` errors here, fix the backend before signaling
completion to the orchestrator.

---

## Notes: extensibility and model routing

- **No-key by design.** Claude Code is the Bedrock-native lane on purpose: keeping the
  credential surface minimal (IAM `bedrock:InvokeModel`, no key) is the security-by-default
  and "put the LLM in a box" tenet. Do not bolt a Token Vault / credential provider onto
  this agent; the Claude Code validator and opencode skills each own their own credential path.
- **Why Opus for this role.** Model routing is per-task: `pr_review` -> Haiku (cheap,
  read-only), `new_task`/`pr_iteration` -> Sonnet (balanced), complex/critical ->
  **Opus**. Backend server work is the complex/critical case, so the default stays
  `us.anthropic.claude-opus-4-6-v1`. Routing is about quality, not just cost: Opus
  self-corrects out of rabbit holes that trap mid-tier models on multi-file work.
- **Swap behind the interface.** New backend strategies plug in behind the same MCP tool
  contract (the Gateway target) without touching the orchestrator: the extensibility
  principle. The contract is the seam; keep it stable.
- **Cost framing.** Quote cost only as illustrative orders of magnitude (e.g. a single
  dev at ~30 to 60 tasks/month lands in the low-hundreds USD range, dominated by Bedrock
  inference + compute, not infra). Use the workshop's own measured per-agent run metrics,
  never vendor "Nx cheaper" claims.
- **Cleanup** (when tearing the harness down): `python coding-agents/claude-code/cleanup.py`,
  then `coding-agents/infra/cleanup.sh`, then `coding-agents/gateway_mcp/delete-all.sh`.
