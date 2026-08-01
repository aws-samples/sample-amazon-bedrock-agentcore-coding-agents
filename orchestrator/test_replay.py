"""The PR body must make the loop legible, and must never become part of it.

A reviewer arriving from a GitHub notification can reach neither the engine log nor
the coordinator session, so the pull request is the only place the run's story can
be told. The old body was two lines ("Automated build for: <task>"), which told them
nothing about who built it or what the gate actually asserted.

The second half of this file is the more important half: `replay` sits on the PR-body
path and must stay off the verdict path. It reports the gate's result; it never
decides one.

Idea from awslabs/aidlc-workflows v2's `aidlc-replay`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import replay  # noqa: E402


def _role(agent, role, note="", latency_ms=0, state="done", tokens=0):
    return types.SimpleNamespace(agent=agent, role=role, note=note, state=state,
                                 latency_ms=latency_ms, tokens=tokens, engine="agentcore")


def _run(**over):
    base = dict(
        run_id="run_120000_001",
        task="build an issue tracker with five features and persistence",
        status="passed",
        iterations=1,
        agents=["claude-code", "opencode", "claude-code-validator"],
        progress={
            "claude-code": _role("claude-code", "backend",
                                 "built the backend side of this request (6 files)",
                                 latency_ms=214_000),
            "opencode": _role("opencode", "frontend",
                              "built the interface this request asked for (2 files)",
                              latency_ms=98_000),
            "claude-code-validator": _role("claude-code-validator", "validator",
                                           "authored the acceptance check for this "
                                           "deliverable", latency_ms=61_000),
        },
        gate={"passed": True, "summary": "all 7 probes passed", "checks": []},
        route={"preset": "project-from-scratch", "rule": "keyword"},
        review=None,
        retry_reasons=[],
        events=[],
        pr_url=None,
        _acceptance_test_file=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


# --------------------------------------------------------- it tells the story

def test_the_body_names_every_role_that_ran_and_what_it_did():
    body = replay.narrative(_run())
    for agent, role in (("claude-code", "backend"), ("opencode", "frontend"),
                        ("claude-code-validator", "validator")):
        assert agent in body and role in body, f"{agent}/{role} missing:\n{body}"
    assert "built the backend side" in body


def test_a_role_the_router_did_not_dispatch_is_not_in_the_story():
    """The roster is configurable; the story is what RAN, not what is registered."""
    run = _run(agents=["claude-code", "claude-code-validator"])
    del run.progress["opencode"]
    body = replay.narrative(run)
    assert "opencode" not in body, body


def test_the_request_is_quoted_verbatim():
    """The attendee's words are the spec; paraphrasing them would be the engine
    pretending to understand the request."""
    task = "make a thing that does the thing, with 5 features"
    body = replay.narrative(_run(task=task))
    assert task in body


def test_the_body_shows_what_the_check_actually_asserted():
    """The most interesting artifact in the run, and the least likely to be opened."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "acceptance_check")
    with open(path, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env python3\n"
                "# probe 1: the service starts\n"
                "# probe 2: an invalid transition is refused\n")
    body = replay.narrative(_run(_acceptance_test_file=path))
    assert "an invalid transition is refused" in body, body
    # And it must say WHERE the definition of correct came from, because a reviewer
    # who assumes a fixed test suite ran would be misreading the whole guarantee.
    assert "wrote one self-contained executable check" in body
    assert "reference answer" in body


def test_a_long_check_is_excerpted_not_pasted():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "acceptance_check")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"line {i}" for i in range(200)))
    body = replay.narrative(_run(_acceptance_test_file=path))
    assert "line 5" in body and "line 190" not in body, "the whole file was pasted"
    assert "200 lines" in body, "the excerpt does not say how much was withheld"


def test_a_single_round_run_does_not_narrate_a_loop_that_did_not_happen():
    body = replay.narrative(_run(iterations=1))
    assert "rounds" not in body.lower().split("what happens next")[0], body


def test_a_second_round_explains_itself_with_the_feedback_that_caused_it():
    run = _run(iterations=2,
               retry_reasons=[{"round": 1, "gate_summary": "18 passed, 2 failed",
                               "reasons": ["the delete path is not persisted",
                                           "invalid input returns 500"]}])
    body = replay.narrative(run)
    assert "2 rounds" in body
    assert "the delete path is not persisted" in body
    assert "invalid input returns 500" in body
    assert "18 passed, 2 failed" in body


