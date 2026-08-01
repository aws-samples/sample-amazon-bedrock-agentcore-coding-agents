"""Reviewer tests: the SEPARATE review pen whose verdict lands on the PR.

Stage 2 has attendees read ``reviewer.py`` after the router: the pass token, the
one-bounded-pass rule, the strict branch-suffix guard, the executable acceptance
gate, and the required integrated review. These tests pin that contract,
unit-tested without a model:

    python3 -m pytest orchestrator/test_reviewer.py -v

The full over-the-wire loop (authored gate + PR + assessment against a booted
endpoint) is exercised end-to-end in ``test_engine.py``. Here we pin the
deterministic units that need no server, so the loop is fast.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reviewer  # noqa: E402
from reviewer import (  # noqa: E402
    LGTM_TOKEN,
    Verdict,
    branch_run_id,
)
from work_items import WorkItem  # noqa: E402


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
    assert (
        branch_run_id("run/run_103512_a1b2c3d4e5f6")
        == "run_103512_a1b2c3d4e5f6"
    )


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
    gate = reviewer.run_gate(run)
    assert gate["passed"] is True
    assert gate["checks"][0]["check"] == "acceptance_check_authored"
    assert "discovery" in gate["summary"]


def test_gate_red_exit_can_never_pass(tmp_path):
    authored = _authored_executable(
        tmp_path, "#!/bin/sh\necho 'correctness: expected hello, got nothing'\nexit 3\n")
    run = _FakeRun(acceptance_test_file=authored, artifact_endpoint="http://127.0.0.1:1")
    gate = reviewer.run_gate(run)
    assert gate["passed"] is False
    assert "exit 3" in gate["checks"][0]["detail"]


def test_gate_is_language_agnostic(tmp_path):
    """The authored test is any executable with a shebang: nothing assumes a
    Python test framework. Here the validator chose plain sh."""
    authored = _authored_executable(
        tmp_path, "#!/bin/sh\n# no python, no test framework\nexit 0\n")
    run = _FakeRun(acceptance_test_file=authored)
    assert reviewer.run_gate(run)["passed"] is True


def test_gate_passes_the_endpoint_env_to_the_executable(tmp_path):
    authored = _authored_executable(
        tmp_path,
        '#!/bin/sh\ntest -n "$DELIVERABLE_URL" || exit 9\n'
        'test -n "$MCP_ENDPOINT_URL" || exit 8\n'
        'echo "probing $DELIVERABLE_URL"\nexit 0\n')
    run = _FakeRun(acceptance_test_file=authored,
                   artifact_endpoint="http://127.0.0.1:9999")
    gate = reviewer.run_gate(run)
    assert gate["passed"] is True
    assert "9999" in gate["summary"]


def test_no_authored_check_is_red_never_a_courtesy_pass():
    """Validation is agentic only: with no check authored by the validator, NOTHING
    proved the deliverable, so the gate is RED. There is deliberately no fallback
    grade, because a fallback would be this repository deciding correctness."""
    run = _FakeRun(artifact_endpoint="http://127.0.0.1:1")
    gate = reviewer.run_gate(run)
    assert gate["passed"] is False
    assert gate["checks"][0]["check"] == "acceptance_check_authored"
    assert "no fallback" in gate["checks"][0]["detail"]


def test_unexecutable_check_is_red_not_an_error(tmp_path):
    """A check that cannot run leaves the deliverable unproven, which is exactly
    what red means: the run fails honestly instead of crashing the engine."""
    bad = tmp_path / "acceptance_check"
    bad.write_text("#!/nonexistent/interpreter\n")
    bad.chmod(0o755)
    gate = reviewer.run_gate(_FakeRun(acceptance_test_file=str(bad)))
    assert gate["passed"] is False


# ------------------------------------------------------------- the verdict shape
def test_verdict_public_shape():
    v = Verdict(state="approved", lgtm=True, round=1,
                gate={"passed": True, "checks": []})
    pub = v.public()
    assert set(pub) == {
        "state", "lgtm", "round", "gate", "reasons", "assessment", "panels",
        "review_unavailable"}
    assert pub["lgtm"] is True


# ---------------------------------------------------- the required integrated review
_GREEN_GATE = {"passed": True, "checks": [
    {"check": "acceptance_test_authored", "passed": True, "detail": "green"}],
    "summary": "all checks green"}
_RED_GATE = {"passed": False, "checks": [
    {"check": "acceptance_test_authored", "passed": False,
     "detail": "correctness: service returned unexpected response"}],
    "summary": "exit 1"}


def test_red_gate_is_never_assessed_approvable():
    """A red gate short-circuits: no judge runs, changes are requested, and the
    failing detail becomes the loop feedback."""
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("judge must not run"))  # noqa: E731
    v = reviewer.assess(_FakeRun(), _RED_GATE, 1, judge=boom)
    assert v.lgtm is False
    assert v.state == "changes_requested"
    assert v.reasons and all(isinstance(r, str) and r for r in v.reasons)
    assert LGTM_TOKEN not in v.assessment
    assert "Request changes" in v.assessment


def test_judge_outage_blocks_merge_without_inventing_a_finding():
    """A green executable is not enough when the required review did not run."""
    for judge in (
        lambda *args: None,
        lambda *args: (_ for _ in ()).throw(RuntimeError("boom")),
    ):
        verdict = reviewer.assess(_FakeRun(), _GREEN_GATE, 1, judge=judge)
        assert verdict.lgtm is False
        assert verdict.state == "changes_requested"
        assert verdict.review_unavailable is True
        assert len(verdict.panels) == 1
        assert verdict.panels[0]["name"] == "integrated"
        assert all(row["state"] == "abstained" for row in verdict.panels)
        assert LGTM_TOKEN not in verdict.assessment


def test_judge_controls_green_gate_and_reads_each_role_patch(tmp_path):
    withheld = reviewer.assess(
        _FakeRun(), _GREEN_GATE, 1,
        judge=lambda *a: {"approve": False,
                          "reasons": ["off-by-one in the price rounding"],
                          "assessment": "**Assessment**: Request changes\n\nrounding bug"})
    assert withheld.lgtm is False
    assert withheld.state == "changes_requested"
    assert "rounding" in " ".join(withheld.reasons)
    assert LGTM_TOKEN not in withheld.assessment

    approved = reviewer.assess(
        _FakeRun(), _GREEN_GATE, 1,
        judge=lambda *a: {"approve": True, "reasons": [],
                          "assessment": "**Assessment**: Approve\n\nclean, well-scoped"})
    assert approved.lgtm is True
    assert approved.assessment.startswith("**Assessment**: Approve")
    assert LGTM_TOKEN in approved.assessment

    candidate = tmp_path / "candidate"
    (candidate / "api").mkdir(parents=True)
    (candidate / "web").mkdir()
    (candidate / "api" / "service.py").write_text("print('api')\n")
    (candidate / "web" / "app.tsx").write_text("export default App\n")
    backend = WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend", token="back")
    frontend = WorkItem.create(
        "run_1", "opencode", "frontend-builder", "frontend", token="front")
    checker = WorkItem.create(
        "run_1", "claude-code-validator", "acceptance-validator", "validator",
        kind="checker", token="check")
    backend.changed_files = ["api/service.py"]
    frontend.changed_files = ["web/app.tsx"]
    checker.changed_files = ["acceptance_check"]
    run = _FakeRun()
    run.candidate_dir = str(candidate)
    run.work_items = {
        backend.agent: backend,
        frontend.agent: frontend,
        checker.agent: checker,
    }
    labels = [label for label, _path in reviewer._artifact_files(run)]
    assert any(backend.work_id in label and "api/service.py" in label
               for label in labels)
    assert any(frontend.work_id in label and "web/app.tsx" in label
               for label in labels)
    assert not any(checker.work_id in label for label in labels)


def test_judge_approval_requires_usage_evidence_for_every_builder():
    backend = WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend", token="back")
    frontend = WorkItem.create(
        "run_1", "opencode", "frontend-builder", "frontend", token="front")
    required = [backend.work_id, frontend.work_id]
    incomplete = (
        '{"approve":true,"reasons":[],"work_item_evidence":{'
        f'"{backend.work_id}":"UI calls its issue API"'
        '},"adversarial_assessment":"Runtime behavior is sound",'
        '"design_assessment":"The components are integrated"}'
    )
    with pytest.raises(reviewer._JudgeEvidenceError, match=frontend.work_id):
        reviewer._parse_judge_response(incomplete, required)

    complete = reviewer._parse_judge_response(
        '{"approve":true,"reasons":[],"work_item_evidence":{'
        f'"{backend.work_id}":"The running API owns persistence",'
        f'"{frontend.work_id}":"The browser calls that API over the shared boundary"'
        '},"adversarial_assessment":"Traced API values into the UI",'
        '"design_assessment":"The runtime path is coherent"}',
        required,
    )
    assert complete["approve"] is True
    assert all(work_id in complete["assessment"] for work_id in required)
    assert "Adversarial verification" in complete["assessment"]
    assert "Design & integration" in complete["assessment"]


def test_integrated_review_runs_once_and_either_lens_can_block(
        monkeypatch, tmp_path):
    """One call must cover both lenses; a finding under either one blocks."""
    import llm

    candidate = tmp_path / "candidate"
    (candidate / "api").mkdir(parents=True)
    (candidate / "web").mkdir()
    (candidate / "api" / "activity.js").write_text(
        "return {detail: {before: oldValue, after: newValue}}\n")
    (candidate / "web" / "activity.jsx").write_text(
        "render(activity.detail.from, activity.detail.to)\n")
    backend = WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend", token="back")
    frontend = WorkItem.create(
        "run_1", "opencode", "frontend-builder", "frontend", token="front")
    backend.changed_files = ["api/activity.js"]
    frontend.changed_files = ["web/activity.jsx"]
    run = _FakeRun(task="show activity changes")
    run.candidate_dir = str(candidate)
    run.integration_brief = {
        "summary": "Share activity values across API and UI.",
        "shared_contract": ["detail carries before and after values"],
        "role_assignments": {
            backend.agent: {"objective": "produce activity data"},
            frontend.agent: {"objective": "render activity data"},
        },
        "merge_order": [backend.agent, frontend.agent],
    }
    run.work_items = {backend.agent: backend, frontend.agent: frontend}

    calls = []

    def invoke(model, prompt, system=None, max_tokens=0):
        calls.append({
            "model": model,
            "prompt": prompt,
            "system": system,
            "max_tokens": max_tokens,
        })
        return {
            "model_id": "integrated-review-model",
            "text": json.dumps({
                "approve": False,
                "reasons": [
                    "API emits detail.before/after while the UI reads "
                    "detail.from/to, so real values disappear."
                ],
                "work_item_evidence": {
                    backend.work_id: "API produces the activity payload.",
                    frontend.work_id: "UI renders the activity payload.",
                },
                "adversarial_assessment": (
                    "The producer and consumer field names disagree."
                ),
                "design_assessment": (
                    "The role split is clear, but the shared contract is not "
                    "implemented consistently."
                ),
            }),
        }

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "invoke", invoke)
    review = reviewer._default_judge(run, _GREEN_GATE)
    assert len(calls) == 1
    assert "Apply BOTH" in calls[0]["system"]
    assert "Adversarial verification" in calls[0]["system"]
    assert "Design and integration" in calls[0]["system"]
    assert review["approve"] is False
    assert [row["state"] for row in review["panels"]] == [
        "changes_requested"]
    assert "Adversarial verification" in review["assessment"]
    assert "Design & integration" in review["assessment"]

    verdict = reviewer.assess(
        run, _GREEN_GATE, 1, judge=lambda *_args: review)
    assert verdict.state == "changes_requested"
    assert "before/after" in " ".join(verdict.reasons)
    assert [row["name"] for row in verdict.panels] == ["integrated"]


def test_integrated_review_requires_both_lens_sections():
    response = json.dumps({
        "approve": False,
        "reasons": ["runtime mismatch"],
        "work_item_evidence": {},
        "adversarial_assessment": "Found a mismatch.",
    })
    with pytest.raises(ValueError, match="design_assessment"):
        reviewer._parse_judge_response(response, [])


def test_reasons_feed_the_reimplement_loop():
    """The engine forwards verdict.reasons into the next round's role prompts:
    the loop's feedback channel is the structured reasons, not a committed file."""
    v = reviewer.assess(
        _FakeRun(), _GREEN_GATE, 1,
        judge=lambda *a: {"approve": False,
                          "reasons": ["error text leaks internals", "no empty-input case"],
                          "assessment": "**Assessment**: Request changes\n\ntwo issues"})
    assert v.reasons == ["error text leaks internals", "no empty-input case"]


