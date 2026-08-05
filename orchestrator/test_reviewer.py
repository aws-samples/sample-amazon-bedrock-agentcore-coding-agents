"""Reviewer tests: the SEPARATE review pen whose verdict lands on ONE pull request.

Stage 2 has attendees read ``reviewer.py`` after the router: the pass token, the
one-bounded-pass rule, the strict branch-suffix guard, the executable acceptance
gate, and the required integrated review. These tests pin that contract,
unit-tested without a model:

    python3 -m pytest orchestrator/test_reviewer.py -v

Every decision here is PER PULL REQUEST. The gate takes one authored check and one
tree; the review takes one work item as its ``subject``, sees that pull request's
own changes on the base branch as it stands, and its verdict can stop only that
pull request.

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
    tested as the pure function of artifacts it is, with no engine.

    Per-pull-request shape: ``_item_checks`` maps a work id to the check the
    validator authored FOR THAT pull request, and ``item_tree_dir`` returns that
    pull request's tree (its own patch applied to the base branch as it stands).
    """

    def __init__(self, *, run_id="run_103512_004", agents=None, route=None,
                 iterations=1, workdir=None, artifact_endpoint=None, task="",
                 final_base_branch="main", item_trees=None, item_checks=None,
                 work_items=None):
        self.run_id = run_id
        self.agents = agents or []
        self.route = route or {}
        self.iterations = iterations
        self.workdir = workdir or ""
        self.artifact_endpoint = artifact_endpoint
        self.task = task
        self.final_base_branch = final_base_branch
        self.work_items = work_items or {}
        self._item_checks = dict(item_checks or {})
        self._item_trees = dict(item_trees or {})

    def item_tree_dir(self, work_id: str) -> str:
        return self._item_trees.get(work_id, "")


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
    gate = reviewer.run_gate(
        authored, str(tmp_path), "build it", "http://127.0.0.1:1")
    assert gate["passed"] is True
    assert gate["checks"][0]["check"] == "acceptance_check_authored"
    assert "discovery" in gate["summary"]


def test_gate_red_exit_can_never_pass(tmp_path):
    authored = _authored_executable(
        tmp_path, "#!/bin/sh\necho 'correctness: expected hello, got nothing'\nexit 3\n")
    gate = reviewer.run_gate(
        authored, str(tmp_path), "build it", "http://127.0.0.1:1")
    assert gate["passed"] is False
    assert "exit 3" in gate["checks"][0]["detail"]


def test_gate_is_language_agnostic(tmp_path):
    """The authored test is any executable with a shebang: nothing assumes a
    Python test framework. Here the validator chose plain sh."""
    authored = _authored_executable(
        tmp_path, "#!/bin/sh\n# no python, no test framework\nexit 0\n")
    assert reviewer.run_gate(authored, str(tmp_path), "build it")["passed"] is True


def test_gate_passes_the_endpoint_env_to_the_executable(tmp_path):
    """The gate hands the check FACTS about its environment and nothing that says
    what a correct answer is: the deliverable URL (plus the compatible
    ``MCP_ENDPOINT_URL`` alias), the tree the builders wrote, the request as typed,
    and the wall-clock budget it is judged against."""
    authored = _authored_executable(
        tmp_path,
        '#!/bin/sh\ntest -n "$DELIVERABLE_URL" || exit 9\n'
        'test -n "$MCP_ENDPOINT_URL" || exit 8\n'
        'test "$MCP_ENDPOINT_URL" = "$DELIVERABLE_URL" || exit 7\n'
        'test "$WORKSHOP_WORK_DIR" = "'
        + str(tmp_path) + '" || exit 6\n'
        'test "$WORKSHOP_TASK" = "an issue tracker" || exit 5\n'
        'test "$WORKSHOP_GATE_TIMEOUT_S" -ge 60 || exit 4\n'
        'echo "probing $DELIVERABLE_URL"\nexit 0\n')
    gate = reviewer.run_gate(
        authored, str(tmp_path), "an issue tracker", "http://127.0.0.1:9999")
    assert gate["passed"] is True
    assert "9999" in gate["summary"]


def test_no_authored_check_is_red_never_a_courtesy_pass():
    """Validation is agentic only: with no check authored by the validator for this
    pull request, NOTHING proved it, so the gate is RED. There is deliberately no
    fallback grade, because a fallback would be this repository deciding
    correctness."""
    gate = reviewer.run_gate("", "", "build it", "http://127.0.0.1:1")
    assert gate["passed"] is False
    assert gate["checks"][0]["check"] == "acceptance_check_authored"
    assert "no fallback" in gate["checks"][0]["detail"]


def test_unexecutable_check_is_red_not_an_error(tmp_path):
    """A check that cannot run leaves the deliverable unproven, which is exactly
    what red means: the run fails honestly instead of crashing the engine."""
    bad = tmp_path / "acceptance_check"
    bad.write_text("#!/nonexistent/interpreter\n")
    bad.chmod(0o755)
    gate = reviewer.run_gate(str(bad), str(tmp_path), "build it")
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


