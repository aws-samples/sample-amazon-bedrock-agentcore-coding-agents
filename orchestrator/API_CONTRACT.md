# Connection API: FROZEN CONTRACT (the Connect layer)

> This is the single thing all three build tracks agree on (see the steering file `AGENTS.md`).
> The **shape** here is frozen: the Console (the Build layer) was built against it as a stub,
> and the embedded orchestration engine (`engine.py`, served by `connection_api.py`) returns it
> today, unchanged. **Changing this file is the only thing that needs a group decision.**
> Everything else proceeds in parallel.

Base URL (local engine): `http://localhost:8090`. All bodies are JSON. CORS is open so the
static console can call it from `file://` or `localhost`.

The model is **fully autonomous orchestration** (no race/winner): submit one task,
give each builder an isolated work item and role PR, validate the combined
candidate, advance green heads through a private merge queue, then open one final
PR to the default branch. The API reflects that lifecycle.

---

## Data shapes

### Run
```json
{
  "run_id": "run_0001",
  "task": "Build a small app with a backend and a frontend",
  "status": "running",                  // queued | running | passed | failed | needs_human
  "phase": "agent_execution",           // the orchestration blueprint phase (see below)
  "created_at": "2026-06-09T07:40:00Z",
  "agents": ["claude-code", "opencode", "claude-code-validator"],
  "roles": {                            // role per agent, composed, NOT raced
    "claude-code": "backend-builder",
    "opencode": "frontend-builder",
    "claude-code-validator": "validator"
  },
  "route": {                            // the routing verdict (additive; see below)
    "preset": "web-app",
    "rule": "preset 'web-app': Build a web app, front and back",
    "agents": ["claude-code", "opencode", "claude-code-validator"],
    "read_only": false
  },
  "fail_reason": null                   // machine-readable reason when status is failed/needs_human
}
```

### Phase (the orchestration blueprint, deterministic except agent_execution)
`admission` → `context_hydration` → `pre_flight` → `agent_execution` → `finalization`
A run also has a terminal status once finalization completes.

### AgentProgress (per role, inside a run's detail)
```json
{
  "agent": "claude-code",
  "role": "backend-builder",
  "state": "done",                      // pending | working | done | error
  "latency_ms": 192340,                 // run metric (observability), NOT a ranking
  "tokens": 184000,
  "cost_usd": 1.84,
  "note": "built the backend side of this request (12 files)",
  "engine": "agentcore"                 // "agentcore" on the shipped path; "" on fixture
}
```

### Result (only meaningful once status is terminal)
```json
{
  "run_id": "run_0001",
  "status": "passed",                   // passed | failed | needs_human
  "phase": "finalization",              // current phase while polling
  "progress": [ /* one AgentProgress per routed role */ ],
  "work_items": {                       // one isolated checkout per routed role
    "claude-code": {
      "work_id": "work_claude-code_a1b2c3",
      "kind": "builder",
      "branch": "workshop/runs/run-0001/claude-code-a1b2c3",
      "base_branch": "workshop/runs/run-0001/integration",
      "state": "done",
      "attempt": 2,
      "pr": {"number": 40, "pr_url": "https://github.com/your-org/your-repo/pull/40"},
      "merge_state": "merged",
      "changed_files": ["src/service.py"],
      "stale": false,
      "refreshes": 1,
      "dependency_refreshes": 1            // semantic owner turns after a dependency merges
    }
  },
  "integration_brief": {                // model-authored coordination, not an answer key
    "summary": "Build the API and UI against one shared interface.",
    "shared_contract": ["The UI consumes the issue JSON API."],
    "role_assignments": { /* exclusive builder ownership */ },
    "merge_order": ["claude-code", "opencode"]
  },
  "integration_candidate": {
    "files": ["src/service.py", "web/app.tsx"],
    "owners": { /* changed path -> work ids */ },
    "digest": "8bc4…"
  },
  "integration_branch": "workshop/runs/run-0001/integration",
  "gate_history": [                     // initial candidate, then one row per queued merge
    {"sequence": 1, "stage": "full candidate round 1", "passed": true,
     "candidate_digest": "8bc4…", "summary": "12 checks passed"},
    {"sequence": 2, "stage": "after merge work_claude-code_a1b2c3", "passed": true,
     "candidate_digest": "8bc4…", "summary": "12 checks passed"}
  ],
  "merge_queue": [
    {"position": 1, "work_id": "work_claude-code_a1b2c3",
     "agent": "claude-code", "state": "merged", "sha": "517e4d…"}
  ],
  "gate": {                             // the validator-authored acceptance check result (agentic, real exit code)
    "passed": true,
    "checks": [
      {"check": "service_reachable", "passed": true,  "detail": "GET /health returned 200"},
      {"check": "conversion_works",  "passed": true,  "detail": "length conversion returned correct value"},
      {"check": "bad_input_rejected","passed": true,  "detail": "negative value returned 400"}
    ],
    "summary": "3 checks passed"
  },
  "pr_url": "https://github.com/your-org/your-repo/pull/42",   // final PR; null until the queue is green
  "composed_from": ["backend-builder", "frontend-builder", "validator"],  // proves compose-not-compete
  "iterations": 1,                      // global gate/panel round; initial + one repair
  "artifact_endpoint": "http://127.0.0.1:49760",  // additive: where the running service answers (when applicable)
  "composed_branch": "run/run_150318_001",        // additive local/offline compatibility branch
  "composed_commit": "517e4dcf66…",               // additive local commit (null until gate green)
  "fail_reason": null,                  // additive: machine-readable reason on failed/needs_human
  "route": {…},                         // additive: same routing verdict as on Run
  "review": {…},                        // additive: independent panel verdict (see below)
  "pr": {…},                            // additive: GitHub finalization result (see below)
  "compose_base": {…},                  // additive: {mode: "external"|"local", …} compose base
  "merge_state": "human_review",        // additive: "human_review"|"merged"|null
  "next_action": "Open the final integration pull request and review its evidence.",
                                        // additive: what to DO about this outcome; "" when
                                        // there is genuinely nothing to say
  "resubmission_allowed": false          // additive: hard immediate-retry constraint
}
```

