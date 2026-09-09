"""Lab 3's read-only CloudWatch evidence, separate from the host run ledger.

One fixed Logs Insights query, one workshop log group, a bounded time window
and wait. Missing credentials, incomplete results and failed queries are errors,
never a synthetic empty dataset. No model or coding agent is invoked.
"""
from __future__ import annotations

import os
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any

QUERY = """filter body = "claude_code.api_request"
| stats sum(attributes.input_tokens) as input_tokens,
        sum(attributes.output_tokens) as output_tokens,
        count(*) as requests
  by resource.user.id as user
| sort requests desc"""
DEFAULT_LOG_GROUP = "/workshop/coding-agents/telemetry"
POLL_BUDGET_S = 20
_SLOTS = threading.BoundedSemaphore(2)


class AttributionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.status = code, status


def configuration() -> dict[str, Any]:
    import boto3

    return {
        "source": "cloudwatch-logs-insights",
        "region": os.environ.get("AWS_REGION") or boto3.Session().region_name,
        "log_group": os.environ.get("WORKSHOP_TELEMETRY_LOG_GROUP", DEFAULT_LOG_GROUP),
        "query": QUERY,
    }


def _logs_client():
    import boto3
    from botocore.config import Config

    session = boto3.Session()
    region = os.environ.get("AWS_REGION") or session.region_name
    if not region:
        raise AttributionError(
            "REGION_NOT_CONFIGURED",
            "Set the console's AWS region to the workshop deployment region.", 503)
    return session.client("logs", region_name=region, config=Config(
        connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}))


def _integer(value: str | None, *, optional: bool = False) -> int | None:
    if optional and value in (None, ""):
        return None
    try:
        number = Decimal(value or "")
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (InvalidOperation, ValueError):
        raise AttributionError("INVALID_QUERY_RESULT",
                               "CloudWatch returned an invalid aggregate; no totals were inferred.") from None


def _summarize(results: list[list[dict[str, str]]]) -> dict[str, Any]:
    rows = []
    for result in results:
        fields = {field["field"]: field.get("value") for field in result}
        user = fields.get("user") or None
        rows.append({
            "user": user,
            "requests": _integer(fields.get("requests")),
            "input_tokens": _integer(fields.get("input_tokens"), optional=True),
            "output_tokens": _integer(fields.get("output_tokens"), optional=True),
        })
    total = sum(row["requests"] for row in rows)
    tagged = sum(row["requests"] for row in rows if row["user"] is not None)
    return {"rows": rows, "total_requests": total, "tagged_requests": tagged,
            "untagged_requests": total - tagged,
            "coverage_percent": round(tagged / total * 100, 1) if total else None}


def _aws_error(exc: Exception) -> AttributionError:
    from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return AttributionError("AWS_CREDENTIALS_UNAVAILABLE",
                                "The console has no AWS credentials. Use the prepared workshop host.", 503)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "AWS_ERROR")
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            return AttributionError(code, "The host needs CloudWatch Logs query permissions in this region.", 403)
        if code == "ResourceNotFoundException":
            return AttributionError(code, "The telemetry log group is absent. Check the region and whether the agent exported events.", 404)
        if code in {"ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException", "InvalidClientTokenId"}:
            return AttributionError(code, "The host's AWS credentials are invalid or expired. Refresh access and query again.", 503)
        return AttributionError(code, "CloudWatch could not complete the query. Check access and retry.")
    return AttributionError("CLOUDWATCH_UNAVAILABLE",
                            "The console could not reach CloudWatch. Check its AWS access and network.")


def query_attribution(window_hours: int = 3) -> dict[str, Any]:
    """Execute the taught query; expose only a complete CloudWatch result."""
    if type(window_hours) is not int or window_hours not in {1, 3, 24}:
        raise AttributionError("INVALID_WINDOW", "Choose a 1h, 3h, or 24h window.", 400)
    if not _SLOTS.acquire(blocking=False):
        raise AttributionError("QUERY_BUSY", "Two queries are already running. Wait for them to finish.", 429)
    client = None
    query_id = None
    pending = False
    failure: AttributionError | None = None
    try:
        client = _logs_client()
        end = int(time.time())
        group = os.environ.get("WORKSHOP_TELEMETRY_LOG_GROUP", DEFAULT_LOG_GROUP)
        deadline = time.monotonic() + POLL_BUDGET_S
        started = client.start_query(
            logGroupName=group, startTime=end - window_hours * 3600,
            endTime=end, queryString=QUERY, limit=10000)
        query_id = started["queryId"]
        pending = True
        while time.monotonic() < deadline:
            result = client.get_query_results(queryId=query_id)
            state = result.get("status", "Unknown")
            if state == "Complete":
                pending = False
                if not isinstance(result.get("results"), list):
                    raise AttributionError("INVALID_QUERY_RESULT",
                                           "CloudWatch omitted its results. No empty dataset was inferred.")
                if result.get("nextToken") or len(result.get("results", [])) >= 10000:
                    raise AttributionError("RESULT_LIMIT",
                                           "Too many identity groups for a complete view. Choose a shorter window.")
                return {
                    "source": "cloudwatch-logs-insights",
                    "status": "Complete", "query_id": query_id,
                    "region": client.meta.region_name, "log_group": group,
                    "start_time": end - window_hours * 3600, "end_time": end,
                    "query": QUERY, **_summarize(result.get("results", [])),
                }
            if state not in {"Scheduled", "Running"}:
                pending = state not in {"Failed", "Cancelled", "Timeout"}
                raise AttributionError("QUERY_" + state.upper(),
                                       f"CloudWatch query ended with {state}. No partial totals are shown.")
            time.sleep(1)
        raise AttributionError("QUERY_TIMEOUT",
                               "The query exceeded the console's wait budget. Choose a shorter window or retry.", 504)
    except AttributionError as exc:
        failure = exc
        raise
    except Exception as exc:
        failure = _aws_error(exc)
        raise failure from None
    finally:
        if pending and client is not None and query_id is not None:
            try:
                stopped = client.stop_query(queryId=query_id).get("success", False)
            except Exception:
                stopped = False
            if not stopped and failure:
                failure.args = (str(failure) + " Cancellation was not confirmed; inspect the query in CloudWatch.",)
        _SLOTS.release()
