"""Exercise user-request admission through real Strands tools and both chat hosts."""
from __future__ import annotations

import ast
import asyncio
from contextlib import aclosing
import contextvars
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chat
import roles


class RecordingEngine:
    """Record actual tool admission without starting a model, worker, or GitHub call."""

    def __init__(self):
        self.runs = []
        self.lock = threading.Lock()

    def submit(self, task, agents=None, options=None, preset=None):
        with self.lock:
            run = SimpleNamespace(
                run_id=f"run_request_{len(self.runs)}",
                task=task,
                agents=agents or chat._presets.resolve(preset=preset).agents,
                options=copy.deepcopy(options or {}),
                preset=preset,
            )
            self.runs.append(run)
        return run

    def get(self, run_id):
        return next(run for run in self.runs if run.run_id == run_id)


@pytest.fixture
def admitted(monkeypatch):
    engine = RecordingEngine()
    monkeypatch.setattr(chat, "ENGINE", engine)
    monkeypatch.setattr(chat, "_wired_roles", lambda: set(roles.roster_ids()))
    monkeypatch.setattr(
        "integration_plan.select_capabilities",
        lambda task, available, **kwargs: ([available[0]], "test capability selection"),
    )
    return engine


def tools():
    return {tool.tool_name: tool for tool in chat.build_tools()}


async def invoke_tool(toolset, name, **arguments):
    """Use Strands validation and its actual to_thread dispatch, not the raw function."""
    events = [
        event async for event in toolset[name].stream(
            {"toolUseId": "request-test-tool", "name": name, "input": arguments}, {},
        )
    ]
    result = events[-1]["tool_result"]
    assert result["status"] == "success", result
    return json.loads(result["content"][0]["text"])


def frontend_tool():
    return next(role.dispatch_tool for role in roles.roster()
                if role.kind == roles.BUILDER and role.capability == "frontend")


class ToolAgent:
    def __init__(self, tool_name, arguments, messages=None):
        self.tool_name = tool_name
        self.arguments = arguments
        self.messages = copy.deepcopy(messages or [])
        self.hooks = SimpleNamespace(add_callback=lambda *args: None)
        self.toolset = tools()
        self.results = []

    async def stream_async(self, prompt):
        result = await invoke_tool(self.toolset, self.tool_name, **self.arguments)
        self.results.append(result)
        yield {"data": json.dumps(result)}


def deployed_entrypoint(agent):
    """Compile the actual cached-agent factory and entrypoint, without starting its server."""
    source = ast.parse((HERE.parent / "orchestrator-agent/main.py").read_text())
    definitions = [
        node for node in source.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_get_or_create_agent", "invoke"}
    ]
    for node in definitions:
        node.decorator_list = []
    namespace = {
        "Any": object,
        "_agent": agent,
        "_chat": chat,
        "_activity": SimpleNamespace(ensure_started=lambda: None, observe=lambda: None),
        "log": Mock(),
        "aclosing": aclosing,
    }
    exec(compile(ast.Module(body=definitions, type_ignores=[]),
                 "actual-deployed-entrypoint", "exec"), namespace)
    return namespace["invoke"]


@pytest.mark.parametrize("tool_name", [frontend_tool(), "run_build"])
def test_console_submits_current_user_text_not_model_constraints(admitted, monkeypatch, tool_name):
    original = "  Fix the duplicate result display.\nPreserve stored values, including zero.  "
    invented = original + "\nChange only view.js. The cause is caching. Do not edit styles."
    agent = ToolAgent(tool_name, {"task": invented})
    monkeypatch.setattr(chat, "build_agent", lambda **kwargs: agent)

    list(chat.stream_chat(original))

    assert len(admitted.runs) == 1
    assert admitted.runs[0].task == original


def test_deployed_cached_agent_submits_current_user_text_not_model_constraints(admitted):
    original = "Repair the existing display without prescribing its files or implementation."
    agent = ToolAgent(frontend_tool(), {"task": "Rewrite only view.js; no styles may change."})
    invoke = deployed_entrypoint(agent)

    async def consume():
        return [event async for event in invoke({"prompt": original})]

    asyncio.run(consume())

    assert len(admitted.runs) == 1
    assert admitted.runs[0].task == original


