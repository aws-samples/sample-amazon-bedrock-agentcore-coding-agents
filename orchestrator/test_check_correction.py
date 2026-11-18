"""One correction of the CHECK, the checker's counterpart of the builder's one repair.

The November 18 canary (60 teams) stopped 14 builds for a person; in 8 of them the
backend was right and the check's own expectation was wrong. Triage could say so, but
a checker defect had no repair at all. These tests drive the real engine loop with the
offline fixture: the gate is still a real subprocess of a real file, the checker (not
the engine) writes the correction, the corrected file runs on the unchanged pull
request, a correction that checks less does not count, and each allowance is spent
at most once.

    python3 -m pytest orchestrator/test_check_correction.py -q
"""

from __future__ import annotations

import os
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import TERMINAL, Engine  # noqa: E402
import roles  # noqa: E402
from fixture_executor import FixtureExecutor  # noqa: E402

TASK = "build a small leaderboard service"
TEAM = ["claude-code", "kiro"]

# The check that ran: two passing lines and one wrong expectation.
ORIGINAL = """#!/bin/sh
echo "PASS the service answers"
echo "PASS a bad score is refused"
echo "FAIL the 20th entry is 500 (got 1001)"
exit 1
"""
# The checker's correction: the same three checks, the expectation fixed.
CORRECTED = """#!/bin/sh
echo "PASS the service answers"
echo "PASS a bad score is refused"
echo "PASS the 20th entry is the lowest of the top 20 the check sent (1001)"
exit 0
"""
# A "correction" that passes by checking less.
NARROWED = """#!/bin/sh
echo "PASS the service answers"
exit 0
"""
# A correction that still finds the builder's first-round defect.
CORRECTED_STRICT = """#!/bin/sh
echo "PASS the service answers"
if grep -l '^fixture repair pending$' "$WORKSHOP_WORK_DIR"/*-work.txt >/dev/null 2>&1; then
  echo "FAIL the work still says a repair is pending"
  exit 1
fi
echo "PASS the work is complete"
exit 0
"""


def _wait(run, timeout_s: float = 120.0):
    deadline = time.monotonic() + timeout_s
    while run.status not in TERMINAL:
        assert time.monotonic() < deadline, f"run stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return run


def _checker_writes(original: str, correction: str):
    """A fixture whose checker authors ``original`` and corrects it to ``correction``."""
    fixture = FixtureExecutor()
    produce = fixture.produce
    turns: list[str] = []

    def write(run, agent_id, role):
        if roles.get(agent_id).kind != roles.CHECKER:
            return produce(run, agent_id, role)
        run._offline_double = True
        path = os.path.join(run.roledir(agent_id), "acceptance_check")
        correcting = bool(run._check_corrections)
        if correcting:
            # The correction starts from the file that ran, in the checker's directory.
            with open(path, encoding="utf-8") as handle:
                assert handle.read() == original
        turns.append("correct" if correcting else "author")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(correction if correcting else original)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    fixture.produce = write
    return fixture, turns


def _triage(monkeypatch, *answers):
    """Triage answers in order; the last answer repeats."""
    calls: list[dict] = []

    def route(task, plan, items, evidence, **kwargs):
        calls.append(evidence)
        agents = answers[min(len(calls), len(answers)) - 1]
        return list(agents), ("the check's own expectation is wrong" if not agents
                              else "the builder's code fails the request")

    monkeypatch.setattr("engine.integration_plan.select_repair_agents", route)
    return calls


def _builder(run):
    return run.work_items["claude-code"]


