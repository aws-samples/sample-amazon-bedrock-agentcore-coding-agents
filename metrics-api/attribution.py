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

# These are CloudWatch's flattened discovered fields, not jsonParse map keys.
# Claude's body is its canonical emitter; its event.name is only "api_request".
QUERY = """filter body = "claude_code.api_request"
    or (attributes.event.name = "codex.sse_event"
        and attributes.event.kind = "response.completed"
        and ispresent(attributes.input_token_count)
        and not ispresent(attributes.error.message))
| fields coalesce(body, attributes.event.name) as emitter,
         coalesce(attributes.input_tokens, attributes.input_token_count) as measured_input,
         coalesce(attributes.output_tokens, attributes.output_token_count) as measured_output,
         coalesce(attributes.cache_creation_tokens, attributes.cache_write_token_count) as cache_creation,
         coalesce(attributes.cache_read_tokens, attributes.cached_token_count) as cache_read,
         attributes.reasoning_token_count as reasoning_output
| stats sum(measured_input) as input_tokens,
        sum(measured_output) as output_tokens,
        sum(cache_creation) as cache_creation_tokens,
        sum(cache_read) as cache_read_tokens,
        sum(reasoning_output) as reasoning_output_tokens,
        count(*) as requests,
        sum(ispresent(measured_input)) as input_tokens_reported,
        sum(ispresent(measured_output)) as output_tokens_reported,
        sum(ispresent(cache_creation)) as cache_creation_tokens_reported,
        sum(ispresent(cache_read)) as cache_read_tokens_reported,
        sum(ispresent(reasoning_output)) as reasoning_output_tokens_reported
  by emitter, resource.user.id as user
| sort requests desc"""
RAW_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "reasoning_output_tokens",
)
TOKEN_FIELDS = (*RAW_TOKEN_FIELDS, "total_input_tokens")
INPUT_FIELDS = ("input_tokens", "cache_creation_tokens", "cache_read_tokens")
EMITTERS = {
    "claude_code.api_request": {
        "id": "claude-code", "label": "Claude Code",
        "event_description": "Exported API request events",
    },
    "codex.sse_event": {
        "id": "codex", "label": "Codex",
        "event_description": "Completed responses with token usage",
    },
}
DEFAULT_LOG_GROUP = "/workshop/coding-agents/telemetry"
POLL_BUDGET_S = 20
MAX_QUERY_GROUPS = 10000
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
        "agents": list(EMITTERS.values()),
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


def _sum_tokens(parts: list[dict[str, Any]], fields=TOKEN_FIELDS) -> dict[str, Any]:
    """Keep observed partial sums; never fill a missing measurement with zero."""
    row: dict[str, Any] = {
        "requests": sum(part["requests"] for part in parts),
        "reported_requests": {},
    }
    for name in fields:
        row["reported_requests"][name] = sum(
            part["reported_requests"][name] for part in parts)
        values = [part[name] for part in parts if part["reported_requests"][name]]
        row[name] = sum(values) if values and all(value is not None for value in values) else None
    return row


def _complete(row: dict[str, Any], *names: str) -> bool:
    return all(row[name] is not None and row["reported_requests"][name] == row["requests"]
               for name in names)


def _coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(row["requests"] for row in rows)
    tagged = sum(row["requests"] for row in rows if row["user"] is not None)
    return {"total_requests": total, "tagged_requests": tagged,
            "untagged_requests": total - tagged,
            "coverage_percent": round(tagged / total * 100, 1) if total else None}


def _summarize(results: list[list[dict[str, str]]]) -> dict[str, Any]:
    # CloudWatch can return separate groups for missing and empty labels. Normalize
    # those groups before counting/displaying users, without trimming named labels.
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for result in results:
        fields = {field["field"]: field.get("value") for field in result}
        emitter = EMITTERS.get(fields.get("emitter"))
        if emitter is None:
            raise AttributionError("INVALID_QUERY_RESULT",
                                   "CloudWatch returned an unknown usage source.")
        agent = emitter["id"]
        user = fields.get("user")
        if user is not None and not isinstance(user, str):
            raise AttributionError("INVALID_QUERY_RESULT", "CloudWatch returned an invalid user label.")
        user = user if user and user.strip() else None
        requests = _integer(fields.get("requests"))
        part: dict[str, Any] = {"requests": requests, "reported_requests": {}}
        for name in RAW_TOKEN_FIELDS:
            reported = _integer(fields.get(f"{name}_reported"))
            value = _integer(fields.get(name), optional=True)
            if reported > requests or (reported == 0 and value not in (None, 0)):
                raise AttributionError(
                    "INVALID_QUERY_RESULT",
                    "CloudWatch returned inconsistent token coverage; no totals were inferred.")
            # SUM can return zero for a field no event contained. That is unknown,
            # unlike an explicit zero reported by an event.
            part[name] = value if reported else None
            part["reported_requests"][name] = reported
        groups.setdefault((agent, user), []).append(part)

    rows = []
    for (agent, user), parts in groups.items():
        row = {"agent": agent, "user": user, **_sum_tokens(parts, RAW_TOKEN_FIELDS)}
        # The two CLIs report input differently. Claude's input excludes both
        # cache components; Codex's Responses input already includes them.
        # Preserve Codex's observed total even if a cache breakdown is absent.
        if agent == "codex":
            total, reported = row["input_tokens"], row["reported_requests"]["input_tokens"]
            if _complete(row, *INPUT_FIELDS):
                uncached = total - row["cache_creation_tokens"] - row["cache_read_tokens"]
                if uncached < 0:
                    raise AttributionError(
                        "INVALID_QUERY_RESULT", "Codex cache tokens exceed its reported input.")
                row["input_tokens"] = uncached
            else:
                row["input_tokens"] = None
                row["reported_requests"]["input_tokens"] = 0
            row["total_input_tokens"] = total
            row["reported_requests"]["total_input_tokens"] = reported
        else:
            complete = _complete(row, *INPUT_FIELDS)
            row["total_input_tokens"] = sum(row[name] for name in INPUT_FIELDS) if complete else None
            row["reported_requests"]["total_input_tokens"] = row["requests"] if complete else 0
        if (_complete(row, "output_tokens", "reasoning_output_tokens")
                and row["reasoning_output_tokens"] > row["output_tokens"]):
            raise AttributionError(
                "INVALID_QUERY_RESULT", "Reasoning tokens exceed the reported output.")
        rows.append(row)
    rows.sort(key=lambda row: row["requests"], reverse=True)
    agents = []
    for emitter in EMITTERS.values():
        agent_rows = [row for row in rows if row["agent"] == emitter["id"]]
        agents.append({**emitter, **_sum_tokens(agent_rows), **_coverage(agent_rows)})
    return {"rows": rows, "agents": agents, **_coverage(rows)}


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
            endTime=end, queryString=QUERY, limit=MAX_QUERY_GROUPS)
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
                if result.get("nextToken") or len(result["results"]) >= MAX_QUERY_GROUPS:
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
