"""Test-support execution seam: the offline producer.

THIS MODULE IS TEST-ONLY. No shipped module imports it (not engine.py, not
connection_api.py, not server.py, not main.py). The shipped engine's sole
producer is ``AgentCoreExecutor`` (executor.py), which dispatches each role to
its deployed AgentCore Runtime and FAILS LOUD on a missing wired ARN; there is
no local/fake/in-process producer on the shipped path.

Deterministic offline tests need SOMETHING on disk without a deployed runtime and
without a model call. ``FixtureExecutor`` is injected into the engine by
constructor (``Engine(executor_obj=FixtureExecutor())``): it runs the engine's role
closures in-process, and the closures route their PRODUCE step here.

What it writes is deliberately CONTENT-FREE. It is not a reference solution, it
does not know a protocol, a language, or an expected value, and it cannot tell
whether a deliverable is correct. Its whole job is to make the MACHINERY runnable
offline: role dispatch, tree read-back, the acceptance check as a real subprocess,
compose, and the PR ladder. A test may assert that machinery is real; it may never
assert what agents produce, because in production nothing in this repository knows
that.

The validator's stub is a REAL executable with a shebang, so even offline the gate
is a real ``subprocess.run`` of a real file whose real exit code decides. That is
the one property the offline path must not simulate: ``run.options`` may set
``fail_first_check`` to make round 1 exit nonzero, which is how the bounded
re-implement loop is exercised with a genuinely red gate rather than a faked one.

Tests import this module explicitly; it is never on the shipped import graph.
"""

from __future__ import annotations

import os
import stat
from typing import Any

import executor
import roles


def _is_checker(agent_id: str, role: Any) -> bool:
    """Whether this dispatch is the CHECKER, read from the registry.

    Deliberately not ``agent_id.endswith("validator")``: that guessed the kind from the
    spelling of an id, so a roster whose checker is named something else (Kiro) silently
    got a builder's stand-in and every gate went red on a passing run. The registry is
    the one place a role declares whether it makes or checks; ``role.role`` is the
    fallback for a fixture that dispatches an unregistered id.
    """
    try:
        return roles.get(agent_id).kind == roles.CHECKER
    except roles.UnknownRole:
        return str(getattr(role, "role", "")) == "validator"

# The validator stub, as a real executable. It checks only that the builders left
# something behind: enough to be a true statement about the run, and nothing about
# what was built. FIXTURE_CHECK_EXIT lets a test drive a real nonzero exit.
_CHECK_BODY = """#!/bin/sh
# Offline acceptance check (test double). Deliberately content-free: it verifies a
# deliverable tree exists, never what is in it.
if [ ! -d "$WORKSHOP_WORK_DIR" ]; then
  echo "check: no work directory to inspect"
  exit 2
fi
n=$(find "$WORKSHOP_WORK_DIR" -type f | wc -l | tr -d ' ')
if [ "$n" -lt 1 ]; then
  echo "check: the deliverable tree is empty"
  exit 1
fi
echo "check: deliverable tree present ($n files)"
exit ${FIXTURE_CHECK_EXIT:-0}
"""


class FixtureExecutor(executor.Executor):
    """A test double on the execution seam: run the engine's role closure
    in-process (so the gate/compose/PR tail still runs), and produce content-free
    stand-in files. It decides WHERE work runs and WHAT PLACEHOLDER exists, never
    WHETHER the work is acceptable: the gate remains a real execution of a real
    file, reading a real exit code."""

    name = "fixture"

    def dispatch(self, run: Any, agent_id: str, role: Any,
                 local_dispatch: executor._LocalDispatch) -> None:
        local_dispatch(role)

    def produce(self, run: Any, agent_id: str, role: Any) -> str:
        """Write the offline stand-in for one role and return its main path.

        Builders get a plain file so the tree is non-empty (which is all the engine
        requires of them). The validator gets a real executable, because the gate
        must stay a real subprocess even offline.

        Both are labelled UNAMBIGUOUSLY as an offline test double. That labelling is
        load-bearing: the LLM assessment is a real reviewer, and a real reviewer SHOULD
        refuse to approve a content-free stub presented as finished work. Saying so
        plainly lets the review layer judge the plumbing rather than be tricked by it,
        and keeps the offline suite from depending on a judge being fooled.
        """
        # Mark the run: its work is a stub, so the integrated review abstains instead of
        # being asked to approve something that implements nothing (see
        # reviewer._default_judge). A real dispatch never sets this.
        run._offline_double = True
        workdir = run.roledir(agent_id)
        os.makedirs(workdir, exist_ok=True)
        if _is_checker(agent_id, role):
            path = os.path.join(workdir, "acceptance_check")
            body = _CHECK_BODY
            # `fail_first_check` exercises the bounded re-implement loop with a
            # GENUINELY red gate: the check exits nonzero for real on round 1. The old
            # mechanism pointed the gate at a dead port, which faked the failure.
            if run.options.get("fail_first_check") and run.iterations == 1:
                body = body.replace("exit ${FIXTURE_CHECK_EXIT:-0}",
                                    'echo "check: first round rejected"\nexit 1')
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP
                     | stat.S_IXOTH)
            return path
        # A builder's stand-in: one file naming the role and the task, so the
        # compose/PR path has real content to carry without encoding any answer.
        path = os.path.join(workdir, f"{agent_id}-work.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"OFFLINE TEST DOUBLE, not a deliverable and not a solution.\n"
                    f"role: {agent_id}\n"
                    f"request it stood in for: {getattr(run, 'task', '')}\n"
                    f"This file exists so the orchestrator's plumbing (tree read-back, "
                    f"compose, commit, pull request) can be exercised with no agent and "
                    f"no model. It intentionally implements nothing.\n")
        return path
