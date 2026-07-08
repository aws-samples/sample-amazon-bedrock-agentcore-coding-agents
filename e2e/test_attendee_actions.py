"""Attendee actions that the journey suite doesn't exercise, same real server, same wire.

`test_attendee_flow.py` walks the happy path a content page teaches; this file closes the
coverage gaps a verification audit found: the buttons and edges an attendee CAN press but
that no e2e test drove yet. Every test here boots the SAME real `console/server.py`
process and drives the same-origin `/api/dev|orchestrator|metrics` mounts behind the same login gate;
if one breaks, an attendee action in the console is broken.

  scaffold-harness   the "Set up harness" button writes the agent's real steering files
  deploy-upload      the code-upload deploy packages the workspace into a real zip bundle
  real PTY           the interactive terminal: open a bash, type, read the echoed output
  smart capture      `agentcore deploy` typed in the terminal registers the agent on the shelf
  edit subagent      right-click Edit renames a deployed agent + sets its purpose, persisted
  router ladder      Stage 2's 5 documented task phrasings resolve to the documented routes
  login edges        wrong password is rejected; logout clears the cookie
  Stage 3 governance the p95 latency + audit endpoints answer over the real ledger

Local engine mode (deterministic, no LLM); the same pytest gate the workshop grades with.
Run: WORKSHOP_SKIP_LIVE=1 python3 -m pytest e2e/test_attendee_actions.py -q
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

from e2e.conftest import seed_skill

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The TEST entrypoint (same app, deterministic FixtureExecutor-backed Stage-2
# engine), not the shipped real-only server.py; a subprocess can't take a
# constructor arg, so the fixture engine reaches the console this way (no env flag
# selects a fake on the shipped binary). See conftest.py for the full rationale.
_SERVER = os.path.join(_REPO, "console", "test_server.py")
# Empty coding-agents dir: the shelf reconciles the real runtime_config.json a
# harness deploy.py writes here, so empty == no agent deployed.
_CODING_AGENTS_DIR = tempfile.mkdtemp(prefix="aa-coding-agents-")


def _write_real_runtime_config(agent_id: str) -> str:
    """Write the runtime_config.json a harness deploy.py produces (the
    arn:aws:bedrock-agentcore ARN) so the console reconciles the agent to ready,
    standing in only for the AWS CreateAgentRuntime call itself."""
    rid = agent_id.replace("-", "_") + "-AA000001cap"
    arn = f"arn:aws:bedrock-agentcore:us-west-2:269550163595:runtime/{rid}"
    d = os.path.join(_CODING_AGENTS_DIR, agent_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "runtime_config.json"), "w", encoding="utf-8") as f:
        json.dump({"agent_name": agent_id.replace("-", "_"), "runtime_id": rid,
                   "runtime_arn": arn, "region": "us-west-2",
                   "s3files_mount_path": "/mnt/s3files"}, f)
    return arn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _req(base: str, method: str, path: str, body: dict | None = None,
         headers: dict | None = None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    with urllib.request.urlopen(r, timeout=30) as resp:
        payload = resp.read()
        return resp.status, (payload if raw else json.loads(payload or b"{}"))


@pytest.fixture(scope="module")
def console():
    """One real console server, exactly as the CFN systemd unit runs it; with
    CONSOLE_PASSWORD set so the login gate is part of the surface under test."""
    port = _free_port()
    env = {**os.environ, "CONSOLE_PORT": str(port),
           "WORKSHOP_CODING_AGENTS_DIR": _CODING_AGENTS_DIR,  # empty shelf by default
           # GitHub + runtime-ARN isolation: empty tmp files so no run reads the dev's
           # real wired PAT (would open a REAL PR) or real runtime ARNs.
           "WORKSHOP_GITHUB_STORE": "local",
           "WORKSHOP_GITHUB_SETTINGS": os.path.join(
               tempfile.mkdtemp(prefix="aa-gh-"), "github.local.json"),
           "WORKSHOP_RUNTIME_CONFIG": os.path.join(
               tempfile.mkdtemp(prefix="aa-rt-"), "runtime.local.json"),
           "CONSOLE_USER": "ubuntu", "CONSOLE_PASSWORD": "attendee-pass"}
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_REPO", None)
    for _k in [k for k in env if k.startswith("AGENTCORE_RUNTIME_")]:
        env.pop(_k, None)
    proc = subprocess.Popen([sys.executable, _SERVER], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            _req(base, "GET", "/api/health")
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("console server never came up")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow the login 302 to /console/ (a 404 on the raw server; nginx owns
    that prefix in prod). The Set-Cookie we assert on lives on the 302 itself."""
    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


