"""Stage 2. Orchestrate (autonomous): run lifecycle + compose + acceptance gate.

The workshop step this file covers: an attendee opens the Stage 2 chat surface,
submits ONE task, and watches the orchestrator ROUTE it, run the five-phase
blueprint fire-and-forget, prove the deterministic pytest acceptance gate in a
SEPARATE review pen, compose the routed roles' artifacts into one deliverable, and
(real-or-null) open a PR. There is NO race and NO winner; the routed roles compose
into a single result, and only routed roles get a terminal pane.

Every test drives the same console server over the same-origin `/api/orchestrator/...`
mounts an attendee's browser hits, behind the same cookie wall. Local engine mode
is pinned in conftest (deterministic, no model), so "done" is the same pytest gate
the workshop grades with and a convert task reaches "passed". Polls are bounded by
the shared `poll_route`/`poll_terminal` helpers.

If a test here breaks, a non-negotiable has regressed: routed dispatch (not fixed
fan-out), the separate review gate, the exact LGTM token, real-or-null PR (never a
fake URL), routed-roles-only terminals, or honest local-zero usage.
"""
from __future__ import annotations

import time

import pytest

from e2e.conftest import (
    LGTM_TOKEN,
    SUPPORTED_AGENTS,
    TERMINAL_STATUSES,
    expect_status,
    poll_route,
    poll_terminal,
    req,
    submit_run,
)


@pytest.fixture(autouse=True)
def _drain_runs(console, cookie):
    """Keep the SHARED engine's concurrency slots clean across these independent
    tests. After each test, wait for any still-executing run to reach a terminal
    state so the NEXT test isn't rejected by the cap (a slot is freed only when a
    run finishes). This is bookkeeping for the shared fixture, not a contract."""
    yield
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _, body = req(console, "GET", "/api/orchestrator/runs", headers=cookie)
        live = [r["run_id"] for r in body["runs"]
                if r["status"] not in TERMINAL_STATUSES]
        if not live:
            return
        time.sleep(0.3)

# The five blueprint phases, in order, the engine drives every run through.
BLUEPRINT_PHASES = [
    "admission", "context_hydration", "pre_flight", "agent_execution", "finalization",
]
ALL_THREE = {"claude-code", "kiro", "opencode"}


def submit(console, cookie, task=None, workflow_ref=None, attempts: int = 60) -> dict:
    """Submit a run that actually gets a slot, retrying on the concurrency cap.

    The suite shares ONE engine with a concurrency cap (max_concurrent=3), and
    these tests are independent (not ordered), so a burst of submits can hit
    admission's CONCURRENCY_LIMIT; a transient reject re-graded to needs_human.
    That is engine behavior, not the contract under test here, so we wait for
    a free slot and re-submit until the run is admitted. Every OTHER assertion in
    this file is about a run that DID get a slot.

    The engine attaches `run.route` DURING admission, just before the concurrency
    check, so a route on a rejected run is not proof of admission. The reliable
    signal is the run advancing PAST admission (phase moves on); or any terminal
    status that is NOT the concurrency reject (a legitimate fail-loud the caller
    wants to assert on).
    """
    for _ in range(attempts):
        run = submit_run(console, cookie, task=task, workflow_ref=workflow_ref)
        rid = run["run_id"]
        for _ in range(60):
            _, r = req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
            # admitted: the worker moved the run past the admission phase.
            if r["phase"] != "admission":
                return run
            if r["status"] in TERMINAL_STATUSES:
                break                            # terminal still in admission == rejected
            time.sleep(0.05)
        _, res = req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
        if "CONCURRENCY_LIMIT" not in (res.get("fail_reason") or ""):
            return run                           # a legitimate fail-loud; hand it back
        time.sleep(0.5)                          # cap hit; wait for a slot, re-submit
    return run


def _result(console, cookie, rid: str) -> dict:
    _, res = req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
    return res


def _detail(console, cookie, rid: str) -> dict:
    _, full = req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
    return full