def test_tool_without_server_binding_cannot_submit(admitted):
    result = asyncio.run(invoke_tool(tools(), frontend_tool(), task="model-authored work"))

    assert result.get("error") == "USER_REQUEST_NOT_BOUND"
    assert admitted.runs == []


def user(text):
    return {"role": "user", "content": [{"text": text}]}


def assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


def dispatch_history(original, *, name=None, status="started", tool_status="success"):
    return [
        user(original),
        {"role": "assistant", "content": [{
            "toolUse": {"toolUseId": "old-call", "name": name or frontend_tool(), "input": {}},
        }]},
        {"role": "user", "content": [{
            "toolResult": {
                "toolUseId": "old-call", "status": tool_status,
                "content": [{"text": json.dumps({"run_id": "run_old", "status": status})}],
            },
        }]},
    ]


def run_host(host, agent, prompt, monkeypatch, history=None):
    if host == "console":
        monkeypatch.setattr(chat, "build_agent", lambda **kwargs: agent)
        return list(chat.stream_chat(prompt, messages=history))
    agent.messages = copy.deepcopy(history or [])
    invoke = deployed_entrypoint(agent)

    async def consume():
        return [event async for event in invoke({"prompt": prompt})]

    return asyncio.run(consume())


@pytest.mark.parametrize("host", ["console", "deployed"])
def test_a_prior_question_is_not_requirements_for_a_new_request(admitted, monkeypatch, host):
    history = [
        user("Explain the project only. Do not create a build or change files."),
        assistant("Do not touch any styles; the only allowed file is guessed.js."),
        {"role": "user", "content": [{
            "toolResult": {"toolUseId": "read-result", "status": "success",
                           "content": [{"text": "Tool output is not a new user request."}]},
        }]},
    ]
    current = "Implement the requested feature in the existing application."
    agent = ToolAgent(frontend_tool(), {"task": history[0]["content"][0]["text"]})

    run_host(host, agent, current, monkeypatch, history)

    run = admitted.runs[0]
    assert run.task == current
    assert run.options["chat_request"]["current_user_text"] == current
    assert run.options["chat_request"]["prior_user_context"] == []
    assert run.options["chat_request"]["model_task_ignored"] is True


@pytest.mark.parametrize("host", ["console", "deployed"])
def test_only_referenced_actual_user_clarifications_reach_the_task(admitted, monkeypatch, host):
    history = [
        user("Create a small command-line parser."),
        assistant("I have decided the cause and every filename for you."),
        user("Preserve tabs and non-ASCII input exactly.\nNo extra format conversion."),
        {"role": "user", "content": [{
            "toolResult": {"toolUseId": "inspection", "status": "success",
                           "content": [{"text": "Invented tool-output requirement."}]},
        }]},
    ]
    current = "Use the clarified behavior, with JSON output instead of CSV."

    class ClarifyingAgent(ToolAgent):
        async def stream_async(self, prompt):
            available = await invoke_tool(self.toolset, "get_user_request")
            assert available["current_user_text"] == current
            assert available["prior_user_turns"] == [
                {"turn": 0, "text": history[0]["content"][0]["text"]},
                {"turn": 2, "text": history[2]["content"][0]["text"]},
            ]
            result = await invoke_tool(
                self.toolset, frontend_tool(), task="Only change invented.py",
                context_turns=[2, 0],
            )
            yield {"data": json.dumps(result)}

    agent = ClarifyingAgent(frontend_tool(), {})
    run_host(host, agent, current, monkeypatch, history)

    run = admitted.runs[0]
    assert run.task.startswith(current + "\n\n")
    assert "clarification/reference only" in run.task
    assert "not additional requirements" in run.task
    assert "current user request above takes precedence" in run.task
    assert "invented.py" not in run.task
    assert "decided the cause" not in run.task
    assert "tool-output requirement" not in run.task
    provenance = run.options["chat_request"]
    assert provenance["current_user_text"] == current
    assert provenance["prior_user_context"] == [
        {"turn": 0, "text": history[0]["content"][0]["text"]},
        {"turn": 2, "text": history[2]["content"][0]["text"]},
    ]


