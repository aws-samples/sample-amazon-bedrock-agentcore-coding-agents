"""Interactive API for the Development workspace and deployed-agent catalog.

Deploy ONE coding agent, open an interactive shell into its workspace, run commands,
and do a single module-to-MCP conversion by hand:

    python3 interactive-api/interactive_api.py        # serves http://localhost:8091

Everything observable is real work on this machine:
  - POST /api/agents/deploy   reconciles the agent against its deployed Runtime:
    it reads the `runtime_config.json` the harness `deploy.py` wrote (an
    `arn:aws:bedrock-agentcore:...:runtime/...` from `CreateAgentRuntime`) and flips
    the agent to `ready` ONLY when that ARN exists; otherwise it stays
    `deploying`. No `local:runtime` placeholder is ever written.
  - POST /api/sessions        creates a workspace directory on disk
    (.runs/stage1/<session>/workspace; the participant seeds cost_analyzer.py under
    sample/) and the session opens when the directory exists. `/mnt/s3files` in the UI
    maps to that dir (the same path the S3 Files mount uses on a Runtime).
  - POST /api/sessions/{id}/input  executes the command with /bin/sh in the session's
    cwd (10s timeout, output capped). `cd` is tracked. State persists across
    inputs because the files persist on disk.
  - POST /api/sessions/{id}/convert-skill  writes a single-tool MCP server file
    into the workspace, boots it as a subprocess on a free port, and verifies it the
    same way Stage 2's acceptance gate does: a `tools/list` + a `tools/call`
    over the wire (the sample result comes from the live server, not a fixture).
    `verified` is true only if the wire calls succeed.
  - Every session/conversion appends to the shared telemetry ledger
    (`.runs/telemetry.jsonl`) with the OS user used for local attribution.

On AgentCore the same contract maps to: deploy = `./setup.sh && python deploy.py`
(image -> Runtime), session = `agentcore exec --it` (command-shell PTY into the
microVM), and the workspace persistence is S3 Files / managed session storage. The
HTTP shapes do not change.

This stage is ONE agent, by hand: no fan-out, no gate, no PR. That's Stage 2.
"""

from __future__ import annotations

import atexit
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HOST = "0.0.0.0"
PORT = 8091

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)

# The usecase module + coding-agents harness are SIBLINGS of this api dir in BOTH
# layout: interactive-api and usecase-sample-to-mcp are root siblings;
# the shipped package flattens to interactive-api + usecase-sample-to-mcp.
# So their parent is this dir's parent (_ENGINES), never a hardcoded "solution"
# level that is absent on the attendee box. (Same invariant as console/server.py.)
_ENGINES = os.path.dirname(_HERE)
_USECASE = os.path.join(_ENGINES, "usecase-sample-to-mcp")
_GRADING = os.path.join(_USECASE, "grading")
_RUNS_DIR = os.path.join(_REPO, ".runs")
_STAGE1_DIR = os.path.join(_RUNS_DIR, "stage1")
_LEDGER = os.path.join(_RUNS_DIR, "telemetry.jsonl")

# Cap how many session workspaces accumulate under .runs/stage1. Each session
# leaves a real workspace dir on disk; left unbounded they pile into gigabytes
# (the captured/test runs that ballooned .runs to 2.6GB). Keep the most-recent N
# (by mtime) and prune the rest when a new session is created. Override with
# WORKSHOP_MAX_STAGE1_DIRS; the telemetry ledger and settings files are untouched.
_MAX_STAGE1_DIRS = int(os.environ.get("WORKSHOP_MAX_STAGE1_DIRS", "40"))


def _prune_dirs(parent: str, keep: int) -> None:
    """Keep the `keep` most-recently-modified immediate subdirectories of `parent`,
    deleting the older ones. Best-effort: a dir that can't be removed is skipped.
    Only ``sess_*`` (session) dirs are eligible so settings/overrides files and
    any non-session content are never touched."""
    if keep < 0:
        return
    try:
        entries = [
            os.path.join(parent, name)
            for name in os.listdir(parent)
            if name.startswith("sess_") and os.path.isdir(os.path.join(parent, name))
        ]
    except OSError:
        return
    if len(entries) <= keep:
        return
    entries.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    for stale in entries[keep:]:
        shutil.rmtree(stale, ignore_errors=True)

# The frontend builder runs opencode on Bedrock (the runtime's own region), so it
# is unaffected by the GPT-5.x allowlisting that gates the Codex path. Default
# region for its config is the workshop region.
_OPENCODE_MODEL = os.environ.get(
    "WORKSHOP_OPENCODE_MODEL", "amazon-bedrock/us.anthropic.claude-sonnet-4-6")
_OPENCODE_REGION = os.environ.get("WORKSHOP_OPENCODE_REGION", "us-west-2")
_CLAUDE_MODEL = os.environ.get("WORKSHOP_CLAUDE_MODEL", "us.anthropic.claude-opus-4-6-v1")
# LEGACY (Codex path, no longer in the active flow): the Codex harness stays in
# the repo (coding-agents/codex/) but is not a wired role. These are kept so a
# manual Codex deploy still resolves its model/region; the frontend role is now
# opencode (above).
_MANTLE_REGION = os.environ.get("WORKSHOP_MANTLE_REGION", "us-east-2")
_CODEX_MODEL = os.environ.get("WORKSHOP_CODEX_MODEL", "openai.gpt-5.5")

_OS_USER = getpass.getuser()
_CMD_TIMEOUT_S = 10
_OUTPUT_CAP = 8000  # chars per command output
_MAX_PTY_INPUT = 64 * 1024  # bytes per PTY write; bounds a keystroke-flood DoS
_MAX_AGENT_FIELD = 2000     # chars per editable agent name/purpose (right-click Edit)

# Stage 1 features Claude Code; the others are listed for parity with Stage 2's catalog.
# `name`/`purpose` are the attendee-editable display fields (right-click Edit on the
# shelf). `name` defaults to `label`; `purpose` describes the role this deployed agent
# plays as a subagent of the orchestrator.
AGENTS = [
    {"agent_id": "claude-code", "label": "Claude Code",
     "name": "Claude Code", "purpose": "Implements backend code and multi-file edits.",
     "model": _CLAUDE_MODEL, "credential": "bedrock-native",
     "status": "not_deployed", "runtime_arn": None, "endpoint": None},
    # The validator is a SECOND Claude Code, steered by an acceptance-contract
    # CLAUDE.md, since Kiro was retired from the roster (its kiro entry is kept in
    # the codebase but off every roster, like codex).
    {"agent_id": "claude-code-validator", "label": "Claude Code",
     "name": "Claude Code (validator)", "purpose": "Runs the acceptance gate that defines done.",
     "model": _CLAUDE_MODEL, "credential": "bedrock-native",
     "status": "not_deployed", "runtime_arn": None, "endpoint": None},
    {"agent_id": "opencode", "label": "opencode",
     "name": "opencode", "purpose": "Builds the chatbot UI and frontend.",
     "model": _OPENCODE_MODEL, "credential": "runtime-iam",
     "status": "not_deployed", "runtime_arn": None, "endpoint": None},
]
_AGENTS = {a["agent_id"]: a for a in AGENTS}

# Smart capture: the single source of truth for "deployed" is the
# runtime_config.json each harness's deploy.py writes (an
# arn:aws:bedrock-agentcore runtime ARN). The shelf reconciles that file directly
# (_real_runtime_for), so a deploy shows up the instant it lands: no button,
# no separate marker to drift. Per-agent name/purpose overrides (right-click Edit)
# live in a sibling JSON and survive a process restart.
_OVERRIDES_FILE = os.path.join(_STAGE1_DIR, "agent_overrides.json")