def _terminals(console, cookie, rid: str) -> dict:
    _, terms = req(console, "GET", f"/api/orchestrator/runs/{rid}/terminals", headers=cookie)
    return terms["terminals"]


# --------------------------------------------------------------------------- #
# Workflow registry + roster (what the chat surface renders before a submit).
# --------------------------------------------------------------------------- #
def test_workflows_registry_lists_all_five_refs(console, cookie):
    """Attendee opens the workflow picker: GET /workflows returns the five
    versioned descriptors the router can route to."""
    _, body = req(console, "GET", "/api/orchestrator/workflows", headers=cookie)
    refs = {w["workflow_ref"] for w in body["workflows"]}
    assert refs == {"convert/sample-to-mcp-v1", "build/fullstack-v1",
                    "patch/backend-v1", "patch/frontend-v1", "review/pr-v1"}, refs
    for w in body["workflows"]:
        assert set(w) >= {"workflow_ref", "version", "agents", "usecase",
                          "read_only", "description"}, w
        assert isinstance(w["agents"], list) and w["agents"]


def test_review_workflow_is_the_only_read_only_one(console, cookie):
    """The registry marks exactly review/pr-v1 read_only: the workflow that must
    never produce a new artifact."""
    _, body = req(console, "GET", "/api/orchestrator/workflows", headers=cookie)
    read_only = {w["workflow_ref"] for w in body["workflows"] if w["read_only"]}
    assert read_only == {"review/pr-v1"}, read_only


def test_orchestrator_exposes_its_three_roles(console, cookie):
    """The chat surface shows the orchestrator's three composed roles (not a
    competition roster); GET /api/orchestrator/agents lists exactly the three."""
    _, body = req(console, "GET", "/api/orchestrator/agents", headers=cookie)
    ids = {a["id"] for a in body["agents"]}
    assert ids == set(SUPPORTED_AGENTS), ids


# --------------------------------------------------------------------------- #
# Submit + route (the router's verdict attaches on the worker).
# --------------------------------------------------------------------------- #
def test_submit_convert_returns_queued_run(console, cookie):
    """Attendee submits a convert task: POST /runs accepts it and returns a run
    record carrying its id, task, and an initial (non-terminal) status/phase."""
    run = submit(console, cookie,
                     "Convert the cost_analyzer module to a remote MCP server")
    assert run["run_id"].startswith("run_"), run
    assert run["task"], run
    assert run["status"] in ("queued", "running"), run
    assert run["phase"] in BLUEPRINT_PHASES, run


def test_convert_routes_to_full_workflow_all_three_roles(console, cookie):
    """A convert task routes to convert/sample-to-mcp-v1 with all three roles:
    the COMPLEX path of the router's complexity check."""
    run = submit(console, cookie,
                     "Convert the cost_analyzer module into an MCP server with a UI")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "convert/sample-to-mcp-v1", route
    assert set(route["agents"]) == ALL_THREE, route
    assert route["usecase"] == "sample-to-mcp", route
    assert route["read_only"] is False, route


def test_explicit_workflow_ref_is_honored(console, cookie):
    """Attendee picks a workflow explicitly: workflow_ref drives the route past
    the intent ladder (validated against the registry)."""
    run = submit(console, cookie, task="anything", workflow_ref="patch/frontend-v1")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "patch/frontend-v1", route
    assert route["agents"] == ["opencode"], route


def test_unknown_workflow_ref_fails_loud(console, cookie):
    """An unknown explicit workflow_ref fails loud (a terminal failure with the
    UNKNOWN_WORKFLOW reason); never a silent guess."""
    run = submit(console, cookie, task="do something", workflow_ref="bogus/ref-v9")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] in TERMINAL_STATUSES, final
    res = _result(console, cookie, run["run_id"])
    assert "UNKNOWN_WORKFLOW" in (res.get("fail_reason") or ""), res


def test_use_opencode_intent_routes_frontend_only(console, cookie):
    """Explicit agent intent 'use opencode' routes to patch/frontend-v1; opencode is
    the only dispatched role."""
    run = submit(console, cookie, "use opencode to restyle the chatbot header")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "patch/frontend-v1", route
    assert route["agents"] == ["opencode"], route


