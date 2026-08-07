"""Runtime shell proxy: connects the browser terminal to a REAL AgentCore Runtime.

Instead of spawning a local bash and pretending, this module opens a WebSocket
shell to the deployed runtime via AgentCoreRuntimeClient.open_shell() and proxies
I/O between the browser (via SSE + POST) and the runtime's PTY.

Each agent (claude-code, opencode, kiro) gets connected to its WIRED runtime ARN.
If no ARN is wired, the connection fails loud.

API (mounted by interactive_api.dispatch):
  POST /api/dev/runtime-sessions         {agent_id, cols, rows} -> {session_id}
  POST /api/dev/runtime-sessions/{id}/input  {input}
  POST /api/dev/runtime-sessions/{id}/resize {cols, rows}
  GET  /api/dev/runtime-sessions/{id}/stream  (SSE output)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))

# Agent CLI launch commands (what runs inside the runtime after the shell opens).
# Each container has /app/run.sh, which handles its own authentication and Bedrock
# setup, then launches the CLI in its trusted form so the TUI never stops
# on a first-run "do you trust this directory / tool" prompt:
#   * claude: `--dangerously-skip-permissions` + baked ~/.claude config;
#   * opencode: reads ~/.config/opencode/opencode.json (amazon-bedrock provider),
#             extended idempotently by run.sh at startup (so a
#             stale image that predates the bake still self-heals, silently, with no
#             echo in the captured terminal);
#   * kiro:   `kiro-cli chat --trust-all-tools` (no trust config key exists; verified
#             against kiro-cli 2.7.0).
# The launch is kept to a single clean `/app/run.sh` line on purpose: all of the trust
# setup lives inside run.sh, where it runs without echoing to the PTY, so the captured
# terminal stays clean.
_AGENT_LAUNCH = {
    "claude-code": "/app/run.sh\n",
    # The validator is a second Claude Code container, so it launches identically.
    "claude-code-validator": "/app/run.sh\n",
    "opencode": "/app/run.sh\n",
    "kiro": "/app/run.sh\n",
}

_SHELL_OPEN_ATTEMPTS = 6
_SHELL_OPEN_RETRY_DELAY_S = 5.0


async def _open_shell_when_ready(client: Any, runtime_arn: str, session_id: str,
                                 *, retry_delay_s: float = _SHELL_OPEN_RETRY_DELAY_S,
                                 retry_notice: Any = None) -> tuple[AsyncExitStack, Any]:
    """Open a command shell after an AgentCore Runtime has finished warming."""
    for attempt in range(1, _SHELL_OPEN_ATTEMPTS + 1):
        stack = AsyncExitStack()
        try:
            shell = await stack.enter_async_context(client.open_shell(
                runtime_arn=runtime_arn,
                session_id=session_id,
                shell_id=str(uuid.uuid4()),
            ))
        # Python 3.10 keeps asyncio.TimeoutError distinct from the built-in
        # TimeoutError. Workshop hosts still use that runtime, and websockets
        # raises the asyncio variant when the opening handshake times out.
        except (TimeoutError, asyncio.TimeoutError, OSError) as error:
            await stack.aclose()
            if attempt == _SHELL_OPEN_ATTEMPTS:
                raise RuntimeError(
                    f"Runtime did not accept a command shell after "
                    f"{_SHELL_OPEN_ATTEMPTS} attempts."
                ) from error
            if retry_notice:
                retry_notice(
                    f"Runtime is still warming (attempt {attempt}/"
                    f"{_SHELL_OPEN_ATTEMPTS}); retrying in {retry_delay_s:g}s."
                )
            await asyncio.sleep(retry_delay_s)
        else:
            return stack, shell

    raise AssertionError("command-shell retry loop exhausted unexpectedly")


class RuntimeShellSession:
    """A live WebSocket shell to a deployed AgentCore Runtime."""

    def __init__(self, session_id: str, agent_id: str, runtime_arn: str,
                 user_id: str = "unknown", opened_by: str = "user",
                 launch_command: str | None = None,
                 run_subdir: str = ""):
        self.session_id = session_id
        self.agent_id = agent_id
        self.runtime_arn = runtime_arn
        self.user_id = user_id
        # An orchestrated session is still a REAL interactive PTY: the engine and
        # every Agents-page subscriber share one shell and can both send input.
        # The metadata only lets the UI distinguish an automatic run tab from a
        # terminal the person opened with +.
        self.opened_by = opened_by
        self.run_subdir = run_subdir
        self.busy = opened_by == "orchestrator"
        # Run dispatches supply a command that hydrates their isolated local
        # worktree before launching the native TUI. Human-opened terminals retain
        # the ordinary /app/run.sh launch.
        self._launch_command = launch_command
        self.started_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        self.buffer = ""
        self.alive = True
        self._shell = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._output_callbacks: list = []
        self._lock = threading.Lock()
        # Launch-size gate (don't paint the CLI banner at a guessed width). The PTY
        # is opened, but the agent CLI is NOT launched until the browser's MEASURED
        # winsize arrives (the first resize), so the banner paints at the real
        # terminal width instead of the 80x24 default and never wraps mid-border.
        # A short fallback timeout launches anyway if no resize comes (e.g. a
        # headless/agent-only consumer), so a connection never hangs un-launched.
        self._size = (80, 24)            # latest requested (cols, rows)
        self._size_event = threading.Event()  # set once a real resize is seen

    def start(self, cols: int = 80, rows: int = 24):
        """Start the runtime connection in a background thread."""
        self._thread = threading.Thread(
            target=self._run_loop, args=(cols, rows), daemon=True)
        self._thread.start()

    def _run_loop(self, cols: int, rows: int):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect(cols, rows))
        except Exception as e:
            self._emit(f"\r\n\x1b[31mRuntime connection error: {e}\x1b[0m\r\n")
            self.alive = False

    async def _connect(self, cols: int, rows: int):
        from bedrock_agentcore.runtime import AgentCoreRuntimeClient
        from bedrock_agentcore.runtime.shell import ShellChannel

        client = AgentCoreRuntimeClient(region=REGION)
        stack, shell = await _open_shell_when_ready(
            client,
            self.runtime_arn,
            self.session_id,
            retry_notice=lambda message: self._emit(
                f"\r\n\x1b[33m{message}\x1b[0m\r\n"
            ),
        )
        async with stack:
            self._shell = shell
            self._size = (cols, rows)

            # Wait for the browser's MEASURED winsize before launching the CLI, so
            # the banner paints at the real terminal width (never the 80-col default
            # that wraps the box-drawing borders and reads as a mock). The frontend
            # sends a resize the moment its xterm mounts; we give it up to 1.5s, then
            # launch with whatever size we have (headless/agent-only consumers never
            # resize, so the fallback keeps them from hanging un-launched).
            await asyncio.get_event_loop().run_in_executor(
                None, self._size_event.wait, 1.5)
            cols, rows = self._size
            await shell.resize(cols, rows)

            # Auto-launch at the measured width. A dispatched command hydrates its
            # own run-local worktree and then execs the native interactive CLI;
            # a manually opened terminal uses the ordinary image launcher.
            launch = (self._launch_command
                      or _AGENT_LAUNCH.get(self.agent_id, "/bin/bash\n"))
            await shell.send(launch.rstrip("\n") + "\n")

            async for frame in shell:
                if frame.channel == ShellChannel.STDOUT:
                    self._emit(frame.text if hasattr(frame, 'text') else frame.payload.decode('utf-8', errors='replace'))
                elif frame.channel == ShellChannel.STDERR:
                    self._emit(frame.text if hasattr(frame, 'text') else frame.payload.decode('utf-8', errors='replace'))
                elif frame.channel in (ShellChannel.STATUS, ShellChannel.CLOSE):
                    break

        self.alive = False
        self._emit("\r\n\x1b[33m[session ended]\x1b[0m\r\n")

    def close(self) -> None:
        """Tear the PTY down for good: mark it dead and stop its event loop so the
        ``async for`` in ``_connect`` exits and the WebSocket closes. Idempotent.
        The registry entry is dropped by ``close_runtime_session``; marking
        ``alive=False`` here also makes ``list_sessions`` report it gone, so the
        browser's server-registry sync will NOT resurrect a tab the human closed."""
        self.alive = False
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

    def emit_banner(self, text: str) -> None:
        """Inject a dim, labeled line into the SAME buffer the human's terminal
        streams, so an explicit interactive-tool turn shows up inline as e.g.
        ``[orchestrator] build the server`` without looking like the human typed
        it. Fan-out (one PTY, many subscribers) means the human sees it live."""
        self._emit(f"\r\n\x1b[2m[orchestrator] {text}\x1b[0m\r\n")

    def wait_ready(self, timeout_s: float = 120.0,
                   settle_s: float = 3.0) -> bool:
        """Wait until the WebSocket and TUI are ready to receive keystrokes."""
        import time as _t
        deadline = _t.monotonic() + timeout_s
        while _t.monotonic() < deadline:
            if not self.alive:
                return False
            if self._shell is not None and self.buffer:
                break
            _t.sleep(0.3)
        else:
            return False
        return self.wait_turn_idle(
            quiet_s=settle_s,
            timeout_s=max(5.0, deadline - _t.monotonic()))

    def send_turn(self, text: str) -> None:
        """Paste a multiline orchestrator turn into the same TUI a human sees."""
        import time as _t
        body = text.rstrip("\r\n")
        self.send_input("\x1b[200~" + body + "\x1b[201~")
        _t.sleep(0.5)
        self.send_input("\r")

    def wait_turn_idle(self, quiet_s: float = 20.0,
                       timeout_s: float = 900.0,
                       poll_s: float = 0.5) -> bool:
        """A working TUI repaints while busy; a quiet input prompt is done."""
        import time as _t
        deadline = _t.monotonic() + timeout_s
        last_len = len(self.buffer)
        quiet_since = _t.monotonic()
        while _t.monotonic() < deadline:
            if not self.alive:
                return True
            current = len(self.buffer)
            now = _t.monotonic()
            if current != last_len:
                last_len = current
                quiet_since = now
            elif now - quiet_since >= quiet_s:
                return True
            _t.sleep(poll_s)
        return False

    def snapshot(self, max_chars: int = 4000) -> str:
        """A thread-safe tail of the shared buffer (the current screen as text).
        The orchestrator reads this after a turn to see what the agent replied."""
        with self._lock:
            return self.buffer[-max_chars:]

    def _emit(self, text: str):
        with self._lock:
            self.buffer += text
            for cb in self._output_callbacks:
                try:
                    cb(text)
                except Exception:
                    pass

    def send_input(self, text: str):
        if self._shell and self._loop and self.alive:
            asyncio.run_coroutine_threadsafe(
                self._shell.send(text), self._loop)

    def send_bytes(self, data: bytes):
        if self._shell and self._loop and self.alive:
            asyncio.run_coroutine_threadsafe(
                self._shell.send_bytes(data), self._loop)

    def resize(self, cols: int, rows: int):
        # Record the measured size and release the launch gate, so a resize that
        # arrives BEFORE the CLI launches sets the width the banner paints at. A
        # resize AFTER launch still reflows the live shell (the send below).
        self._size = (cols, rows)
        self._size_event.set()
        if self._shell and self._loop and self.alive:
            asyncio.run_coroutine_threadsafe(
                self._shell.resize(cols, rows), self._loop)

    def subscribe(self, cb) -> None:
        with self._lock:
            self._output_callbacks.append(cb)

    def unsubscribe(self, cb) -> None:
        with self._lock:
            try:
                self._output_callbacks.remove(cb)
            except ValueError:
                pass


