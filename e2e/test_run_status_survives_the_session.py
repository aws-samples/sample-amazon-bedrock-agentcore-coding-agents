"""A verdict must outlive the session that submitted the run.

`run_status` read only the coordinator process's in-memory registry, so a run was
answerable ONLY from the session that started it. The workshop page had to warn
attendees about it: "if the session expires, inspect the PR or submit a new run".
That is a real dead end on a run that already finished and already opened a pull
request, and it is the failure an attendee is most likely to hit, because the
interesting question ("did it pass?") is asked minutes later.

Borrowed from AI-DLC Workflows, whose whole workflow state lives in files rather
than in a conversation, so a session can be resumed, replayed, or read by someone
else entirely. Same idea, much smaller surface: persist the answer `run_status`
would have given, and read it back by run id alone.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))


def _wait(run, timeout_s: float = 90.0):
    deadline = time.monotonic() + timeout_s
    while run.status not in ("passed", "failed", "needs_human"):
        assert time.monotonic() < deadline, f"stuck in {run.status}"
        time.sleep(0.2)
    return run


def test_a_finished_run_is_readable_from_a_different_engine():
    """The case that mattered: a NEW session asking about an older run."""
    engine = importlib.import_module("engine")
    run_store = importlib.import_module("run_store")
    fixture = importlib.import_module("fixture_executor")

    eng = engine.Engine(executor_obj=fixture.FixtureExecutor())
    try:
        run = _wait(eng.submit("build a small service",
                               ["claude-code", "claude-code-validator"]))
        assert run.status == "passed", run.fail_reason
        run_id = run.run_id
    finally:
        eng.shutdown()

    # A DIFFERENT engine instance: the previous session is gone, and with it the
    # in-memory registry that used to be the only record.
    fresh = engine.Engine(executor_obj=fixture.FixtureExecutor())
    try:
        assert fresh.get(run_id) is None, "precondition: not live in the new engine"
        saved = run_store.load(engine._RUNS_DIR, run_id)
        assert saved is not None, (
            "the finished run left no persisted state, so a new session cannot "
            "answer for it and the attendee has no way back to their result")
        assert saved["run_id"] == run_id
        assert saved["status"] == "passed"
        # The gate verdict and the PR field are what an attendee actually asks for.
        assert saved["gate"]["passed"] is True
        assert "pr_url" in saved
    finally:
        fresh.shutdown()


def test_the_chat_tool_answers_from_the_persisted_record():
    """`run_status` itself, not just the store, must use it."""
    engine = importlib.import_module("engine")
    chat = importlib.import_module("chat")
    fixture = importlib.import_module("fixture_executor")

    eng = engine.Engine(executor_obj=fixture.FixtureExecutor())
    try:
        run = _wait(eng.submit("build a small service",
                               ["claude-code", "claude-code-validator"]))
        run_id = run.run_id
    finally:
        eng.shutdown()

    # Point the chat tools at an engine that never saw this run.
    chat.use_engine(engine.Engine(executor_obj=fixture.FixtureExecutor()))
    try:
        tools = {getattr(t, "tool_name", None) or getattr(t, "__name__", ""): t
                 for t in chat.build_tools()}
        status = tools["run_status"]
        fn = getattr(status, "original_function", None) or status
        out = json.loads(fn(run_id))
        assert "error" not in out, out
        assert out["status"] == "passed" and out["source"] == "persisted", out

        listing = tools["list_runs"]
        lfn = getattr(listing, "original_function", None) or listing
        rows = json.loads(lfn())["runs"]
        assert any(r["run_id"] == run_id for r in rows), rows
    finally:
        chat.use_engine(chat.ENGINE)


def test_an_unknown_run_still_fails_loud_and_says_what_to_do():
    """Persistence must not turn a genuinely unknown id into a fake answer."""
    chat = importlib.import_module("chat")
    tools = {getattr(t, "tool_name", None) or getattr(t, "__name__", ""): t
             for t in chat.build_tools()}
    fn = tools["run_status"]
    fn = getattr(fn, "original_function", None) or fn
    out = json.loads(fn("run_000000_999"))
    assert out["error"].startswith("UNKNOWN_RUN"), out
    assert out["hint"], "the error must say what to do next"


def test_a_missing_bucket_never_breaks_a_run():
    """Durability is best effort: an unreachable mirror is not a run failure."""
    run_store = importlib.import_module("run_store")
    tmp = tempfile.mkdtemp()
    prior = os.environ.get("WORKSHOP_RUNTIME_BUCKET")
    os.environ["WORKSHOP_RUNTIME_BUCKET"] = "no-such-bucket-for-this-test-000"
    logged: list[str] = []
    try:
        run_store.save(tmp, "run_000000_998", {"run_id": "run_000000_998",
                                               "status": "passed"},
                       log=lambda m, level="info": logged.append(m))
        # The LOCAL copy still landed, which is what the box and console read.
        assert run_store.load(tmp, "run_000000_998")["status"] == "passed"
    finally:
        if prior is None:
            os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
        else:
            os.environ["WORKSHOP_RUNTIME_BUCKET"] = prior
        shutil.rmtree(tmp, ignore_errors=True)
