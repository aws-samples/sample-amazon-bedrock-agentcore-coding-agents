"""Stage 1 (Interactive) e2e: the agent catalog/shelf, deploying an agent, opening
a live session, and editing an agent's name/purpose.

Covers the Stage 1 workshop step "Deploy ONE coding agent, open an interactive shell
into its /mnt/s3files workspace": the agents catalog (GET /agents), smart-capture
deploy (POST /agents/deploy + the `agentcore deploy` PTY shim), right-click Edit of an
agent's display fields, and session lifecycle (open -> files/tools -> close -> 409).

Every test drives the real `console/server.py` over HTTP through the shared
conftest helpers. Tests are independent and clean up their own sessions; deploy state
is process-global (a marker on disk), so deploy tests never assert "not_deployed" after
a sibling test may have deployed; they assert the post-deploy contract directly.
"""
from __future__ import annotations

from urllib.error import HTTPError

from e2e.conftest import (
    req, expect_status, open_session, close_session, open_pty, pty_type,
    pty_wait_for, file_tree, write_file, read_file, file_op, seed_skill,
    deploy_real, undeploy_real, SUPPORTED_AGENTS,
)

S1 = "/api/dev"


# ---------------------------------------------------------------------------
# GET /agents: the catalog/shelf shape and the three agent ids.
# ---------------------------------------------------------------------------
def test_agents_list_shape(console, cookie):
    """Attendee loads the Stage 1 shelf: the agents catalog returns the documented fields."""
    code, out = req(console, "GET", f"{S1}/agents", headers=cookie)
    assert code == 200
    agents = out["agents"]
    assert isinstance(agents, list) and len(agents) == 3
    for a in agents:
        for field in ("agent_id", "label", "name", "purpose", "model",
                      "credential", "status", "runtime_arn", "endpoint"):
            assert field in a, f"missing {field} in {a}"


def test_agents_list_has_exactly_three_ids(console, cookie):
    """The shelf offers exactly claude-code, kiro, opencode; no more, no fewer."""
    _, out = req(console, "GET", f"{S1}/agents", headers=cookie)
    ids = {a["agent_id"] for a in out["agents"]}
    assert ids == set(SUPPORTED_AGENTS)


def test_agents_carry_model_and_credential(console, cookie):
    """Each catalog agent advertises its model + credential broker (taught on the shelf)."""
    _, out = req(console, "GET", f"{S1}/agents", headers=cookie)
    by_id = {a["agent_id"]: a for a in out["agents"]}
    assert by_id["claude-code"]["credential"] == "bedrock-native"
    assert by_id["opencode"]["model"] == "amazon-bedrock/us.anthropic.claude-sonnet-4-6"
    assert by_id["kiro"]["model"] == "auto"


# ---------------------------------------------------------------------------
# GET /agents/{id}: single-resource ok + 404, and /edit is not a GET.
# ---------------------------------------------------------------------------
def test_get_single_agent_ok(console, cookie):
    """Clicking an agent on the shelf fetches that one agent by id."""
    code, out = req(console, "GET", f"{S1}/agents/claude-code", headers=cookie)
    assert code == 200
    assert out["agent_id"] == "claude-code"
    assert out["label"] == "Claude Code"


def test_get_unknown_agent_404(console, cookie):
    """Asking for an agent that isn't in the catalog is a clean 404."""
    expect_status(lambda: req(console, "GET", f"{S1}/agents/nope", headers=cookie), 404)


def test_get_agent_edit_is_404(console, cookie):
    """The Edit sub-resource is POST-only; a GET to /agents/{id}/edit is 404."""
    expect_status(
        lambda: req(console, "GET", f"{S1}/agents/claude-code/edit", headers=cookie), 404)


# ---------------------------------------------------------------------------
# Deploy reconciliation: the shelf reads the runtime_config.json the harness
# deploy.py writes; status flips to ready ONLY with a bedrock-agentcore ARN.
# (deploy_real writes that config; the AWS CreateAgentRuntime call is the only
# external step it stands in for; the reconciliation under test runs for real.)
# ---------------------------------------------------------------------------
def test_deploy_claude_code_flips_ready(console, cookie):
    """A claude-code deploy lands a runtime_config.json; the shelf reconciles
    it to ready with an arn:aws:bedrock-agentcore runtime ARN."""
    try:
        out = deploy_real(console, cookie, "claude-code")
        assert out["status"] == "ready"
        assert out["runtime_arn"].startswith(
            "arn:aws:bedrock-agentcore:") and "runtime/" in out["runtime_arn"]
    finally:
        undeploy_real("claude-code")


