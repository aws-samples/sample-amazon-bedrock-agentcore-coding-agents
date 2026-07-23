"""Reviewer tests: the SEPARATE review pen whose verdict lands on the PR.

Stage 2 has attendees read ``reviewer.py`` after the router: the pass token, the
one-bounded-pass rule, the strict branch-suffix guard, the executable acceptance
gate, and the fail-open LLM assessment. These tests pin that contract, unit-tested
without a model:

    python3 -m pytest orchestrator/test_reviewer.py -v

The full over-the-wire loop (authored gate + PR + assessment against a booted
endpoint) is exercised end-to-end in ``test_engine.py``. Here we pin the
deterministic units that need no server, so the loop is fast.
"""

from __future__ import annotations

import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reviewer  # noqa: E402
from reviewer import (  # noqa: E402
    LGTM_TOKEN,
    Verdict,
    branch_run_id,
)


class _FakeRun:
    """The minimal slice of a Run that the reviewer reads, so the verdict can be
    tested as the pure function of artifacts it is, with no engine."""

    def __init__(self, *, run_id="run_103512_004", agents=None, route=None,
                 server_file=None, chatbot_file=None, composed_branch=None,
                 review_target=None, iterations=1, workdir=None,
                 artifact_endpoint=None, task="", acceptance_test_file=None,
                 ui_dir=None):
        self.run_id = run_id
        self.agents = agents or []
        self.route = route or {}
        self._server_file = server_file
        self._chatbot_file = chatbot_file
        self._ui_dir = ui_dir
        self.composed_branch = composed_branch
        self._review_target = review_target
        self.iterations = iterations
        self.workdir = workdir or ""
        self.artifact_endpoint = artifact_endpoint
        self.task = task
        self._acceptance_test_file = acceptance_test_file


# ----------------------------------------------------- the strict branch-suffix guard
def test_branch_run_id_maps_a_run_branch_back_to_its_run():
    assert branch_run_id("run/run_103512_004") == "run_103512_004"


def test_branch_run_id_refuses_lookalikes():
    for bad in ("run/run_103512_004-extra", "feature/run_103512_004",
                "run/run_1035_004", "run/%", "run/run_abcdef_004", "", None):
        assert branch_run_id(bad) is None


# --------------------------------------------------------- constants the engine reads
def test_pass_token_is_the_exact_literal():
    assert LGTM_TOKEN == "LGTM: no changes needed"


def test_review_rounds_bound_is_one():
    assert reviewer.MAX_REVIEW_ROUNDS == 1


# --------------------------------------------------- the executable acceptance gate
def _authored_executable(tmp_path, body: str, name: str = "acceptance_test"):
    """Write an executable acceptance test (any language; here sh for speed)."""
    p = tmp_path / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_gate_runs_the_authored_executable_and_green_exit_passes(tmp_path):
    authored = _authored_executable(
        tmp_path, "#!/bin/sh\necho 'discovery: 5 tools present'\nexit 0\n")
    run = _FakeRun(acceptance_test_file=authored, artifact_endpoint="http://127.0.0.1:1")
    gate = reviewer.run_gate(run, grading_dir="/nonexistent")
    assert gate["passed"] is True
    assert gate["checks"][0]["check"] == "acceptance_test_authored"
    assert "discovery" in gate["summary"]


def test_gate_red_exit_can_never_pass(tmp_path):
    authored = _authored_executable(
        tmp_path, "#!/bin/sh\necho 'correctness: expected 140.16, got 0'\nexit 3\n")
    run = _FakeRun(acceptance_test_file=authored, artifact_endpoint="http://127.0.0.1:1")
    gate = reviewer.run_gate(run, grading_dir="/nonexistent")
    assert gate["passed"] is False
    assert "exit 3" in gate["checks"][0]["detail"]


def test_gate_is_language_agnostic(tmp_path):
    """The authored test is any executable with a shebang: nothing assumes a
    Python test framework. Here the validator chose plain sh."""
    authored = _authored_executable(
        tmp_path, "#!/bin/sh\n# no python, no test framework\nexit 0\n")
    run = _FakeRun(acceptance_test_file=authored)
    assert reviewer.run_gate(run, grading_dir="/nonexistent")["passed"] is True


def test_gate_passes_the_endpoint_env_to_the_executable(tmp_path):
    authored = _authored_executable(
        tmp_path,
        '#!/bin/sh\ntest -n "$MCP_ENDPOINT_URL" || exit 9\n'
        'echo "probing $MCP_ENDPOINT_URL"\nexit 0\n')
    run = _FakeRun(acceptance_test_file=authored,
                   artifact_endpoint="http://127.0.0.1:9999")
    gate = reviewer.run_gate(run, grading_dir="/nonexistent")
    assert gate["passed"] is True
    assert "9999" in gate["summary"]