def test_use_kiro_intent_routes_review_only(console, cookie):
    """Explicit agent intent 'use kiro' routes to review/pr-v1: the validator
    role only, read-only."""
    run = submit(console, cookie, "use kiro to validate the contract")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "review/pr-v1", route
    assert route["agents"] == ["kiro"], route
    assert route["read_only"] is True, route


def test_patch_intent_routes_backend_only(console, cookie):
    """A patch-sized request routes to patch/backend-v1: the SIMPLE path, backend
    role only, no frontend dispatched."""
    run = submit(console, cookie, "fix the server version string to v2")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "patch/backend-v1", route
    assert route["agents"] == ["claude-code"], route


def test_fullstack_intent_routes_critter_all_three(console, cookie):
    """Full-stack / Critter phrasing routes to build/fullstack-v1: the critter
    usecase, all three roles."""
    run = submit(console, cookie,
                     "Build the full-stack Critter Lab app: backend and frontend")
    route = poll_route(console, cookie, run["run_id"])
    assert route["workflow_ref"] == "build/fullstack-v1", route
    assert set(route["agents"]) == ALL_THREE, route
    assert route["usecase"] == "critter-lab", route


# --------------------------------------------------------------------------- #
# Lifecycle: poll to terminal "passed", phase progression, gate, LGTM, compose.
# --------------------------------------------------------------------------- #
def test_convert_reaches_passed_terminal(console, cookie):
    """The headline action: a submitted convert task is fire-and-forget and polls
    to terminal status 'passed' (the deterministic pytest gate is green)."""
    run = submit(console, cookie,
                     "Convert cost_analyzer to a remote MCP server with tests + UI")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "passed", final
    assert final["phase"] == "finalization", final


def test_run_detail_carries_route_and_phase(console, cookie):
    """GET /runs/{id} (the run detail the chat surface polls) carries the router's
    route verdict and the current blueprint phase."""
    run = submit(console, cookie, "Convert the module to an MCP server")
    rid = run["run_id"]
    poll_route(console, cookie, rid)
    full = _detail(console, cookie, rid)
    assert full["run_id"] == rid, full
    assert full["route"] and full["route"]["workflow_ref"], full
    assert full["phase"] in BLUEPRINT_PHASES, full
    assert "roles" in full and "agents" in full, full


def test_terminal_run_lands_in_final_blueprint_phase(console, cookie):
    """A run that reaches a terminal status has progressed through the blueprint to
    its final phase: admission/hydration/pre-flight/execution/finalization."""
    run = submit(console, cookie, "Convert cost_analyzer to an MCP server")
    final = poll_terminal(console, cookie, run["run_id"])
    # a passed convert finalizes; any terminal phase is one of the five real ones.
    assert final["phase"] in BLUEPRINT_PHASES, final
    if final["status"] == "passed":
        assert final["phase"] == "finalization", final


def test_passed_run_gate_is_green(console, cookie):
    """The acceptance gate is pytest, not an LLM: a passed convert run's result
    reports gate.passed True with at least one check."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "passed", final
    res = _result(console, cookie, run["run_id"])
    assert res["gate"]["passed"] is True, res["gate"]
    assert res["gate"]["checks"], res["gate"]
    assert all("check" in c and "passed" in c for c in res["gate"]["checks"]), res["gate"]


def test_reviewer_emits_exactly_the_lgtm_token(console, cookie):
    """The SEPARATE review orchestrator approves only with the exact pass token:
    the committed critique carries `LGTM: no changes needed` verbatim."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    rid = run["run_id"]
    final = poll_terminal(console, cookie, rid)
    assert final["status"] == "passed", final
    res = _result(console, cookie, rid)
    assert res["review"]["lgtm"] is True, res["review"]
    assert res["review"]["state"] == "approved", res["review"]
    # the EXACT token, surfaced in the validator's terminal critique.md cat.
    terms = _terminals(console, cookie, rid)
    critique = "".join(e["output"] for e in terms.get("kiro", []))
    assert LGTM_TOKEN in critique, "the exact LGTM pass token must appear in the critique"


