"""Query plumbing and honest result states, without invoking AWS or a model."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
import attribution
import metrics_api


class Logs:
    meta = SimpleNamespace(region_name="eu-west-1")

    def __init__(self, responses):
        self.responses = iter(responses)
        self.started = []
        self.stopped = []

    def start_query(self, **kwargs):
        self.started.append(kwargs)
        return {"queryId": "query-123"}

    def get_query_results(self, **kwargs):
        assert kwargs == {"queryId": "query-123"}
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    def stop_query(self, **kwargs):
        self.stopped.append(kwargs["queryId"])
        return {"success": True}


def fields(**kwargs):
    return [{"field": k, "value": str(v)} for k, v in kwargs.items()]


def aggregate(**kwargs):
    """A completed CloudWatch group; partial field coverage is supplied explicitly."""
    kwargs.setdefault("emitter", "claude_code.api_request")
    for name in ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
                 "reasoning_output_tokens"):
        kwargs.setdefault(f"{name}_reported", kwargs["requests"] if name in kwargs else 0)
    return fields(**kwargs)


@pytest.fixture
def use_logs(monkeypatch):
    def use(responses):
        client = Logs(responses)
        monkeypatch.setattr(attribution, "_logs_client", lambda: client)
        monkeypatch.setattr(attribution.time, "sleep", lambda _: None)
        return client
    return use


def test_waits_for_complete_and_counts_untagged_events(use_logs):
    logs = use_logs([
        {"status": "Running", "results": [fields(user="partial", requests=900)]},
        {"status": "Complete", "results": [
            aggregate(requests=3, input_tokens=600, output_tokens=80),
            aggregate(user="workshop-user-1", requests=2, input_tokens=120, output_tokens=20),
        ]},
    ])
    result = attribution.query_attribution(3)
    assert result["total_requests"] == 5
    assert result["tagged_requests"] == 2
    assert result["untagged_requests"] == 3
    assert result["coverage_percent"] == 40
    assert result["region"] == "eu-west-1"
    assert result["rows"][0]["user"] is None
    assert logs.started[0]["endTime"] - logs.started[0]["startTime"] == 10800
    assert logs.started[0]["queryString"] == attribution.QUERY
    assert not logs.stopped


def test_complete_empty_results_are_empty_not_full_coverage(use_logs):
    use_logs([{"status": "Complete", "results": []}])
    result = attribution.query_attribution()
    assert result["total_requests"] == 0
    assert result["coverage_percent"] is None


def test_missing_token_aggregate_is_unknown_not_zero(use_logs):
    use_logs([{"status": "Complete", "results": [aggregate(user="user-1", requests=1)]}])
    result = attribution.query_attribution()
    assert result["rows"][0]["input_tokens"] is None
    assert result["rows"][0]["total_input_tokens"] is None
    assert result["rows"][0]["reported_requests"]["input_tokens"] == 0


def test_cache_breakdown_preserves_uncached_input_and_request_counts(use_logs):
    # Numeric shapes from actual CLI/CloudWatch receipts; labels are test-owned.
    use_logs([{"status": "Complete", "results": [
        aggregate(requests=16, input_tokens=18, output_tokens=21474,
                  cache_creation_tokens=49960, cache_read_tokens=567402),
        aggregate(user="person-a", requests=5, input_tokens=11, output_tokens=667,
                  cache_creation_tokens=29416, cache_read_tokens=80582),
        aggregate(user="person-b", requests=1, input_tokens=3, output_tokens=10,
                  cache_creation_tokens=0, cache_read_tokens=20972),
    ]}])
    status, result = metrics_api.dispatch("POST", "/api/attribution/query", "", {})
    assert status == 200
    assert result["total_requests"] == 22
    assert result["tagged_requests"] == 6
    assert result["untagged_requests"] == 16
    assert result["coverage_percent"] == 27.3
    assert [(row["input_tokens"], row["cache_creation_tokens"], row["cache_read_tokens"],
             row["total_input_tokens"], row["output_tokens"]) for row in result["rows"]] == [
        (18, 49960, 567402, 617380, 21474),
        (11, 29416, 80582, 110009, 667),
        (3, 0, 20972, 20975, 10),
    ]
    assert result["rows"][2]["reported_requests"] == {
        "input_tokens": 1, "output_tokens": 1,
        "cache_creation_tokens": 1, "cache_read_tokens": 1,
        "reasoning_output_tokens": 0, "total_input_tokens": 1,
    }


def test_native_codex_totals_do_not_count_cache_or_reasoning_twice(use_logs):
    use_logs([{"status": "Complete", "results": [
        aggregate(emitter="codex.sse_event", user="person-a", requests=3,
                  input_tokens=1200, cache_read_tokens=900, cache_creation_tokens=100,
                  output_tokens=80, reasoning_output_tokens=50),
    ]}])
    result = attribution.query_attribution()
    row = result["rows"][0]
    assert row["agent"] == "codex"
    assert row["total_input_tokens"] == 1200
    assert row["input_tokens"] == 200
    assert row["output_tokens"] == 80
    assert row["reasoning_output_tokens"] == 50
    assert row["reported_requests"]["total_input_tokens"] == 3
    summaries = {agent["id"]: agent for agent in result["agents"]}
    assert summaries["codex"]["total_input_tokens"] == 1200
    assert summaries["codex"]["output_tokens"] == 80
    assert summaries["claude-code"]["requests"] == 0
    assert summaries["claude-code"]["total_input_tokens"] is None


def test_same_user_and_untagged_groups_stay_separate_for_each_agent(use_logs):
    use_logs([{"status": "Complete", "results": [
        aggregate(user="person-a", requests=2, input_tokens=4, output_tokens=6,
                  cache_creation_tokens=20, cache_read_tokens=40),
        aggregate(emitter="codex.sse_event", user="person-a", requests=3,
                  input_tokens=900, output_tokens=30, cache_creation_tokens=0, cache_read_tokens=400),
        aggregate(user="", requests=1, input_tokens=1),
        aggregate(emitter="codex.sse_event", user=" \t", requests=1, input_tokens=100),
        aggregate(emitter="codex.sse_event", requests=2, input_tokens=200),
    ]}])
    result = attribution.query_attribution()
    rows = {(row["agent"], row["user"]): row for row in result["rows"]}
    assert len(rows) == 4
    assert rows["claude-code", "person-a"]["total_input_tokens"] == 64
    assert rows["codex", "person-a"]["total_input_tokens"] == 900
    assert rows["codex", None]["requests"] == 3
    assert rows["codex", None]["total_input_tokens"] == 300
    assert result["total_requests"] == 9
    assert result["tagged_requests"] == 5
    summaries = {agent["id"]: agent for agent in result["agents"]}
    assert summaries["claude-code"]["total_requests"] == 3
    assert summaries["claude-code"]["untagged_requests"] == 1
    assert summaries["codex"]["total_requests"] == 6
    assert summaries["codex"]["untagged_requests"] == 3
    assert summaries["codex"]["coverage_percent"] == 50


def test_codex_total_survives_an_unreported_cache_breakdown(use_logs):
    use_logs([{"status": "Complete", "results": [
        aggregate(emitter="codex.sse_event", requests=2, input_tokens=600,
                  output_tokens=90, cache_read_tokens=300),
    ]}])
    row = attribution.query_attribution()["rows"][0]
    assert row["total_input_tokens"] == 600
    assert row["reported_requests"]["total_input_tokens"] == 2
    assert row["input_tokens"] is None
    assert row["cache_creation_tokens"] is None
    assert row["reported_requests"]["input_tokens"] == 0


def test_codex_partial_native_input_is_marked_partial_without_inferred_uncached(use_logs):
    use_logs([{"status": "Complete", "results": [
        aggregate(emitter="codex.sse_event", requests=2, input_tokens=600,
                  input_tokens_reported=1, output_tokens=90,
                  cache_read_tokens=100, cache_creation_tokens=0),
    ]}])
    result = attribution.query_attribution()
    row = result["rows"][0]
    assert row["total_input_tokens"] == 600
    assert row["reported_requests"]["total_input_tokens"] == 1
    assert row["input_tokens"] is None
    codex = next(agent for agent in result["agents"] if agent["id"] == "codex")
    assert codex["reported_requests"]["total_input_tokens"] < codex["requests"]


@pytest.mark.parametrize("extra", [
    {"cache_read_tokens": 900, "cache_creation_tokens": 200},
    {"reasoning_output_tokens": 101},
])
def test_impossible_codex_breakdowns_are_errors(use_logs, extra):
    values = dict(input_tokens=1000, output_tokens=100,
                  cache_read_tokens=0, cache_creation_tokens=0, reasoning_output_tokens=0)
    values.update(extra)
    use_logs([{"status": "Complete", "results": [
        aggregate(emitter="codex.sse_event", requests=1, **values),
    ]}])
    with pytest.raises(attribution.AttributionError) as exc:
        attribution.query_attribution()
    assert exc.value.code == "INVALID_QUERY_RESULT"


@pytest.mark.parametrize("emitter", ["codex.api_request", "codex.unknown", ""])
def test_unknown_event_source_cannot_be_labeled_as_an_agent(use_logs, emitter):
    use_logs([{"status": "Complete", "results": [
        aggregate(emitter=emitter, requests=1, input_tokens=30),
    ]}])
    with pytest.raises(attribution.AttributionError) as exc:
        attribution.query_attribution()
    assert exc.value.code == "INVALID_QUERY_RESULT"


def test_explicit_zero_tokens_remain_zero(use_logs):
    use_logs([{"status": "Complete", "results": [
        aggregate(requests=2, input_tokens=0, output_tokens=0,
                  cache_creation_tokens=0, cache_read_tokens=0),
    ]}])
    row = attribution.query_attribution()["rows"][0]
    assert row["input_tokens"] == row["output_tokens"] == 0
    assert row["cache_creation_tokens"] == row["cache_read_tokens"] == 0
    assert row["total_input_tokens"] == 0


def test_aggregate_zero_without_reporting_events_is_unavailable(use_logs):
    use_logs([{"status": "Complete", "results": [
        aggregate(requests=2, input_tokens=4, output_tokens=10,
                  cache_creation_tokens=0, cache_creation_tokens_reported=0,
                  cache_read_tokens=20),
    ]}])
    row = attribution.query_attribution()["rows"][0]
    assert row["cache_creation_tokens"] is None
    assert row["reported_requests"]["cache_creation_tokens"] == 0
    assert row["total_input_tokens"] is None


@pytest.mark.parametrize("partial_field", [
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
])
def test_partial_coverage_keeps_reported_sum_without_inventing_total(use_logs, partial_field):
    values = {"input_tokens": 4, "output_tokens": 6,
              "cache_creation_tokens": 10, "cache_read_tokens": 20}
    use_logs([{"status": "Complete", "results": [
        aggregate(requests=2, **values, **{f"{partial_field}_reported": 1}),
    ]}])
    row = attribution.query_attribution()["rows"][0]
    assert row[partial_field] == values[partial_field]
    assert row["reported_requests"][partial_field] == 1
    # Output coverage does not change the independently complete input total.
    assert row["total_input_tokens"] == (34 if partial_field == "output_tokens" else None)


def test_missing_empty_and_whitespace_labels_merge_into_one_untagged_row(use_logs):
    use_logs([{"status": "Complete", "results": [
        aggregate(requests=2, input_tokens=4, output_tokens=2,
                  cache_creation_tokens=0, cache_read_tokens=8),
        aggregate(user="", requests=1, input_tokens=2, output_tokens=1,
                  cache_creation_tokens=10, cache_read_tokens=0),
        aggregate(user=" \t ", requests=1, input_tokens=3, output_tokens=0,
                  cache_creation_tokens=1, cache_read_tokens=0),
        aggregate(user="person-a", requests=3, input_tokens=6, output_tokens=3,
                  cache_creation_tokens=0, cache_read_tokens=60),
    ]}])
    result = attribution.query_attribution()
    assert [row["user"] for row in result["rows"]] == [None, "person-a"]
    assert result["total_requests"] == 7
    assert result["tagged_requests"] == 3
    assert result["untagged_requests"] == 4
    row = result["rows"][0]
    assert row["input_tokens"] == 9
    assert row["cache_creation_tokens"] == 11
    assert row["cache_read_tokens"] == 8
    assert row["total_input_tokens"] == 28
    assert row["reported_requests"]["input_tokens"] == 4


@pytest.mark.parametrize("missing_reported, expected_input", [(0, 3), (1, None)])
def test_merging_untagged_groups_preserves_unknown_aggregate(use_logs, missing_reported, expected_input):
    use_logs([{"status": "Complete", "results": [
        aggregate(requests=1, input_tokens=3, output_tokens=1,
                  cache_creation_tokens=0, cache_read_tokens=5),
        aggregate(user="", requests=1, input_tokens_reported=missing_reported,
                  output_tokens=1, cache_creation_tokens=7, cache_read_tokens=5),
    ]}])
    row = attribution.query_attribution()["rows"][0]
    assert row["requests"] == 2
    assert row["input_tokens"] == expected_input
    assert row["reported_requests"]["input_tokens"] == 1 + missing_reported
    assert row["cache_creation_tokens"] == 7
    assert row["cache_read_tokens"] == 10
    assert row["total_input_tokens"] is None


@pytest.mark.parametrize("reported", [3, -1, "1.5", "NaN", 0])
def test_inconsistent_token_coverage_is_an_error(use_logs, reported):
    use_logs([{"status": "Complete", "results": [
        aggregate(requests=2, input_tokens=4, input_tokens_reported=reported),
    ]}])
    status, body = metrics_api.dispatch("POST", "/api/attribution/query", "", {})
    assert status == 502
    assert body["code"] == "INVALID_QUERY_RESULT"
    assert "rows" not in body


def test_missing_coverage_cannot_be_assumed_complete(use_logs):
    use_logs([{"status": "Complete", "results": [fields(requests=1, input_tokens=4)]}])
    with pytest.raises(attribution.AttributionError) as exc:
        attribution.query_attribution()
    assert exc.value.code == "INVALID_QUERY_RESULT"


def test_response_limit_is_checked_before_empty_groups_are_merged(use_logs):
    logs = use_logs([{"status": "Complete", "results": [
        aggregate(user="", requests=1, input_tokens=3),
    ] * 10000}])
    status, body = metrics_api.dispatch("POST", "/api/attribution/query", "", {})
    assert logs.started[0]["limit"] == 10000
    assert status == 502
    assert body["code"] == "RESULT_LIMIT"
    assert "rows" not in body


@pytest.mark.parametrize("state", ["Failed", "Cancelled", "Timeout", "Unknown"])
def test_unsuccessful_query_never_returns_partial_rows(use_logs, state):
    use_logs([{"status": state, "results": [fields(requests=99)]}])
    with pytest.raises(attribution.AttributionError, match="No partial totals"):
        attribution.query_attribution()


@pytest.mark.parametrize("response", [
    {"status": "Complete"},
    {"status": "Complete", "results": [], "nextToken": "more"},
    {"status": "Complete", "results": [fields(requests="NaN")]},
    {"status": "Complete", "results": [fields(requests=-1)]},
])
def test_missing_truncated_or_invalid_results_cannot_become_totals(use_logs, response):
    use_logs([response])
    with pytest.raises(attribution.AttributionError):
        attribution.query_attribution()


def test_wait_is_bounded_and_its_own_query_is_cancelled(monkeypatch, use_logs):
    logs = use_logs([{"status": "Running", "results": []}])
    clock = iter([0, 0, 21])
    monkeypatch.setattr(attribution.time, "monotonic", lambda: next(clock))
    with pytest.raises(attribution.AttributionError) as exc:
        attribution.query_attribution()
    assert exc.value.status == 504
    assert logs.stopped == ["query-123"]


def test_failed_cancellation_remains_visible(monkeypatch, use_logs):
    logs = use_logs([{"status": "Running", "results": []}])
    clock = iter([0, 0, 21])
    monkeypatch.setattr(attribution.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(logs, "stop_query", lambda **_: {"success": False})
    status, body = metrics_api.dispatch("POST", "/api/attribution/query", "", {})
    assert status == 504
    assert "Cancellation was not confirmed" in body["error"]
    assert "rows" not in body


def test_access_denial_is_not_an_empty_success(use_logs):
    use_logs([ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetQueryResults")])
    status, body = metrics_api.dispatch("POST", "/api/attribution/query", "", {})
    assert status == 403
    assert body["code"] == "AccessDeniedException"
    assert "rows" not in body


@pytest.mark.parametrize("body", [
    {"window_hours": 0}, {"window_hours": 168}, {"window_hours": True},
    {"window_hours": "3"}, {"query": "arbitrary query"}, ["3"],
])
def test_client_cannot_inject_a_query_or_unbounded_window(monkeypatch, body):
    monkeypatch.setattr(attribution, "_logs_client", lambda: pytest.fail("must reject before AWS"))
    status, _ = metrics_api.dispatch("POST", "/api/attribution/query", "", body)
    assert status == 400


def test_region_is_derived_and_absence_is_actionable(monkeypatch):
    import boto3
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setattr(boto3, "Session", lambda: SimpleNamespace(region_name=None))
    with pytest.raises(attribution.AttributionError) as exc:
        attribution._logs_client()
    assert exc.value.code == "REGION_NOT_CONFIGURED"
