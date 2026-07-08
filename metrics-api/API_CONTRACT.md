# Governance & Metrics API: FROZEN CONTRACT (Stage 3, Connect layer)

> Stage 3 backend (Raj). Two things the console needs: (1) the **API-first per-user cost
> surface** (Chandra's P0: a Python lib + co-equal REST, AWS naming `list_*`/`get_*`), and
> (2) the **governance surfaces** (sessions, user attribution, the kill switch, Cedar
> policy view). Same contract-first discipline: console builds against this; the real local
> implementation (`metrics_api.py` over `metrics_lib` + the run ledger) returns it today; the
> AgentCore implementation (session-inspector DynamoDB + CloudWatch + X-Ray) returns it
> later, unchanged.

Base URL (local engine): `http://localhost:8092`. JSON bodies. CORS open.

Grounded in `aws-samples/sample-agent-assisted-sdlc` (inspector DynamoDB schema, routes) and
`metrics-api/README.md`. **Per-user attribution is DERIVED from session metadata** until
Runtime exposes it natively (SIFT 5/26 gap); the surface is stable regardless.

---

## Data shapes

### Session  (from the inspector's session-tracking DynamoDB)
```json
{
  "session_id": "sess-9f3a",
  "invocation_number": 1,
  "runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:<acct>:runtime/...",
  "assistant_type": "claude-code",     // claude-code | kiro | codex
  "user_id": "raj",                    // authenticated user recorded on the session
  "started_at": "2026-06-09T14:15:03Z",
  "issue_url": "https://github.com/your-org/your-repo/issues/42",
  "claude_running": true               // inspector probes the microVM /proc table
}
```

### UserMetrics  (the per-user roll-up, the P0)
```json
{
  "user_id": "raj",
  "time_range": "24h",
  "runs": 7,
  "total_tokens": 1284000,
  "total_cost_usd": 12.40,
  "p95_latency_ms": 214000,
  "by_agent": {"claude-code": 6.20, "kiro": 3.90, "codex": 2.30}   // cost split, USD
}
```

### CostBreakdown
```json
{ "by": "agent", "breakdown": {"claude-code": 6.20, "kiro": 3.90, "codex": 2.30}, "currency": "USD" }
```

### IdentityStatus
```json
{
  "session_id": "sess-9f3a",
  "recorded_user": "raj",             // who started the run
  "user_email": "raj@example.com",
  "user_name": "Raj",
  "auth_provider": "cognito",
  "environment": "agentcore",
  "attribution_source": "run-ledger",
  "github_actor": "credential-dependent",
  "static_credentials_on_agent": false
}
```

This record proves session attribution only. It does not claim OAuth delegation
or a particular pull request author. GitHub authorship is determined separately
by the PAT or GitHub App credential used during finalization.

### CedarDecision  (governance guardrail view, read-only here)
```json
{
  "tier": "hard",                     // hard (absolute) | soft (deny-by-default, grantable)
  "rule_id": "pr_review_forbid_write",
  "effect": "forbid",
  "summary": "pr_review agents may never invoke Write/Edit"
}
```

---

## Endpoints: Metrics (API-FIRST; the lib + REST are co-equal)

The Python lib mirrors these 1:1 (`list_sessions`, `get_user_metrics`, `get_cost_breakdown`,
`get_latency_p95`). UI is a thin layer on top, never the only way in.

### `GET /api/health` → `200 {"status":"ok","mode":"engine"}`

### `GET /api/sessions`  ⇆ `list_sessions(filters=...)`
Query: `?user_id=&assistant_type=&window=<minutes>`. Response `{ "sessions": [ Session, ... ] }`.

### `GET /api/users/{user_id}/metrics`  ⇆ `get_user_metrics(user_id, time_range)`
Query: `?time_range=24h`. Response → a **UserMetrics**.

### `GET /api/cost-breakdown`  ⇆ `get_cost_breakdown(by="agent"|"user")`
Query: `?by=agent` (or `user`). Response → a **CostBreakdown**.

### `GET /api/latency/p95`  ⇆ `get_latency_p95(scope=...)`
Query: `?assistant_type=` or `?user_id=` (omit for fleet-wide). Response `{ "p95_latency_ms": 214000, "scope": {...} }`.

## Endpoints: Governance

### `GET /api/sessions/{session_id}/identity`  → an **IdentityStatus**

### `POST /api/sessions/{session_id}/stop`  (the kill switch, StopRuntimeSession)
Terminate a runaway microVM now. Response `200 {"session_id":"...","stopped":true}`.
(Persistent storage survives; only the microVM dies.)

### `GET /api/policies`  (Cedar policy view, read-only)
Response `{ "policies": [ CedarDecision, ... ] }`. Shows the hard/soft denies the fleet runs under.

### `GET /api/dashboard`  (the thin CloudWatch-style rollup, a VIEW over the metrics above)
Response: `{ "cost_by_agent": {...}, "p95_latency_ms": ..., "active_sessions": N, "runs_total": N }`.
Pure convenience for the console; it derives nothing the four metric endpoints don't already give.

---

## Rules the real implementation must keep
1. **API-first:** every UI number must come from one of these endpoints / the matching lib call.
   The dashboard is a thin view, never a separate data path.
2. Per-user numbers are **derived** from session metadata (recorded user × DynamoDB rows × OTel/X-Ray
   cost+latency) until Runtime ships native per-user metrics. Signatures don't change when it does.
3. `static_credentials_on_agent` must remain `false`. GitHub credentials stay in the
   orchestrator or Gateway, not in a coding-agent workspace.
4. Costs are illustrative fixtures, not live pricing. Never present Kiro vendor cost claims as fact.
5. Don't change a field/enum without editing THIS file + telling the group.