def _login(console: str, username: str, password: str):
    """POST the login form (urlencoded). Returns (status, set_cookie, html_bytes)."""
    body = f"username={username}&password={password}"
    r = urllib.request.Request(console + "/login", data=body.encode(),
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(r, timeout=10)
        return resp.status, resp.headers.get("Set-Cookie", ""), resp.read()
    except HTTPError as e:                       # the unfollowed 302, or a 401 page
        return e.code, e.headers.get("Set-Cookie", ""), e.read()


@pytest.fixture(scope="module")
def cookie(console):
    """Signed-in session cookie (same password as VS Code)."""
    status, set_cookie, _ = _login(console, "ubuntu", "attendee-pass")
    assert "console_session=" in set_cookie, "login must set the session cookie"
    return {"Cookie": set_cookie.split(";")[0]}


@pytest.fixture(scope="module")
def stage1_session(console, cookie):
    """A real open Stage 1 session (claude-code) for the harness/deploy/PTY actions."""
    _, sess = _req(console, "POST", "/api/dev/sessions",
                   {"agent_id": "claude-code"}, headers=cookie)
    sid = sess["session_id"]
    yield sid
    try:
        _req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 1. scaffold-harness: the "Set up harness" button writes the agent's
#    steering files (CLAUDE.md + a SKILL.md for claude-code) into the workspace.
# ---------------------------------------------------------------------------
def test_scaffold_harness_writes_claude_steering_files(console, cookie, stage1_session):
    """POST scaffold-harness {agent_id: claude-code} stages CLAUDE.md and the backend
    SKILL.md into the workspace; both show up in the returned (and re-fetched) tree."""
    sid = stage1_session
    _, res = _req(console, "POST",
                  f"/api/dev/sessions/{sid}/scaffold-harness",
                  {"agent_id": "claude-code"}, headers=cookie)
    assert res["agent_id"] == "claude-code"
    written = res["written"]
    assert any(p.endswith("/CLAUDE.md") for p in written), written
    assert any(p.endswith("/skills/configure-backend/SKILL.md") for p in written), written

    # the freshly returned tree shows the steering files at their virtual paths
    tree_paths = {n["path"] for n in res["tree"]}
    assert "/mnt/s3files/CLAUDE.md" in tree_paths
    assert "/mnt/s3files/skills/configure-backend/SKILL.md" in tree_paths

    # and a subsequent file-tree GET (what the explorer re-renders) agrees
    _, files = _req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
    later = {n["path"] for n in files["tree"]}
    assert "/mnt/s3files/CLAUDE.md" in later

    # the CLAUDE.md content is the real backend steering, not an empty stub
    _, claude = _req(console, "POST", f"/api/dev/sessions/{sid}/file",
                     {"path": "CLAUDE.md"}, headers=cookie)
    assert "BACKEND role" in claude["content"] and "harness:build" in claude["content"]


# ---------------------------------------------------------------------------
# 2. deploy-upload: the code-upload deploy packages the workspace into a real
#    zip bundle; the manifest lists mcp_server.py once the module is converted.
# ---------------------------------------------------------------------------
def test_deploy_upload_packages_a_real_bundle_with_mcp_server(console, cookie):
    """Convert the module first (so mcp_server.py exists in the workspace), then press
    deploy-upload: the bundle has bytes, the manifest includes mcp_server.py, and the
    code-first entrypoint resolves to it."""
    _, sess = _req(console, "POST", "/api/dev/sessions",
                   {"agent_id": "claude-code"}, headers=cookie)
    sid = sess["session_id"]
    try:
        # the workspace starts EMPTY; the participant creates cost_analyzer.py in the
        # editor (New File → paste → Save) before converting. We do it the same way
        # (the real file-write API) so the conversion has the module to import.
        seed_skill(console, cookie, sid)
        # give the workspace content the bundle must capture
        _, conv = _req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                       {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        assert conv["verified"] is True

        _, dep = _req(console, "POST", f"/api/dev/sessions/{sid}/deploy-upload",
                      {}, headers=cookie)
        assert dep["mode"] == "code-upload"
        assert dep["bundle_bytes"] > 0
        assert dep["file_count"] >= 1
        assert "mcp_server.py" in dep["manifest"]
        # the converted server is what AgentCore's code-first launch would run
        assert dep["entrypoint"] == "mcp_server.py"
        # the bundle path is the virtual /mnt/s3files view the UI shows
        assert dep["bundle_file"].endswith("code-bundle.zip")
    finally:
        _req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)


# ---------------------------------------------------------------------------
# 3. Real PTY: open a live bash, type a command, read the echoed output back.
#    Contract (interactive_api._pty_io): {"open": true} spawns the shell;
#    {"input": "...", "offset": N} writes keystrokes and returns
#    {"output", "offset", "alive"} for the bytes after `offset`.
# ---------------------------------------------------------------------------
def test_pty_open_type_and_read_real_output(console, cookie, stage1_session):
    """Open the PTY, send `echo hello`, and poll until "hello" surfaces in the live
    bash output; the shell stays alive across the round-trips."""
    sid = stage1_session
    _, opened = _req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                     {"open": True}, headers=cookie)
    assert opened["pty"] is True

    # write the command from offset 0; the response carries the new read offset
    _, first = _req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                    {"input": "echo hello\n", "offset": 0}, headers=cookie)
    assert first["alive"] is True
    combined = first["output"]
    offset = first["offset"]

    # poll for the command's output to appear (PTY echo + the shell running echo)
    seen = "hello" in combined
    for _ in range(50):
        if seen:
            break
        time.sleep(0.1)
        _, more = _req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                       {"offset": offset}, headers=cookie)
        combined += more["output"]
        offset = more["offset"]
        assert more["alive"] is True
        seen = "hello" in combined
    assert seen, f"'hello' never appeared in PTY output: {combined!r}"


