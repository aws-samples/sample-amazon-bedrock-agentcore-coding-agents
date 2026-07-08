"""Stage 1 interactive API: the product surface, unit-tested without a browser.

Every assertion here exercises the same `dispatch()` the console UI calls, so what the
file explorer, the harness "Create files" button, the code-upload deploy, and the
"Run & verify" button do is proven to be work on disk and over the wire:

  * a session mounts a workspace dir, and the file tree reflects it;
  * the editor reads and writes files, and the jail rejects path escapes;
  * "Set up the harness" writes the agent's steering files (CLAUDE.md/SKILL.md,
    AGENTS.md+toml, or .kiro/steering) into the workspace;
  * "Deploy: direct code upload" produces a zip bundle of the workspace;
  * "Run & verify" boots the converted MCP server and checks it over the wire.

Run:  pytest interactive-api/test_interactive_api.py -v
"""

from __future__ import annotations

import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import interactive_api as ia  # noqa: E402


def _open_session():
    ia.dispatch("POST", "/api/agents/deploy", {"agent_id": "claude-code"})
    code, sess = ia.dispatch("POST", "/api/sessions", {"agent_id": "claude-code"})
    assert code == 201 and sess["status"] == "open"
    return sess["session_id"]


# The workspace starts EMPTY: the participant creates every file in the editor,
# beginning with the input module cost_analyzer.py. Tests that need the module present
# create it the SAME WAY a participant does (New File → paste the real source → save),
# by POSTing the canonical cost_analyzer.py source to the file-write API. This mirrors
# e2e/conftest.py:seed_skill, which drives the same write over the console.
_SKILL_SRC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "usecase-sample-to-mcp", "cost_analyzer.py")


def _seed_skill(sid, name="cost_analyzer.py"):
    """Create cost_analyzer.py in the session workspace from the real source, the way a
    participant does it (New File → paste → save), via the editor's file-write dispatch.
    Lands under sample/ to mirror the content page (mkdir -p sample). Returns the path."""
    with open(_SKILL_SRC_PATH, encoding="utf-8") as f:
        code, out = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                                {"path": f"/mnt/s3files/sample/{name}", "content": f.read()})
    assert code == 200 and "error" not in out, out
    return f"/mnt/s3files/sample/{name}"


def test_file_tree_reflects_the_real_workspace():
    sid = _open_session()
    # the workspace starts EMPTY: nothing is pre-seeded / "magically there"
    code, empty = ia.dispatch("GET", f"/api/sessions/{sid}/files", None)
    assert code == 200 and empty["workspace"] == "/mnt/s3files"
    assert empty["tree"] == [], f"a fresh workspace must be empty, got {empty['tree']!r}"
    # the participant creates the input module in the editor (New File → paste → save)
    _seed_skill(sid)
    code, out = ia.dispatch("GET", f"/api/sessions/{sid}/files", None)
    assert code == 200 and out["workspace"] == "/mnt/s3files"
    paths = {e["path"] for e in out["tree"]}
    # the created module sits under sample/, NOT under a skills/ dir; cost_analyzer
    # is a plain Python module, not an Agent Skill (which would be skills/<name>/SKILL.md)
    assert "/mnt/s3files/sample/cost_analyzer.py" in paths
    # the seeded module file reports a real, non-zero size
    skill = next(e for e in out["tree"] if e["path"].endswith("cost_analyzer.py"))
    assert skill["type"] == "file" and skill["size"] > 0


def test_editor_reads_and_writes_real_files():
    sid = _open_session()
    # the participant creates the module in the editor, then reads it back
    _seed_skill(sid)
    _, f = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                       {"path": "/mnt/s3files/sample/cost_analyzer.py"})
    assert f["language"] == "python" and "def estimate_ec2_monthly_cost" in f["content"]
    # write a new file via the editor
    _, w = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                       {"path": "/mnt/s3files/notes.md", "content": "# hi\n"})
    assert w["bytes"] == 5
    assert any(e["path"] == "/mnt/s3files/notes.md" for e in w["tree"])
    # round-trip: read it back
    _, again = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                           {"path": "/mnt/s3files/notes.md"})
    assert again["content"] == "# hi\n"


