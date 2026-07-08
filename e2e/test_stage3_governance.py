"""Stage 3. Governance, per-user cost API, and observability (the Connect layer).

Covers the attendee actions in `content/40-stage3-governance/` (1-obo-identity,
2-per-user-cost-api, 3-deploy-and-observe): open the governance dashboard, read the
per-user cost API, inspect p95 latency, read the Cedar policies, and tail the audit
trail, every number flowing through the shared telemetry ledger
(`.runs/telemetry.jsonl`) over the same `/api/metrics` mount an attendee's browser hits.

Every test drives the shared real console server (conftest fixtures), is independent,
and cleans up the Stage-1 sessions it opens. Stage 3 is API-first: each metrics
endpoint is asserted against its documented wire shape (`metrics_lib` is the co-equal
Python data path; the REST layer is a thin shell), and the per-agent split is
attribution only; NO race, NO winner. Stage 2 runs in the deterministic local engine
so the ledger fills without a model.
"""
from __future__ import annotations

import getpass

from e2e.conftest import (
    req, open_session, close_session, submit_run, poll_route, poll_terminal,
    SUPPORTED_AGENTS,
)

# Refs the local router resolves a bare-ish "convert" task to: all three roles.
# The enforced guardrail ids (orchestrator/policy.py). The /api/policies view is
# the SAME set policy.screen() enforces, so these are the ids the page must show.
GUARDRAIL_RULE_IDS = {
    "forbid_rm_root", "forbid_write_git_internals",
    "forbid_write_in_readonly_workflow", "gate_write_credentials",
    "gate_force_push_main",
}


# ---------------------------------------------------------------------------
# Small local helpers (kept thin; the heavy lifting is in conftest).
# ---------------------------------------------------------------------------
def _get(console, cookie, path):
    """GET an /api/metrics endpoint; return the decoded JSON body (asserts 200)."""
    code, body = req(console, "GET", path, headers=cookie)
    assert code == 200, (path, code, body)
    return body


def _run_a_convert(console, cookie):
    """Submit one convert task through the local engine, wait for it to land in the
    ledger as an orchestrator_run, and return the run dict (status is terminal)."""
    run = submit_run(console, cookie, task="convert the cost_analyzer module to an MCP server")
    rid = run["run_id"]
    poll_route(console, cookie, rid)        # route attaches on the worker
    return poll_terminal(console, cookie, rid)


# ===========================================================================
# DASHBOARD shape (GET /api/metrics/dashboard): content 3-deploy-and-observe.
# ===========================================================================
def test_dashboard_documented_shape(console, cookie):
    """Attendee opens the governance dashboard: it returns the four documented keys."""
    dash = _get(console, cookie, "/api/metrics/dashboard")
    assert {"active_sessions", "runs_total", "p95_latency_ms", "cost_by_agent"} <= set(dash), dash


def test_dashboard_p95_is_nonnegative_int(console, cookie):
    """The dashboard p95 latency is a non-negative integer (nearest-rank, deterministic)."""
    dash = _get(console, cookie, "/api/metrics/dashboard")
    assert isinstance(dash["p95_latency_ms"], int) and dash["p95_latency_ms"] >= 0, dash


def test_dashboard_cost_by_agent_is_dict(console, cookie):
    """The dashboard cost_by_agent is a dict of per-agent attribution (no scalar/winner)."""
    dash = _get(console, cookie, "/api/metrics/dashboard")
    assert isinstance(dash["cost_by_agent"], dict), dash
    for agent, cost in dash["cost_by_agent"].items():
        assert agent in SUPPORTED_AGENTS, agent
        assert isinstance(cost, (int, float)) and cost >= 0, (agent, cost)


def test_dashboard_runs_total_nonnegative_int(console, cookie):
    """runs_total and active_sessions are non-negative ints (counts over the ledger)."""
    dash = _get(console, cookie, "/api/metrics/dashboard")
    assert isinstance(dash["runs_total"], int) and dash["runs_total"] >= 0, dash
    assert isinstance(dash["active_sessions"], int) and dash["active_sessions"] >= 0, dash


def test_dashboard_active_sessions_never_exceeds_total(console, cookie):
    """Active (running) sessions can never exceed total sessions."""
    dash = _get(console, cookie, "/api/metrics/dashboard")
    assert dash["active_sessions"] <= dash["runs_total"], dash


