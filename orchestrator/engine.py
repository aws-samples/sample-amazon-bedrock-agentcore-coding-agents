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
    INSIDE its deployed Runtime and writes WHATEVER its task calls for; the engine
    names no file for a builder. The engine dispatches over the command shell
    (``runtime_exec`` via ``engine._runtime_cli``) against the role's WIRED runtime
    ARN and reads the role's work back as a whole tree (an empty tree is the failure,
    see ``_require_work``). The one filename that IS part of a contract is the
    validator's authored check, because the engine has to execute that file to read
    its exit code. A role with no wired runtime fails loud; there is no local
    fallback.

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

import filecmp
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

# How much of a run's event log rides along in its persisted state. The tail is where
# a failure is; the head is admission noise. Bounded so a status file stays a few KB.
_PERSIST_LOG_TAIL = 60


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



sys.path.insert(0, _HERE)
import harness_config  # noqa: E402
import executor  # noqa: E402
import github  # noqa: E402
import llm  # noqa: E402  (model-id alias resolution for the runtime dispatch)
import policy  # noqa: E402  (the guardrail every role command is screened against)
import replay  # noqa: E402  (the run's story, for the PR body: reports, never judges)
import reviewer  # noqa: E402
import presets  # noqa: E402
import role_graph  # noqa: E402  (the agent-execution phase as a Strands graph)
import run_store  # noqa: E402  (durable run state: a verdict outlives its session)
import roles  # noqa: E402  (the ONE declarative roster)

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

# -------------------------------------------------- the ONE name the engine knows
# The validator's authored check. A filename, and nothing else: the engine executes
# this file and reads its exit code. It does not know the language, the checks, or
# what the validator considers correct.
#
# This is the whole coupling between the engine and a deliverable, and it is
# deliberately one process. The engine starts NOTHING itself: if the work is a
# service, the validator's check starts it, probes it, and tears it down, because
# only the check knows what "running" means for this deliverable. Every alternative
# put knowledge back in the repo: booting the artifact needs an interpreter and a
# port flag, waiting for it needs a readiness convention, and a declared manifest is
# still a schema we invented. One subprocess, no conventions.
_ACCEPTANCE_CHECK = "acceptance_check"

# What compose must NOT publish. Two kinds, and both were observed on a live run:
#   * the HARNESS we installed into the role's working directory (its steering file
#     and any `harness:setup` skills). That is workshop scaffolding, not the
#     agents' deliverable, and it is identical in every run.
#   * what a RUNNING service leaves behind. The validator's check STARTS the
#     deliverable, so a real 3-role run committed `issues.db` next to
#     `issues.db-wal` and `issues.db-shm`: one run's state, published as source.
#
# The database file goes with its sidecars, and that pairing is the point. In WAL
# mode the committed rows live in the `-wal` until a checkpoint, so shipping the
# `.db` while excluding the `-wal` publishes a TORN database: a live run committed
# an `issues.db` whose tables existed and whose row counts were all zero, because
# the data was in the WAL we correctly left out. Half a database is worse than
# neither half, so the whole set goes.
#
# Nothing here encodes what the deliverable IS; these are only artifacts the
# engine itself put there or that running the code produced. A deliverable that
# genuinely needs seed data ships the code or the migration that creates it, which
# is what a reviewer can actually read.
_COMPOSE_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache",
                      ".ruff_cache", ".mypy_cache", "skills"}
_COMPOSE_SKIP_NAMES = {"CLAUDE.md", "AGENTS.md", ".DS_Store"}
# NFS silly-rename stubs: when a process deletes a file it still has open, the
# S3 Files (NFS) mount keeps it as `.nfsXXXXXXXX` until the handle closes. A live
# run committed three of them (4KB/32KB/49KB of nothing) because the validator's
# check had the database open when it replaced it. Filesystem bookkeeping, never
# anyone's work, and invisible on a local disk, which is why only a real mount
# surfaces it.
_COMPOSE_SKIP_PREFIXES = (".nfs",)
_COMPOSE_SKIP_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm",
                          ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm",
                          ".log")


