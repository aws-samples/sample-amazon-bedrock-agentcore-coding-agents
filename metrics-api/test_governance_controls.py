"""Exercise the policy preview, its real ledger record, and dispatch configuration."""

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import governance_controls
import metrics_api
import metrics_lib


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "governance.jsonl"
    monkeypatch.setattr(metrics_lib, "_LEDGER", str(ledger))
    monkeypatch.setattr(metrics_lib, "_LEDGER_CACHE", {"sig": object(), "rows": []})
    return ledger


@pytest.mark.parametrize("action,target,read_only,outcome,rule", [
    ("run_command", "git status", False, "allow", ""),
    ("run_command", "rm -rf /", False, "deny", "forbid_rm_root"),
    ("run_command", "git push --force origin main", False, "hold", "gate_force_push_main"),
    ("write_file", ".env", False, "hold", "gate_write_credentials"),
    ("write_file", ".git/config", False, "deny", "forbid_write_git_internals"),
    ("write_file", "README.md", True, "deny", "forbid_write_in_readonly_workflow"),
])
def test_evaluations_record_the_actual_decision_and_server_actor(
        action, target, read_only, outcome, rule, isolated_ledger):
    code, result = metrics_api.dispatch("POST", "/api/policies/evaluate", "", {
        "action": action, "target": target, "read_only": read_only,
    }, {"user_id": "signed-in-subject", "user_email": "attendee@example.com"})

    assert code == 200
    assert result["executed"] is False
    assert result["outcome"] == outcome
    assert result["rule_id"] == rule
    assert result["audit_recorded"] is True
    event = metrics_lib.get_audit_trail()["audit"][0]
    assert event["event_id"] == result["event_id"]
    assert event["user_id"] == "attendee@example.com"
    assert event["actor_source"] == "console-session"
    assert event["details"]["outcome"] == outcome
    assert event["details"]["target_sha256"] == hashlib.sha256(target.encode()).hexdigest()
    raw = json.loads(isolated_ledger.read_text())
    assert "target" not in raw["details"]
    assert "command" not in raw["details"]


def test_an_allowed_preview_never_executes_and_never_saves_command_secrets(tmp_path, isolated_ledger):
    sentinel = tmp_path / "must-not-exist"
    target = f"printf secret-preview-value > '{sentinel}'"
    result = governance_controls.evaluate({"action": "run_command", "target": target})
    assert result["allowed"] is True
    assert result["executed"] is False
    assert not sentinel.exists()
    assert "secret-preview-value" not in isolated_ledger.read_text()
    assert "secret-preview-value" not in json.dumps(result)
    event = metrics_lib.get_audit_trail()["audit"][0]
    assert event["user_id"] == "local-session"
    assert event["actor_source"] == "local-session"


@pytest.mark.parametrize("body", [
    None, [], "git status", {},
    {"action": "deploy", "target": "example"},
    {"target": "git status", "user_id": "spoofed"},
    {"target": "git status", "read_only": "false"},
    {"target": "git status", "read_only": 1},
    {"target": ""},
    {"target": " \t "},
    {"target": "x\0y"},
    {"target": "\ud800"},
    {"target": "한" * 700},
])
def test_invalid_requests_do_not_create_audit_events(body, isolated_ledger):
    code, result = metrics_api.dispatch("POST", "/api/policies/evaluate", "", body)
    assert code == 400
    assert "error" in result
    assert not isolated_ledger.exists()


def test_unavailable_checker_is_an_error_not_allow(monkeypatch, isolated_ledger):
    monkeypatch.setattr(metrics_lib, "_policy", None)
    code, result = metrics_api.dispatch("POST", "/api/policies/evaluate", "", {"target": "git status"})
    assert code == 503
    assert "error" in result
    assert not isolated_ledger.exists()


def test_failed_audit_write_does_not_fabricate_an_event_id(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_lib, "_LEDGER", str(tmp_path))
    result = governance_controls.evaluate({"target": "git status"})
    assert result["allowed"] is True
    assert result["executed"] is False
    assert result["audit_recorded"] is False
    assert "audit_error" in result
    assert "event_id" not in result


def test_controls_read_real_host_configuration_and_keep_the_unfinished_lab_seam():
    import github
    import reviewer
    import roles
    from identity_baggage import UserIdentity

    code, result = metrics_api.dispatch("GET", "/api/controls", "", None,
                                        {"user_id": "subject", "user_email": "learner@example.com"})
    assert code == 200
    assert result["merge_policy"] == github.merge_policy()
    assert result["limits"]["repairs_per_pr"] == reviewer.MAX_REVIEW_ROUNDS
    assert result["limits"]["gate_timeout_seconds"] == reviewer.GATE_TIMEOUT_S
    assert [r["id"] for r in result["roles"]] == [r.id for r in roles.roster()]
    expected = UserIdentity(user_id="subject", email="learner@example.com").to_otel_env()
    assert result["identity"]["telemetry_attributes"] == expected.get("OTEL_RESOURCE_ATTRIBUTES")
    assert result["identity"]["mapping_state"] == ("present" if expected else "empty")
    assert governance_controls.configuration()["identity"]["mapping_state"] == "anonymous"


def test_audit_limit_is_bounded_and_includes_the_latest_operation():
    first = governance_controls.evaluate({"target": "git status"})
    last = governance_controls.evaluate({"target": "git diff"})
    events = metrics_lib.get_audit_trail(limit=1)["audit"]
    assert len(events) == 1
    assert events[0]["event_id"] == last["event_id"]
    assert events[0]["event_id"] != first["event_id"]
