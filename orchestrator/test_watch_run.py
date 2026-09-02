"""A build must be watchable while it runs, including from a DEPLOYED coordinator.

The console renders the per-role feed in-process and `watch_agents.py` attaches to its
multiplexed Runtime PTYs, but the served Lab 2 path is a coordinator inside its own
AgentCore Runtime. Before this, the only window into such a run was a chat turn per
poll -- about a minute of model time per line of state. The engine was already recording
the right thing (`Run.role_events`); it simply never reached the durable record.

These tests pin the two halves: the snapshot carries a bounded activity feed, and the
watcher renders it without invoking anything.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import run_store  # noqa: E402
import watch_run  # noqa: E402


class _FakeRun:
    """Only what _persistable_activity touches."""
    def __init__(self, feeds):
        import threading
        self._lock = threading.Lock()
        self.role_events = feeds


# ------------------------------------------------------------------ the feed persists

def test_the_snapshot_carries_what_each_role_is_doing():
    run = _FakeRun({"claude-code": [
        {"kind": "thinking", "text": "the request asks for persistence, so SQLite"},
        {"kind": "tool_use", "name": "Write", "input": "src/storage/database.js"},
        {"kind": "tool_result", "result": "wrote 41 lines"},
    ]})
    activity = engine._persistable_activity(run)
    assert list(activity) == ["claude-code"]
    kinds = [e["kind"] for e in activity["claude-code"]]
    assert kinds == ["thinking", "tool_use", "tool_result"], \
        "the KIND is what makes the feed legible as work rather than log spam"
    assert activity["claude-code"][1]["name"] == "Write"


def test_the_feed_is_bounded_so_a_heartbeat_stays_small():
    # A chatty role: more events than the cap, each longer than the cap.
    flood = [{"kind": "text", "text": "x" * 5000} for _ in range(500)]
    activity = engine._persistable_activity(_FakeRun({"kiro": flood}))
    assert len(activity["kiro"]) == engine._ACTIVITY_EVENTS_PER_ROLE
    assert all(len(e["text"]) <= engine._ACTIVITY_TEXT_CAP for e in activity["kiro"])
    # The snapshot is rewritten on every heartbeat, so the whole feed must stay tiny.
    assert len(json.dumps(activity)) < 8000


def test_it_keeps_the_NEWEST_events():
    feed = [{"kind": "text", "text": f"step {i}"} for i in range(40)]
    activity = engine._persistable_activity(_FakeRun({"opencode": feed}))
    assert activity["opencode"][-1]["text"] == "step 39", \
        "watching means seeing what is happening NOW, not how the run opened"


def test_a_role_with_no_events_is_omitted_rather_than_empty():
    assert engine._persistable_activity(_FakeRun({"kiro": []})) == {}


# ------------------------------------------------------------------- the watcher reads

def _persist(tmp, payload):
    run_store.save(tmp, payload["run_id"], payload, lambda *_a, **_k: None)


def test_the_watcher_renders_a_live_run_without_invoking_anything():
    with tempfile.TemporaryDirectory() as tmp:
        _persist(tmp, {
            "run_id": "run_015138_e9cfbe87b3a8",
            "status": "running", "phase": "gate", "iterations": 2,
            "task": "Build an HTTP API for a personal library",
            "progress": [
                {"agent": "claude-code", "role": "backend", "state": "done",
                 "note": "prepared 18 change(s)"},
                {"agent": "kiro", "role": "validator", "state": "running", "note": ""},
            ],
            "activity": {"kiro": [
                {"kind": "tool_use", "name": "Write", "text": "acceptance_check"},
                {"kind": "text", "text": "141 checks run, 1 failed"},
            ]},
            "role_prs": [{"number": 8, "role": "backend", "state": "OPEN",
                          "url": "https://github.com/o/r/pull/8"}],
            "gate_history": [{"work_id": "work_claude-code_2a27", "round": 1,
                              "passed": False, "summary": "VERDICT: REJECT"}],
        })
        out, rc = _watch_once(tmp, "run_015138_e9cfbe87b3a8")
        assert rc == 0
        for expected in ("run_015138_e9cfbe87b3a8", "claude-code", "kiro",
                         "acceptance_check", "141 checks run, 1 failed", "#8",
                         "FAIL"):
            assert expected in out, f"the frame must show {expected!r}\n{out}"


def _watch_once(runs_dir: str, run_id: str) -> tuple[str, int]:
    """One --once --plain frame, captured."""
    watch_run._RUNS_DIR = runs_dir
    sys.argv = ["watch_run.py", run_id, "--once", "--plain"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = watch_run.main()
    return buf.getvalue(), rc


def test_a_run_with_no_record_yet_says_so_instead_of_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        out, rc = _watch_once(tmp, "run_does_not_exist")
        assert rc == 1
        assert "no durable record" in out


def test_the_watcher_never_reaches_for_a_model_or_a_verdict():
    """Same rule as replay.py: this is reporting, never the verdict path."""
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "watch_run.py")).read()
    for forbidden in ("import llm", "import reviewer", "from llm", "from reviewer",
                      "boto3.client('bedrock", "invoke_agent_runtime"):
        assert forbidden not in source, \
            f"watch_run.py must not {forbidden!r}: watching may not influence a run"
