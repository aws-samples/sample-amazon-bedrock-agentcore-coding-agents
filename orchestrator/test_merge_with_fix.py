"""Merge with fix: under automatic merge, the one fix finishes a pull request.

The 60-team load test (2026-11-17) ended pull requests in needs_human after their only
repair because the fixed, green pull request was reviewed again and blocked on new
findings. Under ``WORKSHOP_MERGE_POLICY=auto`` the fix turn receives every finding of
the first round, and then the SAME executable check decides: green merges with no
second review round, red still blocks. ``human_review`` keeps the second review, which
a person reads before merging anyway.

    python3 -m pytest orchestrator/test_merge_with_fix.py -q
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import TERMINAL, Engine  # noqa: E402
import roles  # noqa: E402
from fixture_executor import FixtureExecutor  # noqa: E402

ALL_AGENTS = list(roles.roster_ids())


def _wait_terminal(run, timeout_s: float = 180.0):
    deadline = time.monotonic() + timeout_s
    while run.status not in TERMINAL:
        assert time.monotonic() < deadline, f"run stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return run


def _reject_first_review_of_one_pull_request(monkeypatch, victim: dict, calls: list):
    import reviewer as _reviewer
    real_assess = _reviewer.assess

    def assess(run, gate, round_no, *args, **kwargs):
        subject = kwargs.get("subject") or (args[1] if len(args) > 1 else None)
        work_id = getattr(subject, "work_id", "")
        calls.append((work_id, bool(gate.get("passed"))))
        if work_id and gate.get("passed") and (not victim or victim.get("work_id") == work_id):
            victim.setdefault("work_id", work_id)
            return _reviewer.Verdict(
                state="changes_requested", gate=gate, round=round_no,
                reasons=["Minor: the README names the wrong default port"],
                assessment="**Assessment**: Request changes\n\nfor the test",
            )
        return real_assess(run, gate, round_no, *args, **kwargs)

    monkeypatch.setattr("engine.reviewer.assess", assess)


def _record_comments(monkeypatch, posted: list):
    real = Engine._comment_work_item

    def comment(self, run, item, body):
        posted.append((item.work_id, body))
        return real(self, run, item, body)

    monkeypatch.setattr(Engine, "_comment_work_item", comment)


def _builders(run):
    return [item for item in run.work_items.values() if item.kind == "builder"]


def test_a_fixed_green_pull_request_merges_without_a_second_review(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    victim: dict = {}
    calls: list = []
    posted: list = []
    _reject_first_review_of_one_pull_request(monkeypatch, victim, calls)
    _record_comments(monkeypatch, posted)
    engine = Engine(executor_obj=FixtureExecutor())
    try:
        run = _wait_terminal(engine.submit("build any useful tool", ALL_AGENTS))
        assert victim, "the test must have rejected one pull request"
        rows = {row["work_id"]: row for row in run.role_prs}
        assert rows[victim["work_id"]]["state"] == "merged", run.role_prs
        assert run.status == "passed", (run.status, run.fail_reason)
        # Reviewed ONCE (the rejection); the fix was judged by the re-run check alone.
        assert [c for c in calls if c[0] == victim["work_id"]] == [(victim["work_id"], True)]
        owner = next(i for i in _builders(run) if i.work_id == victim["work_id"])
        assert owner.repair_attempts == 1
        bodies = [b for w, b in posted if w == victim["work_id"]]
        assert any("merged with the fix" in b for b in bodies), bodies
        assert any("merge with fix" in (e.get("message") or "") for e in run.events)
    finally:
        engine.shutdown()


def test_a_red_rerun_after_the_fix_still_blocks(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    real_gate = Engine._gate_one_pull_request
    victim: dict = {}

    def red_for_one(self, run, item, stage):
        if not victim or victim.get("work_id") == item.work_id:
            victim.setdefault("work_id", item.work_id)
            return {"passed": False, "summary": "held red",
                    "checks": [{"check": "held", "passed": False,
                                "detail": "FAIL the round never ends"}]}
        return real_gate(self, run, item, stage)

    monkeypatch.setattr(Engine, "_gate_one_pull_request", red_for_one)
    engine = Engine(executor_obj=FixtureExecutor())
    try:
        run = _wait_terminal(engine.submit("build any useful tool", ALL_AGENTS))
        rows = {row["work_id"]: row for row in run.role_prs}
        assert rows[victim["work_id"]]["state"] == "blocked"
        assert rows[victim["work_id"]]["error"] == "GATE_RED"
        assert run.status == "needs_human"
        owner = next(i for i in _builders(run) if i.work_id == victim["work_id"])
        assert owner.repair_attempts == 1, "one fix, never a second"
        assert not any("merge with fix" in (e.get("message") or "") for e in run.events)
    finally:
        engine.shutdown()


def test_human_review_keeps_the_second_review(monkeypatch):
    """Nothing merges under human_review, so the second review stays: a person reads it."""
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "human_review")
    import reviewer as _reviewer
    victim: dict = {}
    real_assess = _reviewer.assess

    def always_reject_one(run, gate, round_no, *args, **kwargs):
        subject = kwargs.get("subject") or (args[1] if len(args) > 1 else None)
        work_id = getattr(subject, "work_id", "")
        if work_id and (not victim or victim.get("work_id") == work_id):
            victim.setdefault("work_id", work_id)
            return _reviewer.Verdict(
                state="changes_requested", gate=gate, round=round_no,
                reasons=["not acceptable"], assessment="**Assessment**: Request changes")
        return real_assess(run, gate, round_no, *args, **kwargs)

    monkeypatch.setattr("engine.reviewer.assess", always_reject_one)
    engine = Engine(executor_obj=FixtureExecutor())
    try:
        run = _wait_terminal(engine.submit("build any useful tool", ALL_AGENTS))
        rows = {row["work_id"]: row for row in run.role_prs}
        assert rows[victim["work_id"]]["state"] == "blocked"
        assert rows[victim["work_id"]]["error"] == "ITERATION_CAP"
        assert not any("merge with fix" in (e.get("message") or "") for e in run.events)
    finally:
        engine.shutdown()


def test_the_fix_turn_reads_the_red_check_itself(tmp_path):
    """The canary's repeated failures: a fix that sees only FAIL lines guesses."""
    from types import SimpleNamespace
    engine = Engine.__new__(Engine)
    check = tmp_path / "acceptance_check"
    check.write_text("#!/usr/bin/env node\n// ties: the earlier entry must rank first\n")
    item = SimpleNamespace(agent="claude-code", work_id="work_claude-code_x")
    engine._builder_items = lambda _run: [item]
    run = SimpleNamespace(review={"gate": {"passed": False}}, _item_checks={item.work_id: str(check)})
    text = engine._failed_check_feedback(run, "claude-code")
    assert "THE CHECK THAT FAILED" in text and "ties: the earlier entry must rank first" in text
    assert "Do not edit, imitate, detect, or special-case it" in text
    # A green gate (a review-only fix) sends no check, and neither does another role.
    run.review = {"gate": {"passed": True}}
    assert engine._failed_check_feedback(run, "claude-code") == ""
    run.review = {"gate": {"passed": False}}
    assert engine._failed_check_feedback(run, "codex") == ""
