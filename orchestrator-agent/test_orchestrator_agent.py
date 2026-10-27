"""Tests for the orchestrator agent: the brain (chat.py) and its thin AgentCore
wrapper (main.py). The Strands tools are REAL.

The orchestrator is a CHATBOT: the model converses and only dispatches an agent
when it chooses to call a dispatch_*/run_build tool. These tests prove the tool
wiring without a model call: list_presets reads the real preset registry; the dispatch tools
kick a REAL run on the engine (non-blocking) and we poll it to a terminal verdict
through the validator's authored gate. The engine is REAL-ONLY (it would dispatch to deployed
runtimes and fail loud without them), so the test injects the test-only
FixtureExecutor by constructor: the same seam the shipped binary never exposes
via a flag. The Strands agent loop itself (which calls Bedrock) is exercised live
in the workshop and by the console smoke tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

# Test canonical source even if an ignored deployment bundle exists beside main.
# That bundle deliberately omits the test-only FixtureExecutor.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))
import chat  # noqa: E402  the orchestrator brain (prompt + tools + agent + stream)
import main  # noqa: E402  the thin AgentCore Runtime wrapper over chat
import roles  # noqa: E402
import runtime_config  # noqa: E402
from fixture_executor import FixtureExecutor  # noqa: E402

# Share ONE fixture-backed engine across the brain and its tools, so a dispatch
# tool's run is the run we poll. chat.build_tools() reads chat.ENGINE at call
# time, so wiring it here is the only seam needed (real DI, no monkeypatch).
# NOTE: the autouse fixture below RE-pins this on every test. The module-level
# call alone is unsafe: importing any module that calls chat.use_engine() at
# import time (e.g. connection_api, pulled in by console/e2e tests) replaces the
# global chat.ENGINE with a REAL-executor engine, after which these dispatch tests
# would poll an engine that never ran the fixture build. Re-pinning per test makes
# the suite order-independent.
chat.use_engine(chat._engine.Engine(executor_obj=FixtureExecutor()))


@pytest.fixture(autouse=True)
def _wire_all_roles(tmp_path, monkeypatch):
    """The dispatch tools are generated from the WIRED roles (R8), so wire all
    three coding roles to a temp config via the real env var the module reads.
    Without this the tool set is converse-only (list_presets + run_status) and the
    dispatch tests have nothing to call. Real-seam isolation, no monkeypatch of
    internals. main._get_or_create_agent caches; reset it so each test rebuilds
    the agent against the wired tool set."""
    # Re-pin the fixture-backed engine for THIS test: another test module importing
    # connection_api (which calls chat.use_engine at import) may have swapped the
    # global to a real-executor engine since collection. Without this the dispatch
    # tools would submit to an engine the fixture build never touched and the poll
    # to a terminal verdict would hang/fail. Order-independent isolation.
    chat.use_engine(chat._engine.Engine(executor_obj=FixtureExecutor()))
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    for r in runtime_config.roles():
        monkeypatch.delenv(runtime_config._env_key(r), raising=False)
    for role in chat._roles.roster_ids():
        runtime_config.save_runtime(role, role.replace("-", "_") + "-TESTID0001")
    main._agent = None  # drop any cached agent so it rebuilds with the wired tools


def _tools_map():
    """Current tool set as {name: tool}; rebuilt each call because it is generated
    from the wired roles at build time."""
    return {getattr(t, "tool_name", getattr(t, "__name__", "")): t for t in chat.build_tools()}


def _call(name, **kwargs):
    """Invoke a Strands @tool's underlying function regardless of wrapper shape."""
    tool = _tools_map()[name]
    fn = getattr(tool, "func", None) or getattr(tool, "_tool_func", None) or tool
    if name == "run_build" or name.startswith("dispatch_"):
        # This fixture's task is its explicit participant turn. Production calls
        # receive that binding from the actual invoke/stream_chat entrypoint.
        with chat.bind_user_request(kwargs["task"]):
            return fn(**kwargs)
    return fn(**kwargs)


def _await_terminal(run_id, timeout=60):
    """A dispatch tool is NON-BLOCKING (returns once the run is submitted); poll
    the shared engine to the terminal verdict the gate produced."""
    terminal = {"passed", "failed", "needs_human"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = chat.ENGINE.get(run_id)
        if run and run.status in terminal:
            return chat._engine.public_result(run)
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} never reached a terminal state")


# --------------------------------------------------------- list_presets (advisory)
def test_list_presets_offers_starting_points_and_the_attendees_own_request():
    out = json.loads(_call("list_presets"))
    ids = {p["preset"] for p in out["presets"]}
    assert "your-own" in ids, "the attendee's own request must be first-class"
    for p in out["presets"]:
        assert p["roles"]
        if not p["read_only"]:
            # The SERVED checker, from the registry rather than a role id literal.
            assert roles.checker_ids()[0] in p["roles"], p["preset"]


