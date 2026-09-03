"""Every terminal outcome must say what to DO about it.

`needs_human` covers two situations with opposite next steps: validation stayed
blocked on real work (the deliverable needs changing) and a role produced nothing
(transient, just resubmit). The status alone cannot tell them apart, and the raw
`fail_reason` token is not advice. Idea from awslabs/aidlc-workflows v2, whose
stage checkboxes name who is blocking rather than making you decode a state.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402


def test_the_two_needs_human_cases_get_different_advice():
    blocked = engine.next_action("needs_human", "ITERATION_CAP")
    empty = engine.next_action("needs_human", "ROLE_EXECUTION_ERROR")
    assert blocked and empty and blocked != empty, (blocked, empty)
    # Blocked validation sends you to gate and review evidence, not a resubmit.
    assert "gate.summary" in blocked and "review" in blocked, blocked
    # The empty role sends you to a resubmit, and warns off hand-finishing.
    assert "same" in empty.lower() and "do not resubmit" in blocked.lower(), (
        empty, blocked)
    assert engine.resubmission_allowed(
        "needs_human", "ITERATION_CAP") is False
    assert engine.resubmission_allowed(
        "needs_human", "ROLE_EXECUTION_ERROR") is True


def test_iteration_cap_does_not_call_a_green_gate_red():
    """A live run hit the cap on review findings after two green executable gates."""
    action = engine.next_action("needs_human", "ITERATION_CAP")
    assert "check was still red" not in action.lower(), action
    assert "blocking gate or review evidence" in action.lower(), action


def test_daily_model_quota_does_not_recommend_an_immediate_retry():
    action = engine.next_action("needs_human", "MODEL_QUOTA_EXHAUSTED")
    assert "do not resubmit now" in action.lower()
    assert "reset" in action.lower()


def test_a_passing_run_is_advised_from_its_PR_not_its_reason():
    """A passing run has two endings, and only the PR result tells them apart.

    See test_localdev_findings.py: a live run showed this returning "" for the most
    common outcome there is. With nothing attempted there is genuinely nothing to say.
    """
    assert engine.next_action("passed", None, {}, None) == ""
    assert engine.next_action("passed", None, {"pr_url": "u"}, "u")


def test_every_fail_reason_the_engine_sets_has_advice():
    """A reason with no next action is a dead end for the attendee."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "engine.py"), encoding="utf-8").read()
    reasons = set(re.findall(r'fail_reason = "([A-Z_]+)"', src))
    reasons |= set(re.findall(r'fail_reason\s*=\s*f?"([A-Z_]+)', src))
    missing = [r for r in sorted(reasons)
               if not engine.next_action("failed", r)]
    assert not missing, f"terminal reasons with no next action: {missing}"


def test_prefixed_reasons_resolve():
    """Reasons carry a detail suffix (`RUNTIME_NOT_WIRED:opencode`)."""
    assert engine.next_action("failed", "RUNTIME_NOT_WIRED:opencode")
    assert engine.next_action("failed", "UNKNOWN_ROLE:nope")


def test_an_unknown_reason_invents_nothing():
    assert engine.next_action("failed", "SOMETHING_NEW_NOBODY_MAPPED") == ""


def test_the_public_result_carries_it():
    run = engine.Run(run_id="run_000000_700", task="t", agents=[], roles={})
    run.status, run.fail_reason = "needs_human", "ITERATION_CAP"
    payload = engine.public_result(run)
    assert payload["next_action"], payload
    assert payload["resubmission_allowed"] is False


def test_an_interrupted_coordinator_does_not_invite_a_duplicate_build():
    """A recycled coordinator does not un-open the pull requests it already opened.

    Live on 2026-09-03: the backend pull request had passed 65 checks and been
    approved, the frontend one was red, and the payload still said
    `resubmission_allowed: true` with "Submit the SAME request again". Following that
    duplicates a sixty-minute three-role build, which is the resubmission runaway the
    iteration bound exists to prevent.
    """
    reason = "COORDINATOR_SESSION_INTERRUPTED"
    rows = [{"agent": "claude-code", "pr_url": "https://github.com/o/r/pull/24",
             "state": "awaiting_review"},
            {"agent": "opencode", "pr_url": "https://github.com/o/r/pull/25",
             "state": "checking"}]

    action = engine.next_action("needs_human", reason, None, None, rows)
    assert "2 pull request(s)" in action
    assert "do NOT resubmit" in action
    assert "Submit the SAME request again" not in action
    assert not engine.resubmission_allowed("needs_human", reason, rows), (
        "published pull requests make a resubmission a duplicate, not a recovery")


def test_an_interrupted_coordinator_with_nothing_published_is_still_resubmittable():
    """The original case still works: nothing opened, so repeating it recovers."""
    reason = "COORDINATOR_SESSION_INTERRUPTED"
    action = engine.next_action("needs_human", reason, None, None, [])
    assert "Submit the SAME request again" in action
    assert engine.resubmission_allowed("needs_human", reason, [])
    assert engine.resubmission_allowed("needs_human", reason)
