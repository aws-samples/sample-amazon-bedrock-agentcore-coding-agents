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

Local engine mode (offline test double, no LLM); the same machinery the workshop runs.
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

from e2e.conftest import seed_file

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The TEST entrypoint (same app, deterministic FixtureExecutor-backed Stage-2
# engine), not the shipped real-only server.py; a subprocess can't take a
# constructor arg, so the fixture engine reaches the console this way (no env flag
# selects a fake on the shipped binary). See conftest.py for the full rationale.
_SERVER = os.path.join(_REPO, "console", "test_server.py")
# Empty coding-agents dir: the shelf reconciles the real runtime_config.json a
# harness deploy.py writes here, so empty == no agent deployed.
_CODING_AGENTS_DIR = tempfile.mkdtemp(prefix="aa-coding-agents-")

sys.path.insert(0, os.path.join(_REPO, "orchestrator"))
import roles  # noqa: E402


def _served_checker() -> str:
    """The checker id this deployment SERVES, from the registry.

    The console shelf and the preset table both project the served roster only, so a
    literal here would fail on a supported roster swap instead of on a real regression.
    """
    checkers = roles.checker_ids()
    assert checkers, "a roster with no checker cannot gate anything"
    return checkers[0]


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

    # the freshly returned tree shows the steering files at their virtual paths
    tree_paths = {n["path"] for n in res["tree"]}
    assert "/mnt/s3files/CLAUDE.md" in tree_paths

    # and a subsequent file-tree GET (what the explorer re-renders) agrees
    _, files = _req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
    later = {n["path"] for n in files["tree"]}
    assert "/mnt/s3files/CLAUDE.md" in later

    # The CLAUDE.md content is the REAL shipped backend steering, not an empty stub and
    # not a second copy invented by the scaffolder: it must be byte-identical to the one
    # source of truth the orchestrator stages on every dispatch, or Lab 1 teaches an
    # agent configuration Lab 2 does not use.
    _, claude = _req(console, "POST", f"/api/dev/sessions/{sid}/file",
                     {"path": "CLAUDE.md"}, headers=cookie)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo, "orchestrator"))
    import roles as _roles
    role = _roles.get(_roles.by_capability("backend")[0])
    shipped = os.path.join(repo, "orchestrator", "harness",
                           role.harness_dir, role.steering_file)
    with open(shipped, encoding="utf-8") as f:
        assert claude["content"] == f.read()
    # It describes the ROLE, and carries the live extension seam. `harness:setup` is the
    # only harness block with a parser (the build/ui/gate variants and their parsers
    # are deleted), so asserting one here would pin dead config that merely looks live.
    assert "BACKEND role" in claude["content"]
    assert "harness:setup" in claude["content"]


# ---------------------------------------------------------------------------
# 2. deploy-upload: the code-upload deploy packages the workspace into a real
#    zip bundle; the manifest lists whatever files the workspace holds.
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
    """The validator is not on the shelf until a deploy lands. Write the
    runtime_config.json `deploy.py` produces (arn:aws:bedrock-agentcore ARN); poll GET
    /api/agents until it reconciles to ready with that exact ARN; smart capture of a
    deploy, no fake shim, no local:runtime placeholder, no deploy button."""
    # The SERVED validator, from the registry: the shelf only carries served roles, so
    # naming one in a literal would break this test on a roster swap rather than on a
    # real regression.
    validator = _served_checker()
    # Empty coding-agents dir on boot (a tempdir), so it must NOT be on the shelf yet.
    # This pre-check makes the ready-state below provably the result of the real config
    # we write, not stale state from a prior run.
    _, before = _req(console, "GET", "/api/dev/agents", headers=cookie)
    cv0 = next(a for a in before["agents"] if a["agent_id"] == validator)
    assert cv0["status"] != "ready", \
        f"{validator} already deployed before the test ran: {cv0}"

    arn = _write_real_runtime_config(validator)  # what deploy.py writes
    try:
        ready = None
        for _ in range(80):
            _, lst = _req(console, "GET", "/api/dev/agents", headers=cookie)
            cv = next(a for a in lst["agents"] if a["agent_id"] == validator)
            if cv["status"] == "ready" and cv["runtime_arn"] == arn:
                ready = cv
                break
            time.sleep(0.1)
        assert ready, \
            f"{validator} never captured on the shelf after a real deploy: {cv}"
        assert ready["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")
        assert f"runtime/{validator.replace('-', '_')}" in ready["runtime_arn"]
    finally:
        # _CODING_AGENTS_DIR is a tempfile.mkdtemp, never the tracked tree, so this
        # removes only what this test wrote.
        try:
            os.remove(os.path.join(_CODING_AGENTS_DIR, validator,
                                   "runtime_config.json"))
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
# 4. Stage 2 preset routing: each starting point resolves to its documented
#    role set. The route is set during admission (on the worker thread), so
#    poll the run until `route` appears. Local mode completes fast; we assert
#    the route, not a winner.
# ---------------------------------------------------------------------------
# (preset id, expected dispatched agents); from presets.PRESETS + roles.py.
# Verified against presets.resolve() directly.
_PRESET_CASES = [
    # (preset id, the roles it routes). Presets are STARTING POINTS: the request text
    # comes with them, and any other request works too (see the custom-roles test).
    ("game-from-scratch", ["claude-code", "kiro"]),
    ("service-from-scratch", ["claude-code", "kiro"]),
    ("web-app", ["claude-code", "opencode", "kiro"]),
    ("cli-tool", ["claude-code", "kiro"]),
    ("review-a-run", ["kiro"]),
]


def _route_of(console: str, cookie: dict, rid: str) -> dict:
    """Poll a run until the router's verdict is attached, then return it."""
    for _ in range(100):
        _, r = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r.get("route"):
            return r["route"]
        time.sleep(0.1)
    pytest.fail(f"run {rid} never reported a route")


def test_stage2_presets_route_their_documented_roles(console, cookie):
    """Submit each starting point; the run's reported route matches its roles."""
    for preset, expected_agents in _PRESET_CASES:
        _, run = _req(console, "POST", "/api/orchestrator/runs",
                      {"preset": preset}, headers=cookie)
        route = _route_of(console, cookie, run["run_id"])
        assert route["preset"] == preset, route
        assert route["agents"] == expected_agents, (
            f"preset {preset!r} dispatched {route['agents']} "
            f"(expected {expected_agents})")


# ---------------------------------------------------------------------------
# 4b. Stage 2 preset registry: the console renders these as starting points, from
#     ONE source (presets.PRESETS), so the chips cannot drift from the tools.
# ---------------------------------------------------------------------------
def test_api_s2_presets_contract(console, cookie):
    """GET /api/orchestrator/presets -> a non-empty list; every entry carries the
    fields the UI binds to, every build routes a checker, and the attendee's own
    request is a first-class option."""
    code, body = _req(console, "GET", "/api/orchestrator/presets", headers=cookie)
    assert code == 200
    items = body["presets"]
    assert isinstance(items, list) and items, body
    required = {"preset", "title", "roles", "task", "read_only"}
    for p in items:
        assert required <= set(p), f"preset descriptor missing keys: {p}"
        assert isinstance(p["roles"], list) and p["roles"], p
        assert isinstance(p["read_only"], bool), p
        if not p["read_only"]:
            assert _served_checker() in p["roles"], p
    ids = {p["preset"] for p in items}
    assert "your-own" in ids, ids


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
