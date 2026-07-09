"""Stage 1: the file explorer + the by-hand module-to-MCP conversion.

Covers the Stage 1 workshop arc an attendee runs in the console: open a session on
the live `/mnt/s3files` workspace, drive the VS Code-like file explorer (tree, write a
freely-named file, read it back, rename, delete, nested paths, binary-ish content), the
jail guard that refuses any path escaping the workspace, the scaffold-harness step that
writes the agent's steering files, and the conversion payoff; `convert-skill` boots a
real MCP server and verifies it over the wire (m5.large x2 == 140.16), `verify` runs the
live checks, and `deploy-upload` packages the workspace into a code bundle whose manifest
carries the converted `mcp_server.py`.

Every test drives the shared real console server (conftest fixtures), asserts the real
`/api/dev` contract over HTTP, and cleans up its own session. No mocks, no ordering deps.
"""
from __future__ import annotations

from e2e.conftest import (
    req, expect_status, open_session, close_session,
    file_tree, write_file, read_file, file_op, seed_skill,
    EC2_FIXTURE_COST,
)


# ---------------------------------------------------------------------------
# The file explorer over the live /mnt/s3files session.
# ---------------------------------------------------------------------------
def test_file_tree_starts_empty_then_shows_created_skill(console, cookie):
    """Open the explorer: a fresh workspace is EMPTY; the module shows up only after
    the participant creates cost_analyzer.py in the editor (New File -> paste)."""
    sid = open_session(console, cookie, "claude-code")
    try:
        # the workspace starts empty; nothing is magically pre-seeded
        _, out = req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
        assert out["workspace"] == "/mnt/s3files"
        assert out["tree"] == []
        # the participant creates the input module themselves (the real file-write API)
        seed_skill(console, cookie, sid)
        _, out = req(console, "GET", f"/api/dev/sessions/{sid}/files", headers=cookie)
        paths = {n["path"] for n in out["tree"]}
        # the module sits at the workspace root (it is a plain Python
        # module, not an Agent Skill, so it is NOT under a skills/ dir)
        assert "/mnt/s3files/sample/cost_analyzer.py" in paths
        skill = next(n for n in out["tree"] if n["path"] == "/mnt/s3files/sample/cost_analyzer.py")
        assert skill["type"] == "file"
    finally:
        close_session(console, cookie, sid)


def test_file_tree_entries_have_path_type_size(console, cookie):
    """The explorer's tree rows carry the {path,type,size} shape it renders."""
    sid = open_session(console, cookie, "claude-code")
    try:
        # create the input module the participant's way, then inspect the tree rows
        seed_skill(console, cookie, sid)
        tree = file_tree(console, cookie, sid)
        assert tree, "tree must not be empty after the module is created"
        for node in tree:
            assert set(("path", "type", "size")) <= set(node)
            assert node["type"] in ("file", "dir")
            assert node["path"].startswith("/mnt/s3files")
        skill = next(n for n in tree if n["path"].endswith("cost_analyzer.py"))
        assert skill["type"] == "file" and skill["size"] > 0
    finally:
        close_session(console, cookie, sid)


def test_write_new_free_named_file_and_read_it_back(console, cookie):
    """Attendee creates a real, freely-named file in the editor and reads it back."""
    sid = open_session(console, cookie, "claude-code")
    try:
        path = "my-conversion-notes.md"
        body = "# my plan\nconvert estimate_ec2_monthly_cost first\n"
        w = write_file(console, cookie, sid, path, body)
        assert "error" not in w
        assert w["path"] == "/mnt/s3files/my-conversion-notes.md"
        assert w["bytes"] == len(body.encode("utf-8"))
        # the write returns the fresh tree so the explorer re-renders the new file
        assert any(n["path"] == w["path"] for n in w["tree"])
        r = read_file(console, cookie, sid, path)
        assert r["binary"] is False
        assert r["content"] == body
        assert r["language"] == "markdown"
    finally:
        close_session(console, cookie, sid)