def test_content_search_finds_matching_lines_grouped_by_file():
    """The explorer's Cmd+F: op=search greps every text file in the jail and
    returns matching lines grouped by file with 1-based line numbers."""
    sid = _open_session()
    _seed_skill(sid)
    ia.dispatch("POST", f"/api/sessions/{sid}/file",
                {"path": "/mnt/s3files/notes.md", "content": "alpha\nestimate beta\ngamma\n"})
    _, res = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                         {"op": "search", "query": "estimate"})
    # cost_analyzer.py defines estimate_* functions, and notes.md has "estimate beta"
    paths = {r["path"] for r in res["results"]}
    assert "/mnt/s3files/notes.md" in paths
    notes = next(r for r in res["results"] if r["path"].endswith("notes.md"))
    assert notes["hits"][0]["line"] == 2 and "estimate" in notes["hits"][0]["text"]
    # case-insensitive, and an empty query returns nothing (not an error)
    _, low = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                         {"op": "search", "query": "ESTIMATE"})
    assert any(r["path"].endswith("notes.md") for r in low["results"])
    _, blank = ia.dispatch("POST", f"/api/sessions/{sid}/file", {"op": "search", "query": "   "})
    assert blank["results"] == []


def test_editor_jail_rejects_path_escape():
    sid = _open_session()
    _, esc = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                         {"path": "../../../../etc/passwd", "content": "x"})
    assert "error" in esc
    # and a read escape is refused too (resolves outside the jail -> not found)
    _, r = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                       {"path": "../../../../etc/hosts"})
    assert "error" in r


def test_file_delete_removes_and_returns_fresh_tree():
    sid = _open_session()
    # create a file, then delete it via op="delete"
    ia.dispatch("POST", f"/api/sessions/{sid}/file",
                {"path": "/mnt/s3files/scratch.txt", "content": "bye\n"})
    code, d = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                          {"path": "/mnt/s3files/scratch.txt", "op": "delete"})
    assert code == 200 and d["ok"] is True
    # the fresh tree no longer lists it
    assert not any(e["path"] == "/mnt/s3files/scratch.txt" for e in d["tree"])
    # and a follow-up read confirms it is gone
    _, gone = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                          {"path": "/mnt/s3files/scratch.txt"})
    assert "error" in gone


def test_file_rename_moves_within_jail():
    sid = _open_session()
    ia.dispatch("POST", f"/api/sessions/{sid}/file",
                {"path": "/mnt/s3files/old.md", "content": "# move me\n"})
    code, r = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                          {"path": "/mnt/s3files/old.md", "op": "rename",
                           "to": "/mnt/s3files/new.md"})
    assert code == 200 and r["ok"] is True
    paths = {e["path"] for e in r["tree"]}
    assert "/mnt/s3files/new.md" in paths
    assert "/mnt/s3files/old.md" not in paths
    # content survives the move
    _, moved = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                           {"path": "/mnt/s3files/new.md"})
    assert moved["content"] == "# move me\n"


def test_file_delete_and_rename_reject_jail_escape():
    sid = _open_session()
    # delete cannot reach outside the workspace
    _, de = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                        {"path": "../../../../etc/passwd", "op": "delete"})
    assert "error" in de and "tree" not in de
    # rename cannot escape via EITHER source or destination
    ia.dispatch("POST", f"/api/sessions/{sid}/file",
                {"path": "/mnt/s3files/keep.txt", "content": "stay\n"})
    _, re_out = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                            {"path": "/mnt/s3files/keep.txt", "op": "rename",
                             "to": "../../../../tmp/escaped.txt"})
    assert "error" in re_out
    # the source file is untouched after a rejected rename
    _, still = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                           {"path": "/mnt/s3files/keep.txt"})
    assert still["content"] == "stay\n"


def test_file_delete_missing_errors_without_crashing():
    sid = _open_session()
    code, miss = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                             {"path": "/mnt/s3files/never_existed.py", "op": "delete"})
    assert code == 200 and "error" in miss and "tree" not in miss


def test_scaffold_harness_writes_real_steering_files():
    # claude-code -> CLAUDE.md + a SKILL.md, with the harness build spec inside
    sid = _open_session()
    _, h = ia.dispatch("POST", f"/api/sessions/{sid}/scaffold-harness",
                       {"agent_id": "claude-code"})
    assert "/mnt/s3files/CLAUDE.md" in h["written"]
    assert any(p.endswith("SKILL.md") for p in h["written"])
    _, claude = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                            {"path": "/mnt/s3files/CLAUDE.md"})
    assert "harness:build" in claude["content"]

    # opencode -> project-root AGENTS.md (with the UI spec) + .config/opencode/opencode.json
    sid2 = _open_session()
    _, hc = ia.dispatch("POST", f"/api/sessions/{sid2}/scaffold-harness",
                        {"agent_id": "opencode"})
    assert "/mnt/s3files/AGENTS.md" in hc["written"]
    assert "/mnt/s3files/.config/opencode/opencode.json" in hc["written"]
    _, agents = ia.dispatch("POST", f"/api/sessions/{sid2}/file",
                            {"path": "/mnt/s3files/AGENTS.md"})
    assert "harness:ui" in agents["content"]

    # kiro -> .kiro/steering/*.md with inclusion: always
    sid3 = _open_session()
    _, hk = ia.dispatch("POST", f"/api/sessions/{sid3}/scaffold-harness",
                        {"agent_id": "kiro"})
    assert any("/.kiro/steering/" in p for p in hk["written"])