def test_deploy_kiro_flips_ready(console, cookie):
    """A kiro deploy reconciles to ready with kiro's own runtime ARN."""
    try:
        out = deploy_real(console, cookie, "kiro")
        assert out["status"] == "ready"
        assert "runtime/kiro" in out["runtime_arn"]
    finally:
        undeploy_real("kiro")


def test_deploy_opencode_flips_ready(console, cookie):
    """A opencode deploy reconciles to ready with opencode's own runtime ARN."""
    try:
        out = deploy_real(console, cookie, "opencode")
        assert out["status"] == "ready"
        assert "runtime/opencode" in out["runtime_arn"]
    finally:
        undeploy_real("opencode")


def test_deploy_post_endpoint_reconciles_real_config(console, cookie):
    """POST /agents/deploy reconciles, it does not fake: once a runtime_config.json
    exists it returns 202 + the agent ready with the ARN, and re-posting is
    idempotent (stable ARN)."""
    try:
        deploy_real(console, cookie, "claude-code")
        code, first = req(console, "POST", f"{S1}/agents/deploy",
                          {"agent_id": "claude-code"}, headers=cookie)
        assert code == 202 and first["status"] == "ready", first
        assert first["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")
        _, second = req(console, "POST", f"{S1}/agents/deploy",
                        {"agent_id": "claude-code"}, headers=cookie)
        assert second["runtime_arn"] == first["runtime_arn"]
    finally:
        undeploy_real("claude-code")


def test_deploy_without_real_runtime_stays_deploying(console, cookie):
    """With no runtime_config.json (no CreateAgentRuntime yet), the deploy
    endpoint does NOT fake a ready: the agent stays 'deploying' with a null ARN,
    never a local:runtime placeholder."""
    undeploy_real("kiro")  # ensure no real config
    code, out = req(console, "POST", f"{S1}/agents/deploy",
                    {"agent_id": "kiro"}, headers=cookie)
    assert code == 202
    assert out["status"] == "deploying", out
    assert out["runtime_arn"] is None, out


def test_deploy_reflected_in_catalog(console, cookie):
    """After a deploy, the agent shows ready on the full GET /agents shelf too,
    carrying the runtime ARN."""
    try:
        deploy_real(console, cookie, "opencode")
        _, out = req(console, "GET", f"{S1}/agents", headers=cookie)
        opencode = next(a for a in out["agents"] if a["agent_id"] == "opencode")
        assert opencode["status"] == "ready"
        assert opencode["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")
    finally:
        undeploy_real("opencode")


def test_deploy_unknown_agent_404(console, cookie):
    """Deploying an agent id that doesn't exist is rejected with 404."""
    expect_status(
        lambda: req(console, "POST", f"{S1}/agents/deploy",
                    {"agent_id": "ghost"}, headers=cookie), 404)


# ---------------------------------------------------------------------------
# POST /agents/{id}/edit: rename + purpose, with validation edges.
# ---------------------------------------------------------------------------
def test_edit_name_and_purpose_persists(console, cookie):
    """Right-click Edit: a new name + purpose are saved and read back on the agent."""
    code, out = req(console, "POST", f"{S1}/agents/kiro/edit",
                    {"name": "Test Authority", "purpose": "Writes the gate."},
                    headers=cookie)
    assert code == 200
    assert out["name"] == "Test Authority"
    assert out["purpose"] == "Writes the gate."
    # Persisted: a fresh GET reflects the override.
    _, again = req(console, "GET", f"{S1}/agents/kiro", headers=cookie)
    assert again["name"] == "Test Authority"
    assert again["purpose"] == "Writes the gate."


def test_edit_empty_name_400(console, cookie):
    """An empty/whitespace name is rejected (an agent must keep a label)."""
    expect_status(
        lambda: req(console, "POST", f"{S1}/agents/claude-code/edit",
                    {"name": "   "}, headers=cookie), 400)


def test_edit_non_string_name_400(console, cookie):
    """A non-string name is rejected before it can crash the override store."""
    expect_status(
        lambda: req(console, "POST", f"{S1}/agents/claude-code/edit",
                    {"name": 123}, headers=cookie), 400)


def test_edit_non_string_purpose_400(console, cookie):
    """A non-string purpose is rejected the same way as a non-string name."""
    expect_status(
        lambda: req(console, "POST", f"{S1}/agents/claude-code/edit",
                    {"purpose": ["a", "list"]}, headers=cookie), 400)


def test_edit_over_2000_chars_400(console, cookie):
    """A name longer than 2000 chars is rejected (bounds the shared overrides JSON)."""
    expect_status(
        lambda: req(console, "POST", f"{S1}/agents/opencode/edit",
                    {"name": "x" * 2001}, headers=cookie), 400)


