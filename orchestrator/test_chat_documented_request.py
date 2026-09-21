"""Actual workshop request syntax and one routing decision per submitted build."""
import asyncio
from types import SimpleNamespace

import pytest

import chat
import roles
from test_chat_request_boundary import admitted, invoke_tool, tools  # noqa: F401


def test_documented_lab2_command_keeps_the_preset_and_complete_user_direction(admitted):
    direction = "a moonlit river with paper boats; keep it playable with a keyboard"
    prompt = f"Use preset=game-from-scratch. Creative direction: {direction}"
    with chat.bind_user_request(prompt):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="game-from-scratch",
            creative_direction=direction))
    assert result["status"] == "started"
    run = admitted.runs[0]
    assert run.task.startswith(chat._presets.default_task("game-from-scratch") + "\n\n")
    assert run.task.endswith(prompt)
    assert run.options["chat_request"]["current_user_text"] == prompt
    assert run.options["chat_request"]["creative_direction_verified"] is True


@pytest.mark.parametrize("prompt", [
    "What does Use preset=game-from-scratch mean?",
    "Do not use preset=game-from-scratch.",
    "Use preset=game-from-scratchish. Creative direction: water",
    "Use preset=game-from-scratch.other",
])
def test_a_preset_mention_or_longer_identifier_does_not_authorize_expansion(admitted, prompt):
    with chat.bind_user_request(prompt):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="game-from-scratch"))
    assert result["error"] == "PRESET_NOT_REQUESTED"
    assert admitted.runs == []


@pytest.mark.parametrize("preset", ["", "your-own"])
def test_custom_build_routes_once_through_real_resolution_and_admission(admitted, monkeypatch, preset):
    calls = []
    frontend = next(role.id for role in roles.builders() if role.capability == "frontend")
    checkers = list(roles.checker_ids())

    def changing_selector(task, available, **kwargs):
        calls.append(task)
        return (["frontend"] if len(calls) == 1 else ["backend"]), "test-routing"

    monkeypatch.setattr("integration_plan.select_capabilities", changing_selector)
    prompt = "Fix the existing game's timer without changing its saved scores."
    if preset:
        prompt = f"Use preset={preset}. {prompt}"
    with chat.bind_user_request(prompt):
        result = asyncio.run(invoke_tool(tools(), "run_build", task=prompt, preset=preset))
    submitted = admitted.runs[0]
    probe = chat._engine.Run(
        run_id="custom-route-admission", task=submitted.task,
        agents=list(submitted.agents), roles={}, options={},
        created_at="2026-09-21T00:00:00Z")
    probe._preset_req = submitted.preset
    probe._explicit_agents = bool(submitted.agents)
    engine = SimpleNamespace(active_count=lambda **_kw: 0, max_concurrent=2)
    assert chat._engine.Engine._admission(engine, probe)
    assert calls == [submitted.task]
    assert result["agents"] == admitted.runs[0].agents == [frontend, *checkers]
    assert probe.agents == result["agents"]
    assert result["routing"].endswith("test-routing")


def test_read_only_preset_reaches_real_admission_without_becoming_a_build(admitted, monkeypatch):
    arguments = {}
    record_submit = admitted.submit

    def capture_submit(task, **kwargs):
        arguments.update(kwargs)
        return record_submit(task, **kwargs)

    monkeypatch.setattr(admitted, "submit", capture_submit)
    with chat.bind_user_request("review-a-run"):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="review-a-run"))
    submitted = admitted.runs[0]
    probe = chat._engine.Run(
        run_id="readonly-preset-admission", task=submitted.task,
        agents=list(arguments["agents"] or []), roles={}, options={},
        created_at="2026-09-21T00:00:00Z")
    probe._preset_req = submitted.preset
    probe._explicit_agents = bool(arguments["agents"])
    engine = SimpleNamespace(active_count=lambda **_kw: 0, max_concurrent=2)
    assert chat._engine.Engine._admission(engine, probe)
    assert probe.route["read_only"] is True
    assert probe.agents == result["agents"] == list(roles.checker_ids())
    assert arguments["agents"] is None
