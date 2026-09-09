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

## Effective controls and policy previews

### `GET /api/metrics/controls`

Reads local configuration; it does not invoke AWS or an agent. The response has
`source: "host-configuration"`, an `observed_at` UTC timestamp, and:

| Field | Meaning |
|---|---|
| `identity` | Server-supplied `known`, `user_id`, `email`, and `name`; unknown values are null |
| `identity.mapping_state` | `anonymous`, `empty`, or `present`, from the current `UserIdentity.to_otel_env()` |
| `identity.telemetry_attributes` | The actual `OTEL_RESOURCE_ATTRIBUTES` value, or null |
| `merge_policy` | The host's effective `human_review` or `auto` setting |
| `limits` | `repairs_per_pr`, `gate_timeout_seconds`, and `role_timeout_seconds` from the running engine |
| `roles` | Served registry entries with `id`, `label`, `kind`, `capability`, and `role_name` |

A broken identity mapping returns 503 rather than reporting it as configured.
These values describe this host, not a separately deployed coordinator.

### `POST /api/metrics/policies/evaluate`

Accepts only `action`, `target`, and `read_only`. `action` is `run_command`
(default), `write_file`, or `read_file`. `target` must be non-empty, contain no
NUL, and fit in 2048 UTF-8 bytes. `read_only` is a boolean, default false.
Invalid input returns 400; an unavailable checker returns 503.

The real `policy.screen` evaluates the input as data. Nothing is executed.
The response contains `allowed`, `rule_id`, `reason`, `tier`,
`outcome` (`allow`, `hold`, or `deny`), the action and read-only flag,
`executed: false`, and `source: "policy-preview"`.

The server attempts to append a `policy_evaluation` audit event and returns
`audit_recorded`, plus `event_id` on success or `audit_error` on failure.
Audit failure does not replace the policy decision with a fabricated result.
Raw targets are never stored in the audit event; `target_sha256` identifies
the evaluated input without copying it into the ledger.

The catalog documents the current enforcement boundary. Shell screening applies
to the coordinator's command tool and engine terminal entrypoint; it does not
intercept native agent CLI commands or Development terminals. File rules can be
examined here but do not constitute an OS sandbox. `hold` means human handling,
not a deployed approval-and-resume workflow or Cedar policy.

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
For an interactive Runtime session, an email label alone does not prove Cognito:
`auth_provider` can be `not-recorded` and `static_credentials_on_agent` can be null.

Session rows also include `source`, `state`, and `can_stop`. Run-ledger rows are
historical records unless their recorded process is live. The console adds its
actual registered Runtime PTYs with `source: "runtime-registry"` and state `open`.
A configured Runtime ARN alone does not establish a live session.

`GET /audit` returns up to 200 operations by default; `limit` is bounded to
1..1000. The response is `{audit, total, source}`. Governance events include
`event_id`, `at`, `kind`, `user_id`, `actor_source`, a readable `line`, and
operation-specific `details`. Policy records include
the decision and target hash; session-stop records include the actual stop
outcome. The actor comes from the hosting server, or `local-session` when no
authenticated identity is available.

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

The console stops a registered PTY using its exact recorded Runtime identity.
If AWS refuses the stop, the server returns an error and retains the registry
entry. Stop responses report audit persistence separately with `audit_recorded`
and either `event_id` or `audit_error`.

### `POST /api/metrics/runtimes/{role}/probe`

Starts a small real job against a configured Runtime. This is an execution
operation, unlike Attribution's query. Inspect the returned result and error;
never infer a successful probe solely from HTTP 200.

The standalone HTTP adapter accepts at most 4096 body bytes, rejects malformed
JSON and invalid Content-Length, and does not expose wildcard CORS. Update this
contract whenever a public field or behavior changes.
