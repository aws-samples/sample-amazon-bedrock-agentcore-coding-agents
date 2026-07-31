"""The validator must be TOLD, in the dispatch prompt, to start the deliverable.

Its steering has always said so, but the steering is read from the working
directory while the dispatch prompt is what the agent is handed for this specific
task. A live run produced a check whose only assertion was "is something already
listening on port 3000", which nothing was: the gate went red twice on a WORKING
deliverable and the run ended `needs_human`. The instruction has to be in the
prompt, not only in the steering.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "orchestrator"))


def _validator_prompt() -> str:
    """The exact prompt text the engine builds for the checker."""
    import inspect
    import engine
    src = inspect.getsource(engine.Engine._cli_validator_authors_test)
    return src


def test_the_prompt_tells_the_check_to_start_the_service_itself():
    p = _validator_prompt().lower()
    assert "your check must start it" in p, (
        "the dispatch prompt does not tell the validator to START the deliverable; "
        "a check that only probes an address nothing started can never pass, and it "
        "reports working software as broken")
    # And says WHY, so the agent does not treat it as optional politeness.
    assert "nothing else starts it" in p or "no process is running" in p, p[:400]


def test_the_prompt_warns_against_assuming_a_default_port():
    p = _validator_prompt().lower()
    assert "port" in p and ("unused" in p or "yourself" in p), (
        "the live failure assumed port 3000; the prompt should tell the validator to "
        "choose the port rather than assume a default")


def test_round_two_tells_the_validator_its_own_check_may_be_at_fault():
    p = _validator_prompt().lower()
    assert "your check" in p and "fault" in p, (
        "on a re-implement round the validator is only told to cover the failures, "
        "so a check that blamed working software will blame it again")


def test_the_validator_does_not_contradict_its_own_runtime_oracle():
    p = _validator_prompt().lower()
    assert "runtime oracle" in p and "hand-entered" in p, (
        "a live validator check matched the named standard on every oracle-based "
        "case, then rejected the same implementation using incorrect manually "
        "counted values")


def test_the_validator_syntax_checks_without_running_its_acceptance_behavior():
    p = _validator_prompt().lower()
    assert "parse-only" in p and "python -m py_compile" in p, (
        "a live validator handed off a Python snippet with broken nested quotes; "
        "the prompt must require a syntax-only check before the engine executes it")
    assert "native json" in p and "python -c" in p, (
        "test payloads must not become source code through nested shell quoting")


def test_the_steering_says_the_same_thing_and_the_baked_copy_matches():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shipped = os.path.join(repo, "orchestrator", "harness",
                           "claude-code-validator", "CLAUDE.md")
    baked = os.path.join(repo, "coding-agents", "claude-code-validator", "CLAUDE.md")
    a = open(shipped, encoding="utf-8").read()
    b = open(baked, encoding="utf-8").read()
    assert a == b, "the baked steering drifted from the shipped one"
    assert "YOUR CHECK STARTS IT" in a, a[:400]
    assert "runtime oracle" in a.lower()
    assert "syntax-check" in a.lower()
    assert "python -c" in a
