#!/usr/bin/env python3
"""Watch the agents work, live, from a terminal.

The console already multiplexes each role's real Runtime PTY: ONE session, many
subscribers, so the human and the orchestrator watch the same shell. That fan-out
was only reachable from the browser. This attaches a TERMINAL to the very same
sessions, so an attendee who lives in the VS Code terminal sees what is being
built as it happens instead of waiting for a status line.

    python3 orchestrator/watch_agents.py                 # follow every role
    python3 orchestrator/watch_agents.py --agent opencode
    python3 orchestrator/watch_agents.py --plain          # no colour/ANSI

It is a READ-ONLY subscriber: it opens no session, sends no input, and closes
nothing, so watching can never disturb a run. If no session is live it says so
and keeps waiting, because the interesting moment is usually the one just before
a dispatch starts.

Stdlib only (urllib), because it runs on the workshop box with no extra install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("WORKSHOP_CONSOLE_URL", "http://127.0.0.1:8081")
_POLL_S = 2.0

# One colour per role, so interleaved output stays readable at a glance.
_COLOURS = ("\033[36m", "\033[35m", "\033[32m", "\033[33m", "\033[34m")
_RESET = "\033[0m"
_DIM = "\033[2m"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")


def _get(url: str, cookie: str | None, timeout: float = 10.0):
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _clean(text: str, plain: bool) -> str:
    """Readable lines from raw PTY bytes.

    A TUI repaints with cursor moves and colour, which is right for a terminal
    emulator and noise in a follow view. Strip the control sequences, drop the
    empty repaints, and keep the words.
    """
    if not plain:
        return text
    return _ANSI.sub("", text)


class _Follower(threading.Thread):
    """Follow ONE session's SSE stream and print its lines with a role prefix."""

    daemon = True

    def __init__(self, base: str, cookie: str | None, session: dict,
                 colour: str, plain: bool, lock: threading.Lock):
        super().__init__(name=f"follow-{session.get('session_id','?')}")
        self.base = base
        self.cookie = cookie
        self.sid = session.get("session_id", "")
        self.agent = session.get("agent_id", "?")
        self.opened_by = session.get("opened_by", "")
        self.colour = "" if plain else colour
        self.plain = plain
        self.lock = lock
        self.stop = threading.Event()

    def _emit(self, chunk: str) -> None:
        text = _clean(chunk, self.plain)
        if not text.strip():
            return
        tag = f"{self.colour}[{self.agent}]{'' if self.plain else _RESET} "
        with self.lock:
            for line in text.splitlines():
                if line.strip():
                    sys.stdout.write(tag + line + "\n")
            sys.stdout.flush()

    def run(self) -> None:
        url = f"{self.base}/api/dev/runtime-sessions/{self.sid}/stream"
        req = urllib.request.Request(url)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=None) as r:
                for raw in r:
                    if self.stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if not line.startswith("data:"):
                        continue          # ": ping" keepalive / "event:" markers
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("output"):
                        self._emit(obj["output"])
                    if obj.get("alive") is False:
                        return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            with self.lock:
                sys.stdout.write(f"{_DIM}[{self.agent}] stream ended ({exc}){_RESET}\n")
                sys.stdout.flush()


def watch(base: str, agent: str | None, plain: bool, cookie: str | None,
          once: bool = False) -> int:
    """Attach to every live role session and follow them until interrupted."""
    lock = threading.Lock()
    followers: dict[str, _Follower] = {}
    colours = list(_COLOURS)
    print(f"{_DIM}watching {base} ; Ctrl-C to stop (read-only: this never types "
          f"into a session){_RESET}")
    said_empty = False
    try:
        while True:
            try:
                url = f"{base}/api/dev/runtime-sessions"
                if agent:
                    url += f"?agent_id={agent}"
                sessions = _get(url, cookie).get("sessions", [])
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    print("unauthorized: the console requires a login. Open the "
                          "console once in the browser, or pass "
                          "--cookie \"session=...\".", file=sys.stderr)
                    return 2
                print(f"console error: {exc}", file=sys.stderr)
                return 2
            except (urllib.error.URLError, OSError) as exc:
                print(f"cannot reach the console at {base}: {exc}\n"
                      "Start it with: sudo systemctl start stage2-console",
                      file=sys.stderr)
                return 2

            for s in sessions:
                sid = s.get("session_id")
                if not sid or sid in followers:
                    continue
                colour = colours[len(followers) % len(colours)]
                f = _Follower(base, cookie, s, colour, plain, lock)
                followers[sid] = f
                with lock:
                    who = s.get("opened_by") or "human"
                    print(f"{_DIM}+ attached to {s.get('agent_id')} "
                          f"(session {sid[:8]}, opened by {who}){_RESET}")
                f.start()

            if not sessions and not said_empty:
                with lock:
                    print(f"{_DIM}no live agent session yet; waiting. Submit a "
                          f"build and the roles appear here as they start.{_RESET}")
                said_empty = True
            elif sessions:
                said_empty = False

            if once:
                return 0
            time.sleep(_POLL_S)
    except KeyboardInterrupt:
        for f in followers.values():
            f.stop.set()
        print(f"\n{_DIM}detached (the sessions keep running){_RESET}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Follow the coding agents' live Runtime shells in a terminal.")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"console base URL (default {DEFAULT_BASE})")
    ap.add_argument("--agent", default=None,
                    help="only this role id (default: every role)")
    ap.add_argument("--plain", action="store_true",
                    help="strip ANSI/TUI control sequences and colour")
    ap.add_argument("--cookie", default=os.environ.get("WORKSHOP_CONSOLE_COOKIE"),
                    help="Cookie header when the console requires login")
    ap.add_argument("--once", action="store_true",
                    help="attach to what is live now, then return (for tests)")
    a = ap.parse_args(argv)
    return watch(a.base.rstrip("/"), a.agent, a.plain, a.cookie, a.once)


if __name__ == "__main__":
    sys.exit(main())
