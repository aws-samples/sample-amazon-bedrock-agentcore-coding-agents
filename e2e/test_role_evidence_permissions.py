"""Keep role Runtimes out of the coordinator's durable verdicts and GitHub access.

The October 27 real-event audit wrote disposable markers under run-evidence from
Codex, Kiro and the no-key checker. These checks inspect every real deployer's
policy, including restored roles; live canaries separately exercise AWS enforcement.
"""
from fnmatch import fnmatchcase
import json
from pathlib import Path

import pytest

from e2e.test_deploy_receipts_and_explain import ROLES, load_role
import team_exchange_policy


def matching_allows(document, action, resource):
    """Find Allow statements for this action/resource, before their conditions."""
    for statement in document["Statement"]:
        actions = statement["Action"]
        resources = statement["Resource"]
        actions = [actions] if isinstance(actions, str) else actions
        resources = [resources] if isinstance(resources, str) else resources
        if (statement["Effect"] == "Allow"
                and any(fnmatchcase(action.lower(), a.lower()) for a in actions)
                and any(fnmatchcase(resource, r) for r in resources)):
            yield statement


@pytest.mark.parametrize("role", ROLES)
def test_archive_access_is_one_producer_and_read_only_coordinator_inputs(
        role, tmp_path, monkeypatch):
    module, _ = load_role(role, tmp_path, monkeypatch)
    _, _, document = module.execution_role_documents()
    own = f"arn:aws:s3:::bucket-1/runtime-exchange/roles/{role}/run/result.tar.gz"
    source = "arn:aws:s3:::bucket-1/runtime-exchange/coordinator/run/source.tar.gz"
    assert list(matching_allows(document, "s3:GetObject", source))
    creates = list(matching_allows(document, "s3:PutObject", own))
    assert len(creates) == 1
    assert creates[0]["Condition"] == {"StringEquals": {"s3:if-none-match": "*"}}
    assert not list(matching_allows(document, "s3:PutObject", source))
    for sibling in set(ROLES) - {role}:
        other = f"arn:aws:s3:::bucket-1/runtime-exchange/roles/{sibling}/run/result.tar.gz"
        assert not list(matching_allows(document, "s3:PutObject", other))
    for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                   "s3:AbortMultipartUpload"):
        for resource in (
                "arn:aws:s3:::bucket-1/orchestrator/run-evidence/run/check",
                "arn:aws:s3:::bucket-1/orchestrator/run-state/run.json",
                "arn:aws:s3:::bucket-1/agents/runtime-exchange/run/check.tar.gz",
                "arn:aws:s3:::bucket-1/unrelated.txt",
                f"arn:aws:s3:::other-bucket/runtime-exchange/roles/{role}/run/result.tar.gz"):
            assert not list(matching_allows(document, action, resource)), (
                role, action, resource)
    # Delete followed by conditional creation would defeat immutability. No role
    # can delete its own output, inspect siblings, change history, or modify ACLs.
    for action in ("s3:GetObject", "s3:DeleteObject", "s3:GetObjectVersion",
                   "s3:DeleteObjectVersion", "s3:PutObjectAcl", "s3:PutObjectTagging"):
        assert not list(matching_allows(document, action, own)), (role, action)


@pytest.mark.parametrize("role", ROLES)
def test_role_cannot_list_inputs_outputs_or_run_records(role, tmp_path, monkeypatch):
    module, _ = load_role(role, tmp_path, monkeypatch)
    _, _, document = module.execution_role_documents()
    assert not list(matching_allows(document, "s3:ListBucket", "arn:aws:s3:::bucket-1"))


def test_console_example_is_claude_specific_and_uses_the_same_policy_generator():
    path = Path(__file__).resolve().parents[1] / "coding-agents" / "team-exchange-policy.json"
    assert json.loads(path.read_text()) == team_exchange_policy.document(
        "coding-agents-*", "claude-code")


@pytest.mark.parametrize("role", ROLES)
def test_console_grant_requires_current_role_not_a_legacy_or_sibling_policy(role):
    grant = team_exchange_policy.document("bucket-1", role)
    assert team_exchange_policy.is_current(grant, role)
    sibling = next(r for r in ROLES if r != role)
    assert not team_exchange_policy.is_current(grant, sibling)
    grant["Statement"][1].pop("Condition")
    assert not team_exchange_policy.is_current(grant, role)
    grant = team_exchange_policy.document("bucket-1", role)
    grant["Statement"][1]["Action"].append("s3:DeleteObject")
    assert not team_exchange_policy.is_current(grant, role)


@pytest.mark.parametrize("role", ROLES)
def test_role_runtimes_cannot_manage_event_rules(role, tmp_path, monkeypatch):
    module, _ = load_role(role, tmp_path, monkeypatch)
    _, _, document = module.execution_role_documents()
    rule = "arn:aws:events:us-west-2:111122223333:rule/workshop-rule"
    for action in ("events:PutRule", "events:PutTargets", "events:DisableRule",
                   "events:DeleteRule", "events:RemoveTargets"):
        assert not list(matching_allows(document, action, rule)), (role, action)


@pytest.mark.parametrize("role", ("codex", "kiro", "claude-code-validator"))
def test_builders_and_checkers_without_the_pr_review_role_cannot_invoke_gateway(
        role, tmp_path, monkeypatch):
    module, _ = load_role(role, tmp_path, monkeypatch)
    _, _, document = module.execution_role_documents()
    gateway = "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/github"
    assert not list(matching_allows(document, "bedrock-agentcore:InvokeGateway", gateway))
