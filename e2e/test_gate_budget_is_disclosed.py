"""The check must be told how long it has.

Four consecutive live runs in us-east-1 failed their authored check on a READINESS
POLL, not on a wrong answer:

  run_032941_001 iter 2  "server stopped responding to /health within 30 seconds"
  run_032941_002 iter 1  same
  run_032941_002 iter 2  "started successfully ... then immediately shut down"
  run_045143_001 iter 1  "didn't detect the server as ready within 15 seconds,
                          even though uvicorn actually started successfully"

The validator budgeted 15-30s for a first start that was still preparing declared
dependencies. Nothing in the code tells the validator how much of the enforced wall
clock remains.

Those were RED GATES ON WORKING SERVICES, which is the one verdict this engine must
never manufacture. A gate is allowed to reject bad work; it is not allowed to reject
work because the checker under-waited.

The fix keeps validation agentic. `WORKSHOP_GATE_TIMEOUT_S` is a fact about the
ENVIRONMENT (the wall clock the engine will enforce anyway), not a hint about the
verdict: it says nothing about what to verify or what a correct answer looks like. The
validator still decides every check, the language, and the file.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ORCH = os.path.join(_ROOT, "orchestrator")
if _ORCH not in sys.path:
    sys.path.insert(0, _ORCH)


class _Run:
    def __init__(self, tmp: str) -> None:
        self._acceptance_test_file = os.path.join(tmp, "acceptance_check")
        self.task = "build something"
        self.artifact_endpoint = ""
        self.workdir = tmp


def _authored_check(tmp: str, body: str) -> _Run:
    run = _Run(tmp)
    with open(run._acceptance_test_file, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(run._acceptance_test_file, 0o755)
    return run


def test_the_budget_reaches_the_authored_check(tmp_path) -> None:
    """The check can only size its own poll if it is handed the real deadline.

    Asserted by RUNNING a check that reads the variable, not by reading source.
    """
    import reviewer

    run = _authored_check(str(tmp_path), "#!/bin/sh\n"
                                         'test -n "$WORKSHOP_GATE_TIMEOUT_S" || exit 3\n'
                                         'echo "budget=$WORKSHOP_GATE_TIMEOUT_S"\n')
    result = reviewer.run_gate(run)
    assert result["passed"], result
    assert f"budget={reviewer.GATE_TIMEOUT_S}" in str(result)


def test_the_budget_matches_what_the_engine_enforces(tmp_path) -> None:
    """A budget that disagreed with the real wall clock would be worse than none."""
    import reviewer

    run = _authored_check(str(tmp_path), "#!/bin/sh\n"
                                         'echo "$WORKSHOP_GATE_TIMEOUT_S"\n')
    result = reviewer.run_gate(run)
    reported = "".join(c["detail"] for c in result["checks"] if "detail" in c) \
        if result.get("checks") else ""
    assert str(reviewer.GATE_TIMEOUT_S) in reported + str(result)


def test_the_engine_still_hands_over_no_answers(tmp_path) -> None:
    """Agentic-only validation: the env may carry FACTS, never a verdict or a target.

    A regression here would be someone adding an expected value, a contract path, or a
    pass/fail hint alongside the budget.
    """
    import reviewer

    run = _authored_check(str(tmp_path), "#!/bin/sh\nenv | sort\n")
    reviewer.run_gate(run)
    # Inspect the names the engine sets, from its own source of truth.
    source = open(os.path.join(_ORCH, "reviewer.py"), encoding="utf-8").read()
    block = source[source.index('"WORKSHOP_WORK_DIR":'):]
    block = block[:block.index("}")]
    allowed = {"WORKSHOP_WORK_DIR", "WORKSHOP_TASK", "WORKSHOP_GATE_TIMEOUT_S",
               "DELIVERABLE_URL", "MCP_ENDPOINT_URL"}
    for name in ("EXPECTED", "ANSWER", "CONTRACT", "GRADE", "REFERENCE", "SOLUTION"):
        assert name not in block.upper(), (
            f"{name} in the gate env would make validation non-agentic")
    handed = {n for n in allowed if n in block}
    assert handed == allowed, f"gate env changed shape: {handed}"


def test_the_validator_steering_warns_about_cold_start_time() -> None:
    """Local checkout removed NFS, but dependency startup still needs a real budget."""
    path = os.path.join(_ROOT, "orchestrator", "harness",
                        "claude-code-validator", "CLAUDE.md")
    text = open(path, encoding="utf-8").read()
    assert "WORKSHOP_GATE_TIMEOUT_S" in text
    lowered = text.lower()
    assert "declared dependencies" in lowered
    assert "network file mount" not in lowered
    # The reason a red gate here is the wrong verdict must be stated, not implied.
    assert "answers wrongly" in lowered or "answers incorrectly" in lowered


def test_the_steering_states_a_hard_MINIMUM_poll() -> None:
    """Advice was not enough on its own.

    After the budget was disclosed AND the mount slowness explained, a live run still
    authored a 20s readiness poll. An agent weighing "spend a real share of the budget"
    can reasonably land anywhere; a floor cannot be reasoned below. So the steering
    carries one number.
    """
    path = os.path.join(_ROOT, "orchestrator", "harness",
                        "claude-code-validator", "CLAUDE.md")
    text = open(path, encoding="utf-8").read()
    assert "AT LEAST 60 SECONDS" in text, (
        "a soft 'spend a real share' left a live run choosing 20s; state a floor")


def test_the_budget_exceeds_the_required_poll() -> None:
    """A ceiling below the required floor would re-create the bug it was meant to fix.

    The check must be able to wait its mandated 60s AND still have room to run real
    assertions, otherwise the budget itself pushes checks to under-wait.
    """
    import reviewer

    assert reviewer.GATE_TIMEOUT_S >= 180, (
        f"{reviewer.GATE_TIMEOUT_S}s leaves too little after a 60s readiness poll")


def test_the_budget_is_wirable_but_cannot_be_lowered_into_the_bug(monkeypatch) -> None:
    """Operators may raise it for a slower environment; they may not floor it away."""
    import importlib

    import reviewer

    monkeypatch.setenv("WORKSHOP_GATE_BUDGET_S", "600")
    assert importlib.reload(reviewer).GATE_TIMEOUT_S == 600
    monkeypatch.setenv("WORKSHOP_GATE_BUDGET_S", "5")
    assert importlib.reload(reviewer).GATE_TIMEOUT_S == 180
    monkeypatch.delenv("WORKSHOP_GATE_BUDGET_S")
    importlib.reload(reviewer)


def test_the_dispatch_prompt_repeats_the_floor() -> None:
    """The steering alone did not hold, so the last channel states it too.

    With the mount slowness in its steering AND the budget in its env, a live run still
    authored a 20s poll. The dispatch prompt is the final instruction before the file
    is written, so it carries the number rather than assuming the earlier one won.
    """
    source = open(os.path.join(_ORCH, "engine.py"), encoding="utf-8").read()
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "AT LEAST 60 SECONDS" in body
    assert "WORKSHOP_GATE_TIMEOUT_S" in body


def test_the_two_budget_names_are_distinct() -> None:
    """One name for both directions would read as if a check sets its own deadline."""
    source = open(os.path.join(_ORCH, "reviewer.py"), encoding="utf-8").read()
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "WORKSHOP_GATE_BUDGET_S" in body      # what an OPERATOR sets
    assert "WORKSHOP_GATE_TIMEOUT_S" in body     # what the CHECK is told
    assert 'environ.get("WORKSHOP_GATE_TIMEOUT_S")' not in body, (
        "the check must not be able to raise the deadline it is judged against")


def test_the_backend_skill_keeps_install_off_the_start_path() -> None:
    """The other half: a start command that installs first cannot become ready fast."""
    path = os.path.join(_ROOT, "harness-skills", "skills",
                        "backend-engineering", "SKILL.md")
    text = open(path, encoding="utf-8").read().lower()
    assert "foreground" in text, "a backgrounding start reads as a dead service"
    assert "setup, not startup" in text or "install is setup" in text
