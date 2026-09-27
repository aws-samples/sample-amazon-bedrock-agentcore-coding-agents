"""Real multi-turn conversations, each one a flow that broke before the request ledger.

Every earlier test sent one clean request, which is why 1,900 green tests missed that
"ㄱㄱ", an answer to a clarifying question, a correction, a retry, an attachment, or a
dropped connection each produced the wrong build. These drive the real Strands tools
through the ledger both hosts now use; only the build engine is a recorder.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import chat
import chat_request
import connection_api
from test_chat_request_boundary import (  # noqa: F401 - the shared fixture
    admitted, assistant, frontend_tool, invoke_tool, tools, user,
)

ISSUES = "Build an issue tracker for my team."


@pytest.fixture(autouse=True)
def _anonymous_by_default():
    """The console tests sign people in; never let that identity leak into the next test."""
    import identity_baggage
    identity_baggage.set_current_identity(identity_baggage.ANONYMOUS)
    yield
    identity_baggage.set_current_identity(identity_baggage.ANONYMOUS)


def turn(ledger, text, *calls, attachments=None):
    """One participant turn: bind it, run the tool calls, commit it like a host does."""
    results = []
    request = ledger.begin(chat_request.request_text(text, attachments))
    try:
        with chat_request.bind(request):
            toolset = tools()
            for name, arguments in calls:
                results.append(asyncio.run(invoke_tool(toolset, name, **arguments)))
    finally:
        request.close()
    return results


def test_an_answer_to_a_clarifying_question_keeps_the_original_request(admitted):
    ledger = chat.RequestLedger()
    turn(ledger, ISSUES)
    (result,) = turn(ledger, "FastAPI + React. Also add labels on issues.",
                     ("run_build", {"task": "", "request_turns": [0]}))
    assert result["status"] == "started"
    task = admitted.runs[0].task
    assert task.startswith(ISSUES) and task.endswith("Also add labels on issues.")
    assert "takes precedence" in task


def test_the_retry_repeats_the_recorded_request_not_the_current_question(admitted, monkeypatch):
    """The coordinator retried a role failure while answering "how is it going?" and
    built "how is it going?". resubmit_run repeats the recorded request, once."""
    ledger = chat.RequestLedger()
    (first,) = turn(ledger, ISSUES, ("run_build", {"task": ISSUES}))
    run = admitted.runs[0]
    run.status, run.fail_reason, run.role_prs, run.work_items = (
        "failed", "ROLE_TOTAL_FAILURE", [], {})
    (retry,) = turn(ledger, "how is it going?", ("resubmit_run", {"run_id": first["run_id"]}))
    assert retry["status"] == "started" and retry["resubmission_of"] == first["run_id"]
    assert admitted.runs[1].task == ISSUES
    assert admitted.runs[1].options["chat_request"]["source"] == "resubmission"
    (again,) = turn(ledger, "try again", ("resubmit_run", {"run_id": first["run_id"]}))
    assert again["error"] == "ALREADY_RESUBMITTED_ONCE"
    assert len(admitted.runs) == 2


def test_a_run_with_a_published_pr_is_never_resubmitted(admitted):
    ledger = chat.RequestLedger()
    (first,) = turn(ledger, ISSUES, ("run_build", {"task": ISSUES}))
    run = admitted.runs[0]
    run.status, run.fail_reason, run.role_prs = "needs_human", "ROLE_EXECUTION_ERROR", []
    run.work_items = {"claude-code": type("Item", (), {
        "pr": {"pr_url": "https://github.com/o/r/pull/1"}, "work_id": "w1",
        "public": lambda self: {"pr": {"pr_url": "https://github.com/o/r/pull/1"}}})()}
    (retry,) = turn(ledger, "try again", ("resubmit_run", {"run_id": first["run_id"]}))
    assert retry["error"] == "RESUBMISSION_NOT_ALLOWED"
    assert len(admitted.runs) == 1


def test_only_this_conversations_runs_can_be_resubmitted_or_revised(admitted):
    mine, theirs = chat.RequestLedger(), chat.RequestLedger()
    (first,) = turn(theirs, ISSUES, ("run_build", {"task": ISSUES}))
    results = turn(mine, "retry that", ("resubmit_run", {"run_id": first["run_id"]}),
                   ("run_build", {"task": "", "revise_run": first["run_id"]}))
    assert [r["error"] for r in results] == ["RUN_NOT_STARTED_IN_THIS_CONVERSATION"] * 2


def test_a_correction_after_a_dispatch_revises_that_request(admitted):
    ledger = chat.RequestLedger()
    (first,) = turn(ledger, "A lantern maze game with red lanterns.",
                    ("run_build", {"task": ""}))
    (second,) = turn(ledger, "아니 잠깐, 파란색으로 해줘",
                     ("run_build", {"task": "", "revise_run": first["run_id"]}))
    assert second["status"] == "started"
    task = admitted.runs[1].task
    assert task.startswith("A lantern maze game with red lanterns.")
    assert task.endswith("아니 잠깐, 파란색으로 해줘")


def test_attached_notes_reach_the_build(admitted):
    ledger = chat.RequestLedger()
    notes = [{"name": "playtest.md", "text": "After Begin, the timer froze at 45."},
             {"name": "shot.png", "data": "data:image/png;base64,AAAA"}]
    (result,) = turn(ledger, "Fix the bug described in the attached notes.",
                     ("run_build", {"task": ""}), attachments=notes)
    task = admitted.runs[0].task
    assert "--- attached by the participant: playtest.md ---" in task
    assert "the timer froze at 45" in task
    assert "attached an image, shot.png" in task and "base64" not in task


def test_one_build_per_turn(admitted):
    ledger = chat.RequestLedger()
    results = turn(ledger, ISSUES, (frontend_tool(), {"task": ISSUES}),
                   ("run_build", {"task": ISSUES}))
    assert results[0]["status"] == "started"
    assert results[1]["error"] == "ONE_BUILD_PER_TURN"
    assert len(admitted.runs) == 1


def test_a_dispatch_is_recorded_even_if_the_turn_never_finishes(admitted):
    """A dropped connection after the build started used to forget it, so the same
    "ㄱㄱ" started it again. The ledger records the dispatch at admission."""
    ledger = chat.RequestLedger()
    turn(ledger, ISSUES)
    request = ledger.begin("ㄱㄱ")
    with chat_request.bind(request):
        asyncio.run(invoke_tool(tools(), "run_build", task="", request_turns=[0]))
    # No commit, no tool result delivered to the host: the connection dropped here.
    assert list(ledger.runs) == [admitted.runs[0].run_id]
    request.close()
    (again,) = turn(ledger, "ㄱㄱ", ("run_build", {"task": "", "request_turns": [0]}))
    assert again["error"] == "USER_CONTEXT_UNAVAILABLE"
    assert len(admitted.runs) == 1


def test_a_long_tool_heavy_chat_cannot_trim_the_request_away(admitted):
    """Strands keeps a 40-message window; the ledger does not depend on it."""
    agent = type("Agent", (), {"messages": [user(ISSUES)]})()
    ledger = chat.ledger_for(agent)
    agent.messages = [assistant("tool noise")] * 40      # the window slid past the request
    assert chat.ledger_for(agent) is ledger
    (result,) = turn(ledger, "go", ("run_build", {"task": "", "request_turns": [0]}))
    assert result["status"] == "started" and admitted.runs[0].task.startswith(ISSUES)


@pytest.mark.parametrize("text", [
    "preset=game-from-scratch. Creative direction: a blue dragon",
    "preset=game-from-scratch, Creative direction: a blue dragon",
    "Preset=game-from-scratch Creative direction: a blue dragon",
    "Please use preset=game-from-scratch. Creative direction: a blue dragon",
    "`preset=game-from-scratch` Creative direction: a blue dragon",
])
def test_common_spellings_of_the_lab2_command_select_the_preset(admitted, text):
    ledger = chat.RequestLedger()
    (custom, preset) = turn(
        ledger, text, ("run_build", {"task": text}),
        ("run_build", {"task": "", "preset": "game-from-scratch",
                       "creative_direction": "a blue dragon"}))
    assert custom["error"] == "PRESET_SELECTOR_AS_CUSTOM_TASK"
    assert preset["status"] == "started"
    run = admitted.runs[0]
    assert run.preset == "game-from-scratch"
    assert run.task.startswith(chat._presets.default_task("game-from-scratch"))


@pytest.mark.parametrize("text", ["진행해 주세요", "시작해주세요", "진행 부탁드려요", "바로 진행",
                                  "가보자", "ㄱㄱ요", "고고씽", "넵 부탁드려요",
                                  "Sounds good, go for it", "Looks good to me",
                                  "Yes, go ahead and build it"])
def test_ordinary_approvals_are_recognized(text):
    assert chat_request.is_confirmation(text)


@pytest.mark.parametrize("text", ["파란색으로 해줘", "게임 만들어줘", "go with a blue theme",
                                  "make it faster", "2", "the second one"])
def test_requests_and_bare_choices_are_not_approvals(text):
    assert not chat_request.is_confirmation(text)


def test_the_listing_never_fails_because_a_chat_grew_long(admitted):
    ledger = chat.RequestLedger()
    for n in range(chat_request.MAX_LEDGER_TURNS + 5):
        turn(ledger, f"thought {n}")
    request = ledger.begin("current")
    with chat_request.bind(request):
        listing = asyncio.run(invoke_tool(tools(), "get_user_request"))
    assert "error" not in listing
    assert listing["prior_user_turns"][-1]["text"] == f"thought {chat_request.MAX_LEDGER_TURNS + 4}"
    assert listing["older_turns_not_shown"] == 5


# ------------------------------------------------------------ the console host
def _fake_stream(dispatch=False, finish=True):
    def stream(prompt, *, model_id, messages, attachments, ledger, cancel=None):
        request = ledger.begin(prompt)
        if dispatch:
            ledger.record_dispatch(request, f"run_{len(ledger.runs)}", {"task": prompt})
        yield {"type": "text", "text": "..."}
        if finish:
            request.close()
            yield {"type": "done", "messages": messages + [user(prompt), assistant("ok")]}
    return stream


@pytest.fixture
def console(monkeypatch):
    for name in ("_CONVERSATIONS", "_LEDGERS", "_BUSY"):
        monkeypatch.setattr(connection_api, name, {})
    return connection_api


def test_a_teammate_cannot_continue_someone_elses_conversation(console, monkeypatch):
    monkeypatch.setattr(console._chat, "stream_chat", _fake_stream())
    list(console.chat_stream("c1", "my request", user_identity={"user_id": "a"}))
    list(console.chat_stream("c1", "their message", user_identity={"user_id": "b"}))
    assert console._CONVERSATIONS[("a", "c1")][0]["content"][0]["text"] == "my request"
    assert console._CONVERSATIONS[("b", "c1")][0]["content"][0]["text"] == "their message"


def test_a_second_tab_waits_instead_of_overwriting(console, monkeypatch):
    monkeypatch.setattr(console._chat, "stream_chat", _fake_stream())
    key = console._conversation_key("c1", None)
    console._BUSY[key] = __import__("threading").Lock()
    console._BUSY[key].acquire()
    events = list(console.chat_stream("c1", "second tab"))
    assert events[0]["type"] == "error" and "another tab" in events[0]["error"]
    assert key not in console._CONVERSATIONS


def test_an_interrupted_turn_is_saved_with_the_build_it_started(console, monkeypatch):
    monkeypatch.setattr(console._chat, "stream_chat", _fake_stream(dispatch=True, finish=False))
    stream = console.chat_stream("c1", "ㄱㄱ")
    next(stream)
    stream.close()                         # the browser left mid-turn
    saved = console._CONVERSATIONS["c1"]
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert "it started run run_0" in saved[-1]["content"][0]["text"]
    assert list(console._LEDGERS["c1"].runs) == ["run_0"]


def test_a_trimmed_history_still_starts_with_a_participant_message():
    tool_use = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "x"}}]}
    tool_result = {"role": "user", "content": [{"toolResult": {"toolUseId": "t"}}]}
    messages = [user("q")] + [tool_use, tool_result] * 25 + [user("latest"), assistant("a")]
    trimmed = connection_api._trim_history(messages, 40)
    assert trimmed[0] == user("latest")
    assert json.dumps(trimmed)


def test_the_pages_placeholder_is_never_built_as_a_creative_direction(admitted):
    text = "Use preset=game-from-scratch. Creative direction: <your team's setting, mood, or play idea>"
    (result,) = turn(chat.RequestLedger(), text,
                     ("run_build", {"task": "", "preset": "game-from-scratch",
                                    "creative_direction": "<your team's setting, mood, or play idea>"}))
    assert result["error"] == "PLACEHOLDER_NOT_REPLACED" and admitted.runs == []


def test_the_same_request_twice_while_it_is_still_building_is_one_build(admitted):
    """Pasting the submit block twice opened two sessions, two coordinators, and two
    full builds on one repository."""
    (first,) = turn(chat.RequestLedger(), ISSUES, ("run_build", {"task": ISSUES}))
    admitted.runs[0].status = "running"
    admitted.list = lambda: list(admitted.runs)
    (second,) = turn(chat.RequestLedger(), ISSUES, ("run_build", {"task": ISSUES}))
    assert second["error"] == "DUPLICATE_OF_ACTIVE_BUILD"
    assert second["running_run_id"] == first["run_id"] and len(admitted.runs) == 1


def test_an_approval_cannot_revise_a_run_into_an_unbounded_resubmit(admitted):
    ledger = chat.RequestLedger()
    (first,) = turn(ledger, ISSUES, ("run_build", {"task": ISSUES}))
    (again,) = turn(ledger, "go", ("run_build", {"task": "", "revise_run": first["run_id"]}))
    assert again["error"] == "REVISION_ADDS_NOTHING" and len(admitted.runs) == 1


def test_correcting_a_preset_build_keeps_its_preset_without_a_new_direction(admitted):
    ledger = chat.RequestLedger()
    first_text = "Use preset=game-from-scratch. Creative direction: red paddles"
    (first,) = turn(ledger, first_text, ("run_build", {
        "task": "", "preset": "game-from-scratch", "creative_direction": "red paddles"}))
    (second,) = turn(ledger, "아니 잠깐, 파란색으로 해줘", ("run_build", {
        "task": "blue paddles", "preset": "game-from-scratch",
        "creative_direction": "blue paddles", "revise_run": first["run_id"]}))
    assert second["status"] == "started"
    run = admitted.runs[1]
    assert run.preset == "game-from-scratch"
    assert run.task.startswith(admitted.runs[0].task) and run.task.endswith("파란색으로 해줘")


def test_a_greeting_never_has_to_be_addressed_by_the_next_request(admitted):
    ledger = chat.RequestLedger()
    turn(ledger, "안녕하세요")
    (result,) = turn(ledger, ISSUES, ("run_build", {"task": ISSUES}))
    assert result["status"] == "started" and admitted.runs[0].task == ISSUES


@pytest.mark.parametrize("text", ["해지해 주세요", "취소해줘", "no, wait", "don't do it", "stop"])
def test_a_cancel_or_a_refusal_is_never_an_approval(text):
    assert not chat_request.is_confirmation(text)


def test_a_teammates_identical_request_is_not_their_duplicate(admitted):
    import identity_baggage
    (first,) = turn(chat.RequestLedger(), ISSUES, ("run_build", {"task": ISSUES}))
    admitted.runs[0].status = "running"
    admitted.runs[0].user_identity = {"user_email": "me@workshop.aws"}
    admitted.list = lambda: list(admitted.runs)
    identity_baggage.set_current_identity(identity_baggage.UserIdentity(email="them@workshop.aws"))
    (second,) = turn(chat.RequestLedger(), ISSUES, ("run_build", {"task": ISSUES}))
    assert second["status"] == "started"


def test_one_tool_heavy_turn_is_kept_rather_than_saving_nothing():
    tool_use = {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t", "name": "x"}}]}
    tool_result = {"role": "user", "content": [{"toolResult": {"toolUseId": "t"}}]}
    messages = [user("inspect everything")] + [tool_use, tool_result] * 30 + [assistant("done")]
    trimmed = connection_api._trim_history(messages, 40)
    assert trimmed and trimmed[0] == user("inspect everything") and trimmed[-1] == assistant("done")
