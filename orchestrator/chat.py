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
import re
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import threading
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

# Request text is admitted by the host, never reconstructed from a tool argument.
# Limits reject complete values; they must never hide a clipped request/context.
MAX_USER_REQUEST_BYTES = 64 * 1024
MAX_USER_CONTEXT_BYTES = 64 * 1024
MAX_USER_CONTEXT_TURNS = 16
MAX_REQUEST_PROVENANCE_BYTES = 160 * 1024


class UserRequestError(ValueError):
    pass


@dataclass(frozen=True)
class UserRequest:
    current: str
    prior: tuple[tuple[int, str], ...] = ()
    closed: threading.Event = field(default_factory=threading.Event, compare=False)
    admission_lock: Any = field(default_factory=threading.RLock, compare=False)

    def require_active(self) -> None:
        if self.closed.is_set():
            raise UserRequestError("USER_REQUEST_CLOSED")

    def require_open(self) -> None:
        self.require_active()
        if not isinstance(self.current, str) or not self.current.strip():
            raise UserRequestError("EMPTY_USER_REQUEST")
        if len(self.current.encode("utf-8")) > MAX_USER_REQUEST_BYTES:
            raise UserRequestError("USER_REQUEST_TOO_LARGE")

    def close(self) -> None:
        # A tool already inside submit is admitted; a later call cannot start work
        # after a disconnect, even if its synchronous model/routing call returns late.
        with self.admission_lock:
            self.closed.set()


_USER_REQUEST: ContextVar[UserRequest | None] = ContextVar("chat_user_request", default=None)


def _prior_user_turns(messages: list | None) -> tuple[tuple[int, str], ...]:
    """Read actual user text, stopping the context at a successful dispatch result.

    Strands represents tool results as user messages too. They are not human input.
    Match their real tool-use IDs before treating one as a dispatch boundary.
    """
    turns: list[tuple[int, str]] = []
    dispatch_ids: set[str] = set()
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        blocks = message.get("content") or []
        if not isinstance(blocks, list):
            continue
        if message.get("role") == "assistant":
            for block in blocks:
                use = block.get("toolUse") if isinstance(block, dict) else None
                if (isinstance(use, dict) and use.get("name") in _dispatch_tool_names()
                        and isinstance(use.get("toolUseId"), str) and use["toolUseId"]):
                    dispatch_ids.add(use["toolUseId"])
            continue
        if message.get("role") != "user":
            continue
        results = [block["toolResult"] for block in blocks
                   if isinstance(block, dict) and "toolResult" in block]
        if results:
            for result in results:
                if not isinstance(result, dict) or result.get("toolUseId") not in dispatch_ids:
                    continue
                if result.get("status") == "error":
                    continue
                for block in result.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    try:
                        data = block.get("json") or json.loads(block.get("text", ""))
                    except (TypeError, ValueError):
                        continue
                    if (isinstance(data, dict) and data.get("status") == "started"
                            and isinstance(data.get("run_id"), str) and data["run_id"]):
                        turns.clear()
            continue
        # _build_prompt puts the actual prompt first, followed by attachments.
        # Attachment contents are reference material, not additional user turns.
        if blocks and isinstance(blocks[0], dict):
            text = blocks[0].get("text")
            if isinstance(text, str) and text.strip():
                turns.append((index, text))
    return tuple(turns)


@contextmanager
def _bind_request(request: UserRequest):
    token = _USER_REQUEST.set(request)
    try:
        yield request
    finally:
        _USER_REQUEST.reset(token)


@contextmanager
def bind_user_request(prompt: str, messages: list | None = None):
    """Bind server-owned input for a direct invocation of the decorated tools."""
    request = UserRequest(prompt, _prior_user_turns(messages))
    try:
        with _bind_request(request):
            yield request
    finally:
        request.close()


def _current_user_request() -> UserRequest:
    request = _USER_REQUEST.get()
    if request is None:
        raise UserRequestError("USER_REQUEST_NOT_BOUND")
    request.require_open()
    return request


