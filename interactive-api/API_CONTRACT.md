# Interactive API: FROZEN CONTRACT (Stage 1, Connect layer)

> Stage 1 backend (Aravind). The connection API for the **Interactive** stage: deploy ONE
> coding agent on Runtime, open an interactive shell into its live microVM, run commands, and
> do a single module-to-MCP conversion by hand. Same contract-first discipline as Stage 2
> (`../orchestrator/API_CONTRACT.md`): the console (Build) builds against this, the local
> implementation (`interactive_api.py`, workspaces, subprocesses) returns it today, the
> AgentCore wiring (`connect.py` / `InvokeAgentRuntimeCommandShell` over a SigV4 WebSocket
> PTY) returns it later, unchanged.

Base URL (local engine): `http://localhost:8091`. JSON bodies. CORS open.

Grounded in the reference harness `coding-agents/claude-code/` (`deploy.py`, `connect.py`, `run.sh`).
The teaching arc this API serves: **deploy → open shell → run a command → convert one module →
verify `tools/list`.** One agent, by hand. No orchestration (that's Stage 2).

---

## Data shapes

### Agent
```json
{
  "agent_id": "claude-code",
  "label": "Claude Code",
  "model": "us.anthropic.claude-opus-4-6-v1",   // overridable at deploy
  "credential": "bedrock-native",               // no API key; IAM role has bedrock:InvokeModel
  "status": "ready",                            // not_deployed | deploying | ready | error
  "runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:<acct>:runtime/claude-code-agent-XXXX",
  "endpoint": "DEFAULT"
}
```

### Session (an interactive shell into the running microVM)
```json
{
  "session_id": "sess_0001",
  "agent_id": "claude-code",
  "status": "open",                  // booting | open | closed
  "workspace": "/mnt/s3files",         // persistent (S3 Files @ /mnt/s3files)
  "cwd": "/mnt/s3files",
  "history": [                       // most-recent-last command/output pairs
    {"input": "ls /mnt/s3files", "output": "README.md  cost_analyzer.py"}
  ]
}
```

### ConversionResult (the Stage 1 payoff: one module function -> one MCP tool, by hand)
```json
{
  "session_id": "sess_0001",
  "sample_file": "/mnt/s3files/sample/cost_analyzer.py",
  "server_file": "/mnt/s3files/mcp_server.py",
  "tool": "estimate_ec2_monthly_cost",
  "tools_list": [ { "name": "...", "description": "...", "inputSchema": {} } ],
  "sample_call": {"args": {"instance_type": "m5.large", "count": 2}, "result": {"monthly_cost": 140.16, "currency": "USD"}},
  "verified": true                   // tools/list shows the tool AND the sample call returns the fixture value
}
```

---

## Endpoints

### `GET /api/health`
`200 {"status":"ok","mode":"engine"}` (an AgentCore-dispatching deployment reports `"live"`;
`"stub"` was the contract-only era).

### `GET /api/agents`
List deployable agents + status. (Stage 1 features Claude Code; others listed for parity.)
`{ "agents": [ Agent, ... ] }`

### `POST /api/agents/deploy`  (deploy ONE agent to Runtime)
Models `./setup.sh` (build+push arm64 image to ECR) + `python deploy.py` (CreateAgentRuntime,
VPC, S3 Files mount, IAM). Request: `{"agent_id":"claude-code","model":"<optional override>"}`.
Response `202` → an **Agent** (status `deploying`). Poll `GET /api/agents/{id}` until `ready`.
A Runtime is a template; no microVM boots until a session opens.

### `GET /api/agents/{agent_id}`  (deploy status)
Returns an **Agent** (status advances `deploying` → `ready`).

### `POST /api/sessions`  (open an interactive shell, boots the microVM)
Models `python connect.py` (SigV4 WebSocket PTY). Request: `{"agent_id":"claude-code"}`.
Response `201` → a **Session** (status `booting`, then `open` once the healthcheck on :8080 is
green). `agent_id` must be `ready` first, else `409 {"error":"agent not ready"}`.

### `POST /api/sessions/{session_id}/input`  (run a command / prompt in the shell)
Request: `{"input":"ls /mnt/s3files"}`. Response `200 {"output":"...","cwd":"/mnt/s3files"}`.
State persists across inputs (env, cwd, files in `/mnt/s3files`); that's the Runtime point.

### `GET /api/sessions/{session_id}`  (session state + recent output buffer)
Returns a **Session** (with `history`).

### `POST /api/sessions/{session_id}/convert-skill`  (the by-hand module-to-MCP conversion)
The Stage 1 payoff. Request (optional): `{"tool":"estimate_ec2_monthly_cost"}` (defaults to it).
Inside the shell, the agent wraps ONE `cost_analyzer` function as a minimal FastMCP tool and
proves it discoverable. Response `200` → a **ConversionResult**.

### `GET /api/sessions/{session_id}/tools`  (tools/list of the agent's MCP server)
Response `200 {"tools":[ ... ]}` (empty until a conversion has run).

### `POST /api/sessions/{session_id}/file`  (workspace file ops, the VS Code-like explorer)
One endpoint, four behaviors, selected by an optional `op` field. All paths are
`/mnt/s3files`-relative and jailed to the session workspace; a path that escapes the jail
returns `{"error":"invalid path"}` (never a traversal). Errors return `{"error":"..."}`; the
caller surfaces them, the server never 500s.

| Request body | Behavior | Response |
|---|---|---|
| `{"path":"x.py"}` | **read** | `{path, language, binary, content}` (or `{"error":"file not found"}`) |
| `{"path":"x.py","content":"..."}` | **write** (creates if absent) | `{path, bytes, tree:[...]}` |
| `{"path":"x.py","op":"delete"}` | **delete** (`os.remove`, jailed) | `{ok:true, path, tree:[...]}` |
| `{"path":"x.py","op":"rename","to":"y.py"}` | **rename/move** (`os.rename`, both ends jailed) | `{ok:true, path:"<new>", tree:[...]}` |

Backward-compatible: a body with no `op` is the original read/write contract (write when
`content` is present, else read). After `delete`/`rename` the response carries the fresh
`tree` (same shape as `GET /api/sessions/{id}` → `tree`), so the explorer re-renders from it.
Missing file on delete/rename → `{"error":"not found"}`. `tree` entries are
`{path, type:"file"|"dir", size}` sorted dirs-first by depth.

### `DELETE /api/sessions/{session_id}`  (stop the session; microVM dies, /mnt/s3files persists)
Response `200 {"session_id":"...","status":"closed"}`.

---

## Lifecycle the local engine implements (and the AgentCore wiring must honor)
```
deploy agent -> deploying -> ready          (deploy.py finishes; endpoint READY)
open session -> booting -> open             (connect.py attaches; healthcheck :8080 green)
run input(s) -> output, state persists      (interactive shell while the agent runs)
convert-skill -> ConversionResult           (one function -> one MCP tool, tools/list verified)
close session -> closed                     (microVM stops; /mnt/s3files survives)
```

## Rules the real implementation must keep (so the console never breaks)
1. Don't change a field name or status enum without editing THIS file + telling the group.
2. A session can only open on a `ready` agent. `convert-skill` only succeeds on an `open` session.
3. Pricing in any `result` is the illustrative `cost_analyzer` fixture (e.g. `140.16`), NOT live AWS pricing.
4. This stage is **one agent, by hand**: no fan-out, no grading, no PR. That's Stage 2.
