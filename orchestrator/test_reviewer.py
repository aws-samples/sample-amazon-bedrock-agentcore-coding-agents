"""Reviewer tests: the SEPARATE review pen, unit-tested without a model.

Stage 2 has attendees build ``reviewer.py`` after the router: the pass token, the
one-bounded-pass rule, the strict branch-suffix guard, and the role-aware critique.
These tests are the red→green checkpoints for that build, and the answer-key guard
that the content's code blocks match the real module.

    python3 -m pytest orchestrator/test_reviewer.py -v

The full over-the-wire ``review()`` (pytest gate + RemoteMCPClient against a booted
endpoint) is exercised end-to-end in ``test_engine.py``. Here we pin the deterministic
units that need no server, so the loop is fast.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reviewer  # noqa: E402
from reviewer import (  # noqa: E402
    LGTM_TOKEN,
    ReviewVerdict,
    branch_run_id,
)


class _FakeRun:
    """The minimal slice of a Run that the critique reads, so the critique can
    be tested as the pure function of artifacts it is, with no engine."""

    def __init__(self, *, run_id="run_103512_004", agents=None, route=None,
                 server_file=None, chatbot_file=None, composed_branch=None,
                 review_target=None, iterations=1, workdir=None,
                 artifact_endpoint=None, task=""):
        self.run_id = run_id
        self.agents = agents or []
        self.route = route or {}
        self._server_file = server_file
        self._chatbot_file = chatbot_file
        self.composed_branch = composed_branch
        self._review_target = review_target
        self.iterations = iterations
        self.workdir = workdir or ""
        self.artifact_endpoint = artifact_endpoint
        self.task = task



# ----------------------------------------------------- the strict branch-suffix guard
def test_branch_run_id_maps_a_run_branch_back_to_its_run():
    assert branch_run_id("run/run_103512_004") == "run_103512_004"


def test_branch_run_id_refuses_lookalikes():
    """A strict pattern, never a most-recent heuristic: anything that is not a
    clean ``run/<run_id>`` returns None instead of guessing."""
    assert branch_run_id("run/run_103512_004-extra") is None
    assert branch_run_id("feature/run_103512_004") is None
    assert branch_run_id("run/%") is None
    assert branch_run_id("main") is None
    assert branch_run_id(None) is None


# ----------------------------------------------------- critique check 1: imports skill
def test_critique_passes_when_the_server_imports_the_skill(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n# wraps the registry over MCP\n")
    run = _FakeRun(agents=["claude-code"], server_file=str(server))
    checks = reviewer._critique_checks(run, "cost_analyzer")
    imports = next(c for c in checks if c["check"] == "server_imports_module")
    assert imports["passed"] is True


def test_critique_fails_when_the_server_copies_logic_instead_of_importing(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text("HOURS = 730\ndef price(): return 140.16  # copied, no import\n")
    run = _FakeRun(agents=["claude-code"], server_file=str(server))
    checks = reviewer._critique_checks(run, "cost_analyzer")
    imports = next(c for c in checks if c["check"] == "server_imports_module")
    assert imports["passed"] is False


# ------------------------------------------------- critique check 2: thin frontend
def test_critique_judges_the_frontend_only_when_frontend_was_dispatched(tmp_path):
    """Role-aware: a backend patch is never failed for a chatbot it was not asked
    to build. The frontend check only appears when the route dispatched the frontend role."""
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n")
    backend_only = _FakeRun(agents=["claude-code"], server_file=str(server))
    names = {c["check"] for c in reviewer._critique_checks(backend_only, "cost_analyzer")}
    assert "frontend_is_thin" not in names


def test_critique_passes_for_a_thin_frontend(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n")
    chatbot = tmp_path / "chatbot.html"
    chatbot.write_text("<script>fetch('/mcp', {body: JSON.stringify({method:'tools/call'})})</script>")
    run = _FakeRun(agents=["claude-code", "opencode"],
                   server_file=str(server), chatbot_file=str(chatbot))
    thin = next(c for c in reviewer._critique_checks(run, "cost_analyzer")
                if c["check"] == "frontend_is_thin")
    assert thin["passed"] is True


def test_critique_fails_a_frontend_that_computes_locally(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n")
    chatbot = tmp_path / "chatbot.html"
    chatbot.write_text("<script>const cost = 0.096 * 730 * 2; // local math, no fetch</script>")
    run = _FakeRun(agents=["claude-code", "opencode"],
                   server_file=str(server), chatbot_file=str(chatbot))
    thin = next(c for c in reviewer._critique_checks(run, "cost_analyzer")
                if c["check"] == "frontend_is_thin")
    assert thin["passed"] is False


# --------------------------------------------- critique check 3: branch discipline
def test_critique_branch_maps_to_the_run_that_produced_it(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n")
    run = _FakeRun(run_id="run_103512_004", agents=["claude-code"],
                   server_file=str(server), composed_branch="run/run_103512_004")
    branch = next(c for c in reviewer._critique_checks(run, "cost_analyzer")
                  if c["check"] == "branch_maps_to_run")
    assert branch["passed"] is True


def test_critique_branch_check_fails_on_a_mismatched_branch(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n")
    run = _FakeRun(run_id="run_103512_004", agents=["claude-code"],
                   server_file=str(server), composed_branch="run/run_999999_999")
    branch = next(c for c in reviewer._critique_checks(run, "cost_analyzer")
                  if c["check"] == "branch_maps_to_run")
    assert branch["passed"] is False


def test_review_run_branch_maps_to_the_target_not_the_review(tmp_path):
    """On a read-only review, the composed branch belongs to the run UNDER review,
    so the guard checks the target's id, not the review run's own id."""
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n")
    run = _FakeRun(run_id="run_222222_002", agents=["kiro"],
                   route={"read_only": True}, review_target="run_103512_004",
                   server_file=str(server), composed_branch="run/run_103512_004")
    branch = next(c for c in reviewer._critique_checks(run, "cost_analyzer")
                  if c["check"] == "branch_maps_to_run")
    assert branch["passed"] is True


# ------------------------------------------------------------- the rendered report
def test_report_carries_the_pass_token_when_lgtm(tmp_path):
    run = _FakeRun(iterations=1, workdir=str(tmp_path))
    gate = {"passed": True, "checks": [{"check": "tool_discovery", "passed": True,
                                        "detail": "all five present"}]}
    critique = [{"check": "server_imports_module", "passed": True, "detail": "imports live"}]
    report = reviewer._render_report(run, gate, critique, lgtm=True)
    assert LGTM_TOKEN in report
    assert "gate GREEN" in report


def test_report_requests_changes_when_not_lgtm(tmp_path):
    run = _FakeRun(iterations=1, workdir=str(tmp_path))
    gate = {"passed": False, "checks": [{"check": "tool_correctness", "passed": False,
                                         "detail": "m5.large x2 returned 0.0, expected 140.16"}]}
    critique = [{"check": "server_imports_module", "passed": True, "detail": "imports live"}]
    report = reviewer._render_report(run, gate, critique, lgtm=False)
    assert LGTM_TOKEN not in report
    assert "Changes requested" in report
    assert "gate RED" in report


# --------------------------------------------------------------- grading loader
def test_load_grading_imports_the_contract_and_adapters():
    """The reviewer grades against the usecase's own contract; loading it returns
    the grade function plus both adapters (in-process and over-the-wire)."""
    # The grading dir sits next to the orchestrator package in every tree
    # (../usecase-sample-to-mcp/grading), so resolve it relative to this test file.
    # That runs unchanged from the repo answer-key AND from the attendee's own
    # tree at ~/src, never a hardcoded solution/ segment.
    here = os.path.dirname(os.path.abspath(__file__))
    grading = os.path.join(os.path.dirname(here), "usecase-sample-to-mcp", "grading")
    grade, InProcessClient, RemoteMCPClient = reviewer.load_grading(grading)
    # the in-process adapter grades the reference module directly: green before
    # any agent or deployment exists, which is what lets us trust the grader.
    result = grade(InProcessClient())
    assert result["passed"] is True
    assert {c["check"] for c in result["checks"]} == {
        "tool_discovery", "tool_correctness", "input_validation"}


def test_review_verdict_public_shape():
    v = ReviewVerdict(state="approved", lgtm=True, round=1,
                      gate={"passed": True, "checks": []})
    pub = v.public()
    assert set(pub) == {"state", "lgtm", "round", "gate", "critique"}
    assert pub["lgtm"] is True


# ----------------------------------------------------- the LLM judge (injectable)
def _green_run(tmp_path):
    """A run whose deterministic layer is clean: server imports the module, branch
    maps, backend-only so no frontend check. Used to isolate the judge's effect."""
    server = tmp_path / "mcp_server.py"
    server.write_text("import cost_analyzer\n")
    return _FakeRun(run_id="run_103512_004", agents=["claude-code"],
                    server_file=str(server), composed_branch="run/run_103512_004",
                    workdir=str(tmp_path))