# ------------------------------------------------- dispatch tools (non-blocking, real)
def test_dispatch_backend_kicks_a_real_single_role_run():
    """A dispatch_* tool starts ONE deployed agent (subagents-as-tool) for real:
    it returns IMMEDIATELY with a run id (the chatbot keeps talking), and the run
    grades through the same engine + authored gate when we poll it."""
    out = json.loads(_call("dispatch_backend",
                           task="fix whatever needs fixing in this workspace"))
    assert out["agent"] == "claude-code"
    assert out["status"] == "started"          # non-blocking: started, not a verdict
    assert out["kind"] == "backend"
    assert out["run_id"].startswith("run_")
    result = _await_terminal(out["run_id"])     # the real build + gate
    assert result["status"] == "passed"
    assert result["gate"]["passed"] is True


def test_run_build_accepts_any_request_and_runs_it():
    """The headline: an arbitrary sentence, matching no sample and no keyword, runs
    all the way to a verdict."""
    out = json.loads(_call("run_build", task="write me a haiku about tuesday"))
    assert out["status"] == "started"
    assert out["run_id"].startswith("run_")
    result = _await_terminal(out["run_id"])
    assert result["status"] == "passed"
    assert (result.get("route") or {}).get("preset") == "custom"


def test_run_status_reads_back_a_real_run():
    built = json.loads(_call("dispatch_backend",
                             task="fix whatever needs fixing in this workspace"))
    _await_terminal(built["run_id"])
    status = json.loads(_call("run_status", run_id=built["run_id"]))
    assert status["run_id"] == built["run_id"]
    assert status["status"] == "passed"


def test_run_status_unknown_run_fails_loud():
    out = json.loads(_call("run_status", run_id="run_does_not_exist"))
    assert out["error"].startswith("UNKNOWN_RUN")


# ----------------------------------------------------------- agent wiring
def test_agent_has_all_tools_when_all_roles_wired():
    # The same builder-only dispatch surface must reach the deployed wrapper.
    # A separate checker tool would let the model create a redundant invalid run.
    agent = main._get_or_create_agent()
    have = set(agent.tool_names) if hasattr(agent, "tool_names") else set()
    expected = {"list_presets", "dispatch_backend", "dispatch_frontend",
                "run_build", "run_status"}
    assert expected.issubset(have), f"agent tools: {have}"
    assert not {r.dispatch_tool for r in roles.roster()
                if r.kind == roles.CHECKER} & have


def test_main_reexports_the_system_prompt():
    # main re-exports chat.SYSTEM_PROMPT for back-compat; they are the same brain.
    assert main.SYSTEM_PROMPT == chat.SYSTEM_PROMPT
    assert "clarif" in main.SYSTEM_PROMPT.lower()  # the chatbot clarifies, not guesses


# ------------------------------------------------- a GitHub event (/develop on an issue)
# The PR-review endpoint relays a trusted `/develop <idea>` comment to a NEW coordinator
# session as {"github_event": ...}. The entrypoint answers it with no model turn: the
# admitted task must be the one `./ask.sh` admits for the same idea, and the build must
# be registered with the session's async-task tracking before the response closes.
_IDEA = "a lighthouse keeper guides ships through a storm"
_REPO = "octo-cat/agent-review"


class _RecordingEngine:
    """Record what the entrypoint submits; no worker, model, or GitHub call."""

    def __init__(self, active=()):
        self.runs = [SimpleNamespace(run_id=run_id, status="running", task="other")
                     for run_id in active]

    def submit(self, task, agents=None, options=None, preset=None):
        run = SimpleNamespace(
            run_id=f"run_event_{len(self.runs)}", task=task,
            agents=agents or chat._presets.resolve(preset=preset).agents,
            options=dict(options or {}), preset=preset, status="queued")
        self.runs.append(run)
        return run

    def get(self, run_id):
        return next(run for run in self.runs if run.run_id == run_id)

    def list(self):
        return list(self.runs)


class _Activity:
    def __init__(self):
        self.calls = []

    def ensure_started(self):
        self.calls.append("ensure_started")

    def observe(self):
        self.calls.append("observe")


def _no_model():
    raise AssertionError("a GitHub event must not build or call the model agent")


class _EmptyMirror:
    """The deployed coordinator's run-state bucket, holding no saved runs."""

    def list_objects_v2(self, **_kwargs):
        return {"IsTruncated": False, "Contents": []}

    def put_object(self, **_kwargs):
        return {}