def test_deploy_upload_produces_a_real_code_bundle():
    sid = _open_session()
    _seed_skill(sid)  # the input module the participant created goes up in the bundle
    ia.dispatch("POST", f"/api/sessions/{sid}/scaffold-harness", {"agent_id": "claude-code"})
    ia.dispatch("POST", f"/api/sessions/{sid}/convert-skill",
                {"tool": "estimate_ec2_monthly_cost"})
    _, d = ia.dispatch("POST", f"/api/sessions/{sid}/deploy-upload", None)
    assert d["mode"] == "code-upload"
    # Packaging produces a zip artifact, but it does NOT mint a Runtime: the
    # runtime_arn is null until a CreateAgentRuntime (deploy.py) lands.
    # Never a fabricated local:runtime placeholder.
    assert d["runtime_arn"] is None, d["runtime_arn"]
    assert d["file_count"] >= 3 and d["bundle_bytes"] > 0
    # the manifest names the real artifacts that went up (cost_analyzer under sample/)
    assert "CLAUDE.md" in d["manifest"]
    assert "sample/cost_analyzer.py" in d["manifest"]
    assert d["entrypoint"] == "mcp_server.py"
    # the bundle is a zip on disk that opens and contains the manifest
    sess = ia._SESSIONS[sid]
    bundle = sess["_deploy"]["bundle"]
    assert os.path.isfile(bundle)
    with zipfile.ZipFile(bundle) as z:
        names = set(z.namelist())
    assert "CLAUDE.md" in names and "sample/cost_analyzer.py" in names


def test_verify_runs_the_live_server_over_the_wire():
    sid = _open_session()
    # before converting, verify has nothing to run
    _, v0 = ia.dispatch("POST", f"/api/sessions/{sid}/verify", None)
    assert v0["ran"] is False
    # the participant creates the input module, then convert + verify exercise the live
    # server (the converted mcp_server.py imports cost_analyzer at the workspace root)
    _seed_skill(sid)
    ia.dispatch("POST", f"/api/sessions/{sid}/convert-skill",
                {"tool": "estimate_ec2_monthly_cost"})
    _, v = ia.dispatch("POST", f"/api/sessions/{sid}/verify", None)
    assert v["ran"] is True and v["passed"] is True
    by = {c["check"]: c for c in v["checks"]}
    assert by["server_live"]["passed"]
    assert by["tools_list"]["passed"]
    assert by["tool_call"]["passed"]
    assert by["input_validation"]["passed"]
    # the tool_call check really priced m5.large x2
    assert "140.16" in by["tool_call"]["detail"]
    # clean up the booted server
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


