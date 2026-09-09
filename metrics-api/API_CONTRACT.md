# Governance and metrics API

This contract describes `metrics_api.dispatch`, shared by the standalone server
and console. The standalone prefix is `/api`; in the console it is
`/api/metrics`. Examples below use the console prefix. Authentication is supplied
by the hosting deployment; the standalone server is not a Cognito service.

## CloudWatch attribution

### `GET /api/metrics/attribution`

Returns `source: "cloudwatch-logs-insights"`, the configured `region` (nullable),
`log_group`, and fixed `query`. This reads configuration and does not start a
CloudWatch query.

### `POST /api/metrics/attribution/query`

Accepts only `{"window_hours": 1 | 3 | 24}`; an omitted value defaults to 3.
Other fields and invalid windows return 400. The client cannot choose the query
or log group.

A 200 response always has `status: "Complete"` and these fields:

| Field | Type and meaning |
|---|---|
| `source` | `"cloudwatch-logs-insights"` |
| `query_id` | Actual CloudWatch query identifier |
| `region`, `log_group`, `query` | Actual source and query |
| `start_time`, `end_time` | Query bounds, Unix seconds |
| `rows` | Array of `{user, requests, input_tokens, output_tokens}` |
| `rows[].user` | String, or null for an untagged group |
| `rows[].requests` | Nonnegative integer |
| `rows[].input_tokens`, `rows[].output_tokens` | Nonnegative integers, or null if absent |
| `total_requests`, `tagged_requests`, `untagged_requests` | Counts from completed result rows |
| `coverage_percent` | Tagged requests / total requests, rounded to one decimal; null when total is zero |

The query filters `body = "claude_code.api_request"` and groups on
`resource.user.id`. It does not count Kiro usage or the whole AWS bill. A manual
resource label does not attest an authenticated identity.

Errors return `{error, code, source}` with a non-200 status. Key cases:

| Status | Meaning |
|---|---|
| 400 | Invalid request fields or time window |
| 403 | CloudWatch access denied |
| 404 | Telemetry log group not found |
| 429 | Both query slots are occupied |
| 503 | Missing region or unavailable/expired credentials |
| 504 | Console query wait budget exceeded |
| 502 | Failed, cancelled, malformed, truncated, or otherwise unavailable query result |

`Running` results are never returned as completed counts. The service attempts
to cancel a query still pending when it fails. If cancellation is not confirmed,
the error says so. The 20-second poll deadline is separate from the bounded SDK
calls; it is not a promise of an exact 20-second HTTP response.

## Host session and run records

These endpoints use `metrics_lib`. They describe records available to this host.
They are not the CloudWatch attribution table and do not discover every account
session.

| Method and path after `/api/metrics` | Parameters / response |
|---|---|
| `GET /health` | `{status: "ok", mode: "engine"}` |
| `GET /sessions` | Optional `user_id`, `assistant_type`, `window` in minutes; `{sessions: [...]}` |
| `GET /users/{user_id}/metrics` | Optional `time_range`; user, range, runs, tokens, configured-rate estimate, p95, by-agent split, and source |
| `GET /cost-breakdown` | `by=agent` or `by=user`; `{by, breakdown, currency, source}` |
| `GET /latency/p95` | Optional `assistant_type`, `user_id`; `{p95_latency_ms, scope}` |
| `GET /sessions/{session_id}/identity` | Recorded submitter and attribution source; 404 if absent |
| `GET /policies` | Local engine command-screening rules, not deployed Cedar policies |
| `GET /audit` | Optional `limit`; recorded operations |
| `GET /dashboard` | Agent estimates, p95, active sessions, `runs_total` |
| `GET /runtimes` | Configured targets and observed fleet information |

The legacy `runs_total` field is the number of recorded **sessions**, not a count
of distinct builds. The UI labels it accordingly. An absent latency sample or
usage source must not be presented as a measured zero. Cost is an estimate using
configured rates. `source` distinguishes `ledger` from optional legacy
`bedrock-invocation-log` data; that log is not deployed by the current workshop.

Session identity records include `recorded_user`, `user_email`, `user_name`,
`auth_provider`, `environment`, `attribution_source`, `github_actor`, and
`static_credentials_on_agent`. They record attribution, not OAuth delegation.
The served GitHub App's authorship is determined by the broker credential.

## Operations

### `POST /api/metrics/sessions/{session_id}/stop`

Returns 404 for an unknown session. A known session returns its identifier,
`stopped`, and `mechanism`; failures also include an error. HTTP 200 alone does
not establish success: the client must require **`stopped: true`**.

The Runtime path uses the exact recorded ARN and Runtime session ID with
`StopRuntimeSession`. Missing session identity fails instead of resolving a new
target. The local-process path signals only the recorded live process. A stopped
Runtime loses session-local processes and files; already saved shared files
remain.

### `POST /api/metrics/runtimes/{role}/probe`

Starts a small real job against a configured Runtime. This is an execution
operation, unlike Attribution's query. Inspect the returned result and error;
never infer a successful probe solely from HTTP 200.

The standalone HTTP adapter accepts at most 4096 body bytes, rejects malformed
JSON and invalid Content-Length, and does not expose wildcard CORS. Update this
contract whenever a public field or behavior changes.