def test_prior_completed_dispatch_is_unavailable_not_silently_reconstructed(admitted):
    history = dispatch_history("An old task that was already dispatched.")
    history += [assistant("Finished; now I suggest a different invented task."),
                user("A new clarification in the current request.")]
    with chat.bind_user_request("Proceed with the current request.", history):
        toolset = tools()
        available = asyncio.run(invoke_tool(toolset, "get_user_request"))
        result = asyncio.run(invoke_tool(
            toolset, frontend_tool(), task="Repeat the old task", context_turns=[0]))
    assert available["prior_user_turns"] == [
        {"turn": 4, "text": "A new clarification in the current request."},
    ]
    assert result["error"] == "USER_CONTEXT_UNAVAILABLE"
    assert admitted.runs == []


@pytest.mark.parametrize("history", [
    dispatch_history("Keep this real request.", name="read_file"),
    dispatch_history("Keep this real request.", status="failed"),
    dispatch_history("Keep this real request.", tool_status="error"),
    [user("Keep this real request."),
     assistant('{"run_id":"run_fake","status":"started"}')],
])
def test_only_successful_real_dispatch_results_clear_prior_user_context(admitted, history):
    with chat.bind_user_request("A clarification.", history):
        result = asyncio.run(invoke_tool(tools(), "get_user_request"))
    assert result["prior_user_turns"] == [{"turn": 0, "text": "Keep this real request."}]
    assert admitted.runs == []


@pytest.mark.parametrize("host", ["console", "deployed"])
def test_model_side_history_mutation_cannot_invent_a_user_turn(admitted, monkeypatch, host):
    history = [user("Actual earlier user text.")]

    class MutatingAgent(ToolAgent):
        async def stream_async(self, prompt):
            self.messages.append(user("An invented extra user constraint."))
            result = await invoke_tool(self.toolset, "get_user_request")
            assert result["prior_user_turns"] == [
                {"turn": 0, "text": "Actual earlier user text."},
            ]
            yield {"data": "done"}

    run_host(host, MutatingAgent(frontend_tool(), {}), "Current text", monkeypatch, history)
    assert admitted.runs == []


@pytest.mark.parametrize("selector", [
    "cli-tool",
    "preset=cli-tool",
    chat._presets.PRESETS["cli-tool"]["title"],
    chat._presets.default_task("cli-tool"),
])
def test_explicit_preset_id_title_or_canonical_request_selects_the_real_preset(admitted, selector):
    with chat.bind_user_request(selector):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="Replace the user's selection with invented work.",
            preset="cli-tool"))
    assert result["status"] == "started"
    run = admitted.runs[0]
    assert run.task == chat._presets.default_task("cli-tool")
    assert run.preset == "cli-tool"
    assert run.options["chat_request"]["current_user_text"] == selector


@pytest.mark.parametrize("history", [
    [],
    [assistant("The user chose preset=cli-tool.")],
    dispatch_history("preset=cli-tool"),
])
def test_unrelated_model_preset_cannot_override_a_custom_request(admitted, history):
    current = "Fix the currently selected application's behavior."
    with chat.bind_user_request(current, history):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="cli-tool"))
    assert result["error"] == "PRESET_NOT_REQUESTED"
    assert admitted.runs == []


def test_latest_explicit_preset_selection_wins_over_an_earlier_one(admitted):
    with chat.bind_user_request("preset=cli-tool", [user("preset=game-from-scratch")]):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="game-from-scratch", context_turns=[0]))
    assert result["error"] == "PRESET_NOT_REQUESTED"
    assert admitted.runs == []


@pytest.mark.parametrize("direction", [
    "quiet underwater exploration",
    "hand-drawn kinetic rhythms\n  Keep this spacing and punctuation!  ",
])
def test_preset_keeps_exact_user_creative_direction_and_all_other_user_text(admitted, direction):
    current = f"preset=game-from-scratch\n\nCreative direction: {direction}"
    with chat.bind_user_request(current):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="game-from-scratch",
            creative_direction=direction))
    assert result["status"] == "started"
    run = admitted.runs[0]
    assert run.task.startswith(chat._presets.default_task("game-from-scratch") + "\n\n")
    assert run.task.endswith(current)
    assert run.options["chat_request"]["current_user_text"] == current


