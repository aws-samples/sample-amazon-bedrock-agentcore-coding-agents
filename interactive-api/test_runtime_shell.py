"""Runtime shell session targeting (R26): pick WHICH wired instance to open.

A role can be a FLEET of N deployed runtimes (runtime_config wires a list). The
Agents page lets the attendee choose which instance a new session connects to;
that choice threads down to ``runtime_shell.get_runtime_arn(agent_id, instance_arn)``.
These tests prove the targeting end of that thread without a browser or live AWS:

  * no instance_arn -> the role's first wired ARN (the single-instance default);
  * a valid instance_arn -> exactly that instance (fleet selection);
  * a forged/stale instance_arn (not one of the role's wired ARNs) -> rejected,
    never silently dispatched to an arbitrary runtime.

Isolation is the REAL env seam: WORKSHOP_RUNTIME_CONFIG points runtime_config at a
tmp file (no monkeypatch of internals), and the role env vars are cleared so each
test starts unwired.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orchestrator"))

import runtime_config  # noqa: E402
import runtime_shell  # noqa: E402

_A1 = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/opencode-aaa"
_A2 = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/opencode-bbb"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Wire config at a tmp file via the real env var; start every test unwired."""
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    for role in runtime_config.ROLES:
        monkeypatch.delenv(runtime_config._env_key(role), raising=False)


def test_no_instance_arn_uses_first_wired():
    runtime_config.save_runtime("opencode", _A1)
    runtime_config.add_runtime("opencode", _A2)
    assert runtime_shell.get_runtime_arn("opencode") == _A1


def test_explicit_instance_arn_selects_that_instance():
    runtime_config.save_runtime("opencode", _A1)
    runtime_config.add_runtime("opencode", _A2)
    # The fleet's SECOND instance, chosen explicitly, is honored.
    assert runtime_shell.get_runtime_arn("opencode", _A2) == _A2


def test_forged_instance_arn_is_rejected():
    runtime_config.save_runtime("opencode", _A1)
    forged = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/opencode-evil"
    assert runtime_shell.get_runtime_arn("opencode", forged) is None


def test_unwired_role_returns_none():
    assert runtime_shell.get_runtime_arn("opencode") is None
    assert runtime_shell.get_runtime_arn("opencode", _A1) is None


class _WarmingShellClient:
    """An AgentCore client whose shell becomes available after transient timeouts."""

    def __init__(self, succeeds_on: int, timeout_type=TimeoutError):
        self.succeeds_on = succeeds_on
        self.timeout_type = timeout_type
        self.calls = []
        self.closed = 0
        self.shell = object()

    def open_shell(self, **kwargs):
        self.calls.append(kwargs)
        client = self
        attempt = len(self.calls)

        class _Context:
            async def __aenter__(self):
                if attempt < client.succeeds_on:
                    raise client.timeout_type("runtime warming")
                return client.shell

            async def __aexit__(self, exc_type, exc, traceback):
                client.closed += 1

        return _Context()


@pytest.mark.parametrize(
    "timeout_type",
    [TimeoutError, asyncio.TimeoutError],
    ids=["builtin-timeout", "asyncio-timeout"],
)
def test_runtime_shell_retries_a_warming_runtime_before_failing(timeout_type):
    """A first WebSocket timeout after READY is retried with a fresh shell id."""
    client = _WarmingShellClient(succeeds_on=3, timeout_type=timeout_type)
    notices = []

    async def _open():
        stack, shell = await runtime_shell._open_shell_when_ready(
            client, _A1, "console-warm0000000000000000000000000000000000",
            retry_delay_s=0, retry_notice=notices.append)
        async with stack:
            return shell

    assert asyncio.run(_open()) is client.shell
    assert len(client.calls) == 3
    assert len({call["shell_id"] for call in client.calls}) == 3
    assert len(notices) == 2
    assert client.closed == 1