def test_edit_clear_purpose_to_empty_honored(console, cookie):
    """Clearing the purpose to "" is a real edit and overrides the catalog default."""
    # First set a non-empty purpose, then clear it.
    req(console, "POST", f"{S1}/agents/opencode/edit",
        {"purpose": "temporary"}, headers=cookie)
    code, out = req(console, "POST", f"{S1}/agents/opencode/edit",
                    {"purpose": ""}, headers=cookie)
    assert code == 200
    assert out["purpose"] == ""
    _, again = req(console, "GET", f"{S1}/agents/opencode", headers=cookie)
    assert again["purpose"] == ""


def test_edit_unknown_agent_404(console, cookie):
    """Editing an agent id that doesn't exist is a 404."""
    expect_status(
        lambda: req(console, "POST", f"{S1}/agents/ghost/edit",
                    {"name": "X"}, headers=cookie), 404)


# ---------------------------------------------------------------------------
# POST /sessions: open a live workspace for each agent; unknown -> 404.
# ---------------------------------------------------------------------------
def test_open_session_claude_code(console, cookie):
    """Attendee opens a Claude Code session: 201, open, workspace /mnt/s3files."""
    code, sess = req(console, "POST", f"{S1}/sessions",
                     {"agent_id": "claude-code"}, headers=cookie)
    try:
        assert code == 201
        assert sess["status"] == "open"
        assert sess["workspace"] == "/mnt/s3files"
        assert sess["agent_id"] == "claude-code"
        assert sess["session_id"]
    finally:
        close_session(console, cookie, sess["session_id"])


def test_open_session_kiro(console, cookie):
    """Opening a Kiro session yields an open workspace bound to the kiro agent."""
    sid = open_session(console, cookie, "kiro")
    try:
        _, sess = req(console, "GET", f"{S1}/sessions/{sid}", headers=cookie)
        assert sess["agent_id"] == "kiro"
        assert sess["workspace"] == "/mnt/s3files"
    finally:
        close_session(console, cookie, sid)


def test_open_session_opencode(console, cookie):
    """Opening a opencode session yields an open workspace bound to the opencode agent."""
    sid = open_session(console, cookie, "opencode")
    try:
        _, sess = req(console, "GET", f"{S1}/sessions/{sid}", headers=cookie)
        assert sess["agent_id"] == "opencode"
    finally:
        close_session(console, cookie, sid)


def test_open_session_unknown_agent_404(console, cookie):
    """Opening a session for an agent that isn't in the catalog is a 404."""
    expect_status(
        lambda: req(console, "POST", f"{S1}/sessions",
                    {"agent_id": "phantom"}, headers=cookie), 404)


# ---------------------------------------------------------------------------
# GET session sub-resources: detail, files (empty at open), tools.
# ---------------------------------------------------------------------------
def test_get_session_detail(console, cookie):
    """Fetching the open session returns its public shape (id, status, workspace)."""
    sid = open_session(console, cookie, "claude-code")
    try:
        code, sess = req(console, "GET", f"{S1}/sessions/{sid}", headers=cookie)
        assert code == 200
        assert sess["session_id"] == sid
        assert sess["status"] == "open"
        assert sess["workspace"] == "/mnt/s3files"
    finally:
        close_session(console, cookie, sid)


def test_get_unknown_session_404(console, cookie):
    """Asking for a session id that was never opened is a 404."""
    expect_status(
        lambda: req(console, "GET", f"{S1}/sessions/sess_nope_000", headers=cookie), 404)


def test_session_files_start_empty(console, cookie):
    """The new workspace starts EMPTY; nothing is pre-seeded. The attendee creates
    every file (the first being cost_analyzer.py) themselves in the explorer."""
    sid = open_session(console, cookie, "claude-code")
    try:
        code, out = req(console, "GET", f"{S1}/sessions/{sid}/files", headers=cookie)
        assert code == 200
        assert out["workspace"] == "/mnt/s3files"
        assert out["tree"] == []
    finally:
        close_session(console, cookie, sid)


