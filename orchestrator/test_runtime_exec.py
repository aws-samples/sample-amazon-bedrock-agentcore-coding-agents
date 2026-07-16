"""runtime_exec unit tests (offline): the CLI-level same-provider fallback.

The shipped path runs a role's coding-agent CLI inside its deployed runtime over
the command shell. codex's OpenAI-on-Bedrock model (gpt-5.5) can be de-registered
or have a transient backend outage; the CLI reports that as a nonzero exit with a
model-down signature in its output (it talks to the mantle endpoint itself, so
there is no HTTPError to classify). run_in_runtime then retries ONCE on the healthy
sibling model, the CLI-level analogue of llm._invoke_openai's HTTP fallback.

These tests stub the SHELL DISPATCH seam (_dispatch_once) and the artifact read,
so they run with no AWS, no runtime, no network.

    python3 -m pytest orchestrator/test_runtime_exec.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_exec  # noqa: E402

_GONE = ("stream error: ... status 404 Not Found: Engine not found "
         "for model openai.gpt-5.5")
_BACKEND = "codex: the server had an error processing your request (stream disconnected)"
_REAL_BUG = "error: AGENTS.md not found in workspace; nothing to build"


def _stub_dispatch(monkeypatch, scripted):
    """Replace the shell dispatch with a scripted (exit, transcript) per model.

    ``scripted`` maps a model id -> (exit_code, transcript_text). Records the
    sequence of models actually dispatched so the test can assert the retry."""
    calls: list[str] = []

    def fake_dispatch(runtime_arn, agent_id, prompt, run_subdir, artifact_rel,
                      model, region, on_line, timeout_s):
        calls.append(model)
        exit_code, transcript = scripted[model]
        if on_line:
            on_line(transcript)
        return {"exit": exit_code, "transcript": transcript, "session_id": "sid-" + model}

    monkeypatch.setattr(runtime_exec, "_dispatch_once", fake_dispatch)
    return calls


def _stub_artifact(monkeypatch, text="<html>ok</html>"):
    """Replace the artifact read-back so a green dispatch yields a body.

    Only the read-back path still calls ``asyncio.run`` (_dispatch_once is stubbed),
    so a fake run() that closes the (un-awaited) coroutine and returns a canned
    frame dict, plus a _slice that yields the artifact text, drives the loop offline."""
    def fake_run(coro):
        try:
            coro.close()  # the read-back builds a real coroutine; close to silence warning
        except Exception:  # noqa: BLE001
            pass
        return {"raw": "ARTIFACT", "exit": 0, "session_id": "read"}

    monkeypatch.setattr(runtime_exec.asyncio, "run", fake_run)
    monkeypatch.setattr(runtime_exec, "_slice", lambda raw, b, e: text)


def test_codex_model_gone_falls_back_to_sibling(monkeypatch):
    """gpt-5.5 de-registered (404 'Engine not found' in CLI text) -> retry once on
    the sibling, which succeeds; the run returns the sibling's artifact."""
    calls = _stub_dispatch(monkeypatch, {
        "openai.gpt-5.5": (1, _GONE),
        "openai.gpt-5.4": (0, "wrote chatbot.html"),
    })
    _stub_artifact(monkeypatch, "<html>built</html>")

    out = runtime_exec.run_in_runtime(
        runtime_arn="arn:aws:bedrock-agentcore:...:runtime/codex-xyz",
        agent_id="codex", prompt="build", run_subdir="run1",
        artifact_rel="chatbot.html", model="openai.gpt-5.5")

    assert out["exit"] == 0
    assert out["artifact"] == "<html>built</html>"
    assert calls == ["openai.gpt-5.5", "openai.gpt-5.4"]  # primary, then sibling


def test_codex_backend_outage_falls_back_to_sibling(monkeypatch):
    """A transient 5xx-style CLI failure ('server had an error / stream disconnected')
    also triggers the one-shot sibling retry."""
    calls = _stub_dispatch(monkeypatch, {
        "openai.gpt-5.5": (1, _BACKEND),
        "openai.gpt-5.4": (0, "ok"),
    })
    _stub_artifact(monkeypatch)

    out = runtime_exec.run_in_runtime(
        runtime_arn="arn:...:runtime/codex", agent_id="codex", prompt="build",
        run_subdir="run1", artifact_rel="chatbot.html", model="openai.gpt-5.5")

    assert out["exit"] == 0
    assert calls == ["openai.gpt-5.5", "openai.gpt-5.4"]


def test_codex_real_build_error_does_not_fall_back(monkeypatch):
    """A nonzero exit that is NOT a model-down signature fails loud with no retry:
    the fallback must not paper over a genuine build/config bug."""
    calls = _stub_dispatch(monkeypatch, {
        "openai.gpt-5.5": (1, _REAL_BUG),
    })
    _stub_artifact(monkeypatch)

    with pytest.raises(runtime_exec.RoleExecutionError):
        runtime_exec.run_in_runtime(
            runtime_arn="arn:...:runtime/codex", agent_id="codex", prompt="build",
            run_subdir="run1", artifact_rel="chatbot.html", model="openai.gpt-5.5")
    assert calls == ["openai.gpt-5.5"]  # no retry