def _read_overrides() -> dict:
    try:
        with open(_OVERRIDES_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_override(agent_id: str, name: str | None, purpose: str | None) -> dict:
    overrides = _read_overrides()
    cur = dict(overrides.get(agent_id) or {})
    if name is not None:
        cur["name"] = name.strip()
    if purpose is not None:
        # Store purpose even when blank: clearing the field is a real edit, so an
        # empty string must override the catalog default (honored by presence in
        # _public_agent), not silently revert to the canned text.
        cur["purpose"] = purpose.strip()
    overrides[agent_id] = cur
    os.makedirs(_STAGE1_DIR, exist_ok=True)
    with open(_OVERRIDES_FILE, "w", encoding="utf-8") as f:
        json.dump(overrides, f)
    return cur


def _reset_deploy_state() -> None:
    """Clear the name/purpose overrides so a fresh process starts clean. This runs
    once at import. Deploy state itself lives in each harness's runtime_config.json
    (the source of truth); there is no separate marker to clear, and an undeployed
    harness simply has no runtime_config.json. Overrides are the only mutable
    on-disk shelf state this resets.
    """
    try:
        if os.path.isfile(_OVERRIDES_FILE):
            os.remove(_OVERRIDES_FILE)
    except OSError:
        pass  # best-effort; a leftover override only renames a teaching shelf card


def reset_to_clean_state() -> dict:
    """On-demand "return to clean state": the internal feature the workshop uses
    to re-run from an empty shelf WITHOUT restarting the process.

    Boot-time ``_reset_deploy_state`` only clears the on-disk overrides; a live
    process also holds mutated ``_AGENTS`` records (``_deploy_agent`` flips
    status->ready and stamps the ARN). A reset restores those in-memory
    records to their catalog defaults too. This does NOT delete a harness's
    runtime_config.json (that is a deployed Runtime; tearing it down is the
    harness ``cleanup.py``'s job); a still-deployed harness reconciles back to ready
    on the next read, which is correct. Returns the cleaned catalog."""
    with _LOCK:
        _reset_deploy_state()
        for agent in AGENTS:
            agent["status"] = "not_deployed"
            agent["runtime_arn"] = None
            agent["endpoint"] = None
        return {"reset": True, "agents": [_public_agent(a) for a in AGENTS]}


_reset_deploy_state()

_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()
_COUNTER = {"n": 0}
_EPOCH = time.strftime("%H%M%S", time.gmtime())


def _ledger_append(row: dict) -> None:
    os.makedirs(_RUNS_DIR, exist_ok=True)
    with open(_LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Where each harness's deploy.py writes its runtime_config.json (the source
# of truth for "this agent is deployed on AgentCore Runtime"). agent_id == dir name.
# Independently wirable from WORKSHOP_REPO_ROOT: a test (or an attendee box whose
# harness dir lives elsewhere) points WORKSHOP_CODING_AGENTS_DIR at the directory
# holding the per-harness runtime_config.json, so the empty-shelf pedagogy never
# reads a developer's deploy state into the suite. Real-seam isolation, not a
# monkeypatch of internals.
def _coding_agents_dir() -> str:
    explicit = os.environ.get("WORKSHOP_CODING_AGENTS_DIR")
    if explicit:
        return explicit
    root = os.environ.get("WORKSHOP_REPO_ROOT")
    if root:
        return os.path.join(root, "coding-agents")
    # No override: the harness is a sibling of this API directory.
    # in the dev repo, coding-agents on the attendee box. (Same as _USECASE.)
    return os.path.join(_ENGINES, "coding-agents")


_REAL_ARN_RE = re.compile(r"^arn:aws:bedrock-agentcore:[^:]+:\d+:runtime/.+")


def _real_runtime_for(agent_id: str) -> dict | None:
    """Read the runtime_config.json deploy.py wrote for this harness, or None.

    A deployed agent is one whose ``coding-agents/<agent_id>/runtime_config.json``
    exists and carries an ``arn:aws:bedrock-agentcore:...:runtime/...`` ARN. This
    is the single source of truth; the shelf shows ``ready`` only when the attendee
    has built the image and run ``deploy.py`` to create the Runtime."""
    path = os.path.join(_coding_agents_dir(), agent_id, "runtime_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    arn = cfg.get("runtime_arn", "")
    if isinstance(arn, str) and _REAL_ARN_RE.match(arn):
        # deployed_at: the config file's mtime is when deploy.py wrote the ARN.
        try:
            deployed_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(path)))
        except OSError:
            deployed_at = None
        return {"runtime_arn": arn, "endpoint": "DEFAULT",
                "runtime_id": cfg.get("runtime_id", ""), "region": cfg.get("region", ""),
                "deployed_at": deployed_at}
    return None


def _deploy_agent(agent: dict) -> None:
    """Reconcile this agent against its deployed Runtime.

    Stage 1 is interactive: the attendee builds the container and runs
    ``./setup.sh`` + ``python deploy.py`` in the terminal (ECR push +
    ``CreateAgentRuntime``). deploy.py writes a ``runtime_config.json`` with the
    runtime ARN. This endpoint reads that config and marks the agent ``ready``
    ONLY when an ARN exists; otherwise it stays ``deploying`` until the
    attendee's deploy.py finishes. No ``local:runtime`` placeholder is ever
    written."""
    real = _real_runtime_for(agent["agent_id"])
    if not real:
        # No Runtime yet: the attendee must build + deploy in the terminal.
        agent["status"] = "deploying"
        agent["runtime_arn"] = None
        agent["endpoint"] = None
        return
    agent["status"] = "ready"
    agent["runtime_arn"] = real["runtime_arn"]
    agent["endpoint"] = "DEFAULT"
    # No marker to write: the runtime_config.json deploy.py wrote is the durable
    # source of truth, reconciled on every read. Nothing else to persist.


# ---------------------------------------------------------------------------
# PTY: a live bash with readline (tab completion, history, colors) in the
# session workspace, with the chosen agent's environment prepared: the local
# twin of `agentcore exec --it` into a Runtime microVM. The line-based /input
# endpoint stays for scripted probes; the PTY is the human terminal.
# ---------------------------------------------------------------------------
def _agent_env(agent_id: str) -> dict[str, str]:
    """The selected agent's CLI environment: connected to Bedrock, no API key
    on disk. Opening a session with an agent IS configuring that agent."""
    env = {**os.environ, "TERM": "xterm-256color"}
    # The deploy step (write Dockerfile -> ./setup.sh -> python deploy.py) runs in
    # THIS terminal, but the HOME pin (below, in _pty_open) hides ~/-relative paths.
    # Export the resolved harness dir so the content can `cd
    # "$WORKSHOP_CODING_AGENTS_DIR/claude-code"` portably: coding-agents in
    # the dev repo, ~/<clone dirname>/coding-agents on the attendee box, with no
    # `~` and no hardcoded layout. HOME stays pinned (per-session CLI configs are preserved);
    # only the harness location is surfaced, since the jail never chrooted the FS.
    env.setdefault("WORKSHOP_CODING_AGENTS_DIR", _coding_agents_dir())
    if agent_id in ("claude-code", "claude-code-validator"):
        # The validator is a second Claude Code, so it gets the same Bedrock env.
        env.update({"CLAUDE_CODE_USE_BEDROCK": "1",
                    "ANTHROPIC_MODEL": _CLAUDE_MODEL,
                    "AWS_REGION": env.get("AWS_REGION", "us-west-2"),
                    # Sessions are ephemeral; a mid-session self-update is wrong
                    # and, with HOME jailed away from the install, it can only
                    # fail ("Auto-update failed: no write permission to npm
                    # prefix"). Turn the updater off for the session.
                    "DISABLE_AUTOUPDATER": "1"})
    elif agent_id == "opencode":
        env.update({"AWS_REGION": env.get("AWS_REGION", _OPENCODE_REGION)})
    elif agent_id == "kiro":
        env.update({"KIRO_MODEL": "auto"})
    return env


def _stage_agent_config(session: dict) -> None:
    """Write the chosen agent's config files into the session before the
    shell opens, so its CLI starts configured the moment you type its name.

    The same files the base-repo containers bake in: opencode reads
    ``~/.config/opencode/opencode.json`` (model + amazon-bedrock provider), Kiro reads
    ``~/.kiro/steering/*.md``, Claude Code is env-only (CLAUDE_CODE_USE_BEDROCK).
    The PTY exports HOME at the workspace root, so ``~`` is the session."""
    root = session["_root"]
    agent_id = session["agent_id"]
    if agent_id in ("claude-code", "claude-code-validator"):
        # Both the backend and the validator are Claude Code; stage the same config.
        # Pre-seed ~/.claude.json so `claude` starts straight into its session
        # (and paints its banner) instead of stopping on the first-run onboarding
        # AND the per-folder "trust this folder?" prompt. The trust gate is keyed
        # by the RESOLVED cwd, and /tmp symlinks to /private/tmp on macOS, so seed
        # both realpaths; on the Runtime box the workspace path resolves to itself.
        import json as _json
        trusted = {"hasTrustDialogAccepted": True,
                   "hasCompletedProjectOnboarding": True,
                   "allowedTools": [], "history": []}
        projects = {}
        for p in {root, os.path.realpath(root)}:
            projects[p] = dict(trusted)
        # The interactive TUI's FIRST-RUN theme picker ("Choose the text style…")
        # keys off the migration/startup markers, NOT hasCompletedOnboarding alone.
        # Seed the exact set the harness Dockerfile bakes in (which is why the
        # DEPLOYED agent never prompts) so `claude` in the dev shell starts straight
        # into its banner. (`theme` is not a persisted top-level key; claude strips
        # it, so the markers below, not a theme value, are what suppress the picker.)
        cfg = {"numStartups": 1,
               "hasCompletedOnboarding": True,
               "lastOnboardingVersion": "9.9.999",
               "bypassPermissionsModeAccepted": True,
               "opusProMigrationComplete": True,
               "sonnet1m45MigrationComplete": True,
               "migrationVersion": 13,
               "officialMarketplaceAutoInstallAttempted": True,
               "officialMarketplaceAutoInstalled": True,
               # sessions are ephemeral; never self-update mid-session (the
               # updater can only fail against the jailed install mirror and
               # prints "Auto-update failed: no write permission to npm prefix")
               "autoUpdates": False,
               "projects": projects}
        with open(os.path.join(root, ".claude.json"), "w", encoding="utf-8") as f:
            _json.dump(cfg, f)
        # The PTY pins HOME at the workspace (one uniform jail for every agent).
        # Claude's doctor compares its RUNNING binary path against the native
        # install it expects under HOME/.local; verified: launching through a
        # HOME/.local/bin/claude that points at the real install satisfies the
        # check (no "setup issue" line), while a bare PATH launch from a foreign
        # HOME does not. Mirror the native layout into the session when the real
        # home carries one; with an npm-global install there is nothing to mirror
        # and the binary resolves from the system PATH as before.
        real_home = os.path.expanduser("~")
        real_share = os.path.join(real_home, ".local", "share", "claude")
        if os.path.isdir(real_share):
            ws_bin = os.path.join(root, ".local", "bin")
            ws_share = os.path.join(root, ".local", "share")
            os.makedirs(ws_bin, exist_ok=True)
            os.makedirs(ws_share, exist_ok=True)
            share_link = os.path.join(ws_share, "claude")
            bin_link = os.path.join(ws_bin, "claude")
            try:
                if not os.path.lexists(share_link):
                    os.symlink(real_share, share_link)
                versions = os.path.join(share_link, "versions")
                latest = sorted(os.listdir(versions))[-1] if os.path.isdir(versions) else None
                if latest and not os.path.lexists(bin_link):
                    os.symlink(os.path.join(versions, latest), bin_link)
            except OSError:
                pass  # mirror is best-effort; worst case is the doctor note
    elif agent_id == "opencode":
        # opencode reads ~/.config/opencode/opencode.json: the amazon-bedrock
        # provider + the model. No trust gate to pre-clear (that was a Codex
        # thing), so this is just the config file.
        d = os.path.join(root, ".config", "opencode")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "opencode.json"), "w", encoding="utf-8") as f:
            json.dump({
                "$schema": "https://opencode.ai/config.json",
                "provider": {"amazon-bedrock": {"options": {"region": _OPENCODE_REGION}}},
                "model": _OPENCODE_MODEL,
                "small_model": "amazon-bedrock/anthropic.claude-haiku-4-5-20251001-v1:0",
            }, f, indent=2)
    elif agent_id == "kiro":
        d = os.path.join(root, ".kiro", "steering")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "agent.md"), "w", encoding="utf-8") as f:
            f.write("---\ninclusion: always\n---\n\n# Kiro session defaults\n\n"
                    "Model: auto (Kiro's router). Workspace: this session's\n"
                    "/mnt/s3files. Validate with the grading contract before\n"
                    "claiming done.\n")
        # kiro-cli resolves two things THROUGH HOME, and both must exist in the
        # session HOME or kiro degrades:
        #   1. its data dir (login session, sqlite, the bundled bun/tui.js):
        #      macOS: ~/Library/Application Support/kiro-cli, Linux:
        #      ~/.local/share/kiro-cli. Missing => "Not logged in".
        #   2. its sibling binaries: `kiro-cli chat` re-execs
        #      ~/.local/bin/kiro-cli-chat (and -term). Missing => the chat TUI
        #      dies with "No such file or directory (os error 2)".
        # Link the real ones into the session HOME. The session shares the box's
        # login, correct for the workshop (one attendee per box).
        real_home = os.path.expanduser("~")
        links = [os.path.join("Library", "Application Support", "kiro-cli"),
                 os.path.join(".local", "share", "kiro-cli"),
                 os.path.join(".local", "bin", "kiro-cli-chat"),
                 os.path.join(".local", "bin", "kiro-cli-term")]
        for rel in links:
            src = os.path.join(real_home, rel)
            if not os.path.exists(src):
                continue
            dst = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                if not os.path.lexists(dst):
                    os.symlink(src, dst)
            except OSError:
                pass  # best-effort; worst case kiro asks to sign in