# Global session registry
_sessions: dict[str, RuntimeShellSession] = {}
_sessions_lock = threading.Lock()


def _role_instances(agent_id: str) -> list[str]:
    """Every wired ARN for an agent, fleet order, both id spellings."""
    try:
        import runtime_config
        hits = runtime_config.instances(agent_id)
        if not hits:
            hits = runtime_config.instances(agent_id.replace("-", "_"))
        if hits:
            return [arn for arn, _src in hits]
    except Exception:
        pass
    env_key = f"AGENTCORE_RUNTIME_{agent_id.upper().replace('-', '_')}"
    raw = os.environ.get(env_key)
    return [a.strip() for a in raw.split(",") if a.strip()] if raw else []


def get_runtime_arn(agent_id: str, instance_arn: str | None = None) -> str | None:
    """Get the runtime ARN to connect to for an agent.

    With ``instance_arn`` the caller is choosing WHICH wired instance to open a
    session against (a fleet of N has more than one); we honor it only when it is
    actually one of the role's wired instances, so a stale/forged ARN can never
    reach the runtime. With no instance_arn (the single-instance case) we return
    the first wired ARN, the prior behavior."""
    wired = _role_instances(agent_id)
    if instance_arn:
        return instance_arn if instance_arn in wired else None
    return wired[0] if wired else None


