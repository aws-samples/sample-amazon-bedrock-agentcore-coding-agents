"""The liveness pulse must outlive one failed beat.

`run_status` reads the persisted snapshot whenever the run is not live in THIS
coordinator's memory, and `run_store.active_snapshot_is_stale` turns a snapshot older
than two minutes into `COORDINATOR_SESSION_INTERRUPTED`, whose `next_action` tells the
attendee to submit the same request again. So the heartbeat is not decoration: while a
build is genuinely running, a stopped pulse manufactures a false terminal verdict AND
invites a duplicate 60-minute three-role build. Found on a live run (2026-09-03) whose
snapshot stopped 24 minutes before the run finished.

Both halves are on the REPORTING path, so neither can change a gate result.
"""
import os
import sys
import tempfile
import threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                + "/orchestrator")

import run_store  # noqa: E402


class Unencodable:
    """Anything json cannot encode."""


def test_save_honours_its_never_raises_contract():
    warnings = []
    with tempfile.TemporaryDirectory() as d:
        # json.dumps used to run ABOVE the first try, so this raised TypeError out of
        # a function documented never to raise, straight into the heartbeat thread.
        run_store.save(d, "run_x", {"status": "running", "x": Unencodable()},
                       log=lambda m, *a: warnings.append(m))
        saved = run_store.load(d, "run_x")
    # Degraded, not lost: default=str keeps the pulse and the rest of the state.
    assert saved is not None, "a snapshot with one odd value must still be written"
    assert saved["status"] == "running"
    assert isinstance(saved["x"], str)
    assert saved["_saved_at"]


def test_save_still_writes_a_snapshot_the_staleness_rule_accepts():
    with tempfile.TemporaryDirectory() as d:
        run_store.save(d, "run_y", {"status": "running", "x": Unencodable()})
        saved = run_store.load(d, "run_y")
    assert not run_store.active_snapshot_is_stale(saved), (
        "a written snapshot must not read as an interrupted coordinator")


def test_heartbeat_loop_survives_a_failing_beat():
    """The loop keeps beating after one beat raises."""
    import engine  # noqa: PLC0415

    class Run:
        status = "running"
        def __init__(self): self.logged = []
        def log(self, message, level="info"): self.logged.append((level, message))

    run = Run()
    calls = []

    beats = threading.Event()

    def flaky(_run):
        calls.append(1)
        if len(calls) == 1:
            raise TypeError("one bad beat")
        beats.set()

    eng = object.__new__(engine.Engine)
    eng._persist_run = flaky
    original = engine._PERSIST_HEARTBEAT_S
    engine._PERSIST_HEARTBEAT_S = 1.0
    try:
        t = threading.Thread(target=engine.Engine._heartbeat, args=(eng, run),
                             daemon=True)
        t.start()
        assert beats.wait(timeout=15), (
            "the pulse died on the first failed beat; a live run then reads as "
            "COORDINATOR_SESSION_INTERRUPTED and the attendee is told to resubmit")
    finally:
        engine._PERSIST_HEARTBEAT_S = original
        run.status = "passed"
    assert len(calls) >= 2
    assert any("heartbeat skipped a beat" in m for _, m in run.logged)


def test_the_reporting_path_stays_off_the_verdict_path():
    """run_store may not reach for a model or the reviewer."""
    src = open(os.path.join(os.path.dirname(run_store.__file__),
                            "run_store.py"), encoding="utf-8").read()
    for forbidden in ("import llm", "import reviewer", "from llm", "from reviewer"):
        assert forbidden not in src, f"run_store must not {forbidden}"
