# Per-user metrics API

Governance is API-first. `metrics_lib.py` owns one data path and `metrics_api.py`
is a thin REST adapter over it. The React console consumes the same responses a
customer can query directly.

## Python surface

```python
list_sessions(filters=None)
get_user_metrics(user_id, time_range="24h")
get_cost_breakdown(by="agent")
get_cost_breakdown(by="user")
get_latency_p95(scope=None)
```

The matching HTTP contract is documented in [API_CONTRACT.md](API_CONTRACT.md).

## Evidence and limits

The workshop reads `.runs/telemetry.jsonl`, which Module 1 sessions and Module 2
runs append as work happens. There is no seeded dashboard dataset.

- User identity is the submitter recorded on the run. It supports audit and cost
  grouping, but does not attest GitHub authorship or OAuth OBO delegation.
- Token counts come from model API usage fields. A path that invokes no model
  reports zero.
- Cost is calculated from configured Bedrock rates. It is attribution data, not
  an AWS bill or live Pricing API quote.
- Runtime ARNs are resolved from the same runtime configuration the coordinator
  uses. An unwired role reports null, never a fabricated ARN.

## Run it

```bash
python3 metrics-api/metrics_api.py
curl -fsS http://127.0.0.1:8092/api/dashboard | jq
```

Run the socket-free library tests with:

```bash
python3 -m pytest metrics-api/test_metrics_lib.py -q
```
