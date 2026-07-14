"""The WHOLE 4-hour attendee journey, in order, against one real console server.

`test_attendee_flow.py` walks the happy path and `test_attendee_actions.py` closes
the per-button gaps; THIS file is the spine, one ordered narrative that walks the
ENTIRE agenda end to end, exactly as an attendee does it, against a single real
`console/server.py` process with the login wall ON for the whole journey:

  Getting Started -> Stage 1 (deploy / shell / convert / package) -> Stage 2 (the
  orchestration core: route, autonomous phases, gate, LGTM, compose, anti-race,
  distribution, read-only review, full-stack) -> Stage 3 (governance metrics over
  the real ledger) -> Cleanup.

If a station here breaks, a content page is telling attendees to do something that
does not work, OR a non-negotiable (NO race/winner, routed dispatch, honest local
zero, real-or-null PR, collaboration compose) has regressed.

The test names ARE the agenda: `pytest -v` reads top to bottom like the run sheet.
One module-scoped server, one signed-in "attendee" session, shared state threaded
through an `ATTENDEE` dict (the session ids / run ids each station produces).

Local engine mode (deterministic, no LLM); the same pytest gate the workshop
grades with. Runs in well under 3 minutes.

Run: WORKSHOP_SKIP_LIVE=1 python3 -m pytest e2e/test_full_workshop_journey.py -q
"""

from __future__ import annotations

import getpass
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
# engine), not the shipped real-only server.py; see conftest.py for the rationale.
_SERVER = os.path.join(_REPO, "console", "test_server.py")
_COMPOSED = os.path.join(_REPO, ".runs", "composed")
_WORK = os.path.join(_REPO, ".runs", "work")

# An empty coding-agents dir for this module's server: the Stage-1 shelf reconciles
# the real runtime_config.json a harness deploy.py writes here, so empty == no agent
# deployed. _deploy_real writes the real-ARN config a harness deploy.py produces.
_CODING_AGENTS_DIR = tempfile.mkdtemp(prefix="fwj-coding-agents-")

# The expected fixture the whole pedagogy hangs on: m5.large @ $0.096/hr * 730h * 2.
EC2_FIXTURE_COST = 140.16
LGTM_TOKEN = "LGTM: no changes needed"

# One shared attendee state, populated as the journey advances (ordered tests).
ATTENDEE: dict[str, object] = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _req(base: str, method: str, path: str, body: dict | None = None,
         headers: dict | None = None, raw: bool = False):
    """One same-origin call. Returns (status, json|bytes); raises HTTPError on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    with urllib.request.urlopen(r, timeout=60) as resp:
        payload = resp.read()
        return resp.status, (payload if raw else json.loads(payload or b"{}"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow the login 302 to /console/ (a 404 on the raw server; nginx owns
    that prefix in prod). The Set-Cookie we assert on lives on the 302 itself."""
    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


@pytest.fixture(scope="module")
def console():
    """One real console server, exactly as the CFN systemd unit runs it, with the
    login wall ENABLED (CONSOLE_PASSWORD) and the deterministic local engine pinned."""
    port = _free_port()
    env = {**os.environ, "CONSOLE_PORT": str(port),
           "WORKSHOP_CODING_AGENTS_DIR": _CODING_AGENTS_DIR,  # empty shelf by default
           # GitHub + runtime-ARN isolation: empty tmp files so this server NEVER reads
           # the dev's wired PAT (would open live PRs) or real runtime ARNs.
           "WORKSHOP_GITHUB_STORE": "local",
           "WORKSHOP_GITHUB_SETTINGS": os.path.join(
               tempfile.mkdtemp(prefix="fwj-gh-"), "github.local.json"),
           "WORKSHOP_RUNTIME_CONFIG": os.path.join(
               tempfile.mkdtemp(prefix="fwj-rt-"), "runtime.local.json"),
           "CONSOLE_USER": "ubuntu", "CONSOLE_PASSWORD": "attendee-pass"}
    env.pop("GITHUB_TOKEN", None)                     # force the honest no-credential state
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


@pytest.fixture(scope="module")
def cookie(console):
    """The attendee's signed-in session cookie, every station rides this."""
    body = "username=ubuntu&password=attendee-pass"
    r = urllib.request.Request(console + "/login", data=body.encode(),
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(r, timeout=10)
        set_cookie = resp.headers.get("Set-Cookie", "")
    except HTTPError as e:                            # the unfollowed 302
        set_cookie = e.headers.get("Set-Cookie", "")
    assert "console_session=" in set_cookie, "login must set the session cookie"
    return {"Cookie": set_cookie.split(";")[0]}


# ---------------------------------------------------------------------------
# Helpers shared across stations.
# ---------------------------------------------------------------------------
def _deploy_real(console, cookie, agent_id: str = "claude-code", tries: int = 50) -> dict:
    """Stand in for a Stage-1 deploy: write the runtime_config.json a harness
    deploy.py produces into the wired coding-agents dir, then poll the shelf until
    the console's reconciliation flips the agent to ready with that ARN. Only the
    AWS CreateAgentRuntime call is stood in for."""
    rid = agent_id.replace("-", "_") + "-FWJ00001cap"
    arn = f"arn:aws:bedrock-agentcore:us-west-2:269550163595:runtime/{rid}"
    d = os.path.join(_CODING_AGENTS_DIR, agent_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "runtime_config.json"), "w", encoding="utf-8") as f:
        json.dump({"agent_name": agent_id.replace("-", "_"), "runtime_id": rid,
                   "runtime_arn": arn, "region": "us-west-2",
                   "s3files_mount_path": "/mnt/s3files"}, f)
    for _ in range(tries):
        _, a = _req(console, "GET", f"/api/dev/agents/{agent_id}", headers=cookie)
        if a.get("status") == "ready" and a.get("runtime_arn") == arn:
            return a
        time.sleep(0.1)
    pytest.fail(f"agent {agent_id} never reconciled to ready (got {a})")


