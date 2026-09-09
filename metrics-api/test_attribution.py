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
            fields(requests=3, input_tokens=600, output_tokens=80),
            fields(user="workshop-user-1", requests=2, input_tokens=120, output_tokens=20),
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
    use_logs([{"status": "Complete", "results": [fields(user="user-1", requests=1)]}])
    result = attribution.query_attribution()
    assert result["rows"][0]["input_tokens"] is None


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