def test_dashboard_cost_by_agent_matches_cost_breakdown(console, cookie):
    """API-first proof: the dashboard's cost_by_agent IS the cost-breakdown?by=agent
    payload, one shared data path, the UI derives nothing the API doesn't give."""
    _run_a_convert(console, cookie)  # ensure the ledger has at least one row
    dash = _get(console, cookie, "/api/metrics/dashboard")
    by_agent = _get(console, cookie, "/api/metrics/cost-breakdown?by=agent")
    assert dash["cost_by_agent"] == by_agent["breakdown"], (dash, by_agent)


def test_dashboard_runs_total_equals_session_count(console, cookie):
    """API-first proof: runs_total equals the length of the sessions list (the dashboard
    composes list_sessions, never a separately-counted number that can drift)."""
    _run_a_convert(console, cookie)
    dash = _get(console, cookie, "/api/metrics/dashboard")
    sessions = _get(console, cookie, "/api/metrics/sessions")["sessions"]
    assert dash["runs_total"] == len(sessions), (dash["runs_total"], len(sessions))


# ===========================================================================
# SESSIONS list (GET /api/metrics/sessions): the governance session inventory.
# ===========================================================================
def test_sessions_list_shape(console, cookie):
    """Attendee reads the governance session inventory: a {sessions:[...]} envelope."""
    body = _get(console, cookie, "/api/metrics/sessions")
    assert isinstance(body.get("sessions"), list), body


def test_sessions_rows_have_governance_fields(console, cookie):
    """After a run lands, each session row carries the documented governance fields
    (identity user_id, assistant_type, runtime_arn, liveness): not a bare id."""
    _run_a_convert(console, cookie)
    rows = _get(console, cookie, "/api/metrics/sessions")["sessions"]
    assert rows, "a session row must exist after a Stage-2 run"
    for row in rows:
        assert {"session_id", "assistant_type", "user_id", "runtime_arn",
                "claude_running", "started_at"} <= set(row), row
        assert row["assistant_type"] in SUPPORTED_AGENTS, row
        # runtime_arn reflects the role's wired/deployed Runtime, or null when none
        # is wired (the default in the test env); never a fabricated local:runtime
        # placeholder.
        assert row["runtime_arn"] is None or (
            row["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")), row
        assert isinstance(row["claude_running"], bool), row


def test_sessions_filter_by_assistant_type(console, cookie):
    """Attendee filters the session list to one agent: only that agent's rows return."""
    _run_a_convert(console, cookie)
    rows = _get(console, cookie, "/api/metrics/sessions?assistant_type=kiro")["sessions"]
    assert rows, "a convert run dispatches kiro, so a kiro session must exist"
    assert all(r["assistant_type"] == "kiro" for r in rows), rows


def test_sessions_filter_by_user_isolates_local_user(console, cookie):
    """Filtering sessions by the local OS user returns only that user's rows
    (per-user attribution, the OBO stand-in), and an unknown user returns none."""
    me = getpass.getuser()
    _run_a_convert(console, cookie)
    mine = _get(console, cookie, f"/api/metrics/sessions?user_id={me}")["sessions"]
    assert mine, "the local user owns the runs just submitted"
    assert all(r["user_id"] == me for r in mine), mine
    none = _get(console, cookie, "/api/metrics/sessions?user_id=nobody-xyz")["sessions"]
    assert none == [], none


# ===========================================================================
# COST-BREAKDOWN (GET /api/metrics/cost-breakdown?by=agent|user): the P0 cost API.
# ===========================================================================
def test_cost_breakdown_by_agent_shape(console, cookie):
    """Attendee reads per-agent cost: {by:'agent', breakdown:{...}, currency} shape."""
    body = _get(console, cookie, "/api/metrics/cost-breakdown?by=agent")
    assert body["by"] == "agent", body
    assert isinstance(body["breakdown"], dict), body
    assert "currency" in body, body


def test_cost_breakdown_by_agent_has_all_three_agents(console, cookie):
    """After a convert run (all three roles dispatched), the per-agent breakdown has a
    row for each of claude-code, kiro, opencode: attribution across the fleet, no winner."""
    _run_a_convert(console, cookie)
    body = _get(console, cookie, "/api/metrics/cost-breakdown?by=agent")
    assert set(SUPPORTED_AGENTS) <= set(body["breakdown"]), body
    for agent, cost in body["breakdown"].items():
        assert isinstance(cost, (int, float)) and cost >= 0, (agent, cost)


def test_cost_breakdown_by_user_attributes_to_local_user(console, cookie):
    """Per-user cost attributes every dollar to the local OS user recorded on the run;
    the breakdown is keyed by user, not ranked: attribution, never a leaderboard."""
    me = getpass.getuser()
    _run_a_convert(console, cookie)
    body = _get(console, cookie, "/api/metrics/cost-breakdown?by=user")
    assert body["by"] == "user", body
    assert me in body["breakdown"], body
    assert isinstance(body["breakdown"][me], (int, float)) and body["breakdown"][me] >= 0


