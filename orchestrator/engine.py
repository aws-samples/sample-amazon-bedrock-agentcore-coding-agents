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

The central design rules are:

  * **Registry-derived routing.** ``roles.py`` declares the available roles once,
    and ``presets.py`` selects capabilities rather than agent ids. ``chat.py``
    generates dispatch tools from the active roster and lets the Strands
    coordinator clarify an ambiguous request. Only selected roles are dispatched.
  * **A separate checker and reviewer** (``reviewer.py``). The build side never
    approves its own work: the validator authors an executable check for the pull
    request in front of it and real execution supplies the gate verdict. The
    reviewer then posts its Assessment on that pull request. A red gate or a
    requested change returns only the owning role through one bounded repair pass.
  * **One pull request per role, each merged on its own** (``github.py``). Every
    builder owns a named branch and opens a pull request against the repository's
    DEFAULT branch. There is no assembled candidate, no run-scoped integration
    branch, no merge queue, and no separate final pull request: a pull request is
    the unit a person reviews and merges, so the engine does not invent a second
    unit above it. Each one is checked and reviewed against the default branch AS
    IT STANDS, which is what keeps a cross-role defect catchable: once one role's
    pull request merges, the next role's check runs against a tree containing it.
    A red pull request never blocks a green sibling. Without credentials the PR
    field carries a typed error and ``pr_url`` stays null.

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
constructor. Test-owned fixture outputs exercise the lifecycle, gate plumbing,
reviewer, compose, and PR tail without a model or live AWS. They are not a
customer-path builder or answer key. No env flag selects a fake on the shipped
binary, and no shipped module imports the fixture.

Run it (always via the HTTP shell, ``connection_api.py``):
    python3 orchestrator/connection_api.py
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
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
# Active runs need a durable pulse too. Without it a Runtime recycle kills the
# daemon worker and leaves no evidence that the persisted "running" state is stale.
# 10s, not 30s: this pulse is now also the refresh rate of the live activity feed a
# watcher reads, and a build the attendee is watching should not look frozen for half a
# minute at a time. The snapshot is one small object, and one run is active at a time in
# a workshop, so the extra writes are cheap. Raise it if you ever run many concurrently.
_PERSIST_HEARTBEAT_S = float(os.environ.get(
    "WORKSHOP_RUN_STATE_HEARTBEAT_S", "10"))


def _new_run_id() -> str:
    """Return a readable id unique across coordinator sessions and processes."""
    return (
        f"run_{time.strftime('%H%M%S', time.gmtime())}_"
        f"{secrets.token_hex(6)}"
    )


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
import integration_plan  # noqa: E402
import llm  # noqa: E402  (model-id alias resolution for the runtime dispatch)
import policy  # noqa: E402  (the guardrail every role command is screened against)
import replay  # noqa: E402  (the run's story, for the PR body: reports, never judges)
import reviewer  # noqa: E402
import presets  # noqa: E402
import role_graph  # noqa: E402  (the agent-execution phase as a Strands graph)
import run_store  # noqa: E402  (durable run state: a verdict outlives its session)
import roles  # noqa: E402  (the ONE declarative roster)
import work_items as _work_items  # noqa: E402  (isolated role checkouts + candidate)

# Frozen contract enums (API_CONTRACT.md): the engine's public vocabulary.
PHASES = ["admission", "context_hydration", "pre_flight", "agent_execution", "finalization"]
TERMINAL = {"passed", "failed", "needs_human"}

# Bounded iteration, then a human. The bound's source of truth is the review
# orchestrator's MAX_REVIEW_ROUNDS (one re-implement pass): the cap is the
# initial build round plus that many re-implement rounds.
MAX_ITERATIONS = 1 + reviewer.MAX_REVIEW_ROUNDS
# A role may also receive one later turn when its declared dependency actually
# lands in the integration branch. That is not another red-gate repair cycle; it
# is the bounded semantic rebase a human developer performs after an upstream PR
# merges. Keeping the budgets separate prevents either loop from multiplying.
MAX_DEPENDENCY_REFRESHES = 1

# The offline test double has no GitHub, so it names its own base branch rather than
# leaving one blank: merge_work_item refuses a PR whose base is not the pinned
# branch, and an empty string would make that guard vacuous in tests.
_FIXTURE_BASE_BRANCH = "main"

# Per-role CLI hard timeout (a single coding-agent CLI dispatch inside its deployed
# Runtime). AGENT_EXECUTION_TIMEOUT_S (below) is the outer net; this kills one
# wedged CLI tree.
#
# 1200s, not 600s. This is a BUDGET, not a target: a role that finishes in 3
# minutes still finishes in 3 minutes. But the flagship preset asks for a whole
# small project (several features, real persistence, a UI over the same service),
# and a role doing that job properly runs past 10 minutes. At 600s the timeout was
# effectively a scope cap that rewarded the cheapest possible answer: the CLI got
# killed mid-build, so the deliverable that survived was whatever a single file
# could do. Raised so the ambition in the preset text is actually reachable.
HARNESS_ROLE_TIMEOUT_S = int(os.environ.get("HARNESS_ROLE_TIMEOUT_S", "1200"))

# Bounds for the per-role structured event feed (run.role_events): a chatty agent
# must not grow the in-memory run record without limit. Long bodies are truncated
# to _EVENT_TEXT_CAP chars; the feed is capped at _ROLE_EVENT_CAP events with a
# single visible marker once the cap is hit (never a silent drop).
_EVENT_TEXT_CAP = 4000
_ROLE_EVENT_CAP = 200

# The WATCHABLE slice of the live feed: how many recent events per role are persisted,
# and how much of each line survives. Deliberately small. The full feed is up to
# _ROLE_EVENT_CAP events of _EVENT_TEXT_CAP characters per role, and the snapshot is
# rewritten on every heartbeat, so persisting all of it would turn a liveness pulse into
# a megabyte-scale write every few seconds. A watcher only needs "what is happening now".
_ACTIVITY_EVENTS_PER_ROLE = 12
_ACTIVITY_TEXT_CAP = 240


def _persistable_activity(run: "Run") -> dict:
    """A compact, bounded view of what each role is doing, for an outside watcher.

    Keeps the event KIND (text / thinking / tool_use / tool_result) because that is what
    makes the feed legible as work rather than as log spam, and keeps the tool name when
    there is one. Truncates every body, and never grows with the length of a run."""
    out: dict[str, list[dict]] = {}
    with run._lock:
        feeds = {agent: list(evs) for agent, evs in run.role_events.items()}
    for agent, events in feeds.items():
        recent = []
        for ev in events[-_ACTIVITY_EVENTS_PER_ROLE:]:
            item = {"kind": ev.get("kind", "text")}
            if ev.get("name"):
                item["name"] = str(ev["name"])[:80]
            body = ev.get("text") or ev.get("result") or ev.get("input") or ""
            if not isinstance(body, str):
                body = json.dumps(body, default=str)
            body = " ".join(body.split())
            if body:
                item["text"] = body[:_ACTIVITY_TEXT_CAP]
            recent.append(item)
        if recent:
            out[agent] = recent
    return out

# Single fixed budget for the one agentic phase. A role dispatched to its deployed
# AgentCore Runtime drives a real CLI over the command shell; the per-role hard
# timeout (HARNESS_ROLE_TIMEOUT_S) is the inner net, this is the outer one.
#
# DERIVED from the inner net, never a standalone literal. The phase is
# builders-in-parallel THEN the checker, so the worst legitimate case is two
# sequential role budgets plus staging and read-back overhead. A flat 1800s was
# only ever safe while a role was capped at 600s; leaving it there while raising
# the role cap would make the OUTER net fire first and kill a build that was still
# progressing -- reported, of course, as the roles having failed.
# max(): an OUTER net below the inner one is never what an operator meant. It would
# fire while a role was still legitimately working and report the roles as having
# failed, which is the same "blame the agent for our config" failure mode as the
# region bug. An explicit override can only ever RAISE the phase budget.
AGENT_EXECUTION_TIMEOUT_S = max(
    int(os.environ.get("AGENT_EXECUTION_TIMEOUT_S",
                       str(2 * HARNESS_ROLE_TIMEOUT_S + 600))),
    HARNESS_ROLE_TIMEOUT_S + 600)

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

# How many of the check's own failing lines a repair round is given. The check writes
# whatever it likes, so this reads the lines rather than parsing a format: a line that
# announces a failure is one starting with FAIL, or one that says "failed"/"error"
# without being a PASS line. Capped because the output is already truncated to 4000
# characters upstream and a builder prompt is not the place for a full test log.
_MAX_FAIL_LINES = 25


def _gate_fail_lines(output: str) -> list[str]:
    """Pull the failing lines out of a validator-authored check's own output.

    No format is assumed or required. The validator picks its own language and its own
    reporting style, so this looks for the words a failure is announced with and keeps
    the order they appeared in. Returning nothing is fine and normal: some checks say
    only "VERDICT: REJECT", and the caller simply has less to pass on."""
    hits: list[str] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith(("PASS", "OK", "INFO", "SKIP")):
            continue
        if upper.startswith(("FAIL", "ASSERT", "ERROR", "NOT OK", "✗", "×")) or (
                "FAILED" in upper and "0 FAILED" not in upper):
            hits.append(line[:300])
        if len(hits) >= _MAX_FAIL_LINES:
            break
    return hits

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
_COMPOSE_SKIP_DIRS = {"__pycache__", ".git", ".workshop", "node_modules",
                      ".pytest_cache", ".ruff_cache", ".mypy_cache", "skills"}
_COMPOSE_SKIP_NAMES = {"CLAUDE.md", "AGENTS.md", ".DS_Store", ".git"}
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


def _work_patch_excluded(rel: str) -> bool:
    """Files that are coordination/runtime material rather than a role change."""
    return _compose_excluded(rel) or rel == _ACCEPTANCE_CHECK

# What builders are told about runnability. Not a layout and not a filename: a
# property of good work, stated once. The engine never reads the answer.
_RUNNABLE_RULE = (
    "If your work is something that RUNS, make it runnable from THIS directory with "
    "no manual setup, and say plainly in your output how to start it (the exact "
    "command). A separate validator will start it that way to check it, and a human "
    "will read the same instruction in the pull request.\n")

# An execution fact, not a preference about the deliverable. The authored check runs
# inside the coordinator image, so a builder choosing a toolchain that is absent there
# manufactures an infrastructure red gate. Keep this in sync with
# orchestrator-agent/Dockerfile and the customer-facing caveat.
_SUPPORTED_TOOLCHAINS_RULE = (
    "EXECUTION ENVIRONMENT: the shipped acceptance runtime provides Python and "
    "Node.js 22 (JavaScript/TypeScript). Choose within those toolchains for this "
    "workshop; another language requires its toolchain to be added to the coordinator "
    "image first.\n")

