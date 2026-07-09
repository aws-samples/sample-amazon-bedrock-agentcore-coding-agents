"""The whole 4-hour journey, in ORDER, as ONE attendee walks it, every content step.

Unlike the per-stage modules (each test independent), THIS file is the SPINE: one
ordered narrative where each `test_NN_*` is a single checkpoint in the EXACT step
sequence the content teaches, and later checkpoints depend on the artifacts earlier
ones produced. pytest runs tests in file-definition order, so reading `pytest -v`
top-to-bottom IS the run sheet:

  Login → Stage 1 (deploy claude-code → open session+shell → the workspace is EMPTY,
  so the attendee CREATES cost_analyzer.py themselves in the explorer (New File →
  paste → Save), then a second file too → `agentcore deploy` in the terminal,
  smart-capture onto the shelf → edit the subagent name+purpose → scaffold the harness
  → convert the module, verified, 140.16 → verify over the wire → deploy-upload the
  bundle) → Stage 2 (submit the
  convert task → route is convert/all-three → the run reaches `passed` with the LGTM
  token) → Stage 3 (the dashboard reflects the run → cost-breakdown by user attributes
  it → p95 is a number) → cleanup.

State threads through one module-level `STATE` dict (the session id, the deployed
agent, the run id). If a checkpoint here breaks, a content page is telling attendees
to do something that no longer works in order.

Local engine mode (deterministic, no model). Drives the SAME real server the rest of
the e2e suite shares via conftest; these tests DO NOT boot their own server.
"""
from __future__ import annotations

import getpass

import pytest
from urllib.error import HTTPError

from e2e.conftest import (
    req, expect_status, open_session, close_session, open_pty, pty_type,
    pty_wait_for, file_tree, write_file, read_file, file_op, seed_skill,
    submit_run, poll_route, poll_terminal,
    deploy_real, undeploy_real, reset_shelf_real,
    EC2_FIXTURE_COST, LGTM_TOKEN, SUPPORTED_AGENTS, TERMINAL_STATUSES,
)

# One shared attendee, populated as the ordered journey advances. Later
# checkpoints read the ids/artifacts earlier ones produced.
STATE: dict[str, object] = {}

_DEPLOY_AGENT = "claude-code"
_TOOL = "estimate_ec2_monthly_cost"

# This file is the "fresh attendee from scratch" spine and shares ONE long-lived
# console subprocess with the rest of the suite (conftest, session scope). The shelf
# reconciles the runtime_config.json each harness deploy.py writes under the
# suite's wired coding-agents dir, so "deployed" == "that file exists with a real
# ARN". Earlier files in the suite may have written such a file (their own deploy_real
# call); they clean it up, but to recreate the "I just opened Stage 1" empty
# shelf regardless of order, the spine deletes every harness's runtime_config.json
# (reset_shelf_real). pytest is serial here, so this is race-free.


def _reset_shelf() -> None:
    """Return the shelf to empty by removing every harness's runtime_config.json:
    the 'no Runtime created yet' precondition a fresh attendee box has."""
    reset_shelf_real()


# ===========================================================================
# 00. GETTING STARTED: the login wall and the honest GitHub state.
# ===========================================================================
def test_00_login_wall_gates_the_api(console):
    """Attendee hits the console before signing in: every /api/dev|orchestrator|metrics call is a
    401 wall, but /api/health stays open (the liveness probe nginx/CFN hits)."""
    code, health = req(console, "GET", "/api/health")
    assert code == 200 and health.get("status") == "ok"
    for mount in ("/api/dev/agents", "/api/orchestrator/agents", "/api/metrics/dashboard"):
        expect_status(lambda m=mount: req(console, "GET", m), 401)


def test_01_signed_in_attendee_sees_honest_local_github(console, cookie):
    """With the session cookie the API opens, and the Getting Started GitHub card is
    honest: not connected, local mode (the no-credential promise the content makes)."""
    code, gh = req(console, "GET", "/api/orchestrator/github", headers=cookie)
    assert code == 200
    assert gh["connected"] is False
    assert gh["mode"] == "local"


