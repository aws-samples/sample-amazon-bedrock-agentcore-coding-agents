# Governance and attribution API

The console exposes two distinct evidence sources through one router:

- `metrics_lib.py` reads host session and run records, calculates configured-rate
  estimates, and exposes session controls and local policy information.
- `attribution.py` queries actual CloudWatch Logs Insights request events. This
  is Lab 3's source and is independent of the host ledger.

`metrics_api.py` serves both directly on port 8092. The console mounts the same
router at `/api/metrics`. See [API_CONTRACT.md](API_CONTRACT.md).

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

The fixed query filters `claude_code.api_request`, groups on `resource.user.id`,
and sums the exported request and token fields. Only a complete result becomes
a table. Untagged rows remain visible. Missing token aggregates remain null;
an empty completed query has no coverage percentage. A missing group, denied
request, incomplete query, or absent credentials is an error.

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
