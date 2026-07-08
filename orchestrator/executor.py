"""The execution seam: which producer makes each role's artifact.

The engine drives the same five-phase blueprint no matter where the agents
execute. The producer is what sits behind this seam, and the shipped
orchestrator is real-only:

  * ``AgentCoreExecutor``: the shipped default. A role is dispatched to a coding
    agent already DEPLOYED on its own AgentCore Runtime: the role's CLI runs inside
    that Runtime over the command shell (``engine._runtime_cli`` /
    ``bedrock-agentcore:InvokeAgentRuntime``), writes its artifact there, and the
    engine reads it back. A role with no wired runtime ARN FAILS LOUD; there is no
    local, in-process, or model-in-process producer to fall back to.

Deterministic OFFLINE TESTS inject a test-only ``FixtureExecutor``
(``fixture_executor.py``) by constructor: it runs the role closures in-process and
the closures route their PRODUCE step to the deterministic builders. That double
lives only in test-support code; no env flag selects a fake on the shipped binary.

The engine selects its executor at startup from ``WORKSHOP_EXECUTOR`` (default /
``""`` / ``agentcore`` -> ``AgentCoreExecutor``; unknown values fail loud). Picking
the seam never alters the verdict path: pytest is still the only acceptance
authority, and the reviewer still runs in its separate pen.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Protocol


class RoleOutcome:
    """What an executor returns for one role: the artifact text it produced (or
    None when the executor wrote the file directly), plus usage to charge."""

    __slots__ = ("text", "usage", "engine", "note")

    def __init__(self, text: str | None = None, usage: dict | None = None,
                 engine: str = "", note: str = "") -> None:
        self.text = text
        self.usage = usage          # {"input_tokens", "output_tokens"} or None
        self.engine = engine        # "agentcore" (the runtime that produced it)
        self.note = note


class _LocalDispatch(Protocol):
    """The engine hands the executor a callable that runs one role's closure
    in-process (its backend/validator/frontend path). The shipped
    ``AgentCoreExecutor`` invokes it after confirming the role has a wired runtime
    (the closure's PRODUCE step then dispatches to that deployed Runtime); the
    test-only ``FixtureExecutor`` invokes it directly (the closure then builds the
    artifact deterministically). The seam only decides WHERE work runs; the
    closure does the work."""

    def __call__(self, role: Any) -> None: ...


class Executor:
    """Base seam. Subclasses decide where one role's work runs."""

    name = "executor"

    def dispatch(self, run: Any, agent_id: str, role: Any,
                 local_dispatch: _LocalDispatch) -> None:
        raise NotImplementedError


class AgentCoreExecutor(Executor):
    """Dispatch a role to a coding agent deployed on AgentCore Runtime: the
    shipped, real-only producer.

    Each role maps to a runtime ARN (constructor ``runtime_arns`` mapping, then the
    wirable config: env ``AGENTCORE_RUNTIME_<ROLE>`` / the Settings-pane / terminal
    ``.runs/runtime.local.json``). ``dispatch`` fails loud when the role has no
    wired runtime; there is no local fallback. With a wired runtime it runs the
    engine's role closure, whose PRODUCE step drives the role's CLI INSIDE that
    deployed Runtime over the command shell (``engine._runtime_cli``) and reads the
    artifact back; the engine then reads THAT file and the gate grades it. The
    verdict path is unchanged.

    This class is the shipped default (``from_env``). The legacy
    ``build_prompt``/``invoke_runtime``/``write_artifact`` helpers below are
    retained for the stubbed-client unit tests that pin the ``InvokeAgentRuntime``
    wire shape without a deployed runtime.
    """

    name = "agentcore"

    def __init__(self, runtime_arns: dict[str, str] | None = None,
                 region: str | None = None,
                 client: Any | None = None) -> None:
        self._arns = dict(runtime_arns or {})
        self._region = region or os.environ.get("WORKSHOP_BEDROCK_REGION", "us-west-2")
        self._client = client  # injectable for tests; lazily built otherwise

    def runtime_arn(self, agent_id: str) -> str | None:
        """Resolve the deployed runtime ARN for a role. The ARN is WIRABLE, never
        hardcoded: an explicit constructor mapping first, then the wirable config
        surface (``runtime_config``: env ``AGENTCORE_RUNTIME_<ROLE>`` then the
        Settings-pane / terminal-written ``.runs/runtime.local.json``)."""
        if agent_id in self._arns:
            return self._arns[agent_id]
        try:
            import runtime_config  # noqa: PLC0415
            hit = runtime_config.resolve(agent_id)
            if hit:
                return hit[0]
        except Exception:  # noqa: BLE001 (config module optional in some deploys)
            pass
        return None

    def _runtime_client(self) -> Any:
        if self._client is None:
            import boto3  # noqa: PLC0415 (lazy, mirrors llm.py)
            self._client = boto3.client("bedrock-agentcore", region_name=self._region)
        return self._client

    def dispatch(self, run: Any, agent_id: str, role: Any,
                 local_dispatch: _LocalDispatch) -> None:
        # Fail loud if the role has no wired runtime; real-only never degrades to
        # a local build.
        arn = self.runtime_arn(agent_id)
        if not arn:
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: no AgentCore runtime ARN for role "
                f"'{agent_id}' (set AGENTCORE_RUNTIME_"
                f"{agent_id.replace('-', '_').upper()})")
        # The engine's role closure owns the dispatch: when the executor is
        # ``agentcore`` it routes artifact production to the deployed Runtime over
        # the command shell (engine._runtime_cli) and keeps the local boot/probe/
        # gate steps on the engine box. (The legacy
        # build_prompt/invoke_runtime/write_artifact helpers below are retained for
        # the stubbed-client unit tests only; the shipped path runs the closure.)
        local_dispatch(role)

    # -- the three steps, separated so tests and subclasses can probe each one --
    def build_prompt(self, run: Any, agent_id: str, role: Any) -> str:
        """The role's prompt. The engine owns the role prompts; the executor asks
        it for the same prompt the local path would use."""
        builder: Callable[[Any, str, Any], str] | None = getattr(
            run, "_role_prompt_for", None)
        if builder is not None:
            return builder(run, agent_id, role)
        # Minimal, honest default if the engine didn't supply a prompt builder.
        return (run.task if hasattr(run, "task") else str(run))

    def invoke_runtime(self, arn: str, run: Any, agent_id: str,
                       prompt: str) -> RoleOutcome:
        """Call InvokeAgentRuntime and parse the response into a RoleOutcome."""
        payload = json.dumps({"prompt": prompt}).encode()
        session_id = getattr(run, "run_id", "session")
        user_id = (getattr(run, "options", {}) or {}).get("user_id", "workshop")
        resp = self._runtime_client().invoke_agent_runtime(
            agentRuntimeArn=arn,
            payload=payload,
            runtimeSessionId=f"{session_id}-{agent_id}",
            runtimeUserId=str(user_id),
        )
        text = self._read_response_text(resp)
        return RoleOutcome(text=text, engine="agentcore",
                           note=f"dispatched to {arn.split('/')[-1]}")

    @staticmethod
    def _read_response_text(resp: dict[str, Any]) -> str:
        """Read the runtime's streamed/whole response body into text."""
        body = resp.get("response")
        if body is None:
            return ""
        # botocore StreamingBody, bytes, or an already-decoded str.
        if hasattr(body, "read"):
            raw = body.read()
        else:
            raw = body
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return raw if isinstance(raw, str) else str(raw)

    def write_artifact(self, run: Any, agent_id: str, role: Any,
                       outcome: RoleOutcome) -> None:
        """Persist the returned artifact where the local path would, and record
        the outcome on the role so the engine's gate/compose steps are unchanged."""
        if outcome.engine:
            role.engine = outcome.engine
        if outcome.note:
            role.note = outcome.note
        writer: Callable[[Any, str, Any, str], None] | None = getattr(
            run, "_write_role_artifact", None)
        if writer is not None and outcome.text is not None:
            writer(run, agent_id, role, outcome.text)


def from_env(runtime_arns: dict[str, str] | None = None) -> Executor:
    """Build the executor the shipped engine should use from ``WORKSHOP_EXECUTOR``.

    Real-only: the shipped orchestrator dispatches each role to its deployed
    AgentCore Runtime, so the default (unset / ``""`` / ``agentcore``) is
    ``AgentCoreExecutor``, which fails loud on a missing wired ARN rather than
    building locally. There is no local/in-process producer on the shipped path;
    deterministic offline tests inject the test-only ``FixtureExecutor`` by
    constructor instead. Unknown values fail loud.
    """
    choice = os.environ.get("WORKSHOP_EXECUTOR", "agentcore").strip().lower()
    if choice in ("", "agentcore"):
        return AgentCoreExecutor(runtime_arns=runtime_arns)
    raise ValueError(f"UNKNOWN_EXECUTOR:{choice} (expected 'agentcore'; the shipped "
                     "path is real-only; tests inject FixtureExecutor by constructor)")