@pytest.fixture
def event_entrypoint(monkeypatch, tmp_path):
    activity = _Activity()
    monkeypatch.setattr(main, "_activity", activity)
    monkeypatch.setattr(main, "_get_or_create_agent", _no_model)
    monkeypatch.setattr(chat, "build_agent", lambda *a, **k: _no_model())
    monkeypatch.setattr(chat._engine, "_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("GITHUB_REPO", _REPO)
    # Deployed, a coordinator always has its mirror (configure_deploy sets it), and
    # the event path refuses to start a build without one.
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", "coding-agents-mirror-test")
    monkeypatch.setattr(chat._run_store, "_s3",
                        lambda: (_EmptyMirror(), "coding-agents-mirror-test"))
    return activity


def _event(**over):
    event = {"kind": "develop", "owner": "octo-cat", "repo": "agent-review",
             "issue_number": 7, "author": "octocat", "idea": _IDEA,
             "comment_url": "https://github.com/octo-cat/agent-review/issues/7#issuecomment-1"}
    event.update(over)
    return event


def _invoke(payload):
    async def collect():
        return [chunk async for chunk in main.invoke(payload)]
    chunks = asyncio.run(collect())
    assert len(chunks) == 1, chunks          # one deterministic answer, no stream of text
    return json.loads(chunks[0])


def _ask_sh_admitted_task(monkeypatch) -> str:
    """The task the model path admits for `./ask.sh "Use preset=team-game. ..."`."""
    asked = _RecordingEngine()
    monkeypatch.setattr(chat, "ENGINE", asked)
    tool = _tools_map()["run_build"]
    fn = getattr(tool, "func", None) or getattr(tool, "_tool_func", None) or tool
    with chat.bind_user_request(f"Use preset=team-game. Creative direction: {_IDEA}"):
        out = json.loads(fn(task="", preset="team-game", creative_direction=_IDEA))
    assert out["status"] == "started", out
    return asked.runs[0].task


def test_a_github_event_starts_the_team_build_with_no_model_turn(event_entrypoint, monkeypatch):
    expected_task = _ask_sh_admitted_task(monkeypatch)
    engine = _RecordingEngine()
    monkeypatch.setattr(chat, "ENGINE", engine)
    out = _invoke({"github_event": _event()})
    assert out["status"] == "started" and out["preset"] == "team-game"
    [run] = engine.runs
    assert out["run_id"] == run.run_id and out["agents"] == list(run.agents)
    assert run.task == expected_task
    assert run.options["origin"]["issue_number"] == 7
    assert run.options["origin"]["author"] == "octocat"
    assert event_entrypoint.calls == ["ensure_started", "observe"]


@pytest.mark.parametrize("payload, code", [
    ({"github_event": _event(repo="someone-elses-repo")}, "REPOSITORY_MISMATCH"),
    ({"github_event": _event(idea="<your idea, in one sentence>")}, "IDEA_PLACEHOLDER"),
    ({"github_event": _event(idea="")}, "IDEA_MISSING"),
    ({"github_event": _event(kind="fix")}, "NOT_DEVELOP"),
    ({"github_event": None}, "EVENT_INVALID"),
])
def test_a_refused_github_event_starts_nothing(event_entrypoint, monkeypatch, payload, code):
    engine = _RecordingEngine()
    monkeypatch.setattr(chat, "ENGINE", engine)
    out = _invoke(payload)
    assert out["error"] == code and out["detail"], out
    assert engine.runs == []
    assert event_entrypoint.calls == ["ensure_started", "observe"]


def test_a_github_event_is_refused_while_a_build_is_running(event_entrypoint, monkeypatch):
    engine = _RecordingEngine(active=["run_000000_aaaaaaaaaaaa"])
    monkeypatch.setattr(chat, "ENGINE", engine)
    out = _invoke({"github_event": _event()})
    assert out["error"] == "BUILD_ALREADY_RUNNING", out
    assert out["running_run_id"] == "run_000000_aaaaaaaaaaaa"
    assert [r.run_id for r in engine.runs] == ["run_000000_aaaaaaaaaaaa"]


def test_a_github_event_without_the_run_state_mirror_starts_nothing(event_entrypoint,
                                                                     monkeypatch):
    # Every /develop is a NEW session with an empty engine: without the mirror it
    # cannot see a build another session started, so it must not start one.
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET")
    engine = _RecordingEngine()
    monkeypatch.setattr(chat, "ENGINE", engine)
    out = _invoke({"github_event": _event()})
    assert out["error"] == "RUN_STATE_UNAVAILABLE", out
    assert "NoRunStateMirror" in out["detail"]
    assert engine.runs == []
    assert event_entrypoint.calls == ["ensure_started", "observe"]


def test_a_prompt_payload_still_goes_to_the_model(monkeypatch):
    # The event branch is keyed on "github_event" alone; a prompt is a chat turn.
    monkeypatch.setattr(main, "_activity", _Activity())
    monkeypatch.setattr(main, "_get_or_create_agent",
                        lambda: (_ for _ in ()).throw(RuntimeError("model path reached")))
    with pytest.raises(RuntimeError, match="model path reached"):
        _invoke({"prompt": "hello"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