# --- preview-server reaping: a converted mcp_server.py must NEVER orphan --------
# The Stage-1 "convert + verify" path boots the workspace mcp_server.py as a real
# subprocess. An MCP server never exits on its own, so a half-reaped one survives
# forever; left unbounded they piled into the thousands and wedged the console.
# These prove every teardown path actually kills the real child.
def test_stop_server_kills_the_real_preview_process():
    sid = _open_session()
    _seed_skill(sid)
    ia.dispatch("POST", f"/api/sessions/{sid}/convert-skill",
                {"tool": "estimate_ec2_monthly_cost"})
    sess = ia._SESSIONS[sid]
    proc = sess["_server"]["proc"]
    assert proc.poll() is None  # the preview server is live
    ia._stop_server(sess)
    assert proc.poll() is not None       # the real child is dead
    assert sess["_server"] is None       # and forgotten
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_reconvert_does_not_leak_the_previous_server():
    sid = _open_session()
    _seed_skill(sid)
    ia.dispatch("POST", f"/api/sessions/{sid}/convert-skill",
                {"tool": "estimate_ec2_monthly_cost"})
    first = ia._SESSIONS[sid]["_server"]["proc"]
    # A second conversion in the same session stops the first server before booting.
    ia.dispatch("POST", f"/api/sessions/{sid}/convert-skill",
                {"tool": "estimate_ec2_monthly_cost"})
    second = ia._SESSIONS[sid]["_server"]["proc"]
    assert first is not second
    assert first.poll() is not None      # the prior server was reaped, not leaked
    assert second.poll() is None         # the new one is live
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_atexit_reaper_kills_every_session_preview_server():
    """The host process (the console) imports interactive_api in-process, so its
    sessions' preview servers must be reaped when it exits. The atexit hook walks
    _SESSIONS and stops each one; here we call it directly and assert the real
    child dies (a console --reload / restart relies on exactly this)."""
    sid = _open_session()
    _seed_skill(sid)
    ia.dispatch("POST", f"/api/sessions/{sid}/convert-skill",
                {"tool": "estimate_ec2_monthly_cost"})
    proc = ia._SESSIONS[sid]["_server"]["proc"]
    assert proc.poll() is None
    ia._reap_all_session_servers()       # what atexit fires on process exit
    assert proc.poll() is not None       # the real preview server is reaped
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_closed_session_rejects_input_and_pty_io():
    """After DELETE closes a session, the dispatch guards reject further work on it:
    line `input` and live `pty` I/O both return 409 (session not open), never a
    200 that pretends the torn-down session is still driving a shell."""
    sid = _open_session()
    code, closed = ia.dispatch("DELETE", f"/api/sessions/{sid}", None)
    assert code == 200 and closed["status"] == "closed"

    # line-based input is rejected
    code_in, out_in = ia.dispatch("POST", f"/api/sessions/{sid}/input", {"input": "ls"})
    assert code_in == 409 and "error" in out_in

    # live PTY I/O is rejected (the _pty_io guard fires before any os.write)
    code_pty, out_pty = ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                                    {"input": "ls\n", "offset": 0})
    assert code_pty == 409 and "error" in out_pty


def test_closed_session_rejects_file_ops():
    """A file write to a CLOSED session must be rejected, not silently applied to a
    torn-down workspace. The `action == 'file'` dispatch branch guards on session
    status exactly like input/pty/convert/verify, returning 409."""
    sid = _open_session()
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)
    code, w = ia.dispatch("POST", f"/api/sessions/{sid}/file",
                          {"path": "/mnt/s3files/after-close.txt", "content": "nope"})
    assert code == 409 and "error" in w, f"closed-session write was NOT rejected: {code} {w}"


# --------------------------------------------------------------------------- PTY winsize
def _report_cols(sid, offset):
    """Print the live PTY width with a unique marker, then return the parsed cols.

    bash echoes the command line first (with the literal `$(tput cols)`) and only
    later prints the expanded output `WCOLS=<n>=`. We therefore poll until the
    expanded `WCOLS=<digits>=` form appears (not the command echo), so the read
    reflects the shell's column count, not the unexpanded keystrokes."""
    import re
    import time
    import uuid
    tag = "C" + uuid.uuid4().hex[:8]
    # settle the shell so the banner's `clear` has finished repainting first
    time.sleep(0.3)
    _, w = ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                       {"input": f"echo {tag}_WCOLS=$(tput cols)=END\n", "offset": offset})
    text = w["output"]
    offset = w["offset"]
    expanded = re.compile(rf"{tag}_WCOLS=(\d+)=END")   # only the EXPANDED output matches
    m = expanded.search(text)
    for _ in range(200):
        if m:
            break
        time.sleep(0.05)
        _, out = ia.dispatch("POST", f"/api/sessions/{sid}/pty", {"offset": offset})
        text += out["output"]
        offset = out["offset"]
        m = expanded.search(text)
    assert m is not None, f"no expanded WCOLS marker in PTY output: {text!r}"
    return int(m.group(1)), offset


def test_pty_opens_at_the_client_measured_size():
    """The open call carries xterm's MEASURED cols/rows ({"open": true, "resize":
    {...}}), and the PTY starts at exactly that size: fit first, then open
    sized, so a TUI lays out against the real pane and never wraps its borders
    mid-line. Without a measurement the PTY stays at a safe 80."""
    sid = _open_session()
    _, opened = ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                            {"open": True, "resize": {"rows": 31, "cols": 117}})
    assert opened["pty"] is True
    cols, _ = _report_cols(sid, 0)
    assert cols == 117, f"PTY should open at the measured 117 cols, got {cols}"
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)

    sid2 = _open_session()
    _, opened2 = ia.dispatch("POST", f"/api/sessions/{sid2}/pty", {"open": True})
    assert opened2["pty"] is True
    cols2, _ = _report_cols(sid2, 0)
    assert cols2 == 80, f"unmeasured PTY should stay at the safe 80, got {cols2}"
    ia.dispatch("DELETE", f"/api/sessions/{sid2}", None)


