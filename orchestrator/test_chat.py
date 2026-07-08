"""Tests for the orchestrator brain (chat.py): the chatbot the console drives.

These pin the seams the console depends on WITHOUT a model call: the tool set,
the dynamic model catalog, the multimodal prompt builder (a pasted image becomes
real image content blocks, not base64 text), and the non-blocking dispatch tools
that kick a real run on an injected fixture engine. The streaming loop itself
(which calls Bedrock) is exercised live + by the console smoke tests.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat  # noqa: E402
import runtime_config  # noqa: E402
from fixture_executor import FixtureExecutor  # noqa: E402

# Share one fixture-backed engine so dispatch tools kick a pollable run (real DI).
# Re-pinned per test by the autouse fixture below: importing any module that calls
# chat.use_engine() at import time (e.g. connection_api, pulled in by the console /
# e2e suites) swaps the global chat.ENGINE for a real-executor engine, which would
# break the tests here that read chat.ENGINE. Re-pinning makes the suite order-safe.
chat.use_engine(chat._engine.Engine(executor_obj=FixtureExecutor()))


@pytest.fixture(autouse=True)
def _pin_fixture_engine():
    """Guarantee chat.ENGINE is the fixture-backed engine for every test here,
    regardless of what another test module's import-time use_engine() left behind."""
    chat.use_engine(chat._engine.Engine(executor_obj=FixtureExecutor()))


@pytest.fixture(autouse=True)
def _isolate_deployed_discovery(tmp_path, monkeypatch):
    """Point runtime_config's deployed-ARN auto-discovery at an EMPTY dir so a role
    is unwired unless a test wires it. Without this, a dev box with a leftover
    coding-agents/<role>/runtime_config.json would make the dynamic tool set see
    roles as wired and the 'only wired roles get a tool' tests would flake."""
    monkeypatch.setenv("WORKSHOP_CODING_AGENTS_DIR", str(tmp_path / "coding-agents"))


def _tools_map():
    """The current tool set as {name: tool}. Built fresh each call because the
    dispatch tools are generated from the WIRED roles (R8), so the set depends on
    runtime_config state at build time."""
    return {getattr(t, "tool_name", getattr(t, "__name__", "")): t for t in chat.build_tools()}


def _wire_all(tmp_path, monkeypatch):
    """Point runtime_config at a temp file and wire all three coding roles, so the
    full dispatch tool set is present. Real-seam isolation (the module reads the
    env var), never a monkeypatch of internals."""
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    for r in runtime_config.ROLES:
        monkeypatch.delenv(runtime_config._env_key(r), raising=False)
    runtime_config.save_runtime("claude-code", "claude_code-TESTID0001")
    runtime_config.save_runtime("opencode", "opencode-TESTID0001")
    runtime_config.save_runtime("kiro", "kiro-TESTID0001")


def _call_with(tools, name, **kwargs):
    tool = tools[name]
    fn = getattr(tool, "func", None) or getattr(tool, "_tool_func", None) or tool
    return fn(**kwargs)


def _call(name, **kwargs):
    return _call_with(_tools_map(), name, **kwargs)


# --------------------------------------------------------------- the tool set
def test_build_tools_exposes_all_tools_when_all_roles_wired(tmp_path, monkeypatch):
    _wire_all(tmp_path, monkeypatch)
    names = set(_tools_map())
    # The always-present orchestration tools when every role is wired.
    assert {"route_task", "dispatch_backend", "dispatch_frontend",
            "dispatch_validator", "run_build", "run_status"} <= names
    # The interactive-terminal tools (agent_send/read/status) are added ONLY when
    # runtime_shell is importable (the console hosts the orchestrator). They are an
    # optional, environment-dependent group: present-together or absent-together,
    # never partial. (R8 + F1.)
    interactive = {"agent_send", "agent_read", "agent_status"}
    assert interactive <= names or not (interactive & names)


def test_workspace_inspection_tools_are_always_present(tmp_path, monkeypatch):
    """The Claude-Code-style workspace toolset (read_file/list_files/
    grep_workspace/exec_command) is available with NO role wired, so the
    orchestrator can look before it leaps."""
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    for r in runtime_config.ROLES:
        monkeypatch.delenv(runtime_config._env_key(r), raising=False)
    names = set(_tools_map())
    assert {"read_file", "list_files", "grep_workspace", "exec_command"} <= names


