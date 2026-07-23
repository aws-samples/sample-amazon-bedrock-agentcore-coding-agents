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
import router as _router          # the deterministic route ladder (advisory here)

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

# Roles the dispatch_* tools target, by agent id. The validator is a second
# Claude Code (steered by the acceptance contract) since Kiro was retired.
_BACKEND, _FRONTEND, _VALIDATOR = "claude-code", "opencode", "claude-code-validator"

SYSTEM_PROMPT = """\
You are the orchestrator for a multi-agent coding harness, a chatbot the user \
talks to. Hold a normal conversation: answer questions, explain what you can do, \
and only build when the user actually asks you to.

## Your agents (deployed on their own AgentCore Runtimes, called AS TOOLS)
- dispatch_backend, Claude Code: the backend MCP server (wraps the module).
- dispatch_frontend, opencode: the chatbot UI on top of that server.
- dispatch_validator, Claude Code (validator): runs the acceptance gate that \
defines "done".
Each type is a FLEET, not one agent; you dispatch to a TYPE and the runtime picks \
an instance. You never address one instance.

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

## How to act once the ask is clear
- Focused single-role job (rebuild the UI, patch the backend): call the matching \
dispatch_* tool. It returns a run id immediately and the build runs in the \
background. State that it started and which agent owns it.
- Full build that must be composed and graded: call run_build(task). It dispatches \
the routed roles, composes their work, and runs the validator-authored acceptance gate plus a \
separate review pass. Pass the user's request text VERBATIM as task: the router \
keys on the user's own wording, and a paraphrase can flip the route to the wrong \
workflow or the wrong sample use case.
Call route_task(task) first if unsure which agents a task needs; it is advisory.

## If a build fails or reaches needs_human, RESUBMIT the same build, do not improvise
When a run reaches `failed` or `needs_human` before opening a PR (for example a
transient `ROLE_TOTAL_FAILURE`, where a role's turn produced no artifact), the
correct recovery is to call run_build again with the SAME task text. Do NOT try to
"finish it yourself" by dispatching individual roles, hand-composing files, or
running review/pr-v1: those paths do not compose the deliverable or open the PR the
way run_build does, and a review workflow with no PR to review just fails
`NO_RUN_TO_REVIEW`. One clean resubmit is the whole recovery. Tell the user the run
did not complete and that you are resubmitting the same build.

## Drive a live terminal directly (when the agent's terminal is open)
When the user is watching an agent's interactive terminal and wants you to drive it \
turn by turn, use agent_send(agent_id, message) to type into that same terminal \
(the user sees your message as an "[orchestrator]" line), agent_read(agent_id) to \
see what the agent printed, and agent_status(agent_id) to check a terminal is open. \
agent_id is 'claude-code', 'opencode', or 'claude-code-validator'. This talks to the \
SAME live session the user is watching, so keep turns purposeful; it is for interactive \
guidance, not for kicking a background build (use dispatch_*/run_build for that).

## Voice
Write like a senior engineer: precise, terse, technical. No emoji, no exclamation \
marks, no filler. Report what happened (which agents ran, the run id, the gate \
result) in plain declarative sentences. Never claim a build passed unless a tool \
reported it, and never fabricate a result or a PR URL.
"""


# The dispatch/build tools (by name): the ones whose firing means "a run started"
# and should reveal the run panel in the UI. route_task/run_status start nothing.
_DISPATCH_TOOLS = {"dispatch_backend", "dispatch_frontend", "dispatch_validator", "run_build"}


def _wired_roles() -> set[str]:
    """The set of roles with a wired runtime ARN (from runtime_config). The
    dispatch tools are generated from this, so the orchestrator only offers
    agents that actually exist. Empty set if nothing is wired (or on any error),
    which yields a converse-only agent (route_task + run_status), never a tool
    that would fail loud the moment the model called it."""
    try:
        import runtime_config
        return {r["role"] for r in runtime_config.status()["roles"]
                if r.get("wired") and r["role"] != "orchestrator"}
    except Exception:
        return set()


# The workflow each explicit dispatch_* tool submits under. An explicit agent
# choice must never die on NO_ROUTE: the orchestrator model rewrites the task
# text in its own words, which need not contain a router keyword, and admission
# still routes that text for the usecase. Pinning the single-role workflow here
# keeps "explicit agent selection (router consulted for usecase only)" true.
_ROLE_WORKFLOW = {
    _BACKEND: "patch/backend-v1",
    _FRONTEND: "patch/frontend-v1",
    _VALIDATOR: "review/pr-v1",
}


