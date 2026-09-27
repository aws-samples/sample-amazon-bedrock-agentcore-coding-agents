"""What a participant is TOLD about builds that end the way real ones do.

Each case drives the real engine with the offline fixture executor. Only three seams
change, the way an actual event changes them: publishing returns a real-looking PR,
a check can be red for one pull request, and the review can abstain. Before these
fixes, a checker that failed after the builder's PR opened, a GitHub write refusal,
and a repair that hit the model quota were all reported as "submit the SAME request
again", which duplicates a build whose pull request already exists.
"""
from __future__ import annotations

import os
import time

import pytest

import engine
import reviewer
import runtime_exec
from engine import Engine, TERMINAL, public_result
from fixture_executor import FixtureExecutor

PR = "https://github.com/team/game/pull/{}"


@pytest.fixture
def realistic(monkeypatch):
    numbers = iter(range(1, 100))
    posted: list[str] = []

    def publish(self, run):
        active = run._active_builders if run._active_builders is not None else {
            i.agent for i in run.work_items.values() if i.kind == "builder"}
        for item in run.work_items.values():
            if item.kind != "builder" or item.agent not in active:
                continue
            if run.progress[item.agent].state == "error":
                continue
            item.state, item.stale = "in_review", False
            if not item.pr:
                n = next(numbers)
                item.pr = {"pr_url": PR.format(n), "number": n,
                           "base": item.base_branch, "head": item.branch}

    red, unavailable = set(), set()
    real_gate = reviewer.run_gate

    def gate(check_path, work_dir, task, endpoint):
        result = real_gate(check_path, work_dir, task, endpoint)
        if any(os.path.basename(work_dir).startswith(f"work_{a}_") for a in red):
            result = {**result, "passed": False, "summary": "RESULT: 3 of 41 checks failed",
                      "output": "FAIL score not saved\nRESULT: 3 of 41 checks failed"}
        return result

    real_assess = reviewer.assess

    def judge(run, gate_result, subject):
        if getattr(subject, "agent", "") in unavailable:
            record = reviewer._review_record(reviewer.INTEGRATED_REVIEW_MODEL,
                                             note="ThrottlingException")
            return reviewer._combine_review(gate_result, record, None)
        return None

    monkeypatch.setattr(Engine, "_publish_active_work_items", publish)
    monkeypatch.setattr(Engine, "_comment_work_item",
                        lambda self, run, item, body: posted.append(body))
    monkeypatch.setattr(engine.reviewer, "run_gate", gate)
    monkeypatch.setattr(engine.reviewer, "assess",
                        lambda run, g, n, judge_=None, subject=None, **kw:
                        real_assess(run, g, n, judge=judge, subject=subject))
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "human_review")

    def run(agents, task="Use preset=game-from-scratch. Creative direction: a lantern garden"):
        e = Engine(executor_obj=FixtureExecutor())
        try:
            r = e.submit(task, agents)
            deadline = time.time() + 90
            while r.status not in TERMINAL and time.time() < deadline:
                time.sleep(0.1)
            time.sleep(0.2)
            return public_result(r)
        finally:
            e.shutdown()

    run.red, run.unavailable, run.posted = red, unavailable, posted
    return run


def _fail_agent(monkeypatch, agent, error, *, from_round=1):
    real = FixtureExecutor.produce

    def produce(self, run, agent_id, role):
        if agent_id == agent and run.iterations >= from_round:
            raise error
        return real(self, run, agent_id, role)
    monkeypatch.setattr(FixtureExecutor, "produce", produce)


def test_a_checker_failure_after_the_pr_opened_is_never_resubmitted(realistic, monkeypatch):
    """A bad Kiro key (or the seat cap) fails the checker after the builder's PR is
    open. That PR is real; resubmitting duplicates the build."""
    _fail_agent(monkeypatch, "kiro", RuntimeError(
        "ROLE_EXECUTION_ERROR: kiro-cli exited 1: The bearer token included in the "
        "request is invalid"))
    result = realistic(["claude-code", "kiro"])
    assert result["status"] == "needs_human"
    assert result["resubmission_allowed"] is False
    assert "SAME request again" not in result["next_action"]
    assert "Do not resubmit" in result["next_action"]


def test_a_github_refusal_is_named_and_not_called_transient(realistic, monkeypatch):
    monkeypatch.setattr(Engine, "_publish_active_work_items",
                        lambda self, run: (_ for _ in ()).throw(RuntimeError(
                            "ROLE_PR_PUBLISH_ERROR:w1: Resource not accessible by "
                            "integration (403)")), raising=True)
    result = realistic(["claude-code", "kiro"])
    assert result["fail_reason"] == "ROLE_PR_PUBLISH_ERROR"
    assert result["resubmission_allowed"] is False
    assert "github.py doctor" in result["next_action"]


def test_a_repair_that_hits_the_quota_keeps_that_reason_and_says_so_on_the_pr(
        realistic, monkeypatch):
    realistic.red.add("claude-code")
    _fail_agent(monkeypatch, "claude-code", runtime_exec.ModelQuotaError(
        "MODEL_QUOTA_EXHAUSTED: Too many tokens per day"), from_round=2)
    result = realistic(["claude-code", "kiro"])
    assert result["fail_reason"] == "MODEL_QUOTA_EXHAUSTED"
    assert "Do not resubmit now" in result["next_action"]
    assert [row["error"] for row in result["role_prs"]] == ["MODEL_QUOTA_EXHAUSTED"]
    assert any(body.startswith("**Repair did not complete** (MODEL_QUOTA_EXHAUSTED)")
               for body in realistic.posted)


def test_a_review_outage_never_hides_a_sibling_pr_that_needs_a_person(realistic):
    realistic.red.add("claude-code")
    realistic.unavailable.add("codex")
    result = realistic(["claude-code", "codex", "kiro"],
                       task="Build an issue tracker with an API and a web page.")
    assert result["fail_reason"].startswith("ROLE_PR_BLOCKED:work_claude-code_")
    assert "do not rebuild the application for a review failure" not in result["next_action"]


def test_a_review_outage_is_headlined_as_no_decision(realistic):
    realistic.unavailable.add("claude-code")
    result = realistic(["claude-code", "kiro"])
    assert result["fail_reason"].startswith("REVIEW_UNAVAILABLE:")
    assert result["review"]["state"] == "unavailable"
    assessments = [b for b in realistic.posted if "**Assessment**" in b]
    assert assessments and "Review unavailable (no decision)" in assessments[-1]
    assert "Request changes" not in assessments[-1]


def test_a_run_that_never_checked_says_not_run(realistic, monkeypatch):
    _fail_agent(monkeypatch, "claude-code", runtime_exec.RoleTurnLimitError(
        "claude-code", 50, 1, "Error: Reached max turns (50)"))
    result = realistic(["claude-code", "kiro"])
    assert result["gate"]["executed"] is False and not result["gate_history"]
