"""Two rules about how a failure is REPORTED, both broken on a live us-east-1 box.

1. Never blame the agent for our bug. "finished but wrote no files" is a claim about
   the AGENT. When the runtime workspace still holds the work, that claim is FALSE
   and it sends the reader to debug an agent that did its job.

2. A hang must become a failure. ``timeout_s`` was checked only inside
   ``async for frame in shell``, so a shell that connected and then went silent
   never tripped it: two jobs sat 9m02s (limit 600s) and 4m32s (limit 180s) with
   zero output before being killed by hand. A run that hangs reports no verdict at
   all, which is strictly worse than any red gate.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH = os.path.join(os.path.dirname(_HERE), "orchestrator")
if _ORCH not in sys.path:
    sys.path.insert(0, _ORCH)

_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/claude_code-AbC"


# --- 1. an unreadable-but-present workspace is OUR failure, not the agent's -------

class _FakeExecutor:
    name = "agentcore"


def _engine_with_empty_read(monkeypatch, listing: str):
    """An engine whose read-back returns NOTHING while the runtime HAS `listing`."""
    import engine
    import runtime_config
    import runtime_exec

    eng = engine.Engine(executor_obj=_FakeExecutor())
    monkeypatch.setattr(eng, "_read_work_tree", lambda run, agent: 0)
    monkeypatch.setattr(runtime_config, "pick", lambda agent: (_ARN, "env"))
    monkeypatch.setattr(runtime_exec, "list_tree_in_runtime",
                        lambda arn, sub, **kw: listing)
    monkeypatch.delenv("WORKSHOP_S3FILES_DIR", raising=False)
    return eng


def test_work_present_in_the_runtime_is_a_transport_error_not_a_role_failure(
        monkeypatch) -> None:
    """The live case: server.py was on the mount and the run said the agent wrote
    nothing. The error must name US, and must not claim the agent produced nothing."""
    import engine

    eng = _engine_with_empty_read(monkeypatch,
                                  "/mnt/s3files/run_1/server.py\n"
                                  "/mnt/s3files/run_1/acceptance_check\n")
    run = engine.Run(run_id="run_1", task="t", agents=["claude-code"], roles={},
                     options={}, created_at="now")
    with pytest.raises(RuntimeError) as excinfo:
        eng._require_tree_nonempty(run, "claude-code", tail="")
    msg = str(excinfo.value)
    assert "ARTIFACT_TRANSFER_ERROR" in msg
    assert "DID write work" in msg
    # The false claim must be GONE, not merely softened by a trailing note.
    assert "wrote no files" not in msg
    # And it must point the reader away from the agent.
    assert "not a failed agent turn" in msg
    # The evidence (what the runtime actually holds) travels with the error.
    assert "server.py" in msg


def test_a_genuinely_empty_workspace_still_blames_the_role(monkeypatch) -> None:
    """The honest case must keep working: nothing on the runtime, nothing read back,
    so the role really did produce nothing and the message should say exactly that."""
    import engine

    eng = _engine_with_empty_read(monkeypatch, "")
    run = engine.Run(run_id="run_2", task="t", agents=["claude-code"], roles={},
                     options={}, created_at="now")
    with pytest.raises(RuntimeError) as excinfo:
        eng._require_tree_nonempty(run, "claude-code", tail="boom")
    msg = str(excinfo.value)
    assert "ROLE_EXECUTION_ERROR" in msg
    assert "finished but wrote no files" in msg
    assert "ARTIFACT_TRANSFER_ERROR" not in msg


def test_transport_error_has_its_own_next_action() -> None:
    """``next_action`` is what an attendee actually reads. The transport case must not
    inherit the role-failure advice ("a role's turn produced no usable work"), which
    would contradict the honest error we just raised."""
    import engine

    transport = engine.next_action("failed", "ARTIFACT_TRANSFER_ERROR")
    role = engine.next_action("failed", "ROLE_EXECUTION_ERROR")
    assert transport and transport != role
    assert "transport" in transport.lower()
    assert "produced no" not in transport.lower()


def test_transport_failure_is_retryable_not_permanent() -> None:
    """Resubmitting is the correct recovery, so it must not be classed permanent."""
    import engine

    assert not engine._is_permanent("ARTIFACT_TRANSFER_ERROR")


# --- 2. a silent shell must time out ---------------------------------------------

class _SilentShell:
    """Connects, accepts the command, then never yields a frame (the live hang)."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)

    async def send(self, data):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _TalkingShell:
    """One stdout frame then a STATUS frame: the normal path must still work."""

    def __init__(self):
        self._n = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        from bedrock_agentcore.runtime.shell import ShellChannel
        self._n += 1
        if self._n == 1:
            return _Frame(ShellChannel.STDOUT, "hello\n")
        if self._n == 2:
            frame = _Frame(ShellChannel.STATUS)
            frame.exit_code = 0
            return frame
        raise StopAsyncIteration

    async def send(self, data):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Frame:
    def __init__(self, channel, text=""):
        self.channel, self.text = channel, text


def _patch_shell(monkeypatch, shell) -> None:
    import runtime_exec

    class _Client:
        def open_shell(self, **kwargs):
            return shell

    monkeypatch.setattr(runtime_exec, "_client", lambda region: _Client())


def test_a_silent_shell_raises_at_the_deadline(monkeypatch) -> None:
    """No frame ever arrives, so the OLD code waited forever. It must raise, and
    raise ON TIME: the point is the wall clock decides, not the peer."""
    import runtime_exec

    _patch_shell(monkeypatch, _SilentShell())
    started = time.monotonic()
    with pytest.raises(runtime_exec.RoleExecutionError) as excinfo:
        asyncio.run(runtime_exec._drive_shell(
            _ARN, "cmd", "us-east-1", None, 2.0, "s" * 40))
    elapsed = time.monotonic() - started
    assert elapsed < 12.0, (
        f"raised only after {elapsed:.1f}s for a 2s budget: the deadline is not "
        "bounding the frame wait")
    assert "exceeded" in str(excinfo.value)


def test_the_normal_path_is_unchanged(monkeypatch) -> None:
    """The timeout must not cost us the ordinary case: frames still stream and the
    real exit code still comes back."""
    import runtime_exec

    _patch_shell(monkeypatch, _TalkingShell())
    result = asyncio.run(runtime_exec._drive_shell(
        _ARN, "cmd", "us-east-1", None, 30.0, "s" * 40))
    assert result["exit"] == 0
    assert "hello" in result["raw"]