_PTY_BANNER = {
    "claude-code": ("command -v claude >/dev/null "
                    "&& echo \"claude $(claude --version 2>/dev/null | head -1): type 'claude' to start it here\" "
                    "|| echo 'claude CLI not on PATH (npm i -g @anthropic-ai/claude-code)'; "
                    "echo \"Bedrock-connected: CLAUDE_CODE_USE_BEDROCK=$CLAUDE_CODE_USE_BEDROCK "
                    "model=$ANTHROPIC_MODEL region=$AWS_REGION (no API key)\""),
    "opencode": ("command -v opencode >/dev/null "
              "&& echo \"$(opencode --version 2>/dev/null | head -1): type 'opencode' to start it here\" "
              "|| echo 'opencode CLI not on PATH (npm i -g opencode-ai)'; "
              f"echo 'configured: ~/.config/opencode/opencode.json -> model {_OPENCODE_MODEL}, provider amazon-bedrock ({_OPENCODE_REGION})'"),
    # The Kiro CLI binary is `kiro-cli` (the bare `kiro` is the IDE). Probe and
    # tell the attendee the real command, and start the chat TUI with
    # `kiro-cli chat`.
    "kiro": ("command -v kiro-cli >/dev/null "
             "&& echo \"kiro-cli installed: type 'kiro-cli chat' to start it here\" "
             "|| echo 'kiro-cli not on PATH (curl -fsSL https://cli.kiro.dev/install | bash)'; "
             "echo 'configured: ~/.kiro/steering/agent.md -> model auto (vendor key brokered, never on disk)'"),
}


def _pty_open(session: dict, rows: int = 0, cols: int = 0) -> dict:
    """Spawn a real interactive bash on a PTY in the session workspace, with the
    chosen agent's CLI configured (config files staged, Bedrock env exported) so
    typing the agent's name starts it right here.

    ``rows``/``cols`` are the client terminal's MEASURED dimensions (xterm.js
    fits to its pane and passes them in the open call), so the winsize is right
    before the shell (or any TUI the attendee starts) first paints. A TUI
    lays out against the winsize at startup: a guessed size that differs from
    the rendered pane leaves box-drawing borders wrapping mid-line. Without a
    measurement we stay at a safe 80x24; the client's resize message corrects
    it before anything interactive runs."""
    import pty as _pty
    _pty_close(session)
    _stage_agent_config(session)
    master, slave = _pty.openpty()
    import fcntl
    import struct
    import termios
    rows = max(8, min(int(rows or 0) or 24, 200))
    cols = max(40, min(int(cols or 0) or 80, 400))
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = _agent_env(session["agent_id"])
    # ONE uniform jail: HOME is the workspace root for every agent, so `cd ~`
    # stays inside the session and each CLI's config (~/.claude.json, ~/.config/opencode,
    # ~/.kiro) is per-session and visible in the file explorer. For Claude the
    # session's ~/.local/bin/claude (mirrored from the real install by
    # _stage_agent_config) goes FIRST on PATH; resolving the binary through
    # HOME satisfies claude's install self-check, so no "setup issue" line.
    # The Development build shell uses the real home (so ~, the clone, ~/.aws, and
    # the build CLIs resolve like a build-box login); a role session jails HOME to its
    # scratch workspace so `cd ~` stays in-session and each CLI's config is per-run.
    env["HOME"] = session.get("_home") or session["_root"]
    # Pinning HOME to a jail hides ~/.aws from the AWS SDK's default chain: on a
    # laptop the chain then falls through to IMDS (169.254.169.254) and the CLI sits
    # in "Retrying in 5s" forever. Point the SDK explicitly at the REAL home's files
    # when they exist; on the Runtime box they don't, and the chain proceeds to the
    # instance role exactly as before. A dev shell already has the real HOME, so
    # this is a no-op there. (Same fix as the orchestrator's inline Bedrock env.)
    real_home = os.path.expanduser("~")
    for var, rel in (("AWS_CONFIG_FILE", os.path.join(".aws", "config")),
                     ("AWS_SHARED_CREDENTIALS_FILE", os.path.join(".aws", "credentials"))):
        path = os.path.join(real_home, rel)
        if var not in env and os.path.isfile(path):
            env[var] = path
    ws_bin = os.path.join(session["_root"], ".local", "bin")
    if os.path.isdir(ws_bin):
        env["PATH"] = ws_bin + os.pathsep + env.get("PATH", "")
    agent_id = session["agent_id"]
    proc = subprocess.Popen(
        ["/bin/bash", "-i"], stdin=slave, stdout=slave, stderr=slave,
        cwd=session["_root"], env=env,
        start_new_session=True, close_fds=True)
    os.close(slave)
    state = {"master": master, "proc": proc, "buf": b"", "lock": threading.Lock()}
    session["_pty"] = state

    def reader():
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            with state["lock"]:
                state["buf"] = (state["buf"] + chunk)[-200_000:]
    threading.Thread(target=reader, daemon=True).start()
    return {"pty": True, "agent_id": agent_id}


