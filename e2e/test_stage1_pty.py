"""Stage 1, content page 2 ("Open a shell on the Runtime"): the interactive terminal.

These drive the REAL command-shell PTY an attendee opens on their AgentCore Runtime
session: the local twin of `agentcore exec --it` into the microVM. Every test hits
the live `console/server.py` over the same-origin `/api/dev` mount the browser uses:
open a PTY, type into a real `/bin/bash -i`, read it back, resize, and exercise the
per-agent CLI environment baked into the shell (Claude Code on Bedrock, opencode's
`~/.config/opencode/opencode.json`, the `kiro-cli` banner). Deploy itself is the real container
build (`./setup.sh && python deploy.py`) done in the harness dir; the console
reconciles the runtime_config.json it writes (covered in test_stage1_sessions.py),
so there is no fake `agentcore` shim on PATH here.

Local engine mode (deterministic, no LLM). Each test opens its own session and closes
it (try/finally), so the file is order-independent. Run:
    python3 -m pytest e2e/test_stage1_pty.py -q
"""

from __future__ import annotations

import time
from urllib.error import HTTPError

import pytest

from e2e.conftest import (
    req, expect_status, open_session, close_session, open_pty, pty_type,
    pty_wait_for, seed_skill, SUPPORTED_AGENTS,
)

# A unique marker echoed into the shell so pty_wait_for keys on OUR output, never
# the banner or a stray prompt token. Prefix the literal so the typed command line
# (which echoes back) doesn't satisfy the wait before the command actually runs.
_OK = "STAGE1_PTY_OK_7be3"


def _run(console, cookie, sid, line, needle, tries=80):
    """Type a shell line + Enter, wait for `needle` to appear in PTY output.

    `tries` is the poll budget (×0.1s). The default suits an echo; give a slower
    command a longer budget so it doesn't lose the PTY-output race under a busy
    shared server (the whole-suite run multiplexes many PTYs through one process)."""
    pty_type(console, cookie, sid, line + "\n")
    return pty_wait_for(console, cookie, sid, needle, tries=tries)


# ---------------------------------------------------------------------------
# Opening the PTY + the basic read/write/alive contract.
# ---------------------------------------------------------------------------
def test_open_pty_returns_pty_true_and_agent_id(console, cookie):
    """Attendee opens the shell on the session: POST .../pty {open:true} -> {pty:true}."""
    sid = open_session(console, cookie, "claude-code")
    try:
        code, out = req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                        {"open": True, "resize": {"cols": 100, "rows": 30}},
                        headers=cookie)
        assert code == 200
        assert out["pty"] is True
        assert out["agent_id"] == "claude-code"
    finally:
        close_session(console, cookie, sid)


def test_type_echo_hello_reads_back(console, cookie):
    """Attendee types `echo hello` and sees `hello` print in the terminal.

    bash echoes the typed command line, so the marker shows up once for the
    keystrokes and again as the command's stdout; assert BOTH appear by waiting
    for the second occurrence (a trailing sentinel printed only after the echo)."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        # Build the output token by concatenation so the literal `hello` value is
        # NOT present in the typed command line; only `echo`'s stdout carries it.
        out = _run(console, cookie, sid,
                   f"printf '%s-%s\\n' {_OK} hello", f"{_OK}-hello")
        assert f"{_OK}-hello" in out, out[-400:]
    finally:
        close_session(console, cookie, sid)


def test_pty_alive_stays_true_after_a_command(console, cookie):
    """The shell process keeps running between keystrokes: alive stays true."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        _run(console, cookie, sid, f"echo {_OK}-alive", f"{_OK}-alive")
        out = pty_type(console, cookie, sid, "")          # poll, no input
        assert out["alive"] is True
    finally:
        close_session(console, cookie, sid)