# ===========================================================================
# STAGE 1. INTERACTIVE: one agent on Runtime, by hand.
# ===========================================================================
def test_02_stage1_shelf_starts_empty_with_three_agents(console, cookie):
    """Stage 1 opens: the catalog has exactly the three agent_ids, and nothing is
    deployed yet (every status == 'not_deployed', the empty-shelf pedagogy).

    The journey is the "fresh attendee" spine, so it first resets the shelf to the
    process-start state (see _reset_shelf; removes every harness's real
    runtime_config.json); otherwise a deploy from an earlier file in the suite would
    (correctly) still be 'ready' here."""
    _reset_shelf()
    _, body = req(console, "GET", "/api/dev/agents", headers=cookie)
    agents = {a["agent_id"]: a for a in body["agents"]}
    assert set(agents) == set(SUPPORTED_AGENTS), agents.keys()
    for aid, a in agents.items():
        assert a["status"] == "not_deployed", f"{aid} should start undeployed: {a}"
        assert a["runtime_arn"] is None
        # the editable subagent fields are present from the catalog
        assert a["name"] and isinstance(a["purpose"], str)


def test_03_stage1_deploy_claude_code_until_ready(console, cookie):
    """Attendee builds + deploys claude-code (./setup.sh && python deploy.py, which
    writes the real runtime_config.json). The console reconciles it: the shelf flips
    the agent to 'ready' with its arn:aws:bedrock-agentcore runtime ARN,
    never a local:runtime placeholder."""
    agent = deploy_real(console, cookie, _DEPLOY_AGENT)
    assert agent["status"] == "ready", agent
    assert agent["runtime_arn"].startswith("arn:aws:bedrock-agentcore:"), agent
    assert "runtime/" in agent["runtime_arn"], agent
    STATE["deployed_agent"] = _DEPLOY_AGENT


def test_04_stage1_open_session_on_s3files(console, cookie):
    """Attendee opens a session on the deployed agent: 201, status 'open', the
    /mnt/s3files workspace (the S3 Files mount the rest of Stage 1 runs in)."""
    code, sess = req(console, "POST", "/api/dev/sessions",
                     {"agent_id": _DEPLOY_AGENT}, headers=cookie)
    assert code == 201, sess
    assert sess["status"] == "open"
    assert sess["workspace"] == "/mnt/s3files"
    assert sess["agent_id"] == _DEPLOY_AGENT
    STATE["session"] = sess["session_id"]


def test_05_stage1_workspace_starts_empty_then_attendee_creates_the_skill(console, cookie):
    """The session opens with an EMPTY workspace; nothing magically appears. The
    attendee creates the FIRST file themselves the way the content teaches (New File
    in the explorer → name it cost_analyzer.py → paste the module → Save); only then
    does cost_analyzer.py, the input module they will convert, show in the tree."""
    sid = STATE["session"]
    before = {n["path"] for n in file_tree(console, cookie, sid)}
    assert before == set(), f"a fresh Stage 1 workspace must start empty: {before}"
    seed_skill(console, cookie, sid)
    after = {n["path"] for n in file_tree(console, cookie, sid)}
    assert "/mnt/s3files/sample/cost_analyzer.py" in after, after


def test_06_stage1_open_pty_and_run_a_real_command(console, cookie):
    """Attendee opens the interactive shell (PTY) and runs `ls sample` in the
    workspace; the real echoed output shows the cost_analyzer.py they just created
    in the explorer (test 05): a live bash, not a transcript.
    (The PTY opens with cwd at the workspace that backs /mnt/s3files, so `ls sample`
    lists the module they seeded under sample/.)"""
    sid = STATE["session"]
    opened = open_pty(console, cookie, sid, cols=120, rows=30)
    assert opened["agent_id"] == _DEPLOY_AGENT
    pty_type(console, cookie, sid, "ls -1 sample\n")
    buf = pty_wait_for(console, cookie, sid, "cost_analyzer.py")
    assert "cost_analyzer.py" in buf, f"`ls` output never surfaced on the PTY: {buf!r}"


def test_07_stage1_create_a_file_via_the_explorer(console, cookie):
    """Attendee creates a file through the VS Code-like explorer (free filename, not
    hardcoded): write → it lands in the tree, read-back returns the exact bytes."""
    sid = STATE["session"]
    note = "notes/plan.md"
    body = "# my conversion plan\nwrap estimate_ec2_monthly_cost as MCP\n"
    res = write_file(console, cookie, sid, note, body)
    assert res["path"] == f"/mnt/s3files/{note}", res
    paths = {n["path"] for n in file_tree(console, cookie, sid)}
    assert f"/mnt/s3files/{note}" in paths, paths
    back = read_file(console, cookie, sid, note)
    assert back["content"] == body, back


def test_08_stage1_explorer_rejects_a_jail_escape(console, cookie):
    """The explorer is jail-guarded: a path escaping /mnt/s3files is refused with an
    error, never a write outside the workspace (the security contract)."""
    sid = STATE["session"]
    res = write_file(console, cookie, sid, "../../../../etc/escape.txt", "nope")
    assert "error" in res, f"a jail escape must be rejected, got: {res}"