def test_judge_controls_green_gate_and_reads_the_pull_request_under_review(tmp_path):
    """A green executable is evidence, not permission: the judge decides, per pull
    request. And it must actually READ that pull request -- its own changed files
    first, then the base branch it merges into.

    ``_artifact_files`` once looked for ``_server_file`` / ``_ui_dir`` attributes from
    the deleted fixed-shape design that nothing had set in a long time, so the judge
    saw only the authored check and reviewed a pull request whose code it had never
    read. This pins that it reads the real tree.
    """
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

    # One pull request's tree: this role's own change, plus the sibling code that
    # ALREADY MERGED onto the default branch (which is how a cross-role defect stays
    # catchable without an assembled candidate).
    backend = WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend",
        base_branch="main", token="back")
    frontend = WorkItem.create(
        "run_1", "opencode", "frontend-builder", "frontend",
        base_branch="main", token="front")
    checker = WorkItem.create(
        "run_1", "kiro", "acceptance-validator", "validator",
        kind="checker", base_branch="main", token="check")
    tree = tmp_path / backend.work_id
    (tree / "api").mkdir(parents=True)
    (tree / "web").mkdir()
    (tree / "api" / "service.py").write_text("def items(): return {'items': []}\n")
    (tree / "web" / "app.tsx").write_text("fetch('/items')\n")
    backend.changed_files = ["api/service.py"]
    frontend.changed_files = ["web/app.tsx"]
    checker.changed_files = ["acceptance_check"]
    authored = _authored_executable(tmp_path, "#!/bin/sh\nexit 0\n")
    run = _FakeRun(
        item_trees={backend.work_id: str(tree)},
        item_checks={backend.work_id: authored},
        work_items={backend.agent: backend, frontend.agent: frontend,
                    checker.agent: checker},
    )

    artifacts = reviewer._artifact_files(run, backend)
    labels = [label for label, _path in artifacts]
    by_label = dict(artifacts)

    # The check that will decide, and the pull request's OWN change, attributed.
    assert "the validator's authored acceptance check" in labels
    own = [label for label in labels
           if backend.work_id in label and "api/service.py" in label]
    assert own, labels
    assert backend.capability in own[0]

    # The judge reads real CODE, not just a path list.
    assert "'items'" in open(by_label[own[0]], encoding="utf-8").read()

    # The already-merged sibling file is offered as the base branch, never as this
    # pull request's own work, so the judge can compare both sides of the seam.
    base = [label for label in labels if "web/app.tsx" in label]
    assert base and base[0].startswith("on the base branch:")
    assert backend.work_id not in base[0]

    # Only the pull request under review is attributed: a sibling's open pull request
    # is not this one's responsibility, and the checker never appears as a subject.
    assert not any(frontend.work_id in label for label in labels)
    assert not any(checker.work_id in label for label in labels)

    # Its own changed file comes before the base-branch fill, because the reviewer
    # must see what the change DOES before the tree it sits on.
    assert labels.index(own[0]) < labels.index(base[0])


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


def _seam_mismatch_run(tmp_path):
    """One pull request whose producer disagrees with a consumer already merged.

    This is the live false negative that made both lenses mandatory: run
    ``run_165304_001`` produced ``detail.before/after`` while the code consuming it
    read ``detail.from/to``, and a single broad check passed anyway. So the tree the
    reviewer gets is this pull request's own file PLUS the counterpart sitting on the
    base branch, which is what makes the mismatch findable.
    """
    backend = WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend",
        base_branch="main", token="back")
    frontend = WorkItem.create(
        "run_1", "opencode", "frontend-builder", "frontend",
        base_branch="main", token="front")
    tree = tmp_path / backend.work_id
    (tree / "api").mkdir(parents=True)
    (tree / "web").mkdir()
    (tree / "api" / "activity.js").write_text(
        "return {detail: {before: oldValue, after: newValue}}\n")
    (tree / "web" / "activity.jsx").write_text(
        "render(activity.detail.from, activity.detail.to)\n")
    backend.changed_files = ["api/activity.js"]
    frontend.changed_files = ["web/activity.jsx"]
    run = _FakeRun(
        task="show activity changes",
        item_trees={backend.work_id: str(tree)},
        work_items={backend.agent: backend, frontend.agent: frontend},
    )
    run.integration_brief = {
        "summary": "Share activity values across API and UI.",
        "shared_contract": ["detail carries before and after values"],
        "role_assignments": {
            backend.agent: {"objective": "produce activity data"},
            frontend.agent: {"objective": "render activity data"},
        },
        "merge_order": [backend.agent, frontend.agent],
    }
    return run, backend, frontend