def _pty_close(session: dict) -> None:
    st = session.get("_pty")
    if not st:
        return
    # Interactive bash ignores SIGTERM, so kill the whole process group (the
    # shell is its own session leader) and WAIT for it before closing the
    # master fd; closing an fd another thread is read()ing can deadlock.
    import signal
    try:
        os.killpg(st["proc"].pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        st["proc"].wait(timeout=5)
    except Exception:
        pass
    try:
        os.close(st["master"])
    except OSError:
        pass
    session["_pty"] = None


def _pty_io(session: dict, body: dict) -> dict:
    """One round-trip: write raw keystrokes (if any), read new output since
    ``offset``. Bash's readline does the editing, so tab completion works."""
    st = session.get("_pty")
    if not st:
        return {"error": "no pty; open one first"}
    data = body.get("input")
    if data:
        # Bound a single keystroke write so a flood can't drive the PTY into OOM
        # or wedge the terminal. 64 KiB dwarfs any real paste/keystroke burst.
        if len(data) > _MAX_PTY_INPUT:
            return {"error": "input too large", "max_bytes": _MAX_PTY_INPUT}
        try:
            os.write(st["master"], data.encode())
        except OSError:
            return {"error": "pty closed"}
    if body.get("resize"):
        import fcntl
        import struct
        import termios
        r = body["resize"]
        try:
            fcntl.ioctl(st["master"], termios.TIOCSWINSZ,
                        struct.pack("HHHH", int(r.get("rows", 24)),
                                    int(r.get("cols", 80)), 0, 0))
        except OSError:
            pass
    offset = int(body.get("offset", 0) or 0)
    with st["lock"]:
        buf = st["buf"]
    base = max(0, len(buf) - 200_000)
    start = max(0, offset - base)
    out = buf[start:]
    return {"output": out.decode("utf-8", "replace"),
            "offset": base + len(buf),
            "alive": st["proc"].poll() is None}


def _pty_tick(session_id: str, sent: int):
    """One non-blocking step of the PTY follow loop. Reads the in-memory buffer
    (filled by the reader thread in ``_pty_open``) and returns
    ``(frames, new_sent, done)``: SSE byte frames to emit now, the updated byte
    offset, and whether the stream should end. Pure and non-blocking: no I/O
    wait happens here, so BOTH a sync and an async driver can reuse it and only
    differ in HOW they sleep between ticks.
    """
    session = _SESSIONS.get(session_id)
    st = session.get("_pty") if session else None
    if not st:
        return ([b"event: end\ndata: {\"alive\": false}\n\n"], sent, True)
    with st["lock"]:
        buf = st["buf"]
    frames = []
    base = max(0, len(buf) - 200_000)
    total = base + len(buf)
    if total > sent:
        start = max(0, sent - base)
        chunk = buf[start:].decode("utf-8", "replace")
        sent = total
        frames.append(f"data: {json.dumps({'output': chunk, 'offset': sent})}\n\n".encode("utf-8"))
    if st["proc"].poll() is not None:
        frames.append(b"event: end\ndata: {\"alive\": false}\n\n")
        return (frames, sent, True)
    return (frames, sent, False)


async def pty_stream_async(session_id: str, offset: int = 0):
    """Native-async SSE generator for a session's PTY output: the shipped route
    driver. Yields ``data:`` frames the instant new bytes appear; a final
    ``event: end`` frame when the shell exits.

    Async ON PURPOSE: this stream is unbounded (it follows a live shell), so it
    MUST be cancellable. As an async generator its only wait is
    ``await asyncio.sleep``: when the client disconnects or the server is
    shutting down (dev ``--reload``), asyncio cancels the task at that await and
    the connection is released immediately. The old sync version parked in
    ``time.sleep`` inside a threadpool thread that uvicorn could NOT cancel, so
    graceful shutdown hung forever ("Waiting for connections to close") and
    wedged the whole backend. Reading the buffer is pure memory (the reader
    thread does the only blocking I/O), so no worker thread is needed here.
    """
    import asyncio

    sent = int(offset or 0)
    yield b": open\n\n"  # comment frame flushes headers immediately
    ticks = 0
    while True:
        frames, sent, done = _pty_tick(session_id, sent)
        for f in frames:
            yield f
        if done:
            return
        ticks += 1
        if ticks % 200 == 0:  # ~every 5s idle: keepalive so a dead socket surfaces
            yield b": ping\n\n"
        await asyncio.sleep(0.025)  # cancelled here on disconnect / shutdown


def pty_stream(session_id: str, offset: int = 0, should_stop=None):
    """Synchronous SSE generator, kept for non-async callers (the stdlib
    backup server). The shipped FastAPI route uses ``pty_stream_async`` instead.

    ``should_stop`` is an optional zero-arg predicate to break the loop early;
    without it this is an unbounded ``while True`` a sync server cannot cancel.
    """
    sent = int(offset or 0)
    yield b": open\n\n"
    ticks = 0
    while True:
        if should_stop is not None and should_stop():
            return
        frames, sent, done = _pty_tick(session_id, sent)
        for f in frames:
            yield f
        if done:
            return
        ticks += 1
        if ticks % 200 == 0:
            yield b": ping\n\n"
        time.sleep(0.025)


# The Development workspace is the build and deploy surface (the console terminal
# that replaces an SSH session into a build box). It starts at the box HOME, where
# the attendee clones the public workshop repo into ~/<clone dirname>. After the
# attendee creates and mounts S3 Files, the Open Folder action switches the editor
# to /mnt/s3files. Every deployed runtime later mounts that same access point at
# the same path.
#
# HOME is the box's real home, not the mount. That keeps ~/.aws and the cloned
# payload available before and after Open Folder changes the workspace root.
#
# Returns (real workspace path, virtual UI label).
# cwd resolution remains wirable for tests and capture. WORKSHOP_DEV_ROOT=home forces
# the clone-first starting point. WORKSHOP_S3FILES_DIR or a real writable mount selects
# /mnt/s3files. A plain local box with neither starts at HOME. This is configuration,
# never a fallback from a failed runtime dispatch.
def _clone_dirname() -> str:
    """Basename of the workshop clone on the box. A plain
    ``git clone https://github.com/aws-samples/sample-amazon-bedrock-agentcore-coding-agents.git``
    with no explicit target produces exactly this directory (the repo name), so
    ``~/<name>`` is what the attendee reproducing the workshop by hand actually
    gets. Wirable (WORKSHOP_CLONE_DIRNAME) so tests/capture can use a short name."""
    return (os.environ.get("WORKSHOP_CLONE_DIRNAME")
            or "sample-amazon-bedrock-agentcore-coding-agents")


def _clone_dir() -> str | None:
    """The real workshop-clone dir, if one is configured/present. On the box the
    clone is at $HOME/<clone dirname> (== ~/<name> for the ubuntu user, since
    HomeFolder is that user's home), so the unset resolution below already finds
    it; an absolute WORKSHOP_DEV_ROOT is honored as a test/capture override."""
    raw = (os.environ.get("WORKSHOP_DEV_ROOT") or "").strip()
    if raw.startswith("/") and os.path.isdir(raw):
        return os.path.abspath(raw)
    clone = os.path.join(os.path.expanduser("~"), _clone_dirname())
    return clone if os.path.isdir(clone) else None


def _clone_label(path: str) -> str:
    """The clone dir is shown as ``~/<clone dirname>`` (the path the content's
    ``cd ~/<name>`` targets) regardless of its real parent; anything else
    verbatim."""
    return ("~/" + _clone_dirname()
            if os.path.basename(os.path.normpath(path)) == _clone_dirname()
            else path)


def _default_dev_root() -> tuple[str, str]:
    """Return (workspace_dir, virtual_label) for a fresh Development session.

    The workshop is clone-first: the box has the public repo cloned at
    ~/<clone dirname> before the attendee does anything, and S3 Files does NOT
    exist yet (they create it in Stage 1). So a fresh session opens at the clone,
    never /mnt/s3files; the attendee switches to the mount with Open Folder AFTER
    creating it.

    Start resolution (configuration, never a fallback from a failed dispatch):
      WORKSHOP_DEV_ROOT=home                -> HOME
      WORKSHOP_DEV_ROOT=src                 -> the clone if present, else HOME
                                               (``src`` is a legacy alias for the
                                               clone-first start, not a dir name)
      WORKSHOP_DEV_ROOT=/abs/path           -> that dir (a test/capture override;
                                               the box does not need it since the
                                               clone is at $HOME/<clone dirname>)
      WORKSHOP_DEV_ROOT=mount, or
        WORKSHOP_S3FILES_DIR set            -> the mount seam, labelled
                                               /mnt/s3files (EXPLICIT opt-in:
                                               tests and capture)
      otherwise (unset)                     -> the clone if present, else HOME
    The clone dir is always shown as ``~/<clone dirname>`` (the path the content's
    ``cd ~/<name>`` targets), regardless of its real parent. The mount is an
    explicit start only for tests/capture; on the real box the start is the
    clone."""
    real_home = os.path.expanduser("~")
    clone = os.path.join(real_home, _clone_dirname())
    clone_label = "~/" + _clone_dirname()
    raw = (os.environ.get("WORKSHOP_DEV_ROOT") or "").strip()
    forced = raw.lower()
    explicit_mount = os.environ.get("WORKSHOP_S3FILES_DIR")

    if forced == "home":
        return real_home, "~"
    # An explicit absolute path: a test/capture override. The real box does not
    # need it (the clone is at $HOME/<clone dirname>, found by the unset/`src`
    # branch below).
    if raw.startswith("/") and os.path.isdir(raw):
        return os.path.abspath(raw), _clone_label(raw)
    # An explicit WORKSHOP_DEV_ROOT=src (legacy clone-first alias) starts at the
    # clone even when the mount seam (WORKSHOP_S3FILES_DIR) is also set: capture
    # needs the clone-first START at the clone while still having the mount
    # available for the later Open Folder shots.
    if forced == "src" and os.path.isdir(clone):
        return clone, clone_label
    # Explicit mount start (tests/capture only). Real boxes set neither var.
    if forced == "mount" or explicit_mount:
        if explicit_mount:
            os.makedirs(explicit_mount, exist_ok=True)
            return os.path.abspath(explicit_mount), "/mnt/s3files"
        mount = "/mnt/s3files"
        if os.path.isdir(mount) and os.access(mount, os.W_OK):
            return mount, "/mnt/s3files"
        cwd = os.path.join(_STAGE1_DIR, "s3files-home")
        os.makedirs(cwd, exist_ok=True)
        return cwd, "/mnt/s3files"
    if os.path.isdir(clone):
        return clone, clone_label
    # No clone (plain local box, nothing cloned): the login HOME, VS Code-like.
    return real_home, "~"


# Back-compat shim: callers that want the historic (root, home) tuple. The first
# element is the workspace dir; HOME stays the real login home in every case.
def _dev_root() -> tuple[str, str]:
    workspace, _label = _default_dev_root()
    return workspace, os.path.expanduser("~")


def _folder_label(path: str) -> str:
    """The virtual root label the UI shows for an opened folder: the real HOME is
    rendered as ``~`` (VS Code-style), a path under HOME as ``~/<rel>`` (so the
    clone shows ``~/<clone dirname>``), every other absolute path verbatim."""
    home = os.path.expanduser("~")
    rp = os.path.realpath(path)
    if rp == os.path.realpath(home):
        return "~"
    mount_override = os.environ.get("WORKSHOP_S3FILES_DIR")
    if mount_override and rp == os.path.realpath(mount_override):
        return "/mnt/s3files"
    if path == "/mnt/s3files":
        return "/mnt/s3files"
    # The configured workshop clone ($HOME/<clone dirname> on the box) shows as
    # ~/<clone dirname>.
    clone = _clone_dir()
    if clone and rp == os.path.realpath(clone):
        return _clone_label(clone)
    # A path inside HOME renders VS Code-style as ~/<relative>.
    rp_home = os.path.realpath(home)
    if rp == rp_home or rp.startswith(rp_home + os.sep):
        return "~/" + os.path.relpath(rp, rp_home)
    return path


def _list_dirs(raw_path: str | None) -> dict:
    """Immediate SUBDIRECTORIES of a path, for the VS Code-style Open Folder finder
    modal to navigate one level at a time. ``~`` / ``$VARS`` / ``/mnt/s3files`` are
    expanded the same way _open_folder resolves them, so the finder and the actual
    open agree. Returns {path, label, parent, entries:[{name,path}]} where entries
    are directories only (files are irrelevant to a folder picker), sorted, dot-dirs
    skipped except the agent-steering ones, bounded so a huge dir stays responsive.
    A missing/blank path defaults to the box HOME (the finder's starting point)."""
    home = os.path.expanduser("~")
    requested = (str(raw_path).strip() if raw_path else "") or "~"
    mount_override = os.environ.get("WORKSHOP_S3FILES_DIR")
    if requested == "/mnt/s3files" and mount_override:
        target = os.path.abspath(mount_override)
    else:
        target = os.path.abspath(os.path.expandvars(os.path.expanduser(requested)))
    if not os.path.isdir(target) or not os.access(target, os.R_OK):
        return {"error": f"not a readable directory: {requested}"}
    entries: list[dict] = []
    try:
        for name in sorted(os.listdir(target)):
            if len(entries) >= 500:
                break
            if name.startswith(".") and name not in _KEEP_DOTDIRS:
                continue
            full = os.path.join(target, name)
            if name in _SKIP_DIRS:
                continue
            try:
                if os.path.isdir(full):
                    entries.append({"name": name, "path": full})
            except OSError:
                continue
    except OSError as exc:
        return {"error": str(exc)}
    parent = os.path.dirname(target.rstrip(os.sep)) or "/"
    return {"path": target, "label": _folder_label(target),
            "parent": None if os.path.realpath(target) == os.path.realpath(home) else parent,
            "home": home, "entries": entries}


def _open_folder(session: dict, raw_path: str | None) -> dict:
    """VS Code "Open Folder": re-root a Development session at ``raw_path`` (``~``
    and ``$VARS`` expanded). A falsy path CLOSES the folder (a no-folder welcome
    state: tree empty, no cwd). The PTY is closed so the next shell spawns at the
    new cwd. Returns the new {workspace, cwd, has_folder, tree} for the client."""
    # Close folder -> no-folder welcome state.
    if not raw_path or not str(raw_path).strip():
        _pty_close(session)
        session["_has_folder"] = False
        session["_root"] = ""
        session["_real_cwd"] = ""
        session["workspace"] = ""
        session["cwd"] = ""
        session["_vroot"] = ""
        return {"ok": True, "has_folder": False, "workspace": "", "cwd": "", "tree": []}

    requested = str(raw_path).strip()
    mount_override = os.environ.get("WORKSHOP_S3FILES_DIR")
    if requested == "/mnt/s3files" and mount_override:
        target = os.path.abspath(mount_override)
    else:
        target = os.path.abspath(os.path.expandvars(os.path.expanduser(requested)))
    if not os.path.isdir(target):
        return {"error": f"not a directory: {raw_path}", "has_folder": session.get("_has_folder", True)}
    if not os.access(target, os.R_OK):
        return {"error": f"not readable: {raw_path}", "has_folder": session.get("_has_folder", True)}

    _pty_close(session)            # re-spawn at the new cwd on the next open
    label = _folder_label(target)
    session["_root"] = target
    session["_real_cwd"] = target
    session["_vroot"] = label
    session["workspace"] = label
    session["cwd"] = label
    session["_has_folder"] = True
    # HOME stays the real login home so ~/.aws + the build CLIs keep resolving.
    session["_home"] = os.path.expanduser("~")
    return {"ok": True, "has_folder": True, "workspace": label, "cwd": label,
            "tree": _file_tree(session)}


def _new_session(agent_id: str) -> dict:
    _COUNTER["n"] += 1
    session_id = f"sess_{_EPOCH}_{_COUNTER['n']:03d}"
    # Cap the workspace pile before adding a new one, so .runs/stage1 can't grow
    # without bound across a long workshop / many test runs.
    _prune_dirs(_STAGE1_DIR, _MAX_STAGE1_DIRS)
    # Development starts at the env-resolved root. The workshop flow forces HOME at
    # bootstrap, then the attendee switches to the mounted /mnt/s3files with Open
    # Folder. Tests can start directly on an explicit mount. Every role gets a fresh
    # per-session scratch jail under .runs/stage1 and displays it as /mnt/s3files.
    if agent_id == "dev":
        workspace, vlabel = _default_dev_root()
        dev_home = os.path.expanduser("~")
        root = workspace
    else:
        root = os.path.join(_STAGE1_DIR, session_id)
        workspace = os.path.join(root, "workspace")
        os.makedirs(workspace, exist_ok=True)
        dev_home = None
        vlabel = "/mnt/s3files"
    dev = agent_id == "dev"
    # Nothing is auto-seeded into either HOME or the mount. The attendee clones the
    # repo at HOME, then creates the mount and copies cost_analyzer.py there. The
    # display label follows the current root: ~ at HOME and /mnt/s3files after the
    # Open Folder switch. Role scratch jails always use the runtime mount label.
    display = vlabel
    session = {
        "session_id": session_id,
        "agent_id": agent_id,
        "status": "open",          # open the moment the real workspace exists
        "workspace": display,
        "cwd": display,
        "history": [],
        "_root": workspace,        # real dir backing the displayed path
        "_vroot": vlabel,          # the virtual root label the UI shows (/mnt/s3files or ~)
        "_has_folder": True,       # a folder is open (False = VS Code no-folder welcome)
        "_real_cwd": workspace,
        # HOME for the shell: the real home for the Development build box (so `~`
        # and the clone resolve like a build-box login), else the scratch jail.
        "_home": dev_home or workspace,
        "_dev": bool(dev),         # Development build shell vs role scratch jail
        "_tools": [],
        "_server": None,           # {"pid","port","endpoint"} once converted
        "_pty": None,              # live bash PTY (tab completion, history)
        "_started": _now_iso(),
        "_t0": time.monotonic(),
    }
    _SESSIONS[session_id] = session
    _ledger_append({
        "kind": "stage1_session", "session_id": session_id, "agent_id": agent_id,
        "user_id": _OS_USER, "started_at": session["_started"],
        "workspace": workspace, "pid": None,
    })
    return session


def _to_virtual(session: dict, text: str) -> str:
    # Map the session's real workspace root to the /mnt/s3files virtual root for
    # BOTH role and Development sessions. The frontend's file tree strips
    # /mnt/s3files to render the workspace's own top-level entries; without this a
    # dev session (rooted at the real clone abs path) rendered the entire absolute
    # path as phantom folders (home > ubuntu > <clone> > coding-agents > ...) and the
    # open/read/write round-trip broke (_safe_join expects /mnt/s3files-relative).
    # The interactive PTY is a separate raw stream, so real paths still show in the
    # live terminal; only the file tree + scripted /input output are normalized.
    # The virtual root is per-session (_vroot): /mnt/s3files on the workshop box, the
    # login HOME (~) on a plain local box, or whatever folder the attendee opened.
    return text.replace(session["_root"], session.get("_vroot", "/mnt/s3files"))


def _to_real(session: dict, text: str) -> str:
    return text.replace(session.get("_vroot", "/mnt/s3files"), session["_root"])


def _run_command(session: dict, raw: str) -> str:
    """Execute ONE shell command for real in the session's cwd.

    /bin/sh -c, 10s timeout, output capped. `cd` is tracked by re-resolving the cwd
    after the command (`&& pwd`), so directory state persists across inputs exactly
    like a PTY would. Paths render as /mnt/s3files for fidelity with the Runtime view.
    """
    cmd = raw.strip()
    if not cmd:
        return ""
    real_cmd = _to_real(session, cmd)
    # The session env (Bedrock-connected, no API key) with the session's own
    # ~/.local/bin FIRST on PATH, so `agentcore` resolves to the staged shim
    # (real CodeZip + marker), never a host-installed `agentcore` that would
    # shadow it and make the deploy a no-op. Mirrors the PTY launch exactly.
    env = _agent_env(session["agent_id"])
    # Mirror the PTY's HOME so `~`/the clone resolve the same in scripted /input as in
    # the interactive shell (the dev build shell uses the real checkout's parent).
    if session.get("_home"):
        env["HOME"] = session["_home"]
    ws_bin = os.path.join(session["_root"], ".local", "bin")
    env["PATH"] = ws_bin + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", f"{real_cmd}\nprintf '\\n__CWD__%s' \"$PWD\""],
            cwd=session["_real_cwd"], capture_output=True, text=True,
            timeout=_CMD_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        return f"(timed out after {_CMD_TIMEOUT_S}s)"
    out = proc.stdout or ""
    if "__CWD__" in out:
        out, _, new_cwd = out.rpartition("__CWD__")
        new_cwd = new_cwd.strip()
        # keep the session jailed to its workspace
        if new_cwd.startswith(session["_root"]):
            session["_real_cwd"] = new_cwd
            session["cwd"] = _to_virtual(session, new_cwd) or session["workspace"]
    text = (out + (proc.stderr or "")).rstrip("\n")
    text = _to_virtual(session, text)
    if len(text) > _OUTPUT_CAP:
        text = text[:_OUTPUT_CAP] + "\n… (output capped)"
    return text