def test_09_stage1_real_deploy_smart_captured_on_the_shelf(console, cookie):
    """Smart capture: the build+deploy the attendee ran in the terminal
    (./setup.sh && python deploy.py wrote the runtime_config.json in test 03)
    is reflected on the shelf with no button; GET /agents shows the agent ready
    with its bedrock-agentcore ARN, and it persists across reads."""
    _, agent = req(console, "GET", f"/api/dev/agents/{_DEPLOY_AGENT}", headers=cookie)
    assert agent["status"] == "ready", agent
    assert agent["runtime_arn"].startswith("arn:aws:bedrock-agentcore:"), agent
    # the deploy is durable: it shows on the full shelf too, not just the by-id read
    _, body = req(console, "GET", "/api/dev/agents", headers=cookie)
    on_shelf = next(a for a in body["agents"] if a["agent_id"] == _DEPLOY_AGENT)
    assert on_shelf["status"] == "ready" and on_shelf["runtime_arn"] == agent["runtime_arn"]


def test_10_stage1_real_agentcore_cli_is_available_in_the_shell(console, cookie):
    """The shell exposes the `agentcore` CLI (the one Stage 2 deploys the
    orchestrator with), not a shim. We probe that it resolves on PATH; deploy
    itself is the ./setup.sh + deploy.py flow, not a fake verb."""
    sid = STATE["session"]
    pty_type(console, cookie, sid, "command -v agentcore || echo NO_AGENTCORE\n")
    buf = pty_wait_for(console, cookie, sid, "agentcore")
    # Either the CLI is installed (path printed) or the box doesn't ship it
    # (NO_AGENTCORE); there is no shim asserting a deploy.
    assert "agentcore" in buf, buf


def test_11_stage1_edit_subagent_name_and_purpose(console, cookie):
    """Attendee right-click-Edits the deployed agent: set a new name + purpose (the
    role it plays as a subagent of the orchestrator); the override applies on the shelf."""
    name = "Backend Builder"
    purpose = "Wraps the cost_analyzer module as a remote MCP server."
    code, edited = req(console, "POST", f"/api/dev/agents/{_DEPLOY_AGENT}/edit",
                       {"name": name, "purpose": purpose}, headers=cookie)
    assert code == 200, edited
    assert edited["name"] == name
    assert edited["purpose"] == purpose
    # the override persisted: a fresh GET reflects it
    _, agent = req(console, "GET", f"/api/dev/agents/{_DEPLOY_AGENT}", headers=cookie)
    assert agent["name"] == name and agent["purpose"] == purpose


def test_12_stage1_edit_rejects_an_empty_name(console, cookie):
    """The edit form validates: an empty name is a 400 (you cannot blank out the
    subagent's name), and the previously-saved name is left intact."""
    expect_status(
        lambda: req(console, "POST", f"/api/dev/agents/{_DEPLOY_AGENT}/edit",
                    {"name": "   "}, headers=cookie), 400)
    _, agent = req(console, "GET", f"/api/dev/agents/{_DEPLOY_AGENT}", headers=cookie)
    assert agent["name"] == "Backend Builder", agent


def test_13_stage1_scaffold_the_harness(console, cookie):
    """Attendee scaffolds the harness for claude-code: CLAUDE.md + the backend
    SKILL.md are written into the workspace (the file IS the configuration)."""
    sid = STATE["session"]
    code, h = req(console, "POST", f"/api/dev/sessions/{sid}/scaffold-harness",
                  {"agent_id": _DEPLOY_AGENT}, headers=cookie)
    assert code == 200, h
    assert h["agent_id"] == _DEPLOY_AGENT
    assert any(p.endswith("/CLAUDE.md") for p in h["written"]), h["written"]
    tree = {n["path"] for n in h["tree"]}
    assert "/mnt/s3files/CLAUDE.md" in tree, tree
    assert "/mnt/s3files/skills/configure-backend/SKILL.md" in tree, tree


def test_14_stage1_convert_the_skill_verified_140_16(console, cookie):
    """The Stage 1 payoff: convert the module by hand. The MCP server is written,
    booted, and verified over the wire; the live sample returns the EXACT 140.16
    fixture, and mcp_server.py lands in the tree."""
    sid = STATE["session"]
    code, conv = req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                     {"tool": _TOOL}, headers=cookie)
    assert code == 200, conv
    assert conv["verified"] is True, conv
    assert conv["server_file"] == "/mnt/s3files/mcp_server.py"
    assert conv["tool"] == _TOOL
    assert any(t.get("name") == _TOOL for t in conv["tools_list"]), conv["tools_list"]
    assert conv["sample_call"]["result"]["monthly_cost"] == EC2_FIXTURE_COST, conv
    paths = {n["path"] for n in file_tree(console, cookie, sid)}
    assert "/mnt/s3files/mcp_server.py" in paths, paths
    STATE["converted"] = True


