"""Unit checks for governance facts that do not require a local HTTP socket."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics_lib


def _set_ledger(monkeypatch, tmp_path, row):
    ledger = tmp_path / "telemetry.jsonl"
    ledger.write_text(json.dumps(row) + "\n")
    monkeypatch.setattr(metrics_lib, "_LEDGER", str(ledger))
    monkeypatch.setattr(metrics_lib, "_LEDGER_CACHE", {"sig": object(), "rows": []})
    monkeypatch.setattr(metrics_lib, "_STOPPED", set())


def test_identity_reports_attribution_without_inventing_obo_or_pr_author(tmp_path, monkeypatch):
    _set_ledger(monkeypatch, tmp_path, {
        "kind": "orchestrator_run",
        "run_id": "run_identity",
        "user_id": "user-123",
        "user_email": "builder@example.com",
        "user_name": "Builder",
        "started_at": "2026-06-26T12:00:00Z",
        "iterations": 1,
        "roles": [{"agent": "claude-code", "tokens": 0,
                   "cost_usd": 0.0, "latency_ms": 10}],
    })

    identity = metrics_lib.get_identity("run_identity-claude-code")

    assert identity == {
        "session_id": "run_identity-claude-code",
        "recorded_user": "user-123",
        "user_email": "builder@example.com",
        "user_name": "Builder",
        "auth_provider": "cognito",
        "environment": "local",
        "attribution_source": "run-ledger",
        "github_actor": "credential-dependent",
        "static_credentials_on_agent": False,
    }
    assert "obo_user" not in identity
    assert "pr_author" not in identity


def test_stop_session_uses_the_recorded_agentcore_runtime_session(tmp_path, monkeypatch):
    arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/claude"
    _set_ledger(monkeypatch, tmp_path, {
        "kind": "orchestrator_run",
        "run_id": "run_stop",
        "user_id": "user-123",
        "started_at": "2026-06-26T12:00:00Z",
        "iterations": 1,
        "roles": [{
            "agent": "claude-code",
            "runtime_arn": arn,
            "runtime_session_id": "rex-actual-runtime-session",
            "tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 10,
        }],
    })
    called = {}

    def stop_runtime(runtime_arn, runtime_session_id):
        called["args"] = (runtime_arn, runtime_session_id)
        return {"mechanism": "StopRuntimeSession", "agent_runtime_arn": runtime_arn}

    monkeypatch.setattr(metrics_lib, "_stop_runtime_session", stop_runtime)

    result = metrics_lib.stop_session("run_stop-claude-code")

    assert result["stopped"] is True
    assert called["args"] == (arn, "rex-actual-runtime-session")


def test_stop_session_refuses_to_guess_an_agentcore_runtime_session(tmp_path, monkeypatch):
    arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/claude"
    _set_ledger(monkeypatch, tmp_path, {
        "kind": "orchestrator_run",
        "run_id": "run_missing_session",
        "user_id": "user-123",
        "started_at": "2026-06-26T12:00:00Z",
        "iterations": 1,
        "roles": [{
            "agent": "claude-code",
            "runtime_arn": arn,
            "tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 10,
        }],
    })
    called = []
    monkeypatch.setattr(
        metrics_lib,
        "_stop_runtime_session",
        lambda *args: called.append(args),
    )

    result = metrics_lib.stop_session("run_missing_session-claude-code")

    assert result["stopped"] is False
    assert result["error"] == "RUNTIME_SESSION_ID_UNAVAILABLE"
    assert called == []
