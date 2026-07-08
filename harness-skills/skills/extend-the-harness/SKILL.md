---
name: extend-the-harness
description: >-
  Decision guide for adding a 4th+ role to OUR 3-agent AgentCore coding-agent harness
  (Claude Code = BACKEND / AgentCore MCP server, Kiro = VALIDATOR / acceptance gate,
  Codex = FRONTEND BUILDER / chatbot UI). Use when the user says "add an agent to the
  harness", "extend the harness", "add a reviewer agent", "add a docs agent", "add a
  security/threat agent", "add a second implementer", "split the work across more agents",
  "what else should the harness have", "do we need a code reviewer / critic", "register a
  new coding agent", or "wire a new role into the orchestrator". Applies the harness design
  principles (extensibility behind interfaces, per-task model routing, cost-first,
  reliability) to decide whether a new role is justified and how to register it. The core
  rule it enforces: add a role only when it maps to a genuinely different JOB, never to
  split one job across more agents.
---

# Extend the Harness: adding a 4th+ role

This skill is the gate for growing our 3-agent harness. It does NOT add an agent reflexively.
It first decides whether a new role is justified, and only then walks you through registering it.

The locked baseline you are extending:

| Role | Agent | Job |
|---|---|---|
| BACKEND | Claude Code (Bedrock native) | builds the AgentCore MCP server (the deliverable's API surface) |
| VALIDATOR | Kiro (Token Vault / Identity) | runs the pytest acceptance gate: the autonomous "definition of done" |
| FRONTEND BUILDER | Codex (GPT through Bedrock and Runtime IAM) | builds the chatbot UI on top of the backend |

These three map cleanly to a single agentic step fanned into three distinct JOBS, then
composed in finalization. The orchestrator glue stays deterministic ("put the LLM in a box").
Adding a fourth role must preserve that property: a new role is more agentic surface, so it must
earn its tokens.

---

## THE RULE (read this before anything else)

> Add a role only when it maps to a genuinely different JOB. Never add a role to split ONE job
> across more agents.

"Make the backend faster" is the same job: give it a better model, do not clone Claude Code.
"Review the backend before it merges" is a different job (read-only critique vs. authoring);
that earns a slot. If you cannot name a distinct job with a distinct success signal, STOP: you
are scaling parallelism, not adding a role, and that belongs in orchestrator fan-out, not a new
agent.

Two principles make this concrete:
- **Extensibility**: extend behind interfaces, do not modify core. A new role plugs into the
  orchestrator contract (it implements the same `coding-agents/<agent>/` shape: `setup.sh` +
  `deploy.py`), so core glue is untouched.
- **Cost efficiency**: agents burn compute + inference tokens; cost is first-class. Every new
  role multiplies the autonomous run's cost. The cheaper the model the role can use, the lower
  the bar to justify it (a Haiku read-only reviewer is far easier to justify than an Opus one).

---

## Step 1: Gather inputs (AskUserQuestion)

Before deciding, get the user to name the JOB. Ask (AskUserQuestion style; pick the relevant ones):

1. **What outcome is missing today?** e.g. "PRs ship with subtle bugs", "no docs", "no threat
   review", "backend is a bottleneck". One sentence.
2. **Is it a different job, or the same job done more?** If "the backend can't keep up", that is
   the SAME job → route to a better model or fan out sub-tasks, do NOT add a role. If "nobody
   critiques the diff", that is a DIFFERENT job → candidate role.
3. **What is the new role's success signal?** A role with no measurable signal is not a role.
   (Reviewer → findings count / blocked-merge rate; Docs → README/CHANGELOG present; Security →
   threats filed; extra implementer → sub-feature passes its own slice of the gate.)
4. **Read-only or write?** Read-only roles (reviewer, security-analysis, docs-read) are cheaper,
   safer, and map to `pr_review` → Haiku routing. Write roles need sandbox + least-privilege
   review (security by default).
5. **Where does it sit in the blueprint?** Almost always inside Phase 4 (agent execution), feeding
   Phase 5 (finalization). The VALIDATOR (Kiro) stays the final gate; a new role advises or
   produces an artifact; it does not replace the pytest acceptance gate.

If the answers to (2) and (3) do not yield a distinct job with a distinct signal: do not add a
role. Recommend orchestrator fan-out or a model bump instead, and stop here.

---

## Step 2: Pick the role from the decision table

Map the named job to a candidate. Suggested models follow per-task routing (review is
read-only → cheap; authoring → mid-tier; critical → opt-in Opus).

| Candidate role | Add it WHEN (the distinct job) | Don't add it when | Read/Write | Suggested model |
|---|---|---|---|---|
| **REVIEWER / critic** | The diff ships but nobody adversarially critiques it before the gate; you want a read-only second pair of eyes that can block a merge. Different job from authoring. | "The backend has bugs": that is the author's job; tighten the acceptance gate (Kiro) first. | Read-only | **Haiku** (`pr_review` = fast, cheap, read-only) |
| **DOCS writer** | The deliverable ships without README / CHANGELOG / API docs and that is a stated outcome gap. Authoring prose is a different job from authoring the API. | The backend's docstrings are thin; that is the backend author's job. | Write (docs only) | **Sonnet** (authoring, mid-tier) |
| **SECURITY / threat agent** | You need an explicit threat pass (secrets, injection, IAM over-grant, SSRF) that authoring agents will not self-perform. Security-by-default makes this a real, distinct job for sensitive repos. | The repo is a throwaway sample with no secrets and no external surface. | Read-only (analysis; files findings, does not patch) | **Haiku** for triage, escalate to **Opus** only for critical/complex repos |
| **Extra IMPLEMENTER (parallel sub-feature)** | The work decomposes into genuinely independent sub-features (e.g. two unrelated MCP tools) that can be built and gated independently. This is the ONE case where "more of the same job" is legitimate, because each sub-feature is its own job with its own slice of the gate. | The sub-features share state or one depends on the other; that is one job; do it sequentially or as orchestrator fan-out within the existing BACKEND role. | Write | **Sonnet** default; **Opus** for the complex/critical slice (Opus recognizes rabbit holes; mid-tier persists in unproductive loops) |

Model-routing rationale: routing is not only cost. Capable models (Opus) recognize rabbit holes
and self-correct; mid-tier models persist in unproductive loops. So: default new roles to the
cheapest model that does the job, and reserve Opus for critical/complex, opt-in per repo.

---

## Step 3: Sanity-check against the design principles

Before registering, confirm the new role does not break the platform invariants:

- **Reliability**: the orchestrator must still drive every task to a terminal state if the new
  agent crashes. A new role must be optional/advisory in the blueprint, never a hard blocker that
  can wedge the run (the only hard gate stays the deterministic pytest acceptance gate).
- **Cost efficiency**: estimate the added cost. Illustrative orders of magnitude: a small
  run is ~$150-500/mo, dominated by Bedrock inference + compute, not infra. A Haiku read-only
  reviewer is a small marginal add; a second Opus implementer is not. If the role can run on
  Haiku, it almost always clears the bar.
- **Security by default**: write roles get an isolated sandbox + least-privilege IAM. Read-only
  roles (reviewer/security) must NOT have Write/Edit; hard-deny (`pr_review` agents may never
  invoke Write/Edit). Enforce that in the agent's settings, not by convention.
- **Extensibility**: the role plugs in behind the existing interface (`coding-agents/<agent>/`
  shape + orchestrator contract). If adding it requires editing core orchestrator logic beyond the
  contract, redesign; you are modifying core, not extending.

If any check fails, fix the design before Step 4.

---

## Step 4: Register the new agent

A new role is a new `coding-agents/<agent>/` directory that follows the SAME shape as the three
existing agents (so the frontend observability panes and the orchestrator can discover it). Use
the documented pattern (`setup.sh` then `python deploy.py`); do not invent flags or scripts.

```bash
# 0) shared infra already deployed once (do NOT redo per role)
#    cd coding-agents/infra && ./setup.sh us-west-2

# 1) scaffold the new role from an existing agent of the same auth type:
#    - Bedrock-native role (no API key)  -> copy claude-code/
#    - Token Vault role (vendor key)      -> copy kiro/ or codex/
cd coding-agents
cp -r claude-code reviewer            # example: a Bedrock-native read-only REVIEWER

# 2) edit reviewer/ to fit the role:
#    - run.sh:        launch flags + the role's system prompt / CLAUDE.md (read-only critique)
#    - settings.json: for read-only roles, DENY Write/Edit (hard-deny, pr_review invariant)
#    - deploy.py:     set the runtime name + the model id for this role's routing tier
#    - CLAUDE.md:     the role's job, success signal, and what it must NOT do

# 3) build + deploy the new role's runtime
cd coding-agents/reviewer
./setup.sh                            # builds arm64 image, pushes to ECR
python deploy.py                      # registers/updates the AgentCore Runtime (VPC, S3 Files, IAM)
```

### Auth wiring by role type (only if the role needs a vendor key)

```bash
# Bedrock-native role (Claude family): nothing extra; runtime IAM role has bedrock:InvokeModel.

# Token Vault role using a Kiro-style key (fetched on-demand at session start, in-memory only):
cd coding-agents/<agent>
KIRO_API_KEY=ksk_xxx ./setup.sh       # or interactive ./setup.sh ; or --skip-identity
python deploy.py

# Bedrock GPT role (Codex): nothing extra; use the Runtime IAM role and the
# amazon-bedrock provider in .codex/config.toml.
cd coding-agents/<agent> && ./setup.sh && python deploy.py
```

---

## Step 5: Wire the role into the orchestrator contract

The orchestrator drives the autonomous run (admission → context hydration → pre-flight → agent
execution → finalization). There is NO race and NO winner; roles are composed, not ranked.

1. **Declare the role** in the orchestrator's role map alongside BACKEND / VALIDATOR / FRONTEND,
   with its runtime ARN (from the new agent's `runtime_config.json`), its model id, and its
   read/write capability.
2. **Place it in Phase 4 (agent execution)** as an advisory/producer step. It must feed Phase 5
   (finalization), NOT replace the gate:
   - REVIEWER → produces findings that finalization attaches to the PR (bounded: ~2 rounds, then
     a human), informing but not replacing the pytest gate.
   - DOCS → produces doc artifacts composed into the single deliverable.
   - SECURITY → files threats; criticals can fail-closed before PR, mirroring the pre-flight phase.
   - extra IMPLEMENTER → owns its sub-feature branch; its slice runs the same acceptance gate.
3. **Keep the final gate unchanged.** The autonomous definition of done stays the deterministic
   acceptance gate, run after composition:

```bash
# acceptance gate (the definition of done), unchanged by adding a role
MCP_ENDPOINT_URL=<deployed-endpoint> pytest usecase-sample-to-mcp/grading/
```

4. **Observe the new pane.** The local frontend reads each agent's `runtime_config.json`; the new
   role appears as an additional observability pane (this is a window into the autonomous run, not
   a race UI).

```bash
cd coding-agents/frontend && pip install -r requirements.txt && python app.py
# http://127.0.0.1:5050 has a per-pane agent dropdown + model override; watch all roles work
```

---

## Step 6: Verify, then stop

- Confirm the new runtime deployed and is discoverable (its `runtime_config.json` exists and the
  frontend shows its pane).
- Run one task end-to-end through the orchestrator; confirm the new role produces its artifact /
  finding AND the pytest acceptance gate still passes unchanged.
- For read-only roles, confirm Write/Edit is actually denied (hard-deny), not merely unused.
- Record the role's marginal cost from the run's per-agent metrics (use the workshop's OWN
  measured numbers; never vendor "Nx cheaper" claims).

If the role does not improve a measured outcome (e.g. reviewer findings never change a PR), remove
it. Extensibility cuts both ways: a role that does not earn its tokens is removed as cleanly as it
was added.