def test_pty_offset_advances_and_returns_new_output(console, cookie):
    """The terminal pages output by offset: a follow-up poll returns only new bytes.

    The needle must be built by `printf` concatenation (NOT a literal `echo {_OK}-a`)
    so the typed command line that bash echoes back does NOT itself contain it: the
    wait then keys on the command's STDOUT, which means by the time we sample the
    offset the command has fully run and its output + the trailing prompt are already
    in the buffer. A literal echo satisfies the wait on the keystroke echo alone,
    sampling `end` BEFORE the late stdout/prompt land; on a slow shared box those late
    bytes (carrying the same token) then show up after `end` and fail the assertion."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        # `%s-%s` keeps the literal `a` value out of the typed line; only stdout has it.
        _run(console, cookie, sid, f"printf '%s-%s\\n' {_OK} a", f"{_OK}-a")
        # Settle: drain any trailing prompt bytes so `end` is a true end-of-output
        # baseline, not a mid-flush point. Poll from the running end until it stops
        # advancing (bounded), so the offset contract is tested on quiescent output.
        end = pty_type(console, cookie, sid, "")["offset"]
        for _ in range(20):
            time.sleep(0.1)
            probe = pty_type(console, cookie, sid, "", offset=end)
            if not probe["output"]:
                break
            end = probe["offset"]
        # From the settled end: nothing new, so output is empty but the offset holds.
        nxt = pty_type(console, cookie, sid, "", offset=end)
        assert nxt["offset"] >= end
        assert nxt["output"] == "", nxt["output"]
    finally:
        close_session(console, cookie, sid)


def test_pty_state_persists_across_commands(console, cookie):
    """Real bash state persists: set a var, read it back in a later keystroke round-trip."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        _run(console, cookie, sid, f"X={_OK}-var", f"X={_OK}-var")
        out = _run(console, cookie, sid, "echo $X", f"{_OK}-var")
        assert f"{_OK}-var" in out, out[-400:]
    finally:
        close_session(console, cookie, sid)


def test_pty_cwd_is_the_s3files_workspace(console, cookie):
    """`pwd` shows the session workspace (the /mnt/s3files mount) the shell opened in."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        out = _run(console, cookie, sid, "pwd", "workspace")
        assert "workspace" in out, out[-400:]
    finally:
        close_session(console, cookie, sid)


def test_pty_sees_participant_created_skill(console, cookie):
    """The shell shares the workspace with the editor: a file the participant CREATES
    (New File → paste cost_analyzer.py) shows up in the PTY's `ls`.

    The workspace starts EMPTY; the participant authors cost_analyzer.py in the editor
    (seed_skill writes it under sample/ through the real file API, the same path New
    File takes). The PTY's cwd IS that session workspace (the console maps it to
    /mnt/s3files for the attendee), so `ls sample` lists what they created. A literal
    `ls /mnt/s3files` only resolves on the deployed Runtime box where that mount exists;
    on the local/dev box it errors `No such file or directory`, so list the cwd."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        seed_skill(console, cookie, sid)
        out = _run(console, cookie, sid, "ls -1 sample", "cost_analyzer.py")
        assert "cost_analyzer.py" in out, out[-400:]
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# Resize.
# ---------------------------------------------------------------------------
def test_pty_resize_round_trips(console, cookie):
    """Attendee drags the terminal pane: a resize message is accepted, shell stays alive."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid, cols=80, rows=24)
        _, out = req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                     {"resize": {"cols": 132, "rows": 43}}, headers=cookie)
        assert out["alive"] is True
    finally:
        close_session(console, cookie, sid)


def test_pty_resize_reflected_in_tput_cols(console, cookie):
    """A resize actually changes the winsize: `tput cols` reports the new width."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid, cols=80, rows=24)
        # Resize to a distinctive width, then ask the shell its column count.
        req(console, "POST", f"/api/dev/sessions/{sid}/pty",
            {"resize": {"cols": 137, "rows": 40}}, headers=cookie)
        out = _run(console, cookie, sid, "tput cols", "137")
        assert "137" in out, out[-400:]
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# Closed-session contract: a closed session's PTY is gone -> 409.
# ---------------------------------------------------------------------------
def test_pty_on_closed_session_is_409(console, cookie):
    """After the attendee closes the session, hitting its PTY returns 409, never a 200."""
    sid = open_session(console, cookie, "claude-code")
    open_pty(console, cookie, sid)
    close_session(console, cookie, sid)
    expect_status(
        lambda: req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                    {"open": True, "resize": {"cols": 80, "rows": 24}}, headers=cookie),
        409)