def _compose_excluded(rel: str) -> bool:
    """True when ``rel`` is scaffolding or run-time state, not the deliverable.

    Used for the PULL REQUEST only. The gate uses ``_gate_excluded``, which is
    narrower: a file that merely reads badly in a diff is still part of the
    workspace the authored check was written against.
    """
    parts = rel.replace(os.sep, "/").split("/")
    if any(p in _COMPOSE_SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in _COMPOSE_SKIP_NAMES or name.startswith(_COMPOSE_SKIP_PREFIXES):
        return True
    return name.endswith(_COMPOSE_SKIP_SUFFIXES)


def _gate_excluded(rel: str) -> bool:
    """True when ``rel`` is OUR harness rather than the agents' workspace.

    Deliberately withholds only what the engine installed. Everything the roles
    (or the running deliverable) put in the workspace reaches the gate, because
    the check was authored against that workspace and the engine does not get to
    decide which of those files the check is allowed to see.
    """
    parts = rel.replace(os.sep, "/").split("/")
    if parts[0] == "skills":
        return True
    if any(p in ("__pycache__", ".git") for p in parts[:-1]):
        return True
    name = parts[-1]
    # Not a withheld file so much as not a file at all: see _COMPOSE_SKIP_PREFIXES.
    return name in _COMPOSE_SKIP_NAMES or name.startswith(_COMPOSE_SKIP_PREFIXES)

# What builders are told about runnability. Not a layout and not a filename: a
# property of good work, stated once. The engine never reads the answer.
_RUNNABLE_RULE = (
    "If your work is something that RUNS, make it runnable from THIS directory with "
    "no manual setup, and say plainly in your output how to start it (the exact "
    "command). A separate validator will start it that way to check it, and a human "
    "will read the same instruction in the pull request.\n")


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
# A role node still "working" after the phase deadline is WEDGED, not slow: treat it
# as a timeout failure rather than letting the run finalize a half-built artifact.
# The graph carries the same budget as its execution timeout, and this watchdog is
# the backstop that turns "the graph stopped and this role never finished" into a
# named role failure. The phase deadline is the ONLY liveness authority; each role's
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
    # A resubmit cannot help: the request or the wiring is wrong, not flaky.
    "EMPTY_TASK",
    # routing (presets.py): asking again with the same inputs fails the same way
    "UNKNOWN_PRESET", "UNKNOWN_ROLE", "PRESET_NOT_SPECIFIED",
    "NO_CHECKER_ROUTED", "NO_BUILDER_ROUTED", "NO_ROLES_ROUTED",
    # environment: a role has no steering, or there is nothing to review
    "HARNESS_MISSING", "NO_RUN_TO_REVIEW",
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

def _agents() -> list[dict]:
    """The served roster, in the shape the Stage 1 Agents shelf and /api/agents read.

    Projected from the role REGISTRY (``roles.py``), which declares each role once,
    so the shelf, the dispatch path, and the wiring surface cannot disagree about
    who is on the team. ``local_steering_path`` is the REAL file the engine reads
    and stages from (relative to orchestrator/); ``steering_path`` is the AgentCore
    deploy location.
    """
    return [{"id": r.id, "label": r.label, "default_role": r.role_name,
             "model": r.default_model, "credential": r.credential,
             "harness": {
                 "steering_format": r.steering_file,
                 "steering_path": r.steering_path,
                 "local_steering_path": r.local_steering_path,
                 "skills": list(r.skills),
                 "install": r.install,
             }} for r in roles.roster()]


# Module-level views kept for the callers that read them as data. They are computed
# from the registry at import; a deployment changes its roster through
# WORKSHOP_ROLES (read by the registry), not by editing a list here.
AGENTS = _agents()
ROLE_BY_AGENT = roles.role_names()


def _checker_agents() -> tuple[str, ...]:
    """The agent ids that CHECK. Read from the registry so the maker-checker split
    is declared in one place; the gate path below uses the first (a roster serves
    one checker) and would fail loud rather than proceed with none."""
    return roles.checker_ids()


def _validator_agent() -> str:
    """The agent id that plays the checker role in this deployment. Swapping the
    checker (a second Claude Code today, Kiro on the restore path) is a registry
    entry, not an edit here."""
    checkers = _checker_agents()
    if not checkers:
        raise RuntimeError(
            "NO_CHECKER_ON_ROSTER: this deployment serves no checker role, so no "
            "acceptance check could be authored and no gate could be honest. Fix "
            "WORKSHOP_ROLES; there is nothing to fall back to.")
    return checkers[0]

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _files(n: int) -> str:
    """"1 file" / "6 files". A role's note is now reviewer-facing text on the pull
    request body, not an internal status string, so it reads like prose."""
    return f"{n} file" + ("" if n == 1 else "s")


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
    route: dict | None = None              # the routing verdict (preset, rule, agents, ...)
    review: dict | None = None             # the review orchestrator's verdict
    # Why each round was sent back, captured AS IT HAPPENS. `review` is overwritten by
    # each round, so the reason a re-implement pass was ordered is gone by the time the
    # run ends; this keeps it for the PR narrative and the round comment.
    retry_reasons: list[dict] = field(default_factory=list)
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
    # A read-only review run points the target's authored check at the TARGET's work.
    _review_work_dir: str | None = None
    # Set ONLY by the test-only FixtureExecutor: the work is a stub, so the LLM
    # reviewer abstains rather than judging something that implements nothing.
    _offline_double: bool = False
    # Loop-engineering: the validator AUTHORS its own acceptance test against the
    # live endpoint each run (Compartment-2 generate-verify), rather than running a
    # pinned contract. The engine runs THIS file and reads its real exit code, so
    # the fail-loud spine holds (real execution, never a fabricated pass). Null on the
    # fixture/offline path, which keeps the shipped grading contract as its floor.
    _acceptance_test_file: str | None = None
    _explicit_agents: bool = False
    _preset_req: str | None = None   # a starting point, if one was chosen
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
               options: dict | None = None, preset: str | None = None) -> Run:
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
            run._preset_req = preset
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
            # Persist the verdict where a LATER session can still read it. In the
            # `finally` deliberately: every exit path (passed, failed,
            # needs_human, engine error, stall) has to leave an answer behind, and
            # the one that matters most is the failure an attendee wants to ask
            # about after their session expired. Never raises; see run_store.
            try:
                # The tail of the run's own log rides along, because a verdict
                # without it answers "what happened" but not "why". It is NOT added
                # to public_result: that is the API contract the console renders, and
                # the log is already on the live run there. This is the durable copy
                # for the run whose session is gone (diagnose.py reads it).
                saved = {**public_result(run),
                         "events": (run.events or [])[-_PERSIST_LOG_TAIL:]}
                run_store.save(_RUNS_DIR, run.run_id, saved, run.log)
            except Exception as exc:  # noqa: BLE001 (history is not the verdict)
                run.log(f"run state not persisted: {exc}", "warn")
            # Two-bucket terminal model: a deterministic failure stays
            # `failed` (resubmit won't help); a transient one is re-graded to
            # `needs_human` so a human can resume rather than just see "failed".
            if run.status == "failed" and not _is_permanent(run.fail_reason):
                run.status = "needs_human"
                run.log(f"transient failure ({run.fail_reason}) -> needs_human "
                        "(a human can resume; resubmit may succeed)", "warn")
            # The engine starts NOTHING, so there is nothing here to stop. If the
            # deliverable needs to run, the validator's authored check starts it, and
            # `reviewer.run_gate` tears that whole process group down when the check
            # ends: the only place a started service can leak is the only place one is
            # started. The old replay-server pool lived here and is gone with the
            # deterministic builders that populated it.

    # Phase 1, deterministic. Admission validates AND ROUTES: the workflow
    # registry decides which agents this task dispatches (an unknown
    # an unknown preset fails loud, never a guess).
    def _admission(self, run: Run) -> bool:
        run.phase, run.status = "admission", "queued"
        # A starting point supplies its request text when the caller sent none, so
        # "pick a preset and go" works. `your-own` ships an EMPTY task on purpose: it
        # exists for the attendee's own sentence, so it still fails loud here rather
        # than dispatching roles against nothing.
        if not run.task.strip() and run._preset_req:
            try:
                run.task = presets.default_task(run._preset_req)
            except presets.RouteError:
                run.task = ""
        if not run.task.strip():
            run.status, run.fail_reason = "failed", "EMPTY_TASK"
            run.log("admission rejected: empty task", "error")
            return False
        # Routing picks ROLES and nothing else. The request text is the attendee's,
        # whatever it is, so there is nothing here to classify: naming the roles (or a
        # starting point that names them) is explicit, and anything else fails loud
        # rather than inventing a task.
        try:
            route = presets.resolve(
                task=run.task,
                preset=run._preset_req,
                roles=list(run.agents) if run._explicit_agents else None)
        except presets.RouteError as exc:
            run.status, run.fail_reason = "failed", str(exc)
            run.log(f"admission rejected: {exc}", "error")
            return False
        run.agents = list(route.agents)
        run.route = route.public()
        run.roles = {a: ROLE_BY_AGENT[a] for a in run.agents}
        run.progress = {a: RoleResult(agent=a, role=run.roles[a]) for a in run.agents}
        # Recompute the active count from the source of truth (no drifting counter).
        active = self.active_count(exclude=run.run_id)
        if active >= self.max_concurrent:
            run.status, run.fail_reason = "failed", "CONCURRENCY_LIMIT"
            run.log(f"admission rejected: {active} runs active (limit {self.max_concurrent})", "error")
            return False
        run.log(f"admitted + routed: {route.rule} -> agents {run.agents}")
        return True

    # Phase 2, deterministic, real file reads. Hydration reads the task spec, the
    # module, AND each dispatched role's harness steering file (the same files an
    # attendee edits) because those files drive what agent_execution builds.
    def _hydrate(self, run: Run) -> bool:
        run.phase, run.status = "context_hydration", "running"
        # There is NO sample module and NO pinned contract to hydrate: the request is
        # whatever the attendee typed, so the only context that exists before dispatch
        # is each routed role's own steering (its identity and its skill).
        # Hydrate each dispatched role's harness file so the build is provably
        # steered by it. Fail closed if a routed role has no steering.
        harness: list[str] = []
        for agent_id in run.agents:
            path = harness_config.harness_file(agent_id)
            if os.path.isfile(path):
                harness.append(f"{agent_id} ({os.path.basename(path)}, "
                               f"{len(open(path, encoding='utf-8').read())}B)")
            else:
                run.status, run.fail_reason = "failed", f"HARNESS_MISSING:{agent_id}"
                run.log(f"context hydration failed: no harness file for {agent_id}", "error")
                return False
        run.log("hydrated harness: " + ", ".join(harness))
        return True

    # Phase 3, deterministic, fail-closed (the pre-flight discipline)
    def _preflight(self, run: Run) -> bool:
        run.phase = "pre_flight"
        # Nothing about the WORK can be checked before dispatch: the request is the
        # attendee's and no answer exists in this repository to verify against. What
        # can be checked is that the machine is ready: every routed role has steering
        # and a wired runtime. Those are the fail-closed gates.
        checks: list[tuple[str, Any]] = []
        for agent_id in run.agents:
            checks.append((f"HARNESS_MISSING:{agent_id}",
                           lambda a=agent_id: os.path.isfile(harness_config.harness_file(a))))
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
        run.log("pre-flight green: every routed role has steering and a wired runtime")
        return True

    def _review_target(self, run: Run) -> Run | None:
        """Resolve the run a review workflow inspects: explicit option, else the most
        recent passed run whose work is still on disk.

        Reviewable means "there is work to look at and a check that was written for
        it", which is all the engine can know about any deliverable."""
        def _reviewable(r: Run) -> bool:
            authored = getattr(r, "_acceptance_test_file", None)
            return bool(r.workdir and os.path.isdir(r.workdir)
                        and authored and os.path.isfile(authored))

        target_id = run.options.get("target_run")
        if target_id:
            t = self._runs.get(target_id)
            return t if t and _reviewable(t) else None
        candidates = [r for r in self._runs.values()
                      if r.status == "passed" and r.run_id != run.run_id and _reviewable(r)]
        return max(candidates, key=lambda r: r.created_at, default=None)

    # ----------------------------------------------------------- runtime step
    # The shipped generation step: the role's coding-agent CLI runs INSIDE its
    # deployed AgentCore Runtime, WRITES its artifact file there, and the engine
    # reads THAT file back over the command shell (never a stdout-scraped block).
    def _runtime_cli(self, run: Run, agent_id: str, role: RoleResult, prompt: str,
                     model: str, artifact_rel: str | None = None) -> dict[str, Any]:
        """Run ``agent_id``'s CLI INSIDE its deployed AgentCore Runtime.

        Dispatches over the command shell via ``runtime_exec`` against the role's
        wired runtime ARN; the agent works in ``/mnt/s3files/<run_id>``. Raises if
        the role has no wired runtime: fail loud, never local.

        ``artifact_rel`` names ONE file to read back and require, and is used only
        where a filename is genuinely part of the contract: the validator's authored
        check. Builders pass nothing, because the engine does not decide what files
        their work consists of; their output is read as a whole tree afterwards and
        an empty tree is the failure signal (``_require_work``)."""
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

        # A single coding-agent CLI turn is occasionally flaky: it can end without
        # writing (or writing an empty) artifact even though the runtime is healthy
        # (a stopped-early TUI, a mid-settle read). That surfaces as a
        # RoleExecutionError which, on the last routed role, escalates the WHOLE run
        # to needs_human. So retry the dispatch ONCE, in-role, on a fresh shell,
        # before letting the failure bubble: a second clean turn is far cheaper than
        # a human resubmit, and this is still fail-loud (a second empty artifact
        # raises exactly as before, no fabrication). The engine's separate
        # review-loop re-implement pass is a DIFFERENT lever (a red gate on real
        # output); this catches the turn that produced no output at all.
        _last_exc: Exception | None = None
        result = None
        for _attempt in range(2):
            collected.clear()
            try:
                result = runtime_exec.run_in_runtime(
                    runtime_arn=arn, agent_id=agent_id, prompt=prompt,
                    run_subdir=run.run_id, artifact_rel=artifact_rel,
                    model=llm.resolve(model),
                    region=os.environ.get("WORKSHOP_BEDROCK_REGION", "us-west-2"),
                    on_line=on_line, timeout_s=HARNESS_ROLE_TIMEOUT_S)
                break
            except runtime_exec.RoleExecutionError as exc:
                _last_exc = exc
                if _attempt == 0:
                    run.log(f"{agent_id}: dispatch produced no artifact "
                            "-> one bounded re-dispatch on a fresh shell", "warn")
                    run.term(agent_id, "echo 're-dispatching (empty artifact on the "
                                       "first turn); a fresh shell gets one more try'")
        if result is None:
            raise _last_exc  # both turns produced no artifact: fail loud, unchanged
        role.runtime_arn = arn
        role.runtime_session_id = result.get("session_id")
        # A named artifact (the validator's check) is persisted where the local path
        # would put it, so the gate reads it unchanged. Builders name nothing: their
        # whole tree is read back separately, exactly as they wrote it, with no
        # rewriting of their code. The engine does not edit an agent's output.
        if artifact_rel:
            dest = os.path.join(run.roledir(agent_id), artifact_rel)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(result["artifact"])
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

    def _cli_backend_server(self, run: Run, role: RoleResult) -> None:
        """The backend role's CLI (running INSIDE its deployed Runtime) builds the
        service the TASK asks for, in whatever shape and language it judges right.

        Nothing here tells the agent what to build, and nothing here reads a
        particular file back: the prompt carries the user's request, the role's own
        steering and skill, and the working directory. The engine's only requirement
        is that the role wrote SOMETHING (``_require_work`` raises on an empty tree),
        which is what lets one workshop cover every kind of request without the
        repository knowing the answer to any of them."""
        feedback = ""
        if run.iterations > 1 and run.review:
            failed = [c["detail"] for c in (run.review.get("gate") or {}).get("checks", [])
                      if not c.get("passed")]
            failed += list(run.review.get("reasons") or [])
            if failed:
                feedback = ("\n\nPrevious round's review REQUESTED CHANGES on the "
                            "pull request. Address each point:\n"
                            + "\n".join(f"- {d}" for d in failed))
        # The dispatched role, not a fixed id: role.agent is whichever role the
        # roster serves for this capability, and its default model is the registry's.
        agent_id = role.agent
        model = self._role_model(run, agent_id, roles.get(agent_id).default_model)
        # Read-only material for the run is staged at <run_id>-skill/; the agent works
        # in its own writable <run_id>/ workdir (set as cwd by runtime_exec). Name the
        # paths the agent will actually see in its container.
        import runtime_stage  # noqa: PLC0415 (lazy; the wirable mount root)
        staged = runtime_stage.skill_path(run.run_id)
        prompt = (
            "You are the backend implementer role in a multi-agent build. Read "
            "CLAUDE.md in this directory for your role, and read the "
            f"`{staged}/skills/backend-engineering/SKILL.md` harness staged for this "
            "run (also baked at ~/skills/backend-engineering/SKILL.md) and apply it.\n\n"
            f"THE REQUEST: {run.task}\n\n"
            "Decide everything else yourself: the language, the framework, the files, "
            "the structure, the protocol. Nobody has prescribed a shape. Read the "
            f"request carefully; any material it refers to is staged read-only under "
            f"{staged} . Write your work in THIS directory (it is yours), and use as "
            "many files as the job deserves.\n\n"
            + _RUNNABLE_RULE
            + "\nDo not leave a long-running server in the foreground of your own "
              "session; finish your turn." + feedback)
        result = self._runtime_cli(run, agent_id, role, prompt, model)
        self._require_work(run, agent_id, result)
        run.term(agent_id, "ls -la")

    def _cli_frontend_work(self, run: Run, endpoint: str, role: RoleResult) -> str:
        """The frontend role builds whatever interface the TASK asks for and declares
        how to serve it. The engine reads the work tree back.

        Nothing here says what to build or names a file. The one hard rule that
        remains is not a use-case assumption but a BROWSER fact: a page must resolve
        its backend address at runtime, because `localhost` in a browser means the
        viewer's own machine and a build-time URL is dead the moment the page moves.
        Everything else, the framework, the layout, the files, is the agent's."""
        agent_id = role.agent
        model = self._role_model(run, agent_id, roles.get(agent_id).default_model)
        import runtime_stage  # noqa: PLC0415 (lazy; the wirable mount root)
        staged = runtime_stage.skill_path(run.run_id)
        backend = (f"A backend for this run is live at {endpoint} , and you can call "
                   "it while you work.\n"
                   if endpoint else
                   "No backend is running for this run.\n")
        prompt = (
            "You are the frontend builder role in a multi-agent build. Read "
            "AGENTS.md in this directory for your role, and read the "
            f"`{staged}/skills/frontend-design/SKILL.md` harness staged for this run "
            "and apply it.\n\n"
            f"THE REQUEST: {run.task}\n\n"
            + backend +
            "Decide everything yourself: the files, the structure, the framework, the "
            "styling, the interactions. Nobody has prescribed a shape or a filename. "
            f"Any material the request refers to is staged read-only under {staged} ; "
            "if the request points at an existing interface, read it and carry it "
            "forward rather than starting generic. Write your work in THIS directory.\n\n"
            "TWO rules, and they are browser facts rather than preferences:\n"
            "- RESOLVE the backend address at RUNTIME; never hardcode a host or port "
            "as the only option. Take an explicit override first (a query parameter), "
            "then a value a host page injected, then default to the page's OWN ORIGIN. "
            "A page is normally served by the same process that answers its calls, so "
            "same-origin must work with no configuration. `localhost` inside a browser "
            "means the machine the BROWSER runs on, so a baked loopback URL reaches "
            "nothing once the page moves. Show the resolved address in the UI.\n"
            "- Every value a user sees comes from a backend call. Render the response, "
            "or the backend's own error; never compute an answer locally and never "
            "invent one to fill a gap.\n\n"
            + _RUNNABLE_RULE)
        result = self._runtime_cli(run, agent_id, role, prompt, model)
        self._require_work(run, agent_id, result)
        run.term(agent_id, "ls -la")

    def _read_work_tree(self, run: Run, agent_id: str) -> int:
        """Pull a role's WHOLE work tree back from its runtime workspace.

        The engine does not know which files a role decided to write, so it reads
        the directory rather than a name list: whatever the agent produced IS the
        deliverable. (The manifest came back through the standard artifact read;
        this collects everything around it.) Local-mount and fixture paths already
        share the filesystem, so only the deployed-runtime path needs a transfer.

        Returns the number of files now on disk for that role, which the caller
        uses to fail loud on an empty tree: a role that wrote nothing must never
        look like a role that wrote something.

        The role's directory is REPLACED, not merged into. A re-implement round
        reads the workspace again, and a file round 1 wrote that round 2 replaced
        or deleted would otherwise linger here as a stale copy. A live run showed
        exactly that: round 2 rewrote `server.py`, one role's directory still held
        round 1's version, and compose reported a CONFLICT between two rounds of
        the same file rather than between two roles.
        """
        dest_root = run.roledir(agent_id)
        if self.executor.name == "agentcore":
            import runtime_config  # noqa: PLC0415
            import runtime_exec  # noqa: PLC0415
            if os.environ.get("WORKSHOP_S3FILES_DIR"):
                # Local mount seam: the runtime workspace IS a local dir; copy it.
                # Apply the SAME exclusions the deployed read-back applies, or the two
                # seams disagree about what a role "wrote". They did: a local run
                # counted 20 files for a role whose real output was one, because the
                # copy swept in .git and its 15 sample hooks. That number is the
                # engine's only measure of a role's work, and it appears on the pull
                # request, so an inflated count hides a role that produced nothing.
                import runtime_stage  # noqa: PLC0415
                import runtime_exec  # noqa: PLC0415
                src = os.path.join(runtime_stage.mnt_root(), run.run_id)
                if os.path.isdir(src):
                    self._clear_transferred(dest_root)
                    shutil.copytree(
                        src, dest_root, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*runtime_exec._TREE_EXCLUDES))
            else:
                hit = runtime_config.pick(agent_id)
                if hit:
                    try:
                        tree = runtime_exec.read_tree_from_runtime(
                            hit[0], run.run_id, ".",
                            region=os.environ.get("WORKSHOP_BEDROCK_REGION", "us-west-2"))
                    except Exception as exc:  # noqa: BLE001 (reported, then counted)
                        run.log(f"{agent_id}: work tree read-back failed: {exc}", "warn")
                        tree = {}
                    if not tree:
                        # A read-back that returns NOTHING is ambiguous: either the
                        # role really wrote nothing, or the transfer failed. The
                        # difference matters enormously, because the second is a
                        # TRANSPORT problem being reported to the attendee as
                        # "your agent produced no work", which is what a live run
                        # did when a dependency tree overflowed the channel.
                        #
                        # So ask the runtime directly whether files exist. If they
                        # do, this is a transfer failure and it is worth one more
                        # attempt on a fresh shell; if the workspace is genuinely
                        # empty, fall through and let the empty-tree guard say so.
                        listing = ""
                        try:
                            listing = runtime_exec.list_tree_in_runtime(
                                hit[0], run.run_id,
                                region=os.environ.get("WORKSHOP_BEDROCK_REGION",
                                                      "us-west-2"))
                        except Exception:  # noqa: BLE001 (best effort probe)
                            listing = ""
                        if listing.strip():
                            run.log(f"{agent_id}: the runtime HAS files but the "
                                    f"transfer returned none; retrying the read-back "
                                    f"once (workspace: {listing.strip()[:160]})",
                                    "warn")
                            try:
                                tree = runtime_exec.read_tree_from_runtime(
                                    hit[0], run.run_id, ".",
                                    region=os.environ.get("WORKSHOP_BEDROCK_REGION",
                                                          "us-west-2"))
                            except Exception as exc:  # noqa: BLE001
                                run.log(f"{agent_id}: read-back retry failed: {exc}",
                                        "warn")
                    # Only when the read-back actually returned something: an empty
                    # tree is a FAILED read, and clearing on that would destroy the
                    # previous round's evidence and turn a transport failure into a
                    # role that "wrote nothing".
                    if tree:
                        self._clear_transferred(dest_root)
                    for rel, data in tree.items():
                        dest = os.path.join(dest_root, rel)
                        if (os.path.commonpath([os.path.abspath(dest),
                                                os.path.abspath(dest_root)])
                                != os.path.abspath(dest_root)):
                            continue  # never write outside the role's own directory
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "wb") as f:
                            f.write(data)
        n = self._authored_count(run, agent_id)
        run.log(f"{agent_id}: work tree read back ({n} files)")
        return n

    @staticmethod
    def _clear_transferred(dest_root: str) -> None:
        """Drop a previous round's transferred files before writing this round's.

        Only the paths that TRANSFER a tree call this. A re-implement round reads
        the workspace again, and a file round 1 wrote that round 2 replaced or
        deleted would otherwise linger as a stale copy: a live run had round 2
        rewrite `server.py` while one role's directory still held round 1's, so
        compose reported a CONFLICT between two ROUNDS rather than between two
        roles.

        The harness the engine installed (steering + skills) is kept: it is not
        part of the read-back, and reinstalling it is not this function's job.
        """
        if not os.path.isdir(dest_root):
            return
        for name in os.listdir(dest_root):
            if name == "skills" or name in _COMPOSE_SKIP_NAMES:
                continue
            victim = os.path.join(dest_root, name)
            if os.path.isdir(victim):
                shutil.rmtree(victim, ignore_errors=True)
            else:
                try:
                    os.remove(victim)
                except OSError:
                    pass

    def _harness_installed_paths(self, agent_id: str) -> set[str]:
        """Role-relative paths that the HARNESS put in the workspace, not the role.

        ``install_harness`` copies the steering file and any ``harness:setup``
        skills into the role's own working directory before the CLI runs, so the
        directory is never empty by the time the role finishes. Counting those as
        "work" made the empty-tree guard unreachable, which is the one thing that
        must never happen: a role that wrote nothing would then reach a green gate
        and an empty commit, and the repository has no builder to fall back on.
        """
        paths = {harness_config.steering_filename(agent_id), ".mcp/servers.jsonl"}
        try:
            src = harness_config.harness_file(agent_id)
            setup = harness_config.parse_setup_spec(src)
        except Exception:  # noqa: BLE001 (no steering: nothing to exclude)
            return paths
        for skill_path in setup.get("skills", []):
            full = os.path.join(os.path.dirname(src), skill_path)
            base = os.path.basename(full.rstrip(os.sep))
            if not os.path.isdir(full):
                continue
            for dirpath, _dirs, files in os.walk(full):
                for f in files:
                    inner = os.path.relpath(os.path.join(dirpath, f), full)
                    paths.add(os.path.join("skills", base, inner))
        return paths

    def _authored_count(self, run: Run, agent_id: str) -> int:
        """Files in the role's tree that the ROLE wrote (harness files excluded)."""
        root = run.roledir(agent_id)
        installed = self._harness_installed_paths(agent_id)
        n = 0
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                rel = os.path.relpath(os.path.join(dirpath, f), root)
                if rel.replace(os.sep, "/") not in {
                        p.replace(os.sep, "/") for p in installed}:
                    n += 1
        return n

    def _require_work(self, run: Run, agent_id: str, result: dict[str, Any]) -> int:
        """Read a role's work tree back and REQUIRE that it wrote something.

        This is the fail-loud guard that replaces the old named-artifact check. When
        the engine stopped naming the file, it also lost "the file is missing" as a
        signal, and a CLI that exits 0 having written nothing would otherwise sail
        through to an empty commit. A role that produced no files is a role failure,
        with the CLI output tail attached so the terminal shows why.

        The count EXCLUDES the steering and skills that ``install_harness`` staged
        into the same directory. Counting them made this guard dead code: every
        role's directory already held at least the steering file, so ``n == 0``
        could not happen and a silent no-op role passed as a builder.
        """
        tail = "\n".join(result.get("lines", []))[-600:]
        if result.get("exit") not in (0, None):
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: {agent_id} exited {result['exit']}; "
                f"tail:\n{tail}")
        return self._require_tree_nonempty(run, agent_id, tail)

    def _require_tree_nonempty(self, run: Run, agent_id: str,
                               tail: str = "") -> int:
        """Read the tree back and RAISE if the role authored nothing.

        Separate from ``_require_work`` so the BUILDER CLOSURES can enforce it
        directly. They used to call ``_read_work_tree``, which only counts, so the
        guard depended on whichever producer happened to run underneath; that left
        the enforcement dependent on the execution seam rather than on the rule.
        Emptiness is a role failure regardless of how the role was executed.
        """
        n = self._read_work_tree(run, agent_id)
        if n == 0:
            suffix = f"; tail:\n{tail}" if tail else ""
            # Say WHICH failure this is. "wrote no files" sent a facilitator
            # looking at the agent when a live run had actually written a complete
            # deliverable the transfer could not carry, so name the transport case
            # explicitly when the runtime still has files.
            hint = ""
            if self.executor.name == "agentcore" and not os.environ.get(
                    "WORKSHOP_S3FILES_DIR"):
                try:
                    import runtime_config  # noqa: PLC0415
                    import runtime_exec  # noqa: PLC0415
                    hit = runtime_config.pick(agent_id)
                    if hit:
                        listing = runtime_exec.list_tree_in_runtime(
                            hit[0], run.run_id,
                            region=os.environ.get("WORKSHOP_BEDROCK_REGION",
                                                  "us-west-2"))
                        if listing.strip():
                            hint = ("; NOTE: the runtime workspace is NOT empty, so "
                                    "this is a read-back/transport failure rather "
                                    "than an agent that produced nothing. Resubmit "
                                    f"the same request. Workspace: "
                                    f"{listing.strip()[:200]}")
                except Exception:  # noqa: BLE001 (diagnostic only)
                    pass
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: {agent_id} finished but wrote no files, so "
                f"there is nothing to review or run{hint}{suffix}")
        return n

    def _cli_validator_authors_test(self, run: Run, endpoint: str,
                                    role: RoleResult) -> str:
        """The validator role AUTHORS the acceptance check for THIS deliverable, and
        the engine reads that file back to run it.

        This is the whole of validation: agentic, per task, decided by the checker.
        The validator is told the request and the live URL and NOTHING about what
        acceptable means; it chooses the checks, the language, and the shape. The
        engine then runs the file and reads its real exit code, so the fail-loud
        spine holds without the repository knowing any answer: a red check can never
        be a pass, and nothing is fabricated. Maker (the builders) != checker (the
        validator, which never edits the work, only probes it)."""
        checker = _validator_agent()
        model = self._role_model(run, checker, roles.get(checker).default_model)
        feedback = ""
        if run.iterations > 1 and run.review:
            failed = [c["detail"] for c in (run.review.get("gate") or {}).get("checks", [])
                      if not c.get("passed")]
            failed += list(run.review.get("reasons") or [])
            if failed:
                feedback = ("\n\nA previous round requested changes; make sure your "
                            "check covers each point:\n"
                            + "\n".join(f"- {d}" for d in failed)
                            + "\n\nRe-read those failures critically before you write "
                              "this round's check. If a failure was YOUR CHECK's fault "
                              "rather than the work's (it probed a service it never "
                              "started, assumed a port or a path the deliverable does "
                              "not use, or asserted something the request never asked "
                              "for), fix that in your check now. A check that blames "
                              "working software is the worst outcome available to you: "
                              "the builders will be sent to change code that was "
                              "already correct.")
        live = (f"The deliverable is running at {endpoint} .\n"
                if endpoint else
                "The deliverable does not expose a running service this round.\n")
        prompt = (
            "You are the validator role in a multi-agent build, and you are the "
            "checker in a maker-checker pair. Read CLAUDE.md in this directory for "
            "your role.\n\n"
            f"THE REQUEST the other roles were given: {run.task}\n\n"
            + live +
            "AUTHOR the acceptance check for this deliverable and save it as "
            f"`./{_ACCEPTANCE_CHECK}` in this directory: ONE self-contained "
            "EXECUTABLE file, starting with a shebang line, in whatever language you "
            "judge fits (anything installed in this container works).\n\n"
            "YOU decide what 'acceptable' means for this request. Nobody has given "
            "you a checklist, a contract, or a list of required checks, because only "
            "you have seen this particular task. Read the request, look at what was "
            "actually built in the shared workspace, and encode the checks that would "
            "convince a skeptical engineer that the request was met. Prefer evidence "
            "over assumption: probe the running deliverable over the wire where it "
            "can prove something, and inspect the files where it cannot.\n\n"
            "Contract for your file: exit 0 to accept and nonzero to reject; print "
            "one line per check so a human can read what you verified; read the "
            "deliverable's URL from the `DELIVERABLE_URL` env var"
            + (f" (it will be {endpoint!r})" if endpoint else "") + ". Do not soften "
            "a real failure, and do not rubber-stamp. You do NOT edit the work; you "
            "only check it. Write ONLY the file; do not run it.\n\n"
            "IF THE DELIVERABLE IS A SERVICE, YOUR CHECK MUST START IT ITSELF, wait "
            "for it to accept connections, and stop it when it is done. NOTHING ELSE "
            "STARTS IT FOR YOU: no process is running when your check begins, so a "
            "check that only probes an address it did not start can never pass, and "
            "it would report a working deliverable as broken. Pick the port yourself "
            "(an unused one) rather than assuming a default, and if you cannot start "
            "it, print why and fail: that is a real finding, not a technicality."
            + feedback)
        result = self._runtime_cli(run, _validator_agent(), role, prompt, model,
                                   _ACCEPTANCE_CHECK)
        test_path = os.path.join(run.roledir(_validator_agent()), _ACCEPTANCE_CHECK)
        self._read_artifact(test_path, _ACCEPTANCE_CHECK, result)
        run.term(_validator_agent(), f"head -1 {_ACCEPTANCE_CHECK} && wc -l {_ACCEPTANCE_CHECK}")
        return self._gate_dir_check_path(run, test_path)

    def _gate_dir_check_path(self, run: Run, authored: str) -> str:
        """Reunite the authored check with the work it was authored BESIDE.

        Every role shares ONE directory in the runtime workspace
        (``/mnt/s3files/<run_id>``), so the validator writes its check next to the
        builders' files and naturally addresses them as siblings: a check that does
        ``os.path.dirname(__file__) + "/server.py"``, or plain ``./server.py``, is
        correct where it was written. The engine then reads each role's tree back
        into a SEPARATE ``role-<agent>`` directory (which compose needs, so each
        role's contribution is attributable), and that split leaves the check alone
        in the validator's directory with the deliverable one level away.

        Running it there fails every file and import check and starts no service, so
        a CORRECT deliverable is graded RED and the loop burns its bounded retry to
        ``needs_human``. Verified on a live event box: the same check scored 0/4 in
        the validator's directory and 45/0 beside the work.

        So build one gate directory that looks like the workspace the check was
        authored in: every role's files, plus the check at its root. The per-role
        dirs are untouched (compose still attributes each file to its author), and
        the gate stays exactly what it was: run THAT executable, read its real exit
        code. Nothing here inspects or grades the work.
        """
        gate_dir = os.path.join(run.workdir, "gate")
        # REBUILD it, never add to it. A re-implement round runs this again, and a
        # leftover from the previous round is a file the new check never saw: it
        # could satisfy a check the fixed deliverable no longer satisfies, which
        # would make round 2 pass on round 1's evidence. A live run left round 1's
        # `issues.db` (created when the check STARTED the service) sitting here for
        # round 2.
        shutil.rmtree(gate_dir, ignore_errors=True)
        os.makedirs(gate_dir, exist_ok=True)
        for agent_id in run.agents:
            src = run.roledir(agent_id)
            if not os.path.isdir(src):
                continue
            for dirpath, dirnames, filenames in os.walk(src):
                dirnames[:] = [d for d in dirnames if d not in _COMPOSE_SKIP_DIRS]
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), src)
                    # The gate uses a NARROWER exclusion than compose, on purpose.
                    # Compose also drops things that are merely ugly in a pull
                    # request (a database the service created), but the gate must
                    # see the workspace the check was authored against: a check
                    # that opens an existing database, or reads a file the service
                    # wrote, would fail on work that is fine. Only OUR harness is
                    # withheld here, plus a stale check a builder read back from
                    # the shared mount, which must not shadow the one the validator
                    # authored this round (copied in below).
                    if _gate_excluded(rel) or rel == _ACCEPTANCE_CHECK:
                        continue
                    dest = os.path.join(gate_dir, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(os.path.join(dirpath, fn), dest)
        staged = os.path.join(gate_dir, os.path.basename(authored))
        shutil.copy2(authored, staged)
        os.chmod(staged, os.stat(staged).st_mode | 0o755)
        run.log(f"gate workspace assembled at {gate_dir} "
                f"({sum(len(f) for _, _, f in os.walk(gate_dir))} files, the check beside the work)")
        return staged

    def _write_validator_report(self, run: Run, role: RoleResult,
                                grade_tail: str) -> None:
        """FIXTURE-ONLY role artifact: a deterministic note that the offline
        grading floor ran (the shipped path's validator AUTHORS
        acceptance_test.py instead; verdicts live on the PR, not in files)."""
        report_path = os.path.join(run.roledir(_validator_agent()), "validation_report.md")
        if self.executor.name == "fixture":
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("validation report: the grading contract ran; "
                        "see the grading output above.\n")
            run.add_event(_validator_agent(), {"kind": "text",
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
        # The builders-then-checker join is the GRAPH's, not a hand-rolled latch: the
        # roles are nodes and "every builder -> the checker" is an edge set with an
        # explicit AND condition (see role_graph). This used to be a countdown Event,
        # and the version of it that shipped deadlocked whenever a role ran the wrong
        # closure, because the release and the wait were two places that had to agree.
        endpoint: dict[str, str] = {}

        # Review workflow: no building. The validator boots the target run's
        # artifact and the review orchestrator judges it in finalization.
        if run.route and run.route.get("read_only"):
            return self._execute_review(run)

        def install_harness(agent_id: str) -> None:
            """The role installs its OWN harness: it writes the steering file into
            its container, then applies the OPTIONAL ``harness:setup`` block.

            The named file is the default configuration, but the harness is the
            attendee's to extend. Anything in ``harness:setup`` (MCP servers,
            extra skills, install commands) is set up here, in the role's real
            terminal, exactly as a developer would extend their own harness."""
            src = harness_config.harness_file(agent_id)
            # The dest filename each harness reads from cwd: Claude Code reads
            # CLAUDE.md (backend AND validator, both Claude Code), opencode reads
            # AGENTS.md. The validator's acceptance-contract steering lands as its
            # own CLAUDE.md in the workdir.
            rel = harness_config.steering_filename(agent_id)
            dest_dir = os.path.dirname(rel)
            mkdir = f"mkdir -p {dest_dir} && " if dest_dir else ""
            run.term(agent_id, f"{mkdir}cp {json.dumps(src)} {rel} && head -4 {rel}")
            setup = harness_config.parse_setup_spec(src)
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
            # The backend role builds whatever the REQUEST asks for. There is no module
            # to probe and no shape to conform to: it reads its steering and its skill,
            # reads the request, and decides.
            # Whichever role the roster serves for this capability, not a fixed id:
            # role.agent is the agent actually dispatched into this closure.
            install_harness(role.agent)
            # PRODUCE step: the ONE thing that varies by executor. Shipped
            # (AgentCoreExecutor): the role's CLI runs INSIDE its deployed Runtime
            # and writes whatever the task calls for. Tests (FixtureExecutor): a
            # content-free stub, in-process, no model. No other producer exists; a
            # real-only shipped path fails loud.
            if self.executor.name == "agentcore":
                self._cli_backend_server(run, role)
            elif self.executor.name == "fixture":
                self.executor.produce(run, role.agent, role)
            else:
                raise RuntimeError(_NO_PRODUCER_ERROR)
            # RAISES on an empty tree, whatever produced it: a builder that wrote
            # nothing is a role failure, never a builder that "built" something.
            n = self._require_tree_nonempty(run, role.agent)
            role.note = f"built the backend side of this request ({_files(n)})"
            run.log(f"backend: wrote {n} files; the validator's check decides whether "
                    "they answer the request")

        def validator(role: RoleResult) -> None:
            install_harness(role.agent)
            # The checker in the maker-checker pair. It AUTHORS the acceptance check
            # for this deliverable; the engine executes that file in finalization and
            # reads its real exit code. The engine starts nothing itself: if the work
            # is a service, the authored check stands it up, because only the check
            # knows what running means for THIS deliverable. That is why no protocol,
            # port, or language appears anywhere on this path.
            # No wait here: the graph does not start this node until every routed
            # builder has finished (edge condition ``all_builders_done``), so by the
            # time this runs the tree it grades is complete.
            if self.executor.name == "agentcore":
                run._acceptance_test_file = self._cli_validator_authors_test(
                    run, endpoint.get("url", ""), role)
            elif self.executor.name == "fixture":
                run._acceptance_test_file = self.executor.produce(
                    run, role.agent, role)
            else:
                raise RuntimeError(_NO_PRODUCER_ERROR)
            role.note = "authored the acceptance check for this deliverable"
            run.log("validator: authored the acceptance check; its real exit code is "
                    "the gate")

        def frontend(role: RoleResult) -> None:
            # The backend role dispatches its CLI into a deployed Runtime, which can
            # take a while; wait generously for the endpoint before wiring the UI.
            # A backend, when one is routed, may be worth reading before building
            # against it. This wait is a courtesy, not a dependency: the frontend no
            # longer needs a live endpoint, because a page must resolve its backend
            # address at runtime anyway (a browser fact), so a service-less request
            # is a perfectly good frontend request.
            install_harness(role.agent)
            if self.executor.name == "agentcore":
                self._cli_frontend_work(run, endpoint.get("url", ""), role)
            elif self.executor.name == "fixture":
                self.executor.produce(run, role.agent, role)
            else:
                raise RuntimeError(_NO_PRODUCER_ERROR)
            n = self._require_tree_nonempty(run, role.agent)
            role.note = f"built the interface this request asked for ({_files(n)})"
            run.log(f"frontend: wrote {n} files; the validator's check decides whether "
                    "they answer the request")

        # Which closure runs for a role, keyed by its CAPABILITY (registry), not by a
        # role-name string. A name typo or rename used to fall through to the default
        # and silently run the CHECKER closure for a builder, which waits on a join
        # that builder was supposed to release: a whole-run hang. So there is no
        # default: an unmapped capability fails loud, because a hang reports nothing
        # while a raise names the problem.
        work = {"backend": backend, "frontend": frontend, "validator": validator}

        def _make_dispatch(role: RoleResult, agent_id: str) -> Any:
            """The zero-arg callable one graph node runs: this role's whole turn.

            The graph decides WHEN this runs; everything inside is unchanged, including
            the executor seam, so the framework never touches what a role does or how
            its result is judged."""

            def _run_role() -> None:
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
                    capability = roles.get(agent_id).capability
                    if capability not in work:
                        raise RuntimeError(
                            f"NO_WORK_FOR_CAPABILITY:{capability} (role {agent_id}). "
                            "The roster offers a capability this engine has no closure "
                            "for; there is nothing to substitute.")
                    local_work = work[capability]
                    self.executor.dispatch(run, agent_id, role, local_work)
                    role.state = "done"
                except Exception as exc:
                    role.state, role.note = "error", f"{type(exc).__name__}: {exc}"
                    run.log(f"{role.role} errored: {exc}", "error")
                finally:
                    # No join to release: a FAILED builder still counts as finished to
                    # the graph's edge condition, so one role's exception can never
                    # become a whole-run hang (a hang reports no verdict at all, which
                    # is strictly worse than a red gate on a partial tree).
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

            return _run_role

        # Mark every routed role as started BEFORE the graph runs, so the run view shows
        # the real routed set immediately rather than filling in as nodes fire.
        for agent_id in run.agents:
            r = run.progress[agent_id]
            r.state = "working"
            r.last_beat = time.monotonic()      # first heartbeat = role started

        # Hand the schedule to Strands: builders as one parallel entry batch, the
        # checker behind an explicit AND join. The phase budget becomes the graph's own
        # execution timeout, so a wedged role is bounded by the framework instead of a
        # hand-rolled thread join.
        try:
            graph, _nodes = role_graph.build_graph(
                list(run.agents), lambda a: _make_dispatch(run.progress[a], a),
                execution_timeout=max(1.0, deadline - time.monotonic()))
            role_graph.run_graph(graph)
        except Exception as exc:                # noqa: BLE001
            # A graph that could not be built or driven is a PHASE failure, reported as
            # such. It is never a pass: any role left "working" is caught by the
            # watchdog below and the run fails loud.
            run.log(f"agent execution graph failed: {type(exc).__name__}: {exc}", "error")
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

    def _execute_review(self, run: Run) -> bool:
        """The review workflow's agentic phase: re-run the TARGET run's own authored
        check against the target's work. Nothing is built (read-only).

        The engine does not re-serve anything, because it never knew how to serve the
        target's deliverable in the first place. The target's validator wrote a check
        that knows how to prove that particular work, so reviewing means running THAT
        check again: verification by execution on the artifact under review, never a
        fresh contract invented here."""
        target = self._review_target(run)
        if not target:
            run.status, run.fail_reason = "failed", "NO_RUN_TO_REVIEW"
            return False
        run._review_target = target.run_id
        run._acceptance_test_file = getattr(target, "_acceptance_test_file", None)
        run.composed_branch = target.composed_branch
        # The check inspects the TARGET's work, not this review run's empty workdir.
        run._review_work_dir = target.workdir
        # A review of offline-double work is itself a review of a stub; carry the mark
        # so the LLM reviewer abstains rather than judging something that implements
        # nothing. Never set on a real dispatch.
        run._offline_double = getattr(target, "_offline_double", False)
        run.artifact_endpoint = getattr(target, "artifact_endpoint", "") or ""
        role = run.progress.get(_validator_agent())
        if role:
            role.state = "working"
        t0 = time.monotonic()
        # Routed-roles invariant: only a role the router actually dispatched gets a
        # terminal pane, so a read-only workflow can never fabricate a phantom pane.
        if _validator_agent() in run.agents:
            run.term(_validator_agent(), f"echo 'reviewing {target.run_id} (branch "
                                       f"{target.composed_branch or 'n/a'})'")
            authored = getattr(run, "_acceptance_test_file", None)
            if authored:
                # SHOW the check, do not run it here. `reviewer.run_gate` below is
                # the one execution whose exit code is the verdict, and it is
                # hardened for a check that starts a service (temp-file sink,
                # own process group, group teardown). `run.term` reads through a
                # PIPE with a 60s cap, so running the check here would inherit
                # that pipe into any service the check starts and block until the
                # cap: a passing check displayed as a timeout, and 60s added to
                # every review run for a terminal line nobody needs.
                run.term(_validator_agent(),
                         f"head -1 {json.dumps(authored)} && wc -l {json.dumps(authored)}")
        if role:
            role.state = "done"
            role.latency_ms = int((time.monotonic() - t0) * 1000)
            role.note = (f"re-runs {target.run_id}'s own authored check against its work"
                         if run._acceptance_test_file else
                         f"{target.run_id} left no authored check to re-run")
        run.log(f"review execution: target {target.run_id}, re-running its authored check")
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
        run.log(f"gate: running the validator's authored check (round {run.iterations})")
        gate = reviewer.run_gate(run)
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
                    # New commits appeared on a branch a reviewer may already have
                    # read, with nothing on the timeline saying why. The body cannot
                    # be rewritten (the gateway exposes no update_pull_request), so
                    # the round's story goes on as a comment, which is where an
                    # update belongs anyway.
                    said = github.post_review(run, replay.round_comment(run))
                    if said.get("error"):
                        run.log(f"round note not posted: {said['error']}", "warn")
            else:
                # The body is the run's own story (replay.py): which roles ran, what
                # the validator chose to assert, and why a second round happened.
                # A reviewer arriving from a notification cannot reach the engine log
                # or the coordinator session, so if the loop is not legible here it is
                # not legible at all. Generated from the run record, and reporting
                # only: it reads the gate's verdict, it never contributes to it.
                run.pr = github.open_pr(run, replay.narrative(run))
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
            self._ledger(run)
            return True
        why = (gate.get("summary") or "assessment requested changes")
        # Remember WHY this round was sent back, here and now. `run.review` holds only
        # the LATEST verdict, so by the time the PR narrative is written it has been
        # overwritten by the round that passed: a live 2-round run rendered "what came
        # back as feedback" and then listed round 2's approval notes, which reads as
        # the opposite of what happened. This is the only point where the causal fact
        # exists, so it is captured rather than reconstructed.
        run.retry_reasons.append({
            "round": run.iterations,
            "gate_summary": gate.get("summary") or "",
            "reasons": list((run.review or {}).get("reasons") or []),
        })
        run.log(f"changes requested ({why}) -> one bounded re-implement pass "
                "updating the same PR", "warn")
        return False

    # The composed repo is shared by every run; git allows one writer at a time
    # (index.lock), so compose is serialized across concurrent runs. A bare Lock
    # would deadlock the whole engine if one compose ever hung while holding it,
    # so this is a self-healing lease that auto-evicts a wedged holder.
    _COMPOSE_LEASE = _Lease(COMPOSE_LEASE_STUCK_S)

    def _compose_commit(self, run: Run) -> None:
        """Compose the dispatched roles' artifacts into ONE real git commit.

        The commit carries whatever the routed roles wrote, at the paths THEY chose,
        each role's tree under its own directory, plus the validator's authored
        check. The engine adds no layout of its own. The commit on a per-run branch
        is the local equivalent of finalization's PR, and the exact branch github.py
        pushes when connected.
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
        # Drop any leftover from a prior run (a file a previous run's role wrote and
        # this one did not), so the commit is exactly this run's deliverable.
        subprocess.run(["git", "-C", repo, "clean", "-fdq"], check=True, timeout=20, env=git_env)
        # Copy the deliverable at the paths the AGENTS chose. The engine renames
        # nothing: whatever the roles wrote IS the deliverable.
        #
        # Every role shares ONE directory in the runtime workspace, so each role's
        # read-back tree is a view of the SAME files. Committing one directory per
        # role therefore published the identical project two or three times (a live
        # 3-role run produced 21 files that were really 7), which makes the pull
        # request unreviewable. So compose the union ONCE, flat, exactly as it sits
        # in the workspace.
        #
        # Excluded, deliberately: the harness steering and skills (ours, not the
        # deliverable) and the run-time droppings a service leaves behind (caches,
        # and the SQLite write-ahead sidecars a started service creates: a live run
        # committed .db-wal and .db-shm). A collision between two roles writing the
        # same path is a real possibility and is reported rather than hidden.
        #
        # The authored check is excluded from the ROLES' trees too, and shipped once
        # from the validator's own artifact below. It lives in the same shared mount
        # as the deliverable, so every role reads it back as if it were their own
        # file, and on a re-implement round the builders carry the PREVIOUS round's
        # copy while the validator has just written a new one. A live 3-role run
        # therefore reported a CONFLICT on two byte-identical-looking checks that
        # were really two different rounds of the same file.
        seen: dict[str, str] = {}
        collisions: list[str] = []
        for agent_id in run.agents:
            src = run.roledir(agent_id)
            if not os.path.isdir(src):
                continue
            for dirpath, dirnames, filenames in os.walk(src):
                dirnames[:] = [d for d in dirnames if d not in _COMPOSE_SKIP_DIRS]
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), src)
                    if _compose_excluded(rel) or rel == _ACCEPTANCE_CHECK:
                        continue
                    full = os.path.join(dirpath, fn)
                    prior = seen.get(rel)
                    if prior is not None:
                        # Same path from two roles. Identical is the shared-mount
                        # norm and needs no comment. Different usually means the two
                        # roles were READ AT DIFFERENT TIMES while both were editing
                        # the one shared workspace, so one snapshot is simply older:
                        # a live run committed an 8.9KB index.html at the root while
                        # the workspace (and the file the gate actually ran against)
                        # held the 31.6KB one, because the earlier reader happened to
                        # come first in roster order.
                        #
                        # Roster order is not evidence, so prefer the NEWER file and
                        # keep the older one as the flagged copy. That makes the PR's
                        # root match what was really built and graded, while still
                        # showing a reviewer that two roles disagreed on this path.
                        if not filecmp.cmp(prior, full, shallow=False):
                            collisions.append(rel)
                            newer, older = full, prior
                            if os.path.getmtime(prior) >= os.path.getmtime(full):
                                newer, older = prior, full
                            root_dest = os.path.join(repo, rel)
                            os.makedirs(os.path.dirname(root_dest), exist_ok=True)
                            shutil.copy2(newer, root_dest)
                            seen[rel] = newer
                            dest = os.path.join(repo, f"CONFLICT-{agent_id}", rel)
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            shutil.copy2(older, dest)
                        continue
                    seen[rel] = full
                    dest = os.path.join(repo, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(full, dest)
        run.log(f"compose: {len(seen)} file(s) the agents wrote"
                + (f"; {len(collisions)} path(s) differed between roles and were "
                   f"committed under CONFLICT-<role>/: {', '.join(collisions[:5])}"
                   if collisions else ""))
        # The validator's authored check SHIPS WITH the deliverable, so the PR
        # reviewer (human or bot) can rerun the exact gate that passed. The review
        # verdict itself is NOT a committed file: it is posted on the pull request as
        # an Assessment comment, where reviews belong.
        authored = getattr(run, "_acceptance_test_file", None)
        if authored and os.path.isfile(authored):
            shutil.copy(authored, os.path.join(repo, os.path.basename(authored)))
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True, timeout=20, env=git_env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty",
                        "-m", f"{run.run_id}: {(run.route or {}).get('preset', 'run')}, "
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
                "preset": (run.route or {}).get("preset"),
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
        """Nothing to tear down: the engine starts no processes. Kept so callers
        (tests, the console on exit) have a stable lifecycle hook."""
        return None


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


# What an attendee should DO about each terminal reason. Idea from
# awslabs/aidlc-workflows v2, whose stage checkboxes say WHO IS BLOCKING at a glance
# (`[?]` awaiting you, `[R]` revising) rather than making you decode a state name.
#
# Our `needs_human` covers two very different situations: the gate stayed red on real
# work (read the check's own failing lines, the deliverable needs changing) and a role
# never produced anything (a transport or turn failure, just resubmit). Same status,
# opposite next action, and the raw token said neither.
_NEXT_ACTION = {
    "ITERATION_CAP":
        "The authored check was still red after the bounded re-implement round. Read "
        "the failing lines in gate.summary: they are the check's own output, so they "
        "name what the deliverable did not do. The PR is open with the assessment.",
    "ROLE_EXECUTION_ERROR":
        "A role's turn produced no usable work. This is usually transient: submit the "
        "SAME request again. Do not try to finish it by dispatching one role by hand.",
    "ROLE_TOTAL_FAILURE":
        "EVERY routed role failed, which points at the harness or the environment "
        "rather than the request: check that each role's runtime is wired and READY, "
        "then resubmit.",
    "ENGINE_STALL":
        "The run ended without reaching a verdict. Resubmit; if it stalls again, the "
        "engine log for this run id is the place to look.",
    "NO_RUN_TO_REVIEW":
        "A review-only request needs an earlier run to review. Submit a build first.",
    "PRESET_NOT_SPECIFIED":
        "No task text and no preset. Say what you want built, or name a preset from "
        "list_presets.",
    "NO_BUILDER_ROUTED":
        "The route selected no maker, so nothing would be built. Name a preset or a "
        "role set that includes a builder.",
    "NO_CHECKER_ROUTED":
        "The route selected no checker, so nothing would verify the work. Every build "
        "route must carry the validator.",
    "NO_ROLES_ROUTED":
        "The route selected no roles at all. Name a preset or an explicit role set.",
}


def next_action(status: str, fail_reason: str | None,
                pr: dict | None = None, pr_url: str | None = None) -> str:
    """One sentence telling the reader what to do about this outcome.

    Derived, never stored: the reason is the fact, this is how to read it. An
    unrecognised reason returns "" rather than inventing advice.

    ``pr`` matters because the most common outcome is a run that PASSED, and a passing
    run still has two very different endings: the pull request opened (go read it) or
    it did not (the work is real but stranded in a local branch, and nothing else in
    the payload says so at a glance). The PR failure is NOT a fail_reason -- the build
    genuinely succeeded -- so it can only be read from the PR result.
    """
    if status == "passed":
        pr = pr or {}
        if pr_url:
            return "Open the pull request and read the assessment comment on it."
        pr_error = str(pr.get("error") or "")
        if pr_error.startswith("PR_NO_GATEWAY"):
            return ("The build passed but no GitHub MCP Gateway is wired, so no PR was "
                    "opened and the deliverable is only on a local branch. Run "
                    "`python3 orchestrator/github.py doctor`, then resubmit.")
        if pr_error.startswith("PR_NO_CREDENTIAL"):
            return ("The build passed but the App credential did not resolve, so no PR "
                    "was opened. Re-run deploy-credential.sh, then resubmit.")
        if pr_error:
            return (f"The build passed but the PR step failed: {pr_error[:160]}. Run "
                    "`python3 orchestrator/github.py doctor`.")
        return ""
    reason = (fail_reason or "").split(":")[0].strip()
    if reason in _NEXT_ACTION:
        return _NEXT_ACTION[reason]
    if reason.startswith("RUNTIME_NOT_WIRED"):
        return ("A routed role has no wired runtime ARN. Deploy that role (Lab 1) or "
                "wire its ARN, then resubmit; the engine never falls back to a local "
                "build.")
    if reason.startswith(("UNKNOWN_PRESET", "UNKNOWN_ROLE")):
        return "That preset or role does not exist. Call list_presets for what does."
    # No PR_NO_GATEWAY branch here on purpose: a failed PR step never becomes a
    # fail_reason (the BUILD succeeded), it lands in run.pr["error"], which the
    # status == "passed" arm above reads. A branch here would be unreachable.
    return ""


def public_result(run: Run) -> dict:
    return {
        "run_id": run.run_id,
        "status": run.status,
        # The gate's summary is the authored check's own last line: the closest thing
        # to a human-readable verdict, so it belongs in the public payload.
        "gate": {"passed": bool(run.gate and run.gate["passed"]),
                 "checks": (run.gate or {}).get("checks", []),
                 "summary": (run.gate or {}).get("summary", "")},
        "pr_url": run.pr_url,
        "merge_state": run.merge_state,
        # A run rejected at admission (empty task, bad roles) has `agents` from the
        # request but no `roles` yet, so read through `roles` rather than indexing it:
        # a failed run must still render its result, not 500 the API.
        "composed_from": [run.roles[a] for a in run.agents if a in run.roles],
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
        # What to DO about this outcome, in one sentence. `needs_human` alone cannot
        # tell "the gate stayed red on real work" from "a role produced nothing",
        # and those have opposite next steps.
        "next_action": next_action(run.status, run.fail_reason, run.pr, run.pr_url),
    }