def test_read_file_refuses_to_escape_the_workspace(tmp_path, monkeypatch):
    """read_file resolves under WORKSHOP_REPO_ROOT and refuses a ../ escape, so
    the orchestrator can't read /etc/passwd through the tool."""
    monkeypatch.setenv("WORKSHOP_REPO_ROOT", str(tmp_path))
    (tmp_path / "hello.txt").write_text("hi from the workspace", encoding="utf-8")
    out = json.loads(_call("read_file", path="../../../../etc/passwd"))
    assert "error" in out and "escape" in out["error"]
    assert _call("read_file", path="hello.txt") == "hi from the workspace"


def test_exec_command_is_screened_by_the_governance_policy(tmp_path, monkeypatch):
    """exec_command runs through the same policy.screen the engine enforces: a
    denied command returns the matched rule and never runs."""
    monkeypatch.setenv("WORKSHOP_REPO_ROOT", str(tmp_path))
    blocked = json.loads(_call("exec_command", command="rm -rf /"))
    assert blocked.get("blocked") is True and blocked.get("rule_id") == "forbid_rm_root"
    ok = json.loads(_call("exec_command", command="echo hi"))
    assert ok.get("exit") == 0 and "hi" in ok.get("stdout", "")


def test_tool_set_is_dynamic_from_wired_roles(tmp_path, monkeypatch):
    """R8: the dispatch tools are generated from Settings, not a fixed 3. An
    unwired role gets no dispatch tool; wiring one adds it."""
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    for r in runtime_config.ROLES:
        monkeypatch.delenv(runtime_config._env_key(r), raising=False)
    # Nothing wired -> converse-only: no dispatch_*, no run_build.
    names = set(_tools_map())
    assert "dispatch_backend" not in names and "dispatch_frontend" not in names
    assert "dispatch_validator" not in names and "run_build" not in names
    assert {"route_task", "run_status"} <= names
    # Wire only the backend -> exactly its dispatch tool appears (+ run_build).
    runtime_config.save_runtime("claude-code", "claude_code-TESTID0001")
    names = set(_tools_map())
    assert "dispatch_backend" in names
    assert "dispatch_frontend" not in names and "dispatch_validator" not in names
    assert "run_build" in names
    # Wire the frontend too -> its tool appears, validator still absent.
    runtime_config.save_runtime("opencode", "opencode-TESTID0001")
    names = set(_tools_map())
    assert "dispatch_frontend" in names and "dispatch_validator" not in names


def test_dispatch_tools_are_non_blocking_and_return_a_run_id(tmp_path, monkeypatch):
    """A dispatch tool kicks the run and returns immediately with status 'started';
    it does NOT block on the build (so the chatbot keeps streaming)."""
    _wire_all(tmp_path, monkeypatch)
    out = json.loads(_call("dispatch_backend", task="fix the version string"))
    assert out["status"] == "started"
    assert out["agent"] == "claude-code"
    assert out["kind"] == "backend"
    assert out["run_id"].startswith("run_")
    # the run is real + pollable on the shared engine
    assert chat.ENGINE.get(out["run_id"]) is not None


def test_route_task_is_advisory_and_starts_nothing():
    before = len(chat.ENGINE.list())
    out = json.loads(_call("route_task", task="convert the cost analyzer module to an MCP server"))
    assert out["workflow_ref"] == "convert/sample-to-mcp-v1"
    assert len(chat.ENGINE.list()) == before  # no run created


# --------------------------------------------------- the dynamic model catalog
def test_available_models_comes_from_the_real_bedrock_catalog():
    cat = chat.available_models()
    ids = {m["id"] for m in cat["models"]}
    # ids are full Bedrock ids resolved from llm.BEDROCK_MODEL_MAP, not aliases
    assert "us.anthropic.claude-sonnet-4-6" in ids
    assert all(m.get("label") for m in cat["models"])      # every entry is labelled
    assert cat["default"] == chat.DEFAULT_MODEL_ID
    assert cat["default"] in ids


# --------------------------------------------------- the dynamic opener chips
def test_suggestions_are_capped_and_registry_derived():
    """R7: at most 3 openers, each derived from the real workflow registry (not a
    hardcoded frontend list). The cap is the behavior under test; we don't pin
    exact strings (presentation) beyond that they reflect a real workflow."""
    s = chat.suggestions()["suggestions"]
    assert 1 <= len(s) <= 3
    joined = " ".join(s).lower()
    assert "mcp" in joined or "critter" in joined or "backend" in joined


