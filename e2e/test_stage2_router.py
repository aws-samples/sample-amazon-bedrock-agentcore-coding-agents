"""Stage 2, the admission router ladder: the heart of "routed, fire-and-forget, NO race".

Workshop step: an attendee submits ONE task into the orchestration chat. Before any
agent runs, admission ROUTES the task against the workflow registry (router.py: explicit
workflow_ref → explicit agent intent → review → full-stack → patch → convert default) and
ONLY the routed roles dispatch. These tests drive the real console `/api/orchestrator` mounts over
HTTP and assert every documented phrasing → route, the registry/agent contracts, and the
fail-loud behavior on an unknown workflow_ref; the exact guarantees the console renders
when an attendee watches a run route itself.

Stage 2 runs in the deterministic LOCAL engine (no model). A submitted run routes on a
worker thread, so route facts come from poll_route (the route attaches there); the
unknown-ref case never attaches a route, so it is asserted via the failed terminal status
+ reason instead.
"""
from __future__ import annotations

import time
from urllib.error import HTTPError

import pytest

from e2e.conftest import (
    req,
    submit_run,
    poll_route,
    poll_terminal,
    LGTM_TOKEN,
)

# The frozen registry the router resolves against: refs, their dispatch lists, and the
# usecase each one grades with. Every assertion below cross-checks the live server against
# this table, so a registry drift in router.py fails a test rather than slipping by.
EXPECTED_WORKFLOWS = {
    "convert/sample-to-mcp-v1": {
        "agents": ["claude-code", "kiro", "opencode"],
        "usecase": "sample-to-mcp",
        "read_only": False,
    },
    "build/fullstack-v1": {
        "agents": ["claude-code", "kiro", "opencode"],
        "usecase": "critter-lab",
        "read_only": False,
    },
    "patch/backend-v1": {
        "agents": ["claude-code"],
        "usecase": "sample-to-mcp",
        "read_only": False,
    },
    "patch/frontend-v1": {
        "agents": ["opencode"],
        "usecase": "sample-to-mcp",
        "read_only": False,
    },
    "review/pr-v1": {
        "agents": ["kiro"],
        "usecase": "sample-to-mcp",
        "read_only": True,
    },
}
WORKFLOW_FIELDS = ("workflow_ref", "version", "agents", "usecase", "read_only", "description")
ORCHESTRATOR_ROLES = ("claude-code", "kiro", "opencode")


def _active_runs(console, cookie) -> int:
    """How many runs are still executing (queued/running) on the shared engine."""
    _, out = req(console, "GET", "/api/orchestrator/runs", headers=cookie)
    return sum(1 for r in out["runs"] if r["status"] in ("queued", "running"))


def _settled_run(console, cookie, task: str, attempts: int = 12) -> dict:
    """Submit a task and return its terminal run /result, retrying past a transient
    capacity bounce. The shared engine caps concurrency at 3; under full-suite load a
    late submit can be rejected with CONCURRENCY_LIMIT (transient → needs_human). That is
    correct engine behavior, not a routing fact, so for the end-to-end gate assertions we
    first wait for the in-flight runs (the parametrized sweep above) to drain, then submit,
    and resubmit if we still raced into the cap; until the run is actually executed."""
    last = None
    for _ in range(attempts):
        for _ in range(40):  # wait for a free slot before submitting (≤ ~12s)
            if _active_runs(console, cookie) < 3:
                break
            time.sleep(0.3)
        run = submit_run(console, cookie, task=task)
        poll_terminal(console, cookie, run["run_id"])
        code, result = req(console, "GET", f"/api/orchestrator/runs/{run['run_id']}/result",
                           headers=cookie)
        assert code == 200, result
        last = result
        if (result.get("fail_reason") or "").split(":", 1)[0] != "CONCURRENCY_LIMIT":
            return result
        time.sleep(1.0)
    return last


def _workflows(console, cookie) -> list[dict]:
    code, out = req(console, "GET", "/api/orchestrator/workflows", headers=cookie)
    assert code == 200, out
    return out["workflows"]


def _agents(console, cookie) -> list[dict]:
    code, out = req(console, "GET", "/api/orchestrator/agents", headers=cookie)
    assert code == 200, out
    return out["agents"]


