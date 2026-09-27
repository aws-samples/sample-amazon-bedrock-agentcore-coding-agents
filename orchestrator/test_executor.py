"""Tests for the execution seam (executor.py).

The shipped path is real-only: ``from_env`` builds an ``AgentCoreExecutor`` (it
dispatches a role to its deployed runtime and fails loud on a missing wired ARN),
with no local/in-process producer to select. Here we exercise AgentCoreExecutor's
ARN resolution and dispatch without a deployed runtime, plus the env-driven selection
and fail-loud paths.
"""

from __future__ import annotations

import pytest

import executor


# --------------------------------------------------------------- from_env
def test_from_env_defaults_to_agentcore_real_only(monkeypatch):
    """With WORKSHOP_EXECUTOR unset the shipped engine builds the AgentCoreExecutor;
    it dispatches to deployed role runtimes and fails loud on a missing wired ARN.
    There is no local/in-process producer to default to (real-only)."""
    monkeypatch.delenv("WORKSHOP_EXECUTOR", raising=False)
    ex = executor.from_env()
    assert isinstance(ex, executor.AgentCoreExecutor)
    assert ex.name == "agentcore"


def test_from_env_empty_string_is_also_agentcore(monkeypatch):
    monkeypatch.setenv("WORKSHOP_EXECUTOR", "")
    assert isinstance(executor.from_env(), executor.AgentCoreExecutor)


def test_from_env_agentcore(monkeypatch):
    monkeypatch.setenv("WORKSHOP_EXECUTOR", "agentcore")
    ex = executor.from_env()
    assert isinstance(ex, executor.AgentCoreExecutor)


def test_from_env_unknown_fails_loud(monkeypatch):
    """Any value other than '' / 'agentcore' is rejected; there is no 'local'
    selection on the shipped path; offline tests inject FixtureExecutor instead."""
    for bad in ("sagemaker", "local"):
        monkeypatch.setenv("WORKSHOP_EXECUTOR", bad)
        with pytest.raises(ValueError, match="UNKNOWN_EXECUTOR"):
            executor.from_env()


# --------------------------------------------------------------- AgentCoreExecutor
class _Run:
    """Minimal stand-in for a Run."""

    def __init__(self):
        self.run_id = "run_test_001"
        self.task = "convert the cost analyzer module to an MCP server"
        self.options = {"user_id": "alice"}


class _Role:
    def __init__(self):
        self.engine = ""
        self.note = ""


def test_agentcore_resolves_arn_from_mapping_then_env(monkeypatch, tmp_path):
    # Isolate runtime_config from any wired .runs/runtime.local.json AND from a
    # dev box's leftover coding-agents/<role>/runtime_config.json (the deployed-ARN
    # auto-discovery source): point both at empty tmp locations via the real env
    # seams, so unset roles resolve to None.
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    monkeypatch.setenv("WORKSHOP_CODING_AGENTS_DIR", str(tmp_path / "coding-agents"))
    monkeypatch.delenv("AGENTCORE_RUNTIME_KIRO", raising=False)
    ex = executor.AgentCoreExecutor(runtime_arns={"claude-code": "arn:from:map"})
    assert ex.runtime_arn("claude-code") == "arn:from:map"
    monkeypatch.setenv("AGENTCORE_RUNTIME_CODEX", "arn:from:env")
    assert ex.runtime_arn("codex") == "arn:from:env"
    assert ex.runtime_arn("kiro") is None


def test_agentcore_no_arn_fails_loud(monkeypatch, tmp_path):
    # Isolate BOTH runtime_config sources (Settings file + deployed auto-discovery)
    # so "claude-code" is genuinely unwired and the dispatch fails loud.
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    monkeypatch.setenv("WORKSHOP_CODING_AGENTS_DIR", str(tmp_path / "coding-agents"))
    monkeypatch.delenv("AGENTCORE_RUNTIME_CLAUDE_CODE", raising=False)
    ex = executor.AgentCoreExecutor(runtime_arns={})
    with pytest.raises(RuntimeError, match="no AgentCore runtime ARN"):
        ex.dispatch(_Run(), "claude-code", _Role(), local_dispatch=lambda r: None)


def test_agentcore_dispatch_runs_the_engine_closure():
    # The executor is the thin "where work runs" seam; when a runtime is wired it
    # runs the engine's role closure (which dispatches to the deployed Runtime over
    # the command shell via engine._runtime_cli). The executor no longer calls
    # InvokeAgentRuntime itself.
    ex = executor.AgentCoreExecutor(
        runtime_arns={"claude-code": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/cc"})
    run, role = _Run(), _Role()
    called = {}
    ex.dispatch(run, "claude-code", role, local_dispatch=lambda r: called.setdefault("role", r))
    assert called["role"] is role


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