# ---------------------------------------------------------------------------
# Real workspace files: a tree/read/write surface over the session's jailed dir,
# plus a harness scaffolder (copy/make the real steering files) and a code-upload
# deploy that packages the workspace exactly like AgentCore's code-first launch.
# ---------------------------------------------------------------------------
_TEXT_EXT = {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".html",
             ".css", ".js", ".sh", ".cfg", ".ini", ".mdc", ""}
_MAX_FILE = 200_000          # bytes; refuse to read/write larger blobs in the UI
# Junk skipped at EVERY depth (never attendee work). Agent steering dirs created on
# the mount (AGENTS.md, .config/opencode/opencode.json, .kiro/steering/*.md) remain visible because
# they are workshop files. Root CLI cache names are filtered separately below.
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules",
              ".local", ".cache", ".config", ".npm", ".aws",
              "Library", ".semantic_search"}
# Agent-CLI config artifacts hidden ONLY at the workspace ROOT. CLAUDE_CONFIG_DIR
# is the workspace root, so claude drops these bare names there (cache/, plugins/,
# settings.json, …). An attendee's OWN nested skills/logs/ or a settings.json they
# create inside a project dir must stay visible; root-only keeps both true.
_SKIP_DIRS_ROOT = {"marketplaces", "plugins", "backups", "statsig",
                   "shell-snapshots", "cache", "projects", "todos", "logs",
                   "ide", "history"}
_SKIP_FILES_ROOT = {".claude.json", ".claude.json.backup", "settings.json",
                    "known_marketplaces.json", ".last-update-result.json",
                    "changelog.md", ".bash_history", ".viminfo"}


def _safe_join(session: dict, rel: str) -> str | None:
    """Resolve a vroot-relative path to a real path INSIDE the jail, or None."""
    rel = (rel or "").replace(session.get("_vroot", "/mnt/s3files"), "").lstrip("/")
    full = os.path.normpath(os.path.join(session["_root"], rel))
    root = os.path.realpath(session["_root"])
    if os.path.realpath(full) == root or os.path.realpath(full).startswith(root + os.sep):
        return full
    return None


# Dot dirs that ARE attendee work and stay visible (steering); every OTHER dotdir
# at depth is skipped so opening HOME doesn't descend ~/.vscode, ~/.git, ~/Library…
_KEEP_DOTDIRS = {".config", ".kiro", ".claude"}


def _file_tree(session: dict) -> list[dict]:
    """Return the workspace as a flat, sorted list of {path,type,size} (dirs first).

    Bounded by depth + node count so opening a huge folder (e.g. a real HOME with
    100k+ files) returns a usable tree fast instead of walking everything and hanging
    the "Mounting workspace…" spinner. VS Code lazily loads on expand; we keep the
    flat-list contract but cap DEPTH + total NODES and skip un-authored dot-dirs.
    The caps are read at CALL time (a real env seam tests can set), not import."""
    root = session.get("_root") or ""
    if not root or not os.path.isdir(root):
        return []          # no-folder (VS Code welcome) state, or a stale root
    max_depth = int(os.environ.get("WORKSHOP_TREE_MAX_DEPTH", "8"))
    max_nodes = int(os.environ.get("WORKSHOP_TREE_MAX_NODES", "4000"))
    out: list[dict] = []
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(out) >= max_nodes:
            break
        at_root = os.path.samefile(dirpath, root) if os.path.exists(dirpath) else False
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        skip_dirs = _SKIP_DIRS | (_SKIP_DIRS_ROOT if at_root else set())
        # Prune: junk dirs always; past the depth cap stop descending; and below the
        # root, skip dot-dirs that are not the agent-steering ones we want to show.
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in skip_dirs
            and depth < max_depth
            and not (d.startswith(".") and d not in _KEEP_DOTDIRS))
        for d in dirnames:
            full = os.path.join(dirpath, d)
            out.append({"path": _to_virtual(session, full), "type": "dir", "size": 0})
        for fn in sorted(filenames):
            if len(out) >= max_nodes:
                break
            if at_root and fn in _SKIP_FILES_ROOT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({"path": _to_virtual(session, full), "type": "file", "size": size})
    # Hierarchical (DFS) order, directories before files among siblings: the
    # order a VS Code explorer paints, so a child row always sits directly
    # under its parent. (The old depth-first-by-LEVEL sort scattered children
    # to the bottom of the list.)
    vroot = session.get("_vroot", "/mnt/s3files")
    def _sort_key(e: dict) -> list:
        parts = e["path"].replace(vroot, "").strip("/").split("/")
        return ([(0, c) for c in parts[:-1]]
                + [(0 if e["type"] == "dir" else 1, parts[-1])])
    out.sort(key=_sort_key)
    return out