def _review_with_judge(run, tmp_path, judge, monkeypatch):
    """Drive review() with a green gate stubbed in (no live endpoint/pytest), so we
    isolate the judge layer on top of an already-green deterministic floor. Both
    seams are real boundaries: the pytest subprocess and the grading loader."""
    grading = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "..", "usecase-sample-to-mcp", "grading"))
    # Stub the pytest subprocess to a clean exit, and the grading loader to a green
    # grade, both being true external boundaries (a process + a contract import),
    # not reviewer internals, so this isolates the new judge logic honestly.
    monkeypatch.setattr(reviewer.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 0,
                                                       "stdout": "1 passed", "stderr": ""})())
    monkeypatch.setattr(reviewer, "load_grading",
                        lambda _dir: (lambda _client: {"passed": True, "checks": [
                            {"check": "tool_discovery", "passed": True, "detail": "ok"}]},
                                      object, lambda _ep: object()))
    return reviewer.review(run, grading, "cost_analyzer", 1, judge=judge)


def test_llm_judge_can_withhold_lgtm_on_a_green_gate(tmp_path, monkeypatch):
    """The softened gate: even with pytest + critique green, a disapproving LLM
    judge appends a failing check and the run does NOT get the pass token."""
    run = _green_run(tmp_path)
    verdict = _review_with_judge(
        run, tmp_path, monkeypatch=monkeypatch,
        judge=lambda *a: {"approve": False, "reasons": ["off-by-one in the price rounding"]})
    assert verdict.lgtm is False
    assert any(c["check"] == "llm_review" and not c["passed"] for c in verdict.critique)


def test_llm_judge_abstain_is_a_no_op(tmp_path, monkeypatch):
    """FAIL-OPEN: a judge that abstains (None, e.g. no model) leaves the verdict
    exactly as the deterministic gate+critique decided: green stays green."""
    run = _green_run(tmp_path)
    verdict = _review_with_judge(run, tmp_path, monkeypatch=monkeypatch, judge=lambda *a: None)
    assert verdict.lgtm is True
    assert not any(c["check"] == "llm_review" for c in verdict.critique)


def test_llm_judge_approve_is_recorded(tmp_path, monkeypatch):
    run = _green_run(tmp_path)
    verdict = _review_with_judge(
        run, tmp_path, monkeypatch=monkeypatch, judge=lambda *a: {"approve": True, "reasons": []})
    assert verdict.lgtm is True
    assert any(c["check"] == "llm_review" and c["passed"] for c in verdict.critique)