def test_session_reports_pty_alive_for_reattach():
    """GET /api/sessions/{id} reports pty_alive, the flag the client checks before
    re-attaching after a reload. True while the shell runs; after the session is
    closed it is status=closed with pty_alive False, the exact condition the
    client's reattach() rejects (so it opens a fresh session instead)."""
    sid = _open_session()
    ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                {"open": True, "resize": {"rows": 24, "cols": 80}})
    code, pub = ia.dispatch("GET", f"/api/sessions/{sid}", None)
    assert code == 200 and pub["pty_alive"] is True and pub["status"] == "open"
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)
    code2, pub2 = ia.dispatch("GET", f"/api/sessions/{sid}", None)
    # A closed session is not reattachable: status != open AND the PTY is dead.
    assert code2 == 200 and pub2["status"] == "closed" and pub2["pty_alive"] is False


def test_pty_stream_replays_the_retained_scrollback_from_offset_zero():
    """Re-attaching from offset 0 replays the server's retained PTY buffer, so a
    reloaded terminal shows its HISTORY instead of a blank shell. This is the
    exact mechanism the client's reattach() relies on: bind the stream at offset
    0 WITHOUT re-opening the PTY (which would respawn and wipe the buffer)."""
    import time
    sid = _open_session()
    ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                {"open": True, "resize": {"rows": 24, "cols": 80}})
    ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                {"input": "echo REATTACH_HISTORY_MARKER\n", "offset": 0})
    time.sleep(1.0)
    frames: list[bytes] = []
    for f in ia.pty_stream(sid, offset=0, should_stop=lambda: len(frames) > 3):
        frames.append(f)
        if len(frames) > 3:
            break
    blob = b"".join(frames).decode("utf-8", "replace")
    assert "REATTACH_HISTORY_MARKER" in blob, "offset-0 replay lost the scrollback"
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_pty_resize_changes_winsize():
    """A resize POST {"resize": {rows, cols}} honors TIOCSWINSZ on the LIVE PTY: the
    shell sees the new geometry. Open at a measured 132 cols, confirm the shell
    reports 132, then resize to 80 cols and assert it now reports 80: proof the
    ioctl reached the kernel and the running bash, not just the wire."""
    sid = _open_session()
    _, opened = ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                            {"open": True, "resize": {"rows": 26, "cols": 132}})
    assert opened["pty"] is True

    # before: the measured open size
    before, offset = _report_cols(sid, 0)
    assert before == 132, f"expected 132 cols before resize, got {before}"

    # resize the live PTY to 80 cols (nested {"resize": {...}} per the _pty_io contract)
    _, resized = ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                             {"resize": {"rows": 24, "cols": 80}, "offset": offset})
    assert "error" not in resized and resized["alive"] is True
    offset = resized["offset"]

    # after: the running shell reports the NEW width (TIOCSWINSZ was honored)
    after, _ = _report_cols(sid, offset)
    assert after == 80, f"resize to 80 cols was not honored, got {after}"
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_pty_env_reaches_aws_credentials_through_the_home_jail():
    """The HOME jail hides ~/.aws from the AWS SDK's default chain: on a laptop
    the chain then falls through to IMDS and the agent CLI sits in "Retrying"
    forever (the exact field failure). The PTY env must hand the SDK the real
    home's credential files explicitly. Asserted inside the live jailed bash:
    HOME is the workspace, yet AWS_SHARED_CREDENTIALS_FILE points at the real
    ~/.aws/credentials whenever that file exists on the box."""
    real_creds = os.path.join(os.path.expanduser("~"), ".aws", "credentials")
    if not os.path.isfile(real_creds):
        pytest.skip("no ~/.aws/credentials on this box (Runtime uses the instance role)")
    sid = _open_session()
    _, opened = ia.dispatch("POST", f"/api/sessions/{sid}/pty", {"open": True})
    assert opened["pty"] is True
    import re
    import time as _t
    ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                {"input": 'echo "JAILHOME=$HOME CREDS=$AWS_SHARED_CREDENTIALS_FILE."\n',
                 "offset": 0})
    text = ""
    pat = re.compile(r"JAILHOME=(\S+) CREDS=(\S*)\.")
    deadline = _t.monotonic() + 10
    m = None
    while _t.monotonic() < deadline:
        _t.sleep(0.1)
        _, out = ia.dispatch("POST", f"/api/sessions/{sid}/pty", {"offset": 0})
        text = out["output"]
        # skip past the echoed command; match only expanded output
        for cand in pat.finditer(text):
            if "$HOME" not in cand.group(0):
                m = cand
        if m:
            break
    assert m, f"no expanded JAILHOME marker in PTY output: {text!r}"
    home, creds = m.group(1), m.group(2)
    sess = ia._SESSIONS[sid]
    assert home == sess["_root"], "HOME must be the session jail"
    assert creds == real_creds, (
        "the jailed shell must still see the real credential file via "
        f"AWS_SHARED_CREDENTIALS_FILE, got {creds!r}")
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_dev_session_roots_at_the_empty_s3files_mount(tmp_path, monkeypatch):
    """V1 (empty-mount re-arch): the Development workspace's CWD is the SHARED
    /mnt/s3files mount, which starts EMPTY, while HOME is the box's REAL login home
    (so ~/.aws + the clone resolve for Stage 2; the cwd and HOME are deliberately
    different). The tree starts [] (no monorepo dirs, no pre-seeded files): the
    attendee builds coding-agents/ by hand into a blank slate.

    Real-seam isolation: point WORKSHOP_S3FILES_DIR at a tmp empty dir (a var the code
    reads), never monkeypatch internals."""
    mount = str(tmp_path / "s3files")
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", mount)

    # _dev_root returns (cwd=mount, home=real login home), NOT the same dir.
    cwd, home = ia._dev_root()
    assert cwd == os.path.abspath(mount)
    assert home == os.path.expanduser("~")

    code, sess = ia.dispatch("POST", "/api/sessions", {"agent_id": "dev"})
    assert code == 201
    s = ia._SESSIONS[sess["session_id"]]
    assert s["_dev"] is True
    assert s["_root"] == os.path.abspath(mount)         # cwd = the empty mount
    assert s["_home"] == os.path.expanduser("~")        # HOME = the real login home
    assert s["workspace"] == "/mnt/s3files"

    # THE EMPTY-START CONTRACT: a fresh dev workspace tree is EMPTY.
    _, files = ia.dispatch("GET", f"/api/sessions/{sess['session_id']}/files", None)
    assert files["tree"] == [], f"the agent home must start EMPTY, got {files['tree']!r}"

    # The attendee builds coding-agents/ by hand into the mount (cwd-relative); the
    # file lands on the shared mount every deployed runtime also sees.
    ia.dispatch("POST", f"/api/sessions/{sess['session_id']}/file",
                {"path": "/mnt/s3files/coding-agents/claude-code/Dockerfile", "content": "# built by hand\n"})
    out = ia._run_command(s, "ls coding-agents/claude-code")
    assert "Dockerfile" in out, out
    # and the tree now shows ONLY what was created (coding-agents/), nothing else.
    _, files2 = ia.dispatch("GET", f"/api/sessions/{sess['session_id']}/files", None)
    tops = {e["path"].split("/")[3] for e in files2["tree"] if e["path"].startswith("/mnt/s3files/")}
    assert tops == {"coding-agents"}, f"tree should show only coding-agents/, got {sorted(tops)}"
    ia.dispatch("DELETE", f"/api/sessions/{sess['session_id']}", None)

    # A role session is unaffected: still a jailed scratch workspace shown as /mnt/s3files.
    ia.dispatch("POST", "/api/agents/deploy", {"agent_id": "claude-code"})
    _, rsess = ia.dispatch("POST", "/api/sessions", {"agent_id": "claude-code"})
    r = ia._SESSIONS[rsess["session_id"]]
    assert not r.get("_dev") and r["workspace"] == "/mnt/s3files"
    ia.dispatch("DELETE", f"/api/sessions/{rsess['session_id']}", None)


