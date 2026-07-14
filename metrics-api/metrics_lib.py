"""Metrics library: the API-first P0 (Chandra's hard requirement), the data path.

This module IS the data path. The REST server in `metrics_api.py` is a thin HTTP
shell that calls straight into these four functions, so the Python lib and the REST
surface are co-equal, never two diverging code paths. AWS naming (`list_*`/`get_*`).

    list_sessions(filters=None)
    get_user_metrics(user_id, time_range="24h")
    get_cost_breakdown(by="agent")
    get_latency_p95(scope=None)

Each returns the SAME dict shape as the matching REST endpoint in API_CONTRACT.md.

The data source is the shared telemetry ledger (`.runs/telemetry.jsonl`) that the
orchestrator engine and the Stage 1 interactive API append to as runs happen on this
machine. Run a Module 1 conversion or a Module 2 blueprint and the numbers here move;
run nothing and everything is zero/empty. There is no seed dataset. Identity is the
OS user locally; on AgentCore, `user_id` comes from the authenticated run context.

Per-user attribution is DERIVED from those rows by grouping on `user_id`;
AgentCore Runtime does not yet expose per-user metrics natively (SIFT 5/26 gap).
On AgentCore the rows come from the sdlc session-tracking DynamoDB + CloudWatch/
X-Ray instead of the local ledger; the signatures and shapes do not change.

Token counts in the ledger are the model APIs' own usage figures (Converse /
Responses), priced at published Bedrock per-MTok rates, measured, never inferred
from latency. A run that invoked no model (engine local mode, the Module 1 by-hand
path) reports zero. Costs are not live billing data, and the per-agent split is
attribution only: no race, no winner.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

# Valid assistant types (matches the orchestrator's AGENTS roster). The validator
# is a second Claude Code (claude-code-validator); kiro was retired from the
# roster (kept in the codebase, off every roster, like codex).
ASSISTANT_TYPES = ("claude-code", "claude-code-validator", "opencode")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_RUNS_DIR = os.environ.get("WORKSHOP_RUNS_DIR", os.path.join(_REPO, ".runs"))
_LEDGER = os.path.join(_RUNS_DIR, "telemetry.jsonl")

# The governance rule set the console renders is the SAME set the harness
# enforces (orchestrator/policy.py). We import it so /api/policies can never drift
# from what actually blocks an agent's tool call. If the orchestrator package is
# not reachable (a metrics-only deploy), get_policies() says so honestly.
import sys as _sys  # noqa: E402

# orchestrator/ is a sibling of this metrics-api directory in the single repo
# layout, so resolve it as a peer of _HERE.
_sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "orchestrator"))
try:
    import policy as _policy  # noqa: E402
except Exception:  # noqa: BLE001
    _policy = None

# Runtime configuration still backs the fleet-status endpoints below. Session
# records deliberately use the exact ARN and Runtime session ID written by the
# run ledger, rather than re-resolving the fleet after the fact.
try:
    import runtime_config as _runtime_config  # noqa: E402
except Exception:  # noqa: BLE001
    _runtime_config = None


# ---------------------------------------------------------------------------
# Ledger access: one reader shared by every function (the single data path).
#
# The ledger is an append-only file that can grow to thousands of lines (megabytes).
# A single governance request fans out into several lib calls (get_dashboard alone
# composes cost-breakdown + latency + sessions), so parsing the whole file (and
# re-flattening it into session rows) once PER lib call meant reading multi-MB and
# rebuilding thousands of rows several times for one HTTP request. We cache the
# parsed rows keyed on the file's (mtime, size): a new append changes one or the
# other and invalidates the cache, so the numbers stay live while a burst of reads
# against an unchanged file is served from memory.
# ---------------------------------------------------------------------------
# Parsed-rows cache, keyed on the ledger's (mtime_ns, size). We deliberately cache
# only the heavy parse here (NOT the flattened session list) because flattening
# evaluates a live `_pid_alive` probe for Stage-1 sessions, and we want that
# liveness fresh on every call. With the parse cached and ARN resolution memoized,
# re-flattening is cheap pure-Python dict building.
_LEDGER_CACHE: dict[str, Any] = {"sig": object(), "rows": []}


def _ledger_signature() -> Optional[tuple[int, int]]:
    """(mtime_ns, size) of the ledger, or None if it does not exist yet. Any append
    changes the size (and almost always the mtime), so this is a cheap, reliable
    cache key: one stat() instead of re-reading the whole file."""
    try:
        st = os.stat(_LEDGER)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read_ledger() -> list[dict[str, Any]]:
    """Parsed ledger rows, cached on the file's (mtime, size). Re-parses only when
    the file actually changed; otherwise returns the already-parsed rows."""
    sig = _ledger_signature()
    if sig == _LEDGER_CACHE["sig"]:
        return _LEDGER_CACHE["rows"]
    rows: list[dict[str, Any]] = []
    try:
        with open(_LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn concurrent write never breaks reads
    except OSError:
        pass  # no runs yet -> empty metrics, honestly
    # New snapshot: refresh the parsed ledger rows.
    _LEDGER_CACHE["sig"] = sig
    _LEDGER_CACHE["rows"] = rows
    return rows


def _sessions() -> list[dict[str, Any]]:
    """Flatten ledger rows into per-session records (the Session wire shape).

    Each orchestrator run contributes one session per role agent (that is what
    a run IS on AgentCore: three Runtime sessions); each Stage 1 interactive
    session contributes one row for the agent the attendee deployed.
    """
    out: list[dict[str, Any]] = []
    for row in _read_ledger():
        if row.get("kind") == "orchestrator_run":
            for role in row.get("roles", []):
                out.append({
                    "session_id": f"{row['run_id']}-{role['agent']}",
                    "invocation_number": row.get("iterations", 1),
                    "runtime_arn": role.get("runtime_arn"),
                    "_runtime_session_id": role.get("runtime_session_id"),
                    "_pid": None,
                    "assistant_type": role["agent"],
                    "user_id": row.get("user_id", "unknown"),
                    "user_email": row.get("user_email", ""),
                    "user_name": row.get("user_name", ""),
                    "started_at": row.get("started_at", ""),
                    "issue_url": None,
                    "claude_running": False,  # engine runs are terminal by ledger time
                    "_tokens": role.get("tokens", 0),
                    "_cost_usd": role.get("cost_usd", 0.0),
                    "_latency_ms": role.get("latency_ms", 0),
                    "_estimated": role.get("estimated", False),
                })
        elif row.get("kind") == "stage1_conversion":
            out.append({
                "session_id": row.get("session_id", "sess"),
                "invocation_number": 1,
                # The Stage 1 preview is a local process even when the attendee
                # also has a deployed Runtime configured for that agent.
                "runtime_arn": row.get("runtime_arn"),
                "_runtime_session_id": row.get("runtime_session_id"),
                "_pid": row.get("pid"),
                "assistant_type": row.get("agent_id", "claude-code"),
                "user_id": row.get("user_id", "unknown"),
                "user_email": row.get("user_email", ""),
                "user_name": row.get("user_name", ""),
                "started_at": row.get("started_at", ""),
                "issue_url": None,
                "claude_running": _pid_alive(row.get("pid")),
                "_tokens": 0,             # no model invoked in the by-hand path
                "_cost_usd": 0.0,
                "_latency_ms": row.get("latency_ms", 0),
                "_estimated": False,      # zero is a fact, not an estimate
            })
    return out


def _pid_alive(pid) -> bool:
    """Liveness probe: the local stand-in for the sdlc inspector's /proc check."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