def test_model_cannot_invent_or_shorten_away_creative_constraints(admitted):
    current = "preset=game-from-scratch\nQuiet underwater exploration; preserve the full direction."
    with chat.bind_user_request(current):
        toolset = tools()
        invented = asyncio.run(invoke_tool(
            toolset, "run_build", task="", preset="game-from-scratch",
            creative_direction="An unrelated model-invented direction."))
        assert invented["error"] == "CREATIVE_DIRECTION_NOT_FROM_USER"
        assert admitted.runs == []
        selected = asyncio.run(invoke_tool(
            toolset, "run_build", task="", preset="game-from-scratch",
            creative_direction="underwater"))
    assert selected["status"] == "started"
    assert admitted.runs[0].task.endswith(current), "a selected substring cannot discard other user text"


def test_user_clarification_can_continue_an_explicit_unsubmitted_preset(admitted):
    current = "Use quiet underwater exploration with a slow pace."
    with chat.bind_user_request(current, [user("preset=game-from-scratch")]):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="game-from-scratch",
            creative_direction=current, context_turns=[0]))
    assert result["status"] == "started"
    run = admitted.runs[0]
    assert run.task.startswith(chat._presets.default_task("game-from-scratch"))
    assert current in run.task
    assert run.options["chat_request"]["prior_user_context"] == [
        {"turn": 0, "text": "preset=game-from-scratch"},
    ]