# ---------------------------------------------------------------------------
# GET /workflows: the registry contract the console renders before any run.
# ---------------------------------------------------------------------------
def test_workflows_endpoint_lists_exactly_the_five_refs(console, cookie):
    """Attendee opens the workflow picker: the registry shows exactly the 5 known workflows."""
    refs = {w["workflow_ref"] for w in _workflows(console, cookie)}
    assert refs == set(EXPECTED_WORKFLOWS), refs


def test_every_workflow_has_all_six_fields(console, cookie):
    """The picker can render each workflow: every entry carries all 6 documented fields."""
    for w in _workflows(console, cookie):
        missing = [f for f in WORKFLOW_FIELDS if f not in w]
        assert not missing, f"{w.get('workflow_ref')} missing {missing}"


def test_every_workflow_agents_non_empty(console, cookie):
    """No workflow dispatches zero roles: every registry entry has a non-empty agents list."""
    for w in _workflows(console, cookie):
        assert isinstance(w["agents"], list) and w["agents"], w


def test_workflow_agents_match_the_registry_table(console, cookie):
    """The dispatch list shown for each ref matches the frozen registry (no drift)."""
    by_ref = {w["workflow_ref"]: w for w in _workflows(console, cookie)}
    for ref, expected in EXPECTED_WORKFLOWS.items():
        assert by_ref[ref]["agents"] == expected["agents"], ref


def test_workflow_usecase_and_read_only_match_the_registry_table(console, cookie):
    """Each ref's usecase + read_only flag match the table (review/pr-v1 is the only read_only)."""
    by_ref = {w["workflow_ref"]: w for w in _workflows(console, cookie)}
    for ref, expected in EXPECTED_WORKFLOWS.items():
        assert by_ref[ref]["usecase"] == expected["usecase"], ref
        assert by_ref[ref]["read_only"] == expected["read_only"], ref


def test_only_review_workflow_is_read_only(console, cookie):
    """Exactly one workflow is review-style/read-only: review/pr-v1, the no-build gate."""
    read_only = {w["workflow_ref"] for w in _workflows(console, cookie) if w["read_only"]}
    assert read_only == {"review/pr-v1"}, read_only


def test_workflow_refs_use_documented_agent_ids_only(console, cookie):
    """Every agent named in the registry is one of the 3 known orchestrator roles."""
    known = set(ORCHESTRATOR_ROLES)
    for w in _workflows(console, cookie):
        unknown = [a for a in w["agents"] if a not in known]
        assert not unknown, f"{w['workflow_ref']} names unknown agents {unknown}"


# ---------------------------------------------------------------------------
# GET /s2/agents: the orchestrator's three composed roles.
# ---------------------------------------------------------------------------
def test_agents_endpoint_has_the_three_roles(console, cookie):
    """Attendee sees the harness: the orchestrator exposes exactly claude-code, kiro, opencode."""
    ids = {a["id"] for a in _agents(console, cookie)}
    assert ids == set(ORCHESTRATOR_ROLES), ids


def test_each_orchestrator_agent_declares_a_model_and_credential(console, cookie):
    """Each role advertises a model + credential broker (taught: tools/creds never touch the agent)."""
    for a in _agents(console, cookie):
        assert a["model"], a
        assert a["credential"], a


def test_registry_agents_are_a_subset_of_orchestrator_agents(console, cookie):
    """Every agent any workflow can dispatch is actually a registered orchestrator role."""
    role_ids = {a["id"] for a in _agents(console, cookie)}
    used = {a for w in _workflows(console, cookie) for a in w["agents"]}
    assert used <= role_ids, used - role_ids


