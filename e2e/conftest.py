"""Shared fixtures + helpers for the workshop end-to-end suite.

Every e2e test drives the SAME real `console/server.py` process over HTTP, on
the same `/api/dev|orchestrator|metrics` mounts an attendee's browser hits, behind the
same cookie login wall the CFN systemd unit runs. If a test here breaks, a real
attendee action in the console is broken.

The whole suite shares ONE server (session-scoped) so 200+ tests stay fast: boot
once, log in once, reuse the cookie. The server is the TEST entrypoint
(`console/test_server.py`), which serves the real app but rebinds the Stage-2
engine to the deterministic FixtureExecutor (builders, no model, no live AWS); so
"done" is the same `pytest` acceptance gate the workshop grades with, never an LLM.
Set `WORKSHOP_E2E_LIVE=1` to additionally run the live-model tests (skipped by
default).

These fixtures/helpers are the framework the per-stage modules
(`test_stage1_*`, `test_stage2_*`, `test_stage3_*`, `test_journey_*`) build on.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.error import HTTPError

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The e2e suite boots the TEST entrypoint (console/test_server.py), not the shipped
# server.py. It serves the exact same FastAPI app + login wall, but rebinds the
# Stage-2 engine to the deterministic FixtureExecutor (builders, no model, no live
# AWS) BEFORE serving; the shipped server.py stays real-only (dispatch to deployed
# runtimes, fail loud on a missing ARN). A subprocess can't take a constructor arg,
# so a test-only entrypoint is how the fixture engine reaches the e2e console
# WITHOUT an env flag selecting a fake on the shipped binary.
_SERVER = os.path.join(_REPO, "console", "test_server.py")

# An EMPTY coding-agents dir for the suite: the Stage-1 shelf reconciles the real
# runtime_config.json each harness deploy.py writes here, so an empty dir = an empty
# shelf (no agent deployed). Shared with the server subprocess via the env var the
# fixture sets. deploy_real() writes the real-ARN config a genuine deploy.py would.
CODING_AGENTS_DIR = tempfile.mkdtemp(prefix="e2e-coding-agents-")

# A throwaway GitHub credential store so an e2e server NEVER reads a developer's real
# wired PAT (.runs/github.local.json) and opens REAL pull requests. github.py reads
# WORKSHOP_GITHUB_SETTINGS at module load by design (real-seam isolation). Every e2e
# server MUST launch with isolated_server_env() so the GitHub state is the honest
# "not connected" the tests assert and no run can leak a PR. (The console subprocess
# does not load orchestrator/conftest.py, so it must be isolated here.)
# NOTE: we isolate only the CREDENTIAL file, NOT WORKSHOP_RUNS_DIR; the journey
# tests read the composed deliverable + critique under the default .runs/work and
# .runs/composed; relocating the runs dir would break those path assertions. With no
# credential resolved, compose stays local under .runs and pr_url is null, exactly
# the honest no-GitHub path under test.
_GH_ISOLATE_DIR = tempfile.mkdtemp(prefix="e2e-gh-isolate-")


def isolated_server_env(port: int, **extra) -> dict:
    """The base env for an e2e console server: deterministic engine, empty shelf, and
    a GitHub credential store pointed at an empty tmp file so no real PR can open.
    Pass CONSOLE_USER/CONSOLE_PASSWORD (or anything else) via **extra."""
    env = {**os.environ,
           "CONSOLE_PORT": str(port),
           "WORKSHOP_CODING_AGENTS_DIR": CODING_AGENTS_DIR,
           # GitHub isolation, never touch the dev's real wired connection:
           "WORKSHOP_GITHUB_STORE": "local",
           "WORKSHOP_GITHUB_SETTINGS": os.path.join(_GH_ISOLATE_DIR, "github.local.json"),
           # Gateway auto-discovery -> empty tmp file, so an e2e server never reads a
           # REAL deployed gateway's .deployed-state.json and opens a real PR.
           "WORKSHOP_GATEWAY_STATE": os.path.join(_GH_ISOLATE_DIR, "gateway-state.json"),
           # Runtime-ARN isolation, point the wirable-ARN surface at an empty tmp
           # file so the suite NEVER reads the dev's real .runs/runtime.local.json
           # (which would make "every role unwired by default" false). Tests that
           # wire/unwire write to this same throwaway file.
           "WORKSHOP_RUNTIME_CONFIG": os.path.join(_GH_ISOLATE_DIR, "runtime.local.json"),
           # The shared /mnt/s3files agent home a `dev` session opens on: point it at a
           # fresh empty dir so the Development workspace starts BLANK (the empty-mount
           # contract), never the dev box's real mount or a leaked shared home.
           "WORKSHOP_S3FILES_DIR": os.path.join(_GH_ISOLATE_DIR, "s3files-home"),
           **extra}
    env.pop("GITHUB_TOKEN", None)            # force the honest no-credential state
    env.pop("GITHUB_REPO", None)
    env.pop("GITHUB_GATEWAY_URL", None)      # no gateway wired -> honest not-connected
    # Drop any per-role AGENTCORE_RUNTIME_<ROLE> env so the wired view starts clean.
    for k in [k for k in env if k.startswith("AGENTCORE_RUNTIME_")]:
        env.pop(k, None)
    return env

# The cost fixture for Stage 1->2->3: an m5.large priced at
# $0.096/hr * 730h * 2 instances. Every grading contract asserts this exact value.
EC2_FIXTURE_COST = 140.16
LGTM_TOKEN = "LGTM: no changes needed"
SUPPORTED_AGENTS = ("claude-code", "claude-code-validator", "opencode")
TERMINAL_STATUSES = ("passed", "failed", "needs_human")


# ---------------------------------------------------------------------------
# Low-level HTTP (same-origin, exactly what the console's fetch() does).
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(base: str, method: str, path: str, body: dict | None = None,
        headers: dict | None = None, raw: bool = False, timeout: int = 60):
    """One same-origin call. Returns (status, json|bytes); raises HTTPError on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        payload = resp.read()
        return resp.status, (payload if raw else json.loads(payload or b"{}"))


