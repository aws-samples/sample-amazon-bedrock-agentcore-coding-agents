"""Unit checks for governance facts that do not require a local HTTP socket."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics_lib


def test_identity_reports_attribution_without_inventing_obo_or_pr_author(tmp_path, monkeypatch):
    ledger = tmp_path / "telemetry.jsonl"
    ledger.write_text(json.dumps({
        "kind": "orchestrator_run",
        "run_id": "run_identity",
        "user_id": "user-123",
        "user_email": "builder@example.com",
        "user_name": "Builder",
        "started_at": "2026-06-26T12:00:00Z",
        "iterations": 1,
        "roles": [{"agent": "claude-code", "tokens": 0,
                   "cost_usd": 0.0, "latency_ms": 10}],
    }) + "\n")
    monkeypatch.setattr(metrics_lib, "_LEDGER", str(ledger))
    metrics_lib._LEDGER_CACHE = {"sig": object(), "rows": []}

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
