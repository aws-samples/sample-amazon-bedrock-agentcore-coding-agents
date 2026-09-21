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

Kiro is now a SERVED role (the validator), so its deploy.py is held to the served-role
invariants too, not just this one: see ``test_region_is_never_hardcoded.py``, which
requires its ``REGION_MISMATCH`` same-region guard.

We assert EVERY harness with a deploy.py resolves its mountless S3Files resources to
real ARNs (no empty string, each ``arn:`` or ``*``), by importing each deploy module
with a mountless (empty-AP) infra config. The list deliberately includes the hidden
restore paths (``claude-code-validator``, ``codex``): a restore path whose deploy
crashes is not a working restore.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil

from botocore.exceptions import ClientError

_CODE_ROOT = Path(__file__).resolve().parents[1]
_CODING_AGENTS = _CODE_ROOT / "coding-agents"

# The served roster plus the kept restore paths: any role with a deploy.py counts,
# hidden or not, because a restore path whose deploy crashes is not a restore.
_HARNESS_ROLES = ["claude-code", "opencode", "kiro", "claude-code-validator"]
if (_CODING_AGENTS / "codex" / "deploy.py").exists():
    _HARNESS_ROLES.append("codex")


def _load_deploy_module_mountless(role: str, tmp_path, monkeypatch):
    """Import ``coding-agents/<role>/deploy.py`` with a MOUNTLESS infra config.

    deploy.py reads ``../infra.config`` and ``<role>/agent.config`` at import time,
    so we seed a minimal infra.config WITHOUT ``INFRA_S3FILES_AP_ARN`` (the
    predeploy-mountless state) and an agent.config with an ECR URI, then import the
    module in isolation. Copy the actual script unchanged so its __file__-relative
    config reads use only this test's files, never a real deployment's dotconfigs.
    No AWS call is made."""
    source = _CODING_AGENTS / role / "deploy.py"
    assert source.exists(), f"{source} missing"
    config_root = tmp_path / "coding-agents"
    role_dir = config_root / role
    role_dir.mkdir(parents=True, exist_ok=True)
    deploy_py = role_dir / "deploy.py"
    shutil.copy2(source, deploy_py)
    for shared in ("runtime_deploy.py", "cli_versions.py", "cli-versions.json"):
        shutil.copy2(_CODING_AGENTS / shared, config_root / shared)

    # These valid-configuration tests are not region-mismatch tests. Pin their
    # environment to their own fixture instead of inheriting the host's region.
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    infra_path = config_root / "infra.config"
    agent_path = role_dir / "agent.config"
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
    agent_path.write_text(
        f"AGENT_NAME={role.replace('-', '_')}\n"
        f"ECR_URI=123456789012.dkr.ecr.us-west-2.amazonaws.com/coding-agents-{role}:latest\n"
    )

    spec = importlib.util.spec_from_file_location(
        f"_deploy_{role.replace('-', '_')}", deploy_py)
    mod = importlib.util.module_from_spec(spec)
    # Preserve the script's documented working directory as well as __file__.
    cwd = os.getcwd()
    os.chdir(role_dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.chdir(cwd)
    assert mod.REGION == "us-west-2"
    assert mod.ACCOUNT_ID == "123456789012"
    assert mod.S3FILES_AP_ARN == ""
    return mod


def test_mountless_s3files_resources_are_valid_arns(tmp_path, monkeypatch):
    """A mountless (empty-AP) deploy must yield only real ARNs / ``*`` resources."""
    for role in _HARNESS_ROLES:
        mod = _load_deploy_module_mountless(role, tmp_path, monkeypatch)
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


def test_ap_scoped_s3files_resources_when_mounted(tmp_path, monkeypatch):
    """When the access point IS known, resources scope to that AP + its file system."""
    ap = ("arn:aws:s3files:us-west-2:123456789012:"
          "file-system/fs-abc/access-point/ap-xyz")
    for role in _HARNESS_ROLES:
        mod = _load_deploy_module_mountless(role, tmp_path, monkeypatch)
        mod.S3FILES_AP_ARN = ap
        resources = mod._s3files_policy_resources()
        assert ap in resources, (
            f"{role}: the AP ARN itself must be granted when mounted")
        for resource in resources:
            assert resource.startswith("arn:"), (
                f"{role}: mounted resource {resource!r} must be an ARN")


def test_corrupt_runtime_config_recovers_the_existing_runtime(tmp_path, monkeypatch):
    """A damaged local config must reconcile by Runtime name and repair itself."""

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
        def __init__(self, runtime_name, runtime_id):
            self.runtime_name = runtime_name
            self.runtime_id = runtime_id
            self.updated_ids = []

        def create_agent_runtime(self, **_kwargs):
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "already exists"}},
                              "CreateAgentRuntime")

        def get_paginator(self, operation):
            assert operation == "list_agent_runtimes"
            return Paginator(self.runtime_name, self.runtime_id)

        def update_agent_runtime(self, **kwargs):
            self.updated_ids.append(kwargs["agentRuntimeId"])
            assert kwargs["platformVersion"] == "V2"
            return {
                "agentRuntimeId": self.runtime_id,
                "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{self.runtime_id}",
                "agentRuntimeVersion": "2", "status": "UPDATING",
                "createdAt": 100, "lastUpdatedAt": 200,
            }

        def get_agent_runtime(self, **kwargs):
            assert kwargs["agentRuntimeId"] == self.runtime_id
            return {
                "status": "READY", "agentRuntimeName": self.runtime_name,
                "agentRuntimeId": self.runtime_id,
                "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{self.runtime_id}",
                "agentRuntimeVersion": "2" if self.updated_ids else "1",
                "platformVersion": "V2" if self.updated_ids else "V1",
                "createdAt": 100, "lastUpdatedAt": 200 if self.updated_ids else 100,
            }

    class Session:
        def __init__(self, control):
            self.control = control

        def client(self, service, region_name=None, config=None):
            assert service == "bedrock-agentcore-control"
            assert region_name
            assert config.connect_timeout == 5 and config.read_timeout == 10
            return self.control

    for role in _HARNESS_ROLES:
        mod = _load_deploy_module_mountless(role, tmp_path, monkeypatch)
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
        assert repaired["platform_version"] == "V2"
        assert repaired["runtime_version"] == "2"
        assert control.updated_ids == [runtime_id]


