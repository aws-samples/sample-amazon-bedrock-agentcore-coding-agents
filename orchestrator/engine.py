"""The embedded orchestration engine: routed, reviewed, and terminal-transparent.

Production orchestrators implement this as a durable function (checkpoint/replay,
suspension, condition-based polling). This module is the same design, embedded:
one in-process engine that drives every task through the same deterministic state
machine. It is real-only: each role's artifact is produced by dispatching that role
to its DEPLOYED AgentCore Runtime. There is no local, in-process, or model-in-process
producer on the shipped path, and a missing wired runtime fails loud rather than
silently building locally. The producer sits behind the execution seam
(``executor.Executor``): ``AgentCoreExecutor`` (the shipped default,
``InvokeAgentRuntime`` / command-shell dispatch against deployed role runtimes).

Three design decisions define this engine:

  * **Model-selected tools with a deterministic floor.** ``chat.py`` lets the
    Strands coordinator clarify an ambiguous request and choose dispatch tools.
    ``router.py`` provides the versioned registry and advisory route ladder used
    by ``run_build``. Only selected roles are dispatched.
  * **A separate reviewer whose verdict lands on the PR** (``reviewer.py``). The build
    side never approves its own work: finalization runs the validator-authored
    acceptance test (a real execution, real exit code), opens or updates the pull
    request, and the judge posts an Assessment comment ON that PR: approve
    (closing with the exact pass token ``LGTM: no changes needed``) or request
    changes, which loops the routed roles through one bounded re-implement pass
    that updates the same PR.
  * **A real PR at the end** (``github.py``). When the attendee connects GitHub,
    the composed run branch is pushed to their fork and the PR opens with the
    critique report. Without credentials the PR field carries a typed error and
    ``pr_url`` stays null. A local diagnostic branch is never presented as a PR.

Every role works in its own container directory and leaves a TERMINAL TRANSCRIPT:
``/bin/sh`` commands with their output (installing its harness by writing the
steering file, probing the module, booting the server, running the gate), plus, on
the dispatched role, the live CLI session that ran INSIDE the deployed Runtime,
read back over the command shell. The console streams these transcripts into
per-role xterm panes: what you watch is what ran.

How a role's artifact is produced (the step behind the execution seam):

  * **AgentCore Runtime (shipped, real-only)**: each role's coding-agent CLI runs
    INSIDE its deployed Runtime: Claude Code (``claude``) writes ``mcp_server.py``,
    opencode (``opencode``) writes ``chatbot.html``, and the Claude Code validator
    (``claude``) writes the validation report. The engine dispatches over the command shell
    (``runtime_exec`` via ``engine._runtime_cli``) against the role's WIRED runtime
    ARN, reads back the artifact the CLI wrote, and the gate grades THAT file. A
    role with no wired runtime fails loud; there is no local fallback.

The executor is selected at startup from ``WORKSHOP_EXECUTOR`` (default / ``""`` /
``agentcore`` -> ``AgentCoreExecutor``; unknown values fail loud). Deterministic
OFFLINE TESTS inject a test-only ``FixtureExecutor`` (``fixture_executor.py``) by
constructor: it runs the role closures in-process and routes the PRODUCE step to
the deterministic builders (no model, no live AWS), so the gate / reviewer / compose
/ PR tail is exercised without a deployed runtime. No env flag selects a fake on the
shipped binary, and no shipped module imports the fixture.

Run it (always via the HTTP shell, ``connection_api.py``):
    python3 orchestrator/connection_api.py
"""

from __future__ import annotations

import atexit
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# --- repo paths (engine is path-aware so it runs from any CWD) -----------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
# Wirable so tests isolate all run state (compose repo, ledger) to a tmp dir and
# agree with github.py's _RUNS_DIR: they share the composed repo the PR is pushed
# from, so they MUST resolve to the same place. Defaults to the repo's .runs.
_RUNS_DIR = os.environ.get("WORKSHOP_RUNS_DIR", os.path.join(_REPO, ".runs"))
_LEDGER = os.path.join(_RUNS_DIR, "telemetry.jsonl")

# Cap how many per-run build dirs accumulate under .runs/work. Each run leaves a
# real build tree (role artifacts, composed checkout) on disk; unbounded they grow
# into gigabytes. Keep the most-recent N (by mtime) and prune older ones when a new
# run is submitted. Override with WORKSHOP_MAX_WORK_DIRS. The telemetry ledger and
# the shared composed-git repo are NOT under work/, so they are never touched.
_MAX_WORK_DIRS = int(os.environ.get("WORKSHOP_MAX_WORK_DIRS", "40"))