def test_the_feedback_shown_is_what_CAUSED_the_retry_not_the_final_verdict():
    """Found on a live 2-round run: it printed the APPROVAL notes as "feedback".

    `run.review` holds only the LATEST verdict, so on a run that ended green it
    carries the approving judge's notes. Rendering those under "what came back as
    feedback" states the opposite of what happened, which is worse than saying
    nothing. The engine now records each retry's reasons as it orders the retry.
    """
    run = _run(iterations=2,
               retry_reasons=[{"round": 1, "gate_summary": "2 failed",
                               "reasons": ["THE REAL COMPLAINT"]}],
               # what the run ended with: an approval
               review={"reasons": ["looks great, approving"], "state": "approved"})
    body = replay.narrative(run)
    assert "THE REAL COMPLAINT" in body, body
    assert "looks great" not in body, (
        "the narrative quoted the FINAL approval as the feedback that caused the retry")


def test_the_round_comment_exists_because_a_body_cannot_be_rewritten():
    """New commits on a branch a reviewer already read need a timeline entry."""
    run = _run(iterations=2,
               retry_reasons=[{"round": 1, "gate_summary": "2 failed",
                               "reasons": ["the delete path is not persisted"]}],
               gate={"passed": True, "summary": "all 7 probes passed"})
    note = replay.round_comment(run)
    assert "Round 2" in note
    assert "the delete path is not persisted" in note
    assert "passed" in note


def test_final_pr_reports_the_integrated_review_and_both_lenses():
    run = _run(
        review={
            "state": "approved",
            "panels": [
                {
                    "name": "integrated",
                    "label": "Integrated review",
                    "state": "changes_requested",
                    "model": "model-a",
                    "reasons": ["The restart path drops persisted filters."],
                    "assessment": (
                        "#### Adversarial verification\n\n"
                        "Traced the API status value into the UI.\n\n"
                        "#### Design & integration\n\n"
                        "Persistence ownership is incomplete."
                    ),
                },
            ],
        },
        work_items={},
        integration_brief={"shared_contract": []},
        gate_history=[{
            "stage": "full candidate",
            "candidate_digest": "abc123",
            "passed": True,
            "summary": "all probes passed",
        }],
        merge_queue=[],
    )
    body = replay.integration_narrative(run)
    assert "Integrated Read-only Review" in body
    assert "Adversarial verification" in body
    assert "Design & integration" in body
    assert "The restart path drops persisted filters." in body
    assert (
        "without seeing a builder's conversation or self-review"
        in body
    )


def test_a_retry_with_no_recorded_reasons_says_so_rather_than_implying_none():
    run = _run(iterations=2,
               retry_reasons=[{"round": 1, "gate_summary": "", "reasons": []}])
    body = replay.narrative(run)
    assert "no specific reasons were recorded" in body, body


def test_a_failed_role_note_does_not_shred_the_table():
    """Found by running a real one: on the failure path the note IS the exception.

    `role.note` is set from the raised error, which carries the tail of the agent's
    CLI output -- newlines, pipes and all. Dropped into a markdown table verbatim,
    the first newline ends the row and the reviewer gets a broken table instead of
    the reason the build failed, which is precisely when they need it most.
    """
    note = ("RuntimeError: ROLE_EXECUTION_ERROR: CLI exited 1 without writing "
            "the tree; tail:\nTraceback (most recent call last)\n  File \"x\" | y")
    run = _run(agents=["claude-code"],
               progress={"claude-code": _role("claude-code", "backend", note,
                                              latency_ms=5000, state="error")},
               gate={"passed": False, "summary": "no check to run"},
               status="needs_human")
    body = replay.narrative(run)
    section = body[body.index("| role"):body.index("## What proved")]
    rows = [ln for ln in section.splitlines() if ln.strip().startswith("|")]
    assert len(rows) == 3, f"expected header + separator + ONE row, got:\n{section}"
    # 5 delimiters == exactly 4 cells. A raw pipe from the CLI tail would add one,
    # silently shifting every column after it.
    assert rows[-1].count("|") - rows[-1].count("\\|") == 5, (
        f"row has stray pipes:\n{rows[-1]}")
    # The reason still has to be READABLE, not just well-formed.
    assert "ROLE_EXECUTION_ERROR" in rows[-1]


