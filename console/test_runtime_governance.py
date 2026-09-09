"""Current Runtime terminals must be visible and stoppable from Governance."""

from __future__ import annotations

import os
import sys
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for path in (
    _HERE,
    os.path.join(_REPO, "orchestrator"),
    os.path.join(_REPO, "interactive-api"),
    os.path.join(_REPO, "metrics-api"),
):
    if path not in sys.path:
        sys.path.insert(0, path)

import server


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(server.metrics_api.metrics_lib, "_LEDGER", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(server.metrics_api.metrics_lib, "_LEDGER_CACHE", {"sig": object(), "rows": []})


def _live_session():
    return {
        "session_id": "console-live-session",
        "agent_id": "claude-code",
        "runtime_arn": (
            "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
            "runtime/claude-code"
        ),
        "alive": True,
        "user_id": "attendee@workshop.aws",
        "started_at": "2026-07-30T07:45:21Z",
        "buffer_chars": 120,
    }


def test_governance_lists_the_current_runtime_terminal(monkeypatch):
    monkeypatch.setattr(
        server.runtime_shell,
        "list_sessions",
        lambda: {"sessions": [_live_session()]},
    )
    monkeypatch.setattr(
        server.metrics_api,
        "dispatch",
        lambda *_args: (200, {"sessions": []}),
    )

    code, body = server._route_api(
        "GET",
        "/api/metrics/sessions",
        "assistant_type=claude-code&user_id=attendee%40workshop.aws",
        None,
    )

    assert code == 200
    assert body["sessions"] == [{
        "session_id": "console-live-session",
        "invocation_number": 1,
        "runtime_arn": _live_session()["runtime_arn"],
        "assistant_type": "claude-code",
        "user_id": "attendee@workshop.aws",
        "started_at": "2026-07-30T07:45:21Z",
        "issue_url": None,
        "claude_running": True,
        "state": "open",
        "source": "runtime-registry",
        "can_stop": True,
    }]


def test_governance_stop_uses_the_registered_runtime_session(monkeypatch):
    class Session:
        session_id = "console-live-session"
        runtime_arn = _live_session()["runtime_arn"]

    calls = []
    monkeypatch.setattr(
        server.runtime_shell,
        "get_session",
        lambda session_id: Session() if session_id == Session.session_id else None,
    )
    monkeypatch.setattr(
        server.metrics_api.metrics_lib,
        "_stop_runtime_session",
        lambda arn, session_id: calls.append((arn, session_id)) or {
            "mechanism": "StopRuntimeSession",
            "agent_runtime_arn": arn,
            "region": "us-west-2",
        },
    )
    monkeypatch.setattr(
        server.runtime_shell,
        "close_runtime_session",
        lambda session_id: {"ok": True, "closed": True},
    )

    code, body = server._route_api(
        "POST",
        "/api/metrics/sessions/console-live-session/stop",
        "",
        {},
    )

    assert code == 200
    assert body["stopped"] is True
    assert calls == [(Session.runtime_arn, Session.session_id)]
    assert body["audit_recorded"] is True
    event = server.metrics_api.metrics_lib.get_audit_trail()["audit"][0]
    assert event["event_id"] == body["event_id"]
    assert event["details"]["stopped"] is True


def test_live_identity_does_not_infer_authentication_from_an_email(monkeypatch):
    monkeypatch.setattr(server.runtime_shell, "list_sessions", lambda: {"sessions": [_live_session()]})
    code, body = server._route_api("GET", "/api/metrics/sessions/console-live-session/identity", "", None)
    assert code == 200
    assert body["recorded_user"] == "attendee@workshop.aws"
    assert body["auth_provider"] == "not-recorded"
    assert body["attribution_source"] == "runtime-registry"
    assert body["static_credentials_on_agent"] is None


def test_failed_stop_keeps_the_terminal_registered_and_records_failure(monkeypatch):
    class Session:
        session_id = "console-live-session"
        runtime_arn = _live_session()["runtime_arn"]

    monkeypatch.setattr(server.runtime_shell, "get_session", lambda _: Session())
    def refused(*_args):
        raise RuntimeError("AWS refused this stop")
    monkeypatch.setattr(server.metrics_api.metrics_lib, "_stop_runtime_session", refused)
    closed = []
    monkeypatch.setattr(server.runtime_shell, "close_runtime_session", lambda value: closed.append(value))
    code, body = server._route_api("POST", "/api/metrics/sessions/console-live-session/stop", "", {},
                                   {"user_id": "signed-in-subject"})
    assert code == 502
    assert body["stopped"] is False
    assert not closed
    event = server.metrics_api.metrics_lib.get_audit_trail()["audit"][0]
    assert event["details"]["stopped"] is False
    assert event["user_id"] == "signed-in-subject"