def _prune_work_dirs(keep: int) -> None:
    """Keep the `keep` most-recently-modified run dirs under .runs/work, deleting
    the older ones. Best-effort: an entry that can't be removed is skipped. Only
    ``run_*`` dirs are eligible, so nothing else under work/ is disturbed."""
    if keep < 0:
        return
    import shutil  # noqa: PLC0415 (local, only needed on prune)
    work_root = os.path.join(_RUNS_DIR, "work")
    try:
        entries = [
            os.path.join(work_root, name)
            for name in os.listdir(work_root)
            if name.startswith("run_") and os.path.isdir(os.path.join(work_root, name))
        ]
    except OSError:
        return
    if len(entries) <= keep:
        return
    entries.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    for stale in entries[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


# --- replay-server pool: bound the passed-run MCP servers so they never leak ---
# A PASSED run keeps its built MCP server alive so the produced UI can be replayed
# against the live endpoint. An MCP server process runs FOREVER (it never exits),
# so left unbounded every passing run orphans a python child; over a workshop /
# capture session they pile into the thousands and exhaust the box (the recurring
# "console blocks and dies", traced to 1.6k orphaned mcp_server.py). So passed-run
# servers live in a BOUNDED pool: at most _MAX_REPLAY_SERVERS survive (oldest
# evicted first), any idle past _REPLAY_TTL_S is reaped, and atexit reaps them all
# so a killed / --reload'd process never leaves an orphan behind.
_MAX_REPLAY_SERVERS = int(os.environ.get("WORKSHOP_MAX_REPLAY_SERVERS", "6"))
_REPLAY_TTL_S = float(os.environ.get("WORKSHOP_REPLAY_TTL_S", "900"))  # 15 min


def _kill_proc(proc: subprocess.Popen | None) -> None:
    """Stop a child for good: SIGTERM, a brief wait, then SIGKILL + reap. An MCP
    server that ignores SIGTERM (or is wedged in a syscall) still dies, so it can
    never linger as an orphan. A bare ``.terminate()`` (the old _stop_server) left
    a stubborn child alive; this always collects it."""
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


class _ReplayPool:
    """Thread-safe, bounded registry of passed-run replay servers. The hard
    guarantee is the CAP (absolute live count), enforced on every register; the
    TTL sweep is an opportunistic nicety on top."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, tuple[subprocess.Popen, float]] = {}

    def register(self, run_id: str, proc: subprocess.Popen) -> None:
        with self._lock:
            old = self._procs.pop(run_id, None)
            self._procs[run_id] = (proc, time.monotonic())
            victims = self._evict_locked()
        _kill_proc(old[0]) if old else None
        for p in victims:
            _kill_proc(p)

    def _evict_locked(self) -> list[subprocess.Popen]:
        # Drop entries whose process already died, then evict oldest over cap.
        for rid in [r for r, (p, _) in self._procs.items() if p.poll() is not None]:
            self._procs.pop(rid, None)
        victims: list[subprocess.Popen] = []
        while len(self._procs) > _MAX_REPLAY_SERVERS:
            oldest = min(self._procs.items(), key=lambda kv: kv[1][1])[0]
            victims.append(self._procs.pop(oldest)[0])
        return victims

    def reap_idle(self, ttl: float | None = None) -> int:
        ttl = _REPLAY_TTL_S if ttl is None else ttl
        now = time.monotonic()
        with self._lock:
            stale = [r for r, (p, t) in self._procs.items()
                     if now - t >= ttl or p.poll() is not None]
            procs = [self._procs.pop(r)[0] for r in stale]
        for p in procs:
            _kill_proc(p)
        return len(procs)

    def drop(self, run_id: str) -> None:
        with self._lock:
            entry = self._procs.pop(run_id, None)
        if entry:
            _kill_proc(entry[0])

    def reap_all(self) -> None:
        with self._lock:
            procs = [p for p, _ in self._procs.values()]
            self._procs.clear()
        for p in procs:
            _kill_proc(p)

    def count(self) -> int:
        with self._lock:
            return len(self._procs)


_REPLAY = _ReplayPool()
atexit.register(_REPLAY.reap_all)  # never orphan a replay server on process exit


def _orphan_pids_to_reap(ps_output: str, runs_dir: str) -> list[int]:
    """Pure decision half of the boot sweep (no killing), so the exact predicate is
    unit-testable against synthetic `ps` output. Selects pids of `mcp_server.py`
    processes scoped to ``runs_dir`` whose PARENT IS DEAD (ppid==1 -> re-parented to
    init). An orphan's parent is gone BY DEFINITION, so the ppid==1 gate can never
    select a LIVE console's children (they still have a live parent)."""
    # Match the configured and resolved runs path, so a symlinked or relative
    # WORKSHOP_RUNS_DIR still scopes the sweep (the engine spawns with an absolute
    # server_file, so the absolute form is what normally appears in ps).
    markers = {os.path.realpath(runs_dir), os.path.abspath(runs_dir), runs_dir}
    pids: list[int] = []
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if "mcp_server.py" not in cmd:
            continue
        if not any(m in cmd for m in markers):  # scope: only OUR runs dir
            continue
        if ppid_s != "1":          # parent still alive -> NOT an orphan, leave it
            continue
        try:
            pids.append(int(pid_s))
        except ValueError:
            pass
    return pids


def _reap_orphaned_servers() -> None:
    """Boot-time backstop: kill any `mcp_server.py` under our runs dir whose parent
    is dead. atexit reaps a CLEAN exit, but a SIGKILL'd host (a hard `kill -9` on
    the console, an e2e teardown that times out) runs no cleanup and orphans its
    children to launchd. Those orphans piled up and wedged the box. Best-effort and
    silent: a platform without this `ps` shape, or a process that vanishes
    mid-sweep, is just skipped."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    import signal  # noqa: PLC0415
    for pid in _orphan_pids_to_reap(out, _RUNS_DIR):
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass


_reap_orphaned_servers()  # sweep leftovers from a prior hard-killed host on boot

sys.path.insert(0, _HERE)
import builders  # noqa: E402
import executor  # noqa: E402
import github  # noqa: E402
import llm  # noqa: E402  (model-id alias resolution for the runtime dispatch)
import policy  # noqa: E402  (the guardrail every role command is screened against)
import reviewer  # noqa: E402
import router  # noqa: E402

# Frozen contract enums (API_CONTRACT.md): the engine's public vocabulary.
PHASES = ["admission", "context_hydration", "pre_flight", "agent_execution", "finalization"]
TERMINAL = {"passed", "failed", "needs_human"}

# Bounded iteration, then a human. The bound's source of truth is the review
# orchestrator's MAX_REVIEW_ROUNDS (one re-implement pass): the cap is the
# initial build round plus that many re-implement rounds.
MAX_ITERATIONS = 1 + reviewer.MAX_REVIEW_ROUNDS

# Per-role CLI hard timeout (a single coding-agent CLI dispatch inside its deployed
# Runtime). AGENT_EXECUTION_TIMEOUT_S (below) is the outer net; this kills one
# wedged CLI tree.
HARNESS_ROLE_TIMEOUT_S = int(os.environ.get("HARNESS_ROLE_TIMEOUT_S", "600"))

# Bounds for the per-role structured event feed (run.role_events): a chatty agent
# must not grow the in-memory run record without limit. Long bodies are truncated
# to _EVENT_TEXT_CAP chars; the feed is capped at _ROLE_EVENT_CAP events with a
# single visible marker once the cap is hit (never a silent drop).
_EVENT_TEXT_CAP = 4000
_ROLE_EVENT_CAP = 200

# Single fixed budget for the one agentic phase. A role dispatched to its deployed
# AgentCore Runtime drives a real CLI over the command shell; the per-role hard
# timeout (HARNESS_ROLE_TIMEOUT_S) is the inner net, this is the outer one.
AGENT_EXECUTION_TIMEOUT_S = 1800

# The shipped path produces artifacts ONLY by dispatching each role to its deployed
# AgentCore Runtime (AgentCoreExecutor + engine._runtime_cli); there is no local,
# in-process, or model-in-process producer. A run with no executor that can produce
# artifacts fails loud here, never a silent local build. (Deterministic offline
# tests inject the test-only FixtureExecutor, which routes the produce step to the
# builders; that is the only other producer and it lives in test-support code.)
_NO_PRODUCER_ERROR = (
    "NO_PRODUCER: the shipped orchestrator is real-only; it dispatches each role "
    "to its deployed AgentCore Runtime (WORKSHOP_EXECUTOR=agentcore with the role "
    "runtimes wired). There is no local/in-process artifact producer; wire the "
    "runtimes (Settings or AGENTCORE_RUNTIME_<ROLE>) so dispatch is real.")


# Terminal DISPLAY scrubbing. Commands EXECUTE with real absolute paths (the engine
# runs them locally on this box), but the transcript the console renders must read
# like the attendee's runtime: the clone root shows as ``~/<clone dirname>`` and the
# home dir as ``~``, never a build box's ``/Users/.../workspaces/...`` or
# ``/home/ubuntu`` path. Longest paths first so a nested match (repo root under home)
# wins over its prefix.
def _display_scrub(text: str) -> str:
    if not text:
        return text
    # sys.executable FIRST: it is an absolute interpreter path that usually lives
    # UNDER home, so scrubbing home first would leave a "~/.pyenv/.../python3" that
    # no longer matches. Show it as the plain "python3" the attendee runs.
    if sys.executable:
        text = text.replace(sys.executable, "python3")
    repo_root = os.environ.get(
        "WORKSHOP_REPO_ROOT",
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    home = os.path.expanduser("~")
    # The clone label follows the repo-root basename (a plain `git clone` of the
    # public repo yields ~/<repo name>), so the transcript matches the path the
    # content's `cd ~/<name>` targets regardless of where the box cloned it.
    clone_label = "~/" + os.path.basename(os.path.normpath(repo_root)) if repo_root else "~"
    for real, shown in sorted(
            ((repo_root, clone_label), (home, "~")), key=lambda p: -len(p[0])):
        if real and real != "/":
            text = text.replace(real, shown)
    return text


# --- Resilience constants (in-process analogues of production durability) ----
# A role thread still "working" after the phase deadline is WEDGED, not slow:
# treat it as a timeout failure rather than letting the run finalize a half-built
# artifact. The phase deadline is the ONLY liveness authority; each role's
# last_beat timestamp (touched per terminal line) is display-only; it lets the
# failure note say how long the role was silent, it never gates a kill.
# A crashed/hung compose under a bare lock would wedge every concurrent run
# forever; the lease auto-releases so a dead holder never deadlocks the engine.
COMPOSE_LEASE_STUCK_S = 90
# The reconcile() sweeper force-fails runs whose phase deadline has elapsed but
# whose status is still non-terminal (a stranded-task reconciler). Wider than the
# agent_execution budget so a sweep never kills a live run.
STRANDED_AFTER_S = 600

# Two-bucket terminal model (reconciler-recoverable vs hard preflight reject).
# PERMANENT reasons mean "resubmitting won't help" -> status=failed.
# Everything else transient -> status=needs_human (a human can resume).
PERMANENT_FAIL_REASONS = {
    "EMPTY_TASK", "NO_ROUTE", "UNKNOWN_AGENT", "UNKNOWN_WORKFLOW", "UNKNOWN_USECASE",
    "HARNESS_MISSING", "GRADING_CONTRACT_MISSING", "SKILL_IMPORT_FAILED",
    "NO_RUN_TO_REVIEW", "BACKEND_HARNESS_MISSING",
    "FRONTEND_HARNESS_MISSING",
}


def _is_permanent(reason: str | None) -> bool:
    """True if a fail reason is deterministic (resubmit won't help)."""
    if not reason:
        return False
    head = reason.split(":", 1)[0]
    return head in PERMANENT_FAIL_REASONS


class _Lease:
    """A self-healing mutex: like threading.Lock, but a holder that dies or hangs
    past ``stuck_after_s`` is force-evicted so the resource never deadlocks.

    Used for the shared composed-git repo (one writer at a time) so a crashed
    compose can never wedge every other concurrent run.
    """

    def __init__(self, stuck_after_s: float):
        self._stuck_after_s = stuck_after_s
        self._cv = threading.Condition()
        self._owner: str | None = None
        self._since: float = 0.0
        self.steals = 0  # observability: how often a stuck holder was evicted

    def acquire(self, owner: str) -> None:
        with self._cv:
            while self._owner is not None:
                if time.monotonic() - self._since >= self._stuck_after_s:
                    self.steals += 1
                    self._owner = None  # force-release a wedged holder
                    break
                self._cv.wait(timeout=self._stuck_after_s)
            self._owner, self._since = owner, time.monotonic()

    def release(self, owner: str) -> None:
        with self._cv:
            if self._owner == owner:        # a stolen lease is no longer ours: no-op
                self._owner, self._since = None, 0.0
                self._cv.notify_all()

AGENTS = [
    {"id": "claude-code", "label": "Claude Code", "default_role": "backend-mcp",
     "model": "us.anthropic.claude-opus-4-6-v1", "credential": "bedrock-native",
     # How this agent is steered: each harness reads a different file format.
     # local_steering_path is the REAL file the local engine reads and builds from
     # (relative to orchestrator/); steering_path is the AgentCore deploy location.
     "harness": {
         "steering_format": "CLAUDE.md",
         "steering_path": "coding-agents/claude-code/CLAUDE.md",
         "local_steering_path": "harness/claude-code/CLAUDE.md",
         "skills": ["configure-claude-code-backend", "harness-setup"],
         "install": "cd coding-agents/claude-code && ./setup.sh && python deploy.py",
     }},
    {"id": "claude-code-validator", "label": "Claude Code", "default_role": "validator",
     "model": "us.anthropic.claude-opus-4-6-v1", "credential": "bedrock-native",
     # The validator is a SECOND Claude Code, steered by an acceptance-contract
     # CLAUDE.md (carrying the ```harness:gate``` block) instead of Kiro's
     # .kiro/steering. Bedrock-native, so it needs no API key and no Token Vault.
     "harness": {
         "steering_format": "CLAUDE.md",
         "steering_path": "coding-agents/claude-code-validator/CLAUDE.md",
         "local_steering_path": "harness/claude-code-validator/CLAUDE.md",
         "skills": ["configure-claude-code-validator"],
         "install": "cd coding-agents/claude-code-validator && ./setup.sh && python deploy.py",
     }},
    {"id": "opencode", "label": "opencode", "default_role": "frontend-builder",
     "model": "amazon-bedrock/us.anthropic.claude-sonnet-4-6", "credential": "runtime-iam",
     "harness": {
         "steering_format": "AGENTS.md",
         "steering_path": "coding-agents/opencode/AGENTS.md",
         "local_steering_path": "harness/opencode/AGENTS.md",
         "skills": ["configure-opencode-frontend", "vercel-deploy"],
         "install": "cd coding-agents/opencode && ./setup.sh && python deploy.py",
     }},
]
ROLE_BY_AGENT = {a["id"]: a["default_role"] for a in AGENTS}

# The agent id that plays the validator role. It is a second Claude Code
# (steered by an acceptance-contract CLAUDE.md) since Kiro was retired from the
# roster; keeping it in one constant makes the validator dispatch path readable
# and the swap easy to audit.
_VALIDATOR_AGENT = "claude-code-validator"

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _py(snippet: str) -> str:
    """One-liner python for terminal transcripts (kept readable in the pane)."""
    return f"python3 -c {json.dumps(snippet)}"


@dataclass
class RoleResult:
    agent: str
    role: str
    state: str = "pending"          # pending | working | done | error
    latency_ms: int = 0             # wall-clock for the role's work
    note: str = ""
    tokens: int = 0                 # the role's own reported usage (0 = none reported)
    cost_usd: float = 0.0           # real tokens priced at published rates (0 when none)
    estimated: bool = False         # usage is measured or honestly zero, never inferred
    # How this role's artifact was produced: "agentcore" (its CLI ran inside the
    # deployed Runtime). Left "" where it carries no extra information (the
    # deterministic test fixture).
    engine: str = ""
    # The exact Runtime target and session used by the shipped AgentCore path.
    # Metrics persists these values so a later StopRuntimeSession never guesses
    # from the currently configured fleet.
    runtime_arn: str | None = None
    runtime_session_id: str | None = None
    # liveness heartbeat: monotonic ts of this role's last observable progress
    # (a run.term() line). A role still "working" with a stale beat is WEDGED,
    # which the join-watchdog distinguishes from merely slow.
    last_beat: float = 0.0


@dataclass
class Run:
    run_id: str
    task: str
    agents: list[str]
    roles: dict[str, str]
    options: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"          # queued | running | passed | failed | needs_human
    phase: str = "admission"
    created_at: str = ""
    iterations: int = 0
    fail_reason: str | None = None
    progress: dict[str, RoleResult] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    gate: dict | None = None
    route: dict | None = None              # the router's verdict (workflow_ref, rule, ...)
    usecase: str = "sample-to-mcp"
    review: dict | None = None             # the review orchestrator's verdict
    pr: dict | None = None                 # github finalization result ({pr_url} | {skipped} | {error})
    compose_base: dict | None = None       # external-repo compose base ({mode: external|local, ...})
    terminals: dict[str, list[dict]] = field(default_factory=dict)  # per-role shell transcript
    # Per-role STRUCTURED agent events (text/thinking/tool_use/tool_result), in
    # arrival order, parsed from each role's real CLI event stream. This is what
    # the console renders as live tool calls + reasoning (not the raw transcript).
    role_events: dict[str, list[dict]] = field(default_factory=dict)
    pr_url: str | None = None              # real PR when GitHub is connected; null locally
    merge_state: str | None = None         # auto-merge outcome: merged | skipped:... | error:... | null
    user_identity: dict = field(default_factory=dict)  # Cognito baggage: {user_id, user_email, user_name}
    composed_branch: str | None = None     # real local git branch holding the composed change
    composed_commit: str | None = None     # real commit sha of the composed artifacts
    artifact_endpoint: str | None = None
    _server_proc: subprocess.Popen | None = None
    _server_file: str | None = None        # the GENERATED mcp_server.py for this run
    _chatbot_file: str | None = None       # the GENERATED ui entry point (ui/index.html)
    _ui_dir: str | None = None             # the GENERATED ui/ project dir (multi-file)
    # Loop-engineering: the validator AUTHORS its own acceptance test against the
    # live endpoint each run (Compartment-2 generate-verify), rather than running a
    # pinned contract. The engine runs THIS file and reads its real exit code, so
    # the fail-loud spine holds (real execution, never a fabricated pass). Null on the
    # fixture/offline path, which keeps the shipped grading contract as its floor.
    _acceptance_test_file: str | None = None
    _explicit_agents: bool = False
    _workflow_ref_req: str | None = None
    _review_target: str | None = None      # run_id under review (review/pr-v1 only)
    # Which executor drives this run ("agentcore" shipped | "fixture" test). It
    # decides WHERE a role's coding-agent CLI runs, and therefore what belongs in
    # the per-agent terminal: on the shipped path the agent's terminal is its REAL
    # AgentCore Runtime session only (written by _runtime_cli), and the engine's own
    # host-side plumbing (harness staging, module probes, the acceptance gate) is
    # recorded under a separate ``orchestrator`` lane, never mixed into the agent tab.
    _executor_name: str = "fixture"
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def workdir(self) -> str:
        """Per-run build directory where this run's role artifacts are generated."""
        return os.path.join(_RUNS_DIR, "work", self.run_id)

    def roledir(self, agent: str) -> str:
        """The role's own container directory: its /mnt/workspace equivalent."""
        d = os.path.join(self.workdir, f"role-{agent}")
        os.makedirs(d, exist_ok=True)
        return d

    def _term_lane(self, agent: str) -> str:
        """Which terminal lane a ``term()`` transcript is recorded under.

        ``term()`` runs the engine's OWN host-side plumbing on the orchestrator box
        (harness staging, module probes, the acceptance gate, liveness echoes) --
        it is NOT the coding agent's session. On the shipped path (``agentcore``) the
        agent's own terminal is its real AgentCore Runtime shell session, written by
        ``_runtime_cli``; mixing host ``ls``/``cp`` staging into that tab would be a
        false picture of what ran in the runtime. So on the shipped path this
        plumbing is recorded under a dedicated ``orchestrator`` lane, keeping each
        agent tab session-only. The test-only ``fixture`` executor has no runtime
        session (it builds in-process), so there ``term()`` is the only window and it
        stays under the agent -- preserving the offline tests' terminal contract.
        """
        return agent if self._executor_name == "fixture" else "orchestrator"

    def term(self, agent: str, cmd: str, cwd: str | None = None) -> str:
        """Run a shell command in the role's container dir; record the transcript.

        This is the engine's host-side window: every plumbing step (harness staging,
        module probes, the acceptance gate) is a ``/bin/sh`` invocation on the
        orchestrator box. On the shipped path its transcript lands in the
        ``orchestrator`` lane (see ``_term_lane``), never in the agent's own terminal
        tab, which shows only the agent's real AgentCore Runtime session.

        Every command is first SCREENED against the harness guardrails
        (``policy.screen``, the same list the Governance page shows): a hard-denied
        or human-gated command (``rm -rf /``, a write under ``.git/``, a force-push
        to main) is NOT executed; the block is recorded as a transcript line with the
        matched rule id, so the page's advertised rules are enforced at the engine's
        command boundary.
        """
        lane = self._term_lane(agent)
        verdict = policy.screen("run_command", cmd,
                                read_only=bool(self.route and self.route.get("read_only")))
        if not verdict.allowed:
            with self._lock:
                self.terminals.setdefault(lane, []).append({
                    "cmd": _display_scrub(cmd),
                    "output": (f"POLICY_DENIED [{verdict.rule_id}]: {verdict.reason}. "
                               f"Blocked by the {verdict.tier} guardrail; command not run."),
                    "exit": 126, "elapsed_s": 0.0,
                })
            self.log(f"{agent} command blocked by policy [{verdict.rule_id}]: {cmd[:80]}",
                     "warn")
            return ""
        t0 = time.monotonic()
        try:
            proc = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True,
                                  text=True, cwd=cwd or self.roledir(agent), timeout=60)
            out, code = (proc.stdout + proc.stderr).strip(), proc.returncode
        except subprocess.TimeoutExpired:
            out, code = "(timed out after 60s)", 124
        with self._lock:
            self.terminals.setdefault(lane, []).append({
                "cmd": _display_scrub(cmd), "output": _display_scrub(out[:4000]),
                "exit": code, "elapsed_s": round(time.monotonic() - t0, 2),
            })
            role = self.progress.get(agent)
            if role is not None:            # liveness beat: this role is still alive
                role.last_beat = time.monotonic()
        return out

    def add_event(self, agent: str, event: dict) -> None:
        """Append a STRUCTURED agent event (text/thinking/tool_use/tool_result)
        to this role's live feed, under the lock, and beat the role heartbeat.

        Bounded so one chatty role can't grow the run record without limit: long
        text/result bodies are truncated and the list is capped, with a single
        marker event recorded once the cap is hit (never silently dropped)."""
        ev = dict(event)
        if isinstance(ev.get("text"), str) and len(ev["text"]) > _EVENT_TEXT_CAP:
            ev["text"] = ev["text"][:_EVENT_TEXT_CAP] + " …(truncated)"
        with self._lock:
            feed = self.role_events.setdefault(agent, [])
            if len(feed) < _ROLE_EVENT_CAP:
                feed.append(ev)
            elif len(feed) == _ROLE_EVENT_CAP:
                feed.append({"kind": "text",
                             "text": f"…(event feed capped at {_ROLE_EVENT_CAP})"})
            role = self.progress.get(agent)
            if role is not None:
                role.last_beat = time.monotonic()

    def transition(self, to_status: str, *expected: str,
                   reason: str | None = None) -> bool:
        """Compare-and-swap the run status under the lock: write ``to_status`` only
        if the current status is one of ``expected`` (or ``expected`` is empty).

        This is an idempotency guard so a reconciler sweep and the worker thread can
        never double-transition the same run. Returns False (a no-op) if someone else
        already advanced it, exactly like the ConditionalCheckFailed branch.
        """
        with self._lock:
            if expected and self.status not in expected:
                return False
            self.status = to_status
            if reason is not None:
                self.fail_reason = reason
            return True

    def log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self.events.append({
                "seq": len(self.events) + 1,
                "elapsed_s": round(time.monotonic() - self._t0, 2),
                "phase": self.phase,
                "level": level,
                "message": message,
            })

    # set at submit; monotonic so the journal is wall-clock independent
    _t0: float = 0.0


class Engine:
    """Drives every run to a terminal state: the orchestrator's core guarantee.

    One worker thread per run (the local stand-in for a durable execution).
    A crashed role or a red gate never strands a run: the engine owns the
    transitions, so every run ends passed / failed / needs_human.
    """

    def __init__(self, max_concurrent: int = 3,
                 executor_obj: Any | None = None):
        self.max_concurrent = max_concurrent
        # The execution SEAM (executor.py): which producer makes each role's
        # artifact. Default (shipped, real-only) = AgentCoreExecutor, which
        # dispatches each role to its DEPLOYED Runtime and fails loud on a missing
        # wired ARN. Deterministic offline tests inject the test-only
        # FixtureExecutor by constructor (builders, no model, no live AWS). The
        # verdict path (boot + acceptance gate + reviewer + compose + PR) is identical
        # regardless of which executor produced the artifact.
        self.executor = executor_obj if executor_obj is not None else executor.from_env()
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._counter = 0
        # short per-engine prefix so run ids stay unique across restarts
        # (the ledger is append-only and outlives any single engine process)
        self._epoch = time.strftime("%H%M%S", time.gmtime())
        self._engine_log(f"executor: {self.executor.name}")

    # ---------------------------------------------------------------- submit
    def submit(self, task: str, agents: list[str] | None = None,
               options: dict | None = None, workflow_ref: str | None = None) -> Run:
        # Cap the work-dir pile before this run adds its own, so .runs/work can't
        # grow without bound across a long workshop / many runs.
        _prune_work_dirs(_MAX_WORK_DIRS)
        # Capture the calling user's identity for audit and cost attribution.
        try:
            from identity_baggage import get_current_identity
            identity = get_current_identity().to_dict()
        except Exception:
            identity = {}
        with self._lock:
            self._counter += 1
            run = Run(
                run_id=f"run_{self._epoch}_{self._counter:03d}",
                task=task,
                agents=list(agents) if agents else [],
                roles={},
                options=options or {},
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                user_identity=identity,
            )
            run._explicit_agents = bool(agents)
            run._workflow_ref_req = workflow_ref
            run._executor_name = getattr(self.executor, "name", "fixture")
            run._t0 = time.monotonic()
            self._runs[run.run_id] = run
        threading.Thread(target=self._drive, args=(run,), daemon=True).start()
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self) -> list[Run]:
        return list(self._runs.values())

    # ----------------------------------------------------------- the blueprint
    def _drive(self, run: Run) -> None:
        """One task in -> five phases -> terminal state. Always terminal."""
        try:
            for phase_fn in (self._admission, self._hydrate, self._preflight):
                if not phase_fn(run):
                    return  # fail-closed: phase set status/reason already
            # Bounded iteration around the agentic step + the review (~2 rounds).
            while True:
                run.iterations += 1
                if not self._execute(run):
                    return
                if self._finalize(run):
                    return  # terminal (passed, failed, or needs_human)
        except Exception as exc:  # the engine guarantee: never strand a run
            run.status, run.fail_reason = "failed", f"ENGINE_ERROR: {exc}"
            run.log(f"engine error: {exc}", "error")
        finally:
            if run.status in ("queued", "running"):  # safety net
                run.status = "failed"
                run.fail_reason = run.fail_reason or "ENGINE_STALL"
            # Two-bucket terminal model: a deterministic failure stays
            # `failed` (resubmit won't help); a transient one is re-graded to
            # `needs_human` so a human can resume rather than just see "failed".
            if run.status == "failed" and not _is_permanent(run.fail_reason):
                run.status = "needs_human"
                run.log(f"transient failure ({run.fail_reason}) -> needs_human "
                        "(a human can resume; resubmit may succeed)", "warn")
            # A passed run keeps its backend subprocess alive so the produced UI can
            # be replayed against the live endpoint, but NOT unbounded: it goes into
            # the bounded replay pool (cap + TTL + atexit reap), so passing runs can
            # never pile into thousands of orphaned servers. Any non-passed terminal
            # stops its server immediately so a failed/abandoned run leaks nothing.
            if run.status == "passed":
                self._keep_replay_server(run)
            else:
                self._stop_server(run)

    # Phase 1, deterministic. Admission validates AND ROUTES: the workflow
    # registry decides which agents this task dispatches (an unknown
    # workflow_ref fails loud, never a guess).
    def _admission(self, run: Run) -> bool:
        run.phase, run.status = "admission", "queued"
        if not run.task.strip():
            run.status, run.fail_reason = "failed", "EMPTY_TASK"
            run.log("admission rejected: empty task", "error")
            return False
        try:
            route = router.route(run.task, run._workflow_ref_req)
        except router.RouteError as exc:
            run.status, run.fail_reason = "failed", str(exc)
            run.log(f"admission rejected: {exc}", "error")
            return False
        if run._explicit_agents:
            unknown = [a for a in run.agents if a not in ROLE_BY_AGENT]
            if unknown:
                run.status, run.fail_reason = "failed", f"UNKNOWN_AGENT:{','.join(unknown)}"
                run.log(f"admission rejected: unknown agents {unknown}", "error")
                return False
            route.agents = list(run.agents)
            route.rule = "explicit agent selection (router consulted for usecase only)"
        else:
            run.agents = list(route.agents)
        run.route, run.usecase = route.public(), route.usecase
        run.roles = {a: ROLE_BY_AGENT[a] for a in run.agents}
        run.progress = {a: RoleResult(agent=a, role=run.roles[a]) for a in run.agents}
        # Recompute the active count from the source of truth (no drifting counter).
        active = self.active_count(exclude=run.run_id)
        if active >= self.max_concurrent:
            run.status, run.fail_reason = "failed", "CONCURRENCY_LIMIT"
            run.log(f"admission rejected: {active} runs active (limit {self.max_concurrent})", "error")
            return False
        run.log(f"admitted + routed: {route.workflow_ref} ({route.rule}) -> "
                f"agents {run.agents}, usecase {run.usecase}")
        return True

    # Phase 2, deterministic, real file reads. Hydration reads the task spec, the
    # module, AND each dispatched role's harness steering file (the same files an
    # attendee edits) because those files drive what agent_execution builds.
    def _hydrate(self, run: Run) -> bool:
        run.phase, run.status = "context_hydration", "running"
        uc = router.usecase_paths(run.usecase)
        context: dict[str, int] = {}
        for label, path in [
            (os.path.basename(uc["module"]) + ".py",
             os.path.join(uc["dir"], uc["module"] + ".py")),
            ("grading contract", os.path.join(uc["grading"], "contract.py")),
        ]:
            try:
                with open(path, encoding="utf-8") as f:
                    context[label] = len(f.read())
            except OSError:
                context[label] = 0
        run.log("hydrated context: " + ", ".join(f"{k} ({v}B)" for k, v in context.items()))
        # Hydrate each dispatched role's harness file so the build is provably
        # steered by it. Fail closed if a routed role has no steering.
        harness: list[str] = []
        for agent_id in run.agents:
            path = builders.harness_file(agent_id, run.usecase)
            if os.path.isfile(path):
                harness.append(f"{agent_id} ({os.path.basename(path)}, "
                               f"{len(open(path, encoding='utf-8').read())}B)")
            else:
                run.status, run.fail_reason = "failed", f"HARNESS_MISSING:{agent_id}"
                run.log(f"context hydration failed: no harness file for {agent_id}", "error")
                return False
        run.log("hydrated harness: " + ", ".join(harness))
        # Real-runtime dispatch: the deployed coding agents build inside their
        # container, so the usecase module + grading contract must live on the
        # shared /mnt/s3files mount they can import. Stage them now (read-through
        # S3 upload), keyed to this run so concurrent runs never collide.
        if getattr(self.executor, "name", "") == "agentcore":
            staged = self._stage_module_to_runtime(run, uc)
            if not staged:
                run.status, run.fail_reason = "failed", "SKILL_STAGING_FAILED"
                run.log("context hydration failed: could not stage the module to "
                        "/mnt/s3files for the runtime agents", "error")
                return False
            run.log(f"staged module + grading to the runtime workspace: {staged}")
        return True

    def _stage_module_to_runtime(self, run: Run, uc: dict[str, str]) -> str | None:
        """Upload the usecase module + grading contract to the shared S3Files mount
        so the deployed agents can import them. Returns the runtime workspace path
        (``/mnt/s3files/<run_subdir>``) on success, None on failure.

        S3 is read-through into every runtime's /mnt/s3files: an object uploaded
        under ``agents/mnt/s3files/<key>`` appears at ``/mnt/s3files/<key>`` in the
        container. We stage per-run so runs are isolated and a reset is a prefix
        delete."""
        import runtime_stage  # noqa: PLC0415 (lazy, only on the agentcore path)
        try:
            return runtime_stage.stage_usecase(run.run_id, uc)
        except Exception as exc:  # noqa: BLE001
            run.log(f"module staging error: {exc}", "warn")
            return None

    # Phase 3, deterministic, fail-closed (the pre-flight discipline)
    def _preflight(self, run: Run) -> bool:
        run.phase = "pre_flight"
        uc = router.usecase_paths(run.usecase)
        checks: list[tuple[str, Any]] = [
            ("GRADING_CONTRACT_MISSING", lambda: os.path.isdir(uc["grading"])),
            ("SKILL_IMPORT_FAILED", lambda: self._check_skill_imports(uc)),
        ]
        for agent_id in run.agents:
            checks.append((f"HARNESS_MISSING:{agent_id}",
                           lambda a=agent_id: os.path.isfile(builders.harness_file(a, run.usecase))))
        # REAL-ONLY readiness: on the shipped agentcore executor, every dispatched
        # role MUST have a wired runtime ARN. Check it HERE, before any terminal
        # work runs, so an unwired orchestrator fails loud immediately instead of
        # streaming real-looking ls/install/import theater and only erroring deep
        # in the produce step (which reads as a mock). A role with no wired runtime
        # is RUNTIME_NOT_WIRED; wire it (Settings / runtime_config / a local
        # agentcore dev URI), never a local fake.
        if getattr(self.executor, "name", "") == "agentcore":
            import runtime_config  # noqa: PLC0415 (lazy, only on the agentcore path)
            for agent_id in run.agents:
                checks.append((f"RUNTIME_NOT_WIRED:{agent_id}",
                               lambda a=agent_id: runtime_config.pick(a) is not None))
        if run.route and run.route.get("read_only"):
            # Review workflow: there must be something to review (the PR maps
            # back to an exact run; without one, fail fast, never guess).
            checks.append(("NO_RUN_TO_REVIEW", lambda: self._review_target(run) is not None))
        for reason, check in checks:
            ok = False
            try:
                ok = check()
            except Exception:
                ok = False
            if not ok:
                run.status, run.fail_reason = "failed", reason
                run.log(f"pre-flight failed fast: {reason}", "error")
                return False
        run.log("pre-flight green: contract, module import, harness all ready")
        return True

    @staticmethod
    def _check_skill_imports(uc: dict[str, str]) -> bool:
        r = subprocess.run([sys.executable, "-c", f"import {uc['module']}"],
                           cwd=uc["dir"], capture_output=True, timeout=20)
        return r.returncode == 0

    def _review_target(self, run: Run) -> Run | None:
        """Resolve the run a review workflow inspects: explicit option, else the
        most recent passed run whose generated server file still exists."""
        target_id = run.options.get("target_run")
        if target_id:
            t = self._runs.get(target_id)
            return t if t and t._server_file and os.path.isfile(t._server_file) else None
        candidates = [r for r in self._runs.values()
                      if r.status == "passed" and r.run_id != run.run_id
                      and r._server_file and os.path.isfile(r._server_file)]
        return max(candidates, key=lambda r: r.created_at, default=None)

    # ----------------------------------------------------------- runtime step
    # The shipped generation step: the role's coding-agent CLI runs INSIDE its
    # deployed AgentCore Runtime, WRITES its artifact file there, and the engine
    # reads THAT file back over the command shell (never a stdout-scraped block).
    def _runtime_cli(self, run: Run, agent_id: str, role: RoleResult, prompt: str,
                     model: str, artifact_rel: str) -> dict[str, Any]:
        """Run ``agent_id``'s CLI headless INSIDE its deployed AgentCore Runtime,
        writing the artifact into the role's local workdir so the engine's
        gate/compose read it exactly as the in-process path would.

        Dispatches over the command shell via ``runtime_exec`` against the role's
        wired runtime ARN; the agent builds in ``/mnt/s3files/<run_id>`` (where the
        module was staged in hydration) and we read the artifact back over the same
        shell. Returns the ``_read_artifact`` contract dict (``exit``, ``lines``).
        Raises if the role has no wired runtime: fail loud, never local."""
        import runtime_config  # noqa: PLC0415 (lazy, only on the agentcore path)
        import runtime_exec  # noqa: PLC0415
        # pick() round-robins across the role's FLEET (a role may have N deployed
        # runtimes: 2 Claude Code, 5 opencode, and so on), so concurrent runs spread their
        # dispatch across instances. A singleton fleet always returns the one ARN.
        hit = runtime_config.pick(agent_id)
        if not hit:
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: no AgentCore runtime wired for '{agent_id}' "
                f"(set AGENTCORE_RUNTIME_{agent_id.replace('-', '_').upper()} or wire "
                "it in Settings); real-only dispatch has no local fallback")
        arn = hit[0]
        role.engine = "agentcore"
        run.term(agent_id, f"echo 'dispatching to {arn.split('/')[-1]} on AgentCore "
                           f"Runtime; it builds in /mnt/s3files/{run.run_id} and "
                           "writes its artifact there'")
        t0 = time.monotonic()
        collected: list[str] = []

        def on_line(line: str) -> None:
            collected.append(line)
            with run._lock:
                role.last_beat = time.monotonic()

        result = runtime_exec.run_in_runtime(
            runtime_arn=arn, agent_id=agent_id, prompt=prompt,
            run_subdir=run.run_id, artifact_rel=artifact_rel,
            model=llm.resolve(model),
            region=os.environ.get("WORKSHOP_BEDROCK_REGION", "us-west-2"),
            on_line=on_line, timeout_s=HARNESS_ROLE_TIMEOUT_S)
        role.runtime_arn = arn
        role.runtime_session_id = result.get("session_id")
        # Persist the runtime-built artifact where the local path would, so the
        # gate/compose read it unchanged. The agent hardcoded the runtime-only
        # module path (/mnt/s3files/<run>-skill) into its `sys.path.insert`. That
        # path is empty when the acceptance gate boots this server as a LOCAL
        # subprocess (and meaningless in the attendee's PR). Rewrite it to a
        # PORTABLE import root driven by ``COST_ANALYZER_DIR`` (env), so the same
        # artifact imports in the runtime, in the local gate, and in the PR; the
        # server's logic is unchanged, only where it looks for the module.
        artifact_text = result["artifact"]
        import runtime_stage  # noqa: PLC0415 (lazy; the wirable mount root)
        runtime_skill = runtime_stage.skill_path(run.run_id)
        if runtime_skill in artifact_text:
            portable = ('os.environ.get("COST_ANALYZER_DIR", '
                        'os.path.dirname(os.path.abspath(__file__)))')
            artifact_text = artifact_text.replace(
                f'"{runtime_skill}"', portable).replace(
                f"'{runtime_skill}'", portable)
        dest = os.path.join(run.roledir(agent_id), artifact_rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(artifact_text)
        result["artifact"] = artifact_text
        tail = result["transcript"][-4000:]
        # A live-PTY dispatch (the muxed path) drove the agent's real interactive
        # session -- the SAME one the Agents page streams -- so label the entry as
        # that shared session and record its id, letting the run view point the
        # reader at the live terminal instead of pretending it was a one-shot.
        live_sid = result.get("session_id") if result.get("live_session") else None
        with run._lock:
            run.terminals.setdefault(agent_id, []).append({
                "cmd": (f"agentcore live session {live_sid} on {arn.split('/')[-1]} "
                        f"({agent_id} TUI)" if live_sid else
                        f"agentcore dispatch -> {arn.split('/')[-1]} ({agent_id} CLI)"),
                "output": _display_scrub(tail), "exit": result["exit"],
                "elapsed_s": round(time.monotonic() - t0, 2),
                **({"live_session_id": live_sid} if live_sid else {})})
            role.last_beat = time.monotonic()
        # The runtime CLI does not report machine usage over the shell; record an
        # honest zero (never invented), mirroring the no-usage local branch.
        role.estimated = False
        run.add_event(agent_id, {"kind": "text",
                                 "text": f"[{run.roles[agent_id]}] built on AgentCore "
                                         f"Runtime {arn.split('/')[-1]} ({len(result['artifact'])}B artifact)"})
        # Return the _read_artifact contract: the artifact is already on disk, so
        # exit 0 + the transcript lines suffice.
        return {"exit": result["exit"], "lines": collected,
                "text": result["artifact"], "usage": None}

    @staticmethod
    def _read_artifact(path: str, label: str, result: dict[str, Any]) -> str:
        """Read the file the CLI was told to write, or raise ROLE_EXECUTION_ERROR.

        A nonzero CLI exit OR a missing/empty artifact is a transient role failure
        (the bucket the two-bucket terminal model re-grades to needs_human). The
        message carries the tail of the CLI output so the terminal/journal show why.
        """
        tail = "\n".join(result.get("lines", []))[-600:]
        if result["exit"] != 0:
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: CLI exited {result['exit']} without "
                f"writing {label}; tail:\n{tail}")
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: CLI finished but {label} is missing/empty; "
                f"tail:\n{tail}")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _cli_backend_server(self, run: Run, uc: dict, role: RoleResult) -> str:
        """The Claude Code CLI (running INSIDE its deployed Runtime) writes
        mcp_server.py from CLAUDE.md; the engine reads THAT file back."""
        feedback = ""
        if run.iterations > 1 and run.review:
            failed = [c["detail"] for c in (run.review.get("gate") or {}).get("checks", [])
                      if not c.get("passed")]
            failed += list(run.review.get("reasons") or [])
            if failed:
                feedback = ("\n\nPrevious round's review REQUESTED CHANGES on the "
                            "pull request. Address each point:\n"
                            + "\n".join(f"- {d}" for d in failed))
        model = self._role_model(run, "claude-code", "claude-opus-4-6")
        # On the runtime the module is staged read-only at <run_id>-skill/; the agent
        # builds in its own writable <run_id>/ workdir (set as cwd by runtime_exec).
        # The prompt must name the path the agent will actually see in its container.
        import runtime_stage  # noqa: PLC0415 (lazy; the wirable mount root)
        module_dir = runtime_stage.skill_path(run.run_id)
        prompt = (
            "You are the backend implementer role in a multi-agent build. Read "
            "CLAUDE.md in this directory for your role, and read the "
            f"`{module_dir}/skills/backend-engineering/SKILL.md` harness staged "
            "for this run (also baked at ~/skills/backend-engineering/SKILL.md) "
            "and apply it. Decide the shape of your solution from the "
            f"task; the task here is to expose the `{uc['module']}` module (on disk "
            f"at {module_dir}) as a remote MCP server, so a caller can list and "
            "call its tools over the wire.\n\n"
            f"The user's request for this run: {run.task}\n"
            "Where that request asks for specific backend behavior (a fix, a "
            "docstring, an extra check), realize it in the server you write; the "
            "wire contract below stays exact either way.\n\n"
            "Write the server as `./mcp_server.py` in this directory. The wire "
            "contract the rest of the system depends on (keep it exact):\n"
            f"- Python stdlib only. `sys.path.insert(0, {module_dir!r})` then "
            f"`import {uc['module']}` and call it LIVE; never copy its data or logic.\n"
            f"- Expose every tool the module publishes (`{uc['module']}.list_tools()`); "
            "do not drop or invent tools.\n"
            "- JSON-RPC 2.0 over HTTP POST: `initialize` (protocolVersion, serverInfo, "
            "capabilities.tools), `tools/list` (the exposed specs from "
            f"`{uc['module']}.list_tools()`), `tools/call` (dispatch and return "
            '{"content":[{"type":"text","text":json.dumps(result)}],"isError":false}).\n'
            "- Errors: unknown method/tool -> code -32601; ValueError/TypeError from "
            "dispatch -> -32602.\n"
            "- GET returns HTTP 200 with a small JSON liveness body.\n"
            "- `argparse`: `--port` (int, default env MCP_PORT or 9000) and `--host` "
            "(default 127.0.0.1); serve with ThreadingHTTPServer.\n"
            "Write ONLY the file; do not run the server." + feedback)
        result = self._runtime_cli(run, "claude-code", role, prompt, model, "mcp_server.py")
        server_file = os.path.join(run.roledir("claude-code"), "mcp_server.py")
        self._read_artifact(server_file, "mcp_server.py", result)
        run.term("claude-code", "echo 'CLI wrote mcp_server.py'")
        return server_file

    def _cli_frontend_page(self, run: Run, endpoint: str, role: RoleResult) -> str:
        """The opencode CLI (running INSIDE its deployed Runtime) writes
        chatbot.html from AGENTS.md, wired to the live endpoint; the engine reads
        THAT file back."""
        model = self._role_model(run, "opencode", "amazon-bedrock/us.anthropic.claude-sonnet-4-6")
        import runtime_stage  # noqa: PLC0415 (lazy; the wirable mount root)
        skills_dir = runtime_stage.skill_path(run.run_id)
        prompt = (
            "You are the frontend builder role in a multi-agent build. Read "
            "AGENTS.md in this directory for your role, and read the "
            f"`{skills_dir}/skills/frontend-design/SKILL.md` harness staged for "
            "this run and apply it. Decide the shape of the UI from the task and "
            "the skill: you own the structure, files, styling, and interactions "
            "of a REAL small frontend project.\n\n"
            f"The user's request for this run: {run.task}\n"
            "Where that request asks for specific UI work (a restyle, a layout "
            "change, wording), realize it; if it names an existing page on the "
            "shared mount, read that file and carry its content forward instead "
            "of starting generic.\n\n"
            f"The deployed MCP endpoint is {endpoint} . Build the UI as a project "
            "under `./ui/` in THIS directory: `./ui/index.html` is the entry "
            "point, and you may add any supporting files you judge right "
            "(stylesheets, scripts, assets), all under `./ui/`. The wire contract "
            "the rest of the system depends on (keep it exact):\n"
            "- A THIN client (the skill's top rule): every answer comes from a "
            'JSON-RPC `tools/call` POST to the endpoint via fetch(); the literal '
            'strings "tools/call" and "fetch(" must appear in the project; no '
            "pricing or business logic anywhere in it.\n"
            "- Render the JSON-RPC result (or error) for the user to read.\n"
            "- Static files only (no build step, no external CDN assets): the "
            "project must work served as-is.\n"
            "Write ONLY files under ./ui/.")
        turn_started = time.time()
        ui_dir = os.path.join(run.roledir("opencode"), "ui")
        chatbot_file = os.path.join(ui_dir, "index.html")
        try:
            result = self._runtime_cli(run, "opencode", role, prompt, model,
                                       "ui/index.html")
            self._read_artifact(chatbot_file, "ui/index.html", result)
            self._read_ui_tree(run)
        except Exception:  # noqa: BLE001 (ROLE_EXECUTION_ERROR from either step)
            # A patch-shaped task often names an existing page on the shared
            # mount, and the CLI may honor the user's literal intent by editing
            # THAT file in place instead of writing the ./ui/ project. If the
            # named page really was modified during this turn (mtime check, so
            # nothing stale or fabricated is ever accepted), the edit IS the
            # deliverable: adopt it as the entry point. Anything else re-raises.
            import re  # noqa: PLC0415 (single fallback path)
            import runtime_stage  # noqa: PLC0415 (wirable mount root)
            named = re.findall(r"(/mnt/s3files/[\w./-]+\.html)", run.task)
            adopted = None
            for p in named:
                local = p.replace("/mnt/s3files", runtime_stage.mnt_root(), 1)
                if (os.path.isfile(local) and os.path.getsize(local) > 0
                        and os.path.getmtime(local) >= turn_started - 5):
                    adopted = local
                    break
            if not adopted:
                raise
            os.makedirs(ui_dir, exist_ok=True)
            shutil.copyfile(adopted, chatbot_file)
            run.term("opencode", f"echo 'adopted in-place edit of {named[0]} "
                                 "as ui/index.html (modified this turn)'")
            run.log(f"frontend-builder: CLI edited the task-named page in place "
                    f"({named[0]}); adopted it as the ui/ entry point")
        run._ui_dir = ui_dir
        run.term("opencode", "echo 'CLI wrote the ui/ project'")
        return chatbot_file

    def _read_ui_tree(self, run: Run) -> None:
        """Pull the frontend's WHOLE ./ui/ tree back from the runtime workspace
        (index.html came back via the standard artifact read; supporting files
        need the directory read). Local-mount and fixture paths already share
        the filesystem, so only the deployed-runtime path needs the transfer.
        Best-effort: index.html alone is a valid (single-file) project."""
        ui_dir = os.path.join(run.roledir("opencode"), "ui")
        if self.executor.name != "agentcore":
            return
        import runtime_config  # noqa: PLC0415
        import runtime_exec  # noqa: PLC0415
        if os.environ.get("WORKSHOP_S3FILES_DIR"):
            # Local mount seam: the runtime workspace IS a local dir; copy it.
            import runtime_stage  # noqa: PLC0415
            src = os.path.join(runtime_stage.mnt_root(), run.run_id, "ui")
            if os.path.isdir(src):
                shutil.copytree(src, ui_dir, dirs_exist_ok=True)
            return
        hit = runtime_config.pick("opencode")
        if not hit:
            return
        try:
            tree = runtime_exec.read_tree_from_runtime(
                hit[0], run.run_id, "ui",
                region=os.environ.get("WORKSHOP_BEDROCK_REGION", "us-west-2"))
        except Exception as exc:  # noqa: BLE001 (supporting files are best-effort)
            run.log(f"ui tree read-back skipped: {exc}", "warn")
            return
        for rel, data in tree.items():
            dest = os.path.join(ui_dir, rel)
            if os.path.commonpath([os.path.abspath(dest), os.path.abspath(ui_dir)]) != os.path.abspath(ui_dir):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        if tree:
            run.log(f"frontend-builder: ui/ project read back "
                    f"({len(tree)} files)")

    def _cli_validator_authors_test(self, run: Run, endpoint: str,
                                    role: RoleResult) -> str:
        """The validator's Claude Code CLI (running INSIDE its deployed Runtime)
        AUTHORS an acceptance test for THIS deliverable against the live endpoint,
        and the engine reads that file back to run it.

        This is the loop-engineering checker doing real work (Compartment-2
        generate-verify): the validator decides what "acceptable" means for the
        task and encodes it as a runnable executable, instead of running a contract
        pinned in the repo. The engine runs the authored file and reads its real
        exit code, so the fail-loud spine is unchanged: a red test can never be a
        pass, and nothing is fabricated. Maker (backend) != checker (validator):
        the validator never edits the server, only probes it."""
        model = self._role_model(run, _VALIDATOR_AGENT, "us.anthropic.claude-opus-4-6-v1")
        feedback = ""
        if run.iterations > 1 and run.review:
            failed = [c["detail"] for c in (run.review.get("gate") or {}).get("checks", [])
                      if not c.get("passed")]
            failed += list(run.review.get("reasons") or [])
            if failed:
                feedback = ("\n\nA previous round requested changes; make sure your "
                            "test covers each point:\n"
                            + "\n".join(f"- {d}" for d in failed))
        prompt = (
            "You are the validator role in a multi-agent build. Read your steering "
            "in CLAUDE.md for your role, then AUTHOR the acceptance test for this "
            "deliverable and save it as `./acceptance_test` in this directory: ONE "
            "self-contained EXECUTABLE file, starting with a shebang line, in "
            "whatever language you judge fits the deliverable (anything installed "
            "in this container works). Exit 0 to accept, nonzero to reject, and "
            "print one line per check so a human can read what you verified.\n\n"
            f"The user's request for this run: {run.task}\n"
            "If that request names specific behavior, cover it with a check where "
            "the live endpoint can prove it.\n\n"
            f"The deliverable's server is live at {endpoint} (JSON-RPC 2.0 over HTTP "
            "POST). Your executable decides whether it is acceptable by probing the "
            "LIVE endpoint over the wire. At minimum it must verify: the server "
            "answers `tools/list` and every tool the module publishes is present "
            "(discovery); a real `tools/call` returns the correct structured result "
            "for a known input (correctness); and an invalid input is rejected with "
            "a JSON-RPC error rather than a wrong answer (validation). Read the "
            f"endpoint URL from the `MCP_ENDPOINT_URL` env var (default {endpoint!r}). "
            "You do NOT edit the server; you only test it. Write ONLY the file; do "
            "not run it." + feedback)
        result = self._runtime_cli(run, _VALIDATOR_AGENT, role, prompt, model,
                                   "acceptance_test")
        test_path = os.path.join(run.roledir(_VALIDATOR_AGENT), "acceptance_test")
        self._read_artifact(test_path, "acceptance_test", result)
        run.term(_VALIDATOR_AGENT, "echo 'validator authored the acceptance_test executable'")
        return test_path

    def _write_validator_report(self, run: Run, role: RoleResult,
                                grade_tail: str) -> None:
        """FIXTURE-ONLY role artifact: a deterministic note that the offline
        grading floor ran (the shipped path's validator AUTHORS
        acceptance_test.py instead; verdicts live on the PR, not in files)."""
        report_path = os.path.join(run.roledir(_VALIDATOR_AGENT), "validation_report.md")
        if self.executor.name == "fixture":
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("validation report: the grading contract ran; "
                        "see the grading output above.\n")
            run.add_event(_VALIDATOR_AGENT, {"kind": "text",
                                   "text": "[validator] wrote validation_report.md "
                                           "(deterministic, from the grading output)"})
            return
        raise RuntimeError(_NO_PRODUCER_ERROR)

    @staticmethod
    def _role_model(run: Run, agent_id: str, default: str) -> str:
        """Per-task model selection: ``options.models[agent_id]`` (alias or full
        Bedrock id, resolved by llm.resolve) overrides the roster default: the
        same per-task override surface a production model selector exposes.

        The roster ``default`` is itself wirable at deploy time so an event whose
        account lacks a given model (e.g. Opus 4.6 without a Bedrock Marketplace
        subscription) can retarget a role without a code edit:
        ``WORKSHOP_MODEL_<AGENT_ID>`` (agent-specific, e.g.
        ``WORKSHOP_MODEL_CLAUDE_CODE``) wins over the generic ``WORKSHOP_MODEL``,
        which wins over the baked ``default``. A per-task ``options`` model still
        overrides all of them."""
        env_default = (os.environ.get(f"WORKSHOP_MODEL_{agent_id.replace('-', '_').upper()}")
                       or os.environ.get("WORKSHOP_MODEL"))
        chosen = ((run.options.get("models") or {}).get(agent_id)
                  or run.options.get("model"))
        return chosen or env_default or default

    # Phase 4: THE one agentic phase. Each role is dispatched through
    # ``self.executor`` (executor.py): the shipped AgentCoreExecutor sends the role
    # to its DEPLOYED Runtime, where its CLI builds the artifact and the engine
    # reads it back; the test FixtureExecutor runs the closure in-process and the
    # PRODUCE step builds the artifact deterministically. Either way every visible
    # step is a real shell command captured into the role's terminal, and the
    # verdict path (boot + acceptance gate + reviewer + compose + PR) is identical.
    def _execute(self, run: Run) -> bool:
        run.phase, run.status = "agent_execution", "running"
        budget = AGENT_EXECUTION_TIMEOUT_S
        deadline = time.monotonic() + budget
        uc = router.usecase_paths(run.usecase)
        backend_ready = threading.Event()
        endpoint: dict[str, str] = {}

        # Review workflow: no building. The validator boots the target run's
        # artifact and the review orchestrator judges it in finalization.
        if run.route and run.route.get("read_only"):
            return self._execute_review(run, uc)

        # inject_failure teaches the bounded-iteration loop: round 1 points the
        # gate at a dead port (a broken deploy), round 2 deploys correctly.
        sabotage = bool(run.options.get("inject_failure")) and run.iterations == 1

        def install_harness(agent_id: str) -> None:
            """The role installs its OWN harness: it writes the steering file into
            its container, then applies the OPTIONAL ``harness:setup`` block.

            The named file is the default configuration, but the harness is the
            attendee's to extend. Anything in ``harness:setup`` (MCP servers,
            extra skills, install commands) is set up here, in the role's real
            terminal, exactly as a developer would extend their own harness."""
            src = builders.harness_file(agent_id, run.usecase)
            # The dest filename each harness reads from cwd: Claude Code reads
            # CLAUDE.md (backend AND validator, both Claude Code), opencode reads
            # AGENTS.md. The validator's acceptance-contract steering lands as its
            # own CLAUDE.md in the workdir.
            rel = {"claude-code": "CLAUDE.md",
                   "claude-code-validator": "CLAUDE.md",
                   "opencode": "AGENTS.md"}[agent_id]
            dest_dir = os.path.dirname(rel)
            mkdir = f"mkdir -p {dest_dir} && " if dest_dir else ""
            run.term(agent_id, f"{mkdir}cp {json.dumps(src)} {rel} && head -4 {rel}")
            setup = builders.parse_setup_spec(src)
            for m in setup["mcp"]:
                # Record the MCP server in the harness's own config shape (the
                # same `claude mcp add` / config.toml entry a developer writes).
                run.term(agent_id, f"mkdir -p .mcp && printf '%s\\n' "
                                   f"{json.dumps(json.dumps(m))} >> .mcp/servers.jsonl "
                                   f"&& echo 'mcp server {m.get('name', '?')} registered'")
            skill_dirs = []
            for skill_path in setup["skills"]:
                full = os.path.join(os.path.dirname(src), skill_path)
                if os.path.isdir(full):
                    run.term(agent_id, f"mkdir -p skills && cp -R {json.dumps(full)} skills/ "
                                       f"&& ls skills/")
                    skill_dirs.append(full)
                else:
                    run.term(agent_id, f"echo 'skill path not found: {skill_path}' && false")
            # Runtime dispatch builds in /mnt/s3files/<run_id>, not this local
            # workdir, so the skill must ALSO land there for the dispatched CLI
            # to read the skills/<name>/SKILL.md its prompt names.
            if skill_dirs and getattr(self.executor, "name", "") == "agentcore":
                import runtime_stage  # noqa: PLC0415 (lazy, agentcore path only)
                try:
                    runtime_stage.stage_skills(run.run_id, skill_dirs)
                    run.term(agent_id, "echo 'skills staged to the runtime workspace'")
                except Exception as exc:  # noqa: BLE001 (skill is guidance, not the gate)
                    run.log(f"skill staging to runtime failed ({exc}); the role "
                            "falls back to its baked-in/steering guidance", "warn")
            for cmd in setup["install"]:
                run.term(agent_id, cmd)

        def backend(role: RoleResult) -> None:
            # BUILD the server from the Claude Code harness (CLAUDE.md build spec),
            # against the live module, not a pre-written file. Then boot it.
            # Editing the module or the build spec changes what this produces.
            run.term("claude-code", f"ls {json.dumps(uc['dir'])}")
            install_harness("claude-code")
            run.term("claude-code", _py(
                f"import sys; sys.path.insert(0,{uc['dir']!r}); import {uc['module']}; "
                f"print(len({uc['module']}.list_tools()), 'tools in the tool registry')"))
            # PRODUCE step: the ONE thing that varies by executor. Shipped
            # (AgentCoreExecutor): the Claude Code CLI runs INSIDE its deployed
            # Runtime; _cli_backend_server dispatches there (engine._runtime_cli)
            # and writes mcp_server.py back. Tests (FixtureExecutor): the
            # deterministic builder produces it from CLAUDE.md, no model, offline.
            # No other producer exists; a real-only shipped path fails loud.
            if self.executor.name == "agentcore":
                server_file = self._cli_backend_server(run, uc, role)
            elif self.executor.name == "fixture":
                server_file = builders.build_mcp_server(
                    run.roledir("claude-code"), uc["dir"],
                    claude_md_path=builders.harness_file("claude-code", run.usecase),
                    module_name=uc["module"])
            else:
                raise RuntimeError(_NO_PRODUCER_ERROR)
            run._server_file = server_file
            run.term("claude-code", "ls -la mcp_server.py && head -6 mcp_server.py")
            port = _free_port()
            # A runtime-built server resolves its module import from COST_ANALYZER_DIR
            # (portable env, set by _runtime_cli's rewrite); point it at the local
            # module so the gate boots it here. Harmless for locally-built servers.
            server_env = {**os.environ, "COST_ANALYZER_DIR": uc["dir"]}
            proc = subprocess.Popen(
                [sys.executable, server_file, "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=server_env,
            )
            run._server_proc = proc
            url = f"http://127.0.0.1:{port}"
            for _ in range(50):  # wait for liveness
                try:
                    import urllib.request
                    with urllib.request.urlopen(url, timeout=1) as resp:
                        if resp.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            run.term("claude-code", _py(
                f"import urllib.request; print(urllib.request.urlopen({url!r}, timeout=2)"
                ".read().decode())"))
            endpoint["url"] = f"http://127.0.0.1:{_free_port()}" if sabotage else url
            backend_ready.set()
            spec = builders.parse_build_spec(builders.harness_file("claude-code", run.usecase))
            n_tools = ("all" if spec["expose"] == "all" else len(spec["expose"]))
            role.note = ("built + deployed MCP server (broken endpoint this round)" if sabotage
                         else f"built MCP server from CLAUDE.md, {n_tools} tools live at {url}")
            run.log(f"backend-mcp: generated mcp_server.py from harness, subprocess up on {url}"
                    + (" (sabotaged endpoint for iteration demo)" if sabotage else ""))

        def validator(role: RoleResult) -> None:
            install_harness(_VALIDATOR_AGENT)
            # Loop-engineering checker: the validator AUTHORS the acceptance test
            # for this deliverable (shipped path), rather than running a pinned
            # contract. The engine runs the authored file in finalization and reads
            # its real exit code. On the fixture/offline path there is no runtime to
            # author from, so the shipped grading contract stands in as the floor
            # and the deterministic report is written; either way a real
            # execution decides, the validator never fabricates a pass.
            backend_ready.wait(timeout=300)
            if self.executor.name == "agentcore":
                url = endpoint.get("url", "")
                run._acceptance_test_file = self._cli_validator_authors_test(run, url, role)
                role.note = "authored the acceptance test against the live endpoint"
                run.log("validator: authored the acceptance test for this deliverable")
            else:
                # Fixture/offline: keep the shipped grading contract as the floor.
                grade, InProcessClient, _ = reviewer.load_grading(uc["grading"])
                verdict = grade(InProcessClient())
                n_green = sum(c['passed'] for c in verdict['checks'])
                out = run.term(_VALIDATOR_AGENT,
                               f"echo 'grading contract: {n_green}/{len(verdict['checks'])} "
                               "checks green (in-process floor)'")
                self._write_validator_report(
                    run, role, out[-1500:] if out else "(no output)")
                role.note = (f"proved the contract pre-deploy: "
                             f"{sum(c['passed'] for c in verdict['checks'])}/{len(verdict['checks'])} checks green")
                run.log(f"validator: in-process contract {'green' if verdict['passed'] else 'RED'} "
                        f"({out.splitlines()[-1] if out else ''})")

        def frontend(role: RoleResult) -> None:
            # The backend role dispatches its CLI into a deployed Runtime, which can
            # take a while; wait generously for the endpoint before wiring the UI.
            backend_ready.wait(timeout=300)
            install_harness("opencode")
            # BUILD the chatbot UI from the opencode harness (AGENTS.md UI spec), wired to
            # the live endpoint. Editing the steering file changes this page.
            url = endpoint.get("url", "")
            # PRODUCE step: varies by executor. Shipped (AgentCoreExecutor): the
            # opencode CLI runs INSIDE its deployed Runtime; _cli_frontend_page
            # dispatches there (engine._runtime_cli) and reads the ui/ project
            # back. Tests (FixtureExecutor): the deterministic builder produces a
            # one-file ui/ from AGENTS.md, no model, offline. No other producer;
            # real-only fails loud.
            if self.executor.name == "agentcore":
                chatbot_file = self._cli_frontend_page(run, url, role)
            elif self.executor.name == "fixture":
                ui_out = os.path.join(run.roledir("opencode"), "ui")
                os.makedirs(ui_out, exist_ok=True)
                chatbot_file = builders.build_chatbot(
                    ui_out, url,
                    agents_md_path=builders.harness_file("opencode", run.usecase),
                    filename="index.html")
                run._ui_dir = ui_out
            else:
                raise RuntimeError(_NO_PRODUCER_ERROR)
            run._chatbot_file = chatbot_file
            run.term("opencode", "ls -la ui/ && grep -o '<title>[^<]*' ui/index.html")
            ui = builders.parse_ui_spec(builders.harness_file("opencode", run.usecase))
            try:
                _, _, RemoteMCPClient = reviewer.load_grading(uc["grading"])
                tools = RemoteMCPClient(url).list_tools()
                run.term("opencode", _py(
                    "import json,urllib.request; req=urllib.request.Request("
                    f"{url!r}, data=json.dumps({{'jsonrpc':'2.0','id':1,'method':'tools/list'}})"
                    ".encode(), headers={'Content-Type':'application/json'}); "
                    "r=json.loads(urllib.request.urlopen(req, timeout=3).read()); "
                    "print([t['name'] for t in r['result']['tools']])"))
                role.note = (f"built the ui/ project '{ui['title']}'; "
                             f"live tools/list returned {len(tools)} tools")
                run.log(f"frontend-builder: generated the ui/ project, "
                        f"tools/list round-trip OK ({len(tools)} tools)")
            except Exception as exc:
                role.note = f"built chatbot UI '{ui['title']}'; endpoint not answering yet ({type(exc).__name__})"
                run.log(f"frontend-builder: endpoint probe failed: {exc}", "warn")

        # Frontend-only route: the run still needs a live endpoint to wire the UI
        # to. The ENGINE provides it as infrastructure (hydrated environment, not
        # a dispatched role) and says so in the orchestrator's own terminal.
        if "claude-code" not in run.agents:
            infra = builders.build_mcp_server(
                os.path.join(run.workdir, "infra"), uc["dir"], module_name=uc["module"])
            run._server_file = run._server_file or infra
            port = _free_port()
            run._server_proc = subprocess.Popen(
                [sys.executable, infra, "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
            endpoint["url"] = f"http://127.0.0.1:{port}"
            backend_ready.set()
            # Routed-roles invariant: every per-role terminal pane must map to a role
            # the router dispatched. The orchestrator is infrastructure, not a routed
            # agent, so it gets NO pane; the infra endpoint is recorded in the run
            # log (visible in the journal) instead of a phantom "orchestrator" terminal.
            run.log(f"orchestrator: no backend role routed; infrastructure endpoint up on {endpoint['url']}")

        work = {"backend-mcp": backend, "validator": validator, "frontend-builder": frontend}
        threads = []
        for agent_id in run.agents:
            role = run.progress[agent_id]
            role.state = "working"
            role.last_beat = time.monotonic()  # first heartbeat = role started

            def _run_role(role: RoleResult = role, agent_id: str = agent_id) -> None:
                t0 = time.monotonic()
                # Re-establish the recorded user context on this worker thread. The run captured
                # it at admission (run.user_identity), but a ContextVar does not cross
                # the threading.Thread boundary, so without this the dispatch
                # (runtime_exec) would see an anonymous identity and could neither
                # attribute per-user cost nor set the AGENTCORE_USER_* env. Restore it
                # from the run so identity reaches the runtime. (See identity_baggage.)
                try:
                    from identity_baggage import set_current_identity, UserIdentity
                    if run.user_identity:
                        set_current_identity(UserIdentity.from_dict(run.user_identity))
                except Exception:
                    pass
                try:
                    # Route through the execution seam (executor.py). The shipped
                    # AgentCoreExecutor confirms the role has a wired runtime (fails
                    # loud otherwise) and runs the closure, whose PRODUCE step
                    # dispatches to that deployed Runtime; the test FixtureExecutor
                    # runs the closure in-process and the PRODUCE step builds the
                    # artifact deterministically. Either way the engine reads the
                    # artifact and grades it.
                    local_work = work.get(role.role, validator)
                    self.executor.dispatch(run, agent_id, role, local_work)
                    role.state = "done"
                except Exception as exc:
                    role.state, role.note = "error", f"{type(exc).__name__}: {exc}"
                    run.log(f"{role.role} errored: {exc}", "error")
                finally:
                    # The runtime CLI does not report machine usage over the shell
                    # and the test fixture invokes no model, so tokens/cost stay an
                    # honest zero (never inferred from wall-clock).
                    role.latency_ms = int((time.monotonic() - t0) * 1000)
                    # Uniform event feed: the AgentCore Runtime path already streamed
                    # the role's CLI events. A role with no streamed feed (the
                    # deterministic test fixture) gets ONE honest summary event so
                    # every role has a feed of the same shape, never a fabricated
                    # tool call.
                    if not run.role_events.get(agent_id):
                        how = ("built on its deployed AgentCore Runtime"
                               if role.engine == "agentcore"
                               else "built the deterministic artifact")
                        summary = role.note or f"{role.role} {how}"
                        run.add_event(agent_id, {"kind": "text",
                                                 "text": f"[{role.role}] {summary}"})

            t = threading.Thread(target=_run_role, daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        run.artifact_endpoint = endpoint.get("url")
        # Liveness watchdog: a role still "working" after the PHASE DEADLINE is
        # WEDGED, not slow, and must be counted as a failure; otherwise the run
        # finalizes a half-built artifact. The deadline is the only liveness
        # authority here; last_beat is display-only (it dates the last terminal
        # line so the note can say HOW LONG the role was silent).
        now = time.monotonic()
        for r in run.progress.values():
            if r.state == "working":
                stale = now - r.last_beat
                r.state, r.note = "error", (
                    f"role wedged: no progress for {stale:.0f}s, exceeded the "
                    f"{budget}s phase budget")
                run.log(f"{r.role} timed out (wedged {stale:.0f}s) -> role failure", "error")
        errored = [r for r in run.progress.values() if r.state == "error"]
        if errored:
            # Tiered escalation: a single flaky role is ROLE_EXECUTION_ERROR, but
            # ALL routed roles failing is a SYSTEMIC break (harness/env), which a
            # metric filter should alarm on distinctly (a total-failure tier).
            total = len(errored) == len(run.progress) and len(run.progress) > 0
            reason = "ROLE_TOTAL_FAILURE" if total else "ROLE_EXECUTION_ERROR"
            run.status, run.fail_reason = "failed", reason
            if total:
                run.log(f"agent execution: ALL {len(errored)} routed roles failed "
                        "-> systemic failure (harness or environment)", "error")
            return False
        run.log(f"agent execution complete: {len(run.agents)} role(s) done, "
                "artifacts ready for review")
        return True

    def _execute_review(self, run: Run, uc: dict[str, str]) -> bool:
        """The review workflow's agentic phase: re-serve the target run's artifact
        and let the validator probe it. Nothing is built (read-only)."""
        target = self._review_target(run)
        if not target:
            run.status, run.fail_reason = "failed", "NO_RUN_TO_REVIEW"
            return False
        run._review_target = target.run_id
        run._server_file = target._server_file
        run._chatbot_file = target._chatbot_file
        run._ui_dir = getattr(target, "_ui_dir", None)
        # Re-run the SAME acceptance test the target's validator authored, against
        # the re-served artifact (verification by execution on the artifact under
        # review, not a fresh contract).
        run._acceptance_test_file = getattr(target, "_acceptance_test_file", None)
        run.composed_branch = target.composed_branch
        role = run.progress.get(_VALIDATOR_AGENT)
        if role:
            role.state = "working"
        t0 = time.monotonic()
        port = _free_port()
        run._server_proc = subprocess.Popen(
            [sys.executable, target._server_file, "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                import urllib.request
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        run.artifact_endpoint = url
        # Routed-roles invariant: only a role the router actually dispatched gets a
        # terminal pane. review/pr-v1 routes the validator, so this guard is
        # satisfied today; it's enforced structurally so a future read-only
        # workflow that routes a different validator can never fabricate a phantom
        # validator pane.
        if _VALIDATOR_AGENT in run.agents:
            run.term(_VALIDATOR_AGENT, f"echo 'reviewing {target.run_id} (branch "
                             f"{target.composed_branch or 'n/a'}) at {url}'")
            authored = getattr(run, "_acceptance_test_file", None)
            if authored:
                run.term(_VALIDATOR_AGENT,
                         f"MCP_ENDPOINT_URL={url} {json.dumps(authored)}")
        if role:
            role.state = "done"
            role.latency_ms = int((time.monotonic() - t0) * 1000)
            role.note = f"re-served {target.run_id}'s artifact and probed it over the wire"
        run.log(f"review execution: target {target.run_id} re-served on {url}")
        return True

    # Phase 5, deterministic, but a SEPARATE PEN: the review orchestrator owns
    # the verdict (gate + critique + LGTM token); the build engine only reacts.
    def _finalize(self, run: Run) -> bool:
        """Returns True when the run reached a terminal state, False to iterate.

        The verify-iterate loop, on the pull request itself:

          1. GATE: the validator-authored acceptance test executes for real
             and its exit code decides. Red gate -> no PR work; loop or hand to a human.
          2. PR: on a green gate the deliverable is composed and the pull
             request opens (round 1) or its branch is UPDATED in place (a
             re-implement round pushes new commits to the same PR).
          3. ASSESSMENT: the judge (separate pen; LLM, fail-open) reviews the
             deliverable and its verdict is posted DIRECTLY on the PR as an
             Assessment comment. Approve ends the run (auto policy may then
             squash-merge). Request-changes loops the routed roles with the
             judge's reasons as feedback, bounded by MAX_REVIEW_ROUNDS.
        """
        run.phase = "finalization"
        uc = router.usecase_paths(run.usecase)
        run.log(f"gate: acceptance test against {run.artifact_endpoint} "
                f"(round {run.iterations})")
        gate = reviewer.run_gate(run, uc["grading"])
        run.gate = gate

        read_only = bool(run.route and run.route.get("read_only"))
        verdict = None
        if gate.get("passed") and not read_only:
            # Green gate: land the work on the PR FIRST, then review it there,
            # exactly like a human team (code up for review before the verdict).
            try:
                self._compose_commit(run)
                run.log(f"gate green ({gate.get('summary','')}) -> composed commit "
                        f"{(run.composed_commit or '')[:10]} on {run.composed_branch}")
            except Exception as exc:
                run.log(f"compose commit skipped: {exc}", "warn")
            if run.pr_url:
                # A re-implement round: same PR, updated branch.
                update = github.update_pr(run)
                if update.get("error"):
                    run.log(f"PR update failed: {update['error']}", "warn")
                else:
                    run.log(f"PR branch updated in place: {run.pr_url} "
                            f"(round {run.iterations})")
            else:
                run.pr = github.open_pr(
                    run, f"Automated build for: {run.task}\n\n"
                         f"Acceptance gate: {gate.get('summary', '')}. The reviewer's "
                         "assessment follows as a PR comment.")
                if run.pr.get("pr_url"):
                    run.pr_url = run.pr["pr_url"]
                    run.log(f"PR opened for real: {run.pr_url} (credential source: "
                            f"{run.pr.get('source')})")
                elif run.pr.get("error"):
                    run.log(f"PR open failed: {run.pr['error']}", "warn")
                else:
                    run.log(f"PR skipped: {run.pr.get('skipped', 'local mode')}")

            # The judge reviews the deliverable ON the PR (separate pen).
            verdict = reviewer.assess(run, gate, run.iterations)
            run.review = verdict.public()
            if run.pr_url:
                posted = github.post_review(run, verdict.assessment)
                run.pr["review"] = posted
                run.log("assessment posted on the PR: "
                        f"{verdict.state} ({posted.get('review_url') or posted.get('skipped') or posted.get('error')})")
        elif gate.get("passed") and read_only:
            # Read-only review workflow: assess the TARGET run's deliverable and
            # post the assessment on ITS pull request; never compose a new one.
            verdict = reviewer.assess(run, gate, run.iterations)
            run.review = verdict.public()
            target = self._runs.get(run._review_target) if run._review_target else None
            if target is not None and getattr(target, "pr_url", None):
                run.pr = dict(getattr(target, "pr", None) or {})
                posted = github.post_review(run, verdict.assessment)
                run.log(f"review assessment posted on {target.run_id}'s PR: "
                        f"{posted.get('review_url') or posted.get('skipped') or posted.get('error')}")
            else:
                run.log(f"review APPROVED for {run._review_target} "
                        "(no PR to comment on; verdict recorded on the run)")
        else:
            run.review = {"state": "changes_requested", "lgtm": False,
                          "round": run.iterations, "gate": gate,
                          "reasons": [c["detail"] for c in gate.get("checks", [])
                                      if not c.get("passed")][:5]}

        if verdict is not None and verdict.lgtm:
            if run.pr_url and github.merge_policy() == "auto":
                # The fully-autonomous tail (opt-in, fail-closed default
                # human_review): the judge already approved ON the PR, so
                # squash-merge into the integration branch. github enforces
                # "never the default branch"; the judge stays the sole approver.
                merged = github.merge_pr(run)
                run.pr["merge"] = merged
                run.merge_state = ("merged" if merged.get("merged")
                                   else f"skipped:{merged['skipped']}" if merged.get("skipped")
                                   else f"error:{merged.get('error', 'unknown')}")
                run.log(f"auto-merge: {run.merge_state}")
            elif run.pr_url:
                run.merge_state = "human_review"
                run.log("merge_policy=human_review: PR left open for a human to merge")
            # status flips terminal ONLY after compose+journal are written, so a
            # poller that sees "passed" always sees the full result (no race).
            run.status = "passed"
            self._ledger(run)
            return True

        # Not approved: loop (bounded) or hand to a human. The judge's reasons
        # ride into the next round as feedback (run.review["reasons"]).
        if run.iterations >= MAX_ITERATIONS:
            run.status, run.fail_reason = "needs_human", "ITERATION_CAP"
            run.log(f"changes still requested after {run.iterations} rounds "
                    "-> needs_human (the PR stays open with the assessment)", "warn")
            self._stop_server(run)
            self._ledger(run)
            return True
        why = (gate.get("summary") or "assessment requested changes")
        run.log(f"changes requested ({why}) -> one bounded re-implement pass "
                "updating the same PR", "warn")
        self._stop_server(run)
        return False

    # The composed repo is shared by every run; git allows one writer at a time
    # (index.lock), so compose is serialized across concurrent runs. A bare Lock
    # would deadlock the whole engine if one compose ever hung while holding it,
    # so this is a self-healing lease that auto-evicts a wedged holder.
    _COMPOSE_LEASE = _Lease(COMPOSE_LEASE_STUCK_S)

    def _compose_commit(self, run: Run) -> None:
        """Compose the dispatched roles' artifacts into ONE real git commit.

        The deliverable directory holds whatever the routed roles produced (the
        backend's MCP server, the reviewer's critique report, the frontend's
        chatbot page); the commit on a per-run branch is the local equivalent of
        finalization's PR, and the exact branch github.py pushes when connected.
        """
        Engine._COMPOSE_LEASE.acquire(run.run_id)
        try:
            self._compose_commit_locked(run)
        finally:
            Engine._COMPOSE_LEASE.release(run.run_id)

    def _compose_commit_locked(self, run: Run) -> None:
        repo = os.path.join(_RUNS_DIR, "composed")
        # Gateway model: compose the deliverable into a LOCAL scratch repo here;
        # there is no token to clone the attendee's private repo and none is needed.
        # github.open_pr() later publishes this branch's files into the attendee's
        # template-derived repo via the GitHub MCP Gateway (create_branch +
        # put_file). ensure_compose_base() only reports whether a gateway is wired;
        # it never clones and never fails here.
        base = github.ensure_compose_base()
        run.compose_base = base
        run.log(f"compose base: {base.get('mode')}"
                + (f" ({base.get('repo')})" if base.get("repo") else "")
                + (f": {base['reason']}" if base.get("reason") else ""))
        deliver = os.path.join(repo, "deliverable")
        git_env = {**os.environ, "GIT_AUTHOR_NAME": "orchestrator",
                   "GIT_AUTHOR_EMAIL": "orchestrator@local",
                   "GIT_COMMITTER_NAME": "orchestrator",
                   "GIT_COMMITTER_EMAIL": "orchestrator@local"}
        if not os.path.isdir(os.path.join(repo, ".git")):
            subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True, timeout=20)
            subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty",
                            "-m", "init composed-deliverable repo"],
                           check=True, timeout=20, env=git_env)
        branch = f"run/{run.run_id}"
        # Root this run's branch at the EMPTY init commit (never the previous
        # run's tip) and clean the work tree BEFORE writing this run's files. If a
        # branch were cut from HEAD (the last run's commit), a run whose deliverable
        # is byte-identical to the prior run would produce an EMPTY diff, and both
        # this run's Changes tab AND github.py's PR path (_composed_files uses
        # `git show --name-only`, which lists only files a commit CHANGES vs its
        # parent) would drop those files. Rooting at the empty base makes each
        # commit's diff == exactly its own deliverable set, the invariant
        # github.py's docstring already assumes.
        root = subprocess.run(["git", "-C", repo, "rev-list", "--max-parents=0", "main"],
                              capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        base_ref = root[-1] if root else "main"
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-B", branch, base_ref],
                       check=True, timeout=20, env=git_env)
        # Drop any leftover from a prior run (e.g. a stale chatbot.html when this
        # run is backend-only), so the commit is exactly this run's deliverable.
        subprocess.run(["git", "-C", repo, "clean", "-fdq"], check=True, timeout=20, env=git_env)
        os.makedirs(deliver, exist_ok=True)
        # backend artifact: the exact MCP server the harness GENERATED and the gate
        # just graded (built this run from CLAUDE.md, not a checked-in reference file).
        if run._server_file and os.path.isfile(run._server_file):
            shutil.copy(run._server_file, os.path.join(deliver, "mcp_server.py"))
        # the validator's authored acceptance test SHIPS WITH the deliverable, so
        # the PR reviewer (human or bot) can rerun the exact gate that passed.
        # The review verdict itself is NOT a committed file: it is posted on the
        # pull request as an Assessment comment, where reviews belong.
        authored = getattr(run, "_acceptance_test_file", None)
        if authored and os.path.isfile(authored):
            shutil.copy(authored, os.path.join(deliver, "acceptance_test"))
        # frontend artifact: the chatbot page generated from AGENTS.md (by the
        # model in bedrock mode, by the builder locally), wired to the live MCP
        # endpoint, only when the frontend role was routed.
        if "opencode" in run.agents:
            ui_dir = getattr(run, "_ui_dir", None)
            if ui_dir and os.path.isdir(ui_dir):
                shutil.copytree(ui_dir, os.path.join(deliver, "ui"),
                                dirs_exist_ok=True)
            elif run._chatbot_file and os.path.isfile(run._chatbot_file):
                os.makedirs(os.path.join(deliver, "ui"), exist_ok=True)
                shutil.copy(run._chatbot_file,
                            os.path.join(deliver, "ui", "index.html"))
            else:
                ui_out = os.path.join(deliver, "ui")
                os.makedirs(ui_out, exist_ok=True)
                builders.build_chatbot(ui_out, run.artifact_endpoint or "",
                                       agents_md_path=builders.harness_file("opencode", run.usecase),
                                       filename="index.html")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True, timeout=20, env=git_env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty",
                        "-m", f"{run.run_id}: {(run.route or {}).get('workflow_ref', 'run')}, "
                              f"compose {' + '.join(run.roles[a] for a in run.agents)}\n\n"
                              f"task: {run.task}\ngate: {run.gate.get('summary','')}"],
                       check=True, timeout=20, env=git_env)
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        run.composed_branch, run.composed_commit = branch, sha

    def _ledger(self, run: Run) -> None:
        """Append the run record to the shared telemetry ledger (Stage 3 reads it)."""
        try:
            os.makedirs(_RUNS_DIR, exist_ok=True)
            row = {
                "kind": "orchestrator_run", "run_id": run.run_id,
                "user_id": run.user_identity.get("user_id") or getpass.getuser(),
                "user_email": run.user_identity.get("user_email", ""),
                "user_name": run.user_identity.get("user_name", ""),
                "status": run.status,
                "started_at": run.created_at, "task": run.task,
                "workflow_ref": (run.route or {}).get("workflow_ref"),
                "usecase": run.usecase,
                "iterations": run.iterations, "fail_reason": run.fail_reason,
                "composed_commit": run.composed_commit,
                "review_state": (run.review or {}).get("state"),
                "pr_url": run.pr_url,
                "merge_state": run.merge_state,
                "roles": [
                    {"agent": r.agent, "role": r.role, "state": r.state,
                     "latency_ms": r.latency_ms, "tokens": r.tokens,
                     "cost_usd": r.cost_usd, "estimated": r.estimated,
                     # harness mode: "cli" (real CLI ran) | "bedrock" (per-role
                     # fallback); "" otherwise. Stage 3 reads it for attribution.
                     "engine": r.engine,
                     "runtime_arn": r.runtime_arn,
                     "runtime_session_id": r.runtime_session_id}
                    for r in run.progress.values()
                ],
            }
            with open(_LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as exc:
            run.log(f"ledger write failed: {exc}", "warn")

    def _stop_server(self, run: Run) -> None:
        """Stop a run's MCP server for good and forget it. Hardened
        (terminate -> wait -> kill) so a stubborn child never lingers, and the run
        is dropped from the replay pool too (idempotent: a run that was never in the
        pool is a no-op) so re-stopping or stranding never leaves a half-tracked
        proc."""
        _REPLAY.drop(run.run_id)
        _kill_proc(run._server_proc)
        run._server_proc = None

    def _keep_replay_server(self, run: Run) -> None:
        """A passed run's MCP server stays up for UI replay, but in the bounded
        pool (cap + TTL + atexit), so passing runs can't accumulate orphans. With no
        live server (nothing to replay) this is a no-op."""
        if run._server_proc and run._server_proc.poll() is None:
            _REPLAY.register(run.run_id, run._server_proc)
        # Opportunistic: each newly-passed run also sweeps any idle replay servers,
        # so a quiet box reclaims them without waiting on the 60s reconcile loop.
        _REPLAY.reap_idle()

    # ------------------------------------------------------- reconciliation
    def active_count(self, exclude: str | None = None) -> int:
        """Recompute the live-run count from the source of truth (self._runs),
        never a mutable counter that can drift. The admission CONCURRENCY_LIMIT
        check reads this."""
        with self._lock:
            return sum(1 for r in self._runs.values()
                       if r.status in ("queued", "running") and r.run_id != exclude)

    def reconcile(self) -> dict[str, int]:
        """Sweep for runs the happy path lost and force them to a terminal state.

        This daemon-callable (or startup) sweep finds any run stuck non-terminal
        past STRANDED_AFTER_S and force-transitions it to needs_human via the
        compare-and-swap guard, so a worker thread that legitimately advances
        mid-sweep is never double-written. Returns a tiered count {swept, stranded,
        errors} so a caller can alarm on systemic strand (every candidate failing)
        vs an isolated one.
        """
        now = time.monotonic()
        # Reap any replay server idle past its TTL on the same 60s cadence, so even
        # a box with no new submissions still releases passed-run servers.
        reaped = _REPLAY.reap_idle()
        if reaped:
            self._engine_log(f"reaped {reaped} idle replay server(s)", "info")
        swept = errors = stranded = 0
        for run in list(self._runs.values()):
            if run.status not in ("queued", "running"):
                continue
            if now - run._t0 < STRANDED_AFTER_S:
                continue
            stranded += 1
            try:
                # CAS: only strand it if it's STILL non-terminal (a concurrent
                # legit transition wins and this becomes a no-op (the
                # 'advanced during reconcile' branch).
                if run.transition("needs_human", "queued", "running",
                                   reason="STRANDED_NO_PROGRESS"):
                    self._stop_server(run)
                    run.log(f"reconciler: stranded in {run.phase} for "
                            f"{now - run._t0:.0f}s -> needs_human", "warn")
                    self._ledger(run)
                    swept += 1
            except Exception as exc:  # never let one bad run abort the sweep
                errors += 1
                run.log(f"reconciler error: {exc}", "error")
        # Tiered escalation: distinguish a systemic sweep failure from noise.
        if stranded and swept == 0 and errors:
            run_log_level = "error"
            self._engine_log(f"RECONCILER_TOTAL_FAILURE: {stranded} stranded, "
                             f"0 swept, {errors} errors", run_log_level)
        elif swept:
            self._engine_log(f"reconciler swept {swept}/{stranded} stranded runs", "warn")
        return {"swept": swept, "stranded": stranded, "errors": errors}

    def _engine_log(self, message: str, level: str = "info") -> None:
        """Engine-scoped log line (not tied to a single run), printed so a host
        process / CloudWatch agent can capture it; alarm-able by error_id."""
        print(f"[engine:{level}] {message}", file=sys.stderr)

    def shutdown(self) -> None:
        for run in self._runs.values():
            self._stop_server(run)


# ------------------------------------------------------------------ public views
def public_run(run: Run) -> dict:
    return {
        "run_id": run.run_id,
        "task": run.task,
        "status": run.status,
        "phase": run.phase,
        "created_at": run.created_at,
        "agents": run.agents,
        "roles": run.roles,
        # additive (API_CONTRACT.md "Engine additions"): the router's verdict
        "route": run.route,
        # Why a run stopped (RUNTIME_NOT_WIRED:<role>, HARNESS_MISSING:<role>, …)
        # so the console states the real reason instead of a bare "needs_human":
        # a fail-loud verdict must be legible, never look like a silent mock.
        "fail_reason": run.fail_reason,
    }


def public_progress(run: Run) -> list[dict]:
    return [
        {"agent": r.agent, "role": r.role, "state": r.state,
         "latency_ms": r.latency_ms, "tokens": r.tokens,
         "cost_usd": r.cost_usd, "note": r.note, "engine": r.engine}
        for r in run.progress.values()
    ]


def public_terminals(run: Run) -> dict:
    """Per-role shell transcripts: the console streams these into xterm panes."""
    with run._lock:
        return {agent: list(lines) for agent, lines in run.terminals.items()}


def public_events(run: Run) -> dict:
    """Per-role STRUCTURED agent events (text/thinking/tool_use/tool_result), in
    arrival order; the console renders these as live tool calls + reasoning."""
    with run._lock:
        return {agent: [dict(e) for e in evs] for agent, evs in run.role_events.items()}


def public_diff(run: Run) -> dict:
    """The REAL composed change as a per-file unified diff, for the session
    Changes tab (the local twin of the PR's Files-changed). Reads the run's own
    commit in the shared composed repo (``run.composed_commit`` on branch
    ``run.composed_branch``) with ``git show`` scoped to that commit, so the
    files and hunks are exactly what the PR carries, never a reconstruction.
    Empty ``files`` until compose runs (commit is null pre-gate)."""
    commit = run.composed_commit
    if not commit:
        return {"run_id": run.run_id, "commit": None, "branch": run.composed_branch,
                "files": [], "reason": "not composed yet (the commit lands once the gate is green)"}
    repo = os.path.join(_RUNS_DIR, "composed")
    files: list[dict] = []
    try:
        # Names + add/del counts for THIS commit (numstat), then the patch per file.
        stat = subprocess.run(
            ["git", "-C", repo, "show", "--numstat", "--format=", commit],
            capture_output=True, text=True, timeout=20)
        for line in stat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            patch = subprocess.run(
                ["git", "-C", repo, "show", "--format=", f"{commit}", "--", path],
                capture_output=True, text=True, timeout=20).stdout
            files.append({
                "path": path,
                "added": None if added == "-" else int(added),
                "removed": None if removed == "-" else int(removed),
                "patch": patch,
            })
    except (OSError, subprocess.SubprocessError) as exc:
        return {"run_id": run.run_id, "commit": commit, "branch": run.composed_branch,
                "files": [], "reason": f"diff unavailable: {type(exc).__name__}: {exc}"}
    return {"run_id": run.run_id, "commit": commit, "branch": run.composed_branch,
            "files": files}


def public_result(run: Run) -> dict:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "gate": {"passed": bool(run.gate and run.gate["passed"]),
                 "checks": (run.gate or {}).get("checks", [])},
        "pr_url": run.pr_url,
        "merge_state": run.merge_state,
        "composed_from": [run.roles[a] for a in run.agents],
        "iterations": run.iterations,
        # additive fields (API_CONTRACT.md "Engine additions"):
        "artifact_endpoint": run.artifact_endpoint,
        "composed_branch": run.composed_branch,
        "composed_commit": run.composed_commit,
        "fail_reason": run.fail_reason,
        "route": run.route,
        "review": run.review,
        "pr": run.pr,
        "compose_base": run.compose_base,
    }