def test_system_prompt_has_no_emoji_and_a_voice_section():
    """The orchestrator voice is precise + emoji-free (the lead's tone directive)."""
    import re
    assert "## Voice" in chat.SYSTEM_PROMPT
    assert not re.search(r"[\U0001F300-\U0001FAFF]", chat.SYSTEM_PROMPT)


# ------------------------------------------------- the multimodal prompt builder
def test_build_prompt_text_only_is_a_plain_string():
    assert chat._build_prompt("hello", None) == "hello"
    assert chat._build_prompt("hello", []) == "hello"


def test_build_prompt_image_becomes_real_content_blocks():
    """A pasted image is decoded into a real Strands image content block (format +
    raw bytes), NEVER base64 text smuggled into the prompt."""
    raw = b"\x89PNG\r\n\x1a\nFAKE"
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    blocks = chat._build_prompt("what is this?", [{"name": "x.png", "data": data_url}])
    assert isinstance(blocks, list)
    assert blocks[0] == {"text": "what is this?"}
    img = next(b for b in blocks if "image" in b)["image"]
    assert img["format"] == "png"
    assert img["source"]["bytes"] == raw          # real decoded bytes, not the b64 string


def test_build_prompt_text_file_is_inlined_as_text():
    blocks = chat._build_prompt("review this", [{"name": "notes.md", "text": "# hi"}])
    joined = " ".join(b.get("text", "") for b in blocks if "text" in b)
    assert "notes.md" in joined and "# hi" in joined
    assert not any("image" in b for b in blocks)


# ------------------------------------------------- identity crosses the thread
def test_stream_chat_worker_thread_keeps_the_callers_identity(monkeypatch):
    """The run must be attributed to the SIGNED-IN user even though stream_chat
    executes the agent loop (and therefore the dispatch tools, and ENGINE.submit
    inside them) on its own worker thread. connection_api sets the identity
    ContextVar on the SSE thread; stream_chat must snapshot that context and run
    the worker inside it. Live-found on the event box: without the snapshot every
    console run was attributed to the host user ('ubuntu'), so Lab 3's per-user
    cost attribution grouped every attendee under one identity.

    Exercises the REAL stream_chat (its thread bridge) with a stub agent whose
    stream_async records the identity visible on the worker thread."""
    from identity_baggage import UserIdentity, get_current_identity, set_current_identity

    seen: dict = {}

    class _StubHooks:
        def add_callback(self, *_a, **_k):
            pass

    class _StubAgent:
        hooks = _StubHooks()
        messages: list = []

        async def stream_async(self, _prompt):
            # This runs on stream_chat's worker thread: what ENGINE.submit sees.
            seen["identity"] = get_current_identity().to_dict()
            yield {"data": "ok"}

    monkeypatch.setattr(chat, "build_agent", lambda **_kw: _StubAgent())
    set_current_identity(UserIdentity.from_dict(
        {"user_id": "attendee@workshop.aws", "user_email": "attendee@workshop.aws"}))
    try:
        events = list(chat.stream_chat("hello"))
    finally:
        set_current_identity(UserIdentity.from_dict({}))
    assert any(ev.get("type") == "text" for ev in events)
    assert seen["identity"].get("user_id") == "attendee@workshop.aws"


def test_stream_chat_emits_keepalives_while_the_model_is_silent(monkeypatch):
    """A model can think for longer than the transport chain's idle timeout
    (CloudFront cuts a silent origin response at 30s by default) without
    emitting a single delta. The stream must keep bytes flowing: a typed
    keepalive event whenever the queue is idle past the ping interval."""
    class _StubHooks:
        def add_callback(self, *_a, **_k):
            pass

    class _SlowAgent:
        hooks = _StubHooks()
        messages: list = []

        async def stream_async(self, _prompt):
            import asyncio
            await asyncio.sleep(0.35)  # silent "thinking" longer than the ping
            yield {"data": "answer"}

    monkeypatch.setenv("WORKSHOP_CHAT_KEEPALIVE_S", "0.1")
    monkeypatch.setattr(chat, "build_agent", lambda **_kw: _SlowAgent())
    events = list(chat.stream_chat("hello"))
    kinds = [ev.get("type") for ev in events]
    assert kinds.count("keepalive") >= 2       # pinged through the silence
    assert "text" in kinds                     # the real delta still arrived
    assert kinds[-1] == "done"
    # keepalives stop once the turn ends: no ping after the final done
    assert kinds.index("text") < len(kinds) - 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