# ---------------------------------------------- the gate leaks no processes
def _alive(pid: int) -> bool:
    """True while the pid exists (signal 0 probes without killing)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_gate_reaps_a_service_the_check_started(tmp_path):
    """The validator is TOLD to start the deliverable if it needs to be running, so a
    check routinely leaves a service running as its own child. A service process never
    exits on its own, so if the gate reaped only its direct child, every run would leak
    one forever (the failure mode that once wedged a box with thousands of orphans).
    The whole process group must go down."""
    marker = tmp_path / "child.pid"
    authored = _authored_executable(
        tmp_path,
        "#!/bin/sh\n"
        # a 'service': outlives the check, exactly like a real one would
        f"sleep 120 & echo $! > {marker}\n"
        "echo 'started the deliverable'\nexit 0\n")
    gate = reviewer.run_gate(_FakeRun(acceptance_test_file=authored))
    assert gate["passed"] is True                 # the real exit code still decides
    child = int(marker.read_text().strip())
    for _ in range(50):                           # the reap is signal-fast, not instant
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child), (
        f"pid {child} survived the gate: a service the check started leaked")


def test_gate_reaps_the_group_when_the_check_times_out(tmp_path, monkeypatch):
    """A check that hangs is killed, and so is everything it spawned. Killing only the
    check would leave its children running with nobody left to reap them."""
    monkeypatch.setattr(reviewer, "GATE_TIMEOUT_S", 1)
    marker = tmp_path / "child.pid"
    authored = _authored_executable(
        tmp_path,
        "#!/bin/sh\n"
        f"sleep 120 & echo $! > {marker}\n"
        "sleep 120\n")                            # the check itself hangs
    gate = reviewer.run_gate(_FakeRun(acceptance_test_file=authored))
    assert gate["passed"] is False                # a timeout can never be a pass
    assert "124" in gate["checks"][0]["detail"] or "did not finish" in gate["summary"]
    child = int(marker.read_text().strip())
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child), (
        f"pid {child} survived a timed-out gate: the group was not reaped")


def test_gate_summary_skips_the_started_services_own_log_lines():
    """The gate summary is what a human reads on the PR, so it must be the CHECK's
    verdict, not the deliverable's teardown noise.

    Live: a passing run published `INFO:     Finished server process [25985]` as its
    gate summary (PR body, assessment comment, run_status, ledger) while the check's
    real verdict sat four lines above it. Same shape as the `====` divider case: the one
    human-readable sentence the check produced replaced by noise around it.
    """
    out = (
        "  PASS: Data survives server restart (persistence)\n"
        "══════════\n"
        " TOTAL: 30 checks | PASS: 30 | FAIL: 0\n"
        "══════════\n"
        "INFO:     Shutting down\n"
        "INFO:     Waiting for application shutdown.\n"
        "INFO:     Application shutdown complete.\n"
        "INFO:     Finished server process [26473]\n"
    )
    assert reviewer._summary_line(out, 0) == "TOTAL: 30 checks | PASS: 30 | FAIL: 0"

    # Box-drawing rules are decoration too, not a verdict.
    assert reviewer._summary_line("ok\n════\n", 0) == "ok"

    # A check whose whole output looks like service logs is still QUOTED, never
    # replaced by a verdict we invented.
    assert reviewer._summary_line("INFO: started\nINFO: stopped", 0) == "INFO: stopped"

    # And the empty case still falls back to the exit code.
    assert reviewer._summary_line("", 1) == "exit 1"
