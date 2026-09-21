"""Only IAM propagation is retried, through the helper all role deployers use.

The real five role entry points are exercised in test_runtime_v2_deployment.py.
Here the SDK boundary deliberately fails to exercise propagation and its bound.
"""
from __future__ import annotations

import pathlib
import sys
import types

from botocore.exceptions import ClientError
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coding-agents"))
import runtime_deploy


class _FakeControl:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def create_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(runtime_deploy, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))
    return clock


def validation(message):
    return ClientError({"Error": {"Code": "ValidationException", "Message": message}},
                       "CreateAgentRuntime")


@pytest.mark.parametrize("platform", ["V1", "V2"])
def test_role_validation_failure_is_retried_until_iam_catches_up(clock, monkeypatch, platform):
    monkeypatch.setenv("WORKSHOP_RUNTIME_PLATFORM_VERSION", platform)
    denied = validation("Role validation failed for 'arn:aws:iam::1:role/x'.")
    control = _FakeControl([denied, denied, {"agentRuntimeId": "ok"}])
    assert runtime_deploy.create_runtime_with_role_retry(
        control, {"agentRuntimeName": "x"}, budget_s=240) == {"agentRuntimeId": "ok"}
    assert len(control.calls) == 3
    assert clock[0] == 40.0
    assert all(call["platformVersion"] == platform for call in control.calls)
    assert len({call["clientToken"] for call in control.calls}) == 1


def test_other_validation_errors_are_raised_immediately(clock):
    other = validation("Access denied while validating ECR URI 'x'")
    control = _FakeControl([other])
    with pytest.raises(ClientError):
        runtime_deploy.create_runtime_with_role_retry(control, {}, budget_s=240)
    assert len(control.calls) == 1
    assert clock[0] == 0


def test_the_budget_ends_the_wait_loudly(clock):
    denied = validation("Role validation failed for 'arn'")
    control = _FakeControl([denied] * 50)
    with pytest.raises(ClientError):
        runtime_deploy.create_runtime_with_role_retry(control, {}, budget_s=60)
    assert clock[0] == 60.0 and len(control.calls) <= 5


@pytest.mark.parametrize("budget", [1, 20, 60])
def test_no_create_starts_at_or_after_the_role_propagation_deadline(clock, budget):
    denied = validation("Role validation failed for the original role")
    call_times = []

    class Control:
        def create_agent_runtime(self, **kwargs):
            call_times.append(clock[0])
            # This request would be accepted if the helper incorrectly sent it
            # after the wait consumed its remaining budget.
            if clock[0] >= budget:
                return {"agentRuntimeId": "too-late"}
            raise denied

    with pytest.raises(ClientError) as caught:
        runtime_deploy.create_runtime_with_role_retry(Control(), {}, budget_s=budget)
    assert caught.value is denied, "surface the actual propagation error on expiry"
    assert call_times and all(started < budget for started in call_times)
    assert clock[0] == budget
