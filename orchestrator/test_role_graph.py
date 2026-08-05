"""The agent-execution graph: the SCHEDULE is the framework's, the VERDICT never is.

The loop is allowed to be non-deterministic. The verdict is not. So these tests pin the
scheduling guarantees the engine depends on, and nothing about what a role produces:

  * builders run in parallel, the checker runs AFTER all of them (an explicit AND,
    because Strands fires a node when ANY incoming edge is satisfied),
  * a FAILED builder still releases the join, because a hang reports no verdict at all
    while a red gate reports a real one,
  * the roster shape is the registry's, so a two-role team or a checker-only review
    route schedules correctly with no special case.

The old hand-rolled version of this join DEADLOCKED in exactly the case test
``test_a_failed_builder_never_hangs_the_checker`` covers, which is why it is here.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import role_graph  # noqa: E402
import roles  # noqa: E402


def _recorder():
    """A dispatch factory that records the ORDER roles ran in, thread-safely."""
    order: list[str] = []
    lock = threading.Lock()

    def factory(agent_id: str, boom: bool = False, delay: float = 0.0):
        def run() -> None:
            if delay:
                import time
                time.sleep(delay)
            with lock:
                order.append(agent_id)
            if boom:
                raise RuntimeError(f"{agent_id} failed on purpose")
        return run
    return order, factory


def _run(agent_ids, dispatch_for):
    graph, nodes = role_graph.build_graph(list(agent_ids), dispatch_for)
    return role_graph.run_graph(graph), nodes


def test_checker_waits_for_every_builder_including_the_slowest():
    """The AND join. Strands starts a node as soon as ONE incoming edge is satisfied,
    so without the explicit condition the checker would begin after the FASTEST builder
    and grade a half-written tree."""
    order, factory = _recorder()
    # The frontend is deliberately much slower than the backend.
    delays = {"claude-code": 0.02, "opencode": 0.35, "kiro": 0.0}
    _run(delays, lambda a: factory(a, delay=delays[a]))
    assert order[-1] == "kiro", order
    assert set(order) == set(delays), order


def test_a_failed_builder_never_hangs_the_checker():
    """The regression that matters: a builder that RAISES must still release the join.
    One role's exception becoming a whole-run hang is strictly worse than a red gate,
    because the gate can still report a real verdict on whatever was written."""
    order, factory = _recorder()
    spec = {"claude-code": True, "opencode": False, "kiro": False}
    _, nodes = _run(spec, lambda a: factory(a, boom=spec[a]))
    assert "kiro" in order, "the checker must still run"
    assert order[-1] == "kiro", order
    # The failure is recorded rather than swallowed, so the engine can report it.
    assert nodes["claude-code"].error is not None
    assert nodes["opencode"].error is None


def test_all_builders_failing_still_reaches_the_checker():
    """Even a total builder failure gets a verdict: the checker looks at the (empty or
    partial) tree and the gate decides. Nothing is fabricated and nothing hangs."""
    order, factory = _recorder()
    spec = {"claude-code": True, "opencode": True, "kiro": False}
    _, nodes = _run(spec, lambda a: factory(a, boom=spec[a]))
    assert order[-1] == "kiro", order
    assert all(nodes[b].error is not None for b in ("claude-code", "opencode"))


def test_two_role_roster_schedules_without_a_special_case():
    """A smaller team (one builder + the checker) is a supported roster, so it must
    schedule from the registry with no branch of its own."""
    order, factory = _recorder()
    _run(["claude-code", "kiro"], lambda a: factory(a))
    assert order == ["claude-code", "kiro"]


def test_checker_only_route_runs_with_no_builders_to_wait_for():
    """The review-only route routes just the checker. An AND over an EMPTY builder set
    must be immediately true, not a stall."""
    order, factory = _recorder()
    _run(["kiro"], lambda a: factory(a))
    assert order == ["kiro"]


def test_builders_actually_run_concurrently():
    """Builders are one parallel entry batch, not a sequence: two roles that each sleep
    must finish in about the time of ONE of them. This is the property the thread pool
    used to provide, and losing it would silently double every run's wall clock."""
    import time
    order, factory = _recorder()
    delay = 0.4
    spec = ["claude-code", "opencode", "kiro"]
    t0 = time.monotonic()
    _run(spec, lambda a: factory(
        a, delay=(delay if a != "kiro" else 0.0)))
    elapsed = time.monotonic() - t0
    assert len(order) == 3
    assert elapsed < delay * 1.8, (
        f"builders took {elapsed:.2f}s for 2x{delay}s of work: they serialized")


def test_an_empty_route_fails_loud():
    """No roles is not an empty schedule, it is a routing bug. Raise rather than build
    a graph that would produce an ungated deliverable."""
    with pytest.raises(role_graph.RoleNodeError):
        role_graph.build_graph([], lambda a: (lambda: None))


def test_the_graph_reads_the_roster_from_the_registry(monkeypatch):
    """Which ids are builders and which check is the registry's answer, so a swapped
    checker needs no change in the graph. Restore the Claude Code validator as the
    checker and the ordering must follow it."""
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,claude-code-validator")
    assert roles.checker_ids() == ("claude-code-validator",)
    order, factory = _recorder()
    _run(["claude-code", "claude-code-validator"], lambda a: factory(a))
    assert order == ["claude-code", "claude-code-validator"], order