def expect_status(fn, code: int):
    """Call fn() and assert it raises HTTPError with the given status. Returns the body."""
    try:
        fn()
    except HTTPError as e:
        assert e.code == code, f"expected {code}, got {e.code}"
        try:
            return json.loads(e.read() or b"{}")
        except (ValueError, OSError):
            return {}
    raise AssertionError(f"expected HTTP {code}, but the call succeeded")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow the login 302 to /console/ (nginx owns that prefix in prod). The
    Set-Cookie we assert on lives on the 302 itself."""
    def redirect_request(self, *a, **kw):
        return None


def login(base: str, username: str, password: str):
    """POST the urlencoded login form. Returns (status, set_cookie, html_bytes)."""
    body = f"username={username}&password={password}"
    r = urllib.request.Request(base + "/login", data=body.encode(),
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(r, timeout=10)
        return resp.status, resp.headers.get("Set-Cookie", ""), resp.read()
    except HTTPError as e:
        return e.code, e.headers.get("Set-Cookie", ""), e.read()


# ---------------------------------------------------------------------------
# Session-scoped real server + attendee cookie.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def console():
    """One real console server for the whole suite, exactly as the CFN systemd unit
    runs it: login wall ON (CONSOLE_PASSWORD) and the deterministic local engine.

    The Stage-1 shelf reconciles the REAL ``runtime_config.json`` each harness's
    ``deploy.py`` writes under ``WORKSHOP_CODING_AGENTS_DIR``. We point that at an
    EMPTY tmp dir (``CODING_AGENTS_DIR``, shared with the test process over the
    filesystem) so the shelf starts genuinely empty; a test simulates a real deploy
    by writing the real-ARN config there (``deploy_real``). The AWS
    ``CreateAgentRuntime`` call is the only thing absent: the same external-boundary
    discipline the orchestrator's conftest uses for GitHub."""
    port = _free_port()
    env = isolated_server_env(port, CONSOLE_USER="ubuntu", CONSOLE_PASSWORD="attendee-pass")
    # start_new_session=True puts the server in its OWN process group, so teardown
    # can kill the WHOLE tree (uvicorn + every mcp_server.py the engine booted),
    # not just the parent. A bare proc.terminate() leaves the engine's replay
    # servers re-parented to init = orphaned, which piled up and wedged the box.
    proc = subprocess.Popen([sys.executable, _SERVER], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            req(base, "GET", "/api/health", timeout=5)
            break
        except OSError:
            time.sleep(0.2)
    else:
        _kill_process_group(proc)
        pytest.fail("console server never came up")
    yield base
    _kill_process_group(proc)


def _kill_process_group(proc) -> None:
    """Terminate the server AND every child it spawned (the engine's mcp_server.py
    replay servers), by signalling the whole process group. SIGTERM first for a
    clean shutdown (lets the server's atexit reap its own children), then SIGKILL
    the group as a backstop, so nothing is left orphaned to init."""
    import signal  # noqa: PLC0415
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        pgid = None
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    except (OSError, ProcessLookupError):
        pass
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)  # reap any survivors in the group
        except (OSError, ProcessLookupError):
            pass


