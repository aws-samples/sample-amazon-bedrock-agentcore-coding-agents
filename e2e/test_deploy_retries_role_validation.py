"""The role deploy scripts must survive IAM propagation, and only that.

Live on 2026-09-03 (account 397691659327) CreateAgentRuntime answered
`Role validation failed for '<role just created>'` for roles 10s and 30s old, and the
one-shot deploy died on the first answer, so the attendee's page command failed and every
natural retry failed the same way. The fix is one helper shared by all five
coding-agents/*/deploy.py: retry ONLY that answer, within a budget, and raise everything
else at once. deploy.py talks to AWS at import time, so this test lifts the helper's
source out of each file and drives it against a fake control-plane client.
"""
from __future__ import annotations

import pathlib
import re
import textwrap
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOYS = sorted((ROOT / "coding-agents").glob("*/deploy.py"))


class _ValidationException(Exception):
    pass


class _FakeControl:
    exceptions = types.SimpleNamespace(ValidationException=_ValidationException)

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def create_agent_runtime(self, **kwargs):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _helper(path: pathlib.Path):
    src = path.read_text()
    m = re.search(r"^def _create_runtime_with_role_retry\(.*?(?=^def |\Z)", src, re.S | re.M)
    assert m, f"{path} has no _create_runtime_with_role_retry"
    ns: dict = {"time": types.SimpleNamespace(monotonic=lambda: ns["_clock"][0],
                                              sleep=lambda s: ns["_clock"].__setitem__(0, ns["_clock"][0] + s))}
    ns["_clock"] = [0.0]
    exec(textwrap.dedent(m.group(0)), ns)
    return ns["_create_runtime_with_role_retry"], ns


@pytest.mark.parametrize("path", DEPLOYS, ids=[p.parent.name for p in DEPLOYS])
def test_every_deploy_script_uses_the_helper_on_its_create_call(path):
    src = path.read_text()
    assert "_create_runtime_with_role_retry(control, dict(" in src
    assert "response = control.create_agent_runtime(" not in src, "a bare create call slipped back in"


@pytest.mark.parametrize("path", DEPLOYS, ids=[p.parent.name for p in DEPLOYS])
def test_role_validation_failure_is_retried_until_iam_catches_up(path):
    fn, ns = _helper(path)
    denied = _ValidationException("An error occurred (ValidationException) when calling the CreateAgentRuntime "
                                  "operation: Role validation failed for 'arn:aws:iam::1:role/x'. Please verify ...")
    control = _FakeControl([denied, denied, {"agentRuntimeId": "ok"}])
    assert fn(control, {"agentRuntimeName": "x"}, budget_s=240) == {"agentRuntimeId": "ok"}
    assert control.calls == 3
    assert ns["_clock"][0] == 40.0, "two retries, 20s apart"


@pytest.mark.parametrize("path", DEPLOYS[:1], ids=["claude-code"])
def test_other_validation_errors_are_raised_immediately(path):
    fn, _ = _helper(path)
    other = _ValidationException("Access denied while validating ECR URI 'x'")
    control = _FakeControl([other])
    with pytest.raises(_ValidationException):
        fn(control, {}, budget_s=240)
    assert control.calls == 1


@pytest.mark.parametrize("path", DEPLOYS[:1], ids=["claude-code"])
def test_the_budget_ends_the_wait_loudly(path):
    fn, ns = _helper(path)
    denied = _ValidationException("Role validation failed for 'arn'")
    control = _FakeControl([denied] * 50)
    with pytest.raises(_ValidationException):
        fn(control, {}, budget_s=60)
    assert ns["_clock"][0] >= 60.0 and control.calls <= 5
