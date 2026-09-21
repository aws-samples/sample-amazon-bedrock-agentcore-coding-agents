"""Bootstrap prepares networking first, then attaches storage to the same Runtime.

Exercise the real deployment functions with an API boundary stub. Deferral must
not broaden the IAM resources or claim a mount in the saved connection file.
"""
import ast
import json
import os
from pathlib import Path
import sys
import types
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coding-agents"))
import runtime_deploy
ROLES = ("codex", "opencode", "kiro", "claude-code-validator")
AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-test/access-point/fsap-test"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("platform", ["V1", "V2"])
def test_deferred_create_then_attached_update_preserves_id_and_policy(
        role, tmp_path, monkeypatch, platform):
    monkeypatch.setenv("WORKSHOP_RUNTIME_PLATFORM_VERSION", platform)
    source = ast.parse((ROOT / "coding-agents" / role / "deploy.py").read_text())
    selected = [
        node for node in source.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("deploy_runtime", "main", "_s3files_policy_resources", "_runtime_environment")
    ]
    assignment = next(
        node for node in source.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MOUNT_AP_ARN" for target in node.targets)
    )
    control = mock.Mock()
    control.exceptions = types.SimpleNamespace(
        ConflictException=type("Conflict", (Exception,), {}),
        ResourceNotFoundException=type("Missing", (Exception,), {}),
    )
    runtime_id = "runtime-kept"
    runtime_arn = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/" + runtime_id
    runtime = {
        "agentRuntimeId": runtime_id, "agentRuntimeArn": runtime_arn,
        "agentRuntimeName": role, "agentRuntimeVersion": "1", "platformVersion": platform,
        "createdAt": 100, "lastUpdatedAt": 100,
        "status": "READY", "roleArn": "arn:aws:iam::111122223333:role/agentcore-test",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "registry/image:pinned"}},
    }
    control.create_agent_runtime.return_value = dict(runtime)
    control.update_agent_runtime.return_value = dict(runtime, agentRuntimeVersion="2", lastUpdatedAt=200)
    control.get_agent_runtime.side_effect = lambda **kwargs: dict(
        runtime, agentRuntimeVersion="2" if control.update_agent_runtime.called else "1",
        lastUpdatedAt=200 if control.update_agent_runtime.called else 100)
    session = mock.Mock()
    session.client.return_value = control
    connection = tmp_path / "runtime_config.json"
    namespace = {
        "os": os, "json": json, "session": session, "runtime_deploy": runtime_deploy,
        "boto3": types.SimpleNamespace(Session=lambda **kwargs: session),
        "REGION": "us-west-2", "ACCOUNT_ID": "111122223333", "AGENT_NAME": role,
        "ECR_URI": "111122223333.dkr.ecr.us-west-2.amazonaws.com/agent:pinned",
        "SUBNET_1": "subnet-one", "SUBNET_2": "subnet-two", "SECURITY_GROUP": "sg-test",
        "S3FILES_AP_ARN": AP, "S3FILES_MOUNT_PATH": "/mnt/s3files",
        "SCRIPT_DIR": str(tmp_path), "GATEWAY_URL": "",
        "resolve_gateway_url": lambda: "",
        "require_deploy_prereqs": lambda: None,
        "create_execution_role": lambda: "arn:aws:iam::111122223333:role/agentcore-test",
        "_create_runtime_with_role_retry": lambda client, arguments: client.create_agent_runtime(**arguments),
        "_load_runtime_id": lambda path: json.loads(connection.read_text())["runtime_id"] if connection.exists() else None,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "actual-deploy-functions", "exec"), namespace)
    monkeypatch.setenv("WORKSHOP_DEFER_MOUNT", "1")
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), "actual-mount-choice", "exec"), namespace)
    namespace["main"]()
    first = json.loads(connection.read_text())
    assert first["runtime_id"] == runtime_id
    assert first["s3files_access_point_arn"] == ""
    assert first["platform_version"] == platform and first["runtime_version"] == "1"
    assert control.create_agent_runtime.call_args.kwargs["platformVersion"] == platform
    assert "filesystemConfigurations" not in control.create_agent_runtime.call_args.kwargs
    assert namespace["_s3files_policy_resources"]() == [AP, AP.rsplit("/access-point/", 1)[0]]

    monkeypatch.delenv("WORKSHOP_DEFER_MOUNT")
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), "actual-mount-choice", "exec"), namespace)
    namespace["main"]()
    second = json.loads(connection.read_text())
    assert second["runtime_id"] == first["runtime_id"]
    assert second["s3files_access_point_arn"] == AP
    assert second["platform_version"] == platform and second["runtime_version"] == "2"
    updated = control.update_agent_runtime.call_args.kwargs
    assert updated["platformVersion"] == platform
    assert updated["agentRuntimeId"] == runtime_id
    assert updated["filesystemConfigurations"] == [{
        "s3FilesAccessPoint": {"accessPointArn": AP, "mountPath": "/mnt/s3files"},
    }]
    assert "WORKSHOP_DEFER_MOUNT" not in updated["environmentVariables"]
    assert control.create_agent_runtime.call_count == 1