@pytest.fixture(scope="session")
def cookie(console):
    """The attendee's signed-in session cookie. Every console call rides this."""
    status, set_cookie, _ = login(console, "ubuntu", "attendee-pass")
    assert "console_session=" in set_cookie, "login must set the session cookie"
    return {"Cookie": set_cookie.split(";")[0]}


# ---------------------------------------------------------------------------
# Stage 1 helpers: sessions, the PTY, the file explorer.
# ---------------------------------------------------------------------------
def _fixture_arn(agent_id: str) -> str:
    """A real-shaped AgentCore runtime ARN for the given harness, matching what a
    genuine CreateAgentRuntime returns (account/region from the real infra)."""
    rid = agent_id.replace("-", "_") + "-E2E0001cap"
    return f"arn:aws:bedrock-agentcore:us-west-2:269550163595:runtime/{rid}"


def deploy_real(console, cookie, agent_id: str = "claude-code", tries: int = 50) -> dict:
    """Simulate a REAL Stage-1 deploy: write the exact ``runtime_config.json`` the
    harness ``deploy.py`` produces into the wired coding-agents dir, then poll the
    shelf until the console's REAL reconciliation flips the agent to ``ready`` with
    that genuine ARN. The only thing absent vs. a live attendee is the AWS
    CreateAgentRuntime call itself (the external boundary); the reconciliation code
    under test runs for real. Returns the ready agent record."""
    arn = _fixture_arn(agent_id)
    d = os.path.join(CODING_AGENTS_DIR, agent_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "runtime_config.json"), "w", encoding="utf-8") as f:
        json.dump({"agent_name": agent_id.replace("-", "_"),
                   "runtime_id": arn.rsplit("/", 1)[1],
                   "runtime_arn": arn, "region": "us-west-2",
                   "ecr_uri": f"269550163595.dkr.ecr.us-west-2.amazonaws.com/"
                              f"coding-agents-{agent_id}:latest",
                   "s3files_mount_path": "/mnt/s3files"}, f)
    for _ in range(tries):
        _, a = req(console, "GET", f"/api/dev/agents/{agent_id}", headers=cookie)
        if a.get("status") == "ready" and a.get("runtime_arn") == arn:
            return a
        time.sleep(0.1)
    pytest.fail(f"agent {agent_id} never reconciled to ready after writing a real "
                f"runtime_config.json (got {a})")


def undeploy_real(agent_id: str = "claude-code") -> None:
    """Remove a harness's runtime_config.json so the shelf reads it as not deployed
    again (the genuine 'no Runtime created yet' state). Best-effort."""
    p = os.path.join(CODING_AGENTS_DIR, agent_id, "runtime_config.json")
    try:
        os.remove(p)
    except OSError:
        pass


def reset_shelf_real() -> None:
    """Return the whole shelf to empty: drop every harness's runtime_config.json so
    every agent reconciles back to not_deployed (the fresh-attendee precondition)."""
    for aid in SUPPORTED_AGENTS:
        undeploy_real(aid)


def open_session(console, cookie, agent_id: str = "claude-code") -> str:
    """POST /api/dev/sessions -> a live workspace; returns the session id."""
    code, sess = req(console, "POST", "/api/dev/sessions",
                     {"agent_id": agent_id}, headers=cookie)
    assert code == 201 and sess["status"] == "open", sess
    return sess["session_id"]


