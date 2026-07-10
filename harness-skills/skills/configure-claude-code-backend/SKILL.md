---
name: configure-claude-code-backend
description: >-
  Configure the Claude Code agent as the BACKEND builder in the 3-agent AgentCore
  coding harness, the agent that wraps the cost_analyzer Skill into a remote
  FastMCP server behind the AgentCore Gateway. Use this when the user says "set up
  Claude Code", "configure the backend agent", "configure the backend MCP agent",
  "build the backend MCP server", "deploy claude-code", "point Claude Code at the
  task", "wire the MCP tools", or asks which agent owns the server/tools side.
  Claude Code runs Bedrock-native (CLAUDE_CODE_USE_BEDROCK=1, IAM bedrock:InvokeModel,
  NO API key) on default model us.anthropic.claude-opus-4-6-v1. Opus suits the
  multi-file backend work. Do NOT use this for the Claude Code validator (tests gate) or opencode
  (frontend chatbot UI); those have their own configure skills.
---

# Configure Claude Code: Backend MCP Server Builder

This skill configures **Claude Code** for its LOCKED role in our 3-agent autonomous
harness. Roles do not rotate:

| Agent | Role | Owns |
|---|---|---|
| **Claude Code** (this skill) | **BACKEND** | the AgentCore MCP server: wrap `cost_analyzer`'s 5 functions as FastMCP tools behind the Gateway |
| Claude Code (validator) | VALIDATOR | the acceptance gate: runs the pytest contract, decides "done" |
| opencode | FRONTEND BUILDER | the chatbot UI that calls the MCP tools |

This is **not** a race and there is **no winner**. The three agents are the single
agentic step of the orchestration blueprint, fanned into three roles and composed into ONE
deliverable in finalization (admission → context hydration → pre-flight → agent
execution → finalization). Claude Code's job is to produce the backend so the
validator's gate can go green and the orchestrator can open the PR autonomously.

Why Claude Code is the backend: this role is multi-file, contract-driven server work
(FastMCP wiring, schema fidelity, IAM/Gateway plumbing). Per per-task model routing, the
most capable model is the right call for complex/critical work; Opus recognizes rabbit
holes and self-corrects, where mid-tier models persist in unproductive loops. That is
why the default model here is `us.anthropic.claude-opus-4-6-v1` and why Claude Code,
not the Claude Code validator or opencode, owns the server.

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
   - `gateway_mcp/deploy-all.sh` has produced a Gateway URL.
   If not, point them at those steps (Step 2) before deploying this agent.

Then state the locked role back to the user: "Claude Code = BACKEND MCP server. It will
wrap the 5 `cost_analyzer` functions as FastMCP tools behind the Gateway."

---

## Step 2: Verify prerequisites are deployed

The backend agent cannot reach its tools without shared infra and the Gateway. Confirm
both first.

```bash
# Shared infra (VPC + S3 Files); deploy ONCE for all agents, not per-agent.
cd coding-agents/infra
./setup.sh us-west-2

# Gateway (the single IAM-auth MCP endpoint the agent is pointed at); deploy FIRST.
cd gateway_mcp
export AWS_REGION="us-west-2"
./deploy-all.sh
# Gateway URL is written to gateway_mcp/.deployed-state.json:
GATEWAY_URL=$(jq -r '.gateway_url' gateway_mcp/.deployed-state.json)
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

## Step 4: Point Claude Code at the task (the backend build)

The task is in `usecase-sample-to-mcp/`: convert the `cost_analyzer` Skill into a
**remote FastMCP MCP server** exposing exactly these 5 tools, registered behind the
AgentCore Gateway. The contract is already pinned in `cost_analyzer.py`
(`TOOL_SPECS` + `dispatch`): the agent must preserve names, `inputSchema`, and the
returned result dicts verbatim.

The 5 tools (the acceptance set: all must appear in `tools/list`):

1. `estimate_ec2_monthly_cost(instance_type, count=1, hours_per_month=730.0, region="us-west-2")`
2. `estimate_ebs_monthly_cost(volume_type, size_gb, count=1)`
3. `estimate_s3_monthly_cost(storage_gb, get_requests=0, put_requests=0, storage_class="STANDARD")`
4. `recommend_instance(vcpus, memory_gib)`
5. `estimate_stack_monthly_cost(spec)`

Hand the agent a tight, contract-first task prompt (this is the backend lane, not the
gate and not the UI):

```text
ROLE: BACKEND. Wrap usecase-sample-to-mcp/cost_analyzer.py as a FastMCP server and
register it behind the AgentCore Gateway. Expose EXACTLY the 5 tools in TOOL_SPECS with
their existing names and inputSchema. Each tool must call the matching cost_analyzer
handler and return its structured dict UNCHANGED (e.g. monthly_cost rounded to cents).
Reject unknown instance types / volume types / storage classes (raise, do not silently
misprice); the grader checks input validation. Do NOT change pricing values; they are
illustrative and deterministic. Do NOT write the chatbot UI (opencode) or the test gate
(Claude Code validator). Done = tools/list returns the 5 tools and tools/call returns the contract values.
```

You can drive the agent interactively to do this work:

```bash
cd coding-agents/claude-code
python connect.py --prompt "Wrap cost_analyzer's 5 functions as a FastMCP server behind the Gateway; preserve TOOL_SPECS names + inputSchema; return each handler's dict unchanged."
# or resume a session:
python connect.py --session <session-id>
```

---

## Step 5: Verify over the wire (tools/list + tools/call)

The deterministic acceptance gate is **owned by the Claude Code validator**, but the backend
agent should self-check its endpoint first so it doesn't hand a broken server to the gate.

```bash
GATEWAY_URL=$(jq -r '.gateway_url' gateway_mcp/.deployed-state.json)