def _kick(agent_id: str | None, task: str) -> str:
    """Submit a run (single-role when agent_id is set, else routed) WITHOUT
    blocking, and return its id immediately. The chat keeps streaming; the
    console polls the run for live status. The 'a run started' UI signal is NOT
    raised here; it is read off the tool RESULT by an AfterToolCallEvent hook,
    so it works regardless of which thread strands runs the tool on."""
    run = ENGINE.submit(task, agents=[agent_id] if agent_id else None,
                        workflow_ref=_ROLE_WORKFLOW.get(agent_id) if agent_id else None)
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
    def route_task(task: str) -> str:
        """Advisory: classify a task against the workflow registry without running
        anything. Returns the routed workflow_ref, the agents it would dispatch,
        and why. Use it to decide which dispatch_* tool fits; it starts nothing."""
        try:
            return json.dumps(_router.route(task).public())
        except _router.RouteError as exc:
            return json.dumps({"error": str(exc)})

    @tool
    def dispatch_backend(task: str) -> str:
        """Start the BACKEND builder (Claude Code) on its deployed Runtime, backend
        only. Returns immediately with a run id; the build runs in the background."""
        return json.dumps({"run_id": _kick(_BACKEND, task),
                           "agent": _BACKEND, "kind": "backend", "status": "started"})

    @tool
    def dispatch_frontend(task: str) -> str:
        """Start the FRONTEND builder (opencode) on its deployed Runtime, the chatbot
        UI only. Returns immediately with a run id; the build runs in background."""
        return json.dumps({"run_id": _kick(_FRONTEND, task),
                           "agent": _FRONTEND, "kind": "frontend", "status": "started"})

    @tool
    def dispatch_validator(task: str) -> str:
        """Start the VALIDATOR (Claude Code) on its deployed Runtime, the acceptance
        gate only. Returns immediately with a run id; the build runs in the background."""
        return json.dumps({"run_id": _kick(_VALIDATOR, task),
                           "agent": _VALIDATOR, "kind": "validator", "status": "started"})

    @tool
    def run_build(task: str) -> str:
        """Start a FULL build: the router picks the roles, their work composes into
        one deliverable, the validator-authored acceptance test gates it, and the reviewer posts its assessment on the PR.
        Returns immediately with a run id; the build runs in the background and the
        console shows its live status. Pass the user's request text VERBATIM as
        task: the router keys on the user's own wording, and a paraphrase can
        change which workflow (or which sample use case) gets built."""
        # Pre-route so a mis-phrased task is refused HERE, as a retryable tool
        # error the model can correct, instead of minting a permanently failed
        # run (a dead NO_RUN_TO_REVIEW/NO_ROUTE run is the attendee's first
        # visible result). The router is pure, so the engine's own routing of
        # an admitted task reaches the same verdict.
        try:
            route = _router.route(task)
        except _router.RouteError as exc:
            return json.dumps({
                "error": str(exc),
                "hint": "No run was started. Retry run_build with the user's "
                        "request text verbatim (do not paraphrase it); their "
                        "wording names the target.",
            })
        if route.read_only:
            return json.dumps({
                "error": f"REVIEW_NOT_A_BUILD:{route.workflow_ref}",
                "hint": "No run was started. This wording routes to the "
                        "read-only review workflow, which builds nothing. For "
                        "a build, retry run_build with the user's request text "
                        "verbatim; to review an existing run, use "
                        "dispatch_validator.",
            })
        return json.dumps({"run_id": _kick(None, task), "kind": "build", "status": "started"})

    @tool
    def run_status(run_id: str) -> str:
        """Read back the current verdict for a run id a dispatch_*/run_build tool
        returned: status, gate result, review state, and the PR URL if one opened."""
        run = ENGINE.get(run_id)
        if run is None:
            return json.dumps({"error": f"UNKNOWN_RUN:{run_id}"})
        return json.dumps(_engine.public_result(run))

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
    # so it can answer "what does cost_analyzer expose?" or check a file BEFORE
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
        e.g. 'usecase-sample-to-mcp/cost_analyzer.py'). Returns the file's text,
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

    # The dispatch tools are added ONLY for roles that are actually WIRED, so the
    # orchestrator's real tool list is generated from Settings, not a fixed 3. An
    # unwired role gets no dispatch tool (the model can't pick an agent that does
    # not exist); wiring it in Settings adds its tool on the next agent build.
    dispatch_by_role = {
        _BACKEND: dispatch_backend,
        _FRONTEND: dispatch_frontend,
        _VALIDATOR: dispatch_validator,
    }
    wired = _wired_roles()
    # Workspace inspection is always available (it reads the orchestrator's own
    # repo, no wired role needed), so the orchestrator can look before it leaps.
    tools = [route_task, read_file, list_files, grep_workspace, exec_command]
    for role, fn in dispatch_by_role.items():
        if role in wired:
            tools.append(fn)
    # run_build is useful only when at least one role can be dispatched.
    if any(role in wired for role in dispatch_by_role):
        tools.append(run_build)
    tools.append(run_status)
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


