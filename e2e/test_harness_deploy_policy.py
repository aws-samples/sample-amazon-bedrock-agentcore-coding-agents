"""Regression: harness deploy.py IAM policies must never emit empty-string ARNs.

Every coding-agent harness (`coding-agents/<role>/deploy.py`) is designed to deploy
MOUNTLESS first: the attendee creates the S3 Files access point on Stage 1, so at
predeploy time ``INFRA_S3FILES_AP_ARN`` is empty. The S3Files IAM statement must
still be a valid policy in that state.

The concrete bug this pins (found live on a fresh event box, 2026-07-08): the
backend `claude-code/deploy.py` inlined the AP ARN straight into the statement's
``Resource`` list::

    "Resource": [
        S3FILES_AP_ARN,                                 # "" when mountless
        S3FILES_AP_ARN.rsplit("/access-point/", 1)[0],  # "" too
    ],

With no access point yet, both entries are the empty string and
``iam.put_role_policy`` rejects the whole document with
``MalformedPolicyDocument: Resource must be in ARN format or "*"``, so
``python deploy.py`` (Lab 1 backend deploy) crashes before the runtime is created.
opencode / kiro already routed the same statement through a
``_s3files_policy_resources()`` helper that returns account-scoped wildcards when
the AP is unknown; claude-code was the one harness missing it.

We assert every non-hidden harness resolves its mountless S3Files resources to
real ARNs (no empty string, each ``arn:`` or ``*``), by importing each deploy
module with a mountless (empty-AP) infra config.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_CODE_ROOT = Path(__file__).resolve().parents[1]
_CODING_AGENTS = _CODE_ROOT / "coding-agents"

# Codex is kept in the tree but hidden from the served workshop; it still ships a
# deploy.py, so include it in the invariant. Any role with a deploy.py counts.
_HARNESS_ROLES = ["claude-code", "opencode", "kiro"]
if (_CODING_AGENTS / "codex" / "deploy.py").exists():
    _HARNESS_ROLES.append("codex")


def _load_deploy_module_mountless(role: str):
    """Import ``coding-agents/<role>/deploy.py`` with a MOUNTLESS infra config.

    deploy.py reads ``../infra.config`` and ``<role>/agent.config`` at import time,
    so we seed a minimal infra.config WITHOUT ``INFRA_S3FILES_AP_ARN`` (the
    predeploy-mountless state) and an agent.config with an ECR URI, then import the
    module in isolation. We only touch pure helpers; no AWS call is made."""
    role_dir = _CODING_AGENTS / role
    deploy_py = role_dir / "deploy.py"
    assert deploy_py.exists(), f"{deploy_py} missing"

    # Seed the two dotconfigs deploy.py loads at import. Keep any real ones intact
    # by only writing when absent, and always restoring after.
    infra_path = _CODING_AGENTS / "infra.config"
    agent_path = role_dir / "agent.config"
    created = []
    if not infra_path.exists():
        infra_path.write_text(
            "INFRA_REGION=us-west-2\n"
            "INFRA_ACCOUNT_ID=123456789012\n"
            "INFRA_BUCKET=coding-agents-123456789012-us-west-2\n"
            "INFRA_VPC_ID=vpc-000\n"
            "INFRA_SUBNET_1=subnet-a\n"
            "INFRA_SUBNET_2=subnet-b\n"
            "INFRA_SECURITY_GROUP=sg-000\n"
            "INFRA_S3FILES_ROLE_ARN=arn:aws:iam::123456789012:role/agentcore-s3files-us-west-2-role\n"
            # NOTE: no INFRA_S3FILES_AP_ARN -> the mountless predeploy state.
        )
        created.append(infra_path)
    if not agent_path.exists():
        agent_path.write_text(
            f"AGENT_NAME={role.replace('-', '_')}\n"
            f"ECR_URI=123456789012.dkr.ecr.us-west-2.amazonaws.com/coding-agents-{role}:latest\n"
        )
        created.append(agent_path)

    try:
        spec = importlib.util.spec_from_file_location(
            f"_deploy_{role.replace('-', '_')}", deploy_py)
        mod = importlib.util.module_from_spec(spec)
        # deploy.py is written to run from its own dir for the config-relative reads.
        cwd = os.getcwd()
        os.chdir(role_dir)
        try:
            spec.loader.exec_module(mod)
        finally:
            os.chdir(cwd)
        return mod
    finally:
        for p in created:
            p.unlink(missing_ok=True)


@pytest.mark.parametrize("role", _HARNESS_ROLES)
def test_mountless_s3files_resources_are_valid_arns(role):
    """A mountless (empty-AP) deploy must yield only real ARNs / ``*`` resources."""
    mod = _load_deploy_module_mountless(role)
    assert hasattr(mod, "_s3files_policy_resources"), (
        f"{role}/deploy.py must route the S3Files statement through "
        "_s3files_policy_resources() so a mountless deploy never emits empty ARNs")
    # Force the mountless branch regardless of any real infra.config on disk.
    mod.S3FILES_AP_ARN = ""
    resources = mod._s3files_policy_resources()
    assert resources, f"{role}: mountless S3Files resources must be non-empty"
    for r in resources:
        assert r and isinstance(r, str), f"{role}: empty/invalid resource {r!r}"
        assert r == "*" or r.startswith("arn:"), (
            f"{role}: resource {r!r} is neither an ARN nor '*' "
            "(IAM put_role_policy would reject it as MalformedPolicyDocument)")


@pytest.mark.parametrize("role", _HARNESS_ROLES)
def test_ap_scoped_s3files_resources_when_mounted(role):
    """When the access point IS known, resources scope to that AP + its file system."""
    mod = _load_deploy_module_mountless(role)
    ap = ("arn:aws:s3files:us-west-2:123456789012:"
          "file-system/fs-abc/access-point/ap-xyz")
    mod.S3FILES_AP_ARN = ap
    resources = mod._s3files_policy_resources()
    assert ap in resources, f"{role}: the AP ARN itself must be granted when mounted"
    for r in resources:
        assert r.startswith("arn:"), f"{role}: mounted resource {r!r} must be an ARN"
