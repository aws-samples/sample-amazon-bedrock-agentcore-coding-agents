"""A real PTY's process status cannot disappear behind a WebSocket CLOSE."""
import asyncio
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_exec
from bedrock_agentcore.runtime.shell import ShellChannel
from bedrock_agentcore.runtime.shell.protocol import ShellFrame

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/example"


class LocalPty:
    """Execute in a real PTY and return the documented Runtime STATUS shape."""

    async def __aenter__(self):
        self.master, self.slave = os.openpty()
        self.process = None
        self.finished = False
        return self

    async def send(self, command):
        self.process = subprocess.Popen(
            ["bash", "-c", command], stdin=self.slave, stdout=self.slave,
            stderr=self.slave, start_new_session=True)
        os.close(self.slave)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.finished:
            raise StopAsyncIteration
        try:
            chunk = await asyncio.to_thread(os.read, self.master, 47)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            chunk = b""
        if not chunk:
            code = self.process.wait(timeout=5)
            self.finished = True
            status = {"kind": "Status", "apiVersion": "v1", "metadata": {},
                      "status": "Success" if code == 0 else "Failure"}
            if code:
                status["details"] = {"causes": [{"reason": "ExitCode", "message": str(code)}]}
            return ShellFrame(ShellChannel.STATUS, 3, json.dumps(status).encode())
        return SimpleNamespace(channel=ShellChannel.STDOUT, text=chunk.decode())

    async def __aexit__(self, *_exc):
        os.close(self.master)
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)


def drive(monkeypatch, shell, command):
    monkeypatch.setattr(runtime_exec, "_client", lambda _region: SimpleNamespace(
        open_shell=lambda **_kwargs: shell))
    lines = []
    result = asyncio.run(runtime_exec._drive_shell(
        ARN, command, "us-west-2", lines.append, 10, "s" * 40))
    return result, lines


@pytest.mark.parametrize("code", [0, 7, 127])
def test_real_command_exit_is_read_from_the_runtime_status(monkeypatch, code):
    result, lines = drive(
        monkeypatch, LocalPty(),
        f"test -t 1 || exit 99; printf 'actual command output\\n'; exit {code}")
    assert result["exit"] == code
    assert "actual command output" in result["raw"]
    assert [line for line in lines if line] == ["actual command output"]


def test_a_python_import_failure_never_reports_success(monkeypatch):
    result, _ = drive(
        monkeypatch, LocalPty(),
        f"{sys.executable} -c 'import workshop_package_that_does_not_exist'; exit $?")
    assert result["exit"] == 1
    assert "ModuleNotFoundError" in result["raw"]


def test_close_without_a_command_result_is_a_transport_failure(monkeypatch):
    class Closed:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def send(self, _command):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    with pytest.raises(runtime_exec.RoleExecutionError, match="exit code"):
        drive(monkeypatch, Closed(), "echo never executed")


@pytest.mark.parametrize("frame", [
    SimpleNamespace(),
    SimpleNamespace(payload=b'{"status": "CLOSED"}'),
    SimpleNamespace(payload=b'{"status": true}'),
    SimpleNamespace(exit_code=True),
    SimpleNamespace(payload=b'{"status":"Failure","reason":"InternalError","code":500}'),
    SimpleNamespace(payload=b'{"status":"Success","metadata":{"shellId":"connected"}}'),
])
def test_unknown_status_is_not_zero(frame):
    assert runtime_exec._exit_from_status(frame) is None