def test_gate_offline_floor_uses_the_grading_contract():
    """With no authored test (fixture/offline), the usecase's shipped grading
    contract grades in-process: the deterministic floor, an unreachable endpoint
    is a red gate, never a crash."""
    here = os.path.dirname(os.path.abspath(__file__))
    grading = os.path.join(os.path.dirname(here), "usecase-sample-to-mcp", "grading")
    run = _FakeRun(artifact_endpoint="http://127.0.0.1:1")  # nothing listens
    gate = reviewer.run_gate(run, grading)
    assert gate["passed"] is False
    assert gate["checks"]  # real failing checks, not an empty fabrication


# --------------------------------------------------------------- grading loader
def test_load_grading_imports_the_contract_and_adapters():
    """The offline floor grades against the usecase's own contract; loading it
    returns the grade function plus both adapters (in-process and over-the-wire)."""
    here = os.path.dirname(os.path.abspath(__file__))
    grading = os.path.join(os.path.dirname(here), "usecase-sample-to-mcp", "grading")
    grade, InProcessClient, RemoteMCPClient = reviewer.load_grading(grading)
    result = grade(InProcessClient())
    assert result["passed"] is True
    assert {c["check"] for c in result["checks"]} == {
        "tool_discovery", "tool_correctness", "input_validation"}


# ------------------------------------------------------------- the verdict shape
def test_verdict_public_shape():
    v = Verdict(state="approved", lgtm=True, round=1,
                gate={"passed": True, "checks": []})
    pub = v.public()
    assert set(pub) == {"state", "lgtm", "round", "gate", "reasons", "assessment"}
    assert pub["lgtm"] is True


# ------------------------------------------------- the assessment (fail-open LLM)
_GREEN_GATE = {"passed": True, "checks": [
    {"check": "acceptance_test_authored", "passed": True, "detail": "green"}],
    "summary": "all checks green"}
_RED_GATE = {"passed": False, "checks": [
    {"check": "acceptance_test_authored", "passed": False,
     "detail": "correctness: m5.large x2 returned 0.0, expected 140.16"}],
    "summary": "exit 1"}


def test_red_gate_is_never_assessed_approvable():
    """A red gate short-circuits: no judge runs, changes are requested, and the
    failing detail becomes the loop feedback."""
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("judge must not run"))  # noqa: E731
    v = reviewer.assess(_FakeRun(), _RED_GATE, 1, judge=boom)
    assert v.lgtm is False
    assert v.state == "changes_requested"
    assert any("140.16" in r for r in v.reasons)
    assert LGTM_TOKEN not in v.assessment
    assert "Request changes" in v.assessment


def test_judge_abstain_leaves_the_green_gate_standing():
    """FAIL-OPEN: no model reachable -> the deterministic gate is the verdict,
    and the assessment says so honestly."""
    v = reviewer.assess(_FakeRun(), _GREEN_GATE, 1, judge=lambda *a: None)
    assert v.lgtm is True
    assert v.state == "approved"
    assert LGTM_TOKEN in v.assessment


def test_judge_can_withhold_approval_on_a_green_gate():
    v = reviewer.assess(
        _FakeRun(), _GREEN_GATE, 1,
        judge=lambda *a: {"approve": False,
                          "reasons": ["off-by-one in the price rounding"],
                          "assessment": "**Assessment**: Request changes\n\nrounding bug"})
    assert v.lgtm is False
    assert v.state == "changes_requested"
    assert "rounding" in " ".join(v.reasons)
    assert LGTM_TOKEN not in v.assessment


def test_judge_approval_carries_the_exact_pass_token():
    """An approving assessment always ends with the literal token, even when the
    model's own markdown forgot it: approval is checkable, never a paraphrase."""
    v = reviewer.assess(
        _FakeRun(), _GREEN_GATE, 1,
        judge=lambda *a: {"approve": True, "reasons": [],
                          "assessment": "**Assessment**: Approve\n\nclean, well-scoped"})
    assert v.lgtm is True
    assert v.assessment.startswith("**Assessment**: Approve")
    assert LGTM_TOKEN in v.assessment


def test_judge_crash_is_fail_open():
    v = reviewer.assess(_FakeRun(), _GREEN_GATE, 1,
                        judge=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert v.lgtm is True
    assert LGTM_TOKEN in v.assessment


def test_reasons_feed_the_reimplement_loop():
    """The engine forwards verdict.reasons into the next round's role prompts:
    the loop's feedback channel is the structured reasons, not a committed file."""
    v = reviewer.assess(
        _FakeRun(), _GREEN_GATE, 1,
        judge=lambda *a: {"approve": False,
                          "reasons": ["error text leaks internals", "no empty-input case"],
                          "assessment": "**Assessment**: Request changes\n\ntwo issues"})
    assert v.reasons == ["error text leaks internals", "no empty-input case"]