def _submit(console, cookie, task: str) -> str:
    """Submit a Stage 2 task; return the run_id (route is attached on the worker)."""
    _, run = _req(console, "POST", "/api/orchestrator/runs", {"task": task}, headers=cookie)
    return run["run_id"]


def _poll_terminal(console, cookie, rid: str, tries: int = 150) -> dict:
    """Poll a run to a terminal status WITHOUT any extra POSTs (autonomy check)."""
    for _ in range(tries):
        _, r = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r["status"] in ("passed", "failed", "needs_human"):
            return r
        time.sleep(0.5)
    pytest.fail(f"run {rid} never reached a terminal status")


def _route_of(console, cookie, rid: str, tries: int = 120) -> dict:
    """Poll a run until the router's verdict is attached (set on the worker thread)."""
    for _ in range(tries):
        _, r = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r.get("route"):
            return r["route"]
        time.sleep(0.1)
    pytest.fail(f"run {rid} never reported a route")


def _terminals(console, cookie, rid: str) -> dict:
    _, terms = _req(console, "GET", f"/api/orchestrator/runs/{rid}/terminals", headers=cookie)
    return terms["terminals"]


def _result(console, cookie, rid: str) -> dict:
    _, res = _req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
    return res


def _git_files_on(branch: str) -> set[str]:
    """The composed deliverable file set on a run's branch.

    The result payload exposes the compose via `composed_branch`/`composed_commit`
    (the local stand-in for the PR); the merged artifacts live on that branch
    in `.runs/composed/deliverable/`. Reading the git tree shows that compose
    merged ALL routed roles' work, not just one winner's.
    """
    proc = subprocess.run(
        ["git", "-C", _COMPOSED, "ls-tree", "-r", "--name-only", branch],
        capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        return set()
    return {os.path.basename(p) for p in proc.stdout.split()}


def _git_show_on(branch: str, filename: str) -> str:
    """Read one composed artifact's CONTENT off the run branch (the deliverable
    lives under `deliverable/<filename>`). Reading the bytes, not just the name,
    shows compose merged each role's output, not an empty stub."""
    proc = subprocess.run(
        ["git", "-C", _COMPOSED, "show", f"{branch}:deliverable/{filename}"],
        capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        return ""
    return proc.stdout


# ===========================================================================
# 01. GETTING STARTED: the login wall, sign-in, and the honest GitHub state.
#   content/00-prerequisites + the Getting Started Settings/GitHub card.
# ===========================================================================
def test_01_getting_started_login_wall_then_honest_github_state(console, cookie):
    """Unauthenticated -> wall; signed-in -> workbench on Stage 1; GitHub honestly
    'not connected, local mode' (the no-credential promise the content makes)."""
    # the wall: HTML is the sign-in page, the API is gated, only /api/health is open
    _, html = _req(console, "GET", "/", raw=True)
    assert b"Sign in" in html and b"view-s1" not in html
    code, _ = _req(console, "GET", "/api/health")
    assert code == 200
    with pytest.raises(HTTPError) as e:
        _req(console, "GET", "/api/orchestrator/agents")          # gated without the cookie
    assert e.value.code == 401

    # signed in: the React console shell is served (the SPA mounts into #root and
    # the client router lands on the Agents stage). Bare / returns index.html with
    # the app bundle; the default-stage redirect happens client-side in the router.
    _, page = _req(console, "GET", "/", headers=cookie, raw=True)
    assert b'id="root"' in page
    assert b'/assets/' in page

    # the Getting Started GitHub card: no credential on the ladder -> honest local mode
    _, gh = _req(console, "GET", "/api/orchestrator/github", headers=cookie)
    assert gh["connected"] is False
    assert gh["mode"] == "local"


# ===========================================================================
# 02. STAGE 1 / DEPLOY: deploy one agent (claude-code) and poll it to ready.
#   content/20-stage1-interactive/1-deploy-an-agent.
# ===========================================================================
def test_02_stage1_deploy_claude_code_until_ready(console, cookie):
    """Attendee builds + deploys claude-code (./setup.sh && python deploy.py writes
    the runtime_config.json); the console reconciles it and the shelf shows the
    agent 'ready' with its arn:aws:bedrock-agentcore ARN, never a fabricated
    local:runtime placeholder."""
    agent = _deploy_real(console, cookie, "claude-code")
    assert agent["agent_id"] == "claude-code"
    assert agent["status"] == "ready"
    assert agent["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")
    assert "runtime/" in agent["runtime_arn"]


# ===========================================================================
# 03. STAGE 1 / SHELL: open a session, CREATE the input module the participant's
#   way (New File → paste cost_analyzer.py), open the PTY, run real commands, and
#   confirm the file the attendee just authored is in the workspace tree.
#   content/20-stage1-interactive/2-open-a-shell.
# ===========================================================================
def test_03_stage1_open_session_pty_and_create_the_skill(console, cookie):
    """Open a session on /mnt/s3files; the workspace starts EMPTY (nothing
    pre-seeded). The attendee creates the input module in the editor: New File in the
    Explorer, type `cost_analyzer.py`, paste the contents, Save (⌘S), modeled by the
    file-write API via seed_skill. THEN over the PTY run `ls /mnt/s3files` and
    `head cost_analyzer.py` and read the echoed output; assert the file the
    attendee authored is in the explorer tree."""
    _, sess = _req(console, "POST", "/api/dev/sessions",
                   {"agent_id": "claude-code"}, headers=cookie)
    sid = sess["session_id"]
    ATTENDEE["stage1_session"] = sid
    assert sess["workspace"] == "/mnt/s3files"

    # the workspace starts EMPTY; no file is pre-seeded
    _, empty = _req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
    assert {n["path"] for n in empty["tree"]} == set(), empty["tree"]

    # the attendee's FIRST file: create cost_analyzer.py the way the content teaches
    # (New File → paste the given contents → Save), from the canonical module source.
    seed_skill(console, cookie, sid)

    # now the module the attendee authored is on disk, visible in the explorer tree
    _, files = _req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
    tree = {n["path"] for n in files["tree"]}
    assert "/mnt/s3files/sample/cost_analyzer.py" in tree, tree

    # open a real interactive bash PTY
    _, opened = _req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                     {"open": True}, headers=cookie)
    assert opened["pty"] is True

    # run TWO real commands through the PTY and poll for both outputs to surface
    _, first = _req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                    {"input": "ls /mnt/s3files/sample\nhead -3 sample/cost_analyzer.py\n",
                     "offset": 0}, headers=cookie)
    assert first["alive"] is True
    combined, offset = first["output"], first["offset"]
    for _ in range(60):
        if "cost_analyzer" in combined:
            break
        time.sleep(0.1)
        _, more = _req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                       {"offset": offset}, headers=cookie)
        combined += more["output"]
        offset = more["offset"]
        assert more["alive"] is True
    # `ls sample` lists cost_analyzer.py; `head` of it shows the real module docstring
    assert "cost_analyzer" in combined, f"`ls`/`head` of the module not seen in PTY: {combined!r}"

    # the terminal can be resized (xterm.js fit on the live PTY). The wire shape is a
    # NESTED {"resize": {rows, cols}} (interactive_api._pty_io reads body["resize"]),
    # not siblings of "open"; the shell stays alive through the ioctl.
    _, resized = _req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                      {"resize": {"rows": 30, "cols": 120}, "offset": offset},
                      headers=cookie)
    assert "error" not in resized, resized
    assert resized["alive"] is True