def test_pty_io_on_closed_session_is_409(console, cookie):
    """Typing into a closed session's PTY (not just re-opening) is also 409."""
    sid = open_session(console, cookie, "claude-code")
    open_pty(console, cookie, sid)
    close_session(console, cookie, sid)
    expect_status(
        lambda: req(console, "POST", f"/api/dev/sessions/{sid}/pty",
                    {"input": "echo x\n", "offset": 0}, headers=cookie),
        409)


# ---------------------------------------------------------------------------
# Per-agent CLI environment baked into the shell banner.
# ---------------------------------------------------------------------------
def test_claude_env_set_in_shell(console, cookie):
    """The claude-code shell really exports the Bedrock env: `echo $CLAUDE_CODE_USE_BEDROCK` -> 1."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        # Avoid the banner's literal by tagging the echo with our marker.
        out = _run(console, cookie, sid,
                   f"echo {_OK}=$CLAUDE_CODE_USE_BEDROCK", f"{_OK}=1")
        assert f"{_OK}=1" in out, out[-400:]
    finally:
        close_session(console, cookie, sid)


def test_claude_model_env_set_in_shell(console, cookie):
    """The claude-code shell exports the Bedrock model id: ANTHROPIC_MODEL is the Opus id."""
    sid = open_session(console, cookie, "claude-code")
    try:
        open_pty(console, cookie, sid)
        out = _run(console, cookie, sid, f"echo {_OK}:$ANTHROPIC_MODEL",
                   f"{_OK}:us.anthropic.claude-opus-4-6-v1")
        assert f"{_OK}:us.anthropic.claude-opus-4-6-v1" in out, out[-400:]
    finally:
        close_session(console, cookie, sid)


def test_opencode_config_staged_on_disk(console, cookie):
    """The opencode shell really has ~/.config/opencode/opencode.json with the
    amazon-bedrock provider + the sonnet-4-6 model."""
    sid = open_session(console, cookie, "opencode")
    try:
        open_pty(console, cookie, sid)
        out = _run(console, cookie, sid, "cat .config/opencode/opencode.json", "amazon-bedrock")
        assert '"amazon-bedrock"' in out, out[-600:]
        assert "claude-sonnet-4-6" in out, out[-600:]
    finally:
        close_session(console, cookie, sid)


def test_kiro_steering_staged_on_disk(console, cookie):
    """The kiro shell has its steering file (~/.kiro/steering/agent.md, model auto)."""
    sid = open_session(console, cookie, "kiro")
    try:
        open_pty(console, cookie, sid)
        out = _run(console, cookie, sid, "cat .kiro/steering/agent.md", "Kiro session")
        assert "auto" in out, out[-600:]
    finally:
        close_session(console, cookie, sid)


# ---------------------------------------------------------------------------
# The shelf catalog contract (the agents list the shell deploys onto).
# ---------------------------------------------------------------------------
def test_agents_catalog_shape_and_ids(console, cookie):
    """GET /api/dev/agents lists exactly claude-code,kiro,opencode with the shelf fields."""
    _, out = req(console, "GET", "/api/dev/agents", headers=cookie)
    ids = {a["agent_id"] for a in out["agents"]}
    assert ids == set(SUPPORTED_AGENTS), ids
    for a in out["agents"]:
        assert {"agent_id", "label", "name", "purpose", "model",
                "credential", "status", "runtime_arn", "endpoint"} <= set(a), a


def test_fresh_agent_without_a_real_runtime_carries_no_arn(console, cookie):
    """Stage 1 pedagogy: an agent with no real runtime_config.json (no genuine
    CreateAgentRuntime yet) never carries a runtime ARN. Reading the shelf, or
    opening a session, does NOT mint one; status stays not_deployed/deploying with
    a null ARN until a real deploy lands. ready ONLY ever pairs with a genuine
    bedrock-agentcore ARN (never local:runtime)."""
    _, out = req(console, "GET", "/api/dev/agents", headers=cookie)
    for a in out["agents"]:
        assert a["status"] in ("not_deployed", "deploying", "ready"), a
        if a["status"] == "ready":
            assert a["runtime_arn"].startswith("arn:aws:bedrock-agentcore:"), a
        else:
            assert a["runtime_arn"] is None, a
            assert a["endpoint"] is None, a
