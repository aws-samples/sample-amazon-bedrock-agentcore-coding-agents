# Connection API: FROZEN CONTRACT (the Connect layer)

> This is the single thing all three build tracks agree on (see the steering file `AGENTS.md`).
> The **shape** here is frozen: the Console (the Build layer) was built against it as a stub,
> and the embedded orchestration engine (`engine.py`, served by `connection_api.py`) returns it
> today, unchanged. **Changing this file is the only thing that needs a group decision.**
> Everything else proceeds in parallel.

Base URL (local engine): `http://localhost:8090`. All bodies are JSON. CORS is open so the
static console can call it from `file://` or `localhost`.

The model is **fully autonomous orchestration** (no race/winner): submit one task → the
orchestrator runs the autonomous blueprint → one composed PR. The API reflects exactly that lifecycle.

---

## Data shapes

### Run
```json
{
  "run_id": "run_0001",
  "task": "Convert /mnt/s3files/sample/cost_analyzer.py to a remote MCP server with tests + a chatbot UI",
  "status": "running",                  // queued | running | passed | failed | needs_human
  "phase": "agent_execution",           // the orchestration blueprint phase (see below)
  "created_at": "2026-06-09T07:40:00Z",
  "agents": ["claude-code", "kiro", "codex"],
  "roles": {                            // role per agent, composed, NOT raced
    "claude-code": "backend-mcp",
    "kiro": "validator",
    "codex": "frontend-builder"
  }
}
```

### Phase (the orchestration blueprint, deterministic except agent_execution)
`admission` → `context_hydration` → `pre_flight` → `agent_execution` → `finalization`
A run also has a terminal status once finalization completes.

### AgentProgress (per role, inside a run's detail)
```json
{
  "agent": "claude-code",
  "role": "backend-mcp",
  "state": "done",                      // pending | working | done | error
  "latency_ms": 192340,                 // run metric (observability), NOT a ranking
  "tokens": 184000,
  "cost_usd": 1.84,
  "note": "wrapped 5 tools with FastMCP, deployed behind Gateway"
}
```

### Result (only meaningful once status is terminal)
```json
{
  "run_id": "run_0001",
  "status": "passed",                   // passed | failed | needs_human
  "gate": {                             // the deterministic pytest acceptance gate, no LLM judge
    "passed": true,
    "checks": [
      {"check": "tool_discovery",   "passed": true,  "detail": "all 5 tools discoverable"},
      {"check": "tool_correctness", "passed": true,  "detail": "5 fixture cases correct"},
      {"check": "input_validation", "passed": true,  "detail": "unknown type rejected"}
    ]
  },
  "pr_url": "https://github.com/your-org/your-repo/pull/42",   // null until finalization opens it
  "composed_from": ["backend-mcp", "validator", "frontend-builder"],  // proves compose-not-compete
  "iterations": 1,                      // bounded (~2) then needs_human
  "artifact_endpoint": "http://127.0.0.1:49760",  // additive: where the composed MCP server answers
  "composed_branch": "run/run_150318_001",        // additive: REAL local git branch of the composed change
  "composed_commit": "517e4dcf66…",               // additive: real commit sha (null until gate green)
  "fail_reason": null                   // additive: machine-readable reason on failed/needs_human
}
```

---

## Endpoints

### `GET /api/health`
Liveness. `200 {"status":"ok","mode":"engine","executor":"agentcore"}` reports the embedded
engine, plus its execution seam (additive field). The shipped path is REAL-ONLY:
`executor` is `agentcore` (each routed role is dispatched to its DEPLOYED AgentCore
Runtime; a role with no wired ARN fails loud, there is no local/in-process producer).
Deterministic offline tests inject a test-only `fixture` executor by constructor, which
reports `executor: "fixture"`.

### `GET /api/agents`
List the configurable agents + their default role + model. Console renders these as checkboxes.
```json
{
  "agents": [
    {"id": "claude-code", "label": "Claude Code", "default_role": "backend-mcp", "model": "us.anthropic.claude-opus-4-6-v1", "credential": "bedrock-native"},
    {"id": "kiro",        "label": "Kiro",        "default_role": "validator",   "model": "auto",                          "credential": "token-vault"},
    {"id": "codex",       "label": "Codex",       "default_role": "frontend-builder", "model": "openai.gpt-5.5",           "credential": "runtime-iam"}
  ]
}
```

### `POST /api/runs`: submit one task (fire-and-forget)
Request:
```json
{
  "task": "Convert /mnt/s3files/sample/cost_analyzer.py to a remote MCP server with tests + a chatbot UI",
  "agents": ["claude-code", "kiro", "codex"]      // optional; defaults to all three
}
```
Response `202 Accepted` → a **Run** object (status `queued`, phase `admission`). The caller then
polls `GET /api/runs/{id}`. (Fire-and-forget: the POST returns immediately; the run continues.)

