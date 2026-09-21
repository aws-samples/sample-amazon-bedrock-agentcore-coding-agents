# Governance and attribution API

The console exposes configuration and evidence through one router:

- `metrics_lib.py` reads host session and run records, calculates configured-rate
  estimates, and exposes session controls and local policy information.
- `attribution.py` queries actual CloudWatch Logs Insights request events. This
  is Lab 3's source and is independent of the host ledger.
- `governance_controls.py` reads the host's identity mapping, merge policy,
  execution limits, and role registry. It also evaluates policy inputs without
  executing them and records the actual decision.

`metrics_api.py` serves these directly on port 8092. The console mounts the same
router at `/api/metrics`. See [API_CONTRACT.md](API_CONTRACT.md).

In Agent Studio, **Usage** owns CloudWatch queries, **Controls** owns
configuration and policy previews, and **Activity** owns audit records and
session management.

## Query exported attribution

```bash
python3 metrics-api/metrics_api.py
curl -fsS http://127.0.0.1:8092/api/attribution
curl -fsS -X POST http://127.0.0.1:8092/api/attribution/query \
  -H 'Content-Type: application/json' -d '{"window_hours":3}'
```

The server derives its AWS region from the environment or SDK configuration.
`WORKSHOP_TELEMETRY_LOG_GROUP` defaults to
`/workshop/coding-agents/telemetry`. The caller may choose 1, 3, or 24 hours;
it cannot supply a query or log group.

The fixed query reads Claude Code `claude_code.api_request` events and Codex
`codex.sse_event` events whose kind is `response.completed` and which contain
token usage without an error. It groups by CLI and `resource.user.id`. The API
returns separate `agents` summaries and user `rows` for Claude Code and Codex;
the same user can have one row for each CLI.

Claude Code reports uncached input, cache creation, and cache read separately.
Codex reports an input total that already includes cache tokens. The response
normalizes `total_input_tokens` and uncached `input_tokens` without adding Codex's
cache tokens twice. Reasoning tokens are a subset of output, not an extra charge
to add to that total. See the contract for field coverage and partial results.

Only a complete query result becomes a table. Untagged rows remain visible.
Missing token aggregates remain null; an empty completed query has no coverage
percentage. A missing group, denied request, incomplete query, or absent
credentials is an error. These counts describe exported usage events, not every
API attempt, prompt, session, or build.

Usage shows both CLI summaries, an agent filter, and a compact user table. Cache
and reasoning details expand separately. **Copy query** supports an independent
CloudWatch check; **Export JSON** preserves the full response and exact UTC window,
including rows hidden by the current table filter.

The query permits two concurrent requests, uses a 20-second polling budget and
bounded SDK calls, and attempts to cancel a still-pending query on failure.
Unconfirmed cancellation is reported. The host needs `logs:StartQuery`,
`logs:GetQueryResults`, and `logs:StopQuery` for this path. No model runs.

## Host records and their limits

The Python surface remains `list_sessions`, `get_user_metrics`,
`get_cost_breakdown`, and `get_latency_p95`. These read the host's recorded
sessions, not all sessions or builds in the AWS account. Optional legacy
Bedrock invocation-log configuration can supply user usage; the current
workshop does not deploy that source. Responses identify it when used.

A zero estimate can mean usage was not recorded. Configured rates are not a
live pricing quote, and neither evidence source includes a complete AWS bill.
Kiro's vendor usage is separate. Submitter labels support attribution; they do
not establish GitHub authorship, OAuth delegation, or authorization.

```bash
python3 -m pytest -q metrics-api/test_metrics_lib.py metrics-api/test_attribution.py
```

The attribution tests exercise response handling with controlled SDK doubles.
A real event traversal is still required to prove its region, permissions,
exported events, and console query end to end.

## Inspect controls and evaluate policy

`GET /controls` reads the host's effective configuration and the identity supplied
by the hosting server. `POST /policies/evaluate` accepts an action, target, and
read-only flag. It invokes `policy.screen`, never a shell, model, or file tool.
The response distinguishes allow, deny, and hold for human handling. The hold
is not an approval queue or a promise that a later click can resume the command.

Policy preview and session-stop operations append audit events to the local
ledger. The server supplies the actor; clients cannot submit one. Policy targets
can contain secrets, so only their SHA-256 digest is recorded. If the audit write
fails, the operation reports that failure separately from its actual result.