def _user_context(request: UserRequest, turn_ids: list[int] | None) -> list[dict]:
    if not isinstance(turn_ids, (list, type(None))):
        raise UserRequestError("INVALID_USER_CONTEXT")
    ids = turn_ids or []
    if len(ids) > MAX_USER_CONTEXT_TURNS:
        raise UserRequestError("USER_CONTEXT_TOO_LARGE")
    if any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
        raise UserRequestError("INVALID_USER_CONTEXT")
    available = dict(request.prior)
    if any(i not in available for i in ids):
        raise UserRequestError("USER_CONTEXT_UNAVAILABLE")
    # Chronology is server-owned too; tool argument order cannot reverse precedence.
    selected = [{"turn": i, "text": text} for i, text in request.prior if i in ids]
    if sum(len(item["text"].encode("utf-8")) for item in selected) > MAX_USER_CONTEXT_BYTES:
        raise UserRequestError("USER_CONTEXT_TOO_LARGE")
    return selected


def _explicit_preset(text: str, preset: str, canonical: str) -> bool:
    """Recognize literal registry selectors, not a classifier for arbitrary work."""
    markers = (preset, f"preset={preset}", _presets.PRESETS[preset]["title"], canonical)
    value = text.strip()
    if any(value == marker or value.startswith(marker + "\n")
           or value.startswith(marker + "\r\n") for marker in markers if marker):
        return True
    # The workshop's CLI command is "Use preset=<id>. Creative direction: ...".
    # Recognize that explicit command at the start, not a mention inside a
    # question, negation, or a longer identifier.
    return bool(re.match(
        re.escape(f"use preset={preset}") + r"(?:\.(?=\s|$)|(?=\s|$))",
        value, flags=re.IGNORECASE,
    ))