# ===========================================================================
# 04. STAGE 1 / CONVERT: convert the module by hand; verify the 4 live checks,
#   including the EXACT 140.16 fixture the pedagogy depends on.
#   content/20-stage1-interactive/3-convert-a-skill-by-hand.
# ===========================================================================
def test_04_stage1_convert_skill_and_verify_140_16_fixture(console, cookie):
    """convert-skill -> mcp_server.py in the tree; verify -> 4 checks all pass
    (liveness, tools/list has estimate_ec2_monthly_cost, tool_call, bad-input
    rejected); the tool_call returns the EXACT 140.16 fixture."""
    sid = ATTENDEE["stage1_session"]
    _, conv = _req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                   {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
    assert conv["verified"] is True
    assert conv["server_file"] == "/mnt/s3files/mcp_server.py"
    assert any(t.get("name") == "estimate_ec2_monthly_cost" for t in conv["tools_list"])
    # the live server's own sample call returns the exact fixture (not a stub)
    assert conv["sample_call"]["result"]["monthly_cost"] == EC2_FIXTURE_COST

    # mcp_server.py now exists in the workspace tree
    _, files = _req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
    assert "/mnt/s3files/mcp_server.py" in {n["path"] for n in files["tree"]}

    # the verify card: 4 named checks, all green, over the wire
    _, ver = _req(console, "POST", f"/api/dev/sessions/{sid}/verify", {}, headers=cookie)
    assert ver["ran"] is True and ver["passed"] is True
    checks = {c["check"]: c["passed"] for c in ver["checks"]}
    assert checks == {"server_live": True, "tools_list": True,
                      "tool_call": True, "input_validation": True}, checks
    # the verify sample is the same exact fixture
    assert ver["sample"]["monthly_cost"] == EC2_FIXTURE_COST


# ===========================================================================
# 05. STAGE 1 / PACKAGE: scaffold the harness, then code-upload deploy.
#   content/20-stage1-interactive/3 (the "set up harness" + "deploy" cards).
# ===========================================================================
def test_05_stage1_scaffold_harness_and_deploy_upload(console, cookie):
    """scaffold-harness writes CLAUDE.md + the backend SKILL.md into the tree;
    deploy-upload packages a real bundle (bytes > 0) whose manifest includes the
    converted mcp_server.py and whose entrypoint resolves to it."""
    sid = ATTENDEE["stage1_session"]
    _, h = _req(console, "POST", f"/api/dev/sessions/{sid}/scaffold-harness",
                {"agent_id": "claude-code"}, headers=cookie)
    written = h["written"]
    assert any(p.endswith("/CLAUDE.md") for p in written), written
    assert any(p.endswith("/skills/configure-backend/SKILL.md") for p in written), written
    tree = {n["path"] for n in h["tree"]}
    assert "/mnt/s3files/CLAUDE.md" in tree
    assert "/mnt/s3files/skills/configure-backend/SKILL.md" in tree

    _, dep = _req(console, "POST", f"/api/dev/sessions/{sid}/deploy-upload", {},
                  headers=cookie)
    assert dep["mode"] == "code-upload"
    assert dep["bundle_bytes"] > 0
    assert dep["file_count"] >= 1
    assert "mcp_server.py" in dep["manifest"]          # the converted server rode along
    assert dep["entrypoint"] == "mcp_server.py"


# ===========================================================================
# 06. STAGE 2 / THE ORCHESTRATION CORE. The most important station.
#   content/30-stage2-orchestrate/5-run-the-orchestrator (+ the role pages 1-4).
# ===========================================================================

# 6a. the default convert task: route, autonomous phases, gate, LGTM, three
#      role terminals, honest zero usage, and the COLLABORATION compose.
def test_06a_stage2_convert_routes_runs_gates_and_composes_all_three(console, cookie):
    """Submit the default convert task and assert the full orchestration contract:
    correct route, exactly the three roles dispatched, autonomous phase advance,
    pytest gate green, LGTM (with the exact token in the critique), one iteration,
    three role terminals each with output, zero tokens/cost (local honest zero),
    and a composed deliverable that contains EVERY routed role's artifact."""
    task = ("Convert /mnt/s3files/sample/cost_analyzer.py to a remote MCP server "
            "with tests + a chatbot UI")
    rid = _submit(console, cookie, task)
    ATTENDEE["convert_run"] = rid

    # route: the documented workflow + EXACTLY the three roles
    route = _route_of(console, cookie, rid)
    assert route["workflow_ref"] == "convert/sample-to-mcp-v1", route
    assert set(route["agents"]) == {"claude-code", "claude-code-validator", "opencode"}, route
    assert route["read_only"] is False

    # autonomy: no POSTs between submit and terminal; the engine drives the phases
    run = _poll_terminal(console, cookie, rid)
    assert run["status"] == "passed", run

    res = _result(console, cookie, rid)
    # gate: the pytest acceptance gate is green with a non-empty named check list
    assert res["gate"]["passed"] is True
    gate_checks = {c["check"] for c in res["gate"]["checks"]}
    assert gate_checks >= {"tool_discovery", "tool_correctness", "input_validation"}, gate_checks
    assert all(c["passed"] for c in res["gate"]["checks"])
    # review: LGTM, and the EXACT token lives in the committed critique report
    assert res["review"]["lgtm"] is True
    assert res["review"]["state"] == "approved"
    critique_path = os.path.join(_WORK, rid, "critique.md")
    assert os.path.isfile(critique_path), critique_path
    with open(critique_path, encoding="utf-8") as f:
        critique = f.read()
    assert LGTM_TOKEN in critique, "the exact LGTM pass token must appear in the critique"
    # bounded iteration: a clean run lands in exactly one pass
    assert res["iterations"] == 1
    # composed_from carries the roles (no winner) in dispatch order
    assert res["composed_from"] == ["backend-mcp", "validator", "frontend-builder"]
    # honest no-credential PR: pr_url is null and the compose stayed local
    assert res["pr_url"] is None
    assert (res.get("pr") or {}).get("pr_url") is None
    assert res["compose_base"]["mode"] == "local"
    assert res["composed_commit"] and res["composed_branch"] == f"run/{rid}"

    # three role terminals, each with at least one transcript entry
    terms = _terminals(console, cookie, rid)
    for agent in ("claude-code", "claude-code-validator", "opencode"):
        assert agent in terms, terms.keys()
        assert len(terms[agent]) >= 1, f"{agent} has no terminal output"

    # honest local zero: per-role progress reports zero tokens AND zero cost
    _, full = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
    progress = {p["agent"]: p for p in full["progress"]}
    assert set(progress) == {"claude-code", "claude-code-validator", "opencode"}
    for agent, p in progress.items():
        assert p["tokens"] == 0, f"{agent} tokens must be zero in local mode"
        assert p["cost_usd"] == 0, f"{agent} cost must be zero in local mode"

    # COLLABORATION: the composed deliverable contains EVERY role's artifact, not
    # one winner's. backend -> mcp_server.py, frontend -> chatbot.html,
    # reviewer -> critique.md + gate_report.json. (Reading the git tree on the
    # run branch shows compose merged all roles' work.)
    delivered = _git_files_on(res["composed_branch"])
    assert "mcp_server.py" in delivered, f"backend artifact missing from compose: {delivered}"
    assert "chatbot.html" in delivered, f"frontend artifact missing from compose: {delivered}"
    assert "critique.md" in delivered, f"reviewer critique missing from compose: {delivered}"
    assert "gate_report.json" in delivered, f"validator gate report missing: {delivered}"


# 6f. ANTI-RACE: every dispatched role reached `done` (none "won" while the
#      others were cancelled), and each contributed a DISTINCT artifact path.
def test_06f_stage2_anti_race_all_roles_done_distinct_artifacts(console, cookie):
    """For the convert run, assert NO race/winner: all three roles reached state
    'done', and the composed deliverable holds three distinct role artifacts."""
    rid = ATTENDEE["convert_run"]
    _, full = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
    progress = {p["agent"]: p for p in full["progress"]}
    assert {"claude-code", "claude-code-validator", "opencode"} <= set(progress)
    for agent, p in progress.items():
        assert p["state"] == "done", f"{agent} did not finish done (race/winner?): {p}"
    # distinct artifact paths: backend, frontend, reviewer each land their own file
    branch = f"run/{rid}"
    delivered = _git_files_on(full.get("route", {}) and branch)
    distinct = {"mcp_server.py", "chatbot.html", "critique.md"}
    assert distinct <= delivered, f"roles did not contribute distinct artifacts: {delivered}"

    # NOT just three filenames, read the CONTENT off the branch and assert each is
    # the role's own output, so compose is shown to have merged actual work:
    #   backend  -> the generated MCP server imports the module live (no copied logic)
    #   frontend -> the chatbot is UI markup wired to the endpoint via fetch()
    #   reviewer -> the critique carries the EXACT LGTM pass token
    server_src = _git_show_on(branch, "mcp_server.py")
    assert "import cost_analyzer" in server_src, \
        "composed mcp_server.py does not import the module (compose merged a stub?)"
    chatbot_src = _git_show_on(branch, "chatbot.html")
    assert "<!DOCTYPE html>" in chatbot_src and "tools/call" in chatbot_src \
        and "fetch(" in chatbot_src, "composed chatbot.html is not real wired UI markup"
    critique_src = _git_show_on(branch, "critique.md")
    assert LGTM_TOKEN in critique_src, \
        "composed critique.md is missing the exact LGTM pass token"


# 6b. DISTRIBUTION: a backend patch dispatches ONLY claude-code as a ROLE;
#      the frontend role (opencode) truly does not run.
def test_06b_stage2_backend_patch_dispatches_only_claude_code(console, cookie):
    """'fix the server version string...' -> patch/backend-v1, only claude-code
    is a dispatched role (agents/roles/progress all == just claude-code), and the
    non-dispatched roles (kiro, opencode) never run; no progress row AND no
    terminal pane. The review orchestrator still reviews (its verdict lands in
    the run log and result), but it must not fabricate a pane for a role the
    router never dispatched."""
    rid = _submit(console, cookie, "fix the server version string to v2 in mcp_server.py")
    route = _route_of(console, cookie, rid)
    assert route["workflow_ref"] == "patch/backend-v1", route
    assert route["agents"] == ["claude-code"], route
    run = _poll_terminal(console, cookie, rid)
    assert run["status"] in ("passed", "needs_human"), run   # routed-role-only run

    # the source of truth for "who ran": the dispatched roles (progress)…
    _, full = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
    assert full["roles"] == {"claude-code": "backend-mcp"}, full["roles"]
    assert {p["agent"] for p in full["progress"]} == {"claude-code"}, full["progress"]

    # NO race/winner artifact: the single dispatched role finished `done`, and NO
    # progress entry is in a "cancelled" state (a routed single-role run never
    # cancels a competitor; there is no competitor to cancel).
    if run["status"] == "passed":
        backend = next(p for p in full["progress"] if p["agent"] == "claude-code")
        assert backend["state"] == "done", f"dispatched role not done (race?): {backend}"
    assert not any(p["state"] == "cancelled" for p in full["progress"]), \
        f"a cancelled progress row is a race artifact: {full['progress']}"

    # …and the terminal panes must agree: ONLY the dispatched role has one.
    terms = _terminals(console, cookie, rid)
    assert "claude-code" in terms
    assert "claude-code-validator" not in terms, f"claude-code-validator pane on a backend patch (distribution broke): {list(terms)}"
    assert "opencode" not in terms, f"opencode ran on a backend patch (distribution broke): {list(terms)}"


# 6c. DISTRIBUTION: a frontend restyle dispatches ONLY opencode.
def test_06c_stage2_frontend_patch_dispatches_only_opencode(console, cookie):
    """'use opencode to restyle the chatbot header' -> patch/frontend-v1, only opencode
    is a DISPATCHED role (the engine may stand up an infra endpoint, but no
    backend/validator ROLE is dispatched)."""
    rid = _submit(console, cookie, "use opencode to restyle the chatbot header")
    route = _route_of(console, cookie, rid)
    assert route["workflow_ref"] == "patch/frontend-v1", route
    assert route["agents"] == ["opencode"], route
    run = _poll_terminal(console, cookie, rid)
    assert run["status"] in ("passed", "needs_human"), run

    # NO race/winner artifact: the single dispatched role (opencode) is the only one in
    # progress; on pass it finished `done`, and NO progress row is "cancelled".
    _, full = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
    assert {p["agent"] for p in full["progress"]} == {"opencode"}, full["progress"]
    if run["status"] == "passed":
        fe = next(p for p in full["progress"] if p["agent"] == "opencode")
        assert fe["state"] == "done", f"dispatched role not done (race?): {fe}"
    assert not any(p["state"] == "cancelled" for p in full["progress"]), \
        f"a cancelled progress row is a race artifact: {full['progress']}"

    terms = _terminals(console, cookie, rid)
    assert "opencode" in terms
    # claude-code is NOT a dispatched role here (no backend role on a frontend patch)
    assert "claude-code" not in terms, f"claude-code ran on a frontend patch: {terms.keys()}"


# 6d. READ-ONLY review: 'review the PR from the last run' -> review/pr-v1, only
#      claude-code-validator, and it produces NO new deliverable (no NEW compose commit).
def test_06d_stage2_review_is_read_only_no_new_deliverable(console, cookie):
    """'review the PR from the last run' -> review/pr-v1, only claude-code-validator
    dispatched, read_only True; and the read-only workflow composes NOTHING NEW.

    What distinguishes a review from a build in the result payload: `composed_commit`
    stays null (a review creates NO new commit; engine._finalize only composes when
    `not read_only`). The review DOES adopt the reviewed run's branch name for the
    branch-discipline critique (engine._execute_review sets composed_branch =
    target.composed_branch), so the branch maps back to the TARGET run, not a fresh
    one; that mapping is exactly the read-only contract, not a new deliverable."""
    rid = _submit(console, cookie, "review the PR from the last run")
    route = _route_of(console, cookie, rid)
    assert route["workflow_ref"] == "review/pr-v1", route
    assert route["agents"] == ["claude-code-validator"], route
    assert route["read_only"] is True
    run = _poll_terminal(console, cookie, rid)
    assert run["status"] in ("passed", "needs_human"), run
    terms = _terminals(console, cookie, rid)
    assert "claude-code-validator" in terms
    # neither build role runs (and neither has a finalization side-channel here)
    assert "claude-code" not in terms and "opencode" not in terms, terms.keys()
    if run["status"] == "passed":
        res = _result(console, cookie, rid)
        # read-only: NO new compose commit (the distinguishing field), NO PR
        assert res["composed_commit"] is None, "a review must not compose a NEW deliverable"
        assert res["pr_url"] is None
        # the borrowed branch (if any) maps back to the run UNDER review, never a fresh
        # run/<this_review_id> branch; i.e. the review owns no deliverable of its own.
        if res["composed_branch"] is not None:
            assert res["composed_branch"] != f"run/{rid}", \
                "a review must not open a branch of its OWN; it borrows the target's"


# 6e. FULL-STACK: the Critter Lab phrasing -> build/fullstack-v1, all three,
#      terminal status, gate checks reference the critter grading.
def test_06e_stage2_critter_lab_fullstack_all_three_roles(console, cookie):
    """The Critter Lab full-stack phrasing -> build/fullstack-v1, all three roles,
    reaches a terminal status; on pass the gate references the CRITTER grading set
    (card_renders is unique to the critter contract)."""
    rid = _submit(console, cookie,
                  "Build the full-stack Critter Lab app: backend and frontend")
    ATTENDEE["critter_run"] = rid
    route = _route_of(console, cookie, rid)
    assert route["workflow_ref"] == "build/fullstack-v1", route
    assert set(route["agents"]) == {"claude-code", "claude-code-validator", "opencode"}, route
    assert route["usecase"] == "critter-lab", route
    run = _poll_terminal(console, cookie, rid)
    assert run["status"] in ("passed", "needs_human"), run
    if run["status"] == "passed":
        res = _result(console, cookie, rid)
        gate_checks = {c["check"] for c in res["gate"]["checks"]}
        # the critter contract adds `card_renders` on top of the shared three
        assert "card_renders" in gate_checks, gate_checks
        assert res["gate"]["passed"] is True


# 6g. the SIDEBAR runs list: GET /api/orchestrator/runs is what the console renders as
#      the run history rail. It must hold every run this station submitted (the
#      convert run + the Critter Lab run by id), each as a structured record.
def test_06g_stage2_runs_list_api(console, cookie):
    """GET /api/orchestrator/runs -> a non-empty list that contains the convert run and the
    Critter Lab run by id, each carrying {run_id, task, status, route}."""
    _, body = _req(console, "GET", "/api/orchestrator/runs", headers=cookie)
    runs = body["runs"]
    assert isinstance(runs, list) and runs, body
    by_id = {r["run_id"]: r for r in runs}
    for rid in (ATTENDEE["convert_run"], ATTENDEE["critter_run"]):
        assert rid in by_id, f"run {rid} missing from the sidebar runs list"
        r = by_id[rid]
        assert {"run_id", "task", "status", "route"} <= set(r), r
        assert r["task"] and isinstance(r["task"], str)
        assert r["route"] and r["route"].get("workflow_ref"), r["route"]


# ===========================================================================
# 07. STAGE 3 / GOVERNANCE: dashboard, cost, latency, audit, and recorded user
#   identity all aggregate the runs the attendee just executed.
#   content/40-stage3-governance/1-obo-identity, 2-per-user-cost-api, 3-deploy-and-observe.
# ===========================================================================
def test_07_stage3_governance_reflects_the_real_journey(console, cookie):
    """The dashboard totals reflect the runs above; cost-breakdown by=agent has
    all three agents; by=user attributes to the local user; p95 is a number >= 0;
    the audit trail has run entries; a session reports only recorded evidence."""
    me = getpass.getuser()

    # dashboard: at least the 5 Stage-2 runs above are in the ledger as sessions
    _, dash = _req(console, "GET", "/api/metrics/dashboard", headers=cookie)
    assert dash["runs_total"] >= 5, dash
    assert isinstance(dash["p95_latency_ms"], (int, float)) and dash["p95_latency_ms"] >= 0

    # sessions list is non-empty
    _, sess = _req(console, "GET", "/api/metrics/sessions", headers=cookie)
    assert len(sess["sessions"]) >= 1

    # cost-breakdown by=agent has rows for all three agents (attribution, no winner)
    _, by_agent = _req(console, "GET", "/api/metrics/cost-breakdown?by=agent", headers=cookie)
    assert by_agent["by"] == "agent"
    assert {"claude-code", "claude-code-validator", "opencode"} <= set(by_agent["breakdown"]), by_agent

    # cost-breakdown by=user attributes to the local OS user
    _, by_user = _req(console, "GET", "/api/metrics/cost-breakdown?by=user", headers=cookie)
    assert by_user["by"] == "user"
    assert me in by_user["breakdown"], by_user

    # p95 latency scoped to the user is a real number >= 0
    _, p95 = _req(console, "GET", f"/api/metrics/latency/p95?user_id={me}", headers=cookie)
    assert isinstance(p95["p95_latency_ms"], (int, float)) and p95["p95_latency_ms"] >= 0
    assert p95["scope"].get("user_id") == me

    # audit trail has structured entries, including the orchestrator runs
    _, audit = _req(console, "GET", "/api/metrics/audit?limit=100", headers=cookie)
    assert isinstance(audit["audit"], list) and len(audit["audit"]) >= 1
    assert all({"at", "kind", "user_id", "line"} <= set(row) for row in audit["audit"])
    assert any(row["kind"] == "orchestrator_run" for row in audit["audit"]), \
        "the Stage-2 runs must appear in the governance audit feed"

    # User attribution: the ledger records who started the run. It does not infer
    # OAuth delegation or GitHub authorship from that metadata.
    sid = sess["sessions"][0]["session_id"]
    _, ident = _req(console, "GET", f"/api/metrics/sessions/{sid}/identity", headers=cookie)
    assert ident["session_id"] == sid
    assert ident["recorded_user"]
    assert ident["attribution_source"] == "run-ledger"
    assert ident["github_actor"] == "credential-dependent"
    assert ident["static_credentials_on_agent"] is False
    assert ident["environment"] in ("local", "agentcore")
# ===========================================================================
# 07b. STAGE 2 -> STAGE 3 CONTINUITY: the run ids the orchestrator produced in
#   Stage 2 are the SAME ids the governance layer accounts for in Stage 3; proof
#   the ledger threads one identity across stages (no re-minted/duplicate ids).
# ===========================================================================
def test_07b_stage2_run_ids_flow_into_stage3_governance(console, cookie):
    """The convert run and the Critter Lab run captured in Stage 2 each surface in
    the Stage 3 audit feed (as an orchestrator_run line) AND in the metrics session
    list (as `{run_id}-{agent}` rows), with the canonical run id format, proving
    Stage 2 -> Stage 3 continuity over the shared ledger, not two disjoint stores."""
    import re

    convert_run = ATTENDEE["convert_run"]
    critter_run = ATTENDEE["critter_run"]
    run_fmt = re.compile(r"^run_[0-9]{6}_[0-9]{3}$")
    assert run_fmt.match(convert_run), convert_run
    assert run_fmt.match(critter_run), critter_run

    # the audit feed: each Stage-2 run id appears verbatim in an orchestrator_run line
    _, audit = _req(console, "GET", "/api/metrics/audit?limit=200", headers=cookie)
    orch_lines = [row["line"] for row in audit["audit"]
                  if row["kind"] == "orchestrator_run"]
    assert any(convert_run in line for line in orch_lines), \
        f"convert run {convert_run} missing from the Stage-3 audit feed"
    assert any(critter_run in line for line in orch_lines), \
        f"critter run {critter_run} missing from the Stage-3 audit feed"

    # the metrics session list: a run becomes one session per dispatched role
    # ({run_id}-{agent}); the Stage-2 ids are the prefix of those Stage-3 sessions.
    _, sess = _req(console, "GET", "/api/metrics/sessions", headers=cookie)
    session_ids = {s["session_id"] for s in sess["sessions"]}
    assert any(sid.startswith(convert_run + "-") for sid in session_ids), \
        f"no Stage-3 session derived from convert run {convert_run}"
    assert any(sid.startswith(critter_run + "-") for sid in session_ids), \
        f"no Stage-3 session derived from critter run {critter_run}"

    # STRONGER continuity: EACH dispatched role of each Stage-2 run becomes its own
    # `{run_id}-{agent}` Stage-3 session (a run IS N Runtime sessions, one per role).
    # Pull the dispatched agents straight off the Stage-2 run and require every one
    # to have produced its derived governance session; no role silently dropped.
    for rid in (convert_run, critter_run):
        _, full = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        dispatched = [p["agent"] for p in full["progress"]]
        assert dispatched, f"run {rid} reported no dispatched roles"
        for agent in dispatched:
            assert f"{rid}-{agent}" in session_ids, \
                f"Stage-3 session {rid}-{agent} missing for dispatched role {agent}"


# ===========================================================================
# 08. CLEANUP: stop the live Stage-1 preview process via the kill switch, delete
#   its session, and confirm it is gone from the Stage 1 list.
#   content/90-cleanup.
# ===========================================================================
def test_08_cleanup_stop_session_and_delete_stage1(console, cookie):
    """Stop the real local Stage-1 preview process, then DELETE its session and
    assert it is no longer open in /api/dev."""
    s1 = ATTENDEE["stage1_session"]
    _, stopped = _req(console, "POST", f"/api/metrics/sessions/{s1}/stop", {},
                      headers=cookie)
    assert stopped["session_id"] == s1
    assert stopped["stopped"] is True
    assert stopped["mechanism"] == "local-process-signal"

    # DELETE the live Stage 1 session
    code, closed = _req(console, "DELETE", f"/api/dev/sessions/{s1}", headers=cookie)
    assert code == 200
    assert closed["status"] == "closed"

    # the deleted session is no longer 'open' (a subsequent input is rejected 409)
    with pytest.raises(HTTPError) as e:
        _req(console, "POST", f"/api/dev/sessions/{s1}/input",
             {"input": "ls"}, headers=cookie)
    assert e.value.code == 409, "a closed session must reject further input"

    # …and PTY I/O on a closed session is rejected the same way (the interactive
    # terminal cannot keep driving a torn-down microVM session): _pty_io guards on
    # status, so the dispatch returns 409, never a 200 that pretends the shell lives.
    with pytest.raises(HTTPError) as e:
        _req(console, "POST", f"/api/dev/sessions/{s1}/pty",
             {"input": "ls\n", "offset": 0}, headers=cookie)
    assert e.value.code == 409, "a closed session must reject further PTY I/O"


# ===========================================================================
# 08b. CLOSED-SESSION FILE OPS: once a session is closed, the file explorer's
#   write/delete/rename ops on it must NOT silently succeed. The source-side guard
#   lives in interactive_api.dispatch (the `action == "file"` branch), matching the
#   guard every other action carries; a closed session returns 409, never a 200
#   that mutates a torn-down workspace.
# ===========================================================================
def test_08b_closed_session_file_ops_are_rejected(console, cookie):
    """EVERY file op on a CLOSED Stage 1 session must be rejected with 409 + an error,
    never a 200 that mutates a torn-down workspace. The dispatch guard fires before any
    op (write/delete/rename) runs, so all three are walled the same way."""
    s1 = ATTENDEE["stage1_session"]  # closed by test_08

    # (op-label, request body) for each explorer mutation an attendee could fire.
    # The targets are files the attendee actually authored this journey (the workspace
    # starts empty, so there is no seeded README to act on); the dispatch guard fires
    # before any op runs, so even a now-torn-down workspace returns 409.
    ops = [
        ("write", {"path": "after-close.txt", "content": "should be rejected"}),
        ("delete", {"path": "cost_analyzer.py", "op": "delete"}),
        ("rename", {"path": "cost_analyzer.py", "op": "rename", "to": "renamed.py"}),
    ]
    for label, body in ops:
        try:
            code, w = _req(console, "POST", f"/api/dev/sessions/{s1}/file",
                           body, headers=cookie)
        except HTTPError as e:
            assert e.code == 409, f"closed-session {label} returned {e.code}, expected 409"
            continue
        # No HTTP error → the dispatch must have refused via a 409 status payload
        # carrying an error (the journey _req only raises on 4xx/5xx, so a 409 lands
        # here as a raised HTTPError above; a 200 body without "error" is the failure).
        assert "error" in w, f"closed-session {label} was NOT rejected: {code} {w}"


# ===========================================================================
# 09. HONEST LOCAL ZERO (the non-negotiable): in local engine mode NO model is
#   invoked, so EVERY role of EVERY run this journey produced must report exactly
#   zero tokens and zero cost. A non-zero figure here would be a fabricated usage
#   number; the workshop's "honest zero in local mode" promise, broken.
# ===========================================================================
def test_09_all_runs_honest_zero_in_local_mode(console, cookie):
    """GET /api/orchestrator/runs; for EVERY run, pull its per-role progress and assert each
    role reports tokens == 0 AND cost_usd == 0. Fail loud with run_id + agent + value
    on the first violation (no model ran locally; usage is an honest zero, not
    inferred from wall-clock)."""
    _, body = _req(console, "GET", "/api/orchestrator/runs", headers=cookie)
    runs = body["runs"]
    assert runs, "no Stage 2 runs in the ledger to audit for honest zero"
    seen_roles = 0
    for r in runs:
        rid = r["run_id"]
        _, full = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        for p in full.get("progress", []):
            seen_roles += 1
            assert p["tokens"] == 0, \
                f"local mode must report zero tokens: {rid}/{p['agent']} = {p['tokens']}"
            assert p["cost_usd"] == 0, \
                f"local mode must report zero cost: {rid}/{p['agent']} = {p['cost_usd']}"
    # the audit is only meaningful if it actually inspected role rows
    assert seen_roles >= 1, "no per-role progress rows were audited for honest zero"