def test_session_files_skill_after_create(console, cookie):
    """Once the attendee creates cost_analyzer.py (New File → paste the source), it
    shows up as a non-empty file entry, not a directory."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        tree = file_tree(console, cookie, sid)
        skill = next(e for e in tree if e["path"] == "/mnt/s3files/sample/cost_analyzer.py")
        assert skill["type"] == "file"
        assert skill["size"] > 0
    finally:
        close_session(console, cookie, sid)


def test_session_tools_empty_before_convert(console, cookie):
    """A fresh session lists no MCP tools yet; the conversion is what populates them."""
    sid = open_session(console, cookie, "claude-code")
    try:
        code, out = req(console, "GET", f"{S1}/sessions/{sid}/tools", headers=cookie)
        assert code == 200
        assert out["tools"] == []
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# Smart capture: a deploy (deploy.py writes runtime_config.json) reconciles
# onto the shelf, even with a session/PTY open. No fake shim, no local:runtime.
# ---------------------------------------------------------------------------
def test_real_deploy_reconciles_onto_the_shelf(console, cookie):
    """The attendee builds + deploys the harness in the terminal (./setup.sh +
    python deploy.py, which writes the runtime_config.json). The console picks
    that up on its own; opencode flips to ready on GET /agents with its
    bedrock-agentcore ARN. Smart capture of the deploy, no button."""
    sid = open_session(console, cookie, "opencode")
    try:
        open_pty(console, cookie, sid)
        deploy_real(console, cookie, "opencode")
        _, agents = req(console, "GET", f"{S1}/agents", headers=cookie)
        opencode = next(a for a in agents["agents"] if a["agent_id"] == "opencode")
        assert opencode["status"] == "ready"
        assert opencode["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")
        assert "runtime/opencode" in opencode["runtime_arn"]
    finally:
        undeploy_real("opencode")
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# Workspace edits over the file endpoint (the editor + context menu).
# ---------------------------------------------------------------------------
def test_write_then_read_file(console, cookie):
    """Attendee creates a file in the explorer and reads it back verbatim."""
    sid = open_session(console, cookie, "claude-code")
    try:
        w = write_file(console, cookie, sid, "notes.md", "# my plan\n")
        assert w["path"] == "/mnt/s3files/notes.md"
        r = read_file(console, cookie, sid, "notes.md")
        assert r["content"] == "# my plan\n"
        assert r["binary"] is False
    finally:
        close_session(console, cookie, sid)


def test_rename_file(console, cookie):
    """Right-click Rename moves a file within the workspace jail."""
    sid = open_session(console, cookie, "claude-code")
    try:
        write_file(console, cookie, sid, "old.txt", "data")
        out = file_op(console, cookie, sid, "old.txt", "rename", to="new.txt")
        assert out.get("ok") is True
        paths = {e["path"] for e in out["tree"]}
        assert "/mnt/s3files/new.txt" in paths
        assert "/mnt/s3files/old.txt" not in paths
    finally:
        close_session(console, cookie, sid)


def test_delete_file(console, cookie):
    """Right-click Delete removes a file and returns the refreshed tree."""
    sid = open_session(console, cookie, "claude-code")
    try:
        write_file(console, cookie, sid, "scratch.txt", "tmp")
        out = file_op(console, cookie, sid, "scratch.txt", "delete")
        assert out.get("ok") is True
        paths = {e["path"] for e in out["tree"]}
        assert "/mnt/s3files/scratch.txt" not in paths
    finally:
        close_session(console, cookie, sid)


def test_file_jail_rejects_escape(console, cookie):
    """A path escaping /mnt/s3files is rejected; the workspace is jailed."""
    sid = open_session(console, cookie, "claude-code")
    try:
        out = write_file(console, cookie, sid, "../../etc/evil", "x")
        assert "error" in out
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# DELETE /sessions: close it, and subsequent ops are 409.
# ---------------------------------------------------------------------------
def test_close_session(console, cookie):
    """Attendee closes the session: DELETE returns status closed."""
    sid = open_session(console, cookie, "claude-code")
    code, out = req(console, "DELETE", f"{S1}/sessions/{sid}", headers=cookie)
    assert code == 200
    assert out["status"] == "closed"


def test_file_op_on_closed_session_409(console, cookie):
    """After close, file ops are 409; the workspace is gone."""
    sid = open_session(console, cookie, "claude-code")
    req(console, "DELETE", f"{S1}/sessions/{sid}", headers=cookie)
    expect_status(
        lambda: req(console, "POST", f"{S1}/sessions/{sid}/file",
                    {"path": "x.txt", "content": "y"}, headers=cookie), 409)


def test_convert_on_closed_session_409(console, cookie):
    """After close, convert-skill is 409 (the session no longer accepts work)."""
    sid = open_session(console, cookie, "claude-code")
    req(console, "DELETE", f"{S1}/sessions/{sid}", headers=cookie)
    expect_status(
        lambda: req(console, "POST", f"{S1}/sessions/{sid}/convert-skill",
                    {"tool": "estimate_ec2_monthly_cost"}, headers=cookie), 409)


def test_pty_on_closed_session_409(console, cookie):
    """After close, opening a PTY is 409; no shell into a torn-down microVM."""
    sid = open_session(console, cookie, "claude-code")
    req(console, "DELETE", f"{S1}/sessions/{sid}", headers=cookie)
    expect_status(
        lambda: req(console, "POST", f"{S1}/sessions/{sid}/pty",
                    {"open": True, "resize": {"cols": 100, "rows": 30}}, headers=cookie), 409)