@pytest.mark.parametrize("current,error", [
    (" \n ", "EMPTY_USER_REQUEST"),
    ("🙂" * (chat.MAX_USER_REQUEST_BYTES // 4 + 1), "USER_REQUEST_TOO_LARGE"),
    ("\x00" * (chat.MAX_USER_REQUEST_BYTES // 2), "USER_REQUEST_PROVENANCE_TOO_LARGE"),
])
def test_incomplete_or_oversized_request_is_rejected_without_clipping(admitted, current, error):
    with chat.bind_user_request(current):
        result = asyncio.run(invoke_tool(tools(), frontend_tool(), task="a model's substitute"))
    assert result["error"] == error
    assert admitted.runs == []


def test_large_prior_context_is_explicitly_unavailable_but_does_not_replace_a_clear_task(admitted):
    prior = "x" * (chat.MAX_USER_CONTEXT_BYTES + 1)
    current = "This is a complete new request."
    with chat.bind_user_request(current, [user(prior)]):
        toolset = tools()
        listing = asyncio.run(invoke_tool(toolset, "get_user_request"))
        rejected = asyncio.run(invoke_tool(
            toolset, frontend_tool(), task="summarized old input", context_turns=[0]))
        assert listing["error"] == rejected["error"] == "USER_CONTEXT_TOO_LARGE"
        assert admitted.runs == []
        accepted = asyncio.run(invoke_tool(toolset, frontend_tool(), task="model replacement"))
    assert accepted["status"] == "started"
    assert admitted.runs[0].task == current
    assert admitted.runs[0].options["chat_request"]["prior_user_context"] == []


def test_more_context_turns_than_the_bound_are_not_silently_dropped(admitted):
    history = [user(f"Actual user turn {n}") for n in range(chat.MAX_USER_CONTEXT_TURNS + 1)]
    with chat.bind_user_request("A current clarification.", history):
        toolset = tools()
        result = asyncio.run(invoke_tool(toolset, "get_user_request"))
        rejected = asyncio.run(invoke_tool(
            toolset, frontend_tool(), task="replacement", context_turns=list(range(len(history)))))
    assert result["error"] == rejected["error"] == "USER_CONTEXT_TOO_LARGE"
    assert admitted.runs == []


@pytest.mark.parametrize("host", ["console", "deployed"])
def test_concurrent_turns_use_independent_bindings_with_shared_decorated_tools(admitted, monkeypatch, host):
    barrier = threading.Barrier(2)

    class ConcurrentAgent(ToolAgent):
        async def stream_async(self, prompt):
            before = await invoke_tool(self.toolset, "get_user_request")
            await asyncio.to_thread(barrier.wait, timeout=3)
            result = await invoke_tool(self.toolset, frontend_tool(), task="shared model replacement")
            assert before["current_user_text"] == prompt
            yield {"data": json.dumps(result)}

    agent = ConcurrentAgent(frontend_tool(), {})
    prompts = ["First user's complete request.", "Second user's different request.\nKeep this line."]
    if host == "console":
        monkeypatch.setattr(chat, "build_agent", lambda **kwargs: agent)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda prompt: list(chat.stream_chat(prompt)), prompts))
    else:
        invoke = deployed_entrypoint(agent)

        async def consume(prompt):
            return [event async for event in invoke({"prompt": prompt})]

        async def both():
            await asyncio.gather(*(consume(prompt) for prompt in prompts))

        asyncio.run(both())
    assert sorted(run.task for run in admitted.runs) == sorted(prompts)
    assert all(run.options["chat_request"]["current_user_text"] == run.task for run in admitted.runs)
    outside = asyncio.run(invoke_tool(agent.toolset, frontend_tool(), task="late unbound work"))
    assert outside["error"] == "USER_REQUEST_NOT_BOUND"
    assert len(admitted.runs) == 2


def test_cached_agent_rebinds_each_turn_and_does_not_leak_across_yield_or_close(admitted):
    agent = ToolAgent(frontend_tool(), {"task": "a stale task argument cached with the agent"})
    invoke = deployed_entrypoint(agent)
    originals = ["The first real request.", "A completely different second request."]

    async def consume():
        for original in originals:
            response = invoke({"prompt": original})
            await anext(response)
            outside = await invoke_tool(agent.toolset, "get_user_request")
            assert outside["error"] == "USER_REQUEST_NOT_BOUND"
            # Close in a DIFFERENT asyncio context: no ContextVar token from a
            # suspended generator may be reset in the wrong task.
            await asyncio.create_task(response.aclose())

    asyncio.run(consume())
    assert [run.task for run in admitted.runs] == originals


@pytest.mark.parametrize("host", ["console", "deployed"])
@pytest.mark.parametrize("ending", ["exception", "disconnect"])
def test_failed_or_disconnected_turn_invalidates_copied_tool_context(admitted, monkeypatch, host, ending):
    finished = threading.Event()
    escaped = []

    class EndingAgent(ToolAgent):
        async def stream_async(self, prompt):
            escaped.append(contextvars.copy_context())
            try:
                yield {"data": "ready"}
                if ending == "exception":
                    raise RuntimeError("test model failure")
                await asyncio.Event().wait()
            finally:
                finished.set()

    agent = EndingAgent(frontend_tool(), {})
    if host == "console":
        monkeypatch.setattr(chat, "build_agent", lambda **kwargs: agent)
        response = chat.stream_chat("An unfinished user request.")
        assert next(response)["text"] == "ready"
        if ending == "exception":
            assert any(event.get("type") == "error" for event in response)
        else:
            response.close()
        assert finished.wait(3), "disconnect must close the real worker's model stream"
    else:
        invoke = deployed_entrypoint(agent)

        async def consume():
            response = invoke({"prompt": "An unfinished user request."})
            assert await anext(response) == "ready"
            if ending == "exception":
                with pytest.raises(RuntimeError, match="test model failure"):
                    await anext(response)
            else:
                await asyncio.create_task(response.aclose())

        asyncio.run(consume())
        assert finished.is_set()
    stale = escaped[0].run(
        lambda: asyncio.run(invoke_tool(agent.toolset, frontend_tool(), task="late model work")))
    assert stale["error"] == "USER_REQUEST_CLOSED"
    assert admitted.runs == []


def test_routing_that_returns_after_scope_closes_cannot_submit(admitted, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    resolve = chat._presets.resolve

    def slow_route(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(chat._presets, "resolve", slow_route)
    with chat.bind_user_request("The actual current request.") as request:
        toolset = tools()

        async def exercise():
            pending = asyncio.create_task(invoke_tool(
                toolset, "run_build", task="a fabricated implementation manifest"))
            assert await asyncio.to_thread(entered.wait, 3)
            request.close()
            release.set()
            return await pending

        result = asyncio.run(exercise())
    assert result["error"] == "USER_REQUEST_CLOSED"
    assert admitted.runs == []