# ---------------------------------------------------------------------------
# 3b. Smart capture: a deploy (./setup.sh && python deploy.py writes the
#     runtime_config.json) is reconciled onto the shelf with no button. The console
#     reads the runtime_config.json deploy.py wrote into status=="ready",
#     so the deployed agent appears as an orchestrator subagent on its own.
# ---------------------------------------------------------------------------
def test_real_deploy_captures_agent_on_the_shelf(console, cookie):
    """kiro is not on the shelf until a deploy lands. Write the runtime_config.json
    `deploy.py` produces (arn:aws:bedrock-agentcore ARN); poll GET
    /api/agents until kiro reconciles to ready with that exact ARN; smart capture of
    a deploy, no fake shim, no local:runtime placeholder, no deploy button."""
    # Empty coding-agents dir on boot, so kiro must NOT be on the shelf yet. This
    # pre-check makes the ready-state below provably the result of the real config we
    # write, not stale state from a prior run.
    _, before = _req(console, "GET", "/api/dev/agents", headers=cookie)
    kiro0 = next(a for a in before["agents"] if a["agent_id"] == "kiro")
    assert kiro0["status"] != "ready", f"kiro already deployed before the test ran: {kiro0}"

    arn = _write_real_runtime_config("kiro")    # what deploy.py writes in the harness dir
    try:
        ready = None
        for _ in range(80):
            _, lst = _req(console, "GET", "/api/dev/agents", headers=cookie)
            kiro = next(a for a in lst["agents"] if a["agent_id"] == "kiro")
            if kiro["status"] == "ready" and kiro["runtime_arn"] == arn:
                ready = kiro
                break
            time.sleep(0.1)
        assert ready, f"kiro never captured on the shelf after a real deploy: {kiro}"
        assert ready["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")
        assert "runtime/kiro" in ready["runtime_arn"]
    finally:
        try:
            os.remove(os.path.join(_CODING_AGENTS_DIR, "kiro", "runtime_config.json"))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3c. Edit a deployed subagent: right-click Edit sets a custom name + purpose