def test_file_tree_is_bounded_so_a_huge_folder_never_hangs(tmp_path, monkeypatch):
    """Regression for the 'Mounting workspace…' hang when the root is a real HOME:
    the explorer walk is bounded by depth + node count, so opening a folder with a
    deep, wide, partly-hidden tree returns a usable list fast (not 100k nodes).
    Real-seam isolation: tighten the caps via the env vars the code reads."""
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(tmp_path / "s3files"))
    monkeypatch.setenv("WORKSHOP_TREE_MAX_DEPTH", "3")   # read at call time, no reload
    monkeypatch.setenv("WORKSHOP_TREE_MAX_NODES", "50")

    # A HOME-like folder: a dot-dir we must skip, a deep chain past the depth cap,
    # and many files past the node cap.
    home = tmp_path / "home"
    (home / ".cache" / "junk").mkdir(parents=True)        # dot-dir: skipped below root
    (home / ".cache" / "junk" / "big.bin").write_text("x", encoding="utf-8")
    deep = home / "a" / "b" / "c" / "d" / "e"             # 5 levels, past depth 3
    deep.mkdir(parents=True)
    (deep / "too-deep.txt").write_text("x", encoding="utf-8")
    for i in range(200):                                   # 200 files, past 50-node cap
        (home / f"f{i:03d}.txt").write_text("x", encoding="utf-8")

    _, sess = ia.dispatch("POST", "/api/sessions", {"agent_id": "dev"})
    sid = sess["session_id"]
    code, out = ia.dispatch("POST", f"/api/sessions/{sid}/open-folder", {"path": str(home)})
    assert code == 200 and out["has_folder"] is True
    tree = out["tree"]
    # Bounded: never returns the full 200+ nodes.
    assert len(tree) <= 50, f"tree must be capped, got {len(tree)}"
    # Dot-dir below the root is pruned; nothing past the depth cap leaks in.
    assert not any("/.cache" in n["path"] for n in tree), "dot-dirs must be skipped"
    assert not any("too-deep.txt" in n["path"] for n in tree), "depth cap must hold"
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_open_folder_reroots_a_dev_session(tmp_path, monkeypatch):
    """VS Code 'Open Folder': re-root a Development session at a chosen dir; the tree
    + workspace label follow, and a role session rejects the action (dev-only)."""
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(tmp_path / "s3files"))
    code, sess = ia.dispatch("POST", "/api/sessions", {"agent_id": "dev"})
    sid = sess["session_id"]
    assert code == 201 and sess["workspace"] == "/mnt/s3files" and sess["has_folder"] is True

    # A real folder elsewhere with a file in it.
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    code, out = ia.dispatch("POST", f"/api/sessions/{sid}/open-folder", {"path": str(proj)})
    assert code == 200 and out["ok"] is True and out["has_folder"] is True
    assert out["workspace"] == str(proj)           # an arbitrary path shows verbatim
    paths = {e["path"] for e in out["tree"]}
    assert f"{proj}/pkg" in paths and f"{proj}/pkg/main.py" in paths
    # the re-rooted tree round-trips through the files endpoint too
    _, files = ia.dispatch("GET", f"/api/sessions/{sid}/files", None)
    assert files["workspace"] == str(proj) and files["has_folder"] is True
    # ~ renders as the VS Code-style label
    code, home_out = ia.dispatch("POST", f"/api/sessions/{sid}/open-folder", {"path": "~"})
    assert code == 200 and home_out["workspace"] == "~"
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)

    # A role (non-dev) session refuses open-folder.
    ia.dispatch("POST", "/api/agents/deploy", {"agent_id": "claude-code"})
    _, rsess = ia.dispatch("POST", "/api/sessions", {"agent_id": "claude-code"})
    code, err = ia.dispatch("POST", f"/api/sessions/{rsess['session_id']}/open-folder", {"path": str(proj)})
    assert code == 400 and "Development" in err["error"]
    ia.dispatch("DELETE", f"/api/sessions/{rsess['session_id']}", None)


