"""Every role the workshop's code creates carries the workshop's permissions boundary.

The stack lets the attendee's terminal (and its own build) create or change a workshop
role only while the boundary `coding-agents-role-boundary-<region>` is attached. So the
shared role helper must attach it on create, and attach it to a role made before it
existed, before writing that role's policy. A stack without the boundary is unchanged.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coding-agents"))
import runtime_deploy  # noqa: E402

ACCOUNT = "111122223333"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/coding-agents-role-boundary-us-west-2"


class EntityAlreadyExists(Exception):
    pass


class FakeIam:
    def __init__(self, *, has_boundary_policy=True, existing=False):
        self.calls = []
        self.has_boundary_policy = has_boundary_policy
        self.existing = existing
        self.exceptions = type("E", (), {"EntityAlreadyExistsException": EntityAlreadyExists})

    def get_policy(self, PolicyArn):
        self.calls.append(("get_policy", PolicyArn))
        if not self.has_boundary_policy:
            raise RuntimeError("NoSuchEntity")
        return {"Policy": {"Arn": PolicyArn}}

    def create_role(self, **kwargs):
        self.calls.append(("create_role", kwargs))
        if self.existing:
            raise EntityAlreadyExists()
        return {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{kwargs['RoleName']}"}}

    def put_role_permissions_boundary(self, **kwargs):
        self.calls.append(("put_role_permissions_boundary", kwargs))

    def put_role_policy(self, **kwargs):
        self.calls.append(("put_role_policy", kwargs))


def _create(iam, monkeypatch, **env):
    monkeypatch.delenv("WORKSHOP_ROLE_BOUNDARY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return runtime_deploy.create_execution_role(
        iam, "agentcore-codex-us-west-2-role", {"Version": "2012-10-17", "Statement": []},
        {"Version": "2012-10-17", "Statement": []}, agent_name="codex", account_id=ACCOUNT,
        sleep=lambda _s: None, region="us-west-2")


def test_a_new_role_is_created_with_the_boundary(monkeypatch):
    iam = FakeIam()
    _create(iam, monkeypatch)
    create = next(kw for name, kw in iam.calls if name == "create_role")
    assert create["PermissionsBoundary"] == BOUNDARY


def test_a_role_from_before_the_boundary_gets_it_before_its_policy(monkeypatch):
    iam = FakeIam(existing=True)
    _create(iam, monkeypatch)
    names = [name for name, _ in iam.calls]
    assert names.index("put_role_permissions_boundary") < names.index("put_role_policy")
    put = next(kw for name, kw in iam.calls if name == "put_role_permissions_boundary")
    assert put == {"RoleName": "agentcore-codex-us-west-2-role", "PermissionsBoundary": BOUNDARY}


def test_the_stacks_build_names_the_boundary_without_a_lookup(monkeypatch):
    iam = FakeIam(has_boundary_policy=False)
    _create(iam, monkeypatch, WORKSHOP_ROLE_BOUNDARY="arn:aws:iam::111122223333:policy/explicit")
    assert not any(name == "get_policy" for name, _ in iam.calls)
    create = next(kw for name, kw in iam.calls if name == "create_role")
    assert create["PermissionsBoundary"] == "arn:aws:iam::111122223333:policy/explicit"


def test_a_stack_without_the_boundary_creates_roles_as_before(monkeypatch):
    iam = FakeIam(has_boundary_policy=False)
    _create(iam, monkeypatch)
    create = next(kw for name, kw in iam.calls if name == "create_role")
    assert "PermissionsBoundary" not in create
    assert not any(name == "put_role_permissions_boundary" for name, _ in iam.calls)


def test_every_role_deployer_passes_its_region(monkeypatch):
    for role in ("claude-code", "claude-code-validator", "codex", "kiro", "opencode"):
        source = (ROOT / "coding-agents" / role / "deploy.py").read_text()
        call = source[source.index("runtime_deploy.create_execution_role("):]
        call = call[:call.index(")\n") + 1]
        assert "region=REGION" in call, role


def test_the_endpoint_and_github_roles_carry_the_boundary():
    template = (ROOT / "coding-agents" / "pr_review" / "template.yaml").read_text()
    assert "PermissionsBoundary: !Ref RoleBoundaryArn" in template
    assert "Default: /workshop/iam/role-boundary" in template
    for script in ("deploy-gateway.sh", "deploy-runtime.sh"):
        text = (ROOT / "coding-agents" / "gateway_mcp" / script).read_text()
        assert '${BOUNDARY_ARGS[@]+"${BOUNDARY_ARGS[@]}"}' in text, script
        assert "put-role-permissions-boundary" in text, script
    assert json.dumps("/workshop/iam/role-boundary")[1:-1] in (
        ROOT / "coding-agents" / "gateway_mcp" / "config.sh").read_text()


def test_the_mount_helper_may_write_only_its_own_log_group():
    """Every session's S3 Files mount helper (efs-proxy) creates /aws/efs/utils as the
    Runtime role; without a grant each session left AccessDenied in CloudTrail."""
    statements = runtime_deploy.storage_statements(
        [f"arn:aws:s3files:us-west-2:{ACCOUNT}:file-system/fs-1"], region="us-west-2",
        account_id=ACCOUNT, bucket="coding-agents-bucket", role_id="codex")
    mount = next(s for s in statements if s.get("Sid") == "MountHelperLogs")
    assert "logs:CreateLogGroup" in mount["Action"]
    assert mount["Resource"] == [
        f"arn:aws:logs:us-west-2:{ACCOUNT}:log-group:/aws/efs/utils",
        f"arn:aws:logs:us-west-2:{ACCOUNT}:log-group:/aws/efs/utils:*",
    ]