# What builders are told about SCOPE. Still not a layout and not a filename: it
# constrains the CRAFT, not the shape. Needed because the gate only asks "does it
# do what was asked?", which the cheapest possible answer can also satisfy -- a
# live run shipped one hand-rolled `BaseHTTPRequestHandler` file with inline-styled
# HTML for a request that deserved a structured service, and passed. A reviewer
# reads these pull requests as production work, so say so up front.
_SCOPE_RULE = (
    "BUILD IT AT THE SIZE THE REQUEST ACTUALLY IS. The check that grades you only "
    "asks whether the behaviour is there, so the smallest thing that passes will "
    "pass -- do not build that. Build what you would put up for review at work:\n"
    "- Use a real framework when the request is a real service or app (a proper web "
    "framework, not a hand-rolled request handler; a component-based UI, not one "
    "hand-written file with inline styles), and declare your dependencies in the "
    "manifest your ecosystem expects.\n"
    "- Split the concerns the task actually has into separate modules with real "
    "names. One file holding everything is a prototype.\n"
    "- Cover EVERY feature the shared brief assigns to YOUR role, and honour its "
    "non-functional asks literally (if data must survive a restart, an in-memory "
    "dict is a failure even when the checks pass in one process). The combined team "
    "owns the full request; do not duplicate a sibling's assignment in your patch.\n"
    "You still choose the language, the framework, the files, and the structure. "
    "This constrains the standard of the work, not its shape.\n")


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
# whose status is still non-terminal (a stranded-task reconciler).
#
# DERIVED from the agent-execution budget, never hardcoded, because the two must not
# drift: the sweep compares against `run._t0`, which is set at SUBMIT (see submit()),
# while AGENT_EXECUTION_TIMEOUT_S only starts counting when the graph runs. So the
# threshold has to cover admission + hydration + pre-flight + the whole agent budget +
# finalization (gate execution, compose, PR), or the sweeper kills a run that is still
# legitimately working.
#
# It was 600 against a 1800s agent budget, i.e. the comment claimed "wider than the
# agent_execution budget" while being 3x NARROWER. Any build over ~10 minutes was
# swept to needs_human mid-flight. A measured 3-role build took 819s, and a
# re-implement round doubles the agent phase, so this was reachable on the flagship
# request. It only ever fired on the CONSOLE path (connection_api starts the sweep
# thread; the deployed coordinator does not stage that module), which is exactly the
# path Lab 2's UI page and all of Lab 3 use.
# MAX_ITERATIONS rounds of (agent execution + gate), plus slack for the phases the
# agent budget does not cover: admission, hydration, pre-flight, compose and the PR.
STRANDED_AFTER_S = (MAX_ITERATIONS * (AGENT_EXECUTION_TIMEOUT_S + reviewer.GATE_TIMEOUT_S)
                    + 600)

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
    checker (Kiro today, a second Claude Code on the restore path) is a registry
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
    # Every routed role gets a unique work id and local linked worktree. Its Runtime
    # turn reconstructs the same named worktree from a source archive. Builder items
    # also map to independent GitHub branches and pull requests; the checker item
    # remains isolated but never authors a code PR.
    work_items: dict[str, _work_items.WorkItem] = field(default_factory=dict)
    integration_brief: dict | None = None
    integration_base: dict | None = None
    # Read ONCE from GitHub before any agent work. Every role pull request targets
    # this branch and merges into it on its own, so it is load-bearing for every
    # merge rather than for one final PR; the workflow never changes the setting.
    final_base_branch: str | None = None
    # One row per role pull request: its own check result, review verdict, and merge
    # state. This replaces the single combined gate plus the merge queue -- there is
    # no assembled candidate to have one verdict about.
    role_prs: list[dict] = field(default_factory=list)
    gate_history: list[dict] = field(default_factory=list)
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
    merge_state: str | None = None         # queue_complete | human_review | merged | null
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
    # the fail-loud spine holds (real execution, never a fabricated pass). The
    # test-only fixture supplies its own executable through the same seam.
    _acceptance_test_file: str | None = None
    _explicit_agents: bool = False
    _preset_req: str | None = None   # a starting point, if one was chosen
    _review_target: str | None = None      # run_id under review (review/pr-v1 only)
    _integration_brief_md: str = ""
    _active_builders: set[str] | None = field(default=None, repr=False)
    _refresh_context: str = ""
    # work_id -> the authored check path for THAT pull request. One check per pull
    # request, so this replaces the single _acceptance_test_file on the verdict path
    # (which is kept for the read-only review route and the compose commit).
    _item_checks: dict[str, str] = field(default_factory=dict)
    # work_id -> the patch digest that was last REVIEWED. A changed pull request is
    # reviewed again; a byte-identical one is not.
    _reviewed_digests: dict[str, str] = field(default_factory=dict)
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
        """The role's isolated local Git worktree."""
        item = self.work_items.get(agent)
        suffix = item.work_id if item is not None else agent
        d = os.path.join(self.workdir, "worktrees", suffix)
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def worktree_repo_dir(self) -> str:
        """The run-local common Git metadata shared by its isolated worktrees."""
        return os.path.join(self.workdir, "git", "repo.git")

    def item_tree_dir(self, work_id: str) -> str:
        """The exact tree merging ONE pull request would produce.

        Keyed on the work id because each pull request is checked on its own, so a
        run holds one of these per role rather than a single assembled candidate.
        """
        return os.path.join(self.workdir, "pr", work_id)

    @property
    def integration_base_dir(self) -> str:
        """The repository default branch snapshot every pull request is built on."""
        return os.path.join(self.workdir, "integration-base")

    def item_base_dir(self, agent: str) -> str:
        """Immutable local copy of the exact base one builder received."""
        item = self.work_items.get(agent)
        suffix = item.work_id if item is not None else agent
        return os.path.join(self.workdir, f"base-{suffix}")

    def runtime_subdir(self, agent: str) -> str:
        """The role's unique Runtime worktree archive name."""
        item = self.work_items.get(agent)
        return (item.runtime_subdir(self.run_id)
                if item is not None else f"{self.run_id}/work/{agent}")

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
        self._persist_lock = threading.Lock()
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
            run = Run(
                run_id=_new_run_id(),
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
        self._persist_run(run)
        threading.Thread(target=self._drive, args=(run,), daemon=True).start()
        threading.Thread(target=self._heartbeat, args=(run,), daemon=True).start()
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self) -> list[Run]:
        return list(self._runs.values())

    def _persist_run(self, run: Run) -> None:
        """Write one current checkpoint, serialized against the heartbeat."""
        saved = {
            **public_result(run),
            "task": run.task,
            "agents": list(run.agents),
            "roles": dict(run.roles),
            "created_at": run.created_at,
            "options": dict(run.options),
            "preset": run._preset_req,
            "user_identity": dict(run.user_identity),
            "work_items": {
                agent: item.public()
                for agent, item in run.work_items.items()
            },
            "integration_brief": dict(run.integration_brief or {}),
            "integration_base": dict(run.integration_base or {}),
            "final_base_branch": run.final_base_branch,
            "role_prs": list(run.role_prs),
            "gate_history": list(run.gate_history),
            "events": (run.events or [])[-_PERSIST_LOG_TAIL:],
            # What each role is DOING right now, so a run can be watched from outside
            # the process that is running it. The console renders `public_events(run)`
            # in-process, but the served path is a DEPLOYED coordinator: its engine runs
            # inside an AgentCore Runtime the attendee cannot attach to, so without this
            # the only window is a chat turn per poll, which is a minute of model time
            # to learn one line of state. This is the same feed, bounded hard because it
            # is rewritten on every heartbeat.
            "activity": _persistable_activity(run),
        }
        with self._persist_lock:
            run_store.save(_RUNS_DIR, run.run_id, saved, run.log)

    def _heartbeat(self, run: Run) -> None:
        """Refresh an active run's durable snapshot until it becomes terminal."""
        while run.status in ("queued", "running"):
            time.sleep(max(1.0, _PERSIST_HEARTBEAT_S))
            if run.status not in ("queued", "running"):
                return
            self._persist_run(run)

    def _cleanup_runtime_exchange(self, run: Run) -> None:
        """Remove deployed Runtime transfer objects after the verdict is durable."""
        if (self.executor.name != "agentcore"
                or os.environ.get("WORKSHOP_S3FILES_DIR")):
            return
        import runtime_stage  # noqa: PLC0415 (AgentCore-only lifecycle seam)
        runtime_stage.cleanup_run(run.run_id)

    # ----------------------------------------------------------- the blueprint
    def _drive(self, run: Run) -> None:
        """One task in -> five phases -> terminal state. Always terminal."""
        try:
            for phase_fn in (self._admission, self._hydrate, self._preflight):
                if not phase_fn(run):
                    return  # fail-closed: phase set status/reason already
            # ONE agentic step, then per-pull-request finalization. There is no
            # whole-run round any more: each pull request carries its own bound
            # (``item.attempt``) inside ``_finalize``, so a repair for one role can
            # never restart the others' work. That is what makes the bound hold at
            # every layer instead of only in the engine.
            run.iterations += 1
            if not self._execute(run):
                return  # fail-closed: the phase set status/reason already
            self._finalize(run)
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
            # Do this BEFORE persistence so a later session reads the same verdict
            # the live caller saw.
            if run.status == "failed" and not _is_permanent(run.fail_reason):
                run.status = "needs_human"
                run.log(f"transient failure ({run.fail_reason}) -> needs_human "
                        "(a human can resume; resubmit may succeed)", "warn")
            # Persist the verdict where a LATER session can still read it. In the
            # `finally` deliberately: every exit path (passed, failed,
            # needs_human, engine error, stall) has to leave an answer behind, and
            # the one that matters most is the failure an attendee wants to ask
            # about after their session expired. Never raises; see run_store.
            try:
                self._persist_run(run)
            except Exception as exc:  # noqa: BLE001 (history is not the verdict)
                run.log(f"run state not persisted: {exc}", "warn")
            # Runtime input and output archives are transport, not durable run
            # history. Delete them only after the terminal snapshot above has
            # been attempted; a cleanup failure never changes the verdict.
            try:
                self._cleanup_runtime_exchange(run)
            except Exception as exc:  # noqa: BLE001 (cleanup is best-effort)
                run.log(f"Runtime exchange cleanup failed: {exc}", "warn")
                try:
                    self._persist_run(run)
                except Exception:
                    pass
            # The engine starts NOTHING, so there is nothing here to stop. If the
            # deliverable needs to run, the validator's authored check starts it, and
            # `reviewer.run_gate` tears that whole process group down when the check
            # ends: the only place a started service can leak is the only place one is
            # started. The retired replay-server pool no longer exists.

    # Phase 1. Admission validates AND ROUTES: the workflow registry decides which
    # agents this task dispatches (an unknown preset fails loud, never a guess).
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
        run.work_items = {}
        for agent_id in run.agents:
            registered = roles.get(agent_id)
            run.work_items[agent_id] = _work_items.WorkItem.create(
                run.run_id,
                agent_id,
                run.roles[agent_id],
                registered.capability,
                kind=registered.kind,
            )
        # Recompute the active count from the source of truth (no drifting counter).
        active = self.active_count(exclude=run.run_id)
        if active >= self.max_concurrent:
            run.status, run.fail_reason = "failed", "CONCURRENCY_LIMIT"
            run.log(f"admission rejected: {active} runs active (limit {self.max_concurrent})", "error")
            return False
        run.log(f"admitted + routed: {route.rule} -> agents {run.agents}")
        run.log("isolated work allocated: " + ", ".join(
            f"{agent}={item.work_id}" for agent, item in run.work_items.items()))
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
        builders = [
            item for item in run.work_items.values()
            if item.kind == roles.BUILDER
        ]
        try:
            run.integration_brief = integration_plan.create(
                run.task,
                builders,
                offline_fixture=(self.executor.name == "fixture"),
            )
            run._integration_brief_md = integration_plan.markdown(
                run.task, run.integration_brief, builders)
        except integration_plan.IntegrationPlanError as exc:
            run.status, run.fail_reason = "failed", str(exc)
            run.log(f"context hydration failed: {exc}", "error")
            return False
        run.log("hydrated harness: " + ", ".join(harness))
        run.log(
            "shared integration contract prepared; merge order: "
            + " -> ".join((run.integration_brief or {}).get("merge_order") or []))
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
        if not (run.route and run.route.get("read_only")):
            if not self._prepare_run_base(run):
                return False
        run.log("pre-flight green: every routed role has steering and a wired runtime")
        return True

    def _prepare_run_base(self, run: Run) -> bool:
        """Read the repository default branch and seed every builder checkout.

        Every role pull request is based on this one branch and merges into it on
        its own, so there is no run-scoped branch to create. Reading it here, before
        any agent work, is also what makes a missing Gateway fail in seconds instead
        of after a ten-minute build.
        """
        os.makedirs(run.workdir, exist_ok=True)
        if self.executor.name == "fixture":
            shutil.rmtree(run.integration_base_dir, ignore_errors=True)
            os.makedirs(run.integration_base_dir, exist_ok=True)
            run.integration_base = {
                "mode": "fixture",
                "branch": _FIXTURE_BASE_BRANCH,
                "sha": "fixture-empty-base",
                "files": 0,
            }
            run.final_base_branch = _FIXTURE_BASE_BRANCH
        elif self.executor.name == "agentcore":
            snapshot = github.prepare_run_base(run.integration_base_dir)
            if snapshot.get("error"):
                run.status = "failed"
                run.fail_reason = (
                    "PR_PREFLIGHT_ERROR:" + str(snapshot["error"]))
                run.log(
                    f"pre-flight failed before agent work: {snapshot['error']}",
                    "error",
                )
                return False
            run.integration_base = dict(snapshot)
            run.final_base_branch = str(
                snapshot.get("default_branch") or "") or None
        else:
            run.status, run.fail_reason = "failed", _NO_PRODUCER_ERROR
            return False

        # One base for every pull request, assigned from the branch just read rather
        # than derived per item, so a mid-run default-branch change stays detectable
        # at merge time instead of silently retargeting a PR.
        for item in run.work_items.values():
            item.base_branch = run.final_base_branch or ""

        coordination = os.path.join(run.integration_base_dir, ".workshop")
        os.makedirs(coordination, exist_ok=True)
        with open(os.path.join(coordination, "integration-brief.md"),
                  "w", encoding="utf-8") as f:
            f.write(run._integration_brief_md)

        builders = [
            item for item in run.work_items.values()
            if item.kind == roles.BUILDER
        ]
        for item in builders:
            item_base = run.item_base_dir(item.agent)
            shutil.rmtree(item_base, ignore_errors=True)
            shutil.copytree(run.integration_base_dir, item_base)
            item.base_sha = str((run.integration_base or {}).get("sha") or "")
            item.base_digest = _work_items.tree_digest(
                item_base, exclude=_compose_excluded)

        shutil.rmtree(os.path.dirname(run.worktree_repo_dir), ignore_errors=True)
        shutil.rmtree(os.path.join(run.workdir, "worktrees"),
                      ignore_errors=True)
        base_commit = _work_items.initialize_worktree_repo(
            run.worktree_repo_dir, run.integration_base_dir)
        for item in builders:
            _work_items.add_worktree(
                run.worktree_repo_dir,
                run.roledir(item.agent),
                item.worktree_branch,
                base_commit,
            )

        if self.executor.name != "fixture":
            self._stage_builder_checkouts(run, builders)
        run._active_builders = {item.agent for item in builders}
        run.log(
            f"base ready on {run.final_base_branch}: "
            f"{(run.integration_base or {}).get('files', 0)} file(s); "
            f"{len(builders)} isolated Git worktree(s) seeded")
        return True

    def _stage_builder_checkouts(
        self, run: Run, builders: list[_work_items.WorkItem]
    ) -> None:
        """Publish each local worktree seed through one immutable source archive."""
        import runtime_stage  # noqa: PLC0415

        staged = runtime_stage.stage_base(run.run_id, run.integration_base_dir)
        run.log(f"integration base staged for Runtime builders ({staged} files)")
        if os.environ.get("WORKSHOP_S3FILES_DIR"):
            source = runtime_stage.base_path(run.run_id)
            for item in builders:
                dest = os.path.join(
                    runtime_stage.mnt_root(), run.runtime_subdir(item.agent))
                runtime_stage.copy_tree_files(source, dest)
            return

        import runtime_config  # noqa: PLC0415
        import runtime_exec  # noqa: PLC0415

        for item in builders:
            hit = runtime_config.pick(item.agent)
            if not hit:
                raise RuntimeError(f"RUNTIME_NOT_WIRED:{item.agent}")
            runtime_exec.clone_runtime_tree(
                hit[0],
                runtime_stage.base_subdir(run.run_id),
                run.runtime_subdir(item.agent),
            )

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
        wired runtime ARN. The exact role archive becomes an isolated Git worktree
        on Runtime-local disk for the turn and is uploaded atomically when the turn
        ends. Raises if the role has no wired runtime: fail loud, never local.

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
        run_subdir = run.runtime_subdir(agent_id)
        role.engine = "agentcore"
        run.term(agent_id, f"echo 'dispatching to {arn.split('/')[-1]} on AgentCore "
                           "Runtime; it receives the tracked checkout archive, "
                           f"opens {run.work_items[agent_id].worktree_branch} on "
                           "local disk, and uploads one result archive'")
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
                    run_subdir=run_subdir, artifact_rel=artifact_rel,
                    model=llm.resolve(model),
                    region=runtime_exec.region_for(arn),
                    on_line=on_line, timeout_s=HARNESS_ROLE_TIMEOUT_S)
                break
            except runtime_exec.ModelQuotaError:
                # A daily account limit cannot recover in a fresh shell. Preserve
                # the specific reason and avoid spending another rejected request.
                raise
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
        with run._lock:
            run.terminals.setdefault(agent_id, []).append({
                "cmd": f"agentcore dispatch -> {arn.split('/')[-1]} ({agent_id} CLI)",
                "output": _display_scrub(tail), "exit": result["exit"],
                "elapsed_s": round(time.monotonic() - t0, 2)})
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
            # ...and the check's OWN FAIL lines, which are the only thing that says
            # WHICH assertion broke. Measured on a live run: the check reported
            # "141 checks run, 1 failed" and the builder received only that one-line
            # summary, so its repair round spent 11 minutes rediscovering the failure
            # by re-reading its own code. The evidence already existed in
            # gate["output"] and already reached the pull request; it just never
            # reached the role that had to act on it.
            #
            # This does NOT tell the builder what to build. It reports what the
            # checker OBSERVED, exactly as a CI log does, and the check still decides.
            fail_lines = _gate_fail_lines((run.review.get("gate") or {}).get("output", ""))
            if fail_lines:
                feedback += ("\n\nThe check's own failing lines (it decides the gate, "
                             "so treat these as the specification of what to fix):\n"
                             + "\n".join(f"  {line}" for line in fail_lines))
        if run._refresh_context:
            feedback += "\n\n" + run._refresh_context
        # The dispatched role, not a fixed id: role.agent is whichever role the
        # roster serves for this capability, and its default model is the registry's.
        agent_id = role.agent
        model = self._role_model(run, agent_id, roles.get(agent_id).default_model)
        assignment = (
            (run.integration_brief or {}).get("role_assignments") or {}
        ).get(agent_id, {})
        # Read-only material for the run is staged at <run_id>-skill/; the agent works
        # in its own writable <run_id>/ workdir (set as cwd by runtime_exec). Name the
        # paths the agent will actually see in its container.
        import runtime_stage  # noqa: PLC0415 (lazy; the wirable mount root)
        staged = (
            "."
            if self.executor.name == "agentcore"
            and not os.environ.get("WORKSHOP_S3FILES_DIR")
            else runtime_stage.skill_path(run.run_id)
        )
        # Registry, not a literal, for the same reason as the checker prompt below: the
        # backend is Claude Code today (CLAUDE.md), but WORKSHOP_ROLES can serve a role
        # whose native steering file has another name.
        builder_steering = roles.get(agent_id).steering_file.replace("\\", "/")
        prompt = (
            "You are the backend implementer role in a multi-agent build. Read "
            f"{builder_steering} in this directory for your role, and read the "
            "shared `.workshop/integration-brief.md` before changing code. It "
            "defines the boundary you share with the other builders, not an "
            "implementation you must copy. Read the "
            f"`{staged}/skills/backend-engineering/SKILL.md` harness staged for this "
            "run (also baked at ~/skills/backend-engineering/SKILL.md) and apply it.\n\n"
            f"THE REQUEST: {run.task}\n\n"
            "YOUR EXCLUSIVE ASSIGNMENT:\n"
            f"{json.dumps(assignment, indent=2)}\n\n"
            "Implement that assignment and its side of the shared contract. Your "
            "isolated checkout is not supposed to contain the other builders' work; "
            "do not make it standalone by adding their capability.\n\n"
            "Decide everything else yourself: the language, the framework, the files, "
            "the structure, the protocol. Nobody has prescribed a shape. Read the "
            f"request carefully; any material it refers to is staged read-only under "
            f"{staged} . Write your work in THIS directory (it is yours), and use as "
            "many files as the job deserves.\n\n"
            + _SCOPE_RULE + "\n"
            + _SUPPORTED_TOOLCHAINS_RULE
            + _RUNNABLE_RULE
            + "\nDo not leave a long-running server in the foreground of your own "
              "session; finish your turn." + feedback)
        self._runtime_cli(run, agent_id, role, prompt, model)
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
        staged = (
            "."
            if self.executor.name == "agentcore"
            and not os.environ.get("WORKSHOP_S3FILES_DIR")
            else runtime_stage.skill_path(run.run_id)
        )
        assignment = (
            (run.integration_brief or {}).get("role_assignments") or {}
        ).get(agent_id, {})
        backend_owner = next((
            item for item in run.work_items.values()
            if item.kind == roles.BUILDER
            and item.agent != agent_id
            and item.capability == "backend"
        ), None)
        if endpoint:
            backend = (
                f"A backend for this run is live at {endpoint}, and you can call "
                "it while you work.\n")
        elif backend_owner:
            backend = (
                f"The `{backend_owner.role}` work item "
                f"`{backend_owner.work_id}` owns the backend and is building it in "
                "parallel. It is not live in your isolated checkout. Build against "
                "the shared contract; do not add a substitute backend, persistence "
                "layer, or second full-stack implementation to your submitted tree.\n")
        else:
            backend = (
                "No separate backend builder was routed. Follow your assignment in "
                "the shared brief; do not invent a service unless that assignment "
                "requires one.\n")
        conflict_feedback = ""
        if run._refresh_context:
            conflict_feedback += "\n\n" + run._refresh_context
        prompt = (
            "You are the frontend builder role in a multi-agent build. Read "
            "AGENTS.md in this directory for your role, and read the "
            "shared `.workshop/integration-brief.md` before changing code. It "
            "defines the boundary you share with the other builders, not an "
            "implementation you must copy. Read the "
            f"`{staged}/skills/frontend-design/SKILL.md` harness staged for this run "
            "and apply it.\n\n"
            f"THE REQUEST: {run.task}\n\n"
            "YOUR EXCLUSIVE ASSIGNMENT:\n"
            f"{json.dumps(assignment, indent=2)}\n\n"
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
            + _SCOPE_RULE + "\n"
            + _SUPPORTED_TOOLCHAINS_RULE
            + _RUNNABLE_RULE
            + conflict_feedback)
        self._runtime_cli(run, agent_id, role, prompt, model)
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
        runtime_subdir = run.runtime_subdir(agent_id)
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
                src = os.path.join(runtime_stage.mnt_root(), runtime_subdir)
                if os.path.isdir(src):
                    self._clear_transferred(dest_root)
                    runtime_stage.copy_tree_files(
                        src, dest_root, replace=False,
                        excluded_names=runtime_exec._TREE_EXCLUDES)
            else:
                hit = runtime_config.pick(agent_id)
                if hit:
                    try:
                        # No region argument: read_tree_from_runtime derives it from
                        # the ARN. Passing an env default here is what made every
                        # read-back fail outside us-west-2.
                        tree = runtime_exec.read_tree_from_runtime(
                            hit[0], runtime_subdir, ".")
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
                                hit[0], runtime_subdir)
                        except Exception:  # noqa: BLE001 (best effort probe)
                            listing = ""
                        if listing.strip():
                            run.log(f"{agent_id}: the runtime HAS files but the "
                                    f"transfer returned none; retrying the read-back "
                                    f"once (workspace: {listing.strip()[:160]})",
                                    "warn")
                            try:
                                tree = runtime_exec.read_tree_from_runtime(
                                    hit[0], runtime_subdir, ".")
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
        paths = {
            harness_config.steering_filename(agent_id),
            ".mcp/servers.jsonl",
            ".git",
        }
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
            # Say WHICH failure this is, and NEVER blame the agent for a failure
            # that is ours. "finished but wrote no files" is a claim about the
            # AGENT, and it is FALSE whenever the runtime workspace still holds the
            # work: a live us-east-1 run reported exactly that while a complete
            # server.py sat on the mount, because the read-back had asked the wrong
            # region. So probe the runtime first and, when it HAS files, raise a
            # transport error that says so instead of the no-files claim.
            listing = ""
            if self.executor.name == "agentcore" and not os.environ.get(
                    "WORKSHOP_S3FILES_DIR"):
                try:
                    import runtime_config  # noqa: PLC0415
                    import runtime_exec  # noqa: PLC0415
                    hit = runtime_config.pick(agent_id)
                    if hit:
                        # No region argument: derived from the ARN. This probe is
                        # the ONLY thing standing between a transport bug and an
                        # attendee being told their agent did nothing, so it must
                        # not share the defaulting mistake it exists to catch.
                        listing = runtime_exec.list_tree_in_runtime(
                            hit[0], run.runtime_subdir(agent_id)) or ""
                except Exception:  # noqa: BLE001 (diagnostic only)
                    listing = ""
            if listing.strip():
                raise RuntimeError(
                    f"ARTIFACT_TRANSFER_ERROR: {agent_id} DID write work, but the "
                    f"engine could not read it back from the runtime workspace, so "
                    f"there is nothing local to review or run. This is a transport "
                    f"failure on our side, not a failed agent turn: do not go "
                    f"looking at the agent. Resubmit the same request. Workspace: "
                    f"{listing.strip()[:200]}{suffix}")
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: {agent_id} finished but wrote no files, so "
                f"there is nothing to review or run{suffix}")
        return n

    def _require_item_change(self, run: Run, agent_id: str) -> int:
        """Require a non-empty patch against the exact base this builder received."""
        self._read_work_tree(run, agent_id)
        item = run.work_items[agent_id]
        patch = _work_items.diff_trees(
            item,
            run.item_base_dir(agent_id),
            run.roledir(agent_id),
            exclude=_work_patch_excluded,
        )
        if not patch.changes:
            raise RuntimeError(
                f"ROLE_EXECUTION_ERROR: {agent_id} finished but changed nothing "
                f"relative to integration base {str(item.base_sha or '')[:12]}; "
                "the cloned repository itself is not evidence of role work")
        run.log(
            f"{agent_id}: patch ready ({len(patch.changed_files)} write(s), "
            f"{len(patch.deleted_files)} deletion(s), "
            f"digest {patch.digest[:12]})")
        return len(patch.changes)

    def _publish_active_work_items(self, run: Run) -> None:
        """Publish builder patches before the checker inspects their candidate."""
        active = (
            run._active_builders
            if run._active_builders is not None
            else {
                item.agent for item in run.work_items.values()
                if item.kind == roles.BUILDER
            }
        )
        for item in _work_items.dependency_order(
                item for item in run.work_items.values()
                if item.kind == roles.BUILDER):
            if item.agent not in active:
                continue
            if self.executor.name == "fixture":
                item.state = "in_review"
                item.stale = False
                item.pr = item.pr or {
                    "skipped": "offline fixture (no GitHub side effect)",
                    "base": item.base_branch,
                    "head": item.branch,
                }
                continue
            result = github.publish_work_item(
                run, item, replay.work_item_narrative(run, item))
            if result.get("error"):
                raise RuntimeError(
                    f"ROLE_PR_PUBLISH_ERROR:{item.work_id}: {result['error']}")
            run.log(
                f"{item.role}: role PR ready {result.get('pr_url')} "
                f"({item.work_id})")

    def _build_item_tree(self, run: Run, item: _work_items.WorkItem) -> str:
        """Materialise the tree merging ONE pull request would produce.

        Not a verdict and not a merge of several roles: the base is the repository
        default branch AS IT STANDS, plus this one role's patch. Building it against
        the current base is what keeps a cross-role defect catchable -- once a
        sibling's pull request has merged, this tree contains it, so the check and
        the review see both sides of the seam.

        A path the base changed since this role received it raises ``StalePatch``.
        That is the ordinary "someone merged before you" case, and the owner gets one
        bounded refresh rather than the engine silently overwriting merged work.
        """
        try:
            digest = _work_items.apply_patch(
                run.integration_base_dir,
                item,
                run.item_base_dir(item.agent),
                run.roledir(item.agent),
                run.item_tree_dir(item.work_id),
                exclude=_work_patch_excluded,
            )
        except _work_items.StalePatch as exc:
            item.stale = True
            run.log(
                f"{item.work_id}: base moved under this pull request: "
                + ", ".join(c["path"] for c in exc.conflicts[:8]),
                "warn",
            )
            raise
        item.stale = False
        run.log(f"{item.work_id}: pull request tree built on "
                f"{run.final_base_branch} ({len(item.changed_files)} file(s))")
        return digest

    def _stage_refreshed_work_item(
        self,
        run: Run,
        item: _work_items.WorkItem,
        seed: str,
    ) -> int:
        """Reset one owner's linked worktree and Runtime checkout to a refresh."""
        dest = run.roledir(item.agent)
        local_count = _work_items.reset_worktree(
            run.worktree_repo_dir,
            dest,
            seed,
            f"Refresh baseline for {item.work_id}",
        )
        if self.executor.name == "fixture":
            return local_count

        import runtime_stage  # noqa: PLC0415

        staged = runtime_stage.stage_refresh(run.run_id, item.work_id, seed)
        if os.environ.get("WORKSHOP_S3FILES_DIR"):
            source = runtime_stage.refresh_path(run.run_id, item.work_id)
            runtime_dest = os.path.join(
                runtime_stage.mnt_root(), run.runtime_subdir(item.agent))
            runtime_stage.copy_tree_files(source, runtime_dest)
            return staged

        import runtime_config  # noqa: PLC0415
        import runtime_exec  # noqa: PLC0415

        hit = runtime_config.pick(item.agent)
        if not hit:
            raise RuntimeError(f"RUNTIME_NOT_WIRED:{item.agent}")
        runtime_exec.clone_runtime_tree(
            hit[0],
            runtime_stage.refresh_subdir(run.run_id, item.work_id),
            run.runtime_subdir(item.agent),
        )
        return staged

    def _prepare_checker_checkout(self, run: Run, agent_id: str,
                                  subject: _work_items.WorkItem) -> None:
        """Give the checker its own worktree at ONE pull request's tree.

        Called once per role pull request. ``reset_worktree`` wipes everything but
        ``.git``, so nothing from the previous pull request's check can leak into the
        next one -- each check is authored beside, and only beside, the work it
        grades.
        """
        import runtime_stage  # noqa: PLC0415

        item = run.work_items[agent_id]
        tree = run.item_tree_dir(subject.work_id)
        dest = run.roledir(agent_id)
        if not os.path.isfile(os.path.join(dest, ".git")):
            _work_items.add_worktree(
                run.worktree_repo_dir,
                dest,
                item.worktree_branch,
            )
        _work_items.reset_worktree(
            run.worktree_repo_dir,
            dest,
            tree,
            f"Pull request baseline for {subject.work_id}",
        )
        if self.executor.name == "fixture":
            return
        if self.executor.name != "agentcore":
            raise RuntimeError(_NO_PRODUCER_ERROR)

        staged = runtime_stage.stage_item(run.run_id, subject.work_id, tree)
        run.log(f"{subject.work_id}: pull request tree staged for Runtime "
                f"validation ({staged} files)")
        dest_subdir = run.runtime_subdir(agent_id)
        if os.environ.get("WORKSHOP_S3FILES_DIR"):
            dest = os.path.join(runtime_stage.mnt_root(), dest_subdir)
            runtime_stage.copy_tree_files(
                runtime_stage.item_path(run.run_id, subject.work_id), dest)
            return

        import runtime_config  # noqa: PLC0415
        import runtime_exec  # noqa: PLC0415
        hit = runtime_config.pick(agent_id)
        if not hit:
            raise RuntimeError(f"RUNTIME_NOT_WIRED:{agent_id}")
        runtime_exec.clone_runtime_tree(
            hit[0],
            runtime_stage.item_subdir(run.run_id, subject.work_id),
            dest_subdir,
        )

    def _cli_validator_authors_test(self, run: Run, endpoint: str,
                                    role: RoleResult,
                                    subject: _work_items.WorkItem) -> str:
        """The validator role AUTHORS the acceptance check for ONE PULL REQUEST, and
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
        if run._refresh_context:
            feedback += (
                "\n\nThe base branch advanced because another pull request merged. "
                "Inspect this checkout as it exists now; do not assume the prior "
                "check is still correct.")
        live = (f"The deliverable is running at {endpoint} .\n"
                if endpoint else
                "The deliverable does not expose a running service this round.\n")
        integration_context = integration_plan.review_context(
            run.integration_brief or {},
            self._builder_items(run),
        )
        # The steering FILENAME comes from the registry, never a literal: the served
        # checker is Kiro (`.kiro/steering/validator.md`), while the claude-code-validator
        # restore path reads `CLAUDE.md`. Hardcoding either one points the checker at a
        # file that is not there for the other.
        checker_steering = roles.get(checker).steering_file.replace("\\", "/")
        prompt = (
            "You are the validator role in a multi-agent build, and you are the "
            f"checker in a maker-checker pair. Read {checker_steering} in this "
            "directory for your role.\n\n"
            f"THE REQUEST the other roles were given: {run.task}\n\n"
            f"YOU ARE CHECKING ONE PULL REQUEST: {subject.work_id} "
            f"({subject.role}), which merges into {run.final_base_branch!r}. This "
            "checkout is exactly what merging it would produce: the base branch as "
            "it stands now, plus that pull request's changes. If another role's "
            "pull request has already merged, its code is HERE and this pull request "
            "must work with it.\n\n"
            + live +
            "TEAM INTEGRATION CONTEXT (recorded facts, not an answer key):\n"
            f"{json.dumps(integration_context, indent=2)}\n\n"
            "AUTHOR the acceptance check for this deliverable and save it as "
            f"`./{_ACCEPTANCE_CHECK}` in this directory: ONE self-contained "
            "EXECUTABLE file, starting with a shebang line, in whatever language you "
            "judge fits. The environment where the engine runs it matches this "
            "container's supported runtimes: Python and Node.js 22 "
            "(JavaScript/TypeScript).\n\n"
            "YOU decide what 'acceptable' means for this request. Nobody has given "
            "you a checklist, a contract, or a list of required checks, because only "
            "you have seen this particular task. Read the request, inspect the "
            "pull request tree cloned into this checkout, and encode the checks "
            "that would "
            "convince a skeptical engineer that the request was met. Prefer evidence "
            "over assumption: probe the running deliverable over the wire where it "
            "can prove something, and inspect the files where it cannot. This is a "
            "gate on ONE pull request: verify that what it contributes works, and "
            "works with whatever is already on the base branch. Reject disconnected "
            "duplicate stacks, dead alternative implementations, or this role "
            "replacing a sibling's ownership merely because that path happens to "
            "run. Do not demand a particular framework, path, or file "
            "count; judge the actual request, ownership, and behavior. When the "
            "request names a standard command, protocol, or library as its source "
            "of semantics, use an independent implementation as the runtime oracle "
            "when one is available. Do not mix that oracle with hand-entered "
            "expected values for the same behavior; a contradiction inside your "
            "check is a broken check, not a defect in the work.\n\n"
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
            "it, print why and fail: that is a real finding, not a technicality.\n\n"
            # The floor is stated HERE as well as in the steering because the steering
            # alone did not hold: with the mount slowness explained and the budget in
            # the env, a live run still authored a 20s poll and red-gated a service
            # whose own log said startup was complete. This is the last channel before
            # the file is written, so it repeats the number rather than trusting that
            # the earlier instruction won.
            "WAIT LONG ENOUGH: poll for AT LEAST 60 SECONDS before concluding it did "
            "not come up, and read `WORKSHOP_GATE_TIMEOUT_S` for the total wall clock "
            "you have. A first start may still install declared dependencies. A short "
            "poll rejects services that were merely still starting, which is a false "
            "verdict: reject work that ANSWERS WRONGLY, never work you did not wait "
            "for.\n\n"
            "KEEP THE CHECK ITSELF EXECUTABLE: do not pass quoted test data through "
            "nested shell/source snippets such as `python -c` or `node -e`; serialize "
            "payloads with the check language's native JSON support. Before handing "
            "the file off, run a PARSE-ONLY syntax check (`bash -n`, "
            "`python -m py_compile`, or `node --check`, as appropriate). Do not run "
            "the acceptance behavior; the engine owns that one real execution."
            + feedback)
        result = self._runtime_cli(run, _validator_agent(), role, prompt, model,
                                   _ACCEPTANCE_CHECK)
        test_path = os.path.join(run.roledir(_validator_agent()), _ACCEPTANCE_CHECK)
        self._read_artifact(test_path, _ACCEPTANCE_CHECK, result)
        run.term(_validator_agent(), f"head -1 {_ACCEPTANCE_CHECK} && wc -l {_ACCEPTANCE_CHECK}")
        return self._gate_dir_check_path(run, test_path, subject)

    # Where a round's authored check is kept so the NEXT round can re-run it. It cannot
    # live in the validator's worktree (that is reset per pull request) and it cannot
    # live in the gate directory (``_gate_dir_check_path`` rebuilds that from scratch
    # every round, on purpose, so a previous round's artefacts cannot satisfy a check).
    # So it gets its own place under the run directory.
    def _kept_check_path(self, run: Run, subject: _work_items.WorkItem) -> str:
        return os.path.join(run.workdir, "authored-checks", subject.work_id,
                            _ACCEPTANCE_CHECK)

    def _keep_check_for_later_rounds(self, run: Run, subject: _work_items.WorkItem,
                                     authored: str) -> None:
        """Copy a freshly authored check aside, so a repair round can re-run THIS one."""
        kept = self._kept_check_path(run, subject)
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        shutil.copyfile(authored, kept)

    def _prior_check(self, run: Run, subject: _work_items.WorkItem) -> str:
        """The check a previous round authored for this pull request, if there is one.

        Returns "" on the first round, and on any round where the copy is missing or
        empty -- in which case the caller authors a new one. A missing copy must never
        become a skipped gate: there is no fallback grade anywhere on this path."""
        kept = self._kept_check_path(run, subject)
        try:
            if os.path.isfile(kept) and os.path.getsize(kept) > 0:
                return kept
        except OSError:
            pass
        return ""

    def _gate_dir_check_path(self, run: Run, authored: str,
                             subject: _work_items.WorkItem) -> str:
        """Run the authored check beside the exact pull request tree it inspected.

        Builders never share a writable tree. The validator received a clone of ONE
        pull request's tree and wrote its check there, so the host gate reconstructs
        that same shape: that tree's files plus the authored executable. It never
        merges role directories on the verdict path.
        """
        gate_dir = os.path.join(run.workdir, "gate", subject.work_id)
        # Read the authored check BEFORE the rebuild below, because this same gate
        # directory is where the previous staging left it: the validator's own
        # worktree is reset for the next pull request, so the staged copy is the
        # surviving one, and finalization re-stages from it. Deleting the directory
        # first would delete the check it is about to run.
        name = os.path.basename(authored)
        with open(authored, "rb") as handle:
            check_bytes = handle.read()
        # REBUILD it, never add to it. A re-implement round runs this again, and a
        # leftover from the previous round is a file the new check never saw: it
        # could satisfy a check the fixed deliverable no longer satisfies, which
        # would make round 2 pass on round 1's evidence. A live run left round 1's
        # `issues.db` (created when the check STARTED the service) sitting here for
        # round 2.
        shutil.rmtree(gate_dir, ignore_errors=True)
        tree = run.item_tree_dir(subject.work_id)
        if not os.path.isdir(tree):
            raise RuntimeError(
                f"WORK_TREE_MISSING:{subject.work_id}: validator authored a check "
                "but that pull request's tree is unavailable")
        shutil.copytree(tree, gate_dir)
        staged = os.path.join(gate_dir, name)
        with open(staged, "wb") as handle:
            handle.write(check_bytes)
        os.chmod(staged, os.stat(staged).st_mode | 0o755)
        run.log(f"gate workspace assembled at {gate_dir} "
                f"({sum(len(f) for _, _, f in os.walk(gate_dir))} files, the check beside the work)")
        return staged

    def _write_validator_report(self, run: Run, role: RoleResult,
                                grade_tail: str) -> None:
        """Write a fixture-only trace note for the offline plumbing test.

        The shipped path never calls this: its validator authors the executable
        gate, and verdict evidence lands on the PR rather than in a report file.
        """
        report_path = os.path.join(run.roledir(_validator_agent()), "validation_report.md")
        if self.executor.name == "fixture":
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("validation report: the fixture executable ran; "
                        "see its output above.\n")
            run.add_event(_validator_agent(), {"kind": "text",
                                   "text": "[validator] wrote validation_report.md "
                                           "(fixture trace only)"})
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

    # Phase 4: role execution. The shipped AgentCoreExecutor sends each role to its
    # DEPLOYED Runtime, where its CLI builds the artifact and the engine reads it
    # back. The test FixtureExecutor supplies test-owned output in-process so the
    # surrounding lifecycle can be exercised without pretending to be a customer
    # path. On the shipped path, shell output is captured in the role terminal; the
    # validator-authored executable and integrated read-only review decide the verdict.
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
            # The Runtime receives an independent checkout archive, so the role's
            # skill must travel in its own small archive and be expanded beside it.
            if skill_dirs and getattr(self.executor, "name", "") == "agentcore":
                import runtime_stage  # noqa: PLC0415 (lazy, agentcore path only)
                try:
                    runtime_stage.stage_skills(
                        run.run_id, skill_dirs, agent_id=agent_id)
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
            n = self._require_item_change(run, role.agent)
            role.note = f"prepared the backend role patch ({_files(n)})"
            run.log(f"backend: prepared {n} change(s); the validator's check "
                    "decides whether they answer the request")

        def validator(role: RoleResult) -> None:
            # ONE authored check PER PULL REQUEST. The checker is a single registered
            # role serving the whole run, so it loops the builders' pull requests
            # here rather than grading one assembled tree: per item, build that pull
            # request's tree on the current default branch, reset the checker's own
            # worktree to it, and author a check for THAT pull request.
            #
            # Scheduling cost, stated rather than hidden: this node does not start
            # until every routed builder has finished (the graph's
            # ``all_builders_done`` edge), so the first pull request's check waits for
            # the slowest builder. That is SCHEDULING coupling, not verdict coupling.
            # No tree is shared, no order is imposed on the verdicts, and a red pull
            # request never blocks a green sibling from merging.
            self._publish_active_work_items(run)
            install_harness(role.agent)
            run._item_checks = {}
            for item in self._builder_items(run, pending_only=True):
                self._build_item_tree(run, item)
                # A REPAIR ROUND RE-RUNS THE CHECK IT ALREADY HAS. It does not ask the
                # validator to write a new one, and that is a correctness argument
                # before it is a speed one.
                #
                # The check is authored against the REQUEST, not against the code. A
                # repair changes the code and leaves the request alone, so the same
                # check is still the right check -- and re-running it is exactly what
                # re-running CI on a pushed fix does. Re-authoring after seeing the fix
                # is the checker adapting to the work, which is the one thing the
                # maker-checker split exists to prevent: a second check written with
                # the repaired code in view can be softer than the one that caught the
                # defect, and nothing would reveal that.
                #
                # It is also where the clock went. Measured on a live run: authoring
                # took 5m49s, the repair 11m03s, and RE-authoring for round 2 another
                # 9m44s -- to run a check that executes in 4.9 seconds. Reuse gives
                # that back.
                #
                # One case genuinely invalidates the prior check: the base branch moved
                # because a sibling pull request merged, so the checkout it was written
                # against no longer exists. Then, and only then, author again.
                reused = None if run._refresh_context else self._prior_check(run, item)
                if reused:
                    run._item_checks[item.work_id] = self._gate_dir_check_path(
                        run, reused, item)
                    run._acceptance_test_file = run._item_checks[item.work_id]
                    run.log(f"validator: re-running the check already authored for "
                            f"{item.work_id} against the repaired tree (not "
                            "re-authoring: the request did not change)")
                    continue
                self._prepare_checker_checkout(run, role.agent, item)
                # The checker in the maker-checker pair. It AUTHORS the acceptance
                # check for this pull request; the engine executes that file in
                # finalization and reads its real exit code. The engine starts
                # nothing itself: if the work is a service, the authored check stands
                # it up, because only the check knows what running means for THIS
                # deliverable. That is why no protocol, port, or language appears
                # anywhere on this path.
                if self.executor.name == "agentcore":
                    authored = self._cli_validator_authors_test(
                        run, endpoint.get("url", ""), role, item)
                elif self.executor.name == "fixture":
                    produced = self.executor.produce(run, role.agent, role)
                    authored = self._gate_dir_check_path(run, produced, item)
                else:
                    raise RuntimeError(_NO_PRODUCER_ERROR)
                run._item_checks[item.work_id] = authored
                self._keep_check_for_later_rounds(run, item, authored)
                # Kept for the read-only review route and the compose commit, which
                # ship the last authored check beside the work it graded.
                run._acceptance_test_file = authored
                run.log(f"validator: authored the acceptance check for "
                        f"{item.work_id}; its real exit code is that pull "
                        "request's gate")
            role.note = (f"authored one acceptance check per pull request "
                         f"({len(run._item_checks)})")

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
            n = self._require_item_change(run, role.agent)
            role.note = f"prepared the frontend role patch ({_files(n)})"
            run.log(f"frontend: prepared {n} change(s); the validator's check "
                    "decides whether they answer the request")

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
                role.state = "working"
                role.note = ""
                role.last_beat = t0
                item = run.work_items.get(agent_id)
                if item is not None:
                    item.attempt += 1
                    item.state = "working"
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
                    # runs the closure against test-owned fixture output. Either way
                    # the engine collects the artifact; the separately authored
                    # executable and integrated review decide its verdict later.
                    capability = roles.get(agent_id).capability
                    if capability not in work:
                        raise RuntimeError(
                            f"NO_WORK_FOR_CAPABILITY:{capability} (role {agent_id}). "
                            "The roster offers a capability this engine has no closure "
                            "for; there is nothing to substitute.")
                    local_work = work[capability]
                    self.executor.dispatch(run, agent_id, role, local_work)
                    role.state = "done"
                    if item is not None:
                        item.state = "done"
                except Exception as exc:
                    role.state, role.note = "error", f"{type(exc).__name__}: {exc}"
                    if item is not None:
                        item.state = "error"
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

        active_builders = (
            run._active_builders
            if run._active_builders is not None
            else {
                item.agent for item in run.work_items.values()
                if item.kind == roles.BUILDER
            }
        )
        execution_agents = [
            agent_id for agent_id in run.agents
            if (roles.get(agent_id).kind == roles.CHECKER
                or agent_id in active_builders)
        ]
        # Builders start as one entry batch. The checker is selected for this round,
        # but does not become "working" until its graph node actually starts after
        # every selected builder finishes. Builders not selected for repair keep
        # their completed result and unchanged PR.
        has_builder = any(
            roles.get(agent_id).kind == roles.BUILDER
            for agent_id in execution_agents
        )
        for agent_id in execution_agents:
            r = run.progress[agent_id]
            if roles.get(agent_id).kind == roles.CHECKER and has_builder:
                r.state = "pending"
                r.note = "waiting for the selected builders to finish"
            else:
                r.state = "working"
                r.note = ""
            r.last_beat = time.monotonic()

        # Hand the schedule to Strands: builders as one parallel entry batch, the
        # checker behind an explicit AND join. The phase budget becomes the graph's own
        # execution timeout, so a wedged role is bounded by the framework instead of a
        # hand-rolled thread join.
        try:
            graph, _nodes = role_graph.build_graph(
                execution_agents, lambda a: _make_dispatch(run.progress[a], a),
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
        for agent_id in execution_agents:
            r = run.progress[agent_id]
            if r.state == "working":
                stale = now - r.last_beat
                r.state, r.note = "error", (
                    f"role wedged: no progress for {stale:.0f}s, exceeded the "
                    f"{budget}s phase budget")
                run.log(f"{r.role} timed out (wedged {stale:.0f}s) -> role failure", "error")
            elif r.state == "pending":
                r.state, r.note = "error", (
                    "role never started before the agent-execution phase ended")
                run.log(f"{r.role} never started -> role failure", "error")
        errored = [r for r in run.progress.values() if r.state == "error"]
        if errored:
            # Tiered escalation: a single flaky role is ROLE_EXECUTION_ERROR, but
            # ALL routed roles failing is a SYSTEMIC break (harness/env), which a
            # metric filter should alarm on distinctly (a total-failure tier).
            total = len(errored) == len(run.progress) and len(run.progress) > 0
            reason = "ROLE_TOTAL_FAILURE" if total else "ROLE_EXECUTION_ERROR"
            # ...but never relabel OUR failure as the agents'. When every failing
            # role failed because the engine could not read work back that the
            # runtime still holds, the run-level reason must say transport too,
            # or the honest per-role note is contradicted by the headline the
            # attendee actually reads.
            if any("MODEL_QUOTA_EXHAUSTED" in (r.note or "") for r in errored):
                reason = "MODEL_QUOTA_EXHAUSTED"
            elif all("ARTIFACT_TRANSFER_ERROR" in (r.note or "") for r in errored):
                reason = "ARTIFACT_TRANSFER_ERROR"
            elif any("INTEGRATION_CONFLICT" in (r.note or "") for r in errored):
                reason = "INTEGRATION_CONFLICT"
            run.status, run.fail_reason = "failed", reason
            if total:
                run.log(f"agent execution: ALL {len(errored)} routed roles failed "
                        "-> systemic failure (harness or environment)", "error")
            return False
        run.log(f"agent execution complete: {len(execution_agents)} role(s) ran "
                f"this round, "
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
        run._review_work_dir = self._composed_source_dir(target)
        # A review of offline-double work is itself a review of a stub; carry the mark
        # so the integrated review abstains rather than judging something that implements
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

    def _builder_items(self, run: Run, *, pending_only: bool = False
                       ) -> list[_work_items.WorkItem]:
        items = _work_items.dependency_order(
            item for item in run.work_items.values()
            if item.kind == roles.BUILDER)
        if pending_only:
            return [item for item in items if item.merge_state != "merged"]
        return items

    def _record_gate(self, run: Run, gate: dict, stage: str,
                     item: _work_items.WorkItem | None = None) -> None:
        """Record one executable result, attributed to the pull request it judged."""
        run.gate = gate
        row = {
            "sequence": len(run.gate_history) + 1,
            "stage": stage,
            "work_id": item.work_id if item is not None else "",
            "agent": item.agent if item is not None else "",
            "patch_digest": str(getattr(item, "patch_digest", "") or ""),
            "passed": bool(gate.get("passed")),
            "summary": gate.get("summary") or "",
            "checks": list(gate.get("checks") or []),
        }
        run.gate_history.append(row)
        run.log(
            f"executable gate {stage}: "
            f"{'green' if row['passed'] else 'RED'} "
            f"({row['summary']})")

    def _comment_work_item(self, run: Run, item: _work_items.WorkItem,
                           body: str) -> None:
        """Post this pull request's own evidence on its own timeline.

        One pull request, one comment: an attendee reading a pull request sees the
        check and the review for THAT change, not a run-wide digest that mentions
        work they cannot see in the diff.
        """
        if self.executor.name == "fixture":
            return
        posted = github.comment_on_work_item(item, body)
        if posted.get("error"):
            run.log(
                f"{item.work_id}: PR evidence comment failed: "
                f"{posted['error']}", "warn")

    def _assess_pull_request(
        self,
        run: Run,
        item: _work_items.WorkItem,
        gate: dict,
        stage: str,
    ) -> bool:
        """Review ONE pull request, and post its check + Assessment on that PR.

        Skipped only when this exact pull request was already approved and its bytes
        have not changed since: a changed pull request is reviewed again, a
        byte-identical one is not. The reviewer cannot turn a red gate green (that
        branch lives in ``reviewer.assess``), and it never edits code.
        """
        digest = str(item.patch_digest or "")
        prior = (item.review_rounds[-1] if item.review_rounds else {})
        if (gate.get("passed") and prior.get("state") == "approved"
                and digest and digest == run._reviewed_digests.get(item.work_id)):
            run.log(
                f"{item.work_id}: reviewer not rerun at {stage}: "
                f"{digest[:12]} is byte-identical to the approved version")
            assessment = (
                "**Assessment**: unchanged pull request\n\n"
                "The executable check was rerun on the current base branch. This "
                "pull request's bytes are unchanged, so the reviewer was not run "
                "again."
            )
            approved = True
        else:
            verdict = reviewer.assess(
                run, gate, run.iterations, subject=item)
            run.review = verdict.public()
            item.review_rounds.append(verdict.public())
            run._reviewed_digests[item.work_id] = digest
            assessment = verdict.assessment
            approved = verdict.lgtm
            if verdict.review_unavailable:
                run.log(
                    f"{item.work_id}: required review unavailable; this pull "
                    "request is not merged, and no builder is asked to repair a "
                    "review-service outage",
                    "warn",
                )
                item.merge_state = "review_unavailable"
            run.log(f"{item.work_id}: review {verdict.state} at {stage}")

        body = replay.gate_evidence_comment(
            run, gate, stage=stage, item=item, assessment=assessment)
        self._comment_work_item(run, item, body)
        return approved

    def _route_repair(
        self,
        run: Run,
        gate: dict,
        *,
        eligible: list[_work_items.WorkItem] | None = None,
        stage: str,
    ) -> None:
        eligible = eligible if eligible is not None else self._builder_items(
            run, pending_only=True)
        selected, rationale = integration_plan.select_repair_agents(
            run.task,
            run.integration_brief or {},
            eligible,
            {
                "stage": stage,
                "gate": gate,
                "review": run.review or {},
            },
            offline_fixture=(self.executor.name == "fixture"),
        )
        allowed = {item.agent for item in eligible}
        run._active_builders = {agent for agent in selected if agent in allowed}
        reasons = list((run.review or {}).get("reasons") or [])
        if not reasons:
            reasons = [
                c.get("detail", "")
                for c in gate.get("checks", [])
                if not c.get("passed")
            ]
        run.retry_reasons.append({
            "round": run.iterations,
            "stage": stage,
            "gate_summary": gate.get("summary") or "",
            "reasons": reasons,
            "responsible_agents": sorted(run._active_builders),
            "routing": rationale,
        })
        run._refresh_context = (
            f"REPAIR ROUND for {stage}. The executable/review evidence was:\n"
            + "\n".join(f"- {reason}" for reason in reasons[:8])
            + f"\nRouting rationale: {rationale}\n"
            "Update this existing pull request against its current base branch; "
            "do not replace another role's ownership."
        )
        targets = [
            item for item in eligible
            if item.agent in run._active_builders
        ] or eligible
        for target in targets:
            self._comment_work_item(
                run,
                target,
                replay.gate_evidence_comment(
                    run, gate, stage=f"{stage}: repair requested",
                    item=target,
                    assessment=(run.review or {}).get("assessment", "")),
            )
        run.log(
            f"{stage}: bounded repair routed to "
            f"{', '.join(sorted(run._active_builders)) or 'validator only'} "
            f"({rationale})", "warn")

    def _write_integration_brief(self, run: Run, root: str) -> None:
        coordination = os.path.join(root, ".workshop")
        os.makedirs(coordination, exist_ok=True)
        with open(os.path.join(coordination, "integration-brief.md"),
                  "w", encoding="utf-8") as f:
            f.write(run._integration_brief_md)

    def _merge_work_item_locally(
        self, run: Run, item: _work_items.WorkItem
    ) -> dict[str, Any]:
        """Fixture-only private-branch merge; exercises queue structure offline."""
        destination = os.path.join(
            run.workdir, f"merged-{item.work_id}")
        digest = _work_items.apply_patch(
            run.integration_base_dir,
            item,
            run.item_base_dir(item.agent),
            run.roledir(item.agent),
            destination,
            exclude=_work_patch_excluded,
        )
        shutil.rmtree(run.integration_base_dir, ignore_errors=True)
        os.replace(destination, run.integration_base_dir)
        self._write_integration_brief(run, run.integration_base_dir)
        item.merge_state = "merged"
        item.state = "merged"
        return {"merged": True, "sha": digest}

    def _merge_work_item(
        self, run: Run, item: _work_items.WorkItem
    ) -> dict[str, Any]:
        if self.executor.name == "fixture":
            result = self._merge_work_item_locally(run, item)
            run.integration_base = {
                "mode": "fixture",
                "branch": run.final_base_branch,
                "sha": result["sha"],
                "files": sum(
                    1
                    for dirpath, _dirs, filenames in os.walk(
                        run.integration_base_dir)
                    for filename in filenames
                    if not _work_patch_excluded(os.path.relpath(
                        os.path.join(dirpath, filename),
                        run.integration_base_dir).replace(os.sep, "/"))
                ),
            }
            return result

        result = github.merge_work_item(run, item)
        if result.get("error"):
            return result
        # Re-snapshot the DEFAULT branch right after the merge. This is what makes
        # the next pull request's check see this one's code: its tree is rebuilt on
        # the branch as it now stands, so a cross-role seam is exercised for real
        # rather than reasoned about.
        next_base = run.integration_base_dir + ".next"
        snapshot = github.snapshot_branch(
            run.final_base_branch or "", next_base)
        if snapshot.get("error"):
            return {
                "error": "pull request merged but the base snapshot failed: "
                         + str(snapshot["error"]),
            }
        shutil.rmtree(run.integration_base_dir, ignore_errors=True)
        os.replace(next_base, run.integration_base_dir)
        self._write_integration_brief(run, run.integration_base_dir)
        run.integration_base = dict(snapshot)
        return result

    def _refresh_pending_work(
        self,
        run: Run,
        pending: list[_work_items.WorkItem],
        merged_item: _work_items.WorkItem,
    ) -> list[_work_items.WorkItem]:
        """Rebase pending PRs and return owners that need a real agent turn.

        Textually clean is not the same as integrated. When a work item's declared
        dependency just merged, its owner gets one bounded semantic refresh against
        the actual implementation even if Git found no conflict. Other pending PRs
        still rebase mechanically. A real path conflict also goes back to its owner.
        """
        latest_sha = str((run.integration_base or {}).get("sha") or "")
        latest_digest = _work_items.tree_digest(
            run.integration_base_dir, exclude=_work_patch_excluded)
        conflict_rows: list[str] = []
        semantic_rows: list[str] = []
        active: list[_work_items.WorkItem] = []
        for item in pending:
            previous_base_digest = item.base_digest or _work_items.tree_digest(
                run.item_base_dir(item.agent), exclude=_work_patch_excluded)
            dependency_advanced = (
                merged_item.work_id in item.depends_on
                and previous_base_digest != latest_digest
            )
            seed = os.path.join(run.workdir, f"refresh-{item.work_id}")
            conflicts = _work_items.prepare_refresh_checkout(
                item,
                run.item_base_dir(item.agent),
                run.roledir(item.agent),
                run.integration_base_dir,
                seed,
                exclude=_work_patch_excluded,
            )
            self._write_integration_brief(run, seed)
            item_base = run.item_base_dir(item.agent)
            shutil.rmtree(item_base, ignore_errors=True)
            shutil.copytree(run.integration_base_dir, item_base)
            item.base_sha = latest_sha
            item.base_digest = _work_items.tree_digest(
                item_base, exclude=_work_patch_excluded)
            conflict_rows.extend(
                f"{item.work_id}: {row['path']}" for row in conflicts)

            staged = self._stage_refreshed_work_item(run, item, seed)
            run.log(
                f"{item.work_id}: refreshed checkout staged ({staged} files)")
            if conflicts or dependency_advanced:
                if (dependency_advanced
                        and item.dependency_refreshes >=
                        MAX_DEPENDENCY_REFRESHES):
                    item.state = "blocked"
                    item.merge_state = "blocked"
                    raise RuntimeError(
                        f"STALE_PATCH_REFRESH_CAP:{item.work_id}: more than "
                        f"{MAX_DEPENDENCY_REFRESHES} dependency integration "
                        "refresh was requested")
                allowed_attempts = (
                    MAX_ITERATIONS + MAX_DEPENDENCY_REFRESHES
                    if dependency_advanced else MAX_ITERATIONS
                )
                if item.attempt >= allowed_attempts:
                    item.state = "blocked"
                    item.merge_state = "blocked"
                    raise RuntimeError(
                        f"STALE_PATCH_REFRESH_CAP:{item.work_id}: this pull "
                        "request still needs an owner turn after the bounded "
                        f"{allowed_attempts} attempts")
                active.append(item)
                if dependency_advanced:
                    item.dependency_refreshes += 1
                    semantic_rows.append(
                        f"{item.work_id}: dependency {merged_item.work_id} merged")
                continue

            patch = _work_items.diff_trees(
                item,
                item_base,
                run.roledir(item.agent),
                exclude=_work_patch_excluded,
            )
            if not patch.changes:
                raise RuntimeError(
                    f"STALE_PATCH_EMPTY_REFRESH:{item.work_id}: this pull "
                    "request's patch became empty after a sibling merged")
            if self.executor.name == "fixture":
                item.state = "in_review"
                item.stale = False
            else:
                result = github.publish_work_item(
                    run, item, replay.work_item_narrative(run, item))
                if result.get("error"):
                    raise RuntimeError(
                        f"ROLE_PR_REFRESH_ERROR:{item.work_id}: "
                        f"{result['error']}")
            run.log(
                f"{item.work_id}: clean refresh published without rerunning "
                f"{item.agent} ({len(patch.changes)} change(s))")

        context: list[str] = []
        if semantic_rows:
            context.append(
                "SEMANTIC INTEGRATION REFRESH: the work item you depend on is now "
                "merged into this checkout. A clean Git rebase does not prove that "
                "the product is connected. Inspect the actual merged contribution, "
                "wire your assigned outcome to it through the shared contract, and "
                "remove any stand-in, copied service, or duplicate implementation "
                "of that role. This is your one bounded dependency refresh:\n- "
                + "\n- ".join(semantic_rows)
            )
        if conflict_rows:
            context.append(
                "STALE PATCH: a sibling pull request merged first and moved a "
                "path you also changed. Your "
                "checkout contains the latest default branch plus every prior "
                "change that rebased cleanly. Re-read "
                "`.workshop/integration-brief.md` and "
                "`.workshop/refresh.json`, inspect the merged implementation, "
                "and reconcile these shared paths. Your prior versions are under "
                "`.workshop/prior-work`:\n- "
                + "\n- ".join(conflict_rows)
            )
        run._refresh_context = "\n\n".join(context)
        if semantic_rows:
            run.log(
                "SEMANTIC INTEGRATION REFRESH: "
                + "; ".join(semantic_rows)
                + "; the owning role will inspect the merged dependency",
                "warn",
            )
        return active

    def _gate_one_pull_request(
        self, run: Run, item: _work_items.WorkItem, stage: str
    ) -> dict:
        """Rebuild this pull request's tree on the CURRENT base and run its check.

        Rebuilt, not reused: a sibling may have merged since the check was authored,
        and the honest question is whether THIS pull request works against the branch
        it is about to merge into. That is the whole cross-role guarantee.
        """
        check_path = run._item_checks.get(item.work_id, "")
        if not check_path:
            return {
                "passed": False,
                "checks": [{"check": "acceptance_check_authored", "passed": False,
                            "detail": f"the validator authored no check for "
                                      f"{item.work_id}, so nothing proved it; "
                                      "validation is agentic only and there is no "
                                      "fallback grade"}],
                "summary": f"no authored check for {item.work_id}"}
        try:
            self._build_item_tree(run, item)
            check_path = self._gate_dir_check_path(run, check_path, item)
            run._item_checks[item.work_id] = check_path
        except _work_items.StalePatch as exc:
            return {
                "passed": False,
                "checks": [{"check": "pull_request_is_current", "passed": False,
                            "detail": "the base branch changed a path this pull "
                                      "request also changed: "
                                      + ", ".join(c["path"] for c in
                                                  exc.conflicts[:8])}],
                "summary": f"{item.work_id} is behind {run.final_base_branch}"}
        gate = reviewer.run_gate(
            check_path,
            os.path.dirname(check_path),
            run.task,
            run.artifact_endpoint or "",
        )
        self._record_gate(run, gate, stage, item)
        return gate

    def _merge_if_policy_allows(
        self, run: Run, item: _work_items.WorkItem, row: dict
    ) -> bool:
        """Merge this approved pull request, or leave it open for a person.

        ``human_review`` (the default) is the human boundary: a green, approved pull
        request is left OPEN. It is not a failure and not a retry -- it is the state a
        person is supposed to act on, so it counts as success for the run.
        """
        if github.merge_policy() != "auto":
            item.merge_state = "human_review"
            row["state"] = "awaiting_review"
            run.log(f"{item.work_id}: green and approved; left open for a person "
                    "(WORKSHOP_MERGE_POLICY=human_review)")
            return True
        merged = self._merge_work_item(run, item)
        if merged.get("error"):
            row["state"] = "blocked"
            row["error"] = merged["error"]
            item.merge_state = "blocked"
            run.log(f"{item.work_id}: merge failed: {merged['error']}", "warn")
            return False
        row["state"] = "merged"
        row["sha"] = merged.get("sha", "")
        run.log(f"{item.work_id}: merged into {run.final_base_branch}")
        return True

    def _finalize(self, run: Run) -> bool:
        """Take each pull request through its own check, review, and merge.

        Independent by construction: one pull request's outcome never decides
        another's. A red pull request is recorded and the loop CONTINUES to the next
        one, so a green sibling still merges. The repair bound is per pull request
        (``item.attempt``), so N builders allow at most N repair rounds, one each,
        and this can never become re-entrant.

        Returns True always on the build path: every pull request reaches its own
        terminal state here, so there is no whole-run round to restart.
        """
        run.phase = "finalization"

        read_only = bool(run.route and run.route.get("read_only"))
        if read_only:
            return self._finalize_read_only(run)

        items = self._builder_items(run)
        run.role_prs = [{
            "work_id": item.work_id,
            "agent": item.agent,
            "role": item.role,
            "pr_url": (item.pr or {}).get("pr_url"),
            "state": "checking",
        } for item in items]

        for item, row in zip(items, run.role_prs):
            stage = f"{item.work_id} round {item.attempt}"
            run.log(f"gate: running {item.work_id}'s authored check "
                    f"on {run.final_base_branch}")
            gate = self._gate_one_pull_request(run, item, stage)
            approved = gate.get("passed") and self._assess_pull_request(
                run, item, gate, stage)
            if not approved and not gate.get("passed"):
                # A red gate is never approved; say so on the pull request without
                # asking the reviewer, whose own red-gate branch would say the same.
                self._assess_pull_request(run, item, gate, stage)

            if not approved and item.merge_state != "review_unavailable":
                # ONE bounded repair for THIS pull request only.
                if item.attempt >= MAX_ITERATIONS:
                    row["state"] = "blocked"
                    row["error"] = "ITERATION_CAP"
                    item.merge_state = "blocked"
                    run.log(f"{item.work_id}: still not acceptable after "
                            f"{item.attempt} round(s); its pull request keeps the "
                            "evidence", "warn")
                    continue
                if not self._repair_pull_request(run, item, gate, stage):
                    row["state"] = "blocked"
                    row["error"] = "ROLE_EXECUTION_ERROR"
                    item.merge_state = "blocked"
                    continue
                stage = f"{item.work_id} round {item.attempt}"
                gate = self._gate_one_pull_request(run, item, stage)
                approved = gate.get("passed") and self._assess_pull_request(
                    run, item, gate, stage)
                if not approved and not gate.get("passed"):
                    # Exactly as in round 1: a red gate is never approved, and it must
                    # SAY SO on the pull request. Without this the `and` above
                    # short-circuits, nothing is posted, and the pull request a person
                    # is then told to go open carries only its first round -- while the
                    # log claims it "keeps the evidence". A live routed run ended
                    # ROLE_PR_BLOCKED with the blocking round visible nowhere but the
                    # engine's own memory.
                    self._assess_pull_request(run, item, gate, stage)
                if not approved:
                    row["state"] = "blocked"
                    row["error"] = ("ITERATION_CAP" if gate.get("passed")
                                    else "GATE_RED")
                    item.merge_state = "blocked"
                    continue

            if item.merge_state == "review_unavailable":
                row["state"] = "blocked"
                row["error"] = "REVIEW_UNAVAILABLE"
                continue
            self._merge_if_policy_allows(run, item, row)
            row["pr_url"] = (item.pr or {}).get("pr_url")

        self._settle_run(run, items)
        return True

    def _repair_pull_request(
        self, run: Run, item: _work_items.WorkItem, gate: dict, stage: str
    ) -> bool:
        """Send the recorded reasons back to the ONE role that owns this PR.

        The reviewer reports; the owner fixes. That keeps maker-is-never-checker
        intact, and it is also what a person does: the reviewer comments, and the
        author pushes to the same pull request.
        """
        self._route_repair(run, gate, eligible=[item], stage=stage)
        run.iterations += 1
        run.log(f"{item.work_id}: one bounded repair updates this same pull request",
                "warn")
        return self._execute(run)

    def _settle_run(self, run: Run, items: list[_work_items.WorkItem]) -> None:
        """Decide the run's terminal state from the pull requests' own outcomes."""
        blocked = [i for i in items if i.merge_state == "blocked"]
        unavailable = [i for i in items
                       if i.merge_state == "review_unavailable"]
        settled = [i for i in items
                   if i.merge_state in ("merged", "human_review")]
        # `pr_url` stays the single-role shortcut for the frozen public contract;
        # `role_prs` is the real answer when more than one builder ran.
        run.pr_url = (items[0].pr or {}).get("pr_url") if len(items) == 1 else None
        # A run with no GitHub side effect at all must SAY so rather than leaving the
        # PR field empty and letting a reader assume it just was not reported. The
        # offline double has no gateway, and a real run without one fails pre-flight,
        # so this is the honest report of "the build is real, the publish was not".
        published = [item for item in items if (item.pr or {}).get("pr_url")]
        if published:
            run.pr = dict(published[-1].pr or {})
        elif items:
            skipped = (items[0].pr or {}).get("skipped")
            run.pr = {
                "error": "PR_NO_GATEWAY: the build completed with no GitHub side "
                         "effect. Wire the GitHub MCP Gateway and submit a new run "
                         "to create real pull requests."
            } if skipped else dict(items[0].pr or {})
        if settled:
            self._compose_commit(run)
        if unavailable:
            run.status, run.fail_reason = (
                "needs_human",
                "REVIEW_UNAVAILABLE:" + ",".join(i.work_id for i in unavailable))
        elif blocked:
            run.status, run.fail_reason = (
                "needs_human",
                "ROLE_PR_BLOCKED:" + ",".join(i.work_id for i in blocked))
            run.log(
                f"{len(settled)} pull request(s) settled, "
                f"{len(blocked)} left open for a person", "warn")
        elif not settled:
            run.status, run.fail_reason = "needs_human", "ROLE_PR_BLOCKED:none"
        else:
            run.status = "passed"
        self._ledger(run)

    def _finalize_read_only(self, run: Run) -> bool:
        """The review workflow: re-run the TARGET run's own authored check."""
        check_path = getattr(run, "_acceptance_test_file", "") or ""
        gate = reviewer.run_gate(
            check_path,
            getattr(run, "_review_work_dir", "") or os.path.dirname(check_path),
            run.task,
            run.artifact_endpoint or "",
        )
        run.gate = gate
        verdict = reviewer.assess(run, gate, run.iterations)
        run.review = verdict.public()
        target = self._runs.get(
            run._review_target) if run._review_target else None
        if target is not None and target.pr_url:
            run.pr = dict(target.pr or {})
            github.post_review(run, verdict.assessment)
        if verdict.lgtm:
            run.status = "passed"
        elif verdict.review_unavailable:
            run.status, run.fail_reason = "needs_human", "REVIEW_UNAVAILABLE"
        else:
            run.status, run.fail_reason = "needs_human", "ITERATION_CAP"
        self._ledger(run)
        return True

    # The composed repo is shared by every run; git allows one writer at a time
    # (index.lock), so compose is serialized across concurrent runs. A bare Lock
    # would deadlock the whole engine if one compose ever hung while holding it,
    # so this is a self-healing lease that auto-evicts a wedged holder.
    _COMPOSE_LEASE = _Lease(COMPOSE_LEASE_STUCK_S)

    def _compose_commit(self, run: Run) -> None:
        """Record the validated work in one local evidence commit.

        GitHub already holds each role's own pull request. This separate
        scratch commit powers the console's Changes view and carries the validator's
        authored check; it is never pushed as a substitute PR.
        """
        Engine._COMPOSE_LEASE.acquire(run.run_id)
        try:
            self._compose_commit_locked(run)
        finally:
            Engine._COMPOSE_LEASE.release(run.run_id)

    def _compose_commit_locked(self, run: Run) -> None:
        repo = os.path.join(_RUNS_DIR, "composed")
        # The Gateway queue operates directly on the attendee repo. This local
        # scratch repo is only durable evidence for the console and diagnostics.
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
        # this run's Changes tab would drop those files. Rooting at the empty base
        # makes each commit's diff exactly its own deliverable set.
        root = subprocess.run(["git", "-C", repo, "rev-list", "--max-parents=0", "main"],
                              capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        base_ref = root[-1] if root else "main"
        # Return the SHARED work tree to a clean state BEFORE switching branches.
        # This repo is shared by every run, and `git checkout -B` REFUSES to run when
        # a tracked file it would overwrite is modified ("Your local changes to the
        # following files would be overwritten by checkout"), which surfaced as a
        # compose that failed with a bare `returned non-zero exit status 1` and looked
        # like a race. It is not a race: a previous compose interrupted between its
        # file copies and its commit leaves tracked modifications behind, and every
        # later run in the same runs dir then fails deterministically until someone
        # cleans the directory by hand.
        #
        # `reset --hard` discards nothing that matters: this is a local scratch
        # evidence repo, every prior run's commit is preserved on its own branch, and
        # each compose rewrites its whole tree from the gated pull request anyway.
        # It must come BEFORE the checkout to be any use, and it complements rather
        # than replaces the `clean` below (reset restores tracked files; clean removes
        # untracked leftovers a previous run's role wrote and this one did not).
        subprocess.run(["git", "-C", repo, "reset", "-q", "--hard"],
                       check=False, timeout=20, env=git_env)
        subprocess.run(["git", "-C", repo, "clean", "-fdq"],
                       check=False, timeout=20, env=git_env)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-B", branch, base_ref],
                       check=True, timeout=20, env=git_env)
        # Drop any leftover from a prior run (a file a previous run's role wrote and
        # this one did not), so the commit is exactly this run's deliverable.
        subprocess.run(["git", "-C", repo, "clean", "-fdq"], check=True, timeout=20, env=git_env)
        # Publish the tree of the LAST pull request this run took through its gate.
        # Every pull request tree is built on the default branch AS IT STANDS, so the
        # last one already contains whatever merged before it: that tree IS the
        # repository state this run produced. This is a local scratch commit for the
        # console's Changes tab only. GitHub already holds the real record -- one pull
        # request per role, each with its own check and review -- and nothing here is
        # on the verdict path.
        source = self._composed_source_dir(run)
        if not source:
            raise RuntimeError("WORK_TREE_MISSING: no gated pull request tree to compose")
        copied = 0
        for dirpath, dirnames, filenames in os.walk(source):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, source)
                dest = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(full, dest)
                copied += 1
        run.log(f"compose: copied the last gated pull request tree "
                f"({copied} file(s))")
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

    def _composed_source_dir(self, run: Run) -> str:
        """The tree that represents what this run produced, or "" when none exists.

        When any pull request MERGED, the base-branch snapshot is that answer: each
        merge re-snapshots the branch, so the snapshot accumulates every merged
        role's work. Using the last single pull request's tree instead would drop the
        earlier roles' files, which is exactly the bug this comment exists to prevent.

        With nothing merged (``human_review``, or a blocked run), fall back to the
        last pull request tree that was built at all, so a run still composes
        something a person can read.
        """
        ordered = self._builder_items(run)
        if any(item.merge_state == "merged" for item in ordered):
            if os.path.isdir(run.integration_base_dir):
                return run.integration_base_dir
        # Nothing merged (human_review leaves every pull request open, which is the
        # DEFAULT). Overlay each gated pull request's own changed files onto the base
        # so the local Changes tab shows every role's work, not just the last one's.
        # This is a reporting convenience only; the real record is the pull requests.
        settled = [item for item in ordered
                   if os.path.isdir(run.item_tree_dir(item.work_id))]
        if not settled:
            return ""
        overlay = os.path.join(run.workdir, "composed-view")
        shutil.rmtree(overlay, ignore_errors=True)
        if os.path.isdir(run.integration_base_dir):
            shutil.copytree(run.integration_base_dir, overlay)
        else:
            os.makedirs(overlay, exist_ok=True)
        for item in settled:
            tree = run.item_tree_dir(item.work_id)
            for rel in item.changed_files:
                src = os.path.join(tree, *rel.split("/"))
                if not os.path.isfile(src):
                    continue
                dest = os.path.join(overlay, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
        return overlay

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
# Our `needs_human` covers two very different situations: validation stayed blocked
# on real work (read the gate and review evidence) and a role never produced anything
# (a transport or turn failure, just resubmit). Same status, opposite next action,
# and the raw token said neither.
_NEXT_ACTION = {
    # A blocked run may already have merged some pull requests while another stayed
    # red. ITERATION_CAP can mean a red executable OR a required review finding.
    "ITERATION_CAP":
        "Do not resubmit this request. The candidate still had blocking gate or "
        "review evidence after the bounded re-implement round, so no final "
        "integration pull request was opened. Read the latest gate.summary and the "
        "integrated review evidence on the existing role pull requests.",
    "ROLE_EXECUTION_ERROR":
        "A role's turn produced no usable work. This is usually transient: submit the "
        "SAME request again. Do not try to finish it by dispatching one role by hand.",
    # Distinct from the above ON PURPOSE: the agent SUCCEEDED and its work is still
    # in the runtime workspace. Saying "the role produced nothing" here would send
    # the reader to debug an agent that did its job.
    "ARTIFACT_TRANSFER_ERROR":
        "The role's work exists in its runtime workspace but the engine could not "
        "read it back, so this is our transport failure and not a failed agent turn. "
        "Submit the SAME request again; the workspace listing is in the fail reason.",
    "ROLE_TOTAL_FAILURE":
        "EVERY routed role failed, which points at the harness or the environment "
        "rather than the request: check that each role's runtime is wired and READY, "
        "then resubmit.",
    "MODEL_QUOTA_EXHAUSTED":
        "The selected model's daily token allowance is exhausted. Do not resubmit "
        "now. Wait for the allowance to reset or choose a model with available "
        "capacity, then submit the SAME request once.",
    "ROLE_PR_BLOCKED":
        "One or more pull requests did not become mergeable: their check stayed red, "
        "a review finding remained after the one bounded repair, or the merge itself "
        "was refused. The work ids are in the reason. Open each named pull request, "
        "read its executable check and Assessment comment, and continue from there as "
        "a person. Pull requests that did pass are already settled; do not resubmit "
        "the whole build to retry one of them.",
    "STALE_PATCH_REFRESH_CAP":
        "A pull request fell behind the default branch and used up its one bounded "
        "refresh. Open the named pull request, update its branch yourself, and "
        "re-run its check as a person.",
    "STALE_PATCH_EMPTY_REFRESH":
        "After a sibling merged, the named pull request had nothing left to "
        "contribute: its change is already on the default branch. Close it, or open "
        "it and confirm nothing is missing.",
    "WORK_TREE_MISSING":
        "The validator authored a check but the pull request tree it was authored "
        "against is gone, so nothing could be executed. This is an engine-side "
        "failure, not a failed agent turn: retry the same request once.",
    "REVIEW_UNAVAILABLE":
        "The executable check passed, but the required review did not run for the "
        "named pull request(s). Keep them open and retry after model access is "
        "restored; do not ask builders to change code for this outage.",
    "ENGINE_STALL":
        "The run ended without reaching a verdict. Resubmit; if it stalls again, the "
        "engine log for this run id is the place to look.",
    "COORDINATOR_SESSION_INTERRUPTED":
        "The coordinator Runtime was recycled while the background build was active. "
        "Submit the SAME request again and keep polling that session until it reaches "
        "a terminal status.",
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
                pr: dict | None = None, pr_url: str | None = None,
                role_prs: list[dict] | None = None) -> str:
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
        rows = list(role_prs or [])
        merged = [r for r in rows if r.get("state") == "merged"]
        waiting = [r for r in rows if r.get("state") == "awaiting_review"]
        if waiting:
            return (
                f"{len(waiting)} pull request(s) passed their check and review and "
                "are open for you to merge. Read each one's Assessment comment, then "
                "merge it (or set WORKSHOP_MERGE_POLICY=auto to have the engine "
                "merge approved pull requests itself).")
        if merged:
            return (
                f"{len(merged)} pull request(s) passed their own check and review "
                "and merged into the default branch. Read each one's Assessment "
                "comment for the evidence.")
        if pr_url:
            return ("Open the role pull request and read its executable check and "
                    "Assessment comment.")
        pr_error = str(pr.get("error") or "")
        if pr_error.startswith("PR_NO_GATEWAY"):
            return (
                "The build completed without a GitHub side effect. Wire the GitHub "
                "MCP Gateway and submit a new run to create real pull requests.")
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


_RESUBMITTABLE_REASONS = {
    "ROLE_EXECUTION_ERROR",
    "ARTIFACT_TRANSFER_ERROR",
    "ROLE_TOTAL_FAILURE",
    "ENGINE_STALL",
    "COORDINATOR_SESSION_INTERRUPTED",
    # The validator authored a check and the engine then lost the tree it was
    # authored against, so nothing was ever executed. That is OUR bookkeeping
    # failure, not a judged outcome: no verdict was reached, so there is nothing
    # for a person to read and repeating the request is the honest recovery. It is
    # deliberately NOT grouped with ROLE_PR_BLOCKED / ITERATION_CAP, which mean a
    # real gate or review decided against real work.
    "WORK_TREE_MISSING",
}


def resubmission_allowed(status: str, fail_reason: str | None) -> bool:
    """Whether immediately repeating the same request can recover this outcome."""
    if status not in ("failed", "needs_human"):
        return False
    reason = (fail_reason or "").split(":")[0].strip()
    return reason in _RESUBMITTABLE_REASONS


def public_result(run: Run) -> dict:
    return {
        "run_id": run.run_id,
        "status": run.status,
        # CLI users poll this payload for 10-20 minutes. A bare "running" makes a
        # healthy build indistinguishable from a stuck one even though the engine
        # already tracks each role. Keep the same phase/progress facts the console
        # exposes on its Run endpoint.
        "phase": run.phase,
        "progress": public_progress(run),
        "work_items": {
            agent: item.public()
            for agent, item in run.work_items.items()
        },
        "integration_brief": run.integration_brief,
        "integration_base": run.integration_base,

        "final_base_branch": run.final_base_branch,
        "role_prs": run.role_prs,
        "gate_history": run.gate_history,
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
        "next_action": next_action(
            run.status, run.fail_reason, run.pr, run.pr_url, run.role_prs),
        "resubmission_allowed": resubmission_allowed(
            run.status, run.fail_reason),
    }
