"""Current Runtime terminals must be visible and stoppable from Governance."""

from __future__ import annotations

import os
import sys

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


def _live_session():
    return {
        "session_id": "console-live-session",
        "agent_id": "claude-code",
        "runtime_arn": (
            "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
            "runtime/claude-code"
        ),
        "alive": True,
        "opened_by": "user",
        "busy": False,
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