def test_create_your_own_filename_is_not_hardcoded(console, cookie):
    """Two attendees pick different real filenames; the explorer honors each verbatim."""
    sid = open_session(console, cookie, "claude-code")
    try:
        for name, body in (("alpha_notes.txt", "alpha"),
                           ("zeta-pricing.md", "zeta")):
            w = write_file(console, cookie, sid, name, body)
            assert w["path"] == f"/mnt/s3files/{name}", w
            assert read_file(console, cookie, sid, name)["content"] == body
        # both freely-named files coexist in the tree (no overwrite of a fixed name)
        paths = {n["path"] for n in file_tree(console, cookie, sid)}
        assert "/mnt/s3files/alpha_notes.txt" in paths
        assert "/mnt/s3files/zeta-pricing.md" in paths
    finally:
        close_session(console, cookie, sid)


def test_write_then_read_nested_path_creates_dirs(console, cookie):
    """Writing to a nested path (src/server/main.py) creates the directories for free."""
    sid = open_session(console, cookie, "claude-code")
    try:
        path = "src/server/main.py"
        w = write_file(console, cookie, sid, path, "print('hi')\n")
        assert w["path"] == "/mnt/s3files/src/server/main.py"
        tree = w["tree"]
        # the intermediate directories appear as dir nodes
        assert any(n["path"] == "/mnt/s3files/src" and n["type"] == "dir" for n in tree)
        assert any(n["path"] == "/mnt/s3files/src/server" and n["type"] == "dir"
                   for n in tree)
        assert read_file(console, cookie, sid, path)["content"] == "print('hi')\n"
    finally:
        close_session(console, cookie, sid)


def test_rename_file_within_workspace(console, cookie):
    """Right-click Rename moves a file; the old name leaves the tree, the new one enters."""
    sid = open_session(console, cookie, "claude-code")
    try:
        write_file(console, cookie, sid, "draft.md", "draft body")
        mv = file_op(console, cookie, sid, "draft.md", "rename", to="final.md")
        assert mv.get("ok") is True
        assert mv["path"] == "/mnt/s3files/final.md"
        paths = {n["path"] for n in mv["tree"]}
        assert "/mnt/s3files/final.md" in paths
        assert "/mnt/s3files/draft.md" not in paths
        # the moved content survives the rename
        assert read_file(console, cookie, sid, "final.md")["content"] == "draft body"
    finally:
        close_session(console, cookie, sid)


def test_rename_into_nested_path(console, cookie):
    """Rename can also move a file into a new subdirectory (real mv semantics)."""
    sid = open_session(console, cookie, "claude-code")
    try:
        write_file(console, cookie, sid, "loose.txt", "x")
        mv = file_op(console, cookie, sid, "loose.txt", "rename",
                     to="archive/2026/loose.txt")
        assert mv.get("ok") is True
        assert mv["path"] == "/mnt/s3files/archive/2026/loose.txt"
        assert read_file(console, cookie, sid, "archive/2026/loose.txt")["content"] == "x"
    finally:
        close_session(console, cookie, sid)


def test_delete_file_removes_it_from_tree(console, cookie):
    """Right-click Delete removes the file and returns the fresh tree without it."""
    sid = open_session(console, cookie, "claude-code")
    try:
        write_file(console, cookie, sid, "scratch.txt", "temp")
        rm = file_op(console, cookie, sid, "scratch.txt", "delete")
        assert rm.get("ok") is True
        assert not any(n["path"] == "/mnt/s3files/scratch.txt" for n in rm["tree"])
        # reading the now-deleted file reports not found (no stale 200 with content)
        assert "error" in read_file(console, cookie, sid, "scratch.txt")
    finally:
        close_session(console, cookie, sid)


def test_write_then_read_binary_ish_content(console, cookie):
    """Bytes-heavy content (NULs, control chars, emoji) round-trips through the editor verbatim."""
    sid = open_session(console, cookie, "claude-code")
    try:
        # control bytes + a multibyte emoji; the kind of "binary-ish" blob an attendee
        # might paste into the editor; the file must round-trip byte-for-byte, not mangle it.
        # (no bare CR: the engine reads text-mode UTF-8, which universal-newlines would fold.)
        blob = "\x00\x01\x02\x7f\tTAB \U0001f680 rocket \U0001f9ee math"
        w = write_file(console, cookie, sid, "blob.dat", blob)
        assert "error" not in w, w
        assert w["bytes"] == len(blob.encode("utf-8"))
        r = read_file(console, cookie, sid, "blob.dat")
        assert r["binary"] is False  # valid UTF-8 stays decodable, not falsely flagged
        assert r["content"] == blob  # exact round-trip, control bytes intact
        assert r["path"] == "/mnt/s3files/blob.dat"
    finally:
        close_session(console, cookie, sid)


