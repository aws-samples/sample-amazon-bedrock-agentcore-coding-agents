"""Tests for the orchestrator agent: the brain (chat.py) and its thin AgentCore
wrapper (main.py). The Strands tools are REAL.

The orchestrator is a CHATBOT: the model converses and only dispatches an agent
when it chooses to call a dispatch_*/run_build tool. These tests prove the tool
wiring without a model call: route_task hits the real router; the dispatch tools
kick a REAL run on the engine (non-blocking) and we poll it to a terminal verdict
through the pytest gate. The engine is REAL-ONLY (it would dispatch to deployed
runtimes and fail loud without them), so the test injects the test-only
FixtureExecutor by constructor: the same seam the shipped binary never exposes
via a flag. The Strands agent loop itself (which calls Bedrock) is exercised live
in the workshop and by the console smoke tests.
"""

from __future__ import annotations

import json
import time

import pytest

# Import main first: it adds the sibling orchestrator/ to sys.path (where chat,
# engine, fixture_executor live), so the brain modules resolve afterwards.
import main  # noqa: E402  the thin AgentCore Runtime wrapper over chat
import chat  # noqa: E402  the orchestrator brain (prompt + tools + agent + stream)
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
    Without this the tool set is converse-only (route_task + run_status) and the
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
    for r in runtime_config.ROLES:
        monkeypatch.delenv(runtime_config._env_key(r), raising=False)
    runtime_config.save_runtime("claude-code", "claude_code-TESTID0001")
    runtime_config.save_runtime("opencode", "opencode-TESTID0001")
    runtime_config.save_runtime("claude-code-validator", "claude-code-validator-TESTID0001")
    main._agent = None  # drop any cached agent so it rebuilds with the wired tools


def _tools_map():
    """Current tool set as {name: tool}; rebuilt each call because it is generated
    from the wired roles at build time."""
    return {getattr(t, "tool_name", getattr(t, "__name__", "")): t for t in chat.build_tools()}


def _call(name, **kwargs):
    """Invoke a Strands @tool's underlying function regardless of wrapper shape."""
    tool = _tools_map()[name]
    fn = getattr(tool, "func", None) or getattr(tool, "_tool_func", None) or tool
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


# ----------------------------------------------------------- route_task (advisory)
def test_route_task_routes_convert_to_full_workflow():
    out = json.loads(_call("route_task", task="convert the cost analyzer module to an MCP server"))
    assert out["workflow_ref"] == "convert/sample-to-mcp-v1"
    assert set(out["agents"]) == {"claude-code", "claude-code-validator", "opencode"}


def test_route_task_routes_patch_to_backend_only():
    out = json.loads(_call("route_task", task="fix the version string"))
    assert out["workflow_ref"] == "patch/backend-v1"
    assert out["agents"] == ["claude-code"]


def test_route_task_explicit_agent_intent_wins():
    out = json.loads(_call("route_task", task="use codex to tweak the UI"))
    assert out["workflow_ref"] == "patch/frontend-v1"


# ------------------------------------------------- dispatch tools (non-blocking, real)
def test_dispatch_backend_kicks_a_real_single_role_run():
    """A dispatch_* tool starts ONE deployed agent (subagents-as-tool) for real:
    it returns IMMEDIATELY with a run id (the chatbot keeps talking), and the run
    grades through the same engine + pytest gate when we poll it."""
    out = json.loads(_call("dispatch_backend",
                           task="fix the server version string in the cost analyzer MCP server"))
    assert out["agent"] == "claude-code"
    assert out["status"] == "started"          # non-blocking: started, not a verdict
    assert out["kind"] == "backend"
    assert out["run_id"].startswith("run_")
    result = _await_terminal(out["run_id"])     # the real build + gate
    assert result["status"] == "passed"
    assert result["gate"]["passed"] is True


def test_run_build_kicks_a_real_routed_run():
    out = json.loads(_call("run_build",
                           task="fix the server version string in the cost analyzer MCP server"))
    assert out["status"] == "started"
    assert out["run_id"].startswith("run_")
    result = _await_terminal(out["run_id"])
    assert result["status"] == "passed"
    assert (result.get("route") or {}).get("workflow_ref") == "patch/backend-v1"


def test_run_status_reads_back_a_real_run():
    built = json.loads(_call("dispatch_backend",
                             task="fix the server version string in the cost analyzer MCP server"))
    _await_terminal(built["run_id"])
    status = json.loads(_call("run_status", run_id=built["run_id"]))
    assert status["run_id"] == built["run_id"]
    assert status["status"] == "passed"


def test_run_status_unknown_run_fails_loud():
    out = json.loads(_call("run_status", run_id="run_does_not_exist"))
    assert out["error"].startswith("UNKNOWN_RUN")


# ----------------------------------------------------------- agent wiring
def test_agent_has_all_tools_when_all_roles_wired():
    # With all three coding roles wired (the autouse fixture), the agent exposes
    # the full tool set: the advisory router, the three dispatch tools, the
    # composed build, and run_status.
    agent = main._get_or_create_agent()
    have = set(agent.tool_names) if hasattr(agent, "tool_names") else set()
    expected = {"route_task", "dispatch_backend", "dispatch_frontend",
                "dispatch_validator", "run_build", "run_status"}
    assert expected.issubset(have), f"agent tools: {have}"


def test_main_reexports_the_system_prompt():
    # main re-exports chat.SYSTEM_PROMPT for back-compat; they are the same brain.
    assert main.SYSTEM_PROMPT == chat.SYSTEM_PROMPT
    assert "clarif" in main.SYSTEM_PROMPT.lower()  # the chatbot clarifies, not guesses


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
