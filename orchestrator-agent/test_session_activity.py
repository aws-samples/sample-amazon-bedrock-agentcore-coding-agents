"""Native SDK health must follow real background activity and retain its bound."""
from __future__ import annotations

import ast
import asyncio
from contextlib import aclosing
from pathlib import Path
import sys
import types
from unittest import mock

import pytest
from bedrock_agentcore.runtime import BedrockAgentCoreApp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from session_activity import ActivityTracker  # noqa: E402
sys.path.insert(0, str(HERE.parent / "orchestrator"))
import chat  # noqa: E402


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.delenv("WORKSHOP_KEEPALIVE_MAX_S", raising=False)
    state = {"count": 0, "clock": 0.0}
    app = BedrockAgentCoreApp()
    tracker = ActivityTracker(
        app, lambda: state["count"], max_s=1800, now=lambda: state["clock"],
    )
    yield app, tracker, state
    tracker.close()


def status(app):
    return app.get_current_ping_status().value


def test_real_sdk_stays_busy_past_fifteen_minutes_without_an_invocation(environment):
    app, tracker, state = environment
    tracker.observe()
    assert status(app) == "Healthy"
    state["count"] = 1
    tracker.observe()
    task_id = tracker._task_id
    state["clock"] = 1200
    tracker.observe()
    assert status(app) == "HealthyBusy"
    assert app.get_async_task_info()["active_count"] == 1
    assert tracker._task_id == task_id, "observation must not restart the task or its budget"


def test_all_workers_must_finish_before_sdk_returns_to_idle(environment):
    app, tracker, state = environment
    for count in (2, 1):
        state["count"] = count
        tracker.observe()
        assert status(app) == "HealthyBusy"
    state["count"] = 0
    tracker.observe()
    assert status(app) == "Healthy"
    assert app.get_async_task_info()["active_count"] == 0


def test_cap_releases_a_wedged_build_and_repeated_calls_cannot_renew_it(environment):
    app, tracker, state = environment
    state["count"] = 1
    tracker.observe()
    for moment in (1800, 1801, 3600):
        state["clock"] = moment
        tracker.observe()
        assert status(app) == "Healthy"
        assert app.get_async_task_info()["active_count"] == 0
    state["count"] = 0
    tracker.observe()
    state.update(count=1, clock=3601)
    tracker.observe()
    assert status(app) == "HealthyBusy", "a genuinely new work window gets its own budget"


def test_a_failing_counter_remains_bounded(environment):
    app, tracker, state = environment
    state["count"] = 1
    tracker.observe()
    tracker.in_flight = mock.Mock(side_effect=RuntimeError("counter unavailable"))
    state["clock"] = 1200
    tracker.observe()
    assert status(app) == "HealthyBusy"
    state["clock"] = 1800
    tracker.observe()
    assert status(app) == "Healthy"


def test_tracking_does_not_clear_someone_elses_async_task(environment):
    app, tracker, state = environment
    other = app.add_async_task("unrelated")
    state["count"] = 1
    tracker.observe()
    state["count"] = 0
    tracker.observe()
    assert app.get_async_task_info()["active_count"] == 1
    assert status(app) == "HealthyBusy"
    app.complete_async_task(other)


def test_close_releases_our_registration_and_prevents_rearming(environment):
    app, tracker, state = environment
    state["count"] = 1
    tracker.ensure_started()
    thread = tracker._thread
    tracker.ensure_started()
    assert tracker._thread is thread
    tracker.close()
    tracker.observe()
    assert status(app) == "Healthy"
    assert not thread.is_alive()


def test_zero_is_a_valid_sdk_task_id(monkeypatch):
    monkeypatch.delenv("WORKSHOP_KEEPALIVE_MAX_S", raising=False)
    app = mock.Mock()
    app.add_async_task.return_value = 0
    state = {"count": 1}
    tracker = ActivityTracker(app, lambda: state["count"], max_s=60)
    tracker.observe()
    tracker.observe()
    state["count"] = 0
    tracker.observe()
    app.add_async_task.assert_called_once()
    app.complete_async_task.assert_called_once_with(0)


def test_operator_override_remains_supported(monkeypatch):
    monkeypatch.setenv("WORKSHOP_KEEPALIVE_MAX_S", "111")
    assert ActivityTracker(mock.Mock(), lambda: 0, max_s=7080).max_s == 111


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_bounds_fail_before_work_can_start(monkeypatch, duration):
    monkeypatch.delenv("WORKSHOP_KEEPALIVE_MAX_S", raising=False)
    with pytest.raises(ValueError):
        ActivityTracker(mock.Mock(), lambda: 0, max_s=duration)


@pytest.mark.parametrize("ending", ["return", "raise", "disconnect"])
def test_actual_entrypoint_registers_dispatched_work_before_response_closes(environment, ending):
    app, tracker, state = environment
    source = ast.parse((HERE / "main.py").read_text())
    invoke = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "invoke")
    invoke.decorator_list = []

    class Agent:
        async def stream_async(self, prompt):
            state["count"] = 1  # A tool dispatched a worker.
            yield {"data": "accepted"}
            if ending == "raise":
                raise RuntimeError("chat failed after dispatch")

    namespace = {
        "Any": object, "_activity": tracker, "log": mock.Mock(),
        "_get_or_create_agent": Agent, "_chat": chat, "aclosing": aclosing,
    }
    exec(compile(ast.Module(body=[invoke], type_ignores=[]), "actual-entrypoint", "exec"), namespace)

    async def consume():
        response = namespace["invoke"]({"prompt": "build"}, types.SimpleNamespace())
        assert await anext(response) == "accepted"
        if ending == "disconnect":
            await response.aclose()
        elif ending == "raise":
            with pytest.raises(RuntimeError, match="chat failed"):
                await anext(response)
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(response)

    asyncio.run(consume())
    assert status(app) == "HealthyBusy"


def test_activity_has_no_model_network_or_verdict_dependency():
    source = ast.parse((HERE / "session_activity.py").read_text())
    modules = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(source) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert not modules.intersection({"boto3", "engine", "reviewer", "llm", "run_store"})
    main = (HERE / "main.py").read_text()
    assert "STRANDED_AFTER_S" in main
    assert "KEEPALIVE_PROMPT" not in main