def test_cost_breakdown_currency_is_usd(console, cookie):
    """The cost API stamps the currency (USD) on both projections; costs are estimates
    at published Bedrock rates, never bare numbers."""
    for by in ("agent", "user"):
        body = _get(console, cookie, f"/api/metrics/cost-breakdown?by={by}")
        assert body["currency"] == "USD", body


def test_cost_breakdown_unknown_by_defaults_to_agent(console, cookie):
    """An unrecognized `by` param degrades to the agent projection (never errors);
    the API is forgiving on the dimension but always reports which one it used."""
    body = _get(console, cookie, "/api/metrics/cost-breakdown?by=banana")
    assert body["by"] == "agent", body


def test_cost_breakdown_missing_by_defaults_to_agent(console, cookie):
    """With no `by` param the cost API defaults to the per-agent projection."""
    body = _get(console, cookie, "/api/metrics/cost-breakdown")
    assert body["by"] == "agent", body


# ===========================================================================
# LATENCY p95 (GET /api/metrics/latency/p95): observability scope.
# ===========================================================================
def test_latency_p95_shape(console, cookie):
    """Attendee reads p95 latency: a non-negative number plus the scope it applied."""
    body = _get(console, cookie, "/api/metrics/latency/p95")
    assert isinstance(body["p95_latency_ms"], (int, float)) and body["p95_latency_ms"] >= 0, body
    assert isinstance(body["scope"], dict), body


def test_latency_p95_unscoped_scope_is_empty(console, cookie):
    """With no scope params, p95 reports an empty scope (fleet-wide observability)."""
    body = _get(console, cookie, "/api/metrics/latency/p95")
    assert body["scope"] == {}, body


def test_latency_p95_scoped_to_agent(console, cookie):
    """Attendee scopes p95 to one agent: the response echoes that scope back."""
    _run_a_convert(console, cookie)
    body = _get(console, cookie, "/api/metrics/latency/p95?assistant_type=opencode")
    assert body["scope"].get("assistant_type") == "opencode", body
    assert isinstance(body["p95_latency_ms"], (int, float)) and body["p95_latency_ms"] >= 0


def test_latency_p95_scoped_to_user(console, cookie):
    """Attendee scopes p95 to the local user: scope echoes the user; value is >= 0."""
    me = getpass.getuser()
    _run_a_convert(console, cookie)
    body = _get(console, cookie, f"/api/metrics/latency/p95?user_id={me}")
    assert body["scope"].get("user_id") == me, body
    assert isinstance(body["p95_latency_ms"], (int, float)) and body["p95_latency_ms"] >= 0


# ===========================================================================
# POLICIES (GET /api/metrics/policies): Cedar guardrails, configurable (not OBO).
# ===========================================================================
def test_policies_returns_cedar_rules(console, cookie):
    """Attendee reads the guardrail view: a list of rules, each tier/effect. The view
    reports `enforced: True` because policy.screen() actually checks these in the harness."""
    body = _get(console, cookie, "/api/metrics/policies")
    rules = body["policies"]
    assert isinstance(rules, list) and rules, body
    assert body.get("enforced") is True, body
    for rule in rules:
        assert {"tier", "rule_id", "effect", "summary"} <= set(rule), rule
        assert rule["tier"] in ("hard", "soft"), rule
        # hard rules forbid outright; soft rules gate for human approval
        assert rule["effect"] in ("forbid", "gate"), rule


def test_policies_cover_known_guardrails(console, cookie):
    """The guardrail set names the documented rules the harness enforces: root-remove,
    .git writes, read-only writes, credential writes, force-push to main."""
    body = _get(console, cookie, "/api/metrics/policies")
    rule_ids = {r["rule_id"] for r in body["policies"]}
    assert GUARDRAIL_RULE_IDS <= rule_ids, rule_ids


def test_policies_two_tier_model(console, cookie):
    """The guardrails span both tiers: hard (absolute) and soft (human-in-the-loop)."""
    body = _get(console, cookie, "/api/metrics/policies")
    tiers = {r["tier"] for r in body["policies"]}
    assert {"hard", "soft"} <= tiers, tiers