#     that persists and layers over the catalog; an empty name is rejected.
# ---------------------------------------------------------------------------
def test_edit_agent_name_and_purpose_persists(console, cookie):
    """POST /api/agents/opencode/edit {name, purpose} -> the catalog reflects the custom
    fields on the next GET; an empty name is a 400, not a silent wipe."""
    new_name = "Frontend builder"
    new_purpose = "Owns the chatbot UI for the orchestrator."
    _, edited = _req(console, "POST", "/api/dev/agents/opencode/edit",
                     {"name": new_name, "purpose": new_purpose}, headers=cookie)
    assert edited["name"] == new_name and edited["purpose"] == new_purpose

    # It persists: a fresh GET of the catalog carries the override.
    _, lst = _req(console, "GET", "/api/dev/agents", headers=cookie)
    opencode = next(a for a in lst["agents"] if a["agent_id"] == "opencode")
    assert opencode["name"] == new_name and opencode["purpose"] == new_purpose

    # An empty name is rejected (400) and must not blank the stored name.
    # A non-string value is a clean 400 (not a 500), and an over-long value is
    # capped; none of these may wipe the persisted name.
    for bad in ({"name": "   "}, {"name": ["array"]}, {"purpose": {"obj": 1}},
                {"name": "x" * 5000}):
        try:
            _req(console, "POST", "/api/dev/agents/opencode/edit", bad, headers=cookie)
            raise AssertionError(f"bad edit {bad!r} should have been rejected")
        except HTTPError as e:
            assert e.code == 400, f"{bad!r} returned {e.code}, expected 400"
    _, lst2 = _req(console, "GET", "/api/dev/agents", headers=cookie)
    opencode2 = next(a for a in lst2["agents"] if a["agent_id"] == "opencode")
    assert opencode2["name"] == new_name, "rejected edit must not wipe the name"
    assert opencode2["purpose"] == new_purpose, "rejected edit must not wipe the purpose"

    # Clearing the purpose is a real edit: it must stick as empty, not snap back
    # to the hardcoded catalog default.
    _, cleared = _req(console, "POST", "/api/dev/agents/opencode/edit",
                      {"purpose": ""}, headers=cookie)
    assert cleared["purpose"] == "", f"cleared purpose reverted to default: {cleared['purpose']!r}"
    _, lst3 = _req(console, "GET", "/api/dev/agents", headers=cookie)
    opencode3 = next(a for a in lst3["agents"] if a["agent_id"] == "opencode")
    assert opencode3["purpose"] == "" and opencode3["name"] == new_name


# ---------------------------------------------------------------------------
# 4. Stage 2 router ladder: each documented task phrasing resolves to the
#    documented workflow_ref + dispatched agents. The route is set during
#    admission (on the worker thread), so poll the run until `route` appears.
#    Local mode completes fast; we assert the route, not a winner.
# ---------------------------------------------------------------------------
# (task phrasing, expected workflow_ref, expected dispatched agents); from the
# content + router.py ladder. Verified against router.route() directly.
_ROUTER_CASES = [
    ("Convert /mnt/s3files/sample/cost_analyzer.py to a remote MCP server with "
     "tests + a chatbot UI", "convert/sample-to-mcp-v1",
     ["claude-code", "kiro", "opencode"]),
    ("fix the server version string in mcp_server.py",
     "patch/backend-v1", ["claude-code"]),
    ("use opencode to restyle the chatbot UI",
     "patch/frontend-v1", ["opencode"]),
    ("Build the full-stack Critter Lab app: backend and frontend",
     "build/fullstack-v1", ["claude-code", "kiro", "opencode"]),
    ("review the PR from the last run",
     "review/pr-v1", ["kiro"]),
]


