"""The timeout constants must not contradict each other.

Every one of these pins a relationship, not a number, so tuning any single budget stays
allowed while an inconsistent SET fails loudly. They exist because a real inversion
shipped: `STRANDED_AFTER_S = 600` sat against `AGENT_EXECUTION_TIMEOUT_S = 1800`, while
the comment above it claimed to be "wider than the agent_execution budget". The
reconciler therefore force-failed any run still working after 10 minutes -- reachable on
the flagship request, whose measured 3-role two-round build took 819s -- and the attendee
saw `needs_human`/`STRANDED_NO_PROGRESS` on a build that was fine.

Nothing about this is visible from a passing test suite or a green stack: it only bites a
LONG run on the console path, which is what Lab 2's UI page and all of Lab 3 use.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import reviewer  # noqa: E402


def test_the_stranded_sweep_cannot_kill_a_legitimately_running_build():
    """The sweep measures from SUBMIT; the agent budget starts later.

    So the threshold must cover the worst case a run is ALLOWED to take: every
    permitted round of (agent execution + gate), plus the phases outside that budget
    (admission, hydration, pre-flight, compose, PR).
    """
    worst_case_agent_phases = engine.MAX_ITERATIONS * (
        engine.AGENT_EXECUTION_TIMEOUT_S + reviewer.GATE_TIMEOUT_S)
    assert engine.STRANDED_AFTER_S > worst_case_agent_phases, (
        f"STRANDED_AFTER_S={engine.STRANDED_AFTER_S} is not greater than the worst-case "
        f"legitimate run ({worst_case_agent_phases}s = {engine.MAX_ITERATIONS} rounds of "
        f"{engine.AGENT_EXECUTION_TIMEOUT_S}s agent + {reviewer.GATE_TIMEOUT_S}s gate). "
        "The reconciler would force-fail builds that are still working.")


def test_the_sweep_has_slack_for_the_phases_outside_the_agent_budget():
    """Admission, hydration, pre-flight, compose and the PR are all outside it."""
    worst_case_agent_phases = engine.MAX_ITERATIONS * (
        engine.AGENT_EXECUTION_TIMEOUT_S + reviewer.GATE_TIMEOUT_S)
    slack = engine.STRANDED_AFTER_S - worst_case_agent_phases
    assert slack >= 300, (
        f"only {slack}s of slack beyond the agent phases; compose and the PR upload "
        "(one gateway put_file per deliverable file) need room")


def test_a_measured_real_build_is_far_inside_the_threshold():
    """Guards against a future edit that technically satisfies the invariant but
    leaves no practical headroom. 819s is a MEASURED 3-role two-round build."""
    measured_worst_real_build_s = 819
    assert engine.STRANDED_AFTER_S > measured_worst_real_build_s * 3, (
        f"STRANDED_AFTER_S={engine.STRANDED_AFTER_S} leaves little headroom over the "
        f"measured {measured_worst_real_build_s}s build")


def test_the_role_timeout_is_inside_the_agent_phase_timeout():
    """The per-role net must trip BEFORE the outer one, or the outer timeout hides
    which role hung and the failure message names nothing actionable."""
    assert engine.HARNESS_ROLE_TIMEOUT_S <= engine.AGENT_EXECUTION_TIMEOUT_S, (
        f"HARNESS_ROLE_TIMEOUT_S={engine.HARNESS_ROLE_TIMEOUT_S} exceeds "
        f"AGENT_EXECUTION_TIMEOUT_S={engine.AGENT_EXECUTION_TIMEOUT_S}, so the outer "
        "timeout fires first and the run cannot say which role was stuck")


def test_the_compose_lease_cannot_outlive_the_sweep():
    """A wedged compose holder must self-release long before the sweep gives up."""
    assert engine.COMPOSE_LEASE_STUCK_S < engine.STRANDED_AFTER_S


def test_stranded_after_is_derived_not_hardcoded():
    """A literal here is what allowed the inversion: someone tuned the agent budget
    and this number silently stayed behind."""
    src = open(engine.__file__, encoding="utf-8").read()
    line = next(ln for ln in src.splitlines()
                if ln.startswith("STRANDED_AFTER_S ="))
    assert "AGENT_EXECUTION_TIMEOUT_S" in line or "MAX_ITERATIONS" in line, (
        f"STRANDED_AFTER_S is a bare literal ({line.strip()}); derive it from the "
        "agent-execution budget so the two cannot drift apart again")