# tools/list must return all 5 tools.
awscurl --service bedrock-agentcore --region us-west-2 -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}'

# tools/call must return 140.16 for m5.large x2 (the anchor fixture).
awscurl --service bedrock-agentcore --region us-west-2 -X POST "$GATEWAY_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","id":2,
       "params":{"name":"estimate_ec2_monthly_cost",
                 "arguments":{"instance_type":"m5.large","count":2}}}'
# Expect monthly_cost == 140.16 (0.096 * 730 * 2). Other anchors:
#   t3.micro -> 7.59 ; gp3 100GB -> 8.0 ; s3 100GB -> 2.3 ; recommend(2,8) -> "m5.large".
```

---

## Step 6: Acceptance gate (deterministic, no LLM judge)

This is the autonomous "definition of done": the same pytest contract the orchestrator
shells out to in finalization, and the same one the Claude Code validator runs. Run it against
the deployed endpoint by setting `MCP_ENDPOINT_URL`; the test suite swaps its in-process
adapter for the remote MCP client automatically.

```bash
MCP_ENDPOINT_URL="$GATEWAY_URL" AWS_REGION=us-west-2 \
  pytest usecase-sample-to-mcp/grading/ -v
```

Green means the backend is acceptable: all 5 tools discoverable (`tool_discovery`),
fixture values correct including `140.16` (`tool_correctness`), and unknown inputs
rejected (`input_validation`). Red → bounded retry (≈2 rounds, per the harness's
shift-feedback-left / bounded-iteration spine), then escalate to a human. The backend
agent's job is finished when this gate can pass; it does not self-approve. The validator
lane owns the verdict, and per-role wall-clock + token cost are recorded as run metrics
(observability, not a ranking).

---

## Notes: extensibility & model routing

- **No-key by design.** Claude Code is the Bedrock-native lane on purpose: keeping the
  credential surface minimal (IAM `bedrock:InvokeModel`, no key) is the security-by-default
  and "put the LLM in a box" tenet. Do not bolt a Token Vault / credential provider onto
  this agent; the Claude Code validator and opencode skills each own their own credential path.
- **Why Opus for this role.** Model routing is per-task: `pr_review` → Haiku (cheap,
  read-only), `new_task`/`pr_iteration` → Sonnet (balanced), complex/critical →
  **Opus**. Backend MCP server work is the complex/critical case, so the default stays
  `us.anthropic.claude-opus-4-6-v1`. Routing is about quality, not just cost: Opus
  self-corrects out of rabbit holes that trap mid-tier models on multi-file work.
- **Swap behind the interface.** New backend strategies plug in behind the same MCP tool
  contract (the `TOOL_SPECS` / Gateway target) without touching the orchestrator: the
  extensibility/flexibility principle. The contract is the seam; keep it stable.
- **Cost framing.** Quote cost only as illustrative orders of magnitude (e.g. a single
  dev at ~30 to 60 tasks/month lands in the low-hundreds USD range, dominated by Bedrock
  inference + compute, not infra). Use the workshop's own measured per-agent run metrics,
  never vendor "Nx cheaper" claims.
- **Cleanup** (when tearing the harness down): `coding-agents/cleanup_all.sh`, then
  `coding-agents/infra/cleanup.sh`, then `gateway_mcp/delete-all.sh`.