def test_open_session_against_chosen_instance_records_that_arn():
    runtime_config.save_runtime("opencode", _A1)
    runtime_config.add_runtime("opencode", _A2)
    out = runtime_shell.open_runtime_session("opencode", instance_arn=_A2)
    try:
        assert "error" not in out
        # The opened session connects to the chosen instance, not the first.
        assert out["runtime_arn"] == _A2
        assert out["agent_id"] == "opencode"
        assert out["session_id"].startswith("console-")
        # session id must be >=33 chars (AgentCore runtimeSessionId requirement).
        assert len(out["session_id"]) >= 33
    finally:
        # The session spawned a background connect thread; drop it from the registry
        # so a later test's registry stays clean (the thread fails loud offline).
        runtime_shell._sessions.pop(out.get("session_id", ""), None)


def test_open_session_forged_instance_fails_loud():
    runtime_config.save_runtime("opencode", _A1)
    forged = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/opencode-evil"
    out = runtime_shell.open_runtime_session("opencode", instance_arn=forged)
    assert "error" in out
    assert "not wired" in out["error"]


# --- F1: orchestrator drives the SAME live session (shared PTY, fan-out) -------
class _FakeShellSession:
    """A live session stand-in: records sends + exposes the shared buffer, with no
    real WebSocket. The orchestrator API only needs agent_id/alive/buffer + send_input."""
    def __init__(self, agent_id, session_id="console-fake000000000000000000000000000000"):
        self.agent_id = agent_id
        self.session_id = session_id
        self.runtime_arn = f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{agent_id}-fake"
        self.opened_by = "user"
        self.busy = False
        self.alive = True
        self.buffer = ""
        self.sent = []
        self._lock = __import__("threading").Lock()
    def emit_banner(self, text):
        with self._lock:
            self.buffer += f"\r\n[orchestrator] {text}\r\n"
    def snapshot(self, max_chars=4000):
        with self._lock:
            return self.buffer[-max_chars:]
    def send_input(self, text):
        self.sent.append(text)
    # the REAL turn-framing/idle methods, exercised against the fake's buffer/sends
    send_turn = runtime_shell.RuntimeShellSession.send_turn
    wait_turn_idle = runtime_shell.RuntimeShellSession.wait_turn_idle


def _register(session):
    with runtime_shell._sessions_lock:
        runtime_shell._sessions[session.session_id] = session


def test_agent_send_requires_a_live_session():
    out = runtime_shell.agent_send("claude-code", "hello")
    assert "error" in out and "No live session" in out["error"]


def test_agent_send_banners_then_submits_into_the_same_session(monkeypatch):
    s = _FakeShellSession("claude-code")
    _register(s)
    try:
        out = runtime_shell.agent_send("claude-code", "what is 2+2?")
        assert out.get("ok") is True
        assert out["session_id"] == s.session_id
        # the human sees a labeled banner in the SAME buffer
        assert "[orchestrator] what is 2+2?" in s.buffer
        # the message body is typed, and Enter is submitted as its OWN keystroke
        assert s.sent[0] == "what is 2+2?"
        assert s.sent[-1] == "\r"
    finally:
        runtime_shell._sessions.pop(s.session_id, None)


def test_agent_read_returns_the_live_screen():
    s = _FakeShellSession("kiro")
    s.buffer = "validator output: 4"
    _register(s)
    try:
        out = runtime_shell.agent_read("kiro")
        assert out["alive"] is True
        assert "validator output: 4" in out["output"]
    finally:
        runtime_shell._sessions.pop(s.session_id, None)


def test_agent_status_reflects_liveness():
    assert runtime_shell.agent_status("opencode")["alive"] is False
    s = _FakeShellSession("opencode")
    _register(s)
    try:
        st = runtime_shell.agent_status("opencode")
        assert st["alive"] is True and st["session_id"] == s.session_id
    finally:
        runtime_shell._sessions.pop(s.session_id, None)


def test_find_session_picks_a_live_one_for_the_agent():
    dead = _FakeShellSession("opencode", "console-dead0000000000000000000000000000000000")
    dead.alive = False
    live = _FakeShellSession("opencode", "console-live0000000000000000000000000000000000")
    _register(dead); _register(live)
    try:
        assert runtime_shell.find_session_for_agent("opencode") is live
    finally:
        runtime_shell._sessions.pop(dead.session_id, None)
        runtime_shell._sessions.pop(live.session_id, None)