def test_read_missing_file_returns_error(console, cookie):
    """Reading a path that doesn't exist returns an error payload, not fake content."""
    sid = open_session(console, cookie, "claude-code")
    try:
        r = read_file(console, cookie, sid, "does/not/exist.md")
        assert "error" in r
        assert "content" not in r or not r.get("content")
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# The jail guard: nothing escapes /mnt/s3files.
# ---------------------------------------------------------------------------
def test_jail_rejects_relative_traversal_write(console, cookie):
    """A write to ../../etc/passwd is refused; the workspace jail isn't escaped."""
    sid = open_session(console, cookie, "claude-code")
    try:
        out = write_file(console, cookie, sid, "../../etc/passwd", "pwned")
        # the contract: an error payload, NOT a 200 that wrote outside the jail
        assert "error" in out, out
        assert "escapes" in out["error"] or "invalid" in out["error"]
        # nothing named passwd shows up anywhere in the workspace tree
        assert not any("passwd" in n["path"] for n in file_tree(console, cookie, sid))
    finally:
        close_session(console, cookie, sid)


def test_jail_rejects_absolute_path_read(console, cookie):
    """Reading an absolute /etc/passwd doesn't leak the host file; it's jailed away."""
    sid = open_session(console, cookie, "claude-code")
    try:
        r = read_file(console, cookie, sid, "/etc/passwd")
        # either rejected outright, or normalized inside the jail where it doesn't
        # exist; but NEVER the real host file's contents (no root:x: line)
        assert "root:x:" not in (r.get("content") or "")
        assert "error" in r
    finally:
        close_session(console, cookie, sid)


def test_jail_rejects_traversal_rename_destination(console, cookie):
    """Rename can't smuggle a file OUT of the jail via a ../ destination."""
    sid = open_session(console, cookie, "claude-code")
    try:
        write_file(console, cookie, sid, "secret.txt", "data")
        mv = file_op(console, cookie, sid, "secret.txt", "rename",
                     to="../../tmp/escaped.txt")
        assert "error" in mv, mv
        assert not mv.get("ok")
        # the source file stayed put inside the jail
        assert read_file(console, cookie, sid, "secret.txt")["content"] == "data"
    finally:
        close_session(console, cookie, sid)


def test_jail_rejects_delete_outside_workspace(console, cookie):
    """Delete with a traversal path is refused; can't remove a host file."""
    sid = open_session(console, cookie, "claude-code")
    try:
        rm = file_op(console, cookie, sid, "../../../etc/hosts", "delete")
        assert "error" in rm, rm
        assert not rm.get("ok")
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# Closed-session guards: a gone workspace never answers 200.
# ---------------------------------------------------------------------------
def test_file_op_on_closed_session_409(console, cookie):
    """A file op on a closed session is 409, never a 200 pretending the workspace lives."""
    sid = open_session(console, cookie, "claude-code")
    write_file(console, cookie, sid, "a.txt", "before close")
    code, _ = req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)
    assert code == 200
    # write, read, and an op-tagged call all reject post-close
    err = expect_status(lambda: req(console, "POST",
        f"/api/dev/sessions/{sid}/file", {"path": "b.txt", "content": "x"},
        headers=cookie), 409)
    assert err.get("status") == "closed"
    expect_status(lambda: req(console, "POST",
        f"/api/dev/sessions/{sid}/file", {"path": "a.txt"}, headers=cookie), 409)
    expect_status(lambda: req(console, "POST",
        f"/api/dev/sessions/{sid}/file", {"path": "a.txt", "op": "delete"},
        headers=cookie), 409)


def test_files_listing_on_unknown_session_404(console, cookie):
    """The explorer asking for a session that never existed gets 404, not an empty tree."""
    expect_status(lambda: req(console, "GET",
        "/api/dev/sessions/sess_nope_999/files", headers=cookie), 404)