def test_close_folder_gives_the_no_folder_welcome_state(tmp_path, monkeypatch):
    """Close Folder (null path) drops to the VS Code no-folder state: has_folder
    False, empty workspace + tree, until a folder is opened again."""
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(tmp_path / "s3files"))
    _, sess = ia.dispatch("POST", "/api/sessions", {"agent_id": "dev"})
    sid = sess["session_id"]
    code, out = ia.dispatch("POST", f"/api/sessions/{sid}/open-folder", {"path": None})
    assert code == 200 and out["has_folder"] is False and out["workspace"] == "" and out["tree"] == []
    _, files = ia.dispatch("GET", f"/api/sessions/{sid}/files", None)
    assert files["has_folder"] is False and files["tree"] == []
    # reopening a folder restores it
    code, re = ia.dispatch("POST", f"/api/sessions/{sid}/open-folder", {"path": str(tmp_path / "s3files")})
    assert code == 200 and re["has_folder"] is True
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


def test_default_dev_root_is_env_resolved(tmp_path, monkeypatch):
    """The Development default is env-resolved: WORKSHOP_S3FILES_DIR (or the real mount)
    keeps the /mnt/s3files label (workshop + tests); WORKSHOP_DEV_ROOT=home forces the
    login HOME (a plain local box)."""
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(tmp_path / "s3files"))
    monkeypatch.delenv("WORKSHOP_DEV_ROOT", raising=False)
    ws, label = ia._default_dev_root()
    assert ws == os.path.abspath(str(tmp_path / "s3files")) and label == "/mnt/s3files"

    monkeypatch.setenv("WORKSHOP_DEV_ROOT", "home")
    ws2, label2 = ia._default_dev_root()
    assert ws2 == os.path.expanduser("~") and label2 == "~"