# Sessions killed via the stop kill switch (POST /api/sessions/{id}/stop).
_STOPPED: set[str] = set()


def _public_session(row: dict[str, Any]) -> dict[str, Any]:
    claude_running = row["claude_running"] and row["session_id"] not in _STOPPED
    return {
        "session_id": row["session_id"],
        "invocation_number": row["invocation_number"],
        "runtime_arn": row["runtime_arn"],
        "assistant_type": row["assistant_type"],
        "user_id": row["user_id"],
        "started_at": row["started_at"],
        "issue_url": row["issue_url"],
        "claude_running": claude_running,
    }


def _percentile_95(values: list[int]) -> int:
    """p95 over per-session latencies. Nearest-rank, deterministic. Empty -> 0."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = -(-95 * len(ordered) // 100)  # ceil(0.95 * n)
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


def _round2(x: float) -> float:
    return round(x + 0.0, 2)


def _within(row: dict[str, Any], time_range: str) -> bool:
    """Filter a row by lookback range ('24h', '7d', '30m', 'all')."""
    units = {"m": 60, "h": 3600, "d": 86400}
    tr = (time_range or "24h").strip().lower()
    if tr in ("all", ""):
        return True
    try:
        seconds = int(tr[:-1]) * units[tr[-1]]
    except (KeyError, ValueError):
        return True
    try:
        started = time.mktime(time.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (KeyError, ValueError):
        return True
    return (time.time() - started) <= seconds


# ---------------------------------------------------------------------------
# Per-user cost from the Bedrock model-invocation log (the claude-code path).
#
# The coding agents run their CLI inside their AgentCore Runtime over the command
# shell, so they emit no in-process usage the ledger can read (that path records an
# honest zero). But every model call they make is a real Bedrock call, and Bedrock
# model-invocation logging records each one with the caller identity and the four
# token counts. When the agent assumes a per-user session
# (assumed-role/<PERUSER_ROLE>/<user>), that identity carries the user, so this log
# is a real per-user cost source with no in-container collector.
#
# This is the native-Bedrock path: claude-code, the claude-code-validator, AND
# opencode (the frontend, on the amazon-bedrock provider) all make real Bedrock
# calls, so all appear here with their caller identity. See the Stage 3 observe
# lab for how telemetry attributes per user. Fail-soft everywhere: any problem
# (logging off, role unused, AWS unreachable, offline tests) returns {} and the callers fall
# back to the ledger path, never an invented number.
# ---------------------------------------------------------------------------
_INVOCATION_LOG_GROUP = os.getenv("BEDROCK_INVOCATION_LOG_GROUP", "/aws/bedrock/modelinvocations")


def _resolve_peruser_role() -> str:
    """The per-user role NAME the invocation-log query filters on. Prefer an
    explicit PERUSER_ROLE_NAME, else derive it from PERUSER_ROLE_ARN (the env the
    workshop CFN sets on the console, region-suffixed e.g. cca-peruser-us-east-1)
    so the metrics query matches the role the runtime actually assumed without a
    second env var. Falls back to the legacy name only when neither is set."""
    name = os.getenv("PERUSER_ROLE_NAME")
    if name:
        return name
    arn = os.getenv("PERUSER_ROLE_ARN", "")
    if "/" in arn:
        return arn.rsplit("/", 1)[-1]
    return "cca-claude-peruser"


_PERUSER_ROLE = _resolve_peruser_role()
_BEDROCK_REGION = os.getenv("WORKSHOP_BEDROCK_REGION", os.getenv("AWS_REGION", "us-west-2"))
# Illustrative per-MTok USD rates (NOT live pricing; documented as approximate).
_PERMTOK = {"in": 15.0, "out": 75.0, "cache_read": 1.5, "cache_write": 18.75}
_RANGE_UNITS = {"m": 60, "h": 3600, "d": 86400}

# The exact Logs Insights query, proven against the live log. Two CWLI rules it
# respects: pre-alias the dotted JSON fields in `fields` (you cannot sum a dotted
# field directly), and never alias an aggregate to `input`/`output` (collides with
# the input.*/output.* field namespace). %s is the per-user role name.
_PERUSER_QUERY = (
    "fields identity.arn as arn, input.inputTokenCount as itok, "
    "output.outputTokenCount as otok, input.cacheReadInputTokenCount as crtok, "
    "input.cacheWriteInputTokenCount as cwtok "
    "| filter arn like /%(role)s/ "
    "| parse arn \"assumed-role/%(role)s/*\" as user "
    "| stats sum(itok) as input_tokens, sum(otok) as output_tokens, "
    "sum(crtok) as cache_read, sum(cwtok) as cache_write, count(*) as calls by user "
    "| sort user asc"
)


def _range_seconds(time_range: str) -> int:
    tr = (time_range or "24h").strip().lower()
    if tr in ("all", ""):
        return 86400 * 30
    try:
        return int(tr[:-1]) * _RANGE_UNITS[tr[-1]]
    except (KeyError, ValueError):
        return 86400


def _cost_usd(i: int, o: int, cr: int, cw: int) -> float:
    return _round2((i * _PERMTOK["in"] + o * _PERMTOK["out"]
                    + cr * _PERMTOK["cache_read"] + cw * _PERMTOK["cache_write"]) / 1_000_000)


def bedrock_user_usage(time_range: str = "24h") -> dict[str, dict[str, Any]]:
    """Per-user token + cost for the claude-code path, read from the Bedrock
    model-invocation log via one Logs Insights query. Returns
    {user: {input, output, cache_read, cache_write, total_tokens, calls, cost_usd}}.
    Fail-soft: {} on any error so callers fall back to the ledger."""
    # Opt-in: off by default so the test suite and local dev never hit live
    # CloudWatch (and stay fast). The workshop console / lab sets
    # METRICS_BEDROCK_INVOCATION_LOG=1 to turn the real per-user source on.
    if os.getenv("METRICS_BEDROCK_INVOCATION_LOG", "").lower() not in ("1", "true", "yes", "on"):
        return {}
    try:
        import boto3  # noqa: PLC0415 (lazy: keeps offline import/tests AWS-free)
        import time as _t  # noqa: PLC0415
        logs = boto3.client("logs", region_name=_BEDROCK_REGION)
        now = int(_t.time())
        qid = logs.start_query(
            logGroupName=_INVOCATION_LOG_GROUP,
            startTime=now - _range_seconds(time_range), endTime=now,
            queryString=_PERUSER_QUERY % {"role": _PERUSER_ROLE},
        )["queryId"]
        res: dict[str, Any] = {"status": "Running"}
        for _ in range(30):
            _t.sleep(1)
            res = logs.get_query_results(queryId=qid)
            if res.get("status") in ("Complete", "Failed", "Cancelled"):
                break
        out: dict[str, dict[str, Any]] = {}
        for row in res.get("results", []):
            d = {f["field"]: f["value"] for f in row}
            user = d.get("user")
            if not user:
                continue
            i = int(d.get("input_tokens", 0) or 0)
            o = int(d.get("output_tokens", 0) or 0)
            cr = int(d.get("cache_read", 0) or 0)
            cw = int(d.get("cache_write", 0) or 0)
            out[user] = {"input": i, "output": o, "cache_read": cr, "cache_write": cw,
                         "total_tokens": i + o + cr + cw, "calls": int(d.get("calls", 0) or 0),
                         "cost_usd": _cost_usd(i, o, cr, cw)}
        return out
    except Exception:  # noqa: BLE001 (fail-soft; the ledger path is the fallback)
        return {}


# ---------------------------------------------------------------------------
# The four co-equal API functions.
# ---------------------------------------------------------------------------
def list_sessions(filters: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """List sessions, optionally filtered. Mirrors `GET /api/sessions`.

    `filters` keys (all optional): user_id, assistant_type,
    window (lookback in MINUTES).
    """
    filters = filters or {}
    user_id = filters.get("user_id")
    assistant_type = filters.get("assistant_type")
    window = filters.get("window")
    out = []
    for row in _sessions():
        if user_id and row["user_id"] != user_id:
            continue
        if assistant_type and row["assistant_type"] != assistant_type:
            continue
        if window and not _within(row, f"{window}m"):
            continue
        out.append(_public_session(row))
    return {"sessions": out}


def get_user_metrics(user_id: str, time_range: str = "24h") -> dict[str, Any]:
    """Per-user roll-up (the P0). Mirrors `GET /api/users/{user_id}/metrics`.

    DERIVED: groups the ledger rows on `user_id` and aggregates the model APIs'
    own token counts, cost at published rates, latency p95, and run count.
    """
    rows = [r for r in _sessions()
            if r["user_id"] == user_id and _within(r, time_range)]
    by_agent: dict[str, float] = {}
    for r in rows:
        by_agent[r["assistant_type"]] = _round2(
            by_agent.get(r["assistant_type"], 0.0) + r["_cost_usd"])
    total_tokens = sum(r["_tokens"] for r in rows)
    total_cost = _round2(sum(r["_cost_usd"] for r in rows))
    runs = len(rows)
    source = "ledger"

    # Prefer the real Bedrock model-invocation log for this user's tokens + cost
    # (the claude-code path). The ledger still supplies latency; it records the
    # coding agents' own usage as honest zero, so without this the per-user cost
    # would read zero. Fail-soft: if the log has nothing for this user, keep ledger.
    bedrock = bedrock_user_usage(time_range).get(user_id)
    if bedrock:
        total_tokens = bedrock["total_tokens"]
        total_cost = bedrock["cost_usd"]
        by_agent = {"claude-code": bedrock["cost_usd"]}
        runs = runs or bedrock["calls"]
        source = "bedrock-invocation-log"

    return {
        "user_id": user_id,
        "time_range": time_range,
        "runs": runs,
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "p95_latency_ms": _percentile_95([r["_latency_ms"] for r in rows]),
        "by_agent": by_agent,
        "source": source,
    }


def get_cost_breakdown(by: str = "agent") -> dict[str, Any]:
    """Cost attribution. Mirrors `GET /api/cost-breakdown?by=agent|user`.

    Token counts priced at published Bedrock rates: attribution only,
    never a ranking between agents.
    """
    if by not in ("agent", "user"):
        by = "agent"
    # Per-user: prefer the real Bedrock model-invocation log (the claude-code path,
    # where the agent's calls sign as the user); fall back to the ledger when it has
    # nothing (offline, or no per-user runs yet). This is what lights up the console
    # chargeback view with real per-user cost.
    if by == "user":
        users = bedrock_user_usage("all")
        if users:
            return {"by": "user",
                    "breakdown": {u: v["cost_usd"] for u, v in users.items()},
                    "currency": "USD", "source": "bedrock-invocation-log"}
    key = "assistant_type" if by == "agent" else "user_id"
    breakdown: dict[str, float] = {}
    for r in _sessions():
        bucket = r[key]
        breakdown[bucket] = _round2(breakdown.get(bucket, 0.0) + r["_cost_usd"])
    return {"by": by, "breakdown": breakdown, "currency": "USD", "source": "ledger"}


def get_latency_p95(scope: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """p95 latency. Mirrors `GET /api/latency/p95`. Scope: assistant_type, user_id."""
    scope = scope or {}
    assistant_type = scope.get("assistant_type")
    user_id = scope.get("user_id")
    rows = _sessions()
    applied: dict[str, str] = {}
    if assistant_type:
        rows = [r for r in rows if r["assistant_type"] == assistant_type]
        applied["assistant_type"] = assistant_type
    if user_id:
        rows = [r for r in rows if r["user_id"] == user_id]
        applied["user_id"] = user_id
    return {"p95_latency_ms": _percentile_95([r["_latency_ms"] for r in rows]),
            "scope": applied}


# ---------------------------------------------------------------------------
# Governance helpers: shared so REST stays a thin shell (ONE data path).
# ---------------------------------------------------------------------------
def get_identity(session_id: str) -> Optional[dict[str, Any]]:
    """Recorded user attribution for a session.

    The run ledger proves which authenticated console user started the work. It
    does not prove that GitHub received that user's delegated OAuth identity.
    GitHub authorship depends on the credential selected by ``github.py``. Keep
    those two facts separate so this endpoint never presents attribution baggage
    as an OBO attestation.
    """
    row = next((r for r in _sessions() if r["session_id"] == session_id), None)
    if row is None:
        return None
    on_agentcore = bool(os.environ.get("AGENTCORE_RUNTIME_ID")
                        or os.environ.get("BEDROCK_AGENTCORE_RUNTIME_ARN"))
    return {
        "session_id": session_id,
        "recorded_user": row["user_id"],
        "user_email": row.get("user_email", ""),
        "user_name": row.get("user_name", ""),
        "auth_provider": "cognito" if row.get("user_email") else "os-user",
        "environment": "agentcore" if on_agentcore else "local",
        "attribution_source": "run-ledger",
        "github_actor": "credential-dependent",
        "static_credentials_on_agent": False,
    }


def _stop_runtime_session(runtime_arn: str, runtime_session_id: str) -> dict[str, Any]:
    """Call AgentCore StopRuntimeSession against a deployed runtime.

    The kill switch on the governed path: it terminates the live Runtime session
    (the per-session microVM compute) while persistent storage on the S3Files mount
    survives. Returns a small result dict; raises on the boto error so the caller
    can record that the call failed rather than report a stop that did not happen."""
    import boto3  # noqa: PLC0415 (lazy, mirrors executor.py / llm.py)
    region = os.environ.get("WORKSHOP_BEDROCK_REGION", "us-west-2")
    # The runtime ARN carries its region (arn:aws:bedrock-agentcore:<region>:...);
    # prefer it so the client targets the runtime's own region.
    try:
        arn_region = runtime_arn.split(":")[3]
        if arn_region:
            region = arn_region
    except IndexError:
        pass
    client = boto3.client("bedrock-agentcore", region_name=region)
    client.stop_runtime_session(
        runtimeSessionId=runtime_session_id,
        agentRuntimeArn=runtime_arn,
    )
    return {"mechanism": "StopRuntimeSession", "agent_runtime_arn": runtime_arn,
            "region": region}


def stop_session(session_id: str) -> Optional[dict[str, Any]]:
    """Kill switch. Mirrors `POST /api/sessions/{id}/stop`.

    Two paths, picked by what the session is:
      * a session attributed to a DEPLOYED runtime (its row carries an
        ``arn:aws:bedrock-agentcore`` ARN) is killed with AgentCore
        ``StopRuntimeSession`` against that runtime, the governed kill switch;
      * a local Stage-1 session (no wired ARN, just a recorded pid) is killed by
        signalling that process, the local stand-in.
    Either way the session is marked stopped so the view reflects it immediately,
    and the result reports WHICH mechanism fired: if StopRuntimeSession errors,
    that error is surfaced rather than a fabricated success."""
    row = next((r for r in _sessions() if r["session_id"] == session_id), None)
    if row is None:
        return None

    if session_id in _STOPPED:
        return {"session_id": session_id, "stopped": True, "mechanism": "already-stopped"}

    runtime_arn = row.get("runtime_arn")
    if runtime_arn and str(runtime_arn).startswith("arn:aws:bedrock-agentcore:"):
        # Governed path: terminate the real Runtime session. The runtimeSessionId
        # must be the exact ID recorded when the role was dispatched. A logical
        # run/agent label is not a substitute, and re-resolving a current target
        # can stop the wrong Runtime after a fleet change.
        runtime_session_id = row.get("_runtime_session_id")
        if not runtime_session_id:
            return {"session_id": session_id, "stopped": False,
                    "mechanism": "StopRuntimeSession",
                    "error": "RUNTIME_SESSION_ID_UNAVAILABLE",
                    "agent_runtime_arn": runtime_arn}
        try:
            result = {"session_id": session_id, "stopped": True}
            result.update(_stop_runtime_session(str(runtime_arn), str(runtime_session_id)))
        except Exception as exc:  # noqa: BLE001 (surface the real failure, never fake a stop)
            return {"session_id": session_id, "stopped": False,
                    "mechanism": "StopRuntimeSession", "error": str(exc),
                    "agent_runtime_arn": runtime_arn}
    else:
        # Local stand-in: only report success after signalling the recorded,
        # still-live Stage 1 preview process.
        pid = row.get("_pid")
        if not pid or not _pid_alive(pid):
            return {"session_id": session_id, "stopped": False,
                    "mechanism": "local-process-signal",
                    "error": "LOCAL_SESSION_NOT_RUNNING"}
        try:
            os.kill(int(pid), 15)
        except OSError as exc:
            return {"session_id": session_id, "stopped": False,
                    "mechanism": "local-process-signal", "error": str(exc)}
        result = {"session_id": session_id, "stopped": True,
                  "mechanism": "local-process-signal"}

    _STOPPED.add(session_id)
    return result


def get_policies() -> dict[str, Any]:
    """Governance policy view (read-only). Mirrors `GET /api/policies`.

    These are the SAME guardrails the harness enforces (orchestrator/policy.py):
    the engine calls ``policy.screen()`` at its command boundary (``Run.term``
    screens every shell command a role runs) before it executes, so a blocked
    action (write under .git/, a credential file, ``rm -rf /``, a force-push to
    main, a write in a read-only workflow) is refused with the matched rule id.
    The list shown here is the list enforced; they cannot drift. Two tiers:
    hard = absolute deny; soft = human-in-the-loop gate.
    """
    if _policy is not None:
        return _policy.get_policies()
    return {"policies": [], "enforced": False,
            "note": "orchestrator policy module not reachable from this deploy"}


def get_audit_trail(limit: int = 200) -> dict[str, Any]:
    """The governance audit feed. Mirrors `GET /api/audit`.

    Every row is a ledger event (orchestrator runs, stage-1 sessions,
    conversions, deploys, verifies) rendered as one auditable line, the
    append-only audit discipline. The console streams this into the governance
    terminal.
    """
    lines: list[dict[str, Any]] = []
    for row in _read_ledger()[-limit:]:
        kind = row.get("kind", "event")
        user = row.get("user_id", "?")
        when = row.get("started_at") or row.get("at") or ""
        if kind == "orchestrator_run":
            roles = ",".join(r.get("agent", "?") for r in row.get("roles", []))
            cost = sum(r.get("cost_usd", 0) for r in row.get("roles", []))
            msg = (f"run {row.get('run_id')} [{row.get('workflow_ref') or 'n/a'}] "
                   f"user={user} agents={roles or '-'} status={row.get('status')} "
                   f"review={row.get('review_state') or 'n/a'} "
                   f"iter={row.get('iterations')} cost=${cost:.2f}"
                   + (f" pr={row.get('pr_url')}" if row.get("pr_url") else ""))
        elif kind.startswith("stage1"):
            msg = (f"{kind} user={user} agent={row.get('agent_id', row.get('agent', '?'))}"
                   + (f" session={row.get('session_id')}" if row.get("session_id") else "")
                   + (f" passed={row.get('passed')}" if "passed" in row else ""))
        else:
            msg = f"{kind} user={user}"
        lines.append({"at": when, "kind": kind, "user_id": user, "line": msg})
    return {"audit": lines, "total": len(lines), "source": ".runs/telemetry.jsonl"}


def get_dashboard() -> dict[str, Any]:
    """Thin CloudWatch-style rollup. Mirrors `GET /api/dashboard`.

    Composes the four metric functions, deriving nothing they don't already
    give, proving the API-first invariant (the UI is a thin view over ONE path).
    """
    cost_by_agent = get_cost_breakdown(by="agent")["breakdown"]
    p95 = get_latency_p95()["p95_latency_ms"]
    sessions = list_sessions()["sessions"]
    active = sum(1 for s in sessions if s["claude_running"])
    return {
        "cost_by_agent": cost_by_agent,
        "p95_latency_ms": p95,
        "active_sessions": active,
        "runs_total": len(sessions),
    }


# ---------------------------------------------------------------------------
# AgentCore execution surface: governance is not just a read view of the ledger;
# it can SEE and ACT on the deployed runtimes. These two functions reach the same
# wirable runtime config + dispatch path the orchestrator uses, so the page shows
# and acts on the deployed fleet.
# ---------------------------------------------------------------------------
def list_runtimes() -> dict[str, Any]:
    """The deployed-runtime wiring view. Mirrors `GET /api/runtimes`.

    Returns, per role, whether a runtime is wired, from where (env / settings),
    the ARN tail, and the fleet size, read straight from ``runtime_config`` (the
    SAME surface the orchestrator dispatches against). A role with no wired ARN is
    ``wired: false``."""
    if _runtime_config is None:
        return {"executor": "agentcore", "remote_dispatch": True, "roles": [],
                "note": "runtime_config module not reachable from this deploy"}
    return _runtime_config.status()


def dispatch_probe(role: str = "claude-code") -> dict[str, Any]:
    """Prove live execution: dispatch a tiny job to a role's deployed runtime and
    read its echoed artifact back. Mirrors `POST /api/runtimes/{role}/probe`.

    This is the governance page's "is the fleet alive" button. It runs the role's
    CLI inside its deployed AgentCore Runtime over the command shell (the
    orchestrator's ``runtime_exec.run_in_runtime``), asking it to write a one-line
    health marker, then reads that file back. A role with no wired ARN fails loud
    (``wired: false``). The dispatch is billable, so it is a deliberate, single,
    minimal job."""
    if _runtime_config is None:
        return {"role": role, "ok": False, "error": "runtime_config not reachable"}
    hit = _runtime_config.resolve(role)
    if not hit:
        return {"role": role, "ok": False, "wired": False,
                "error": f"no AgentCore runtime wired for role '{role}'"}
    arn, source = hit

    # Import the orchestrator's real dispatcher lazily (it is a sibling package in
    # both layouts; the sys.path for orchestrator/ was added at module import).
    try:
        import runtime_exec  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"role": role, "ok": False, "wired": True, "arn": arn,
                "error": f"runtime_exec not reachable: {exc}"}

    # A minimal, deterministic probe: have the agent write one marker line to an
    # artifact in a throwaway run subdir, then read it back. This exercises the
    # dispatch + S3Files round-trip without running a full build.
    marker = f"agentcore-probe-ok:{role}"
    artifact_rel = "probe.txt"
    run_subdir = f"governance-probe/{role}"
    prompt = (f"Write exactly the text '{marker}' (no quotes, no extra words) to a "
              f"file named {artifact_rel} in the current directory, then stop.")
    try:
        result = runtime_exec.run_in_runtime(
            arn, role, prompt, run_subdir, artifact_rel,
            model="", on_line=None, timeout_s=120.0)
    except Exception as exc:  # noqa: BLE001 (surface the real dispatch failure)
        return {"role": role, "ok": False, "wired": True, "arn": arn,
                "source": source, "error": str(exc)}
    artifact = (result.get("artifact") or "").strip()
    return {
        "role": role,
        "ok": marker in artifact,
        "wired": True,
        "arn": arn,
        "source": source,
        "marker_echoed": marker in artifact,
        "artifact_preview": artifact[:200],
        "session_id": result.get("session_id"),
    }


if __name__ == "__main__":
    # Smoke: print the four co-equal functions against the ledger.
    # Resolve the user from the ledger's OWN most-recent row, not getpass: a run
    # submitted through the console is attributed to the Cognito user (the sub),
    # not the OS login (`ubuntu`), so filtering by getpass would show empty
    # per-user rows even though the ledger has data. Fall back to the OS login
    # only when the ledger is empty (nothing to key on yet).
    import getpass

    _rows = _read_ledger()
    me = (_rows[-1].get("user_id") if _rows else None) or getpass.getuser()
    print(f"# user_id resolved from the ledger: {me}")
    print(json.dumps(list_sessions({"user_id": me}), indent=2))
    print(json.dumps(get_user_metrics(me, "24h"), indent=2))
    print(json.dumps(get_cost_breakdown(by="agent"), indent=2))
    print(json.dumps(get_latency_p95({"user_id": me}), indent=2))