# ---------------------------------------------------------------------------
# The router ladder, every documented phrasing → route. Parametrized.
# Each (task, ref, agents, rule_substr) is one attendee phrasing in the chat.
# ---------------------------------------------------------------------------
PHRASING_CASES = [
    # 6. convert intent / default; all three roles, the module-to-MCP conversion.
    pytest.param(
        "Convert /mnt/s3files/sample/cost_analyzer.py to a remote MCP server with a chatbot UI",
        "convert/sample-to-mcp-v1", ["claude-code", "kiro", "opencode"], "conversion",
        id="convert-explicit-word",
    ),
    pytest.param(
        "Turn the cost analyzer skill into an MCP server",
        "convert/sample-to-mcp-v1", ["claude-code", "kiro", "opencode"], "conversion",
        id="convert-mcp-server-phrase",
    ),
    # NOTE: there is deliberately NO "intent-less phrasing -> convert default" case
    # here. The router is task-agnostic: a task that matches no intent fails loud
    # (NO_ROUTE), it does NOT silently become the cost-analyzer conversion. That
    # fail-loud edge is asserted directly in test_no_intent_fails_loud_* below.
    # 4. full-stack / Critter; all three roles, the Critter Lab usecase (build/fullstack-v1).
    pytest.param(
        "Build the full-stack Critter Lab app",
        "build/fullstack-v1", ["claude-code", "kiro", "opencode"], "full-stack",
        id="fullstack-critter",
    ),
    pytest.param(
        "I want a frontend and backend end-to-end app",
        "build/fullstack-v1", ["claude-code", "kiro", "opencode"], "full-stack",
        id="fullstack-frontend-and-backend",
    ),
    # 5. patch; backend role only (the SIMPLE path of the complexity check).
    pytest.param(
        "fix the pricing rounding bug in the backend",
        "patch/backend-v1", ["claude-code"], "patch",
        id="patch-fix",
    ),
    pytest.param(
        "tweak the EC2 rate constant",
        "patch/backend-v1", ["claude-code"], "patch",
        id="patch-tweak",
    ),
    # 2. explicit agent intent; "use opencode" → frontend role only (patch/frontend-v1).
    pytest.param(
        "use opencode to rebuild the chatbot UI",
        "patch/frontend-v1", ["opencode"], "opencode",
        id="use-opencode",
    ),
    # 2. explicit agent intent; "use claude code" → backend role only (patch/backend-v1).
    pytest.param(
        "use claude code to add a new pricing tool",
        "patch/backend-v1", ["claude-code"], "claude",
        id="use-claude-code",
    ),
    # 2. explicit agent intent; "use kiro" → validator/review role only (review/pr-v1).
    pytest.param(
        "use kiro to validate the run",
        "review/pr-v1", ["kiro"], "kiro",
        id="use-kiro",
    ),
    # 3. review intent → review/pr-v1, validator role only.
    pytest.param(
        "review the PR on the run branch",
        "review/pr-v1", ["kiro"], "review",
        id="review-pr",
    ),
    pytest.param(
        "please review the pull request diff",
        "review/pr-v1", ["kiro"], "review",
        id="review-pull-request",
    ),
]


@pytest.mark.parametrize("task,expected_ref,expected_agents,rule_substr", PHRASING_CASES)
def test_phrasing_routes_to_expected_workflow(console, cookie, task, expected_ref,
                                              expected_agents, rule_substr):
    """Attendee types a task; admission routes it to the documented workflow_ref."""
    run = submit_run(console, cookie, task=task)
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == expected_ref, (task, route)


@pytest.mark.parametrize("task,expected_ref,expected_agents,rule_substr", PHRASING_CASES)
def test_phrasing_dispatches_only_the_routed_agents(console, cookie, task, expected_ref,
                                                    expected_agents, rule_substr):
    """The 'no race' guarantee: only the routed roles dispatch; not always all three."""
    run = submit_run(console, cookie, task=task)
    route = poll_route(console, cookie, run["run_id"])
    assert route["agents"] == expected_agents, (task, route)


@pytest.mark.parametrize("task,expected_ref,expected_agents,rule_substr", PHRASING_CASES)
def test_phrasing_explains_the_matched_rule(console, cookie, task, expected_ref,
                                            expected_agents, rule_substr):
    """The console shows WHY a task routed: the route carries the matched-rule explanation."""
    run = submit_run(console, cookie, task=task)
    route = poll_route(console, cookie, run["run_id"])
    assert rule_substr.lower() in route["rule"].lower(), (task, route["rule"])


@pytest.mark.parametrize("task,expected_ref,expected_agents,rule_substr", PHRASING_CASES)
def test_routed_usecase_matches_the_registry(console, cookie, task, expected_ref,
                                             expected_agents, rule_substr):
    """The routed run grades against the right usecase (critter-lab only for full-stack)."""
    run = submit_run(console, cookie, task=task)
    route = poll_route(console, cookie, run["run_id"])
    assert route["usecase"] == EXPECTED_WORKFLOWS[expected_ref]["usecase"], (task, route)


# ---------------------------------------------------------------------------
# Specific high-value ladder edges (beyond the parametrized sweep).
# ---------------------------------------------------------------------------
def test_agent_intent_beats_review_intent(console, cookie):
    """Ladder order: 'use opencode' wins even when 'review' is also in the text (intent > review)."""
    run = submit_run(console, cookie, task="use opencode to review and rebuild the UI")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "patch/frontend-v1", route
    assert route["agents"] == ["opencode"], route


