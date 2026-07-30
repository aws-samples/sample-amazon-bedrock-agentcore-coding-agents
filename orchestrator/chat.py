"""The orchestrator's brain: the Strands agent you chat with, shared by the
deployed runtime (``orchestrator-agent/main.py``) and the console's chat endpoint.

This is the ONE definition of the orchestrator's system prompt, its tools, and
how a conversation streams. ``main.py`` imports ``build_agent`` to host it on
AgentCore Runtime; ``connection_api`` imports ``stream_chat`` to drive the SAME
agent in-process behind the console's chat box. Real-only: the dispatch tools
submit runs to the engine, which sends each role to its DEPLOYED runtime.

The key behavior the console needs: a chat turn is a NORMAL conversation by
default: "hi" gets a plain answer, no run, no "Running". A run is born ONLY when
the model actually calls a ``dispatch_*`` / ``run_build`` tool. A
``BeforeToolCallEvent`` hook fires ``on_run(run_id, kind)`` at that exact moment,
so the UI reveals the run panel then, not before. The dispatch tools are
NON-BLOCKING: they kick the run and return its id immediately, so the chat keeps
streaming while the build proceeds and the UI polls the run for live status.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

import engine as _engine          # the in-process build engine
import policy as _policy          # the guardrail exec_command is screened against
import presets as _presets        # role selection (never task classification)
import roles as _roles            # the ONE declarative roster (configurable)
import run_store as _run_store    # durable run state (a verdict outlives its session)

# One engine instance backs every conversation in this process. REAL-ONLY: it
# dispatches each routed role to its DEPLOYED AgentCore Runtime; a role with no
# wired runtime ARN fails loud. The console wires its OWN engine in via
# ``use_engine`` so the runs the chat tools create are the same runs its
# /api/runs endpoints poll; standalone (the deployed runtime) uses this default.
ENGINE = _engine.Engine()


def use_engine(engine: Any) -> None:
    """Share an existing Engine so chat-created runs are visible to the caller's
    run endpoints. The console calls this at import; the deployed runtime does not
    (its tools and its entrypoint already share this module's ENGINE)."""
    global ENGINE
    ENGINE = engine

SYSTEM_PROMPT = """\
You are the orchestrator for a multi-agent coding harness, a chatbot the user \
talks to. Hold a normal conversation: answer questions, explain what you can do, \
and only build when the user actually asks you to.

## Your agents
They are listed below under "Your roster", generated from the roles this \
deployment actually serves and wires. Each is a coding agent deployed on its own \
AgentCore Runtime and called AS A TOOL. Each type is a FLEET, not one agent; you \
dispatch to a TYPE and the runtime picks an instance. You never address one \
instance, and you never assume a role that is not in your tool list exists.

## Converse first: do not dispatch on a greeting or a question
If the user greets you, asks what you do, or asks a question, reply in words. Do \
not call any tool. A dispatch tool spins up a real microVM; never call one to be \
eager.

## Inspect the workspace before you dispatch (read-only tools)
You can look at your own workspace to answer a question or ground a decision \
WITHOUT dispatching: read_file(path) reads a file, list_files(path) lists a \
directory, grep_workspace(pattern) searches, and exec_command(command) runs one \
bounded shell command (screened by the governance policy). Use them to answer \
"what does this module expose?", to confirm a file exists, or to check a detail \
before deciding which agent to dispatch. They are cheap and local; a dispatch \
spins up a real microVM, so look first when looking answers the question.

## Clarify before you dispatch
When the request is for work but is ambiguous or under-specified (unclear which \
agents, missing the target module or file, two plausible readings), ask one concise \
clarifying question and stop. Prefer inspecting the workspace to resolve an \
ambiguity you can answer yourself; ask the user only when inspection cannot. \
Dispatch only when the ask is unambiguous.

A BUILD request is under-specified in two more ways that matter, and both change \
what gets built rather than who builds it. Ask about them, ONCE, in a single \
message, before the first `run_build` of a new project:
- **Stack.** Any language, framework, or storage preference, or should the agents \
choose? Left unasked, each agent picks alone and you get a hand-rolled server \
where the user wanted FastAPI, or plain HTML where they wanted React.
- **Scope and shape.** State the features you understood as a short list and ask \
whether that is the right set. A vague request becomes a toy deliverable, and a \
reviewer cannot tell an agent that under-built from a user who under-asked.
Offer a sensible default in the same breath ("otherwise I will have them choose, \
aiming for a production-shaped project rather than a script") so a user who does \
not care can just say "go". Ask once and then build: this is one question, not an \
interview, and never a reason to stall a clear request. If the user already named \
their stack and features, or says "just build it", dispatch immediately.

## How to act once the ask is clear
- Focused single-role job (rebuild the UI, patch the backend): call the matching \
dispatch_* tool. It returns a run id immediately and the build runs in the \
background. State that it started and which agent owns it.
- Full build that must be integrated and graded: call run_build(task). Every builder \
gets an isolated work id and role PR. Their patches form a candidate, the checker's \
authored executable gates it, and a private merge queue reruns that gate before a \
final PR can target the default branch. Pass the user's request text VERBATIM as task. \
It can be ANY request: \
nothing classifies it and nothing maps it to a sample, so there is no wording to \
get right.
If the user has no idea yet, call list_presets() and offer one; those are example \
starting points, not a limit on what can be built.

## Explicit preset command
The console and CLI accept `preset=<id>` as a concise build request. When the \
user's message has exactly that form, it is already unambiguous: call \
`run_build(task="", preset="<id>")` immediately. Do not call `list_presets`, \
describe the preset, or ask a clarifying question first. This applies to any id \
the user supplies; the tool and routing layer validate it and fail loud if it does \
not exist.

After `run_build`, report only the roles in the tool result's `agents` list. Never \
infer a role count from the roster or claim that every role works in parallel. \
Builders may run in parallel, but the checker waits for their combined work.

## Reading back a run you did not start
run_status(run_id) answers for runs from EARLIER sessions too: the engine persists \
every verdict, so an expired session no longer loses the result. If the user has \
lost their run id, call list_runs() for the recent builds and their outcomes rather \
than telling them the run is gone.

## If a build did not complete, READ next_action. Never improvise, and never loop
Every terminal result carries a `next_action` field. It is derived from the actual
fail reason, so it already knows which of the two very different `needs_human` cases
you are in. FOLLOW IT rather than deciding for yourself.

Do NOT try to "finish it yourself" by dispatching individual roles, hand-composing
files, or dispatching the validator alone: those paths do not create the integration
candidate, run the merge queue, or open the final PR the way run_build does, and a
review with no PR to review just fails `NO_RUN_TO_REVIEW`.

The two cases, because they have OPPOSITE recoveries:

* A role produced nothing (`ROLE_EXECUTION_ERROR`, `ROLE_TOTAL_FAILURE`,
  `ARTIFACT_TRANSFER_ERROR`), or the coordinator Runtime was recycled mid-build
  (`COORDINATOR_SESSION_INTERRUPTED`). Nothing was judged, so the work is unproven
  rather than rejected. Call run_build ONCE more with the SAME task text, and say
  you are resubmitting.
* The gate stayed RED on real work (`ITERATION_CAP`). The roles built something and
  the validator's own check rejected it, twice, having already had its bounded
  re-implement round. Resubmitting here is not recovery, it is the unbounded loop the
  cap exists to prevent: it spends another full build to reach the same verdict, and
  it hides a real red gate behind "let me try again". REPORT it instead. Quote the
  failing lines from `gate.summary` (they are the check's own output, so they name
  exactly what the deliverable did not do) and stop. It is the human's call whether
  to change the request, change the deliverable, or accept the finding.

Resubmit AT MOST once per request in a session. If a resubmitted run also does not
complete, report that and stop; do not start a third.

## Drive a live terminal directly (when the agent's terminal is open)
When the user is watching an agent's interactive terminal and wants you to drive it \
turn by turn, use agent_send(agent_id, message) to type into that same terminal \
(the user sees your message as an "[orchestrator]" line), agent_read(agent_id) to \
see what the agent printed, and agent_status(agent_id) to check a terminal is open. \
agent_id is one of the role ids in your roster below. This talks to the SAME live \
session the user is watching, so keep turns purposeful; it is for interactive \
guidance, not for kicking a background build (use dispatch_*/run_build for that).

## Voice
Write like a senior engineer: precise, terse, technical. No emoji, no exclamation \
marks, no filler. Report what happened (which agents ran, the run id, the gate \
result) in plain declarative sentences. Never claim a build passed unless a tool \
reported it, and never fabricate a result or a PR URL.
"""


def _dispatch_tool_names() -> set[str]:
    """The tools whose firing means "a run started" and should reveal the run panel
    in the UI: one per served role, plus run_build. Derived from the roster, so a
    roster change cannot leave a dispatch tool unrecognized here (which would have
    silently stopped the UI from ever showing that role's run).
    list_presets/run_status start nothing and are deliberately absent."""
    return {r.dispatch_tool for r in _roles.roster()} | {"run_build"}


def _wired_roles() -> set[str]:
    """The set of roles with a wired runtime ARN (from runtime_config). The
    dispatch tools are generated from this, so the orchestrator only offers
    agents that actually exist. Empty set if nothing is wired (or on any error),
    which yields a converse-only agent (list_presets + run_status), never a tool
    that would fail loud the moment the model called it."""
    try:
        import runtime_config
        return {r["role"] for r in runtime_config.status()["roles"]
                if r.get("wired") and r["role"] != "orchestrator"}
    except Exception:
        return set()


def _kick(agent_id: str | None, task: str, preset: str | None = None) -> str:
    """Submit a run (focused on one builder when agent_id is set, else routed)
    WITHOUT blocking, and return its id immediately. The chat keeps streaming; the
    console polls the run for live status. The 'a run started' UI signal is NOT
    raised here; it is read off the tool RESULT by an AfterToolCallEvent hook,
    so it works regardless of which thread strands runs the tool on.

    The CHECKER always rides along with a builder. Validation is agentic only, so a
    builder dispatched alone would produce work with no authored acceptance check,
    and with no check the gate is red by design. Focusing a run means choosing which
    BUILDER works, never dropping the verification."""
    agents = None
    checkers = list(_roles.checker_ids())
    if agent_id:
        # A focused run: the named role, plus the checker unless the named role IS
        # the checker. Expressed in kinds, so it holds for any roster.
        agents = ([agent_id] if agent_id in checkers
                  else [agent_id] + checkers)
    elif not preset:
        # A full build with no roles named: every served role works. The request is
        # the user's, so there is nothing to classify; the roles are simply all of
        # them, however many this deployment serves.
        agents = list(_roles.roster_ids())
    run = ENGINE.submit(task, agents=agents, preset=preset)
    return run.run_id


# --------------------------------------------------------------------------- #
# The tools. Imported by main.py too, so there is ONE definition. They are
# created by a factory because @tool decoration happens against the live strands
# import; keeping them in a function lets main.py and the console share them
# without import-order surprises.
# --------------------------------------------------------------------------- #
def build_tools() -> list:
    from strands import tool  # local import: strands is an agent-runtime dep

    @tool
    def list_presets() -> str:
        """The starting points an attendee can begin from: id, title, the roles each
        uses, and its request text. They are EXAMPLES, not a menu of what is
        supported: any request at all can be built with run_build. Starts nothing."""
        return json.dumps({"presets": _presets.public_presets()})

    def _make_dispatch(role: _roles.Role):
        """Build ONE role's dispatch tool from its registry entry.

        Generated rather than hand-written so the tool list is exactly the roster:
        adding, hiding, or swapping a role changes which tools exist with no edit
        here, and a role can never be missing its tool (or have a stale one).
        """
        def dispatch(task: str) -> str:
            return json.dumps({"run_id": _kick(role.id, task), "agent": role.id,
                               "kind": role.capability, "status": "started"})
        dispatch.__name__ = role.dispatch_tool
        dispatch.__qualname__ = role.dispatch_tool
        # The docstring IS the tool description the model reads, so it carries this
        # role's real job from the registry.
        focus = ("the acceptance check only, and it never edits the work"
                 if role.kind == _roles.CHECKER else f"the {role.capability} only")
        dispatch.__doc__ = (
            f"Start the {role.capability.upper()} role ({role.label}) on its deployed "
            f"Runtime: {focus}. {role.description} Returns immediately with a run id; "
            f"the work runs in the background.")
        return tool(dispatch)

    @tool
    def run_build(task: str, preset: str = "") -> str:
        """Start a FULL build of ANY request. Every role on the roster works, their
        isolated role pull requests enter a private integration queue, the checker
        authors and executes a gate for the candidate after every merge, and one
        final integration pull request collects the evidence. Returns immediately
        with a run id; the build runs in the background.

        Pass the user's request text VERBATIM as task. It can be anything at all:
        nothing here classifies it or maps it to a sample, so there is no wording to
        get right. Optionally pass a `preset` id (see list_presets) to start from one
        of the example requests instead."""
        if not task.strip() and not preset:
            return json.dumps({
                "error": "EMPTY_TASK",
                "hint": "No run was started. Ask the user what they want built, in "
                        "their own words, or offer a starting point from list_presets.",
            })
        agents = (
            _presets.resolve(preset=preset).agents
            if preset else list(_roles.roster_ids())
        )
        return json.dumps({
            "run_id": _kick(None, task, preset=preset or None),
            "kind": "build",
            "status": "started",
            "agents": agents,
        })

    @tool
    def run_status(run_id: str) -> str:
        """Read back the current state for a run id a dispatch_*/run_build tool
        returned: phase, per-role progress, gate result, review state, and the PR URL
        if one opened.

        Answers for a run this session did not submit, by reading the state the
        engine persisted when the run reached a terminal state. Without that, a
        recycled or expired session lost the verdict permanently: the build had
        finished and the PR was open, but nobody could ask what happened.
        """
        run = ENGINE.get(run_id)
        if run is not None:
            return json.dumps(_engine.public_result(run))
        # Not live here: fall back to the durable record.
        saved = _run_store.load(_engine._RUNS_DIR, run_id)
        if saved is not None:
            if _run_store.active_snapshot_is_stale(saved):
                reason = "COORDINATOR_SESSION_INTERRUPTED"
                return json.dumps({
                    **saved,
                    "status": "needs_human",
                    "fail_reason": reason,
                    "next_action": _engine.next_action(
                        "needs_human", reason, saved.get("pr"),
                        saved.get("pr_url"),
                        saved.get("integration_conflicts")),
                    "source": "persisted",
                })
            return json.dumps({**saved, "source": "persisted"})
        recent = [r.get("run_id") for r in
                  _run_store.recent(_engine._RUNS_DIR, limit=5) if r.get("run_id")]
        return json.dumps({
            "error": f"UNKNOWN_RUN:{run_id}",
            "hint": "This session did not submit that run and no persisted state "
                    "was found for it. Check the run id, or inspect the pull "
                    "request on the repository.",
            "recent_runs": recent,
        })

    @tool
    def list_runs() -> str:
        """The most recent builds this workshop has run, newest first.

        The answer to "what did I run?" when a session id or a run id has been
        lost, which is otherwise a dead end: run ids are minted per run and the
        only other record is the pull request itself.
        """
        rows = _run_store.recent(_engine._RUNS_DIR, limit=10)
        return json.dumps({"runs": [
            {k: r.get(k) for k in ("run_id", "status", "task", "preset",
                                   "pr_url", "fail_reason", "_saved_at")}
            for r in rows]})

    # --- Interactive control of a LIVE agent terminal (shared PTY, F1) -------
    # These talk to the SAME run.sh TUI the human is watching on the Agents page
    # (server fan-out: one PTY, both subscribe). agent_send announces the turn as
    # a "[orchestrator]" banner in the human's terminal, then types it; agent_read
    # returns the current screen. Lazy import: runtime_shell lives in the console's
    # interactive-api dir, present only when the console hosts the orchestrator.
    def _shell_mod():
        import runtime_shell  # noqa: PLC0415 (optional, console-only)
        return runtime_shell

    @tool
    def agent_send(agent_id: str, message: str) -> str:
        """Send a message into a coding agent's LIVE interactive terminal (the same
        Claude Code / opencode TUI the human is watching), then return what the
        agent has printed so far. Use agent_id 'claude-code', 'opencode', or
        'claude-code-validator'. The agent's terminal must already be open. Follow up
        with agent_read to see more output as the agent works."""
        try:
            m = _shell_mod()
        except Exception:
            return json.dumps({"error": "interactive terminals are not available here"})
        out = m.agent_send(agent_id, message)
        if "error" in out:
            return json.dumps(out)
        import time as _t
        _t.sleep(1.5)  # let the first output land before the read-back
        return json.dumps({**out, "screen": m.agent_read(agent_id).get("output", "")})

    @tool
    def agent_read(agent_id: str) -> str:
        """Read the current screen of a coding agent's LIVE terminal (claude-code /
        opencode / claude-code-validator), to see what it printed since your last agent_send."""
        try:
            m = _shell_mod()
        except Exception:
            return json.dumps({"error": "interactive terminals are not available here"})
        return json.dumps(m.agent_read(agent_id))

    @tool
    def agent_status(agent_id: str) -> str:
        """Check whether a coding agent (claude-code / opencode / claude-code-validator)
        has a LIVE terminal open that you can drive with agent_send/agent_read."""
        try:
            m = _shell_mod()
        except Exception:
            return json.dumps({"error": "interactive terminals are not available here"})
        return json.dumps(m.agent_status(agent_id))

    # --- Workspace inspection: the Claude-Code-style toolset -----------------
    # The orchestrator can READ its own workspace and run a bounded command,
    # so it can answer "what is already in this workspace?" or check a file BEFORE
    # deciding whether (and how) to dispatch, instead of spinning up a microVM
    # just to look. All four resolve paths under the workspace root
    # (WORKSHOP_REPO_ROOT, the clone on the box) and refuse to escape it; exec_command
    # additionally passes through the SAME policy.screen() guardrail the engine
    # enforces on a role's shell, so the console's Governance rules apply here too.
    import os as _os
    import subprocess as _subprocess

    def _ws_root() -> str:
        return _os.environ.get("WORKSHOP_REPO_ROOT") or _os.path.expanduser(
            "~/sample-amazon-bedrock-agentcore-coding-agents")

    def _resolve_in_ws(rel: str) -> str | None:
        """Absolute path for a workspace-relative path, or None if it escapes the
        workspace root (no reading /etc/passwd via ../../)."""
        root = _os.path.realpath(_ws_root())
        full = _os.path.realpath(_os.path.join(root, rel))
        if full == root or full.startswith(root + _os.sep):
            return full
        return None

    @tool
    def read_file(path: str) -> str:
        """Read a text file from the workspace (path relative to the repo root,
        e.g. 'orchestrator/engine.py'). Returns the file's text,
        capped at 60 KB. Use it to inspect the module or a harness file before
        dispatching. Refuses paths outside the workspace."""
        full = _resolve_in_ws(path)
        if not full:
            return json.dumps({"error": f"path escapes the workspace: {path}"})
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                return f.read(60_000)
        except OSError as exc:
            return json.dumps({"error": f"cannot read {path}: {exc}"})

    @tool
    def list_files(path: str = ".") -> str:
        """List the entries of a workspace directory (relative to the repo root).
        Returns each name with a trailing '/' for directories. Use it to explore
        the tree before reading a file. Refuses paths outside the workspace."""
        full = _resolve_in_ws(path)
        if not full or not _os.path.isdir(full):
            return json.dumps({"error": f"not a workspace directory: {path}"})
        try:
            names = sorted(
                n + ("/" if _os.path.isdir(_os.path.join(full, n)) else "")
                for n in _os.listdir(full) if not n.startswith("."))
            return json.dumps({"path": path, "entries": names[:400]})
        except OSError as exc:
            return json.dumps({"error": f"cannot list {path}: {exc}"})

    @tool
    def grep_workspace(pattern: str, path: str = ".") -> str:
        """Search the workspace for a regex/string (like ripgrep), under an
        optional relative subpath. Returns up to 100 'file:line: text' matches.
        Use it to locate a symbol or usage before dispatching. Read-only."""
        full = _resolve_in_ws(path)
        if not full:
            return json.dumps({"error": f"path escapes the workspace: {path}"})
        try:
            proc = _subprocess.run(
                ["grep", "-rIn", "--exclude-dir=.git", "--exclude-dir=node_modules",
                 "-e", pattern, full],
                capture_output=True, text=True, timeout=20)
        except (OSError, _subprocess.SubprocessError) as exc:
            return json.dumps({"error": f"grep failed: {exc}"})
        root = _os.path.realpath(_ws_root())
        lines = [ln.replace(root + _os.sep, "") for ln in proc.stdout.splitlines()[:100]]
        return json.dumps({"pattern": pattern, "matches": lines, "count": len(lines)})

    @tool
    def exec_command(command: str) -> str:
        """Run ONE shell command in the workspace and return its output (stdout,
        stderr, exit code), capped and with a 30s timeout. For quick inspection
        (python -c, ls, cat, jq, sed -n, running a check) - NOT for a build; use
        dispatch_*/run_build for real work. Screened by the same policy the
        Governance page enforces: a denied command (rm -rf /, a write under
        .git/, a force-push to main) returns the rule that blocked it and never
        runs."""
        verdict = _policy.screen("run_command", command)
        if not verdict.allowed:
            return json.dumps({"blocked": True, "rule_id": verdict.rule_id,
                               "tier": verdict.tier, "reason": verdict.reason})
        try:
            proc = _subprocess.run(
                ["/bin/bash", "-lc", command], cwd=_ws_root(),
                capture_output=True, text=True, timeout=30)
        except _subprocess.TimeoutExpired:
            return json.dumps({"error": "command timed out after 30s"})
        except (OSError, _subprocess.SubprocessError) as exc:
            return json.dumps({"error": f"command failed to start: {exc}"})
        out = (proc.stdout or "")[-12_000:]
        err = (proc.stderr or "")[-4_000:]
        return json.dumps({"exit": proc.returncode, "stdout": out, "stderr": err})

    # The dispatch tools are generated from the ROSTER and added ONLY for roles that
    # are actually WIRED, so the orchestrator's real tool list is (registry x
    # Settings), never a fixed count. An unwired role gets no dispatch tool (the
    # model cannot pick an agent that does not exist); wiring it in Settings adds its
    # tool on the next agent build.
    wired = _wired_roles()
    # Workspace inspection is always available (it reads the orchestrator's own
    # repo, no wired role needed), so the orchestrator can look before it leaps.
    tools = [list_presets, read_file, list_files, grep_workspace, exec_command]
    dispatchable = [r for r in _roles.roster() if r.id in wired]
    tools += [_make_dispatch(r) for r in dispatchable]
    # run_build is useful only when at least one role can be dispatched.
    if dispatchable:
        tools.append(run_build)
    tools.append(run_status)
    # Always available, even with nothing wired: it reads persisted history, so it
    # is the way back to a run whose session (or run id) was lost.
    tools.append(list_runs)
    # Interactive terminal control is added only when runtime_shell is importable
    # (the console hosts it); in the standalone agent bundle it is absent, so the
    # model never sees tools it cannot use.
    try:
        import runtime_shell  # noqa: F401, PLC0415
        tools += [agent_send, agent_read, agent_status]
    except Exception:
        pass
    return tools


# The orchestrator's own model id (the chatbot's brain, NOT a per-role model).
# Wirable via env; the console's message-bar picker overrides it per conversation
# by passing model_id into build_agent/stream_chat.
DEFAULT_MODEL_ID = os.environ.get(
    "ORCHESTRATOR_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# Human labels/hints for the orchestrator-brain models the picker offers. Only
# Claude tiers belong here: the orchestrator REASONS with Claude (the dispatched
# coding agents bring their own models). Labels are presentation; the ids are the
# real Bedrock ids resolved from llm.BEDROCK_MODEL_MAP at call time.
_MODEL_META: dict[str, dict[str, str]] = {
    "claude-opus-4-6": {"label": "Claude Opus 4.6",  "hint": "most capable"},
    "claude-sonnet-4-6": {"label": "Claude Sonnet 4.6", "hint": "fast, balanced; the default brain"},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5", "hint": "fastest"},
}


def available_models() -> dict[str, Any]:
    """The orchestrator's selectable models, resolved at runtime from the Bedrock
    catalog (``llm.BEDROCK_MODEL_MAP``), so the picker reflects the catalog rather
    than a hardcoded frontend list. Returns ``{"models": [{id,label,hint}],
    "default": id}`` where ``id`` is the full Bedrock model id the chat endpoint accepts."""
    import llm  # noqa: PLC0415 (lazy; offline UI render doesn't need boto3)
    models: list[dict[str, str]] = []
    for alias, meta in _MODEL_META.items():
        bedrock_id = llm.BEDROCK_MODEL_MAP.get(alias)
        if bedrock_id:
            models.append({"id": bedrock_id, "label": meta["label"], "hint": meta["hint"]})
    return {"models": models, "default": DEFAULT_MODEL_ID}


# How many opener chips the empty chat offers. Wirable, because it is presentation:
# the console renders whatever this returns and caps at the same number.
MAX_SUGGESTIONS = int(os.environ.get("WORKSHOP_MAX_SUGGESTIONS", "3"))


def suggestions() -> dict[str, list[str]]:
    """Opening prompts for the empty chat: the preset titles, from ONE source
    (presets.PRESETS), so the chips cannot drift from what the tools offer. They are
    starting points; the attendee can type anything instead."""
    items = [p["title"] for p in _presets.public_presets() if not p["read_only"]]
    return {"suggestions": items[:MAX_SUGGESTIONS]}


def _roster_section() -> str:
    """The "Your roster" block: one line per SERVED role, naming its dispatch tool,
    its role id, and what it does. Generated from the registry, and from the
    operator's per-role description (set in Settings) when there is one, so the
    prompt describes the team this deployment actually runs instead of a hardcoded
    trio the roster may have moved on from."""
    try:
        import runtime_config
        descs = runtime_config.describe_map()
    except Exception:
        descs = {}
    lines = [f"- {r.dispatch_tool} ({r.id}, {r.label}): {descs.get(r.id) or r.description}"
             for r in _roles.roster()]
    if not lines:
        return ""
    return ("\n\n## Your roster (the roles this deployment serves)\n"
            "Each line is a dispatch tool, the role id behind it, and what that role "
            "does. An operator-provided description is authoritative. Only these "
            "roles exist:\n" + "\n".join(lines))


def build_agent(model_id: str | None = None, messages: list | None = None):
    """Build the Strands orchestrator agent. ``model_id`` sets the orchestrator's
    OWN model (the chatbot's brain, the message-bar choice), ``messages`` seeds
    prior conversation turns for multi-turn memory.

    The system prompt is the static base plus the generated roster section, so the
    set of dispatch targets is described from the registry + Settings, not hardcoded."""
    from strands import Agent
    from strands.models import BedrockModel
    model = BedrockModel(model_id=model_id or DEFAULT_MODEL_ID)
    system_prompt = SYSTEM_PROMPT + _roster_section()
    kwargs: dict[str, Any] = {"model": model, "system_prompt": system_prompt,
                              "tools": build_tools()}
    if messages:
        kwargs["messages"] = messages
    return Agent(**kwargs)


def _extract_run(tool_name: str, result: Any) -> dict | None:
    """If ``tool_name`` is a dispatch/build tool, pull {run_id, kind} out of its
    JSON result. Reading the RESULT (not a side-channel) is thread-safe: strands
    may run the tool on any thread, but the event delivers the result to us."""
    if tool_name not in _dispatch_tool_names():
        return None
    # The tool result is a strands ToolResult; the text we returned is in its
    # content blocks. Find the first JSON object that carries a run_id.
    blocks = []
    if isinstance(result, dict):
        blocks = result.get("content") or []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else None
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("run_id"):
            return {"run_id": data["run_id"], "kind": data.get("kind", "build")}
    return None


def _tool_name_of(event: Any) -> str | None:
    """The tool name off a Before/AfterToolCallEvent, across strands shapes."""
    tu = getattr(event, "tool_use", None)
    if isinstance(tu, dict):
        return tu.get("name")
    return getattr(tu, "name", None)


_IMAGE_FORMATS = {"png": "png", "jpeg": "jpeg", "jpg": "jpeg", "gif": "gif", "webp": "webp"}


def _build_prompt(prompt: str, attachments: list[dict] | None):
    """Turn the typed text + attachments into what stream_async receives. With no
    attachments it is a plain string. With an image it is a LIST of Strands content
    blocks ([{text}, {image:{format,source:{bytes}}}]), the multimodal shape, so a
    pasted image reaches the model as decoded bytes, not base64 text."""
    import base64
    if not attachments:
        return prompt
    blocks: list[dict] = [{"text": prompt}] if prompt else []
    for att in attachments:
        data_url = att.get("data") or ""
        name = att.get("name") or "attachment"
        # data URL: data:image/png;base64,XXXX
        if data_url.startswith("data:image/") and ";base64," in data_url:
            header, b64 = data_url.split(";base64,", 1)
            mime = header[len("data:"):]           # image/png
            ext = mime.split("/", 1)[-1].lower()
            fmt = _IMAGE_FORMATS.get(ext)
            if fmt:
                try:
                    blocks.append({"image": {"format": fmt,
                                             "source": {"bytes": base64.b64decode(b64)}}})
                    continue
                except Exception:  # noqa: BLE001 (fall through to a text note)
                    pass
        # Non-image (or undecodable) attachment: inline its text so it is still seen.
        text = att.get("text") or ""
        blocks.append({"text": f"--- attached: {name} ---\n{text}"})
    return blocks or prompt


def stream_chat(prompt: str, *, model_id: str | None = None,
                messages: list | None = None,
                attachments: list[dict] | None = None) -> Iterator[dict]:
    """Drive one chat turn of the orchestrator agent and yield events AS THEY
    ARRIVE (token-by-token streaming), not collected-then-dumped:

      {"type": "text", "text": "..."}            (an assistant text delta)
      {"type": "reasoning", "text": "..."}        (a thinking/reasoning delta)
      {"type": "tool", "name", "status"}          (a tool call started/finished)
      {"type": "run_started", "run_id", "kind"}    (a dispatch/build tool fired)
      {"type": "done", "messages": [...]}          (turn finished; carries history)

    A plain conversational turn yields only ``text`` then ``done`` (NO
    ``run_started``), so the console shows a normal answer with no run panel.

    The strands agent loop is async and the console handler is a SYNC generator
    (it feeds an SSE response). We bridge them with a background thread that runs
    ``stream_async`` and pushes each event onto a queue the generator drains, so a
    delta reaches the browser the instant the model emits it.
    """
    import asyncio
    import contextvars
    import queue
    import threading
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent

    q: queue.Queue = queue.Queue()
    _DONE = object()
    agent = build_agent(model_id=model_id, messages=messages)
    # A plain string for a text-only turn; a list of content blocks (text + image)
    # when the user attached an image: the Strands multimodal prompt shape.
    agent_input = _build_prompt(prompt, attachments)

    # Tool lifecycle yields tool rows; a dispatch/build tool result yields run_started.
    def _before_tool(event: Any) -> None:
        name = _tool_name_of(event)
        if name:
            q.put({"type": "tool", "name": name, "status": "running"})

    def _after_tool(event: Any) -> None:
        name = _tool_name_of(event) or ""
        q.put({"type": "tool", "name": name, "status": "done"})
        hit = _extract_run(name, getattr(event, "result", None))
        if hit:
            q.put({"type": "run_started", **hit})

    agent.hooks.add_callback(BeforeToolCallEvent, _before_tool)
    agent.hooks.add_callback(AfterToolCallEvent, _after_tool)

    # The caller (connection_api / main.py) set the user's identity in a
    # ContextVar on THIS thread. The agent loop (and therefore every dispatch
    # tool, and ENGINE.submit inside it) runs on the worker thread below, and a
    # ContextVar does NOT cross a bare Thread. Snapshot the context here and run
    # the worker inside it, so the run is attributed to the signed-in user, not
    # the host account the process runs as.
    _caller_ctx = contextvars.copy_context()

    def _run() -> None:
        """Worker thread: drive the async stream, push events onto the queue."""
        async def _drive() -> None:
            async for event in agent.stream_async(agent_input):
                if not isinstance(event, dict):
                    continue
                # reasoning/thinking deltas (when the model emits them natively)
                rt = event.get("reasoningText") or event.get("reasoning_text")
                if rt:
                    q.put({"type": "reasoning", "text": str(rt)})
                # assistant text deltas: `data` is the human-readable token
                data = event.get("data")
                if isinstance(data, str) and data:
                    q.put({"type": "text", "text": data})
        try:
            asyncio.new_event_loop().run_until_complete(_drive())
        except Exception as exc:  # noqa: BLE001 (surface, never hang the stream)
            q.put({"type": "error", "error": str(exc)})
        finally:
            q.put(_DONE)

    threading.Thread(target=lambda: _caller_ctx.run(_run), daemon=True).start()

    # Keepalive: the model can think for well over 30s without emitting a single
    # delta, and an SSE response that sends NO bytes for that long is cut by the
    # transport chain (CloudFront's default origin read timeout is 30s; nginx
    # read-timeouts too). The PTY and runtime-shell streams already ping; this
    # stream must too. A typed event (not an SSE comment) so it survives the
    # JSON encode in console/server.py; every consumer ignores unknown types.
    keepalive_s = float(os.environ.get("WORKSHOP_CHAT_KEEPALIVE_S", "15"))
    while True:
        try:
            ev = q.get(timeout=keepalive_s)
        except queue.Empty:
            yield {"type": "keepalive"}
            continue
        if ev is _DONE:
            break
        yield ev
    yield {"type": "done", "messages": list(getattr(agent, "messages", []) or [])}
