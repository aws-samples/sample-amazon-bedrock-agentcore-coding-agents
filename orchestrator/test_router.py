"""Router tests: the deterministic admission router, unit-tested without a model.

This is the test file Stage 2 has attendees turn green as they build ``router.py``
in code, one rung of the ladder at a time. It is also the answer-key guard: if the
registry or the ladder in ``router.py`` ever drifts from what the content teaches,
a test here fails rather than the drift slipping into the workshop.

    python3 -m pytest orchestrator/test_router.py -v

The router is pure: same task string in, same route out, no LLM. So every case
below is a plain function call with an exact, deterministic expectation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import router  # noqa: E402
from router import Route, RouteError, route  # noqa: E402

ALL_THREE = ["claude-code", "claude-code-validator", "opencode"]


# ---------------------------------------------------------------- the registry
# The five versioned workflow descriptors are the contract the console renders
# and the engine dispatches. Pin the whole table so a registry edit is a
# deliberate, tested change, never a silent one.
EXPECTED_REGISTRY = {
    "convert/sample-to-mcp-v1": (ALL_THREE, "sample-to-mcp", False),
    "build/fullstack-v1":      (ALL_THREE, "critter-lab", False),
    "patch/backend-v1":        (["claude-code"], "sample-to-mcp", False),
    "patch/frontend-v1":       (["opencode"], "sample-to-mcp", False),
    "review/pr-v1":            (["claude-code-validator"], "sample-to-mcp", True),
}


def test_registry_has_exactly_the_five_workflows():
    assert set(router.WORKFLOWS) == set(EXPECTED_REGISTRY)


def test_each_workflow_matches_the_registry_table():
    for ref, (agents, usecase, read_only) in EXPECTED_REGISTRY.items():
        wf = router.WORKFLOWS[ref]
        assert wf["agents"] == agents, ref
        assert wf["usecase"] == usecase, ref
        assert wf["read_only"] is read_only, ref
        assert wf["version"] and wf["description"], ref


def test_only_the_review_workflow_is_read_only():
    read_only = {ref for ref, wf in router.WORKFLOWS.items() if wf["read_only"]}
    assert read_only == {"review/pr-v1"}


def test_every_dispatch_list_is_non_empty_and_known():
    for ref, wf in router.WORKFLOWS.items():
        assert wf["agents"], f"{ref} dispatches zero roles"
        unknown = [a for a in wf["agents"] if a not in ALL_THREE]
        assert not unknown, f"{ref} names unknown agents {unknown}"


def test_public_workflows_exposes_the_registry_for_the_console():
    refs = {w["workflow_ref"] for w in router.public_workflows()}
    assert refs == set(EXPECTED_REGISTRY)


# --------------------------------------------------------- rung 1: workflow_ref
def test_explicit_workflow_ref_is_honored():
    r = route("anything at all", workflow_ref="patch/frontend-v1")
    assert r.workflow_ref == "patch/frontend-v1"
    assert r.agents == ["opencode"]
    assert "explicit workflow_ref" in r.rule


def test_unknown_workflow_ref_fails_loud():
    """The fail-closed rule: an unknown ref raises, it is never guessed."""
    try:
        route("convert the skill", workflow_ref="no/such-workflow-v9")
    except RouteError as exc:
        assert "UNKNOWN_WORKFLOW:no/such-workflow-v9" in str(exc)
    else:
        raise AssertionError("expected RouteError on an unknown workflow_ref")


# -------------------------------------------------- rung 2: explicit agent intent
def test_use_codex_routes_to_frontend_only():
    r = route("use codex to restyle the chatbot header")
    assert r.workflow_ref == "patch/frontend-v1"
    assert r.agents == ["opencode"]


def test_use_claude_code_routes_to_backend_only():
    r = route("use claude code to fix the server")
    assert r.workflow_ref == "patch/backend-v1"
    assert r.agents == ["claude-code"]


def test_use_kiro_routes_to_review_only():
    r = route("use kiro to check the contract")
    assert r.workflow_ref == "review/pr-v1"
    assert r.agents == ["claude-code-validator"]


# ------------------------------------------------------------- rungs 3-6: intent
def test_review_intent_routes_to_review_workflow():
    r = route("review the PR from the last run")
    assert r.workflow_ref == "review/pr-v1"
    assert r.read_only is True
    assert r.agents == ["claude-code-validator"]


def test_fullstack_intent_routes_to_critter_lab_all_three():
    r = route("Build the full-stack Critter Lab app: backend plus a UI")
    assert r.workflow_ref == "build/fullstack-v1"
    assert r.usecase == "critter-lab"
    assert r.agents == ALL_THREE


def test_patch_intent_routes_to_backend_only():
    r = route("fix the server version string in the cost analyzer MCP server")
    assert r.workflow_ref == "patch/backend-v1"
    assert r.agents == ["claude-code"]


def test_convert_intent_routes_to_the_full_conversion():
    r = route("convert cost_analyzer.py into a deployed MCP server with a chatbot UI")
    assert r.workflow_ref == "convert/sample-to-mcp-v1"
    assert r.agents == ALL_THREE


def test_no_intent_fails_loud_not_a_hardcoded_default():
    """Task-agnostic: an unrecognized task must NOT silently become the
    cost-analyzer conversion. It fails loud (RouteError) so the orchestrator asks
    what to do, instead of fabricating the sample-to-mcp build."""
    try:
        route("do the thing we talked about")
    except RouteError as exc:
        assert "NO_ROUTE" in str(exc)
        return
    raise AssertionError("expected RouteError on a task that matches no intent")


# ------------------------------------------------------- ladder ORDER guarantees
def test_explicit_agent_intent_beats_a_patch_keyword():
    """A 'fix ...' task that also says 'use codex' goes to the frontend, not the
    backend patch: explicit agent intent sits ABOVE patch intent on the ladder."""
    r = route("fix the header, use codex")
    assert r.workflow_ref == "patch/frontend-v1"


def test_explicit_workflow_ref_beats_text_intent():
    """Rung 1 wins outright: an explicit ref overrides what the text would match."""
    r = route("use codex to do everything", workflow_ref="convert/sample-to-mcp-v1")
    assert r.workflow_ref == "convert/sample-to-mcp-v1"
    assert r.agents == ALL_THREE


# ---------------------------------------------------------------- purity / shape
def test_router_is_deterministic():
    """Same task in, same route out: the property that lets the gate trust it."""
    task = "convert cost_analyzer.py into a deployed MCP server with a chatbot UI"
    first, second = route(task).public(), route(task).public()
    assert first == second


def test_route_public_shape_is_the_frozen_contract():
    r = route("convert the skill")
    pub = r.public()
    assert set(pub) == {"workflow_ref", "version", "rule", "agents", "usecase", "read_only"}
    assert isinstance(pub["agents"], list)
    assert isinstance(r, Route)


# ------------------------------------------------------------- usecase resolution
def test_usecase_paths_resolves_the_grading_dir():
    paths = router.usecase_paths("sample-to-mcp")
    assert paths["module"] == "cost_analyzer"
    assert paths["grading"].endswith(os.path.join("grading"))
    assert os.path.isdir(paths["dir"])


def test_usecase_paths_fails_loud_on_unknown_usecase():
    try:
        router.usecase_paths("no-such-usecase")
    except RouteError as exc:
        assert "UNKNOWN_USECASE:no-such-usecase" in str(exc)
    else:
        raise AssertionError("expected RouteError on an unknown usecase")