def suggestions() -> dict[str, Any]:
    """Opening prompts for the empty chat, derived from the workflow registry
    (router.WORKFLOWS), so the chips reflect what the orchestrator can do. Kept
    SHORT and actionable (the chips must not clip): a concise imperative per
    workflow, capped at 3. The full registry descriptions are too long for a chip,
    so each known workflow maps to a brief opener; unknown ones fall back to a
    trimmed description."""
    _SHORT = {
        "convert/sample-to-mcp-v1": "Convert the cost analyzer module to MCP + UI",
        "build/fullstack-v1": "Build the Critter Lab full-stack app",
        "patch/backend-v1": "Patch the backend MCP server",
        "patch/frontend-v1": "Rebuild the chatbot UI",
        "review/pr-v1": "Review an existing run branch",
    }
    items: list[str] = []
    seen: set[str] = set()
    for wf in _router.public_workflows():
        ref = wf.get("workflow_ref", "")
        if ref in seen:
            continue
        seen.add(ref)
        opener = _SHORT.get(ref)
        if not opener:
            desc = (wf.get("description") or "").strip().rstrip(".")
            opener = (desc[0].upper() + desc[1:]) if desc else ref
            if len(opener) > 48:
                opener = opener[:46].rstrip() + "..."
        items.append(opener)
    return {"suggestions": items[:3]}


# Which dispatch tool owns each wired role, for the dynamic agent-description block.
_ROLE_TO_TOOL = {
    "claude-code": "dispatch_backend",
    "opencode": "dispatch_frontend",
    "claude-code-validator": "dispatch_validator",
}


def _dynamic_agent_section() -> str:
    """Build a system-prompt section from the WIRED role descriptions (set in
    Settings), so the orchestrator describes its dispatch targets dynamically.
    Empty string when nothing is described, leaving the static roster as-is."""
    try:
        import runtime_config
        descs = runtime_config.describe_map()
    except Exception:
        descs = {}
    lines = []
    for role, tool in _ROLE_TO_TOOL.items():
        d = descs.get(role)
        if d:
            lines.append(f"- {tool} ({role}): {d}")
    if not lines:
        return ""
    return ("\n\n## Wired agent descriptions (operator-provided, authoritative)\n"
            "These describe what each currently-wired agent does. Prefer them when "
            "deciding which agent a task needs:\n" + "\n".join(lines))


def build_agent(model_id: str | None = None, messages: list | None = None):
    """Build the Strands orchestrator agent. ``model_id`` sets the orchestrator's
    OWN model (the chatbot's brain, the message-bar choice), ``messages`` seeds
    prior conversation turns for multi-turn memory.

    The system prompt is the static base plus any WIRED role descriptions, so the
    set of dispatch targets is described dynamically from Settings, not hardcoded."""
    from strands import Agent
    from strands.models import BedrockModel
    model = BedrockModel(model_id=model_id or DEFAULT_MODEL_ID)
    system_prompt = SYSTEM_PROMPT + _dynamic_agent_section()
    kwargs: dict[str, Any] = {"model": model, "system_prompt": system_prompt,
                              "tools": build_tools()}
    if messages:
        kwargs["messages"] = messages
    return Agent(**kwargs)


def _extract_run(tool_name: str, result: Any) -> dict | None:
    """If ``tool_name`` is a dispatch/build tool, pull {run_id, kind} out of its
    JSON result. Reading the RESULT (not a side-channel) is thread-safe: strands
    may run the tool on any thread, but the event delivers the result to us."""
    if tool_name not in _DISPATCH_TOOLS:
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