def test_a_truncated_failure_note_keeps_the_root_cause():
    """Truncating only the front would cut off the one line worth reading.

    A role failure note reads "ROLE_EXECUTION_ERROR: ... tail: <traceback>". The
    front is boilerplate shared by every failure; the END is the actual cause. A
    head-only truncation shows the reviewer the category and drops the diagnosis.
    """
    note = ("RuntimeError: ROLE_EXECUTION_ERROR: CLI exited 1 without writing the "
            "tree; tail:\nTraceback (most recent call last):\n  File \"/x/y.py\", "
            "line 44\n    raise ValueError('THE ROOT CAUSE IS HERE')")
    cell = replay._cell(note)
    assert "ROLE_EXECUTION_ERROR" in cell, cell
    assert "THE ROOT CAUSE IS HERE" in cell, (
        f"the end of the note was truncated away:\n{cell}")


def test_one_file_is_not_reported_as_1_files():
    """The notes are reviewer-facing prose on the PR now, so they read like it.

    Checks the note lines in engine.py themselves. Rendering a note this test built
    would only prove the test can format a string; and a blanket grep of the source
    would also match the engine's internal log lines, which never reach a reviewer.
    """
    import engine  # noqa: PLC0415 (local: keeps this file importable standalone)
    assert engine._files(1) == "1 file"
    assert engine._files(6) == "6 files"
    src = open(engine.__file__, encoding="utf-8").read()
    for phrase in ("prepared the backend role patch",
                   "prepared the frontend role patch"):
        line = next(ln for ln in src.splitlines() if phrase in ln and "role.note" in ln)
        assert "_files(" in line, (
            f"this note goes on the pull request but hardcodes the plural, so a "
            f"single-file deliverable reads '1 files':\n{line.strip()}")


def test_a_run_with_nothing_recorded_still_produces_a_body():
    """Never raise on the PR path: a thin story beats a crashed finalization."""
    bare = types.SimpleNamespace(run_id="run_1", task="", agents=[], progress={},
                                 gate=None, route=None, review=None, events=[],
                                 iterations=0, status="passed", pr_url=None)
    body = replay.narrative(bare)
    assert body.strip(), "produced an empty PR body"
    assert "no task recorded" in body


# ------------------------------------------- it reports; it never participates

def test_the_narrative_never_contradicts_a_red_gate():
    """The one thing this file must never do: describe a failure as a success."""
    body = replay.narrative(_run(
        gate={"passed": False, "summary": "probe 3 failed: state was lost on restart"},
        status="needs_human"))
    assert "FAILED" in body, body
    assert "PASSED" not in body, body
    assert "probe 3 failed" in body


def test_replay_holds_no_opinion_about_the_deliverable():
    """It must not become a second, repo-side grader.

    The gate is the validator's authored check and the assessment is the judge's.
    A module that scored the work here would be exactly the pinned repo-side
    contract this system does not have -- so it may not import the judge, and it may
    not decide approval.
    """
    src = open(replay.__file__, encoding="utf-8").read()
    for forbidden in ("import llm", "import reviewer", "def approve",
                      "LGTM", "lgtm"):
        assert forbidden not in src, (
            f"replay.py references {forbidden!r}: the PR narrative must report the "
            "verdict, never form one")


def test_replay_reads_the_run_and_writes_nothing():
    """Called during finalization, so it must not mutate the run it describes."""
    run = _run()
    before = (run.status, run.iterations, dict(run.gate), list(run.agents))
    replay.narrative(run)
    replay.round_comment(run)
    replay.as_json(run)
    assert (run.status, run.iterations, dict(run.gate), list(run.agents)) == before


def test_a_missing_check_file_is_simply_absent_not_invented():
    """No authored check on disk -> no excerpt, and certainly no claim of one."""
    body = replay.narrative(_run(_acceptance_test_file="/nonexistent/acceptance_check"))
    assert "The check that ran" not in body, body
    # The gate line still tells the truth about the verdict it was handed.
    assert "PASSED" in body


def test_the_engine_uses_the_narrative_as_the_pr_body():
    """A body that is never wired up is documentation, not a feature."""
    engine_src = open(os.path.join(os.path.dirname(replay.__file__), "engine.py"),
                      encoding="utf-8").read()
    assert "replay.integration_narrative(run)" in engine_src, (
        "engine.py does not pass the queue evidence narrative to the final "
        "integration PR")
    assert "Automated build for:" not in engine_src, (
        "the old two-line PR body is still there")