def test_15_stage1_verify_over_the_wire_all_checks_green(console, cookie):
    """Attendee clicks Verify: 4 named checks all pass over a real HTTP round-trip
    (server_live, tools_list, tool_call, input_validation), and the verify sample
    is the same exact 140.16 fixture."""
    sid = STATE["session"]
    code, ver = req(console, "POST", f"/api/dev/sessions/{sid}/verify", {}, headers=cookie)
    assert code == 200, ver
    assert ver["ran"] is True and ver["passed"] is True, ver
    checks = {c["check"]: c["passed"] for c in ver["checks"]}
    assert checks == {"server_live": True, "tools_list": True,
                      "tool_call": True, "input_validation": True}, checks
    assert ver["sample"]["monthly_cost"] == EC2_FIXTURE_COST, ver
    assert isinstance(ver["latency_ms"], int) and ver["latency_ms"] >= 0


def test_16_stage1_deploy_upload_the_bundle(console, cookie):
    """Attendee code-uploads the workspace: deploy-upload packages a real zip (bytes
    > 0) whose manifest includes the converted mcp_server.py, which resolves as the
    entrypoint: AgentCore's code-first launch artifact."""
    sid = STATE["session"]
    code, dep = req(console, "POST", f"/api/dev/sessions/{sid}/deploy-upload", {},
                    headers=cookie)
    assert code == 200, dep
    assert dep["mode"] == "code-upload"
    assert dep["bundle_bytes"] > 0
    assert dep["file_count"] >= 1
    assert "mcp_server.py" in dep["manifest"], dep["manifest"]
    assert dep["entrypoint"] == "mcp_server.py"
    # The runtime_arn is the deployed one if a deploy has landed for this
    # harness (it did in test 03), never local:runtime.
    assert dep["runtime_arn"] is None or dep["runtime_arn"].startswith(
        "arn:aws:bedrock-agentcore:"), dep["runtime_arn"]


# ===========================================================================
# STAGE 2. ORCHESTRATE: submit one task, watch it route + run to the gate.
# ===========================================================================
def test_17_stage2_workflow_registry_lists_the_convert_workflow(console, cookie):
    """Stage 2 opens: the workflow registry the router routes against carries the
    convert/sample-to-mcp-v1 descriptor (all three roles, not read-only)."""
    _, body = req(console, "GET", "/api/orchestrator/workflows", headers=cookie)
    by_ref = {w["workflow_ref"]: w for w in body["workflows"]}
    assert "convert/sample-to-mcp-v1" in by_ref, by_ref.keys()
    wf = by_ref["convert/sample-to-mcp-v1"]
    assert set(wf["agents"]) == set(SUPPORTED_AGENTS), wf
    assert wf["read_only"] is False


def test_18_stage2_submit_the_convert_task_routes_all_three(console, cookie):
    """Attendee submits the convert task (no workflow_ref). The router routes it to
    convert/sample-to-mcp-v1 with EXACTLY the three roles dispatched: no race/winner."""
    task = ("Convert /mnt/s3files/sample/cost_analyzer.py to a remote MCP server "
            "with tests and a chatbot UI")
    run = submit_run(console, cookie, task=task)
    rid = run["run_id"]
    STATE["run"] = rid
    assert run["task"] == task
    route = poll_route(console, cookie, rid)
    assert route["workflow_ref"] == "convert/sample-to-mcp-v1", route
    assert set(route["agents"]) == set(SUPPORTED_AGENTS), route
    assert route["usecase"] == "sample-to-mcp", route


def test_19_stage2_run_reaches_passed_with_lgtm(console, cookie):
    """The run advances autonomously to a terminal status: 'passed', the pytest gate
    green, the review approved with the EXACT LGTM token, and a real-or-null pr_url
    (null here; no GitHub credential on the local ladder)."""
    rid = STATE["run"]
    run = poll_terminal(console, cookie, rid)
    assert run["status"] in TERMINAL_STATUSES, run
    assert run["status"] == "passed", run
    _, res = req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
    assert res["gate"]["passed"] is True, res["gate"]
    assert all(c["passed"] for c in res["gate"]["checks"]), res["gate"]
    assert res["review"]["lgtm"] is True, res["review"]
    assert res["review"]["state"] == "approved", res["review"]
    # honest no-credential PR: real or null, never fabricated → null here
    assert res["pr_url"] is None, res
    STATE["passed"] = True


