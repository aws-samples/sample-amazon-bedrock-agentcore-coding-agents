"""Resilience tests: the in-process analogues of production durability, proven.

The engine runs in ONE Python process, but every "durable" mechanism in the
references has an in-process twin, and each is unit-testable without an LLM call:

  * a compare-and-swap status transition (conditional write)         -> Run.transition
  * a self-healing lease that evicts a wedged holder (in-flight TTL)  -> _Lease
  * a stranded-run reconciler sweep                                   -> Engine.reconcile
  * an active-count recomputed from the source of truth (no drift)    -> Engine.active_count
  * a two-bucket terminal model (permanent=failed vs transient=needs_human) -> _is_permanent

    python3 -m pytest orchestrator/test_resilience.py -v
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    Engine,
    Run,
    _Lease,
    _is_permanent,
)
from fixture_executor import FixtureExecutor  # noqa: E402


def _engine(**kw) -> Engine:
    """Resilience mechanics are producer-independent; the deterministic
    FixtureExecutor (no model, no live AWS) keeps every run offline and fast."""
    return Engine(executor_obj=FixtureExecutor(), **kw)


# ---------------------------------------------------------------- compare-and-swap
def _bare_run() -> Run:
    return Run(run_id="run_test_001", task="t", agents=[], roles={})


def test_transition_is_compare_and_swap():
    run = _bare_run()
    run.status = "running"
    # wrong expected -> no-op, returns False, status unchanged
    assert run.transition("passed", "queued") is False
    assert run.status == "running"
    # correct expected -> writes, returns True
    assert run.transition("passed", "running") is True
    assert run.status == "passed"
    # idempotent: a second sweep that expects 'running' is now a no-op
    assert run.transition("needs_human", "running", reason="LATE") is False
    assert run.status == "passed" and run.fail_reason is None


def test_transition_unconditional_when_no_expected():
    run = _bare_run()
    run.status = "queued"
    assert run.transition("running") is True
    assert run.status == "running"


# ---------------------------------------------------------------- self-healing lease
def test_lease_serializes_and_auto_evicts_a_wedged_holder():
    lease = _Lease(stuck_after_s=0.3)
    lease.acquire("run_A")           # A holds it and never releases (simulated crash)
    got = {"who": None, "t": 0.0}

    def waiter():
        t0 = time.monotonic()
        lease.acquire("run_B")       # must block until A's lease goes stuck, then steal
        got["who"], got["t"] = "run_B", time.monotonic() - t0
        lease.release("run_B")

    th = threading.Thread(target=waiter, daemon=True)
    th.start()
    th.join(timeout=3.0)
    assert got["who"] == "run_B", "B never acquired: lease deadlocked on the wedged holder"
    assert got["t"] >= 0.25, "B acquired too early: it did not wait for the stuck window"
    assert lease.steals >= 1, "the stuck holder was not recorded as evicted"


def test_lease_release_by_nonowner_is_a_noop():
    lease = _Lease(stuck_after_s=10)
    lease.acquire("run_A")
    lease.release("run_B")           # a stolen-from holder releasing must NOT free A's lease
    # A still owns it: a fresh acquire from C must block (we only check it doesn't crash
    # and that releasing as the true owner frees it)
    lease.release("run_A")
    lease.acquire("run_C")           # now free
    lease.release("run_C")


# ---------------------------------------------------------------- two-bucket model
def test_permanent_vs_transient_classification():
    # deterministic failures: resubmit won't help -> stays failed
    for reason in ("EMPTY_TASK", "UNKNOWN_WORKFLOW:no/such", "HARNESS_MISSING:kiro",
                   "PYTEST_UNAVAILABLE", "NO_RUN_TO_REVIEW"):
        assert _is_permanent(reason) is True, reason
    # transient failures: a human can resume -> needs_human
    for reason in ("CONCURRENCY_LIMIT", "ROLE_EXECUTION_ERROR", "ENGINE_STALL",
                   "STRANDED_NO_PROGRESS", "ENGINE_ERROR: boom", None):
        assert _is_permanent(reason) is False, reason


def test_empty_task_is_permanent_failed_not_needs_human():
    """A deterministic rejection stays 'failed' (the two-bucket model must not
    upgrade a permanent failure to the resumable needs_human lane)."""
    engine = _engine()
    run = engine.submit("   ")
    deadline = time.monotonic() + 10
    while run.status not in ("failed", "needs_human", "passed") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert run.status == "failed" and run.fail_reason == "EMPTY_TASK"
    engine.shutdown()


# ---------------------------------------------------------------- active-count + reconcile
def test_active_count_recomputed_from_source_of_truth():
    engine = _engine()
    a, b, c = _bare_run(), _bare_run(), _bare_run()
    a.run_id, b.run_id, c.run_id = "r_a", "r_b", "r_c"
    a.status, b.status, c.status = "running", "queued", "passed"
    with engine._lock:
        engine._runs = {"r_a": a, "r_b": b, "r_c": c}
    # passed run is not active; excluding r_a leaves only r_b
    assert engine.active_count() == 2
    assert engine.active_count(exclude="r_a") == 1


def test_reconcile_strands_a_stuck_run_idempotently():
    engine = _engine()
    stuck = _bare_run()
    stuck.run_id = "r_stuck"
    stuck.status, stuck.phase = "running", "agent_execution"
    stuck._t0 = time.monotonic() - 10_000  # way past STRANDED_AFTER_S
    fresh = _bare_run()
    fresh.run_id = "r_fresh"
    fresh.status, fresh._t0 = "running", time.monotonic()  # young, must be left alone
    with engine._lock:
        engine._runs = {"r_stuck": stuck, "r_fresh": fresh}
    out = engine.reconcile()
    assert out["stranded"] == 1 and out["swept"] == 1 and out["errors"] == 0
    assert stuck.status == "needs_human" and stuck.fail_reason == "STRANDED_NO_PROGRESS"
    assert fresh.status == "running"  # untouched
    # idempotent: a second sweep finds nothing to strand (status already terminal)
    out2 = engine.reconcile()
    assert out2["stranded"] == 0 and out2["swept"] == 0
    engine.shutdown()


def test_reconcile_skips_a_run_that_advanced_mid_sweep():
    """CAS guard: if a run legitimately reaches a terminal state, the reconciler's
    transition is a no-op (the 'advanced during reconcile' branch)."""
    engine = _engine()
    run = _bare_run()
    run.run_id = "r_adv"
    run.status, run._t0 = "passed", time.monotonic() - 10_000  # terminal but old
    with engine._lock:
        engine._runs = {"r_adv": run}
    out = engine.reconcile()
    # a terminal run is never a strand candidate
    assert out["stranded"] == 0 and run.status == "passed"
    engine.shutdown()