def test_review_intent_beats_fullstack_and_convert(console, cookie):
    """Ladder order: a review of a PR routes to review/pr-v1 even with build/convert words present."""
    run = submit_run(console, cookie, task="review the pull request that converts the module")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "review/pr-v1", route


def test_fullstack_intent_beats_convert_default(console, cookie):
    """Ladder order: 'Critter' full-stack wins over the convert default → build/fullstack-v1."""
    run = submit_run(console, cookie, task="convert this into the full-stack Critter app")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "build/fullstack-v1", route


def test_review_route_is_read_only(console, cookie):
    """A review run must never produce a new artifact: its route is flagged read_only."""
    run = submit_run(console, cookie, task="review the diff on the run branch")
    route = poll_route(console, cookie, run["run_id"])
    assert route["read_only"] is True, route


def test_convert_route_is_not_read_only(console, cookie):
    """A build/convert run produces an artifact: its route is not read_only."""
    run = submit_run(console, cookie, task="convert the cost analyzer module to an MCP server")
    route = poll_route(console, cookie, run["run_id"])
    assert route["read_only"] is False, route


def test_patch_dispatches_backend_only_not_all_three(console, cookie):
    """The complexity check keeps a patch small: the frontend role is NOT dispatched."""
    run = submit_run(console, cookie, task="fix a typo in the backend docstring")
    route = poll_route(console, cookie, run["run_id"])
    assert "opencode" not in route["agents"] and "kiro" not in route["agents"], route
    assert route["agents"] == ["claude-code"], route


# ---------------------------------------------------------------------------
# Explicit workflow_ref; honored or fails loud.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ref", sorted(EXPECTED_WORKFLOWS))
def test_explicit_workflow_ref_is_honored(console, cookie, ref):
    """Attendee picks a workflow from the registry: that exact ref is routed, intent ignored."""
    run = submit_run(console, cookie, task="some unrelated text", workflow_ref=ref)
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == ref, route
    assert route["agents"] == EXPECTED_WORKFLOWS[ref]["agents"], route


def test_explicit_workflow_ref_overrides_conflicting_intent(console, cookie):
    """An explicit ref wins over phrasing: 'use opencode' text + a backend ref → the backend ref."""
    run = submit_run(console, cookie, task="use opencode please",
                     workflow_ref="patch/backend-v1")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "patch/backend-v1", route
    assert route["agents"] == ["claude-code"], route


def test_explicit_workflow_ref_rule_cites_the_registry(console, cookie):
    """The route explains it was an explicit pick validated against the registry."""
    run = submit_run(console, cookie, task="anything",
                     workflow_ref="convert/sample-to-mcp-v1")
    route = poll_route(console, cookie, run["run_id"])
    assert "explicit workflow_ref" in route["rule"].lower(), route["rule"]


def test_unknown_workflow_ref_fails_loud(console, cookie):
    """Fail-closed: an unknown workflow_ref never guesses; the run fails with a clear reason."""
    run = submit_run(console, cookie, task="convert the skill",
                     workflow_ref="bogus/not-a-real-workflow-v9")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "failed", final
    # The run is rejected at admission; no route is ever attached.
    assert final.get("route") in (None, {}), final


def test_unknown_workflow_ref_reason_names_the_bad_ref(console, cookie):
    """The fail reason is specific: it names the rejected workflow_ref so the attendee can fix it."""
    bad = "patch/does-not-exist-v1"
    run = submit_run(console, cookie, task="patch something", workflow_ref=bad)
    poll_terminal(console, cookie, run["run_id"])
    code, result = req(console, "GET", f"/api/orchestrator/runs/{run['run_id']}/result", headers=cookie)
    assert code == 200, result
    assert result["status"] == "failed", result
    reason = result.get("fail_reason") or ""
    assert "UNKNOWN_WORKFLOW" in reason and bad in reason, reason