`gate` is the latest executable result for compatibility. `gate_history` is the
complete evidence: the validator authors and the engine executes a fresh check for
the assembled candidate and after every queued merge. A green `gate` never erases an
earlier checkpoint. Builder `work_items` have role PRs; checker work items have an
isolated checkout but no code PR.

`next_action` is DERIVED from `(status, fail_reason, pr)` on every read, never stored:
the reason is the fact, this is how to read it. It exists because `needs_human` covers
both a gate that stayed red on real work and a role that produced nothing (opposite
fixes), and because a PASSING run splits into "the PR opened" and "no PR opened, so
the work is only on a local branch". That second case is NOT a `fail_reason` (the
build succeeded), so it is read from `pr.error`. An unmapped reason yields `""` rather
than invented advice.

`resubmission_allowed` is also derived from `(status, fail_reason)`. It is `false`
for a completed run, for a bounded red gate or review finding such as
`ITERATION_CAP`, and whenever an external prerequisite must change first. It is
`true` only when immediately repeating the same request is an intended recovery,
such as a transient role execution or artifact-transfer failure. Callers must not
offer an immediate retry when this field is false; they still report conditional
future steps from `next_action`, such as waiting for quota to reset.

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
List the served agents + their default role + model, projected from the role registry.
Console renders these on the Stage 1 shelf.
```json
{
  "agents": [
    {"id": "claude-code",           "label": "Claude Code", "default_role": "backend-builder",   "model": "us.anthropic.claude-opus-4-6-v1",          "credential": "bedrock-native"},
    {"id": "opencode",              "label": "opencode",    "default_role": "frontend-builder",  "model": "amazon-bedrock/us.anthropic.claude-sonnet-4-6", "credential": "runtime-iam"},
    {"id": "claude-code-validator", "label": "Claude Code", "default_role": "validator",         "model": "us.anthropic.claude-opus-4-6-v1",          "credential": "bedrock-native"}
  ]
}
```
The list is derived from `roles.py` (the one declarative registry) and is wirable at runtime
via `WORKSHOP_ROLES`. Kiro and Codex remain in the registry as a restore path but are not
included in the served roster by default.

### `POST /api/runs`: submit one task (fire-and-forget)
Request:
```json
{
  "task": "Build a small service that converts between units",
  "preset": "service-from-scratch",    // optional; one of the ids from GET /api/presets
  "agents": ["claude-code", "claude-code-validator"],  // optional explicit role override
  "options": {}                        // optional per-run overrides (e.g. {"model": "..."})
}
```
Routing is explicit, never a guess: supply `preset` (a starting point), `agents` (an explicit
role list), or both. With neither, `task` alone fails at admission with `PRESET_NOT_SPECIFIED`,
because the coordinator must know which roles to dispatch. Unknown preset: `UNKNOWN_PRESET`.
Unknown role in `agents`: `UNKNOWN_ROLE`. Build with no checker routed: `NO_CHECKER_ROUTED`.

Response `202 Accepted` → a **Run** object (status `queued`, phase `admission`). The caller then
polls `GET /api/runs/{id}`. (Fire-and-forget: the POST returns immediately; the run continues.)