# ---------------------------------------------------------------------------
# Scaffold the harness: the agent's steering files land in the tree.
# ---------------------------------------------------------------------------
def test_scaffold_harness_writes_claude_md_and_skill_md(console, cookie):
    """'Set up harness' for claude-code writes CLAUDE.md + a SKILL.md into the workspace."""
    sid = open_session(console, cookie, "claude-code")
    try:
        _, out = req(console, "POST", f"/api/dev/sessions/{sid}/scaffold-harness",
                     {"agent_id": "claude-code"}, headers=cookie)
        assert out["agent_id"] == "claude-code"
        written = out["written"]
        assert "/mnt/s3files/CLAUDE.md" in written
        assert "/mnt/s3files/skills/configure-backend/SKILL.md" in written
        # the returned tree reflects the new harness files
        tree_paths = {n["path"] for n in out["tree"]}
        assert "/mnt/s3files/CLAUDE.md" in tree_paths
        assert "/mnt/s3files/skills/configure-backend/SKILL.md" in tree_paths
    finally:
        close_session(console, cookie, sid)


def test_scaffold_harness_files_are_real_and_readable(console, cookie):
    """The scaffolded CLAUDE.md is a real file: read it back and see the backend role text."""
    sid = open_session(console, cookie, "claude-code")
    try:
        req(console, "POST", f"/api/dev/sessions/{sid}/scaffold-harness",
            {"agent_id": "claude-code"}, headers=cookie)
        r = read_file(console, cookie, sid, "CLAUDE.md")
        assert r["binary"] is False
        assert "BACKEND" in r["content"]
        skill = read_file(console, cookie, sid, "skills/configure-backend/SKILL.md")
        assert "configure-backend" in skill["content"]
    finally:
        close_session(console, cookie, sid)


def test_scaffold_harness_claude_code_validator_writes_claude_md(console, cookie):
    """Scaffolding for claude-code-validator writes CLAUDE.md (validator role), not .kiro steering."""
    sid = open_session(console, cookie, "claude-code-validator")
    try:
        _, out = req(console, "POST", f"/api/dev/sessions/{sid}/scaffold-harness",
                     {"agent_id": "claude-code-validator"}, headers=cookie)
        assert out["agent_id"] == "claude-code-validator"
        assert "/mnt/s3files/CLAUDE.md" in out["written"]
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# The conversion payoff: write -> boot -> verify a real MCP server over the wire.
# ---------------------------------------------------------------------------
def test_convert_skill_returns_verified_server(console, cookie):
    """convert-skill on estimate_ec2_monthly_cost boots a real server and verifies it."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)  # the input module the server imports
        _, conv = req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                      {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        assert conv["verified"] is True
        assert conv["tool"] == "estimate_ec2_monthly_cost"
        assert conv["server_file"] == "/mnt/s3files/mcp_server.py"
        assert conv["session_id"] == sid
        assert conv["endpoint"].startswith("http://127.0.0.1:")
    finally:
        close_session(console, cookie, sid)


def test_convert_skill_tools_list_has_the_tool(console, cookie):
    """The booted MCP server's tools/list (over the wire) advertises the converted tool."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        _, conv = req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                      {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        names = [t.get("name") for t in conv["tools_list"]]
        assert "estimate_ec2_monthly_cost" in names
        # and the converted MCP server's own /tools mirror reflects the same tool
        _, tools = req(console, "GET", f"/api/dev/sessions/{sid}/tools",
                       headers=cookie)
        assert any(t.get("name") == "estimate_ec2_monthly_cost" for t in tools["tools"])
    finally:
        close_session(console, cookie, sid)


def test_convert_skill_sample_call_returns_fixture_cost(console, cookie):
    """The live server's own sample call (m5.large x2) returns the 140.16 fixture."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        _, conv = req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                      {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        sample = conv["sample_call"]
        assert sample["args"] == {"instance_type": "m5.large", "count": 2}
        assert sample["result"]["monthly_cost"] == EC2_FIXTURE_COST
    finally:
        close_session(console, cookie, sid)


def test_convert_skill_writes_server_file_into_tree(console, cookie):
    """After convert, mcp_server.py is a real file in the workspace tree and readable."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
            {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        paths = {n["path"] for n in file_tree(console, cookie, sid)}
        assert "/mnt/s3files/mcp_server.py" in paths
        server_src = read_file(console, cookie, sid, "mcp_server.py")
        assert server_src["binary"] is False
        assert "tools/list" in server_src["content"]
    finally:
        close_session(console, cookie, sid)


def test_convert_skill_default_tool_when_unspecified(console, cookie):
    """convert-skill with no tool defaults to estimate_ec2_monthly_cost (the taught one)."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        _, conv = req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                      {}, headers=cookie)
        assert conv["tool"] == "estimate_ec2_monthly_cost"
        assert conv["verified"] is True
    finally:
        close_session(console, cookie, sid)


def test_convert_skill_on_closed_session_409(console, cookie):
    """Converting on a closed session is 409; the booted server can't outlive it."""
    sid = open_session(console, cookie, "claude-code")
    req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)
    expect_status(lambda: req(console, "POST",
        f"/api/dev/sessions/{sid}/convert-skill",
        {"tool": "estimate_ec2_monthly_cost"}, headers=cookie), 409)