def open_runtime_session(agent_id: str, cols: int = 80, rows: int = 24,
                         instance_arn: str | None = None,
                         user_id: str = "unknown", opened_by: str = "user",
                         launch_command: str | None = None,
                         run_subdir: str = "") -> dict:
    """Open a real runtime shell session. Fails loud if no ARN wired (or if a
    requested instance is not one of the role's wired instances)."""
    arn = get_runtime_arn(agent_id, instance_arn)
    if not arn:
        if instance_arn:
            return {"error": f"Instance {instance_arn} is not wired for {agent_id}."}
        return {"error": f"No runtime wired for {agent_id}. Wire an ARN in Settings first."}
    # The interactive terminal is a WebSocket command-shell (open_shell), which only
    # a DEPLOYED runtime ARN can host. A local `agentcore dev` URL serves HTTP
    # /invocations (request/response) for the orchestrator's dispatch, not a PTY, so
    # we say that plainly instead of letting the SDK raise a cryptic "Invalid runtime
    # ARN format".
    if arn.startswith("http://") or arn.startswith("https://"):
        return {"error": (
            "The interactive terminal needs a deployed AgentCore runtime ARN. "
            f"{arn} is a local dev URL, which serves the orchestrator over HTTP, not "
            "an interactive shell. Wire a deployed ARN (agentcore deploy) to open a terminal.")}

    session_id = f"console-{uuid.uuid4().hex}{uuid.uuid4().hex[:4]}"
    session = RuntimeShellSession(
        session_id, agent_id, arn, user_id=user_id, opened_by=opened_by,
        launch_command=launch_command, run_subdir=run_subdir)
    session.start(cols, rows)

    with _sessions_lock:
        _sessions[session_id] = session

    return {"session_id": session_id, "agent_id": agent_id, "runtime_arn": arn,
            "opened_by": opened_by, "run_subdir": run_subdir}