def test_default_dev_root_is_clone_first_clone(tmp_path, monkeypatch):
    """Clone-first: with NO mount env set, a fresh Development session opens at
    ~/<clone dirname> (the public repo the box cloned), labelled the same, NEVER
    /mnt/s3files (which the attendee has not created yet). Falls back to HOME if
    the clone is absent. A plain `git clone` of the public repo yields exactly
    ~/sample-amazon-bedrock-agentcore-coding-agents."""
    fake_home = tmp_path / "home"
    dirname = ia._clone_dirname()
    (fake_home / dirname).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("WORKSHOP_S3FILES_DIR", raising=False)
    monkeypatch.delenv("WORKSHOP_DEV_ROOT", raising=False)
    ws, label = ia._default_dev_root()
    assert ws == str(fake_home / dirname) and label == "~/" + dirname

    # No clone on a bare box -> HOME, never the mount.
    import shutil
    shutil.rmtree(fake_home / dirname)
    ws2, label2 = ia._default_dev_root()
    assert ws2 == str(fake_home) and label2 == "~"


def test_home_start_can_open_the_explicit_s3files_mount(tmp_path, monkeypatch):
    """Clone-first starts at HOME, then Open Folder maps the UI mount path to the
    explicit local mount seam while preserving the /mnt/s3files display label."""
    mount = tmp_path / "s3files"
    mount.mkdir()
    (mount / "probe.txt").write_text("shared\n", encoding="utf-8")
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(mount))
    monkeypatch.setenv("WORKSHOP_DEV_ROOT", "home")

    code, sess = ia.dispatch("POST", "/api/sessions", {"agent_id": "dev"})
    assert code == 201 and sess["workspace"] == "~"
    sid = sess["session_id"]
    code, opened = ia.dispatch(
        "POST", f"/api/sessions/{sid}/open-folder", {"path": "/mnt/s3files"})
    assert code == 200 and opened["workspace"] == "/mnt/s3files"
    assert any(item["path"] == "/mnt/s3files/probe.txt" for item in opened["tree"])
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)


@pytest.mark.skipif(os.environ.get("WORKSHOP_E2E_LIVE") != "1",
                    reason="live model call; set WORKSHOP_E2E_LIVE=1 to run")
def test_live_claude_answers_from_inside_the_jailed_pty():
    """OPT-IN live check (the one that would have caught 'Retrying in 5s'):
    run the real `claude -p` INSIDE the jailed session PTY and require a model
    reply. Proves end to end that the session env carries working Bedrock
    credentials through the HOME jail."""
    import re
    import time as _t
    sid = _open_session()
    ia.dispatch("POST", f"/api/sessions/{sid}/pty", {"open": True})
    _t.sleep(1)
    ia.dispatch("POST", f"/api/sessions/{sid}/pty",
                {"input": "claude -p 'reply with exactly: JAIL-LIVE-OK' 2>&1 | tail -2\n",
                 "offset": 0})
    text = ""
    deadline = _t.monotonic() + 90
    ok = False
    while _t.monotonic() < deadline:
        _t.sleep(2)
        _, out = ia.dispatch("POST", f"/api/sessions/{sid}/pty", {"offset": 0})
        text = out["output"]
        # the reply appears on its own line (the echoed command also contains
        # the token, so look after the command line)
        tail = text.split("| tail -2", 1)[-1]
        if re.search(r"^JAIL-LIVE-OK\s*$", tail, re.M):
            ok = True
            break
        if "Retrying" in tail:
            break
    assert ok, f"claude did not answer from inside the jail: {text[-400:]!r}"
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)