# ---------------------------------------------------------------------------
# Verify: run the converted server's live checks.
# ---------------------------------------------------------------------------
def test_verify_after_convert_runs_passed_checks(console, cookie):
    """verify exercises the live server: ran+passed true, four named checks all green."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
            {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        _, ver = req(console, "POST", f"/api/dev/sessions/{sid}/verify", {},
                     headers=cookie)
        assert ver["ran"] is True
        assert ver["passed"] is True
        assert ver["tool"] == "estimate_ec2_monthly_cost"
        assert ver["endpoint"].startswith("http://127.0.0.1:")
        assert isinstance(ver["latency_ms"], int) and ver["latency_ms"] >= 0
        checks = {c["check"]: c["passed"] for c in ver["checks"]}
        assert checks == {"server_live": True, "tools_list": True,
                          "tool_call": True, "input_validation": True}, checks
    finally:
        close_session(console, cookie, sid)


def test_verify_sample_is_the_fixture_cost(console, cookie):
    """verify's sample tools/call returns the same 140.16 fixture the gate grades on."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
            {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        _, ver = req(console, "POST", f"/api/dev/sessions/{sid}/verify", {},
                     headers=cookie)
        assert ver["sample"]["monthly_cost"] == EC2_FIXTURE_COST
    finally:
        close_session(console, cookie, sid)


def test_verify_without_server_reports_not_ran(console, cookie):
    """verify before any convert (no mcp_server.py) honestly reports ran=False, not a fake pass."""
    sid = open_session(console, cookie, "claude-code")
    try:
        _, ver = req(console, "POST", f"/api/dev/sessions/{sid}/verify", {},
                     headers=cookie)
        assert ver["ran"] is False
        assert ver["checks"] == []
        assert "error" in ver
    finally:
        close_session(console, cookie, sid)


def test_verify_boots_agent_written_server(console, cookie):
    """If the attendee hand-writes mcp_server.py (no engine convert), verify boots THAT file."""
    # First, capture the real single-tool server source the workshop teaches: a throwaway
    # session converts once so we can read the genuine mcp_server.py the engine wrote.
    src_sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, src_sid)
        req(console, "POST", f"/api/dev/sessions/{src_sid}/convert-skill",
            {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        server_src = read_file(console, cookie, src_sid, "mcp_server.py")["content"]
        assert "tools/list" in server_src
    finally:
        close_session(console, cookie, src_sid)

    # Now a FRESH session that never ran convert: the attendee creates the input module
    # and hand-writes the server, then verify must boot that hand-written file (no
    # engine-staged _server) and pass; the booted server imports cost_analyzer.py.
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        w = write_file(console, cookie, sid, "mcp_server.py", server_src)
        assert "error" not in w, w
        _, ver = req(console, "POST", f"/api/dev/sessions/{sid}/verify", {},
                     headers=cookie)
        assert ver["ran"] is True
        assert ver["passed"] is True
        assert ver["sample"]["monthly_cost"] == EC2_FIXTURE_COST
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# Deploy-upload: package the workspace into a code bundle.
# ---------------------------------------------------------------------------
def test_deploy_upload_packages_bundle_with_bytes(console, cookie):
    """deploy-upload zips the workspace into a real code bundle with bytes>0."""
    sid = open_session(console, cookie, "claude-code")
    try:
        # the participant has created the input module; it then rides along in the bundle
        seed_skill(console, cookie, sid)
        _, up = req(console, "POST", f"/api/dev/sessions/{sid}/deploy-upload", {},
                    headers=cookie)
        assert up["mode"] == "code-upload"
        assert up["bundle_bytes"] > 0
        assert up["file_count"] >= 1
        assert up["bundle_file"].endswith(".zip")
        # Packaging is a real artifact, but it does NOT mint a Runtime. runtime_arn is
        # the GENUINE ARN if a real deploy has landed for this harness, else null;
        # NEVER a fabricated local:runtime placeholder. (In the shared suite another
        # ordered test may have deployed claude-code for real, so accept either.)
        assert up["runtime_arn"] is None or up["runtime_arn"].startswith(
            "arn:aws:bedrock-agentcore:"), up["runtime_arn"]
        assert isinstance(up["manifest"], list) and up["manifest"]
        # the module the participant created rides along in the bundle
        assert any(m.endswith("cost_analyzer.py") for m in up["manifest"])
    finally:
        close_session(console, cookie, sid)


def test_deploy_upload_manifest_has_mcp_server_after_convert(console, cookie):
    """After a convert, the deploy bundle's manifest carries mcp_server.py as the entrypoint."""
    sid = open_session(console, cookie, "claude-code")
    try:
        seed_skill(console, cookie, sid)
        req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
            {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        _, up = req(console, "POST", f"/api/dev/sessions/{sid}/deploy-upload", {},
                    headers=cookie)
        assert "mcp_server.py" in up["manifest"]
        assert up["entrypoint"] == "mcp_server.py"
        assert up["bundle_bytes"] > 0
    finally:
        close_session(console, cookie, sid)


def test_deploy_upload_records_scaffolded_harness_agent(console, cookie):
    """A bundle deployed after scaffolding reports which harness agent it carries."""
    sid = open_session(console, cookie, "claude-code")
    try:
        req(console, "POST", f"/api/dev/sessions/{sid}/scaffold-harness",
            {"agent_id": "claude-code"}, headers=cookie)
        _, up = req(console, "POST", f"/api/dev/sessions/{sid}/deploy-upload", {},
                    headers=cookie)
        assert up["harness_agent"] == "claude-code"
        # the scaffolded CLAUDE.md is part of the uploaded bundle
        assert "CLAUDE.md" in up["manifest"]
    finally:
        close_session(console, cookie, sid)


def test_deploy_upload_on_closed_session_409(console, cookie):
    """Packaging a closed session's gone workspace is 409, never a phantom bundle."""
    sid = open_session(console, cookie, "claude-code")
    req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)
    expect_status(lambda: req(console, "POST",
        f"/api/dev/sessions/{sid}/deploy-upload", {}, headers=cookie), 409)


# ---------------------------------------------------------------------------
# End-to-end Stage 1 hand flow: write -> scaffold -> convert -> verify -> deploy.
# ---------------------------------------------------------------------------
def test_full_by_hand_conversion_flow(console, cookie):
    """The whole Stage 1 by-hand arc: edit, scaffold, convert, verify green, deploy bundle."""
    sid = open_session(console, cookie, "claude-code")
    try:
        # 0) the workspace starts empty; the attendee creates the input module first
        seed_skill(console, cookie, sid)
        # 1) attendee jots a plan with their own filename
        write_file(console, cookie, sid, "plan.md", "wrap the module as MCP")
        # 2) set up the harness
        _, sc = req(console, "POST", f"/api/dev/sessions/{sid}/scaffold-harness",
                    {"agent_id": "claude-code"}, headers=cookie)
        assert "/mnt/s3files/CLAUDE.md" in sc["written"]
        # 3) convert the module -> verified server with the fixture sample
        _, conv = req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                      {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
        assert conv["verified"] is True
        assert conv["sample_call"]["result"]["monthly_cost"] == EC2_FIXTURE_COST
        # 4) verify the live server
        _, ver = req(console, "POST", f"/api/dev/sessions/{sid}/verify", {},
                     headers=cookie)
        assert ver["passed"] is True
        # 5) package the deploy bundle; it carries the server we just built
        _, up = req(console, "POST", f"/api/dev/sessions/{sid}/deploy-upload", {},
                    headers=cookie)
        assert "mcp_server.py" in up["manifest"]
        assert up["bundle_bytes"] > 0
    finally:
        close_session(console, cookie, sid)
