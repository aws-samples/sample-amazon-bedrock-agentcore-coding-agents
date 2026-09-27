"""A dispatched command must arrive whole when it is typed into a Runtime PTY.

On 2026-09-27 a Lab 2 build typed a 10,980-character line into the backend
Runtime's shell; the terminal kept only the first 4095 characters and bash waited
at a continuation prompt until the 1,200-second dispatch limit. The PTY tests
type through a real Linux pseudo-terminal, so they fail on that defect instead of
on a fake. macOS terminals discard queued input differently, so they run on Linux
only; the same typed form was also run on the live backend and Kiro Runtimes
(9,000-character value intact, exit status kept) while the raw line hung.
"""
from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time

import pytest

from pty_typing import CHUNK, SAFE_LINE, typeable


def _long_command(n: int = 9000) -> str:
    # One line, like a dispatch: a quoted value, a cd, and an explicit exit status.
    return (f'cd /; V="{"x" * n}"; echo "LEN=${{#V}} DIR=$(pwd)"; '
            'if [ "${#V}" -gt 0 ]; then echo DONE-MARK; fi; exit 7')


def _type_into_pty(text: str, marker: str, timeout_s: float = 20.0) -> tuple[str, int | None]:
    """Start an interactive bash on a PTY and type ``text`` at once, as a dispatch does."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execvp("bash", ["bash", "--norc", "--noprofile", "-i"])
    # Feed the keystrokes as the terminal accepts them (a blocking write stalls once
    # the input queue is full), while draining its output.
    os.set_blocking(fd, False)
    pending, out, deadline = text.encode(), b"", time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready, writable, _ = select.select([fd], [fd] if pending else [], [], 0.2)
        if writable and pending:
            try:
                pending = pending[os.write(fd, pending[:512]):]
            except BlockingIOError:
                pass
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    try:
        _, status = os.waitpid(pid, os.WNOHANG)
        code = os.waitstatus_to_exitcode(status) if status else None
    except ChildProcessError:
        code = None
    if code is None:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    return out.decode(errors="replace"), code


def test_short_commands_are_typed_unchanged():
    cmd = 'echo "$B1"; cd /tmp; /app/run.sh --model m'
    assert typeable(cmd) == cmd + "\n"
    assert typeable(cmd + "\n") == cmd + "\n"


def test_long_commands_become_short_lines_that_round_trip():
    cmd = _long_command()
    typed = typeable(cmd)
    assert max(len(line) for line in typed.splitlines()) <= CHUNK + 16
    assert len(cmd) > SAFE_LINE
    # bash -s reads lines exactly as an interactive shell receives typed ones.
    run = subprocess.run(["bash", "-s"], input=typed, capture_output=True, text=True, timeout=30)
    assert "LEN=9000 DIR=/" in run.stdout and "DONE-MARK" in run.stdout
    assert run.returncode == 7


LINUX_PTY = pytest.mark.skipif(not sys.platform.startswith("linux"),
                               reason="Runtime terminals are Linux; macOS drops queued input differently")


@LINUX_PTY
def test_the_typed_form_survives_a_real_terminal():
    out, code = _type_into_pty(typeable(_long_command()), "DONE-MARK")
    assert "LEN=9000" in out and "DONE-MARK" in out.replace("echo DONE-MARK", "")
    assert code == 7


@LINUX_PTY
def test_negative_control_the_raw_long_line_does_not_survive():
    # The defect itself: typed whole, the line is cut and the command never runs.
    out, code = _type_into_pty(_long_command() + "\n", "DONE-MARK", timeout_s=8.0)
    assert "LEN=9000" not in out
    assert code != 7