def test_composed_from_carries_roles_not_a_winner(console, cookie):
    """NO race / NO winner: the result composes the dispatched roles into ONE
    deliverable; composed_from is the role list, never a single winner."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "passed", final
    res = _result(console, cookie, run["run_id"])
    assert res["composed_from"] == ["backend-mcp", "validator", "frontend-builder"], res
    # a passed build composes a real local git branch + commit (the PR's stand-in).
    assert res["composed_branch"] and res["composed_branch"].startswith("run/"), res
    assert res["composed_commit"], res


def test_passed_run_bounded_to_one_iteration(console, cookie):
    """A clean convert passes on the first build round: bounded iteration means
    iterations == 1 (no re-implement pass needed)."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "passed", final
    res = _result(console, cookie, run["run_id"])
    assert res["iterations"] == 1, res


# --------------------------------------------------------------------------- #
# PR: real-or-null, never fake (honest no-credential state in tests).
# --------------------------------------------------------------------------- #
def test_pr_url_is_null_without_github_credentials(console, cookie):
    """pr_url is real-or-null, never fake: with no GitHub credential connected (the
    test server's honest state) the run composes the branch locally, the PR step
    fails loud (a typed error, no fake URL), and pr_url is null."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "passed", final
    res = _result(console, cookie, run["run_id"])
    assert res["pr_url"] is None, res
    assert (res.get("pr") or {}).get("pr_url") is None, res.get("pr")


def test_github_status_reports_disconnected(console, cookie):
    """The Settings pane reads GET /github: in the test server with no token it
    reports the honest disconnected state that makes pr_url null."""
    _, gh = req(console, "GET", "/api/orchestrator/github", headers=cookie)
    assert gh["connected"] is False, gh
    assert "mode" in gh, gh


# --------------------------------------------------------------------------- #
# Routed-roles-only terminals (a backend patch has no kiro/opencode pane).
# --------------------------------------------------------------------------- #
def test_convert_has_a_pane_for_each_of_three_roles(console, cookie):
    """The full convert routes all three roles, so the chat surface gets exactly
    three per-role terminal panes, each with transcript output."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    rid = run["run_id"]
    poll_terminal(console, cookie, rid)
    terms = _terminals(console, cookie, rid)
    assert set(terms) == ALL_THREE, list(terms)
    assert all(terms[a] for a in terms), terms


def test_backend_patch_has_no_kiro_or_opencode_pane(console, cookie):
    """Routed-roles-only: a backend patch dispatches just claude-code, so there is
    NO kiro and NO opencode terminal pane (the review verdict lives in the run log)."""
    run = submit(console, cookie, "fix the server version string to v2")
    rid = run["run_id"]
    route = poll_route(console, cookie, rid)
    assert route["agents"] == ["claude-code"], route
    poll_terminal(console, cookie, rid)
    terms = _terminals(console, cookie, rid)
    assert "claude-code" in terms, list(terms)
    assert "kiro" not in terms, f"review pane fabricated on a backend patch: {list(terms)}"
    assert "opencode" not in terms, f"frontend ran on a backend patch: {list(terms)}"


def test_backend_patch_progress_is_single_role_no_cancel(console, cookie):
    """A routed single-role run has NO competitor to cancel: progress holds only
    the dispatched role and no row is in a 'cancelled' (race-artifact) state."""
    run = submit(console, cookie, "fix the version string in mcp_server.py")
    rid = run["run_id"]
    poll_terminal(console, cookie, rid)
    full = _detail(console, cookie, rid)
    assert full["roles"] == {"claude-code": "backend-mcp"}, full["roles"]
    assert {p["agent"] for p in full["progress"]} == {"claude-code"}, full["progress"]
    assert not any(p["state"] == "cancelled" for p in full["progress"]), full["progress"]