def test_integrated_review_runs_once_and_either_lens_can_block(
        monkeypatch, tmp_path):
    """One call must cover both lenses; a finding under either one blocks THIS pull
    request (and only this one)."""
    import llm

    run, backend, frontend = _seam_mismatch_run(tmp_path)

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
    review = reviewer._default_judge(run, _GREEN_GATE, backend)
    assert len(calls) == 1
    assert "Apply BOTH" in calls[0]["system"]
    assert "Adversarial verification" in calls[0]["system"]
    assert "Design and integration" in calls[0]["system"]

    # The turn is scoped to ONE pull request, is told which base it merges into, and
    # is handed both sides of the seam so the mismatch is actually findable.
    prompt = calls[0]["prompt"]
    assert f"YOU ARE REVIEWING ONE PULL REQUEST: {backend.work_id}" in prompt
    assert "'main'" in prompt
    assert "detail: {before" in prompt          # this pull request's own change
    assert "detail.from" in prompt              # the counterpart already on the base

    assert review["approve"] is False
    assert [row["state"] for row in review["panels"]] == [
        "changes_requested"]
    assert "Adversarial verification" in review["assessment"]
    assert "Design & integration" in review["assessment"]

    verdict = reviewer.assess(
        run, _GREEN_GATE, 1, judge=lambda *_args: review, subject=backend)
    assert verdict.state == "changes_requested"
    assert "before/after" in " ".join(verdict.reasons)
    assert [row["name"] for row in verdict.panels] == ["integrated"]
    # A finding on this pull request says nothing about the sibling: the verdict names
    # only the work item it reviewed.
    assert frontend.work_id not in verdict.assessment


@pytest.mark.parametrize("blocking_lens", ["adversarial", "design"])
def test_each_lens_alone_can_block_one_pull_request(
        monkeypatch, tmp_path, blocking_lens):
    """BOTH lenses stay mandatory and EITHER ONE alone withholds approval.

    A clean adversarial pass does not license a design/integration defect, and vice
    versa. The reviewer that only asked "does it do what was asked?" is exactly the
    one that approved a disconnected parallel stack, so neither lens may be treated
    as advisory. Expressed per pull request: the block lands on this work item.
    """
    import llm

    run, backend, _frontend = _seam_mismatch_run(tmp_path)
    clean = "No finding under this lens."
    finding = "Real values disappear across the seam this change touches."
    payload = {
        "approve": False,
        "reasons": [f"{blocking_lens} lens: {finding}"],
        "work_item_evidence": {backend.work_id: "API produces the payload."},
        "adversarial_assessment": (
            finding if blocking_lens == "adversarial" else clean),
        "design_assessment": finding if blocking_lens == "design" else clean,
    }
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "invoke",
        lambda *a, **k: {"model_id": "m", "text": json.dumps(payload)})

    review = reviewer._default_judge(run, _GREEN_GATE, backend)
    assert review["approve"] is False
    verdict = reviewer.assess(
        run, _GREEN_GATE, 1, judge=lambda *_a: review, subject=backend)
    assert verdict.lgtm is False
    assert verdict.state == "changes_requested"
    assert LGTM_TOKEN not in verdict.assessment
    assert "Request changes" in verdict.assessment
    assert blocking_lens in " ".join(verdict.reasons)
    # Both lens sections are still REPORTED, so a human on the pull request can see
    # which one objected and that the other one ran at all.
    assert "Adversarial verification" in verdict.assessment
    assert "Design & integration" in verdict.assessment
    assert finding in verdict.assessment
    assert verdict.review_unavailable is False


def test_a_red_gate_can_never_be_approved_even_if_the_judge_says_approve():
    """Red can never become green. Real execution decides; the reviewer layers on
    top of that verdict and cannot overturn it.

    A judge that returns ``approve: True`` on a red gate is the worst case, so it is
    the one pinned here: the judge is not even consulted, the pass token never
    appears, and the failing detail becomes the loop feedback for the owning role.
    """
    approving_judge_calls = []

    def eager_approve(*args):
        approving_judge_calls.append(args)
        return {"approve": True, "reasons": [],
                "assessment": f"**Assessment**: Approve\n\n{LGTM_TOKEN}\n",
                "panels": [], "review_unavailable": False}

    item = WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend",
        base_branch="main", token="back")
    verdict = reviewer.assess(
        _FakeRun(), _RED_GATE, 1, judge=eager_approve, subject=item)

    assert approving_judge_calls == [], (
        "a red gate must short-circuit: the judge is never given the chance to "
        "approve unproven work")
    assert verdict.lgtm is False
    assert verdict.state == "changes_requested"
    assert LGTM_TOKEN not in verdict.assessment
    assert "Request changes" in verdict.assessment
    assert verdict.reasons == [_RED_GATE["checks"][0]["detail"]]


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
    gate = reviewer.run_gate(authored, str(tmp_path), "build it")
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
    gate = reviewer.run_gate(authored, str(tmp_path), "build it")
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