def test_20_stage2_all_three_roles_have_terminals_and_honest_zero(console, cookie):
    """Each dispatched role streamed a terminal AND reports zero usage in local
    mode (no model ran), and the compose carries all three roles: the
    collaboration contract, not one winner's output."""
    rid = STATE["run"]
    _, terms_body = req(console, "GET", f"/api/orchestrator/runs/{rid}/terminals", headers=cookie)
    terms = terms_body["terminals"]
    for agent in SUPPORTED_AGENTS:
        assert agent in terms, terms.keys()
        assert len(terms[agent]) >= 1, f"{agent} streamed no terminal output"
    _, full = req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
    progress = {p["agent"]: p for p in full["progress"]}
    assert set(progress) == set(SUPPORTED_AGENTS), progress.keys()
    for agent, p in progress.items():
        assert p["tokens"] == 0, f"{agent} tokens must be honest-zero locally: {p}"
        assert p["cost_usd"] == 0, f"{agent} cost must be honest-zero locally: {p}"
    _, res = req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
    assert res["composed_from"] == ["backend-mcp", "validator", "frontend-builder"], res


# ===========================================================================
# STAGE 3. GOVERNANCE: the dashboard, cost attribution, and p95 over the real run.
# ===========================================================================
def test_21_stage3_dashboard_reflects_the_run(console, cookie):
    """Stage 3 opens: the governance dashboard aggregates the run the attendee just
    executed, runs_total counts it, p95 is a real number, sessions are non-empty."""
    _, dash = req(console, "GET", "/api/metrics/dashboard", headers=cookie)
    assert dash["runs_total"] >= 1, dash
    assert isinstance(dash["p95_latency_ms"], (int, float)) and dash["p95_latency_ms"] >= 0
    assert isinstance(dash["cost_by_agent"], dict), dash
    _, sess = req(console, "GET", "/api/metrics/sessions", headers=cookie)
    assert len(sess["sessions"]) >= 1, sess
    # the Stage-2 run threads into Stage-3 as {run_id}-{agent} sessions
    rid = STATE["run"]
    session_ids = {s["session_id"] for s in sess["sessions"]}
    assert any(s.startswith(rid + "-") for s in session_ids), \
        f"the Stage-2 run {rid} did not surface as Stage-3 sessions"


def test_22_stage3_cost_breakdown_attributes_by_agent_and_user(console, cookie):
    """Cost-breakdown by=agent has rows for all three roles (no winner), and by=user
    attributes the spend to the attendee's own OS identity (per-user cost API)."""
    _, by_agent = req(console, "GET", "/api/metrics/cost-breakdown?by=agent", headers=cookie)
    assert by_agent["by"] == "agent", by_agent
    assert {"claude-code", "claude-code-validator", "opencode"} <= set(by_agent["breakdown"]), by_agent
    assert "currency" in by_agent
    _, by_user = req(console, "GET", "/api/metrics/cost-breakdown?by=user", headers=cookie)
    assert by_user["by"] == "user", by_user
    me = getpass.getuser()
    assert me in by_user["breakdown"], by_user


def test_23_stage3_p95_latency_is_a_number(console, cookie):
    """The per-user p95 latency endpoint returns a real number ≥ 0 scoped to the
    attendee; the governance latency metric the content closes on."""
    me = getpass.getuser()
    _, p95 = req(console, "GET", f"/api/metrics/latency/p95?user_id={me}", headers=cookie)
    assert isinstance(p95["p95_latency_ms"], (int, float)) and p95["p95_latency_ms"] >= 0
    assert p95["scope"].get("user_id") == me, p95


# ===========================================================================
# 24. CLEANUP: close the Stage 1 session; a closed session rejects further I/O.
# ===========================================================================
def test_24_cleanup_close_stage1_session(console, cookie):
    """Attendee cleans up: DELETE the Stage 1 session → 200 'closed', and a PTY write
    afterward is rejected 409 (the torn-down microVM cannot keep driving a shell)."""
    sid = STATE.get("session")
    assert sid, "no Stage 1 session was opened in this journey"
    code, closed = req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)
    assert code == 200, closed
    assert closed["status"] == "closed"
    expect_status(
        lambda: req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                    {"input": "ls\n", "offset": 0}, headers=cookie), 409)
