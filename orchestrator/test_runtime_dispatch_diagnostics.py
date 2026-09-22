"""A bounded retry must retain why its first Runtime dispatch failed.

Exercise the real dispatch wrapper and local checkpoint writer. Runtime calls
are scripted; only the shell-injection regression executes the fixed local
terminal commands. No model, Runtime, or network connection is used.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import runtime_config  # noqa: E402
import runtime_exec  # noqa: E402


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET", raising=False)
    monkeypatch.setattr(
        runtime_config, "pick",
        lambda agent: (
            "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/offline-test",
            {},
        ),
    )
    monkeypatch.setattr(engine.llm, "resolve", lambda model: model)

    def no_network(*args, **kwargs):
        raise AssertionError("The dispatch diagnostic test must stay offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    def invoke(scripted, *, agent="claude-code", artifact_rel=None,
               execute_terminal=False):
        registered = engine.roles.get(agent)
        run = engine.Run(
            run_id="run_010000_000000000001",
            task="Exercise Runtime dispatch diagnostics",
            agents=[agent],
            roles={agent: registered.role_name},
            status="running",
            phase="agent_execution",
            _executor_name="agentcore",
        )
        run._t0 = time.monotonic()
        role = engine.RoleResult(agent, registered.role_name)
        run.progress[agent] = role
        run.work_items[agent] = engine._work_items.WorkItem.create(
            run.run_id, agent, registered.role_name, registered.capability,
            kind=registered.kind, base_branch="main", token="offline",
        )
        calls, commands = [], []
        real_term = run.term

        def terminal(agent_id, command, cwd=None):
            commands.append(command)
            return real_term(agent_id, command, cwd) if execute_terminal else ""

        monkeypatch.setattr(run, "term", terminal)
        worker = object.__new__(engine.Engine)
        worker.executor = SimpleNamespace(name="agentcore")
        worker._persist_lock = threading.Lock()
        state_path = tmp_path / "runs/state" / (run.run_id + ".json")
        during_retry = None

        def fake_runtime(**kwargs):
            nonlocal during_retry
            calls.append(kwargs)
            assert len(calls) <= len(scripted), "Unexpected additional dispatch"
            if len(calls) == 2:
                worker._persist_run(run)
                during_retry = json.loads(state_path.read_text())
            outcome = scripted[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return {
                "exit": 0,
                "artifact": "synthetic check" if artifact_rel else "",
                "transcript": "Synthetic success",
                "session_id": "00000000-0000-4000-8000-000000000002",
            }

        monkeypatch.setattr(runtime_exec, "run_in_runtime", fake_runtime)
        result = error = None
        try:
            result = worker._runtime_cli(
                run, agent, role, "Synthetic request", "synthetic-model",
                artifact_rel=artifact_rel,
            )
        except runtime_exec.RoleExecutionError as caught:
            error = caught
        worker._persist_run(run)
        return SimpleNamespace(
            run=run, result=result, error=error, calls=calls, commands=commands,
            saved=json.loads(state_path.read_text()), during_retry=during_retry,
            agent=agent, artifact_rel=artifact_rel,
        )

    return invoke


def _assert_dispatch_contract(result, expected_calls):
    assert len(result.calls) == expected_calls
    for call in result.calls:
        assert call["timeout_s"] == engine.HARNESS_ROLE_TIMEOUT_S
        assert call["agent_id"] == result.agent
        assert call["prompt"] == "Synthetic request"
        assert call["model"] == "synthetic-model"
        assert call["artifact_rel"] == result.artifact_rel
        assert call["region"] == "us-west-2"
    if expected_calls == 2:
        assert result.calls[0] == result.calls[1]


def _warning(snapshot):
    warnings = [row for row in snapshot["events"] if row["level"] == "warn"]
    assert len(warnings) == 1
    return warnings[0]["message"]


@pytest.mark.parametrize(("agent", "artifact", "detail"), [
    ("claude-code", None,
     "ROLE_EXECUTION_ERROR: CLI exited 17; transcript tail: synthetic failure"),
    ("kiro", "acceptance_check.py",
     "ROLE_EXECUTION_ERROR: acceptance_check.py is missing/empty after retries"),
])
def test_first_reason_survives_a_successful_retry(dispatch, agent, artifact, detail):
    result = dispatch(
        [runtime_exec.RoleExecutionError(detail), "success"],
        agent=agent, artifact_rel=artifact,
    )
    _assert_dispatch_contract(result, 2)
    assert result.error is None and result.result["exit"] == 0
    for snapshot in (result.during_retry, result.saved):
        assert detail in _warning(snapshot)
        assert "RoleExecutionError" in _warning(snapshot)
    assert "dispatch produced no artifact" not in _warning(result.saved)
    assert all("empty artifact on the first turn" not in cmd for cmd in result.commands)


def test_first_reason_survives_a_second_failure_without_changing_the_error(dispatch):
    first = runtime_exec.RoleExecutionError("synthetic first CLI exit 17")
    second = runtime_exec.RoleExecutionError("synthetic second transport failure")
    result = dispatch([first, second])
    _assert_dispatch_contract(result, 2)
    assert result.error is second and result.result is None
    assert str(first) in _warning(result.during_retry)
    assert str(first) in _warning(result.saved)


def test_two_failures_make_exactly_two_calls_and_reraise_the_last_error(dispatch):
    first = runtime_exec.RoleExecutionError("first failure")
    second = runtime_exec.RoleExecutionError("last failure")
    result = dispatch([first, second])
    _assert_dispatch_contract(result, 2)
    assert result.error is second and result.result is None


@pytest.mark.parametrize("after_first_failure", [False, True])
def test_quota_error_is_not_retried(dispatch, after_first_failure):
    quota = runtime_exec.ModelQuotaError("MODEL_QUOTA_EXHAUSTED: synthetic")
    scripted = ([runtime_exec.RoleExecutionError("first failure")]
                if after_first_failure else []) + [quota]
    result = dispatch(scripted)
    _assert_dispatch_contract(result, len(scripted))
    assert result.error is quota
    if after_first_failure:
        assert "first failure" in _warning(result.saved)
    else:
        assert not result.saved["events"]


def test_first_attempt_success_does_not_retry(dispatch):
    result = dispatch(["success"])
    _assert_dispatch_contract(result, 1)
    assert result.error is None and result.result["exit"] == 0
    assert not result.saved["events"]


def test_reason_is_stripped_of_terminal_controls_and_outer_whitespace(dispatch):
    detail = "\n \t\x1b[31msynthetic failure\x1b[0m\x00 \t\n"
    result = dispatch([runtime_exec.RoleExecutionError(detail), "success"])
    message = _warning(result.saved)
    assert ": synthetic failure ->" in message
    assert all(char not in message for char in ("\x1b", "\x00", "\t", "\n"))


def test_empty_reason_does_not_invent_an_artifact_failure(dispatch):
    result = dispatch([runtime_exec.RoleExecutionError(" \n\t "), "success"])
    message = _warning(result.saved)
    assert "RoleExecutionError" in message and "(no error detail)" in message
    assert "no artifact" not in message


def test_reason_uses_existing_redaction_and_display_scrubbing(dispatch, monkeypatch, tmp_path):
    clone = tmp_path / "private-clone"
    monkeypatch.setenv("WORKSHOP_REPO_ROOT", str(clone))
    secrets = [
        "ksk_" + "a" * 24, "ghp_" + "b" * 24,
        "ASIA" + "C" * 16, "sk-" + "d" * 24,
    ]
    detail = f"failed at {clone}/file.py: " + " ".join(secrets)
    result = dispatch([runtime_exec.RoleExecutionError(detail), "success"])
    for snapshot in (result.during_retry, result.saved):
        message = _warning(snapshot)
        assert str(clone) not in message and "~/private-clone/file.py" in message
        assert message.count("[redacted]") == len(secrets)
        assert all(secret not in message for secret in secrets)


def test_redaction_precedes_truncation_so_a_long_token_does_not_hide_the_reason(dispatch):
    secret = "ksk_" + "a" * (engine._EVENT_TEXT_CAP + 100)
    detail = f"before {secret} actionable explanation after the token"
    result = dispatch([runtime_exec.RoleExecutionError(detail), "success"])
    message = _warning(result.saved)
    assert "[redacted]" in message and "actionable explanation after the token" in message
    assert secret not in message and "truncated" not in message


def test_long_reason_is_capped_with_a_visible_truncation_marker(dispatch):
    detail = "synthetic cause: " + "x" * (engine._EVENT_TEXT_CAP * 3) + "omitted tail"
    result = dispatch([runtime_exec.RoleExecutionError(detail), "success"])
    message = _warning(result.saved)
    assert "synthetic cause:" in message and "(truncated)" in message
    assert "omitted tail" not in message
    assert len(message) <= engine._EVENT_TEXT_CAP + 200


def test_exception_text_never_becomes_a_shell_command(dispatch, tmp_path):
    marker = tmp_path / "dispatch-error-injected"
    detail = f"synthetic failure'; printf injected > {shlex.quote(str(marker))}; #"
    result = dispatch(
        [runtime_exec.RoleExecutionError(detail), "success"],
        execute_terminal=True,
    )
    _assert_dispatch_contract(result, 2)
    assert "dispatch-error-injected" in _warning(result.saved)
    assert not marker.exists()
    assert all("dispatch-error-injected" not in cmd for cmd in result.commands)
    assert "re-dispatching after the first failed turn" in result.commands[-1]
    assert all(row["exit"] == 0 for row in result.run.terminals["orchestrator"])