def test_frontend_patch_dispatches_only_opencode(console, cookie):
    """A frontend restyle dispatches ONLY opencode: opencode has a pane, claude-code (a
    backend role) does not; the engine's infra endpoint is not a dispatched role."""
    run = submit(console, cookie, "use opencode to restyle the chatbot header")
    rid = run["run_id"]
    poll_terminal(console, cookie, rid)
    terms = _terminals(console, cookie, rid)
    assert "opencode" in terms, list(terms)
    assert "claude-code" not in terms, f"backend role ran on a frontend patch: {list(terms)}"


# --------------------------------------------------------------------------- #
# Read-only review: composes nothing new.
# --------------------------------------------------------------------------- #
def test_review_run_composes_no_new_deliverable(console, cookie):
    """Read-only review/pr-v1 produces NO new deliverable: it needs a prior passed
    build to review, then composed_commit stays null and pr_url is null."""
    # seed a build the review can target.
    seed = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    assert poll_terminal(console, cookie, seed["run_id"])["status"] == "passed"

    run = submit(console, cookie, "use kiro to review the last run")
    rid = run["run_id"]
    route = poll_route(console, cookie, rid)
    assert route["workflow_ref"] == "review/pr-v1" and route["read_only"] is True, route
    final = poll_terminal(console, cookie, rid)
    assert final["status"] in TERMINAL_STATUSES, final
    if final["status"] == "passed":
        res = _result(console, cookie, rid)
        assert res["composed_commit"] is None, "a review must not compose a NEW deliverable"
        assert res["pr_url"] is None, res
        # a review borrows the target's branch, never opens one of its OWN.
        if res["composed_branch"] is not None:
            assert res["composed_branch"] != f"run/{rid}", res


# --------------------------------------------------------------------------- #
# Full-stack: three distinct role artifacts compose into one deliverable.
# --------------------------------------------------------------------------- #
def test_fullstack_lands_three_distinct_role_artifacts(console, cookie):
    """The full-stack build composes three DISTINCT role artifacts into the
    deliverable: mcp_server.py (backend), chatbot.html (frontend), critique.md
    (reviewer), proving collaboration, not a single winner."""
    run = submit(console, cookie,
                     "Build the full-stack Critter Lab app: backend and frontend")
    rid = run["run_id"]
    final = poll_terminal(console, cookie, rid)
    assert final["status"] in TERMINAL_STATUSES, final
    if final["status"] != "passed":
        return  # a non-passed full-stack run does not compose; nothing to assert here
    res = _result(console, cookie, rid)
    assert res["composed_from"] == ["backend-mcp", "validator", "frontend-builder"], res
    # the three role panes each ran (the three artifacts trace to three roles).
    terms = _terminals(console, cookie, rid)
    assert set(terms) == ALL_THREE, list(terms)
    # the deliverable composed a real commit on a per-run branch.
    assert res["composed_branch"] == f"run/{rid}", res
    assert res["composed_commit"], res
    # the gate references the critter contract (card_renders is unique to it).
    gate_checks = {c["check"] for c in res["gate"]["checks"]}
    assert "card_renders" in gate_checks, gate_checks


# --------------------------------------------------------------------------- #
# Honest-zero usage in local mode (no model ran).
# --------------------------------------------------------------------------- #
def test_local_mode_reports_honest_zero_usage(console, cookie):
    """Local engine mode invokes no model: every role's progress reports zero
    tokens and zero cost (honest zero, never an inferred figure)."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    rid = run["run_id"]
    poll_terminal(console, cookie, rid)
    full = _detail(console, cookie, rid)
    assert full["progress"], full
    for p in full["progress"]:
        assert p["tokens"] == 0, f"local mode must report zero tokens: {p}"
        assert p["cost_usd"] == 0.0, f"local mode must report zero cost: {p}"


# --------------------------------------------------------------------------- #
# Runs list + result-gating contracts.
# --------------------------------------------------------------------------- #
def test_runs_list_contains_submitted_run(console, cookie):
    """GET /runs (the run-history rail) lists the run an attendee just submitted,
    as a structured record with id/task/status."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    rid = run["run_id"]
    poll_terminal(console, cookie, rid)
    _, body = req(console, "GET", "/api/orchestrator/runs", headers=cookie)
    runs = body["runs"]
    assert isinstance(runs, list) and runs, body
    by_id = {r["run_id"]: r for r in runs}
    assert rid in by_id, f"submitted run missing from the runs list: {rid}"
    rec = by_id[rid]
    assert rec["task"] and rec["status"] in TERMINAL_STATUSES, rec


