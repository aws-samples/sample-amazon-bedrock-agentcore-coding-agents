"""Replay-server pool: passed runs may NOT leak MCP server subprocesses.

A passed run keeps its built MCP server alive so the produced UI can be replayed
against the live endpoint. An MCP server process never exits on its own, so left
unbounded every passing run orphans a python child; over a workshop / capture
session they piled into the thousands and exhausted the box (the recurring
"console blocks and dies", traced to ~1.6k orphaned mcp_server.py). The fix is a
BOUNDED replay pool: at most _MAX_REPLAY_SERVERS survive (oldest evicted first),
idle ones past the TTL are reaped, _stop_server kills for good (terminate -> wait
-> kill), and atexit reaps the lot.

These tests prove the pool in isolation (a fake Popen, no real server) AND end to
end (real FixtureExecutor convert runs, real subprocesses, asserting the live
child count stays bounded no matter how many passing runs we drive).

    python3 -m pytest orchestrator/test_replay_pool.py -v
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine as _engine_mod  # noqa: E402
from engine import (  # noqa: E402
    TERMINAL,
    Engine,
    _kill_proc,
    _orphan_pids_to_reap,
    _reap_orphaned_servers,
    _ReplayPool,
)
from fixture_executor import FixtureExecutor  # noqa: E402

ALL_AGENTS = ["claude-code", "claude-code-validator", "opencode"]


# --- a fake child proc: looks like Popen, records terminate/kill, no real OS proc
class _FakeProc:
    def __init__(self, *, stubborn: bool = False, dead: bool = False):
        self._alive = not dead
        self._stubborn = stubborn  # ignores terminate(); only kill() stops it
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated += 1
        if not self._stubborn:
            self._alive = False

    def kill(self):
        self.killed += 1
        self._alive = False

    def wait(self, timeout=None):
        if self._alive:
            # stubborn proc: terminate() didn't stop it, so the wait after
            # terminate times out (mirrors a real SIGTERM-ignoring server).
            raise __import__("subprocess").TimeoutExpired("fake", timeout)
        return 0


def _wait_terminal(run, timeout_s: float = 60.0):
    deadline = time.monotonic() + timeout_s
    while run.status not in TERMINAL:
        assert time.monotonic() < deadline, f"run stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return run


# --------------------------------------------------------------- unit: the pool
def test_kill_proc_escalates_to_kill_for_a_stubborn_child():
    p = _FakeProc(stubborn=True)
    _kill_proc(p)
    assert p.terminated == 1 and p.killed == 1  # SIGTERM ignored -> SIGKILL
    assert p.poll() is not None


def test_kill_proc_is_a_noop_on_a_dead_child():
    p = _FakeProc(dead=True)
    _kill_proc(p)
    assert p.terminated == 0 and p.killed == 0


def test_pool_caps_live_servers_and_evicts_oldest():
    pool = _ReplayPool()
    procs = [_FakeProc() for _ in range(10)]
    # Register more than the cap; register() evicts oldest beyond _MAX_REPLAY_SERVERS.
    cap = _engine_mod._MAX_REPLAY_SERVERS
    for i, p in enumerate(procs):
        pool.register(f"run_{i:03d}", p)
    assert pool.count() == cap
    # The oldest (first registered) were evicted and actually killed.
    evicted = procs[: len(procs) - cap]
    survivors = procs[len(procs) - cap :]
    assert all(p.poll() is not None for p in evicted), "evicted servers must be reaped"
    assert all(p.poll() is None for p in survivors), "survivors must stay alive"


def test_pool_register_same_run_twice_reaps_the_old_proc():
    pool = _ReplayPool()
    first, second = _FakeProc(), _FakeProc()
    pool.register("run_x", first)
    pool.register("run_x", second)  # same run id -> old proc replaced + killed
    assert pool.count() == 1
    assert first.poll() is not None  # superseded proc reaped
    assert second.poll() is None


def test_pool_reap_idle_kills_only_aged_entries():
    pool = _ReplayPool()
    fresh, stale = _FakeProc(), _FakeProc()
    pool.register("fresh", fresh)
    pool.register("stale", stale)
    # ttl=0 reaps everything currently registered (all are "idle past 0s").
    n = pool.reap_idle(ttl=0)
    assert n == 2
    assert fresh.poll() is not None and stale.poll() is not None
    assert pool.count() == 0


def test_pool_drop_is_idempotent():
    pool = _ReplayPool()
    p = _FakeProc()
    pool.register("run_y", p)
    pool.drop("run_y")
    assert p.poll() is not None and pool.count() == 0
    pool.drop("run_y")          # dropping an unknown run is a no-op
    pool.drop("never_seen")
    assert pool.count() == 0


def test_pool_reap_all_clears_everything():
    pool = _ReplayPool()
    procs = [_FakeProc() for _ in range(3)]
    for i, p in enumerate(procs):
        pool.register(f"r{i}", p)
    pool.reap_all()
    assert pool.count() == 0
    assert all(p.poll() is not None for p in procs)


# ------------------------------------------------ e2e: real engine, real procs
def _live_replay_children() -> int:
    """How many real replay servers the GLOBAL engine pool is holding alive."""
    return _engine_mod._REPLAY.count()


@pytest.fixture(autouse=True)
def _isolate_replay(monkeypatch, tmp_path):
    """Each e2e test starts with an empty global replay pool and a tmp runs dir, so
    a leftover server from another test can't skew the live count, and reaps the
    pool afterward so no real child outlives the test."""
    _engine_mod._REPLAY.reap_all()
    monkeypatch.setenv("WORKSHOP_RUNS_DIR", str(tmp_path / "runs"))
    yield
    _engine_mod._REPLAY.reap_all()


def test_many_passing_converts_do_not_leak_servers(monkeypatch):
    """Drive far more passing convert runs than the cap; the live replay-server
    count must stay <= the cap the whole time (the anti-orphan guarantee). Before
    the fix this number equaled the run count and grew without bound."""
    cap = _engine_mod._MAX_REPLAY_SERVERS
    engine = Engine(executor_obj=FixtureExecutor())
    n_runs = cap + 5
    for i in range(n_runs):
        run = _wait_terminal(
            engine.submit("Convert the module to an MCP server", ALL_AGENTS))
        assert run.status == "passed", f"run {i} did not pass: {run.fail_reason}"
        # Invariant after EVERY passing run, not just at the end.
        assert _live_replay_children() <= cap, (
            f"replay pool exceeded cap after run {i}: "
            f"{_live_replay_children()} > {cap}")
    # We drove cap+5 passing runs; the pool is capped, not run-count-sized.
    assert _live_replay_children() <= cap


def test_shutdown_reaps_every_replay_server(monkeypatch):
    """engine.shutdown() stops every run's server for good and empties the pool, so
    a torn-down engine (test cleanup, process exit) never leaves an orphan. Drive a
    passing run (parks a server), then shut down: the pool is empty and the child
    is dead."""
    engine = Engine(executor_obj=FixtureExecutor())
    run = _wait_terminal(
        engine.submit("Convert the module to an MCP server", ALL_AGENTS))
    assert run.status == "passed"
    proc = run._server_proc  # the parked replay server (a real subprocess)
    engine.shutdown()
    assert _live_replay_children() == 0
    assert proc is None or proc.poll() is not None  # the real child is reaped


def test_reconcile_reaps_idle_replay_servers(monkeypatch):
    """The 60s reconcile cadence also sweeps idle replay servers. Force the TTL to
    0 so a single reconcile() reaps every parked server."""
    monkeypatch.setattr(_engine_mod, "_REPLAY_TTL_S", 0.0)
    engine = Engine(executor_obj=FixtureExecutor())
    run = _wait_terminal(
        engine.submit("Convert the module to an MCP server", ALL_AGENTS))
    assert run.status == "passed"
    # A passed run parked its server (TTL is 0 but reap only runs on reconcile/next
    # pass; the post-pass reap_idle may already have cleared it, so accept either).
    engine.reconcile()
    assert _live_replay_children() == 0


# ----------------------------------------- boot-time orphan sweep (hard-kill case)
# atexit reaps a CLEAN exit, but a SIGKILL'd host (a `kill -9` on the console, an
# e2e teardown that times out) runs no cleanup and orphans its mcp_server.py
# children to init. The boot sweep is the backstop: on engine import it kills any
# mcp_server.py under the runs dir whose PARENT IS DEAD (ppid==1). The decision
# half (_orphan_pids_to_reap) is pure, so the exact predicate is pinned here
# against synthetic ps output (deterministic, no reliance on OS re-parenting).
_RUNS = "/work/.runs"


def _ps(*rows) -> str:
    # rows are (pid, ppid, command) tuples, rendered like `ps -eo pid=,ppid=,command=`
    return "\n".join(f"{pid} {ppid} {cmd}" for pid, ppid, cmd in rows)


def test_sweep_selects_only_orphaned_runs_dir_servers():
    out = _ps(
        # orphan (ppid 1) under the runs dir -> REAP
        (101, 1, f"python3 {_RUNS}/work/run_a/role-claude-code/mcp_server.py --port 5"),
        # live parent (ppid != 1) under the runs dir -> KEEP (a running console's child)
        (102, 9999, f"python3 {_RUNS}/work/run_b/role-claude-code/mcp_server.py --port 6"),
        # orphan but NOT under the runs dir (some other repo's server) -> KEEP (out of scope)
        (103, 1, "python3 /elsewhere/mcp_server.py --port 7"),
        # orphan under the runs dir but a DIFFERENT program -> KEEP (not an mcp_server)
        (104, 1, f"python3 {_RUNS}/work/run_c/other.py"),
    )
    assert _orphan_pids_to_reap(out, _RUNS) == [101]


def test_sweep_handles_empty_and_malformed_ps_lines():
    out = "\n".join(["", "   ", "123", "456 1", f"789 1 python3 {_RUNS}/work/r/mcp_server.py"])
    assert _orphan_pids_to_reap(out, _RUNS) == [789]


def test_boot_sweep_preserves_a_live_parent_server(tmp_path, monkeypatch):
    """End-to-end safety: a REAL server with a LIVE parent (a child of this test
    process, ppid != 1, like a running console's child) must survive the actual
    sweep. The orphan-kill half is covered deterministically above; this proves the
    sweep never kills a process whose parent is still alive."""
    import subprocess
    runs = tmp_path / "runs"
    monkeypatch.setattr(_engine_mod, "_RUNS_DIR", str(runs))
    path = runs / "work" / "run_live" / "role-claude-code" / "mcp_server.py"
    os.makedirs(path.parent, exist_ok=True)
    path.write_text("import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(path), "--port", "0"],   # child of THIS process: live parent
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        assert proc.poll() is None
        _reap_orphaned_servers()
        time.sleep(0.5)
        assert proc.poll() is None, "boot sweep must NOT kill a live-parent server"
    finally:
        _kill_proc(proc)
