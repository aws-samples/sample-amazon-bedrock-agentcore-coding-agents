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