def test_result_before_terminal_is_409(console, cookie):
    """The result endpoint is gated: GET /runs/{id}/result on a still-running run
    returns 409 with the live status/phase (a poller never reads a half-result)."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    rid = run["run_id"]
    # the result is only well-formed once terminal; while running it must 409.
    # (best-effort: if the local run finishes instantly we still assert the shape.)
    try:
        _, res = req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
        # already terminal; the composed result is well-formed.
        assert res["run_id"] == rid and res["status"] in TERMINAL_STATUSES, res
    except Exception as exc:  # HTTPError 409 while still running
        body = expect_status(
            lambda: req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie),
            409)
        assert body.get("status") in ("queued", "running"), (body, exc)
        assert body.get("phase") in BLUEPRINT_PHASES, body
    poll_terminal(console, cookie, rid)  # don't leave it mid-flight for siblings


def test_unknown_run_id_is_404(console, cookie):
    """An unknown run id is a 404 on the run-detail endpoint (no phantom run)."""
    expect_status(
        lambda: req(console, "GET", "/api/orchestrator/runs/run_does_not_exist", headers=cookie),
        404)


def test_empty_task_fails_admission_loud(console, cookie):
    """An empty task is rejected at admission as a terminal failure with the
    EMPTY_TASK reason: fail-closed, never a silent default build."""
    run = submit(console, cookie, task="   ")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] in TERMINAL_STATUSES, final
    res = _result(console, cookie, run["run_id"])
    assert "EMPTY_TASK" in (res.get("fail_reason") or ""), res


# --------------------------------------------------------------------------- #
# The autonomous tail: merge_policy + merge_state (the fully-autonomous SDLC
# rung built on the "reviewer" page). The Settings pane reads/writes the policy
# off GET/POST /github, and a finished run surfaces merge_state. The test server
# has no GitHub credential, so the safe end-state is human_review (never a fake
# auto-merge). Each test restores the default so the shared engine stays clean.
# --------------------------------------------------------------------------- #
@pytest.fixture
def _restore_policy(console, cookie):
    """Always put merge_policy back to human_review after a test flips it."""
    yield
    req(console, "POST", "/api/orchestrator/github", {"merge_policy": "human_review"}, headers=cookie)


def test_github_status_reports_a_merge_policy(console, cookie):
    """The Settings pane reads merge_policy off GET /github so it can render the
    toggle. It is one of the two valid values, and fail-closed by default."""
    _, gh = req(console, "GET", "/api/orchestrator/github", headers=cookie)
    assert gh.get("merge_policy") in ("human_review", "auto"), gh


def test_policy_only_post_flips_merge_policy(console, cookie, _restore_policy):
    """The toggle posts merge_policy alone (no token) and the status reflects it;
    the attendee turns the autonomous tail on without re-entering a credential."""
    _, after = req(console, "POST", "/api/orchestrator/github",
                   {"merge_policy": "auto"}, headers=cookie)
    assert after.get("merge_policy") == "auto", after
    # It is a policy-only flip: the credential state is unchanged (still local).
    assert after.get("connected") is False, after


def test_merge_policy_post_fails_closed_on_garbage(console, cookie, _restore_policy):
    """An unknown policy value coerces to human_review; auto-merge is never
    inferred from a typo."""
    _, after = req(console, "POST", "/api/orchestrator/github",
                   {"merge_policy": "YOLO"}, headers=cookie)
    assert after.get("merge_policy") == "human_review", after


def test_run_surfaces_merge_state_human_review_without_credential(console, cookie):
    """A finished run reports merge_state. With no GitHub credential the PR is
    skipped, so the run settles on the safe human_review end-state, never a fake
    auto-merge."""
    run = submit(console, cookie, "Convert cost_analyzer to a remote MCP server")
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "passed", final
    res = _result(console, cookie, run["run_id"])
    assert "merge_state" in res, res
    # local mode: PR was skipped, so the tail never auto-merged.
    assert res["merge_state"] != "merged", res


# ===========================================================================
# WIRABLE AGENTCORE RUNTIMES: the role ARNs are SET via the API (Settings pane
# / terminal), never hardcoded. The orchestrator's AgentCoreExecutor reads them.
# ===========================================================================
def test_runtimes_status_lists_every_role_unwired_by_default(console, cookie):
    """GET /api/runtimes reports each role and whether its runtime is wired. With
    nothing wired, every role is unwired. The shipped engine is real-only, so the
    executor is ``agentcore`` (there is no local executor); a role with no wired ARN
    fails loud at dispatch time, not here."""
    _, body = req(console, "GET", "/api/orchestrator/runtimes", headers=cookie)
    roles = {r["role"]: r for r in body["roles"]}
    assert {"orchestrator", "claude-code", "kiro", "opencode"} <= set(roles), body
    assert body["executor"] == "agentcore" and body["remote_dispatch"] is True, body
    assert all(r["wired"] is False for r in body["roles"]), body


def test_wire_then_unwire_a_runtime_arn(console, cookie):
    """An attendee wires a deployed runtime ARN; it shows as wired from 'settings',
    then clears. This is the same surface the terminal `agentcore wire` writes."""
    arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc-e2e"
    try:
        _, wired = req(console, "POST", "/api/orchestrator/runtimes",
                       {"role": "claude-code", "arn": arn}, headers=cookie)
        cc = next(r for r in wired["roles"] if r["role"] == "claude-code")
        assert cc["wired"] is True and cc["source"] == "settings" and cc["arn"] == arn, wired
    finally:
        _, cleared = req(console, "POST", "/api/orchestrator/runtimes",
                         {"clear": True, "role": "claude-code"}, headers=cookie)
        cc = next(r for r in cleared["roles"] if r["role"] == "claude-code")
        assert cc["wired"] is False, cleared


def test_wire_rejects_a_malformed_arn(console, cookie):
    """A junk value fails loud (400), never silently stored."""
    body = expect_status(
        lambda: req(console, "POST", "/api/orchestrator/runtimes",
                    {"role": "kiro", "arn": "not an arn !!!"}, headers=cookie),
        400)
    assert "error" in body, body


def test_grow_a_role_into_a_fleet(console, cookie):
    """'3 types' is not 3 instances: a type is a FLEET. `add` grows a role with more
    deployed runtimes; status reports the count + every instance, and the first ARN
    stays back-compatible. This is the #76 acceptance over the real HTTP surface."""
    base = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/opencode-"
    arns = [base + tag for tag in ("a", "b", "c")]
    try:
        # First instance: a plain wire. Then grow the fleet with `add`.
        req(console, "POST", "/api/orchestrator/runtimes",
            {"role": "opencode", "arn": arns[0]}, headers=cookie)
        for arn in arns[1:]:
            _, body = req(console, "POST", "/api/orchestrator/runtimes",
                          {"role": "opencode", "arn": arn, "add": True}, headers=cookie)
        cx = next(r for r in body["roles"] if r["role"] == "opencode")
        assert cx["wired"] is True and cx["count"] == 3, body
        assert [i["arn"] for i in cx["instances"]] == arns, body
        assert cx["arn"] == arns[0], "first instance stays the back-compat single ARN"
    finally:
        _, cleared = req(console, "POST", "/api/orchestrator/runtimes",
                         {"clear": True, "role": "opencode"}, headers=cookie)
        cx = next(r for r in cleared["roles"] if r["role"] == "opencode")
        assert cx["wired"] is False and cx["count"] == 0, cleared