def close_session(console, cookie, sid: str) -> None:
    try:
        req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)
    except (OSError, HTTPError):
        pass


def open_pty(console, cookie, sid: str, cols: int = 100, rows: int = 30) -> dict:
    code, out = req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                    {"open": True, "resize": {"cols": cols, "rows": rows}}, headers=cookie)
    assert code == 200 and out.get("pty") is True, out
    return out


def pty_type(console, cookie, sid: str, text: str, offset: int = 0) -> dict:
    _, out = req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                 {"input": text, "offset": offset}, headers=cookie)
    return out


def pty_wait_for(console, cookie, sid: str, needle: str, tries: int = 80) -> str:
    """Poll the PTY until `needle` appears in the accumulated output; return that output."""
    _, first = req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                   {"offset": 0}, headers=cookie)
    buf = first["output"]
    offset = first["offset"]
    if needle in buf:
        return buf
    for _ in range(tries):
        time.sleep(0.1)
        _, more = req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                      {"offset": offset}, headers=cookie)
        buf += more["output"]
        offset = more["offset"]
        if needle in buf:
            return buf
    return buf  # caller asserts; return what we have for the message


def file_tree(console, cookie, sid: str) -> list[dict]:
    _, out = req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
    return out.get("tree", [])


def write_file(console, cookie, sid: str, path: str, content: str) -> dict:
    _, out = req(console, "POST", f"/api/dev/sessions/{sid}/file",
                 {"path": path, "content": content}, headers=cookie)
    return out


# The workspace now starts EMPTY: the participant creates every file in the editor,
# beginning with the input module cost_analyzer.py. Tests that need the module present
# call this to create it the same way (the real file-write API), from the canonical
# source, so the seed contract lives in ONE place instead of an auto-copy in the
# engine. Returns the file path it created.
_SKILL_SRC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "usecase-sample-to-mcp", "cost_analyzer.py")


def seed_skill(console, cookie, sid: str, name: str = "sample/cost_analyzer.py") -> str:
    """Create cost_analyzer.py in the session workspace from the real source, the
    way a participant would (New File → paste). Lands under sample/ to mirror the
    content page (mkdir -p sample). Returns the workspace path."""
    with open(_SKILL_SRC_PATH, encoding="utf-8") as f:
        write_file(console, cookie, sid, name, f.read())
    return f"/mnt/s3files/{name}"


def read_file(console, cookie, sid: str, path: str) -> dict:
    _, out = req(console, "POST", f"/api/dev/sessions/{sid}/file",
                 {"path": path}, headers=cookie)
    return out


def file_op(console, cookie, sid: str, path: str, op: str, to: str | None = None) -> dict:
    body = {"path": path, "op": op}
    if to is not None:
        body["to"] = to
    _, out = req(console, "POST", f"/api/dev/sessions/{sid}/file", body, headers=cookie)
    return out


# ---------------------------------------------------------------------------
# Stage 2 helpers: submit a task, watch the routed run reach a terminal state.
# ---------------------------------------------------------------------------
def submit_run(console, cookie, task: str | None = None,
               workflow_ref: str | None = None) -> dict:
    body: dict = {}
    if task is not None:
        body["task"] = task
    if workflow_ref is not None:
        body["workflow_ref"] = workflow_ref
    _, run = req(console, "POST", "/api/orchestrator/runs", body, headers=cookie)
    return run


def poll_route(console, cookie, rid: str, tries: int = 120) -> dict:
    for _ in range(tries):
        _, r = req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r.get("route"):
            return r["route"]
        time.sleep(0.1)
    pytest.fail(f"run {rid} never reported a route")


def poll_terminal(console, cookie, rid: str, tries: int = 200) -> dict:
    for _ in range(tries):
        _, r = req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r["status"] in TERMINAL_STATUSES:
            return r
        time.sleep(0.5)
    pytest.fail(f"run {rid} never reached a terminal status")


# Skip marker for the live-model tests (opt in with WORKSHOP_E2E_LIVE=1).
live = pytest.mark.skipif(
    os.environ.get("WORKSHOP_E2E_LIVE") != "1",
    reason="live-model journey (set WORKSHOP_E2E_LIVE=1 to run)")