### `GET /api/runs/{run_id}`: poll run status
Returns a **Run** plus a `progress` array of **AgentProgress**:
```json
{
  "run_id": "run_0001", "task": "…", "status": "running", "phase": "agent_execution",
  "created_at": "…",
  "agents": ["claude-code","opencode","claude-code-validator"],
  "roles": {"claude-code":"backend-builder","opencode":"frontend-builder",
            "claude-code-validator":"validator"},
  "route": {"preset":"web-app","rule":"…","agents":[…],"read_only":false},
  "fail_reason": null,
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

- **`GET /api/presets`**: the starting points the console renders as chips, resolved from
  the one registry (`presets.PRESETS`) so the console and the engine cannot drift:
  `{ "presets": [ {"preset":"service-from-scratch","title":"Build a small service",
  "roles":["claude-code","claude-code-validator"],"task":"…","read_only":false}, … ] }`.
  Every build preset includes the checker in `roles`. `your-own` is always present with
  an empty `task` (the attendee supplies the request).
- **`GET /api/roster`**: the SERVED roles, projected from the one declarative registry
  (`roles.py`). The console renders these instead of keeping its own copy, so the
  sidebar can never advertise an agent this deployment does not run:
  `{ "roster": [ {"role":"claude-code","label":"Claude Code","kind":"builder",
  "capability":"backend","role_name":"backend-builder","description":"…",
  "steering_file":"CLAUDE.md","model":"us.anthropic.claude-opus-4-6-v1",
  "credential":"bedrock-native"}, … ] }`.
- **`GET /api/runs/{id}/terminals`**: per-role shell transcripts (every line a real
  `/bin/sh` command the role ran in its container, with output + exit code):
  `{ "run_id":"…", "terminals": {"claude-code":[{"cmd":"…","output":"…","exit":0,"elapsed_s":0.1}], …},
  "events": {…} }`. The `events` field carries the structured per-role agent event
  stream (text/thinking/tool_use/tool_result) the console renders as live tool calls.
- **`GET /api/runs/{id}/diff`**: the composed change as a per-file unified diff (the
  session Changes tab's data), read from this run's own commit in the composed repo:
  `{ "run_id":"…", "commit":"…"|null, "branch":"run/…", "files": [{"path":"claude-code/server.py",
  "added":42,"removed":0,"patch":"@@ …"}, …] }`. `files` is empty (with `reason`) until
  the gate is green and the commit lands. Each run's commit roots at the empty base, so
  its diff is exactly its own deliverable set (the same invariant the PR path assumes).
- **`GET /api/github`** / **`POST /api/github`**: the real-PR credential ladder status
  (env var → Secrets Manager → settings file → local mode) and the Settings-pane
  save/clear. Storage backend: `WORKSHOP_GITHUB_STORE=secretsmanager` (the workshop box)
  stores the repo config in AWS Secrets Manager (`status().source` = `secrets-manager`);
  the default `local` keeps the gitignored 0600 settings file (`source` = `settings`),
  and any Secrets Manager failure degrades to that file transparently. The GitHub App
  credential (repo + optional gateway URL) never surfaces beyond a masked summary.

### Additive fields on Run / Result (the routed engine)

- `Run.route`: the routing verdict: `{"preset","rule","agents","read_only"}`. `preset` is
  the id from `GET /api/presets`, or `"custom"` when `agents` was given explicitly.
  `rule` is a human-readable explanation for the run log. `agents` is the resolved
  role list; `read_only` is true for review-only routes (no builder dispatched).
  Absent until the run exits admission (the router sets it there).
- `Result.review`: the independent review-panel verdict:
  `{"state":"approved"|"changes_requested","lgtm":bool,"round":n,"gate":{…},
  "reasons":[…],"assessment":"…","panels":[…],"review_unavailable":bool}`.
  Each `panels` row records
  `name` (`adversarial` or `design`), display `label`, `state`
  (`approved`, `changes_requested`, or `abstained`), `model`, `reasons`,
  `assessment`, and an optional abstention `note`.
  `reasons` is the list of change-request feedback items fed back to the routed roles on a
  re-implement pass. `assessment` is the full markdown posted on the PR.
  Pass token is the exact string `LGTM: no changes needed`; non-LGTM buys ONE bounded
  re-implement pass (`MAX_REVIEW_ROUNDS`). The two panel members run as separate,
  read-only model turns over the integrated candidate and never reuse a builder
  conversation. Any finding blocks the queue. An unreachable member is recorded
  as `abstained`, sets `review_unavailable`, and blocks the queue with
  `REVIEW_UNAVAILABLE`; builders are not asked to repair a model outage.
- `Result.pr`: GitHub finalization: `{"pr_url":…}` when connected, `{"skipped":…}` in local
  mode, `{"error":…}` on a real failure. `pr_url` is real or null, never fake.

---

## Status / phase state machine (what the engine drives and any deployment must honor)

```
POST /api/runs
   └─> queued (admission)
        └─> running (context_hydration)
             └─> running (pre_flight)        // fail-closed: may go -> failed here
                  └─> running (agent_execution)   // builders in parallel; checker after their join
                       └─> running (finalization)  // candidate + executable gate + review panel + queue
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
