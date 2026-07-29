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
import json
import os
from pathlib import Path

_CODE_ROOT = Path(__file__).resolve().parents[1]
_CODING_AGENTS = _CODE_ROOT / "coding-agents"

# Codex is kept in the tree but hidden from the served workshop; it still ships a
# deploy.py, so include it in the invariant. Any role with a deploy.py counts.
_HARNESS_ROLES = ["claude-code", "opencode", "kiro", "claude-code-validator"]
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


def test_mountless_s3files_resources_are_valid_arns():
    """A mountless (empty-AP) deploy must yield only real ARNs / ``*`` resources."""
    for role in _HARNESS_ROLES:
        mod = _load_deploy_module_mountless(role)
        assert hasattr(mod, "_s3files_policy_resources"), (
            f"{role}/deploy.py must route the S3Files statement through "
            "_s3files_policy_resources() so a mountless deploy never emits empty "
            "ARNs")
        # Force the mountless branch regardless of any real infra.config on disk.
        mod.S3FILES_AP_ARN = ""
        resources = mod._s3files_policy_resources()
        assert resources, f"{role}: mountless S3Files resources must be non-empty"
        for resource in resources:
            assert resource and isinstance(resource, str), (
                f"{role}: empty/invalid resource {resource!r}")
            assert resource == "*" or resource.startswith("arn:"), (
                f"{role}: resource {resource!r} is neither an ARN nor '*' "
                "(IAM put_role_policy would reject it as MalformedPolicyDocument)")


def test_ap_scoped_s3files_resources_when_mounted():
    """When the access point IS known, resources scope to that AP + its file system."""
    ap = ("arn:aws:s3files:us-west-2:123456789012:"
          "file-system/fs-abc/access-point/ap-xyz")
    for role in _HARNESS_ROLES:
        mod = _load_deploy_module_mountless(role)
        mod.S3FILES_AP_ARN = ap
        resources = mod._s3files_policy_resources()
        assert ap in resources, (
            f"{role}: the AP ARN itself must be granted when mounted")
        for resource in resources:
            assert resource.startswith("arn:"), (
                f"{role}: mounted resource {resource!r} must be an ARN")


def test_corrupt_runtime_config_recovers_the_existing_runtime(tmp_path):
    """A damaged local config must reconcile by Runtime name and repair itself."""

    class ResourceNotFoundException(Exception):
        pass

    class ConflictException(Exception):
        pass

    class Exceptions:
        pass

    Exceptions.ResourceNotFoundException = ResourceNotFoundException
    Exceptions.ConflictException = ConflictException

    class Paginator:
        def __init__(self, runtime_name, runtime_id):
            self.runtime_name = runtime_name
            self.runtime_id = runtime_id

        def paginate(self):
            return [{"agentRuntimes": [{
                "agentRuntimeName": self.runtime_name,
                "agentRuntimeId": self.runtime_id,
            }]}]

    class Control:
        exceptions = Exceptions

        def __init__(self, runtime_name, runtime_id):
            self.runtime_name = runtime_name
            self.runtime_id = runtime_id
            self.updated_ids = []

        def create_agent_runtime(self, **_kwargs):
            raise ConflictException("already exists")

        def get_paginator(self, operation):
            assert operation == "list_agent_runtimes"
            return Paginator(self.runtime_name, self.runtime_id)

        def update_agent_runtime(self, **kwargs):
            self.updated_ids.append(kwargs["agentRuntimeId"])

        def get_agent_runtime(self, **kwargs):
            assert kwargs["agentRuntimeId"] == self.runtime_id
            return {"status": "READY"}

    class Session:
        def __init__(self, control):
            self.control = control

        def client(self, service, region_name=None):
            assert service == "bedrock-agentcore-control"
            assert region_name
            return self.control

    for role in _HARNESS_ROLES:
        mod = _load_deploy_module_mountless(role)
        role_dir = tmp_path / role
        role_dir.mkdir()
        config_path = role_dir / "runtime_config.json"
        config_path.write_text("export RUNTIME_ARN=not-json\n")

        runtime_id = f"{mod.AGENT_NAME}-existing"
        control = Control(mod.AGENT_NAME, runtime_id)
        mod.SCRIPT_DIR = str(role_dir)
        mod.session = Session(control)
        fake_boto3 = type("FakeBoto3", (), {})()
        fake_boto3.Session = lambda **_kwargs: Session(control)
        mod.boto3 = fake_boto3
        mod.create_execution_role = lambda: "arn:aws:iam::123456789012:role/test"

        mod.main()

        repaired = json.loads(config_path.read_text())
        assert repaired["runtime_id"] == runtime_id
        assert repaired["runtime_arn"].endswith(f"/{runtime_id}")
        assert control.updated_ids == [runtime_id]
