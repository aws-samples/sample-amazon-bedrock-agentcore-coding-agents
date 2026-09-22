"""The game request reaches builders without a room-wide scoring protocol."""
import asyncio

import chat
import presets
import roles
import score_protocol
from test_chat_request_boundary import admitted, invoke_tool, tools  # noqa: F401


def test_public_game_preset_keeps_play_and_persistence_without_a_shared_score_api():
    offered = next(
        item for item in presets.public_presets()
        if item["preset"] == "game-from-scratch"
    )
    task = presets.default_task("game-from-scratch")
    assert offered["task"] == task
    for outcome in (
        "persistent high-score table",
        "one short, complete round",
        "one core mechanic",
        "meaningful earned score",
        "clear end state",
        "restart",
    ):
        assert outcome in task
    assert "/api/scores" not in task
    assert "1000" not in task
    assert "shared room interface" not in task
    assert "reported values" not in task


def test_game_preset_keeps_its_builder_and_independent_checker():
    route = presets.resolve(preset="game-from-scratch")
    assert route.agents == [
        *roles.by_capability("backend"),
        *roles.checker_ids(),
    ]
    assert not route.read_only
    assert any(agent in roles.builder_ids() for agent in route.agents)
    assert any(agent in roles.checker_ids() for agent in route.agents)


def test_chat_delivers_game_goal_and_creative_direction_without_room_scoring(admitted):
    direction = "a tiny kite race with clear keyboard controls and its own visual style"
    prompt = f"Use preset=game-from-scratch. Creative direction: {direction}"
    with chat.bind_user_request(prompt):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="game-from-scratch",
            creative_direction=direction,
        ))
    assert result["status"] == "started"
    assert len(admitted.runs) == 1
    submitted = admitted.runs[0]
    assert submitted.task.startswith(presets.default_task("game-from-scratch") + "\n\n")
    assert submitted.task.endswith(prompt)
    assert submitted.task.count(prompt) == 1
    assert submitted.options["chat_request"]["current_user_text"] == prompt
    assert submitted.options["chat_request"]["creative_direction_verified"] is True
    assert submitted.agents == presets.resolve(preset="game-from-scratch").agents
    assert "/api/scores" not in submitted.task
    assert "1000" not in submitted.task


def test_custom_game_request_can_keep_its_own_range_and_route(admitted):
    prompt = (
        "Extend my existing game. Keep its scores from 0 to 1000 and its "
        "GET /api/scores route; preserve saved rounds."
    )
    with chat.bind_user_request(prompt):
        result = asyncio.run(invoke_tool(tools(), "run_build", task=prompt))
    assert result["status"] == "started"
    assert len(admitted.runs) == 1
    assert admitted.runs[0].task == prompt


def test_legacy_reporter_constants_remain_compatible():
    assert score_protocol.MAX_SCORE == 1000
    assert score_protocol.SCORES_PATH == "api/scores"