def _admit_user_task(model_task: str, *, preset: str = "", creative_direction: str = "",
                     context_turns: list[int] | None = None) -> tuple[str, dict]:
    request = _current_user_request()
    context = _user_context(request, context_turns)
    task = request.current
    if creative_direction.strip() and (not preset or model_task.strip()):
        raise UserRequestError("AMBIGUOUS_PRESET_DIRECTION")
    if preset:
        try:
            canonical = _presets.default_task(preset)
        except _presets.RouteError as exc:
            raise UserRequestError(str(exc)) from exc
        user_text = [request.current, *(turn["text"] for turn in context)]
        current_selections = {
            key for key, value in _presets.PRESETS.items()
            if _explicit_preset(request.current, key, value["task"])
        }
        if current_selections and preset not in current_selections:
            raise UserRequestError("PRESET_NOT_REQUESTED")
        if not any(_explicit_preset(text, preset, canonical) for text in user_text):
            raise UserRequestError("PRESET_NOT_REQUESTED")
        if creative_direction and not any(creative_direction in text for text in user_text):
            raise UserRequestError("CREATIVE_DIRECTION_NOT_FROM_USER")
        task = canonical
        selectors = (preset, f"preset={preset}", _presets.PRESETS[preset]["title"], canonical)
        if request.current.strip() not in selectors:
            # Retain the entire user text. A model-selected substring of a creative
            # direction must not silently discard the user's other constraints.
            task += "\n\nCurrent participant request (verbatim; latest user text wins):\n" + request.current
        if not task.strip():
            raise UserRequestError("EMPTY_USER_REQUEST")
    if context:
        task += (
            "\n\nEarlier USER context, for clarification/reference only. These are not "
            "additional requirements of a new task. The current user request above "
            "takes precedence; do not carry an earlier question's restrictions into "
            "a later request.\n"
            + json.dumps(context, ensure_ascii=False, indent=2)
        )
    provenance = {
        "source": "server_user_turn",
        "current_user_text": request.current,
        "current_user_sha256": hashlib.sha256(request.current.encode("utf-8")).hexdigest(),
        "prior_user_context": context,
        "available_prior_user_turns": len(request.prior),
        "effective_task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "model_task_ignored": bool(model_task) and model_task != request.current,
        "preset": preset or None,
        "creative_direction_verified": bool(creative_direction),
    }
    if len(json.dumps(provenance, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_PROVENANCE_BYTES:
        raise UserRequestError("USER_REQUEST_PROVENANCE_TOO_LARGE")
    return task, provenance


def _request_error(exc: UserRequestError) -> str:
    return json.dumps({
        "error": str(exc),
        "hint": "No run was started. Use the current user's own words; get_user_request "
                "lists available user context. Missing or oversized input must be "
                "supplied explicitly, never reconstructed from an assistant response.",
    })


async def stream_user_turn(agent: Any, prompt: str, *, messages: list | None = None,
                           agent_input: Any = None, request: UserRequest | None = None):
    """Own one request scope without leaking its ContextVar across a yielded event."""
    request = request or UserRequest(prompt, _prior_user_turns(messages))
    stream = None
    try:
        with _bind_request(request):
            request.require_active()
            stream = agent.stream_async(prompt if agent_input is None else agent_input)
        while True:
            try:
                with _bind_request(request):
                    event = await anext(stream)
            except StopAsyncIteration:
                return
            yield event
    finally:
        request.close()
        if stream is not None and hasattr(stream, "aclose"):
            with _bind_request(request):
                await stream.aclose()


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
AgentCore Runtime. Builder tools start work; the engine schedules the independent \
checker for every resulting pull request. Each type is a FLEET, not one agent; you \
dispatch to a TYPE and the runtime picks an instance. You never address one \
instance, and you never assume a role that is not in your tool list exists.
Verification is a handoff with separate outputs: the builder produces candidate \
source only. The coordinator engine publishes its pull request through the GitHub \
App/Gateway; builders and their Runtimes never receive GitHub credentials. \
The checker produces the source of an executable check; \
the engine runs that executable against the candidate and records its real exit \
code; a separate reviewer produces an assessment of the pull request and its gate \
evidence. When explaining what the checker produces, explicitly name the engine \
as the component that executes its check and records the result. A checker's prose \
approval is not a gate result.

## Converse first: do not dispatch on a greeting or a question
If the user greets you, asks what you do, or asks a question, reply in words. Do \
not call any tool. A dispatch tool spins up a real microVM; never call one to be \
eager.

## Inspect the selected project before you dispatch
The project is the GitHub repository selected in Settings. The workshop host's \
console and orchestrator source are the platform running this team, not the \
application the user wants changed. Never infer the project's framework, routes, \
or filenames from those platform files.
You can inspect the selected repository's default branch to ground a decision \
WITHOUT dispatching: read_file(path) reads a file, list_files(path) lists a \
directory, grep_workspace(pattern) searches, and exec_command(command) runs one \
bounded shell command (screened by the governance policy). Use them to answer \
"what does this module expose?", to confirm a file exists, or to check a detail \
before deciding which agent to dispatch. Results identify the repository and \
source commit. If inspection fails, report the failure or ask for missing setup; \
never fill the gap from the workshop checkout. Preserve the existing application's \
framework and entry point when the user asks for a change to it.

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
- Focused single-builder job (rebuild the UI, patch the backend): call the matching \
dispatch_* tool ONCE. It starts a complete checked and reviewed build for that \
builder, including the independent checker automatically. Never dispatch the \
checker separately. It returns a run id and the same authoritative agents and \
schedule fields as run_build. Pass the user's request text VERBATIM as task, just \
as with run_build; do not replace it with a file manifest or a new architecture. \
State which builder started and which checker waits.
- Full build that must be checked and reviewed: call run_build(task). Every builder \
gets an isolated work id and its OWN pull request against the default branch. The \
checker then authors one executable per pull request, and each pull request is gated, \
reviewed, and merged on its own. Pass the user's request text VERBATIM as task. \
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
When a message names a preset AND supplies a creative direction, call \
`run_build(task="", preset="<id>", creative_direction="<their exact direction>")`. \
The tool retains the preset's shared interface and appends that direction. \
Do not rewrite it into a file list, choose a game for the builder, or discard the \
preset's shared interface. A plain custom request still goes in task VERBATIM.

## User-request boundary
The host binds the actual current user text to each turn. Custom build tools use \
that text even if you supply a different task argument. For a clear new request, \
leave context_turns empty. If this turn clarifies or confirms an earlier request, \
call get_user_request and reference the relevant prior USER turn IDs with \
context_turns. Only those actual user messages can accompany the current request \
as labeled context; assistant plans, guesses, and tool results are never task \
requirements. A previous question's restrictions do not constrain a new request. \
After a successful dispatch, its earlier user turns are no longer available as \
context for a new build. Presets must be explicitly named by the user through a \
preset ID, title, or canonical request; never select an unrelated preset to replace \
a custom request. Keep creative_direction exactly as the user supplied it. \
Missing or oversized context is an error to report or clarify, not permission to \
invent the missing text.

After `run_build`, treat the tool result's `schedule` as authoritative. Report only \
the roles in its `agents` list. Say that each selected builder started, then say the \
selected checker is WAITING for the builders and will then author an executable \
check per pull request. The engine runs the executable and records its exit code. \
Never group builders and checkers together as "agents are working", \
infer a role count from the roster, or claim that every role works in parallel.

## Reading back a run you did not start
run_status(run_id) answers for runs from EARLIER sessions too: the engine persists \
every verdict, so an expired session no longer loses the result. If the user has \
lost their run id, call list_runs() for the recent builds and their outcomes rather \
than telling them the run is gone.

## Report status facts without inventing lifecycle rules
Treat every field returned by run_status as authoritative. Builder pull requests are \
published BEFORE the checker inspects them, and `role_prs` carries each one's own \
check, review, and merge state. If a builder's `work_items.*.pr.pr_url` is empty \
during a live transition, say only that it is not reported yet. Never claim that a \
gate must pass before a pull request opens, and never infer a missing field's cause \
or timing. Report `gate_history`, the integrated review evidence, \
`role_prs`, `next_action`, and `resubmission_allowed` exactly as returned.

## If a build did not complete, READ next_action. Never improvise, and never loop
Every terminal result carries a `next_action` field. It is derived from the actual
fail reason, so it already knows which of the two very different `needs_human` cases
you are in. FOLLOW IT rather than deciding for yourself.
`resubmission_allowed` is a hard constraint on an IMMEDIATE retry: when it is
false, do not offer repeating the request now or a "clean retry." Preserve any
external prerequisite in `next_action` exactly (for example, wait for quota to
reset first).

Do NOT try to "finish it yourself" by dispatching individual roles or hand-composing
files. A focused dispatch starts another build with its own budget; it is not a
repair of the existing pull request. The engine already schedules that pull
request's checker. Follow the recorded next_action instead of starting another loop.

The three cases, because they have different recoveries:

* A role produced nothing (`ROLE_EXECUTION_ERROR`, `ROLE_TOTAL_FAILURE`,
  `ARTIFACT_TRANSFER_ERROR`), or the coordinator Runtime was recycled mid-build
  (`COORDINATOR_SESSION_INTERRUPTED`). Nothing was judged, so the work is unproven
  rather than rejected. Call run_build ONCE more with the SAME task text, and say
  you are resubmitting.
* The account reached its daily model allowance (`MODEL_QUOTA_EXHAUSTED`). A fresh
  shell or another immediate build cannot restore that allowance. Report the limit
  and stop. Resume after it resets, or after the operator selects a model with
  available capacity.
* Validation stayed blocked on real work (`ITERATION_CAP`). This can be a RED
  validator-authored executable OR a finding under either required lens of the
  integrated review, even when the executable is green. The bounded re-implement
  round is already spent.
  Resubmitting here is not recovery, it is the unbounded loop the cap exists to
  prevent. REPORT it instead. If the latest gate is red, quote its `gate.summary`;
  if a review blocks, quote the recorded lens evidence. Never call a green gate
  red. It is the human's call whether to change the request, change the deliverable,
  or accept the finding.

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
    in the UI: one per served builder, plus run_build. Derived from the roster, so a
    roster change cannot leave a dispatch tool unrecognized here (which would have
    silently stopped the UI from ever showing that role's run).
    list_presets/run_status start nothing and are deliberately absent."""
    return {r.dispatch_tool for r in _roles.roster()
            if r.kind == _roles.BUILDER} | {"run_build"}


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


def _schedule(agents: list[str]) -> list[dict[str, str]]:
    return [
        {
            "agent": agent_id,
            "kind": _roles.BY_ID[agent_id].kind,
            "timing": (
                "after every selected builder finishes"
                if _roles.BY_ID[agent_id].kind == _roles.CHECKER
                else "starts immediately"
            ),
        }
        for agent_id in agents
    ]


def _kick(agent_id: str | None, task: str, preset: str | None = None,
          *, request_provenance: dict | None = None,
          resolved_agents: list[str] | None = None) -> str:
    """Submit a run (focused on one builder when agent_id is set, else routed)
    WITHOUT blocking, and return its id immediately. The chat keeps streaming; the
    console polls the run for live status. The 'a run started' UI signal is NOT
    raised here; it is read off the tool RESULT by an AfterToolCallEvent hook,
    so it works regardless of which thread strands runs the tool on.

    The CHECKER always rides along with a builder. Validation is agentic only, so a
    builder dispatched alone would produce work with no authored acceptance check,
    and with no check the gate is red by design. Focusing a run means choosing which
    BUILDER works, never dropping the verification."""
    request = _current_user_request()
    agents = resolved_agents
    checkers = list(_roles.checker_ids())
    if agent_id:
        # A focused run: the named role, plus the checker unless the named role IS
        # the checker. Expressed in kinds, so it holds for any roster.
        agents = ([agent_id] if agent_id in checkers
                  else [agent_id] + checkers)
    elif agents is None and not preset:
        # A full build with no roles named: ASK which capabilities the request needs.
        # "every served role works" was the old answer, and it is wrong in the same
        # way a keyword table is: it dispatches a frontend builder for a command line
        # tool. Routing fails OPEN to every maker when the model is unreachable, so an
        # outage never silently drops work the attendee asked for.
        agents = _presets.resolve(task=task).agents
    with request.admission_lock:
        request.require_open()
        run = ENGINE.submit(task, agents=agents, preset=preset,
                            options={"chat_request": request_provenance or {}})
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
    def get_user_request() -> str:
        """Read the server-bound current request and retained prior USER turn IDs.

        Starts nothing. For a clear new task leave context_turns empty in dispatch.
        For a clarification/confirmation, select only relevant IDs returned here.
        Assistant guesses and tool results are never available as user context.
        """
        try:
            request = _current_user_request()
            prior = _user_context(request, [turn for turn, _text in request.prior])
            return json.dumps({"current_user_text": request.current, "prior_user_turns": prior,
                               "context_scope": "Retained user turns after the previous successful dispatch."})
        except UserRequestError as exc:
            return _request_error(exc)

    @tool
    def list_presets() -> str:
        """The starting points an attendee can begin from: id, title, the roles each
        uses, and its request text. They are EXAMPLES, not a menu of what is
        supported: any request at all can be built with run_build. Starts nothing."""
        return json.dumps({"presets": _presets.public_presets()})

    def _make_dispatch(role: _roles.Role):
        """Build one builder's focused-build tool from its registry entry.

        Generated rather than hand-written so the tool list follows the roster:
        adding, hiding, or swapping a role changes which tools exist with no edit
        here. Checkers are scheduled by the engine, not dispatched independently.
        """
        def dispatch(task: str, context_turns: list[int] | None = None) -> str:
            try:
                task, provenance = _admit_user_task(task, context_turns=context_turns)
                run_id = _kick(role.id, task, request_provenance=provenance)
            except UserRequestError as exc:
                return _request_error(exc)
            agents = list(ENGINE.get(run_id).agents)
            return json.dumps({"run_id": run_id, "agent": role.id,
                               "kind": role.capability, "status": "started",
                               "agents": agents, "schedule": _schedule(agents),
                               "request_source": "server_user_turn"})
        dispatch.__name__ = role.dispatch_tool
        dispatch.__qualname__ = role.dispatch_tool
        # The docstring IS the tool description the model reads, so it carries this
        # role's real job from the registry.
        dispatch.__doc__ = (
            f"Start a focused build with the {role.capability.upper()} builder "
            f"({role.label}) on its deployed Runtime. {role.description} "
            "The independent checker is included automatically and waits for this "
            "builder's pull request; do not dispatch it separately. Returns the run "
            "id, selected agents, and schedule immediately. Pass the user's request "
            "text VERBATIM as task; the host uses its own current-user binding. "
            "For a clear new task, omit context_turns. For clarification only, use "
            "prior USER turn IDs from get_user_request. Never use a file manifest "
            "or assistant guesses in place of the request.")
        return tool(dispatch)

    @tool
    def run_build(task: str, preset: str = "", creative_direction: str = "",
                  context_turns: list[int] | None = None) -> str:
        """Start a FULL build of ANY request. Every selected builder gets an
        isolated pull request against the default branch. The checker authors an
        executable check; the engine runs it and records its exit code. Each pull
        request is reviewed and merged on its own. Returns immediately with a run
        id; the build runs in the background.

        Pass the user's request text VERBATIM as task. It can be anything at all:
        nothing here classifies it or maps it to a sample, so there is no wording to
        get right. Optionally pass a `preset` id (see list_presets) to start from one
        of the example requests instead. To personalize a preset, leave task empty
        and pass the user's exact creative_direction; the preset's interface stays
        intact while the builder chooses the implementation. The host uses its
        actual current-user binding, not a model-authored replacement. For a clear
        new task omit context_turns. Only for clarification/confirmation, reference
        prior USER turn IDs returned by get_user_request."""
        try:
            task, provenance = _admit_user_task(
                task, preset=preset, creative_direction=creative_direction,
                context_turns=context_turns)
        except UserRequestError as exc:
            return _request_error(exc)
        # Routing is the MODEL's decision when the attendee typed their own request:
        # `resolve` asks which capabilities the request needs. It used to hand back
        # the whole roster here, which dispatched a frontend builder for a command
        # line tool. A preset carries its own declared capabilities, so it skips the
        # model turn.
        route = _presets.resolve(task=task, preset=preset or None)
        try:
            run_id = _kick(None, task, preset=preset or None, request_provenance=provenance,
                           # Keep preset admission (including read-only) intact.
                           # Custom routes, including "your-own", retain their
                           # single model-selected roster.
                           resolved_agents=(list(route.agents)
                                            if route.preset == "routed" else None))
        except UserRequestError as exc:
            return _request_error(exc)
        # Preset admission happens asynchronously. Before it fills the run,
        # report that preset's declared roster, not an empty schedule.
        agents = list(ENGINE.get(run_id).agents) or list(route.agents)
        return json.dumps({
            "run_id": run_id,
            "kind": "build",
            "status": "started",
            "agents": agents,
            "routing": route.rule,
            "schedule": _schedule(agents),
            "request_source": "server_user_turn",
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
                        saved.get("role_prs")),
                    "resubmission_allowed": _engine.resubmission_allowed(
                        "needs_human", reason, saved.get("role_prs")),
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
                                   "pr_url", "fail_reason", "next_action",
                                   "resubmission_allowed", "_saved_at")}
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
        native TUI the human is watching), then return what the agent has printed
        so far. Use an agent_id from the configured roster. The agent's terminal
        must already be open. Follow up
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
        """Read the current screen of a configured coding agent's LIVE terminal,
        to see what it printed since your last agent_send."""
        try:
            m = _shell_mod()
        except Exception:
            return json.dumps({"error": "interactive terminals are not available here"})
        return json.dumps(m.agent_read(agent_id))

    @tool
    def agent_status(agent_id: str) -> str:
        """Check whether a coding agent from the configured roster
        has a LIVE terminal open that you can drive with agent_send/agent_read."""
        try:
            m = _shell_mod()
        except Exception:
            return json.dumps({"error": "interactive terminals are not available here"})
        return json.dumps(m.agent_status(agent_id))

    # Every inspection uses the selected project's source through the same
    # Gateway as the engine. The host checkout is the workshop implementation:
    # treating it as the project produced a parallel Python service in a Node app.
    import subprocess as _subprocess
    from project_workspace import inspect_project, ProjectUnavailable

    @tool
    def read_file(path: str) -> str:
        """Read a text file from the selected project's default branch.
        Paths are relative to the project root; output is capped at 60 KB.
        Inspect the existing application before dispatching a change."""
        try:
            with inspect_project() as workspace:
                with workspace.resolve(path).open(encoding="utf-8", errors="replace") as f:
                    content = f.read(60_000)
                return json.dumps({**workspace.source, "path": path, "content": content})
        except (OSError, ProjectUnavailable) as exc:
            return json.dumps({"error": f"cannot read {path}: {exc}"})

    @tool
    def list_files(path: str = ".") -> str:
        """List entries in the selected project's default branch.
        Returns each name with a trailing '/' for directories. Use it to explore
        the tree before reading a file. Refuses paths outside the workspace."""
        try:
            with inspect_project() as workspace:
                names = sorted(
                    entry.name + ("/" if entry.is_dir() else "")
                    for entry in workspace.resolve(path).iterdir()
                    if not entry.name.startswith("."))
                return json.dumps({**workspace.source, "path": path, "entries": names[:400]})
        except (OSError, ProjectUnavailable) as exc:
            return json.dumps({"error": f"cannot list {path}: {exc}"})

    @tool
    def grep_workspace(pattern: str, path: str = ".") -> str:
        """Search the selected project for a regex/string, under an
        optional relative subpath. Returns up to 100 'file:line: text' matches.
        Use it to locate a symbol or usage before dispatching. Read-only."""
        try:
            with inspect_project() as workspace:
                proc = _subprocess.run(
                    ["grep", "-rIn", "--exclude-dir=.git", "--exclude-dir=node_modules",
                     "-e", pattern, str(workspace.resolve(path))],
                    capture_output=True, text=True, timeout=20)
                if proc.returncode not in (0, 1):
                    return json.dumps({**workspace.source, "error": proc.stderr[-4000:]})
                lines = [line.replace(str(workspace.root) + os.sep, "")
                         for line in proc.stdout.splitlines()[:100]]
                return json.dumps({**workspace.source, "pattern": pattern,
                                   "matches": lines, "count": len(lines)})
        except (OSError, _subprocess.SubprocessError, ProjectUnavailable) as exc:
            return json.dumps({"error": f"grep failed: {exc}"})

    @tool
    def exec_command(command: str) -> str:
        """Run ONE bounded inspection command in the selected project's snapshot.
        Return its output (stdout,
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
            with inspect_project() as workspace:
                proc = _subprocess.run(
                    ["/bin/bash", "-c", command], cwd=workspace.root,
                    capture_output=True, text=True, timeout=30)
                out = (proc.stdout or "")[-12_000:]
                err = (proc.stderr or "")[-4_000:]
                return json.dumps({**workspace.source, "exit": proc.returncode,
                                   "stdout": out, "stderr": err})
        except _subprocess.TimeoutExpired:
            return json.dumps({"error": "command timed out after 30s"})
        except (OSError, _subprocess.SubprocessError, ProjectUnavailable) as exc:
            return json.dumps({"error": f"command failed to start: {exc}"})

    # The dispatch tools are generated from the ROSTER and added ONLY for builders that
    # are actually WIRED, so the orchestrator's real tool list is (registry x
    # Settings), never a fixed count. An unwired role gets no dispatch tool (the
    # model cannot pick an agent that does not exist); wiring it in Settings adds its
    # tool on the next agent build.
    wired = _wired_roles()
    # Inspection needs no wired coding role. A missing project is reported as
    # missing configuration; it never falls back to inspecting the platform.
    tools = [list_presets, get_user_request, read_file, list_files, grep_workspace, exec_command]
    # A checker-only build is structurally invalid. Exposing a checker dispatch
    # made a live model start a valid focused build AND a redundant rejected run.
    dispatchable = [r for r in _roles.roster()
                    if r.kind == _roles.BUILDER and r.id in wired]
    tools += [_make_dispatch(r) for r in dispatchable]
    # A checker-only installation can still use the read-only review preset.
    if wired:
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
    """The "Your roster" block: one line per served role and its scheduling,
    its role id, and what it does. Generated from the registry, and from the
    operator's per-role description (set in Settings) when there is one, so the
    prompt describes the team this deployment actually runs instead of a hardcoded
    trio the roster may have moved on from."""
    try:
        import runtime_config
        descs = runtime_config.describe_map()
    except Exception:
        descs = {}
    wired = _wired_roles()
    lines = []
    for role in _roles.roster():
        if role.kind == _roles.CHECKER:
            scheduling = (
                "checker; automatically authors executable checks for the engine to run")
        elif role.id in wired:
            scheduling = role.dispatch_tool
        else:
            scheduling = "builder; Runtime not connected"
        lines.append(
            f"- {role.id} ({role.label}; {scheduling}): "
            f"{descs.get(role.id) or role.description}")
    if not lines:
        return ""
    return ("\n\n## Your roster (the roles this deployment serves)\n"
            "Builder dispatch tools already include the independent checker. "
            "Checkers have no standalone dispatch tool. Each line describes a role "
            "and its scheduling. An operator-provided description is authoritative. Only these "
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
    request = UserRequest(prompt, _prior_user_turns(messages))
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
    worker: dict[str, Any] = {}

    def _run() -> None:
        """Worker thread: drive the async stream, push events onto the queue."""
        async def _drive() -> None:
            async with aclosing(stream_user_turn(
                    agent, prompt, agent_input=agent_input, request=request)) as events:
                async for event in events:
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
        loop = asyncio.new_event_loop()
        task = loop.create_task(_drive())
        worker.update(loop=loop, task=task)
        if request.closed.is_set():
            task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 (surface, never hang the stream)
            q.put({"type": "error", "error": str(exc)})
        finally:
            request.close()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            q.put(_DONE)

    threading.Thread(target=lambda: _caller_ctx.run(_run), daemon=True).start()

    # Keepalive: the model can think for well over 30s without emitting a single
    # delta, and an SSE response that sends NO bytes for that long is cut by the
    # transport chain (CloudFront's default origin read timeout is 30s; nginx
    # read-timeouts too). The PTY and runtime-shell streams already ping; this
    # stream must too. A typed event (not an SSE comment) so it survives the
    # JSON encode in console/server.py; every consumer ignores unknown types.
    keepalive_s = float(os.environ.get("WORKSHOP_CHAT_KEEPALIVE_S", "15"))
    try:
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
    finally:
        request.close()
        loop, task = worker.get("loop"), worker.get("task")
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker already closed its loop and its request binding.
