"""A RED GATE must not be answered by starting the same build again.

Found on a live us-east-1 run. `run_032941_001` failed its authored check twice
(iteration 1: the frontend needed a Node build step the check refused; iteration 2:
the declared entrypoint never became healthy), correctly ended `needs_human` with
`ITERATION_CAP` and no PR, and the deployed coordinator then said "This is a
recoverable situation. I will resubmit the same build now." and started
`run_032941_002` on its own.

That is the unbounded loop `MAX_REVIEW_ROUNDS` exists to prevent, arriving one level
up: the engine's cap holds, and the chat layer restarts the whole thing anyway. Two
costs, and the second is the one that matters:

  * a second full multi-agent build to reach the same verdict, and
  * a real red gate reported as "let me try again" rather than as a finding, which is
    exactly the "verify step is honest" property the workshop teaches.

The engine already draws this distinction correctly (`_NEXT_ACTION` gives
ITERATION_CAP "read the failing lines" and the role-failure reasons "submit the SAME
request again"). The bug was that the coordinator's steering told it to resubmit ANY
`needs_human`, so it overrode the per-reason advice it was handed.
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH = os.path.join(os.path.dirname(_HERE), "orchestrator")
if _ORCH not in sys.path:
    sys.path.insert(0, _ORCH)


def _prompt() -> str:
    """The coordinator's steering, comments stripped.

    Stripping matters: an earlier test of mine matched its own explanatory comment
    and passed while the code was wrong.
    """
    import chat

    text = chat.SYSTEM_PROMPT if hasattr(chat, "SYSTEM_PROMPT") else ""
    if not text:
        source = open(os.path.join(_ORCH, "chat.py"), encoding="utf-8").read()
        text = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
    return text


def test_the_prompt_tells_the_coordinator_to_read_next_action() -> None:
    """`next_action` is derived per fail reason, so it is the thing to obey."""
    assert "next_action" in _prompt()


def test_blocked_validation_is_reported_not_resubmitted() -> None:
    """ITERATION_CAP covers gate/review evidence and is REPORTED, not retried."""
    prompt = _prompt()
    assert "ITERATION_CAP" in prompt, (
        "the steering must distinguish blocked validation from a role that produced "
        "nothing; they have opposite recoveries and the same status token")
    # The instruction around ITERATION_CAP must point at evidence, not a retry.
    window = prompt[prompt.index("ITERATION_CAP"):][:1200]
    assert "gate.summary" in window
    assert "review" in window
    assert "Never call a green gate" in window
    assert "resubmission_allowed" in prompt
    assert re.search(r"\bREPORT\b", window), (
        "blocked validation after its bounded re-implement round must be reported")


def test_resubmission_is_bounded() -> None:
    """An unbounded 'just resubmit' is the runaway the cap exists to prevent."""
    prompt = _prompt().lower()
    assert "at most once" in prompt, (
        "the steering must bound resubmission; without a bound a persistently red "
        "gate can restart the build forever")


def test_role_failures_are_still_resubmitted() -> None:
    """The honest retry case must survive: nothing was judged, so retry is right."""
    prompt = _prompt()
    assert "ROLE_EXECUTION_ERROR" in prompt or "ROLE_TOTAL_FAILURE" in prompt
    assert "SAME task text" in prompt


def test_engine_and_steering_agree_on_the_two_cases() -> None:
    """The engine's per-reason advice is the contract the steering must not contradict.

    Asserted against the ENGINE rather than a copy of its strings, so this test keeps
    holding if the wording changes but the meaning does not.
    """
    import engine

    cap = engine.next_action("needs_human", "ITERATION_CAP").lower()
    role = engine.next_action("needs_human", "ROLE_EXECUTION_ERROR").lower()
    # The blocked-validation arm must NOT advise resubmitting...
    assert "do not resubmit" in cap and "same request again" not in cap
    assert engine.resubmission_allowed(
        "needs_human", "ITERATION_CAP") is False
    # ...while the role-failure arm must.
    assert "same request again" in role or "resubmit" in role
    assert engine.resubmission_allowed(
        "needs_human", "ROLE_EXECUTION_ERROR") is True
    # Role PRs retain evidence, but blocked validation cannot publish the final PR.
    assert "no final integration pull request" in cap
    assert "existing role pull requests" in cap
