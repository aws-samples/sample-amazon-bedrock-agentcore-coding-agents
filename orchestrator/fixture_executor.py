"""Test-support execution seam: the deterministic offline producer.

THIS MODULE IS TEST-ONLY. No shipped module imports it (not engine.py, not
connection_api.py, not server.py, not main.py). The shipped engine's sole
producer is ``AgentCoreExecutor`` (executor.py), which dispatches each role to
its deployed AgentCore Runtime and FAILS LOUD on a missing wired ARN; there is
no local/fake/in-process producer on the shipped path.

Deterministic offline tests need artifacts without a deployed runtime and without
a model call. ``FixtureExecutor`` is injected into the engine by constructor
(``Engine(executor_obj=FixtureExecutor())``): it runs the engine's role closures
in-process, and the closures branch on ``self.executor.name == "fixture"`` to route
their PRODUCE step to the deterministic builders. The verdict path (boot the
artifact as a subprocess, probe liveness, run the pytest acceptance gate, the
reviewer's separate pen, the PR ladder) is identical regardless of which executor
produced the artifact, so a test exercises the gate/reviewer/compose/PR chain
against a deterministically built artifact, with no LLM and no live AWS.

Tests import this module explicitly; it is never on the shipped import graph.
"""

from __future__ import annotations

from typing import Any

import executor


class FixtureExecutor(executor.Executor):
    """A test double on the execution seam: run the engine's role closure
    in-process (so the gate/compose/PR tail still runs), and let the closure build
    its artifact deterministically (it branches on
    ``self.executor.name == "fixture"``). It only decides WHERE work runs (here,
    in-process); the closure does the work."""

    name = "fixture"

    def dispatch(self, run: Any, agent_id: str, role: Any,
                 local_dispatch: executor._LocalDispatch) -> None:
        local_dispatch(role)