def _route_of(console: str, cookie: dict, rid: str) -> dict:
    """Poll a run until the router's verdict is attached, then return it."""
    for _ in range(100):
        _, r = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r.get("route"):
            return r["route"]
        time.sleep(0.1)
    pytest.fail(f"run {rid} never reported a route")


@pytest.mark.parametrize("task,expected_ref,expected_agents", _ROUTER_CASES,
                         ids=[c[1] for c in _ROUTER_CASES])
def test_stage2_router_resolves_documented_routes(console, cookie, task,
                                                   expected_ref, expected_agents):
    """Submit each documented phrasing; the run's reported route matches the docs."""
    _, run = _req(console, "POST", "/api/orchestrator/runs", {"task": task}, headers=cookie)
    rid = run["run_id"]
    route = _route_of(console, cookie, rid)
    assert route["workflow_ref"] == expected_ref, (
        f"task {task!r} routed to {route['workflow_ref']} (expected {expected_ref})")
    assert route["agents"] == expected_agents, (
        f"task {task!r} dispatched {route['agents']} (expected {expected_agents})")


# ---------------------------------------------------------------------------
# 4b. Stage 2 workflows registry: the console's run page renders this list to
#     offer the documented workflows; GET /api/orchestrator/workflows must expose the
#     full versioned registry (router.public_workflows). Every entry carries the
#     fields the UI binds to, and all five documented refs are present.
# ---------------------------------------------------------------------------
def test_api_s2_workflows_contract(console, cookie):
    """GET /api/orchestrator/workflows -> a non-empty workflows list; every entry has the
    full descriptor shape and all five documented workflow_refs are present."""
    code, body = _req(console, "GET", "/api/orchestrator/workflows", headers=cookie)
    assert code == 200
    workflows = body["workflows"]
    assert isinstance(workflows, list) and workflows, body
    required = {"workflow_ref", "version", "agents", "usecase", "description", "read_only"}
    for wf in workflows:
        assert required <= set(wf), f"workflow descriptor missing keys: {wf}"
        assert isinstance(wf["agents"], list) and wf["agents"], wf
        assert isinstance(wf["read_only"], bool), wf
    refs = {wf["workflow_ref"] for wf in workflows}
    assert {"convert/sample-to-mcp-v1", "patch/backend-v1", "patch/frontend-v1",
            "build/fullstack-v1", "review/pr-v1"} <= refs, refs


# ---------------------------------------------------------------------------
# 5. Login edges: wrong password is rejected with the login page; logout clears
#    the cookie. (The open-by-default no-CONSOLE_PASSWORD path is covered by the
#    journey suite's getting-started tests; the gate is ENABLED here.)
# ---------------------------------------------------------------------------
def test_wrong_password_is_rejected_with_the_login_page(console):
    """A bad password returns 401 + the sign-in page, and sets no session cookie."""
    status, set_cookie, html = _login(console, "ubuntu", "wrong-pass")
    assert status == 401
    assert "console_session=" not in set_cookie
    assert b"Sign in" in html
    assert b"Incorrect username or password" in html


def test_logout_clears_the_session_cookie(console, cookie):
    """GET /logout expires the cookie (Max-Age=0), bouncing the attendee to login.

    NOTE: the server wires /logout on GET only (server.py do_GET); it is reachable
    regardless of session state. A POST to /logout is NOT handled (it falls through
    to 404), so the logout action is a GET link, not a form POST."""
    r = urllib.request.Request(console + "/logout", method="GET", headers={**cookie})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(r, timeout=10)
        set_cookie = resp.headers.get("Set-Cookie", "")
    except HTTPError as e:                       # the unfollowed 302 to /console/
        set_cookie = e.headers.get("Set-Cookie", "")
    assert "console_session=" in set_cookie and "Max-Age=0" in set_cookie