def get_session(session_id: str) -> RuntimeShellSession | None:
    with _sessions_lock:
        return _sessions.get(session_id)


def close_runtime_session(session_id: str) -> dict:
    """Close a session and DROP it from the registry, so the human closing a tab
    truly ends the PTY (stops billing the microVM session) and the browser's
    server-registry sync cannot resurrect it. Idempotent: closing an unknown or
    already-closed id is a no-op success."""
    with _sessions_lock:
        s = _sessions.pop(session_id, None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    return {"ok": True, "closed": s is not None}


def find_session_for_agent(agent_id: str) -> RuntimeShellSession | None:
    """The newest LIVE session for an agent, so the orchestrator can reach the
    SAME PTY the human is watching (fan-out: one shell, both subscribe). Returns
    None when no live session is open for that agent."""
    with _sessions_lock:
        live = [s for s in _sessions.values() if s.agent_id == agent_id and s.alive]
    return live[-1] if live else None


def list_sessions(agent_id: str | None = None) -> dict:
    """Every interactive terminal registered by the console.

    Both human-opened terminals and run-local orchestrator PTYs appear. The latter
    expose the native TUI and accept the same human input as a manually opened
    terminal, while source/result exchange remains isolated per work item.
    """
    with _sessions_lock:
        rows = [
            {"session_id": s.session_id, "agent_id": s.agent_id,
             "runtime_arn": s.runtime_arn, "alive": s.alive,
             "opened_by": getattr(s, "opened_by", "user"),
             "busy": getattr(s, "busy", False),
             "run_subdir": getattr(s, "run_subdir", ""),
             "user_id": getattr(s, "user_id", "unknown"),
             "started_at": getattr(s, "started_at", ""),
             "buffer_chars": len(s.buffer)}
            for s in _sessions.values()
            if agent_id is None or s.agent_id == agent_id
        ]
    return {"sessions": rows}


def ensure_dispatch_session(agent_id: str, instance_arn: str,
                            launch_command: str, run_subdir: str,
                            user_id: str = "unknown") -> RuntimeShellSession | None:
    """Open one fresh, registered PTY for an isolated orchestrated work item."""
    out = open_runtime_session(
        agent_id, cols=120, rows=32, instance_arn=instance_arn,
        user_id=user_id, opened_by="orchestrator",
        launch_command=launch_command, run_subdir=run_subdir)
    if "error" in out:
        return None
    return get_session(out["session_id"])


# --- Explicit interactive tools <-> live PTY (server fan-out) ----------------
# An interactive tool drives the SAME RuntimeShellSession the human opened: it emits
# a labeled banner (visible in the human's terminal), sends the turn into the one
# PTY, and reads the buffer back. Both see the same screen; no second WebSocket,
# so no kick (the SDK forbids two clients on one shell_id).
def agent_send(agent_id: str, text: str) -> dict:
    """Send one orchestrator turn into the agent's live PTY. The turn is announced
    in the shared buffer (the human sees ``[orchestrator] <text>``), then typed into
    the shell. A coding-agent TUI takes the message text and the Enter as SEPARATE
    keystrokes (type, brief pause, submit), so we send the body, wait a beat, then
    send a carriage return; otherwise the line sits unsubmitted in the input box.
    Fails loud if no live session is open for the agent."""
    import time as _t
    s = find_session_for_agent(agent_id)
    if not s:
        return {"error": f"No live session for {agent_id}. Open the agent's terminal first."}
    body = text.rstrip("\r\n")
    s.emit_banner(body)
    s.send_input(body)      # type the message
    _t.sleep(0.4)           # let the TUI register the input line
    s.send_input("\r")      # submit (Enter as its own keystroke)
    return {"ok": True, "session_id": s.session_id, "agent_id": agent_id}


def agent_read(agent_id: str, max_chars: int = 4000) -> dict:
    """Read the current screen (a tail of the shared PTY buffer) for an agent, so
    the orchestrator can see what the agent replied to its last turn."""
    s = find_session_for_agent(agent_id)
    if not s:
        return {"error": f"No live session for {agent_id}."}
    return {"agent_id": agent_id, "session_id": s.session_id,
            "alive": s.alive, "output": s.snapshot(max_chars)}


def agent_status(agent_id: str) -> dict:
    """Whether the agent has a live PTY the orchestrator can talk to."""
    s = find_session_for_agent(agent_id)
    if not s:
        return {"agent_id": agent_id, "alive": False, "session_id": None}
    return {"agent_id": agent_id, "alive": s.alive, "session_id": s.session_id,
            "buffer_chars": len(s.buffer)}


def send_input(session_id: str, text: str) -> dict:
    s = get_session(session_id)
    if not s:
        return {"error": "session not found"}
    s.send_input(text)
    return {"ok": True}


def resize(session_id: str, cols: int, rows: int) -> dict:
    s = get_session(session_id)
    if not s:
        return {"error": "session not found"}
    s.resize(cols, rows)
    return {"ok": True}


async def stream_output(session_id: str):
    """Async generator yielding SSE frames. Non-blocking so uvicorn stays responsive."""
    import asyncio
    s = get_session(session_id)
    if not s:
        yield f"data: {json.dumps({'error': 'session not found'})}\n\n"
        return

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def on_output(text: str):
        loop.call_soon_threadsafe(q.put_nowait, text)

    if s.buffer:
        yield f"data: {json.dumps({'output': s.buffer})}\n\n"

    s.subscribe(on_output)
    try:
        while s.alive:
            try:
                text = await asyncio.wait_for(q.get(), timeout=2.0)
                yield f"data: {json.dumps({'output': text})}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        s.unsubscribe(on_output)
    yield "event: end\ndata: {}\n\n"