def test_a_check_defect_is_corrected_once_and_the_corrected_check_decides(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    calls = _triage(monkeypatch, [])
    fixture, turns = _checker_writes(ORIGINAL, CORRECTED)
    engine = Engine(executor_obj=fixture)
    try:
        run = _wait(engine.submit(TASK, TEAM))
        assert run.status == "passed", (run.status, run.fail_reason, run.role_prs)
        assert turns == ["author", "correct"]
        assert [row["passed"] for row in run.gate_history] == [False, True]
        first, corrected = run.gate_history
        assert first["check_sha256"] != corrected["check_sha256"]
        assert corrected["stage"].endswith("(corrected check)")
        assert first["check_lines"] == corrected["check_lines"] == 3
        # No builder was asked to change anything, and its one repair is unspent.
        assert _builder(run).attempt == 1
        assert _builder(run).repair_attempts == 0
        assert _builder(run).check_corrections == 1
        assert run.role_prs[0]["state"] == "merged"
        # Triage read the check's own source, not only its FAIL line.
        assert "the 20th entry is 500" in calls[0]["check_source"]
        assert any("corrected its own check" in e["message"] for e in run.events)
    finally:
        engine.shutdown()


def test_a_check_the_checker_keeps_stops_for_a_person(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    _triage(monkeypatch, [])
    fixture, turns = _checker_writes(ORIGINAL, ORIGINAL)
    engine = Engine(executor_obj=fixture)
    try:
        run = _wait(engine.submit(TASK, TEAM))
        assert run.status == "needs_human"
        assert run.role_prs[0]["error"] == "CHECK_REQUIRES_HUMAN"
        assert turns == ["author", "correct"]
        # Upheld: the original red is the only result, never re-run.
        assert [row["passed"] for row in run.gate_history] == [False]
        assert _builder(run).attempt == 1
    finally:
        engine.shutdown()


def test_a_correction_that_checks_less_does_not_count(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    _triage(monkeypatch, [])
    fixture, _turns = _checker_writes(ORIGINAL, NARROWED)
    engine = Engine(executor_obj=fixture)
    try:
        run = _wait(engine.submit(TASK, TEAM))
        assert run.status == "needs_human"
        assert run.role_prs[0]["error"] == "CHECK_CORRECTION_NARROWED"
        assert [row["check_lines"] for row in run.gate_history] == [3, 1]
        assert run.role_prs[0]["state"] == "blocked"
    finally:
        engine.shutdown()


def test_an_unchanged_repair_of_the_same_red_gets_the_correction(monkeypatch):
    """Teams 3, 6, 7, 51 and 60: triage blamed the builder, the builder changed
    nothing, and the same check failed the same way."""
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    _triage(monkeypatch, ["claude-code"])
    fixture, turns = _checker_writes(ORIGINAL, CORRECTED)
    engine = Engine(executor_obj=fixture)
    try:
        run = _wait(engine.submit(TASK, TEAM))
        assert run.status == "passed", (run.status, run.fail_reason, run.role_prs)
        assert turns == ["author", "correct"]
        assert [row["passed"] for row in run.gate_history] == [False, False, True]
        digests = {row["patch_digest"] for row in run.gate_history}
        assert len(digests) == 1, "the builder's repair left the pull request unchanged"
        assert _builder(run).repair_attempts == 1
        assert _builder(run).check_corrections == 1
    finally:
        engine.shutdown()


def test_after_a_correction_the_builder_still_has_its_one_repair(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    _triage(monkeypatch, [], ["claude-code"])
    fixture, turns = _checker_writes(ORIGINAL, CORRECTED_STRICT)
    engine = Engine(executor_obj=fixture)
    try:
        run = _wait(engine.submit(TASK, TEAM, options={"fail_first_check": True}))
        assert run.status == "passed", (run.status, run.fail_reason, run.role_prs)
        assert turns == ["author", "correct"]
        assert [row["passed"] for row in run.gate_history] == [False, False, True]
        assert _builder(run).attempt == 2
        assert _builder(run).repair_attempts == 1
    finally:
        engine.shutdown()


def test_each_allowance_is_spent_at_most_once(monkeypatch):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    calls = _triage(monkeypatch, [])
    fixture, turns = _checker_writes(ORIGINAL, CORRECTED_STRICT)
    engine = Engine(executor_obj=fixture)
    try:
        run = _wait(engine.submit(TASK, TEAM, options={"fail_first_check": True}))
        assert run.status == "needs_human"
        assert run.role_prs[0]["error"] == "CHECK_REQUIRES_HUMAN"
        assert turns == ["author", "correct"], "never a second correction"
        assert [row["passed"] for row in run.gate_history] == [False, False]
        assert len(calls) == 2
        assert _builder(run).attempt == 1
    finally:
        engine.shutdown()


def test_a_changed_repair_that_fails_on_the_same_lines_gets_the_correction(monkeypatch):
    """Round 6, teams 1 and 34: the builder changed its code and the same check failed
    on exactly the same lines again; the checker looks at its own check once."""
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    _triage(monkeypatch, ["claude-code"])
    fixture, turns = _checker_writes(ORIGINAL, CORRECTED)
    produce = fixture.produce

    def changing_builder(run, agent_id, role):
        path = produce(run, agent_id, role)
        if roles.get(agent_id).kind != roles.CHECKER:
            with open(os.path.join(run.roledir(agent_id), "attempt.txt"), "w") as f:
                f.write(f"attempt {run.iterations}\n")
        return path

    fixture.produce = changing_builder
    engine = Engine(executor_obj=fixture)
    try:
        run = _wait(engine.submit(TASK, TEAM))
        assert run.status == "passed", (run.status, run.fail_reason, run.role_prs)
        assert turns == ["author", "correct"]
        assert [row["passed"] for row in run.gate_history] == [False, False, True]
        first, repaired, _ = (row["patch_digest"] for row in run.gate_history)
        assert first != repaired, "the builder's repair changed the code"
    finally:
        engine.shutdown()