# --- launch-size gate: paint the CLI banner at the MEASURED width -------------
def test_launch_gate_starts_closed_and_opens_on_resize():
    """A fresh runtime session does NOT launch the CLI at the 80x24 default: it
    waits on a size gate that the browser's first measured resize releases. The
    real resize() records the size AND sets the gate, so _connect launches at the
    measured width (no wrapped banner). No WebSocket needed to test the seam."""
    s = runtime_shell.RuntimeShellSession(
        "console-gate0000000000000000000000000000000000", "claude-code",
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/claude_code-X")
    # Gate is closed until a measured resize arrives; default size is the 80x24
    # fallback, but the CLI is held until the real size is known.
    assert s._size == (80, 24)
    assert not s._size_event.is_set()
    # A measured resize from the browser records the size and releases the gate.
    s.resize(212, 51)
    assert s._size == (212, 51)
    assert s._size_event.is_set()


# --- muxed dispatch: the engine drives ONE live PTY the human also watches ----
def test_send_turn_uses_bracketed_paste_and_separate_enter():
    """A dispatch turn is framed like a human paste: the body arrives inside
    bracketed-paste markers (so embedded newlines never submit partial prompts),
    and Enter is its OWN keystroke after a beat."""
    s = _FakeShellSession("claude-code")
    s.send_turn("line one\nline two")
    assert s.sent[0] == "\x1b[200~line one\nline two\x1b[201~"
    assert s.sent[-1] == "\r"


def test_wait_turn_idle_returns_when_buffer_goes_quiet():
    """Buffer-idle is the turn boundary: a working TUI repaints continuously, so
    a quiet buffer means the turn finished. A still-writing buffer times out."""
    s = _FakeShellSession("kiro")
    assert s.wait_turn_idle(quiet_s=0.2, timeout_s=2.0, poll_s=0.05) is True

    import threading, time as _t
    busy = _FakeShellSession("kiro", "console-busy0000000000000000000000000000000000")
    stop = _t.monotonic() + 1.0
    def _spam():
        while _t.monotonic() < stop:
            busy.buffer += "."
            _t.sleep(0.05)
    th = threading.Thread(target=_spam, daemon=True); th.start()
    assert busy.wait_turn_idle(quiet_s=0.5, timeout_s=0.7, poll_s=0.05) is False
    th.join()


def test_ensure_dispatch_session_reuses_the_humans_live_session():
    """The engine's dispatch reuses the newest live non-busy session -- the very
    session the human is watching on the Agents page -- so both surfaces share
    one PTY."""
    s = _FakeShellSession("opencode")
    _register(s)
    try:
        got = runtime_shell.ensure_dispatch_session("opencode", instance_arn=s.runtime_arn)
        assert got is s
    finally:
        runtime_shell._sessions.pop(s.session_id, None)


def test_ensure_dispatch_session_skips_busy_sessions():
    """A session already driven by another dispatch is never shared: two prompts
    interleaved into one TUI input box would corrupt both turns. With the only
    session busy and no wired runtime to open a new one, the caller gets None
    (and falls back to the headless one-shot path)."""
    s = _FakeShellSession("opencode")
    s.busy = True
    _register(s)
    try:
        assert runtime_shell.ensure_dispatch_session("opencode") is None
    finally:
        runtime_shell._sessions.pop(s.session_id, None)


def test_list_sessions_reports_owner_and_liveness():
    """The server-side registry is the Agents page's source of truth for tabs:
    it must carry opened_by (user vs orchestrator) and alive."""
    s = _FakeShellSession("claude-code")
    s.opened_by = "orchestrator"
    _register(s)
    try:
        rows = runtime_shell.list_sessions("claude-code")["sessions"]
        mine = [r for r in rows if r["session_id"] == s.session_id]
        assert mine and mine[0]["opened_by"] == "orchestrator"
        assert mine[0]["alive"] is True and mine[0]["busy"] is False
    finally:
        runtime_shell._sessions.pop(s.session_id, None)