def test_claude_role_does_not_fall_back(monkeypatch):
    """A non-OpenAI role (claude-code) has no sibling, so a failure never retries,
    even if the text happens to look like a backend error."""
    calls = _stub_dispatch(monkeypatch, {
        "us.anthropic.claude-opus-4-6-v1": (1, "server had an error"),
    })
    _stub_artifact(monkeypatch)

    with pytest.raises(runtime_exec.RoleExecutionError):
        runtime_exec.run_in_runtime(
            runtime_arn="arn:...:runtime/claude", agent_id="claude-code",
            prompt="build", run_subdir="run1", artifact_rel="mcp_server.py",
            model="us.anthropic.claude-opus-4-6-v1")
    assert calls == ["us.anthropic.claude-opus-4-6-v1"]


def test_fallback_disabled_fails_loud(monkeypatch):
    """With WORKSHOP_OPENAI_FALLBACK="" (sibling disabled), a model-down failure
    propagates as RoleExecutionError: the resilience is opt-outable."""
    import llm
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "")
    calls = _stub_dispatch(monkeypatch, {
        "openai.gpt-5.5": (1, _GONE),
    })
    _stub_artifact(monkeypatch)

    with pytest.raises(runtime_exec.RoleExecutionError):
        runtime_exec.run_in_runtime(
            runtime_arn="arn:...:runtime/codex", agent_id="codex", prompt="build",
            run_subdir="run1", artifact_rel="chatbot.html", model="openai.gpt-5.5")
    assert calls == ["openai.gpt-5.5"]


# --- Dispatch env contract (Lab 3 telemetry seam) ---------------------------
# _build_command assembles the env prefix for every dispatched role. These
# tests pin what ships: telemetry EMISSION is on for every role (the agent
# CLIs export to the collector sidecar at 127.0.0.1:4318), but telemetry
# IDENTITY is absent until the attendee implements to_otel_env() in Lab 3.

def _cmd_for(agent_id, monkeypatch, identity=None):
    import identity_baggage
    if identity is not None:
        identity_baggage.set_current_identity(identity)
    else:
        identity_baggage.set_current_identity(identity_baggage.ANONYMOUS)
    monkeypatch.delenv("PERUSER_ROLE_ARN", raising=False)
    return runtime_exec._build_command(
        agent_id, "do the thing", "run_test_001", "deliverable/out.md",
        "", "us-west-2", "cafe12345678")


def test_dispatch_enables_claude_code_telemetry(monkeypatch):
    cmd = _cmd_for("claude-code", monkeypatch)
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in cmd
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" in cmd
    assert "OTEL_LOGS_EXPORTER=otlp" in cmd


def test_dispatch_enables_validator_telemetry(monkeypatch):
    cmd = _cmd_for("claude-code-validator", monkeypatch)
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in cmd


def test_dispatch_gives_opencode_endpoint_and_flush(monkeypatch):
    cmd = _cmd_for("opencode", monkeypatch)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318" in cmd
    # Short-lived CLI: without an immediate flush the batch span processor
    # dies with the process and the spans never leave the container.
    assert "OTEL_BSP_SCHEDULE_DELAY=1" in cmd


def test_dispatch_carries_run_ledger_identity(monkeypatch):
    from identity_baggage import UserIdentity
    ident = UserIdentity(user_id="sub-1", email="attendee@workshop.aws")
    cmd = _cmd_for("claude-code", monkeypatch, ident)
    assert "AGENTCORE_USER_EMAIL=attendee@workshop.aws" in cmd


def test_dispatch_telemetry_identity_follows_the_seam(monkeypatch):
    # Whatever to_otel_env() returns is what the dispatched process gets.
    # Shipped state: {} -> no user.id in the resource attributes (the Lab 3
    # gap); after the attendee's fix the user stamp must appear alongside the
    # always-on run/agent correlation stamp.
    from identity_baggage import UserIdentity
    ident = UserIdentity(user_id="sub-1", email="attendee@workshop.aws")
    cmd = _cmd_for("claude-code", monkeypatch, ident)
    stamp = ident.to_otel_env().get("OTEL_RESOURCE_ATTRIBUTES")
    if stamp is None:
        assert "user.id=" not in cmd
    else:
        assert "user.id=" in cmd


def test_dispatch_always_stamps_task_correlation(monkeypatch):
    # run.id + agent.id ride every dispatch, identity or not: one Logs
    # Insights query groups a task's cost across the fleet by run.id even
    # though the CLIs cannot join a shared trace tree.
    for agent_id in ("claude-code", "claude-code-validator", "opencode"):
        cmd = _cmd_for(agent_id, monkeypatch)
        assert "run.id=run_test_001" in cmd
        assert f"agent.id={agent_id}" in cmd


def test_correlation_merges_with_identity_stamp(monkeypatch):
    # The correlation stamp must EXTEND the seam's resource attributes, never
    # clobber them: post-fix, one OTEL_RESOURCE_ATTRIBUTES value carries both.
    from identity_baggage import UserIdentity
    ident = UserIdentity(user_id="sub-1", email="attendee@workshop.aws")
    stamp = ident.to_otel_env().get("OTEL_RESOURCE_ATTRIBUTES")
    cmd = _cmd_for("claude-code", monkeypatch, ident)
    assert cmd.count("OTEL_RESOURCE_ATTRIBUTES=") == 1
    if stamp is not None:
        assert "user.id=" in cmd and "run.id=" in cmd


def test_anonymous_dispatch_never_stamps_identity(monkeypatch):
    cmd = _cmd_for("claude-code", monkeypatch)
    assert "AGENTCORE_USER_EMAIL" not in cmd
    assert "user.id=" not in cmd
