"""A shell that never opened is retried, a few times, on the same session.

October 13, dev stack: the first backend dispatch after a Runtime update failed with
`TimeoutError: timed out during opening handshake` and the team build stopped at
ROLE_EXECUTION_ERROR, although the Runtime answered in 4 s minutes later. Nothing had
been typed, so retrying the connect cannot run anything twice.

November 18, team 8: the abandoned handshake still reached the cold microVM after the
retry, and two connections on ONE shell ID kicked each other until the engine's
closed. The first connect's retry now uses a new shell ID; a reconnect to a shell
that already holds a command keeps its ID.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import runtime_exec


class _Session:
    def __init__(self, outcome, log):
        self.outcome, self.log = outcome, log

    async def __aenter__(self):
        self.log.append("enter")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *exc):
        self.log.append("exit")


class _Client:
    def __init__(self, outcomes):
        self.outcomes, self.calls, self.log = list(outcomes), [], []

    def open_shell(self, **kw):
        self.calls.append(kw)
        return _Session(self.outcomes.pop(0), self.log)


class _Rejected(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


def _use(client, deadline_s=60.0, **kw):
    slept, used = [], []

    async def sleep(seconds):
        slept.append(seconds)

    async def go():
        async with runtime_exec._open_shell(client, "arn:rt", "s" * 36, "shell-1",
                                            time.monotonic() + deadline_s, sleep=sleep,
                                            connected=used.append, **kw) as shell:
            return shell
    shell = asyncio.run(go())
    _use.used = used
    return shell, slept


def test_a_handshake_timeout_is_retried_on_the_same_session_with_a_new_shell():
    client = _Client([TimeoutError("timed out during opening handshake"), "SHELL"])
    shell, slept = _use(client)
    assert shell == "SHELL" and slept == [2.0]
    assert [c["session_id"] for c in client.calls] == ["s" * 36] * 2
    first, retry = (c["shell_id"] for c in client.calls)
    assert first == "shell-1" and retry != "shell-1", "a late abandoned upgrade must not share it"
    assert _use.used == [retry]
    assert client.log == ["enter", "enter", "exit"]


def test_a_reconnect_retry_keeps_the_shell_that_holds_the_command():
    client = _Client([TimeoutError("timed out during opening handshake"), "SHELL"])
    shell, _ = _use(client, new_shell_on_retry=False)
    assert shell == "SHELL"
    assert {c["shell_id"] for c in client.calls} == {"shell-1"}


def test_a_throttled_or_unavailable_service_is_retried_but_a_refusal_is_not():
    shell, _ = _use(_Client([_Rejected(503), _Rejected(429), "SHELL"]))
    assert shell == "SHELL"
    with pytest.raises(_Rejected):
        _use(_Client([_Rejected(403), "SHELL"]))
    with pytest.raises(ValueError):
        _use(_Client([ValueError("region mismatch"), "SHELL"]))


def test_the_retry_is_bounded_by_attempts_and_by_the_deadline():
    with pytest.raises(TimeoutError):
        _use(_Client([TimeoutError("a"), TimeoutError("b"), TimeoutError("c"), "SHELL"]))
    with pytest.raises(TimeoutError):
        _use(_Client([TimeoutError("late"), "SHELL"]), deadline_s=1.0)