def test_no_intent_fails_loud_not_a_hardcoded_default(console, cookie):
    """Task-agnostic: a task that matches no intent (no convert/patch/review/build
    words, no explicit agent, no workflow_ref) must NOT silently become the
    cost-analyzer conversion. It fails loud with NO_ROUTE so the orchestrator asks
    what to do, instead of fabricating the sample-to-mcp build."""
    run = submit_run(console, cookie, task="Please ship the deliverable for me")
    poll_terminal(console, cookie, run["run_id"])
    code, result = req(console, "GET", f"/api/orchestrator/runs/{run['run_id']}/result", headers=cookie)
    assert code == 200, result
    assert result["status"] == "failed", result
    assert "NO_ROUTE" in (result.get("fail_reason") or ""), result
    # No route is ever attached to an unroutable task.
    assert result.get("route") in (None, {}), result


# ---------------------------------------------------------------------------
# Submit contract + the run-list view the console polls.
# ---------------------------------------------------------------------------
def test_submit_returns_a_run_with_id_task_status_phase(console, cookie):
    """Submitting a task returns a run handle the console can poll (run_id/task/status/phase)."""
    run = submit_run(console, cookie, task="convert the skill to an MCP server")
    for key in ("run_id", "task", "status", "phase"):
        assert key in run, run
    assert run["task"] == "convert the skill to an MCP server", run


def test_submitted_run_appears_in_the_runs_list(console, cookie):
    """The submitted run shows up in GET /runs (the console's run history)."""
    run = submit_run(console, cookie, task="convert the cost analyzer skill")
    code, out = req(console, "GET", "/api/orchestrator/runs", headers=cookie)
    assert code == 200, out
    ids = {r["run_id"] for r in out["runs"]}
    assert run["run_id"] in ids, ids


def test_run_detail_carries_route_after_admission(console, cookie):
    """Polling a run surfaces the router's verdict on the run detail (route attaches on the worker)."""
    run = submit_run(console, cookie, task="convert the skill to an MCP server")
    route = poll_route(console, cookie, run["run_id"])
    code, detail = req(console, "GET", f"/api/orchestrator/runs/{run['run_id']}", headers=cookie)
    assert code == 200, detail
    assert detail["route"]["workflow_ref"] == route["workflow_ref"], detail


def test_routed_agents_match_run_detail_agents(console, cookie):
    """The dispatched-agents list on the run detail equals the route's agents (only routed roles run)."""
    run = submit_run(console, cookie, task="use kiro to review the branch")
    route = poll_route(console, cookie, run["run_id"])
    code, detail = req(console, "GET", f"/api/orchestrator/runs/{run['run_id']}", headers=cookie)
    assert code == 200, detail
    assert detail["agents"] == route["agents"] == ["kiro"], detail


def test_get_unknown_run_id_is_404(console, cookie):
    """Polling a run id that was never submitted is a clean 404, not a server error."""
    try:
        req(console, "GET", "/api/orchestrator/runs/run_nope_999", headers=cookie)
    except HTTPError as e:
        assert e.code == 404, e.code
    else:
        raise AssertionError("expected 404 for an unknown run id")


# ---------------------------------------------------------------------------
# End-to-end: a routed convert run passes the pytest gate with the LGTM token.
# ---------------------------------------------------------------------------
def test_convert_run_passes_the_pytest_gate(console, cookie):
    """The acceptance gate is pytest, not an LLM: a routed convert run reaches status 'passed'."""
    result = _settled_run(console, cookie, "convert the cost analyzer module to an MCP server")
    assert result["status"] == "passed", result


def test_passed_run_review_is_lgtm_approved(console, cookie):
    """A passing run only passes on an LGTM verdict: the review reports lgtm=True / approved.

    The exact LGTM token (``LGTM: no changes needed``) is the pass string the gate accepts;
    over the API the verdict surfaces as ``lgtm: true`` + ``state: "approved"`` (the token
    itself rides the PR body, not the run JSON)."""
    assert LGTM_TOKEN == "LGTM: no changes needed"  # the verbatim gate token
    result = _settled_run(console, cookie, "convert the cost analyzer module to an MCP server")
    assert result["status"] == "passed", result
    review = result.get("review") or {}
    assert review.get("lgtm") is True, review
    assert review.get("state") == "approved", review


def test_passed_run_composes_from_all_routed_roles_no_winner(console, cookie):
    """No race/winner: a passed convert run is composed_from all three routed roles."""
    result = _settled_run(console, cookie, "convert the cost analyzer module to an MCP server")
    assert result["status"] == "passed", result
    composed = result.get("composed_from") or []
    assert len(composed) == 3, result
    assert "pr_url" in result, result  # real PR or null, never fabricated