def test_api_requires_auth_is_401(console):
    """With the login gate ENABLED (this suite's server has CONSOLE_PASSWORD set), an
    UNAUTHENTICATED GET and POST to a protected API both return 401; the wall covers
    the per-stage APIs, not just the HTML. (No cookie header is sent.)"""
    with pytest.raises(HTTPError) as ge:
        _req(console, "GET", "/api/orchestrator/runs")
    assert ge.value.code == 401, "an unauthenticated GET must be walled (401)"
    with pytest.raises(HTTPError) as pe:
        _req(console, "POST", "/api/orchestrator/runs", {"task": "convert the module"})
    assert pe.value.code == 401, "an unauthenticated POST must be walled (401)"


def test_logout_cookie_is_no_longer_accepted_on_the_api(console):
    """After /logout expires the cookie, a protected API call carrying that EXPIRED
    cookie value is rejected 401; logout truly drops the session, it doesn't just
    rewrite the client's jar. We log in fresh, log out (capturing the Max-Age=0
    Set-Cookie), then replay the expired cookie against the API."""
    # fresh sign-in
    _, set_cookie, _ = _login(console, "ubuntu", "attendee-pass")
    assert "console_session=" in set_cookie
    live_cookie = {"Cookie": set_cookie.split(";")[0]}

    # the expired cookie the logout response hands back (console_session=; Max-Age=0)
    r = urllib.request.Request(console + "/logout", method="GET", headers={**live_cookie})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        cleared = opener.open(r, timeout=10).headers.get("Set-Cookie", "")
    except HTTPError as e:
        cleared = e.headers.get("Set-Cookie", "")
    assert "Max-Age=0" in cleared
    expired_cookie = {"Cookie": cleared.split(";")[0]}        # console_session=
    assert expired_cookie["Cookie"] == "console_session="     # empty, no valid token

    # the expired/empty cookie carries no valid session token -> the API walls it
    with pytest.raises(HTTPError) as e:
        _req(console, "GET", "/api/orchestrator/runs", headers=expired_cookie)
    assert e.value.code == 401, "an expired logout cookie must not pass the API gate"


# ---------------------------------------------------------------------------
# 6. Stage 3 governance: the p95 latency + audit endpoints answer over the
#    ledger. Run a Stage 2 task first so the ledger is non-empty.
# ---------------------------------------------------------------------------
def test_stage3_latency_p95_and_audit_reflect_real_runs(console, cookie):
    """After a Stage 2 run lands in the ledger, /latency/p95 returns a p95 field and
    /audit returns a list of real ledger lines."""
    # ensure the ledger has at least one orchestrator run to aggregate
    _, run = _req(console, "POST", "/api/orchestrator/runs",
                  {"task": "Convert the module to a remote MCP server with tests "
                           "+ a chatbot UI"}, headers=cookie)
    rid = run["run_id"]
    for _ in range(120):
        _, r = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r["status"] in ("passed", "failed", "needs_human"):
            break
        time.sleep(1)

    me = __import__("getpass").getuser()
    _, p95 = _req(console, "GET", f"/api/metrics/latency/p95?user_id={me}",
                  headers=cookie)
    assert "p95_latency_ms" in p95
    assert isinstance(p95["p95_latency_ms"], (int, float)) and p95["p95_latency_ms"] >= 0
    assert p95["scope"].get("user_id") == me

    _, audit = _req(console, "GET", "/api/metrics/audit?limit=50", headers=cookie)
    assert isinstance(audit["audit"], list)
    assert len(audit["audit"]) >= 1
    # every audit line is a structured ledger event, not free text
    assert all({"at", "kind", "user_id", "line"} <= set(row) for row in audit["audit"])
    assert audit["source"].endswith("telemetry.jsonl")
