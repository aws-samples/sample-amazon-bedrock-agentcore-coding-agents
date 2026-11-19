"""A dropped connection is not the end of the command; a new PTY proves nothing.

November 18, team 8, every canary round: the Runtime logged "Kicked by new connection"
0.4 s after the shell started, the engine's connection closed with no exit code, and
the build stopped as REMOTE_EXECUTION_UNSETTLED while the same PTY finished the
backend's command with exit 0. The engine now reopens the SAME session and shell IDs
and keeps reading; it never types the command twice.

    python3 -m pytest orchestrator/test_shell_reconnect.py -q
"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_exec  # noqa: E402
from bedrock_agentcore.runtime.shell import ShellChannel  # noqa: E402
from bedrock_agentcore.runtime.shell.protocol import ShellFrame  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/example"


def _status(code):
    body = {"kind": "Status", "apiVersion": "v1", "metadata": {},
            "status": "Success" if code == 0 else "Failure"}
    if code:
        body["details"] = {"causes": [{"reason": "ExitCode", "message": str(code)}]}
    return ShellFrame(ShellChannel.STATUS, 3, json.dumps(body).encode())


def _out(text):
    return SimpleNamespace(channel=ShellChannel.STDOUT, text=text)


class Connection:
    """One WebSocket to the shell: its frames, then the end of the iteration."""

    def __init__(self, frames, *, reconnected=False, kicked=False):
        self.frames = list(frames)
        self.reconnected = reconnected
        self.kicked = kicked
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def send(self, command):
        self.sent.append(command)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


def _drive(monkeypatch, connections):
    opened = []

    def open_shell(**kwargs):
        opened.append((kwargs["session_id"], kwargs["shell_id"]))
        return connections[len(opened) - 1]

    monkeypatch.setattr(runtime_exec, "_client",
                        lambda _region: SimpleNamespace(open_shell=open_shell))
    lines = []
    result = asyncio.run(runtime_exec._drive_shell(
        ARN, "echo building", "us-west-2", lines.append, 10, "s" * 40))
    return result, opened, lines


def test_a_kicked_connection_reconnects_to_the_same_shell_and_reads_the_result(monkeypatch):
    first = Connection([_out("starting the backend\n")], kicked=True)
    second = Connection([_out("starting the backend\nserver ready\n"), _status(0)],
                        reconnected=True)
    result, opened, lines = _drive(monkeypatch, [first, second])
    assert result["exit"] == 0
    assert len(opened) == 2 and opened[0] == opened[1], "same session and shell IDs"
    assert len(first.sent) == 1 and second.sent == [], "the command is typed once"
    assert "server ready" in result["raw"]
    assert any("reconnecting to the same shell" in line for line in lines)


def test_a_reconnect_that_finds_a_new_pty_is_still_unknown(monkeypatch):
    first = Connection([_out("starting\n")], kicked=True)
    fresh = Connection([_status(0)], reconnected=False)
    with pytest.raises(runtime_exec.RemoteExecutionUnsettled):
        _drive(monkeypatch, [first, fresh])
    assert fresh.sent == [] and len(first.sent) == 1


def test_a_status_frame_ends_the_command_even_when_its_code_is_unreadable(monkeypatch):
    unreadable = ShellFrame(ShellChannel.STATUS, 3, b'{"kind": "Status"}')
    only = Connection([unreadable])
    with pytest.raises(runtime_exec.RemoteExecutionUnsettled):
        _drive(monkeypatch, [only, Connection([_status(0)], reconnected=True)])
    assert len(only.sent) == 1


def test_reconnects_are_bounded(monkeypatch):
    connections = [Connection([_out("x\n")], kicked=True)] + [
        Connection([_out("x\n")], reconnected=True)
        for _ in range(runtime_exec.SHELL_RECONNECTS + 3)]
    with pytest.raises(runtime_exec.RemoteExecutionUnsettled):
        _, opened, _ = _drive(monkeypatch, connections)
    assert sum(len(c.sent) for c in connections) == 1
    assert sum(1 for c in connections if not c.frames) == 1 + runtime_exec.SHELL_RECONNECTS


def test_a_command_over_the_frame_limit_is_typed_in_pieces_that_fit(monkeypatch):
    """A 65 KB check in a repair prompt once made a single 70 KB STDIN frame, which the
    SDK refuses and the shell protocol would close on (November 19)."""
    only = Connection([_out("done\n"), _status(0)])
    opened = []

    def open_shell(**kwargs):
        opened.append(kwargs["shell_id"])
        return only

    monkeypatch.setattr(runtime_exec, "_client",
                        lambda _region: SimpleNamespace(open_shell=open_shell))
    big = "printf %s " + "y" * 70_000
    result = asyncio.run(runtime_exec._drive_shell(ARN, big, "us-west-2", None, 10, "s" * 40))
    assert result["exit"] == 0
    assert len(only.sent) > 1
    assert all(len(piece.encode()) < 64 * 1024 for piece in only.sent)
    assert "".join(only.sent) == runtime_exec.typeable(big)