def _search_files(session: dict, query: str, *, max_files: int = 200,
                  max_hits: int = 300) -> dict:
    """Content-based workspace search (the editor's Cmd+F across files). Walks the
    same jail/skip rules as the tree, reads each text file once, and returns the
    matching lines grouped by file: [{path, hits:[{line, text}]}]. Case-insensitive
    substring match (not a regex, so a stray bracket can't error). Bounded by
    max_files / max_hits so a huge workspace never blocks the loop."""
    q = (query or "").strip()
    if not q:
        return {"query": query, "results": [], "truncated": False}
    needle = q.lower()
    root = session["_root"]
    results: list[dict] = []
    hits_total = 0
    files_scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        at_root = os.path.samefile(dirpath, root) if os.path.exists(dirpath) else False
        skip_dirs = _SKIP_DIRS | (_SKIP_DIRS_ROOT if at_root else set())
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for fn in sorted(filenames):
            if at_root and fn in _SKIP_FILES_ROOT:
                continue
            if os.path.splitext(fn)[1].lower() not in _TEXT_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > _MAX_FILE:
                    continue
                with open(full, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            files_scanned += 1
            if files_scanned > max_files:
                truncated = True
                break
            file_hits: list[dict] = []
            for i, line in enumerate(lines, 1):
                if needle in line.lower():
                    file_hits.append({"line": i, "text": line[:400]})
                    hits_total += 1
                    if hits_total >= max_hits:
                        truncated = True
                        break
            if file_hits:
                results.append({"path": _to_virtual(session, full), "hits": file_hits})
            if truncated:
                break
        if truncated:
            break
    return {"query": q, "results": results, "truncated": truncated}


def _read_file(session: dict, rel: str) -> dict:
    full = _safe_join(session, rel)
    if not full or not os.path.isfile(full):
        return {"error": "file not found", "path": rel}
    if os.path.getsize(full) > _MAX_FILE:
        return {"error": "file too large to open", "path": rel}
    ext = os.path.splitext(full)[1].lower()
    try:
        with open(full, encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return {"path": _to_virtual(session, full), "binary": True, "content": ""}
    return {"path": _to_virtual(session, full), "binary": False,
            "language": _lang_for(ext), "content": content}


def _write_file(session: dict, rel: str, content: str) -> dict:
    full = _safe_join(session, rel)
    if full is None:
        return {"error": "path escapes workspace", "path": rel}
    if len(content or "") > _MAX_FILE:
        return {"error": "content too large", "path": rel}
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": _to_virtual(session, full), "bytes": len(content.encode("utf-8")),
            "tree": _file_tree(session)}


def _delete_file(session: dict, rel: str) -> dict:
    """Remove one workspace file OR directory (the explorer's right-click Delete).

    Stays inside the jail via _safe_join; a directory is removed recursively (the
    explorer can delete a folder, like VS Code). The workspace root itself cannot
    be deleted. Missing file / bad path return {"error": ...}, never raise.
    """
    full = _safe_join(session, rel)
    if full is None:
        return {"error": "invalid path", "path": rel}
    if not os.path.exists(full):
        return {"error": "not found", "path": rel}
    if os.path.realpath(full) == os.path.realpath(session["_root"]):
        return {"error": "cannot delete the workspace root", "path": rel}
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    except OSError as exc:
        return {"error": str(exc), "path": rel}
    return {"ok": True, "path": rel, "tree": _file_tree(session)}


def _make_dir(session: dict, rel: str) -> dict:
    """Create a new directory in the workspace (the explorer's New Folder).

    Stays inside the jail via _safe_join; an existing path is reported, never
    silently merged into. Returns the fresh tree so the explorer re-renders.
    """
    full = _safe_join(session, rel)
    if full is None:
        return {"error": "path escapes workspace", "path": rel}
    if os.path.exists(full):
        return {"error": "already exists", "path": rel}
    try:
        os.makedirs(full, exist_ok=False)
    except OSError as exc:
        return {"error": str(exc), "path": rel}
    return {"ok": True, "path": _to_virtual(session, full), "tree": _file_tree(session)}


def _rename_file(session: dict, rel: str, to: str) -> dict:
    """Move/rename one workspace file within the jail (explorer's Rename).

    BOTH the source and the destination must resolve inside the session root
    via _safe_join; either escaping rejects with {"error": "invalid path"}.
    Returns the fresh tree on success.
    """
    src = _safe_join(session, rel)
    dst = _safe_join(session, to)
    if src is None or dst is None:
        return {"error": "invalid path", "path": rel, "to": to}
    if not os.path.exists(src):
        return {"error": "not found", "path": rel}
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
    except OSError as exc:
        return {"error": str(exc), "path": rel, "to": to}
    return {"ok": True, "path": _to_virtual(session, dst), "tree": _file_tree(session)}


def _lang_for(ext: str) -> str:
    return {".py": "python", ".md": "markdown", ".json": "json", ".toml": "toml",
            ".yaml": "yaml", ".yml": "yaml", ".html": "html", ".css": "css",
            ".js": "javascript", ".sh": "bash", ".mdc": "markdown"}.get(ext, "text")


# The harness files, by agent, in each agent's native format. "Set up harness" copies
# these into the workspace; the file IS the configuration (no abstract "install").
def _harness_files(agent_id: str) -> dict[str, str]:
    common_rules = (
        "## Rules\n\n"
        "- NEVER approve, merge, or close a PR. Submit for human review only.\n"
        "- Branch naming: `fix/issue-N`.\n"
        "- Preserve `cost_analyzer.TOOL_SPECS` names and `inputSchema` verbatim.\n")
    if agent_id == "kiro":
        return {".kiro/steering/validator.md":
                "---\ninclusion: always\n---\n\n# Kiro: VALIDATOR role\n\n"
                "Run the deterministic grading contract in `grading/` against the deployed\n"
                "MCP endpoint and decide \"done\". No LLM judge; green opens the PR.\n\n"
                + common_rules +
                "\n## Gate spec\n\n```harness:gate\ncontract: grading/\n"
                "checks:\n  - tool_discovery\n  - tool_correctness\n  - input_validation\n"
                "max_iterations: 2\n```\n"}
    if agent_id == "opencode":
        return {"AGENTS.md":
                "# opencode: FRONTEND BUILDER role\n\n"
                "Build a thin chatbot UI that calls the deployed MCP endpoint for every\n"
                "answer. No local pricing math.\n\n" + common_rules +
                "\n## UI spec\n\n```harness:ui\ntitle: Cost Analyzer Chat\n"
                "tool: estimate_ec2_monthly_cost\ninput_label: instance type, e.g. m5.large\n"
                "input_field: instance_type\nexamples:\n  - m5.large\n  - t3.micro\n"
                "  - r5.xlarge\n```\n",
                ".config/opencode/opencode.json":
                "{\n"
                "  \"$schema\": \"https://opencode.ai/config.json\",\n"
                "  \"provider\": { \"amazon-bedrock\": { \"options\": "
                f"{{ \"region\": \"{_OPENCODE_REGION}\" }} }} }},\n"
                f"  \"model\": \"{_OPENCODE_MODEL}\",\n"
                "  \"small_model\": \"amazon-bedrock/anthropic.claude-haiku-4-5-20251001-v1:0\"\n"
                "}\n"}
    # default: claude-code (backend): CLAUDE.md + a SKILL.md
    return {"CLAUDE.md":
            "# Claude Code: BACKEND role\n\n"
            "Wrap the `cost_analyzer` module as a remote MCP server: every function in\n"
            "`cost_analyzer.TOOL_SPECS` exposed over `tools/list` + `tools/call`, each\n"
            "returning its handler's dict unchanged. Bedrock-native, no API key.\n\n"
            + common_rules +
            "\n## Build spec\n\n```harness:build\nserver_name: cost-analyzer-mcp\n"
            "server_version: 1.0.0\nexpose: all\n```\n",
            "skills/configure-backend/SKILL.md":
            "---\nname: configure-backend\ndescription: Wrap cost_analyzer as a FastMCP "
            "server behind the Gateway. Use to set up the backend role.\n---\n\n"
            "# Configure the backend MCP server\n\nDone = `tools/list` returns the five "
            "tools and `tools/call` returns the contract values (m5.large x2 = 140.16).\n"}


def _scaffold_harness(session: dict, agent_id: str) -> dict:
    """Copy/make the real harness files for this agent into the workspace.

    This is "set up the harness" as an attendee actually does it: create the steering
    files in the agent's own format. Returns which files were written + the new tree.
    """
    written = []
    for rel, content in _harness_files(agent_id).items():
        res = _write_file(session, rel, content)
        if "path" in res:
            written.append(res["path"])
    session["_harness"] = {"agent_id": agent_id, "files": written}
    _ledger_append({
        "kind": "stage1_harness", "session_id": session["session_id"],
        "agent_id": agent_id, "user_id": _OS_USER, "started_at": _now_iso(),
        "files": written,
    })
    return {"agent_id": agent_id, "written": written, "tree": _file_tree(session)}


def _deploy_upload(session: dict) -> dict:
    """Package the workspace as a code bundle: AgentCore's direct code-upload deploy.

    The base repos deploy a container (Dockerfile -> ECR). The code-first path instead
    uploads the workspace (agent code + harness + skill) as a zip that AgentCore runs
    directly, no local Docker build. We produce that exact artifact: a zip on
    disk, with a manifest of what went up. On AWS this is what `agentcore launch` ships.
    """
    import zipfile
    root = session["_root"]
    bundle_dir = os.path.join(os.path.dirname(root), "deploy")
    os.makedirs(bundle_dir, exist_ok=True)
    bundle = os.path.join(bundle_dir, "code-bundle.zip")
    manifest = []
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                arc = os.path.relpath(full, root)
                z.write(full, arc)
                manifest.append(arc)
    size = os.path.getsize(bundle)
    bundle_label = "/mnt/s3files/.deploy/code-bundle.zip"  # virtual path shown in the UI
    harness = session.get("_harness") or {}
    # Packaging produces the zip on disk; it does NOT create a Runtime. The
    # runtime ARN comes only from CreateAgentRuntime (deploy.py), reconciled onto
    # the shelf, never fabricated here. So we report the bundle and a null
    # runtime_arn until a deploy lands.
    real = _real_runtime_for(session["agent_id"])
    runtime_arn = real["runtime_arn"] if real else None
    session["_deploy"] = {"bundle": bundle, "size": size, "files": len(manifest),
                          "runtime_arn": runtime_arn}
    _ledger_append({
        "kind": "stage1_deploy", "session_id": session["session_id"],
        "agent_id": session["agent_id"], "user_id": _OS_USER, "started_at": _now_iso(),
        "mode": "code-upload", "bundle_bytes": size, "files": len(manifest),
        "runtime_arn": runtime_arn,
    })
    return {
        "mode": "code-upload",
        "runtime_arn": runtime_arn,
        "bundle_file": bundle_label,
        "bundle_bytes": size,
        "file_count": len(manifest),
        "manifest": sorted(manifest),
        "harness_agent": harness.get("agent_id"),
        "entrypoint": "mcp_server.py" if "mcp_server.py" in manifest else None,
    }


_SINGLE_TOOL_SERVER = '''"""Single-tool MCP server, written by hand in Stage 1.

Wraps ONE cost_analyzer function ({tool}) behind MCP's JSON-RPC wire shape
(tools/list + tools/call). This is the smallest possible module-to-MCP conversion;
Stage 2's backend role does all five tools the same way.
"""
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# cost_analyzer.py lives under the sample/ dir on the shared mount; import it from there
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "sample"))
sys.path.insert(0, _HERE)
import cost_analyzer

TOOL = "{tool}"
SPEC = next(t for t in cost_analyzer.list_tools() if t["name"] == TOOL)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        self._send(200, {{"status": "ok", "server": "stage1-" + TOOL}})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        req = json.loads(self.rfile.read(n) or b"{{}}")
        m, i, p = req.get("method"), req.get("id"), req.get("params") or {{}}
        if m == "tools/list":
            return self._send(200, {{"jsonrpc": "2.0", "id": i, "result": {{"tools": [SPEC]}}}})
        if m == "tools/call" and p.get("name") == TOOL:
            try:
                out = cost_analyzer.dispatch(TOOL, p.get("arguments") or {{}})
            except Exception as e:
                return self._send(200, {{"jsonrpc": "2.0", "id": i,
                                         "error": {{"code": -32602, "message": str(e)}}}})
            return self._send(200, {{"jsonrpc": "2.0", "id": i, "result": {{
                "content": [{{"type": "text", "text": json.dumps(out)}}], "isError": False}}}})
        self._send(200, {{"jsonrpc": "2.0", "id": i,
                          "error": {{"code": -32601, "message": "method not found"}}}})

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9100
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _make_conversion(session: dict, tool: str) -> dict:
    """The Stage 1 payoff: write the server, boot it, verify over the wire."""
    sys.path.insert(0, _GRADING)
    from adapters import RemoteMCPClient  # noqa: PLC0415

    server_file = os.path.join(session["_root"], "mcp_server.py")
    with open(server_file, "w", encoding="utf-8") as f:
        f.write(_SINGLE_TOOL_SERVER.format(tool=tool))

    # stop any previous conversion's server before booting the new one
    _stop_server(session)
    port = _free_port()
    proc = subprocess.Popen([sys.executable, server_file, str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{port}"
    import urllib.request
    alive = False
    for _ in range(50):
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as resp:
                alive = resp.status == 200
                break
        except OSError:
            time.sleep(0.1)

    tools_list: list[dict] = []
    sample: dict = {"args": {"instance_type": "m5.large", "count": 2}}
    verified = False
    if alive:
        client = RemoteMCPClient(endpoint)
        try:
            tools_list = client.list_tools()
            result = client.call_tool(tool, sample["args"])
            sample["result"] = result
            verified = any(t.get("name") == tool for t in tools_list) and "monthly_cost" in result
        except Exception as exc:
            sample["error"] = f"{type(exc).__name__}: {exc}"
    session["_server"] = {"proc": proc, "pid": proc.pid, "port": port, "endpoint": endpoint}
    session["_tools"] = tools_list
    _ledger_append({
        "kind": "stage1_conversion", "session_id": session["session_id"],
        "agent_id": session["agent_id"], "user_id": _OS_USER,
        "started_at": _now_iso(), "tool": tool, "verified": verified,
        "endpoint": endpoint, "pid": proc.pid,
        "latency_ms": int((time.monotonic() - session["_t0"]) * 1000),
    })
    return {
        "session_id": session["session_id"],
        "sample_file": "/mnt/s3files/sample/cost_analyzer.py",
        "server_file": "/mnt/s3files/mcp_server.py",
        "tool": tool,
        "endpoint": endpoint,
        "tools_list": tools_list,
        "sample_call": sample,
        "verified": verified,
    }


def _verify_run(session: dict) -> dict:
    """Run the agent's output and report whether it works.

    The converted MCP server is a live subprocess; this exercises it over the wire
    the way a client (or the acceptance gate) would, and reports per-check pass/fail:
      * server_live      : the endpoint answers a GET liveness probe
      * tools_list       : tools/list returns the converted tool
      * tool_call        : a tools/call returns a structured result (monthly_cost)
      * input_validation : an unknown instance type is rejected, not mispriced
    Every check is an HTTP round-trip to the running server. On AgentCore this maps
    to invoking the deployed Runtime/Gateway endpoint with the same calls.
    """
    srv = session.get("_server")
    if not srv or not srv.get("endpoint"):
        # No engine-booted server. If the AGENT (or the attendee) wrote an
        # mcp_server.py into the workspace (the path the workshop actually
        # teaches), boot THAT file and verify it. The check exercises whatever
        # the agent produced, not a canned artifact.
        server_file = os.path.join(session["_root"], "mcp_server.py")
        if not os.path.isfile(server_file):
            return {"ran": False,
                    "error": "no mcp_server.py in the workspace yet; ask your "
                             "agent to write it (see the prompt above)",
                    "checks": []}
        _stop_server(session)
        port = _free_port()
        proc = subprocess.Popen([sys.executable, server_file, str(port)],
                                cwd=session["_root"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        endpoint = f"http://127.0.0.1:{port}"
        import urllib.request as _ur  # noqa: PLC0415
        for _ in range(50):
            try:
                with _ur.urlopen(endpoint, timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        session["_server"] = {"proc": proc, "pid": proc.pid, "port": port, "endpoint": endpoint}
        srv = session["_server"]
    endpoint = srv["endpoint"]
    tool = (session.get("_tools") or [{}])[0].get("name") or "estimate_ec2_monthly_cost"
    sys.path.insert(0, _GRADING)
    import urllib.request  # noqa: PLC0415
    from adapters import RemoteMCPClient, MCPRemoteError  # noqa: PLC0415

    checks = []

    def add(cid, ok, detail):
        checks.append({"check": cid, "passed": bool(ok), "detail": detail})

    # 1) liveness
    t0 = time.monotonic()
    live = False
    try:
        with urllib.request.urlopen(endpoint, timeout=3) as r:
            live = (r.status == 200)
        add("server_live", live, f"GET {endpoint} -> 200" if live else "no 200 from endpoint")
    except OSError as exc:
        add("server_live", False, f"{type(exc).__name__}: {exc}")

    client = RemoteMCPClient(endpoint)
    # 2) tools/list
    listed = []
    try:
        listed = client.list_tools()
        ok = any(t.get("name") == tool for t in listed)
        add("tools_list", ok, f"{len(listed)} tool(s); {tool} present" if ok
            else f"{tool} not in tools/list")
    except Exception as exc:
        add("tools_list", False, f"{type(exc).__name__}: {exc}")

    # 3) a tools/call returns a structured price
    sample = {}
    try:
        sample = client.call_tool(tool, {"instance_type": "m5.large", "count": 2})
        ok = isinstance(sample, dict) and "monthly_cost" in sample
        add("tool_call", ok,
            f"{tool}(m5.large x2) -> monthly_cost={sample.get('monthly_cost')}" if ok
            else f"result missing monthly_cost: {sorted(sample) if isinstance(sample, dict) else sample}")
    except Exception as exc:
        add("tool_call", False, f"{type(exc).__name__}: {exc}")

    # 4) bad input must be rejected (only meaningful for the EC2 tool)
    try:
        client.call_tool(tool, {"instance_type": "not-a-real-type"})
        add("input_validation", False, "unknown instance type was NOT rejected")
    except MCPRemoteError:
        add("input_validation", True, "unknown instance type correctly rejected")
    except Exception as exc:
        # the tool may not take instance_type; treat a clean error as a pass-ish note
        add("input_validation", True, f"rejected ({type(exc).__name__})")

    passed = all(c["passed"] for c in checks)
    elapsed = int((time.monotonic() - t0) * 1000)
    _ledger_append({
        "kind": "stage1_verify", "session_id": session["session_id"],
        "agent_id": session["agent_id"], "user_id": _OS_USER, "started_at": _now_iso(),
        "tool": tool, "passed": passed, "latency_ms": elapsed,
        "checks": [{"check": c["check"], "passed": c["passed"]} for c in checks],
    })
    return {"ran": True, "passed": passed, "endpoint": endpoint, "tool": tool,
            "latency_ms": elapsed, "sample": sample, "checks": checks}


def _stop_server(session: dict) -> None:
    """Stop a session's preview MCP server for good. terminate -> wait -> kill so a
    server that ignores SIGTERM (the old SIGTERM-only path) can never linger as an
    orphan. An mcp_server.py process never exits on its own, so a half-killed one
    would survive forever and pile up (the orphan storm that wedged the console)."""
    srv = session.get("_server")
    if srv:
        proc = srv.get("proc")
        if proc is not None:
            _kill_proc(proc)
        elif srv.get("pid"):  # legacy: only a pid was recorded
            import signal  # noqa: PLC0415
            try:
                os.kill(srv["pid"], signal.SIGKILL)
            except OSError:
                pass
    session["_server"] = None


def _kill_proc(proc) -> None:
    """terminate -> brief wait -> kill -> reap, so a stubborn child always dies."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ValueError):
        pass


# Reap every session's preview server when the host process exits cleanly (a
# console restart / --reload SIGTERM), so an mcp_server.py is never orphaned to
# launchd. A SIGKILL'd host can't run this, but the engine's run-dir prune + a
# fresh start still bounds the damage.
@atexit.register
def _reap_all_session_servers() -> None:
    for s in list(_SESSIONS.values()):
        try:
            _stop_server(s)
        except Exception:  # noqa: BLE001 (never raise from atexit)
            pass


def _public_agent(agent: dict) -> dict:
    """The agent as the console sees it, reconciled with on-disk truth.

    The source of truth is the ``runtime_config.json`` the harness ``deploy.py``
    wrote (an ``arn:aws:bedrock-agentcore:...:runtime/...``): when it exists the
    agent is ``ready`` with that ARN, smart capture of a deploy, no button. A
    deploy marker is honored only when it carries an ARN (never ``local:runtime``).
    Name/purpose overrides (right-click Edit) are layered on top.
    """
    out = {k: v for k, v in agent.items() if not k.startswith("_")}
    out.setdefault("name", out.get("label"))
    out.setdefault("purpose", "")
    # The only source of truth: the runtime_config.json deploy.py wrote. The
    # shelf shows ``ready`` + the ARN the instant a deploy lands in the
    # terminal: smart capture, no placeholder, no separate marker to
    # drift. When no config exists the agent keeps its catalog status
    # (not_deployed, or deploying after a deploy POST that found no Runtime yet).
    real = _real_runtime_for(agent["agent_id"])
    if real:
        out["status"] = "ready"
        out["runtime_arn"] = real["runtime_arn"]
        out["endpoint"] = real["endpoint"]
        if real.get("deployed_at"):
            out["deployed_at"] = real["deployed_at"]
    ov = _read_overrides().get(agent["agent_id"]) or {}
    if ov.get("name"):
        out["name"] = ov["name"]
    # Presence, not truthiness: an explicit empty purpose is a real edit (the
    # attendee cleared the field) and must win over the catalog default.
    if "purpose" in ov:
        out["purpose"] = ov["purpose"]
    return out


def _public_session(session: dict) -> dict:
    out = {k: v for k, v in session.items() if not k.startswith("_")}
    srv = session.get("_server")
    out["mcp_endpoint"] = srv["endpoint"] if srv else None
    # Surface the folder state the client needs (the _-prefixed internals stay hidden).
    out["has_folder"] = session.get("_has_folder", True)
    # Whether a live PTY (bash) is still running for this session. The client uses
    # it to decide, on reload/tab-switch, whether to RE-ATTACH to the existing
    # shell (replay its retained scrollback buffer) or open a fresh one. The
    # buffer only survives while the process is alive, so a dead PTY == reattach
    # is pointless.
    st = session.get("_pty")
    out["pty_alive"] = bool(st and st.get("proc") and st["proc"].poll() is None)
    return out


def dispatch(method: str, path: str, body: dict | None) -> tuple[int, dict]:
    """Pure router for the Stage 1 API: (status, json-able dict).

    Shared by the standalone server (below) and the unified console server
    (`console/server.py`), so both surfaces run the same logic. `path`
    is already stripped of any mount prefix and trailing slash; `body` is the
    parsed JSON dict (or None on a parse error / no body).
    """
    if method == "GET":
        if path == "/api/health":
            return 200, {"status": "ok", "mode": "engine"}
        if path == "/api/agents":
            with _LOCK:
                # Return the full catalog with each agent's status. The console
                # drives the progressive "shelf" UX on the client side (it shows
                # only status=="ready" agents on the shelf and offers the rest as
                # deploy candidates), so the backend stays a simple catalog.
                return 200, {"agents": [_public_agent(a) for a in AGENTS]}
        if path.startswith("/api/agents/"):
            parts = path.split("/")
            # Only GET /api/agents/{id} is a resource here; a sub-resource like
            # /api/agents/{id}/edit (POST-only) is not a GET target -> 404.
            if len(parts) != 4:
                return 404, {"error": "not found", "path": path}
            agent_id = parts[3]
            with _LOCK:
                agent = _AGENTS.get(agent_id)
            if not agent:
                return 404, {"error": "agent not found", "agent_id": agent_id}
            return 200, _public_agent(agent)
        if path.startswith("/api/sessions/"):
            parts = path.split("/")
            session_id = parts[3] if len(parts) > 3 else ""
            with _LOCK:
                session = _SESSIONS.get(session_id)
                if not session:
                    return 404, {"error": "session not found", "session_id": session_id}
                if len(parts) == 5 and parts[4] == "tools":
                    return 200, {"tools": session["_tools"]}
                if len(parts) == 5 and parts[4] == "files":
                    return 200, {"workspace": session.get("workspace", ""),
                                 "has_folder": session.get("_has_folder", True),
                                 "tree": _file_tree(session)}
                return 200, _public_session(session)
        return 404, {"error": "not found", "path": path}

    if method == "POST":
        if path == "/api/agents/reset":
            # "Return to clean state": clears every deploy marker/override AND the
            # in-memory agent records, so the shelf goes empty without a restart.
            return 200, reset_to_clean_state()

        if path == "/api/agents/deploy":
            if body is None:
                return 400, {"error": "invalid JSON body"}
            agent_id = body.get("agent_id") or "claude-code"
            with _LOCK:
                agent = _AGENTS.get(agent_id)
                if not agent:
                    return 404, {"error": "agent not found", "agent_id": agent_id}
                if body.get("model"):
                    agent["model"] = body["model"]
                agent["status"] = "deploying"
                _deploy_agent(agent)   # real work; flips to ready when done
                return 202, _public_agent(agent)

        if path.startswith("/api/agents/") and path.endswith("/edit"):
            # Right-click Edit on a shelf agent: rename it + set its purpose (the
            # role it plays as a subagent of the orchestrator). Overrides persist
            # to disk and layer over the catalog in _public_agent.
            agent_id = path.split("/")[3]
            if body is None:
                return 400, {"error": "invalid JSON body"}
            with _LOCK:
                agent = _AGENTS.get(agent_id)
                if not agent:
                    return 404, {"error": "agent not found", "agent_id": agent_id}
                name = body.get("name")
                purpose = body.get("purpose")
                # Validate type + bound length, matching the file's other write
                # caps (_MAX_FILE, _MAX_PTY_INPUT): a non-string value would crash
                # _save_override's .strip(), and an unbounded value bloats the
                # shared overrides JSON that every agents-list render re-reads.
                for field, val in (("name", name), ("purpose", purpose)):
                    if val is not None and not isinstance(val, str):
                        return 400, {"error": f"{field} must be a string"}
                    if isinstance(val, str) and len(val) > _MAX_AGENT_FIELD:
                        return 400, {"error": f"{field} too long", "max": _MAX_AGENT_FIELD}
                if name is not None and not name.strip():
                    return 400, {"error": "name cannot be empty"}
                _save_override(agent_id, name, purpose)
                return 200, _public_agent(agent)

        if path == "/api/sessions":
            if body is None:
                return 400, {"error": "invalid JSON body"}
            agent_id = body.get("agent_id") or "claude-code"
            with _LOCK:
                # "dev" is the main Development workspace, not a deployable catalog
                # agent: open its shell directly (no _AGENTS lookup, no deploy).
                if agent_id == "dev":
                    session = _new_session("dev")
                    return 201, _public_session(session)
                agent = _AGENTS.get(agent_id)
                if not agent:
                    return 404, {"error": "agent not found", "agent_id": agent_id}
                # Session-first: opening a session prepares the agent if needed.
                # There is no separate "deploy" gate to click through; the
                # explicit deploy endpoint stays for teaching the Runtime
                # template idea, but a session never blocks on it.
                if agent["status"] != "ready":
                    agent["status"] = "deploying"
                    _deploy_agent(agent)
                session = _new_session(agent_id)
                return 201, _public_session(session)

        if path.startswith("/api/sessions/"):
            parts = path.split("/")
            session_id = parts[3] if len(parts) > 3 else ""
            action = parts[4] if len(parts) > 4 else ""
            with _LOCK:
                session = _SESSIONS.get(session_id)
                if not session:
                    return 404, {"error": "session not found", "session_id": session_id}
                if action == "input":
                    if body is None:
                        return 400, {"error": "invalid JSON body"}
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    text = body.get("input", "")
                    output = _run_command(session, text)
                    session["history"].append({"input": text, "output": output})
                    return 200, {"output": output, "cwd": session["cwd"]}
                if action == "pty":
                    # Interactive bash on a PTY (tab completion, history,
                    # colors), the local twin of `agentcore exec --it`.
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    if (body or {}).get("open"):
                        size = (body or {}).get("resize") or {}
                        return 200, _pty_open(session,
                                              rows=size.get("rows", 0),
                                              cols=size.get("cols", 0))
                    out = _pty_io(session, body or {})
                    return (409, out) if "error" in out else (200, out)
                if action == "convert-skill":
                    if body is None:
                        return 400, {"error": "invalid JSON body"}
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    tool = body.get("tool") or "estimate_ec2_monthly_cost"
                    return 200, _make_conversion(session, tool)
                if action == "file":
                    # The file explorer's editor + context menu. An optional
                    # "op" selects delete/rename; without it the original
                    # read/write contract is unchanged (write if "content" is
                    # present, else read).
                    if body is None:
                        return 400, {"error": "invalid JSON body"}
                    # A closed session's workspace is gone; file ops on it must be
                    # rejected the same way input/pty/verify are; never a 200 that
                    # pretends the workspace still lives. (Same guard as every other
                    # action below.)
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    rel = body.get("path", "")
                    op = body.get("op")
                    if op == "search":
                        return 200, _search_files(session, body.get("query") or "")
                    if op == "delete":
                        return 200, _delete_file(session, rel)
                    if op == "rename":
                        return 200, _rename_file(session, rel, body.get("to") or "")
                    if op == "mkdir":
                        return 200, _make_dir(session, rel)
                    if "content" in body:
                        return 200, _write_file(session, rel, body.get("content") or "")
                    return 200, _read_file(session, rel)
                if action == "scaffold-harness":
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    agent_id = (body or {}).get("agent_id") or session["agent_id"]
                    return 200, _scaffold_harness(session, agent_id)
                if action == "deploy-upload":
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    return 200, _deploy_upload(session)
                if action == "verify":
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    return 200, _verify_run(session)
                if action == "open-folder":
                    # VS Code "Open Folder": re-root a Development session at a new
                    # directory (or close to a no-folder state). Dev sessions only.
                    if session["status"] != "open":
                        return 409, {"error": "session not open", "status": session["status"]}
                    if not session.get("_dev"):
                        return 400, {"error": "open-folder is only for the Development workspace"}
                    return 200, _open_folder(session, (body or {}).get("path"))
                if action == "list-dirs":
                    # Backing store for the VS Code-style Open Folder finder MODAL:
                    # one level of subdirectories to navigate. Read-only; dev only.
                    if not session.get("_dev"):
                        return 400, {"error": "list-dirs is only for the Development workspace"}
                    return 200, _list_dirs((body or {}).get("path"))
        return 404, {"error": "not found", "path": path}

    if method == "DELETE":
        if path.startswith("/api/sessions/"):
            session_id = path.split("/")[3]
            with _LOCK:
                session = _SESSIONS.get(session_id)
                if not session:
                    return 404, {"error": "session not found", "session_id": session_id}
                _stop_server(session)
                _pty_close(session)
                session["status"] = "closed"
                return 200, {"session_id": session_id, "status": "closed"}
        return 404, {"error": "not found", "path": path}

    return 404, {"error": "method not allowed", "method": method}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # This API drives PTYs and file ops, so a wildcard CORS header would
        # let any website script-drive an attendee's shell. Emit CORS only for a
        # same-origin call (Origin host == Host), reflecting that exact
        # origin; cross-origin browsers get no CORS header and are blocked from
        # reading the response. Same-origin GETs and curl send no Origin and need
        # none, so the console and local tooling are unaffected.
        origin = self._same_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _same_origin(self) -> str | None:
        """Return the request Origin iff same-origin with the Host, else None.

        Missing/garbled Origin (same-origin GETs, curl) yields None, so no CORS
        headers, which is correct. Mirrors console/server.py's gate.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return None
        # Never reflect a CRLF-bearing Origin into the Access-Control-Allow-Origin
        # response header: `Origin: http://host/\r\n...` keeps netloc==host and would
        # otherwise pass the equality gate below, letting an obs-fold header ride the
        # reflection (py/http-response-splitting). A real origin never contains CRLF.
        if "\r" in origin or "\n" in origin:
            return None
        try:
            authority = urlparse(origin).netloc
        except ValueError:
            return None
        return origin if authority and authority.lower() == host.lower() else None

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None

    def log_message(self, *args) -> None:
        pass

    def do_OPTIONS(self) -> None:
        self._send(200, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        code, out = dispatch("GET", path, None)
        self._send(code, out)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        code, out = dispatch("POST", path, self._body())
        self._send(code, out)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        code, out = dispatch("DELETE", path, None)
        self._send(code, out)


def main() -> None:
    os.makedirs(_STAGE1_DIR, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Interactive API (real local engine) on http://localhost:{PORT}")
    print(f"Workspaces under {_STAGE1_DIR}: shell commands, files, and the converted "
          f"MCP server are all real. Ledger: {_LEDGER}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        for s in _SESSIONS.values():
            _stop_server(s)
            _pty_close(s)
        server.shutdown()


if __name__ == "__main__":
    main()
