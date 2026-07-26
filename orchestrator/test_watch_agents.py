"""The terminal attaches to the SAME muxed session the console shows.

`watch_agents.py` is a read-only subscriber to the console's existing PTY
fan-out, so an attendee working in the VS Code terminal can watch the agents
build instead of waiting on a status line. These tests pin the two properties
that make it safe and useful: it never writes to a session, and it renders the
role's real output.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import sys
import threading
import time
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLI = os.path.join(_HERE, "watch_agents.py")

_SESSIONS = {"sessions": [
    {"session_id": "s-backend", "agent_id": "claude-code", "alive": True,
     "opened_by": "orchestrator"},
    {"session_id": "s-frontend", "agent_id": "opencode", "alive": True,
     "opened_by": "human"},
]}


class _Console(http.server.BaseHTTPRequestHandler):
    """The two routes the watcher uses, with the SHIPPED response shapes."""

    frames = [
        'data: {"output": "\\u001b[36mBuilding\\u001b[0m\\r\\n"}\n\n',
        'data: {"output": "wrote server.py\\n"}\n\n',
    ]
    writes: list[str] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if "/stream" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": open\n\n")
            for f in self.frames:
                try:
                    self.wfile.write(f.encode())
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.05)
            time.sleep(2)
            return
        if self.path.startswith("/api/dev/runtime-sessions"):
            body = json.dumps(_SESSIONS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        # Any POST here means the watcher tried to TYPE into or open a session.
        type(self).writes.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_DELETE = do_POST


def _serve():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Console)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _run(port, args=(), settle=2.0):
    p = subprocess.Popen(
        [sys.executable, _CLI, "--base", f"http://127.0.0.1:{port}", "--plain", *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(settle)
    p.terminate()
    try:
        out, _ = p.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out


def test_follows_every_role_and_prefixes_its_output():
    _Console.writes = []
    srv, port = _serve()
    try:
        out = _run(port)
    finally:
        srv.shutdown()
    assert "attached to claude-code" in out, out
    assert "attached to opencode" in out, out
    # The role's real output, tagged so interleaved roles stay readable.
    assert "[claude-code] wrote server.py" in out, out
    # --plain strips the TUI control sequences but keeps the words.
    assert "Building" in out and "\x1b[36m" not in out, out


def test_is_strictly_read_only():
    """Watching must never disturb a run: no POST, no DELETE, ever."""
    _Console.writes = []
    srv, port = _serve()
    try:
        _run(port)
    finally:
        srv.shutdown()
    assert _Console.writes == [], (
        "the watcher wrote to the console; it must be a read-only subscriber so "
        f"watching can never type into a live agent session: {_Console.writes}")


def test_says_how_to_start_the_console_when_it_is_down():
    """A refused connection is the common case; it must name the fix."""
    out = _run(9, settle=1.5)          # port 9 (discard) refuses
    assert "cannot reach the console" in out, out
    assert "systemctl start stage2-console" in out, out