def test_every_harness_cleanup_deletes_its_ecr_repository():
    """Every role's ``cleanup.py`` must delete the ECR repo its ``setup.sh`` created.

    The cleanup page promises this literally ("Each agent folder ships its own
    cleanup.py that removes its Runtime, ECR repo, and IAM role") and its verify table
    expects ECR to end up with "no agent / MCP / coordinator images". An arm64 agent
    image is several hundred MB and keeps billing after an attendee has followed the
    whole teardown, so a missing delete is a real charge on an account the attendee
    believes is clean.

    ``kiro`` shipped without it. That was invisible while kiro was hidden and the
    served validator was ``claude-code-validator`` (whose cleanup.py DOES delete its
    repo): the roster swap moved the teardown onto the one cleanup.py missing the step.
    Assert it for EVERY role with a cleanup.py, hidden ones included, so a future
    roster swap cannot re-expose the same gap.
    """
    for role in _HARNESS_ROLES:
        cleanup = _CODING_AGENTS / role / "cleanup.py"
        if not cleanup.exists():
            continue
        body = cleanup.read_text(encoding="utf-8")
        assert 'client("ecr")' in body, (
            f"coding-agents/{role}/cleanup.py never creates an ECR client, so it "
            f"cannot delete coding-agents-{role}; the image keeps billing after "
            f"teardown.")
        assert "delete_repository(" in body, (
            f"coding-agents/{role}/cleanup.py does not call ecr.delete_repository, so "
            f"the coding-agents-{role} repo (an arm64 image) survives the documented "
            f"cleanup and keeps billing.")