### `GET /api/runs/{run_id}`: poll run status
Returns a **Run** plus a `progress` array of **AgentProgress**:
```json
{
  "run_id": "run_0001", "task": "…", "status": "running", "phase": "agent_execution",
  "created_at": "…", "agents": ["claude-code","kiro","codex"],
  "roles": {"claude-code":"backend-mcp","kiro":"validator","codex":"frontend-builder"},
  "progress": [ /* one AgentProgress per agent */ ]
}
```

### `GET /api/runs/{run_id}/result`: final result
Returns a **Result**. While the run is non-terminal, returns `409 {"status":"running","phase":"…"}`.

### `GET /api/runs`: list recent runs (optional, for a history view)
`{ "runs": [ Run, … ] }`

### `GET /api/runs/{run_id}/events`: the run journal (additive, engine)
Append-only audit trail of phase transitions and role activity (embedded event audit). `{ "run_id": "…", "events": [ {"seq":1,"elapsed_s":0.0,"phase":"admission","level":"info","message":"…"}, … ] }`

### Additive endpoints (the routed engine, all contract-safe extensions)

- **`GET /api/workflows`**: the workflow registry the router resolves against (versioned
  workflow descriptors): `{ "workflows": [ {"workflow_ref":"convert/sample-to-mcp-v1","version":"1.0.0",
  "agents":[…],"usecase":"sample-to-mcp","read_only":false,"description":"…"}, … ] }`
- **`GET /api/runs/{id}/terminals`**: per-role shell transcripts (every line a real
  `/bin/sh` command the role ran in its container, with output + exit code):
  `{ "run_id":"…", "terminals": {"claude-code":[{"cmd":"…","output":"…","exit":0,"elapsed_s":0.1}], …} }`
- **`GET /api/runs/{id}/diff`**: the composed change as a per-file unified diff (the
  session Changes tab's data), read from this run's own commit in the composed repo:
  `{ "run_id":"…", "commit":"…"|null, "branch":"run/…", "files": [{"path":"deliverable/mcp_server.py",
  "added":42,"removed":0,"patch":"@@ …"}, …] }`. `files` is empty (with `reason`) until
  the gate is green and the commit lands. Each run's commit roots at the empty base, so
  its diff is exactly its own deliverable set (the same invariant the PR path assumes).
- **`GET /api/github`** / **`POST /api/github`**: the real-PR credential ladder status
  (env var → Secrets Manager → settings file → local mode) and the Settings-pane
  save/clear. Storage backend: `WORKSHOP_GITHUB_STORE=secretsmanager` (the workshop box)
  stores the pasted PAT in AWS Secrets Manager (secret name `WORKSHOP_GITHUB_SECRET`,
  default `agentcore/workshop/github-connection`; `status().source` = `secrets-manager`);
  the default `local` keeps the gitignored 0600 settings file (`source` = `settings`),
  and any Secrets Manager failure degrades to that file transparently. The token never
  surfaces beyond its last 4 characters.

### Additive fields on Run / Result (the routed engine)

- `Run.route`: the router's verdict: `{"workflow_ref","version","rule","agents","usecase","read_only"}`.
  `POST /api/runs` accepts an optional `workflow_ref` (unknown → run fails `UNKNOWN_WORKFLOW:…`,
  fail-closed) and treats `agents` as an explicit override; OMIT it and the router decides.
- `Result.review`: the SEPARATE review orchestrator's verdict: `{"state":"approved"|
  "changes_requested","lgtm":bool,"round":n,"gate":…,"critique":[{check,passed,detail},…]}`.
  Pass token is the exact string `LGTM: no changes needed`; non-LGTM buys ONE bounded
  re-implement pass.
- `Result.pr`: GitHub finalization: `{"pr_url":…}` when connected, `{"skipped":…}` in local
  mode, `{"error":…}` on a real failure. `pr_url` is real or null, never fake.

---

## Status / phase state machine (what the engine drives and any deployment must honor)

```
POST /api/runs
   └─> queued (admission)
        └─> running (context_hydration)
             └─> running (pre_flight)        // fail-closed: may go -> failed here
                  └─> running (agent_execution)   // the 3 roles work in parallel
                       └─> running (finalization)  // compose + pytest gate
                            ├─> passed        (gate green, pr_url set)
                            ├─> failed        (gate red after bounded iterations)
                            └─> needs_human   (iteration cap hit)
```

## Rules the real implementation must keep (so the Console never breaks)
1. **Never change a field name or status/phase enum without editing THIS file + telling the group.**
2. `pr_url` is `null` until `finalization` opens it. `result` is `409` until terminal.
3. Per-agent `latency_ms` / `tokens` / `cost_usd` are **run metrics** (observability), never a
   ranking; there is no "winner" field, by design.
4. `roles` always maps each agent to a distinct job (compose-not-compete).