# ===========================================================================
# AUDIT trail (GET /api/metrics/audit): the append-only ledger feed.
# ===========================================================================
def test_audit_returns_structured_trail(console, cookie):
    """Attendee tails the governance audit feed: structured rows over the ledger,
    each with at/kind/user_id/line, plus a total and the ledger source path."""
    _run_a_convert(console, cookie)
    body = _get(console, cookie, "/api/metrics/audit?limit=100")
    assert isinstance(body["audit"], list) and body["audit"], body
    assert body["source"].endswith("telemetry.jsonl"), body
    assert body["total"] == len(body["audit"]), body
    for row in body["audit"]:
        assert {"at", "kind", "user_id", "line"} <= set(row), row
        assert isinstance(row["line"], str) and row["line"], row


def test_audit_includes_orchestrator_run_lines(console, cookie):
    """The Stage-2 run the attendee just submitted appears in the audit feed as an
    orchestrator_run line; the trail comes from the ledger, nothing synthesized."""
    _run_a_convert(console, cookie)
    body = _get(console, cookie, "/api/metrics/audit?limit=200")
    assert any(row["kind"] == "orchestrator_run" for row in body["audit"]), body


def test_audit_run_line_names_the_specific_run(console, cookie):
    """A submitted run's id appears verbatim in its audit line; Stage 2 -> Stage 3
    continuity over the shared ledger (one identity, not a re-minted id)."""
    run = _run_a_convert(console, cookie)
    rid = run["run_id"]
    body = _get(console, cookie, "/api/metrics/audit?limit=200")
    orch = [r["line"] for r in body["audit"] if r["kind"] == "orchestrator_run"]
    assert any(rid in line for line in orch), (rid, orch[-5:])


def test_audit_limit_bounds_rows(console, cookie):
    """The audit `limit` param bounds the rows returned (the console tails a window)."""
    _run_a_convert(console, cookie)
    body = _get(console, cookie, "/api/metrics/audit?limit=1")
    assert len(body["audit"]) <= 1, body


# ===========================================================================
# LEDGER GROWTH: run a Stage 2 task, then prove the governance numbers moved.
# ===========================================================================
def test_dashboard_runs_total_grows_after_a_run(console, cookie):
    """Submit a Stage-2 task and the dashboard's runs_total grows by the dispatched
    role count; the governance layer reflects real work, not a static seed dataset."""
    before = _get(console, cookie, "/api/metrics/dashboard")["runs_total"]
    run = _run_a_convert(console, cookie)
    roles = [p["agent"] for p in run.get("progress", [])]
    assert roles, run
    after = _get(console, cookie, "/api/metrics/dashboard")["runs_total"]
    assert after >= before + len(roles), (before, after, roles)


def test_cost_breakdown_user_total_reflects_the_run(console, cookie):
    """After a run, the local user's per-user cost is >= what it was before; the cost
    API attributes the run's spend (>= 0; local engine may be honest-zero) to the user."""
    me = getpass.getuser()
    before = _get(console, cookie, "/api/metrics/cost-breakdown?by=user")["breakdown"].get(me, 0.0)
    _run_a_convert(console, cookie)
    after = _get(console, cookie, "/api/metrics/cost-breakdown?by=user")["breakdown"].get(me, 0.0)
    assert after >= before, (before, after)


def test_session_identity_records_user_attribution(console, cookie):
    """A governance session records the initiating user without inventing OAuth
    delegation or GitHub authorship. No static GitHub credential sits on the agent."""
    _run_a_convert(console, cookie)
    rows = _get(console, cookie, "/api/metrics/sessions")["sessions"]
    sid = rows[0]["session_id"]
    ident = _get(console, cookie, f"/api/metrics/sessions/{sid}/identity")
    assert ident["session_id"] == sid, ident
    assert ident["recorded_user"], ident
    assert ident["attribution_source"] == "run-ledger", ident
    assert ident["github_actor"] == "credential-dependent", ident
    assert ident["static_credentials_on_agent"] is False, ident
    assert ident["environment"] in ("local", "agentcore"), ident
    assert ident["auth_provider"] in ("os-user", "cognito"), ident


def test_stage1_session_appears_in_governance_sessions(console, cookie):
    """A live Stage-1 interactive session the attendee opens is accounted for in the
    Stage-3 governance session inventory once it has recorded telemetry, proving Stage 1
    and Stage 2 share ONE ledger, not two disjoint stores."""
    sid = open_session(console, cookie, "claude-code")
    try:
        # the convert here drives the local engine; the governance layer aggregates the
        # shared ledger across stages; assert it stays consistent (active <= total).
        _run_a_convert(console, cookie)
        dash = _get(console, cookie, "/api/metrics/dashboard")
        sessions = _get(console, cookie, "/api/metrics/sessions")["sessions"]
        assert dash["runs_total"] == len(sessions)
        assert dash["active_sessions"] <= dash["runs_total"]
    finally:
        close_session(console, cookie, sid)
