"""Exercise deployment at the SDK boundary without AWS or model calls."""
from __future__ import annotations

import ast
import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.exceptions import ClientError, ReadTimeoutError
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coding-agents"))
import runtime_deploy as deploy

ACCOUNT = "111122223333"
REGION = "us-west-2"
ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/runtime-kept"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/runtime"
AP = f"arn:aws:s3files:{REGION}:{ACCOUNT}:file-system/fs-one/access-point/ap-one"
ROLES = ("claude-code", "codex", "kiro", "claude-code-validator", "opencode")


def error(code, message="denied"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "AgentRuntime")


def runtime(**overrides):
    return {
        "agentRuntimeId": "runtime-kept", "agentRuntimeArn": ARN,
        "agentRuntimeName": "agent", "agentRuntimeVersion": "3",
        "createdAt": 100, "lastUpdatedAt": 100,
        "platformVersion": "V1", "status": "READY", "roleArn": ROLE,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "registry/agent@sha256:old"}},
        "networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
            "subnets": ["subnet-one", "subnet-two"], "securityGroups": ["sg-one"],
            "requireServiceS3Endpoint": False}},
        "filesystemConfigurations": [{"s3FilesAccessPoint": {
            "accessPointArn": AP, "mountPath": "/mnt/s3files"}}],
        "environmentVariables": {"EXISTING": "kept", "WORKSHOP_CLAUDE_EFFORT": ""},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User"]},
        "authorizerConfiguration": {"customJWTAuthorizer": {
            "discoveryUrl": "https://identity.example/.well-known/openid-configuration",
            "allowedClients": ["client"]}},
        "metadataConfiguration": {"requireMMDSV2": True},
        **overrides,
    }


class Control:
    """Stateful boundary stub; every submitted request is retained for assertions."""

    def __init__(self, current=None):
        self.current = copy.deepcopy(current)
        self.creates, self.updates, self.gets = [], [], []
        self.name_conflict = False

    def get_agent_runtime(self, **kwargs):
        self.gets.append(kwargs)
        if self.current is None or kwargs["agentRuntimeId"] != self.current["agentRuntimeId"]:
            raise error("ResourceNotFoundException")
        return copy.deepcopy(self.current)

    def create_agent_runtime(self, **kwargs):
        self.creates.append(copy.deepcopy(kwargs))
        if self.name_conflict:
            raise error("ConflictException", "name exists")
        self.current = runtime(**kwargs, agentRuntimeVersion="1")
        return {key: self.current[key] for key in (
            "agentRuntimeId", "agentRuntimeArn", "agentRuntimeVersion", "status", "createdAt")}

    def update_agent_runtime(self, **kwargs):
        self.updates.append(copy.deepcopy(kwargs))
        revision = str(int(self.current["agentRuntimeVersion"]) + 1)
        self.current.update(copy.deepcopy(kwargs))
        self.current["agentRuntimeVersion"] = revision
        self.current["status"] = "READY"
        self.current["lastUpdatedAt"] += 1
        return {key: self.current[key] for key in (
            "agentRuntimeId", "agentRuntimeArn", "agentRuntimeVersion", "status",
            "createdAt", "lastUpdatedAt")}

    def get_paginator(self, name):
        assert name == "list_agent_runtimes"
        return SimpleNamespace(paginate=lambda: iter([
            {"agentRuntimes": []}, {"agentRuntimes": [self.current]},
        ]))


@pytest.fixture
def clock(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(deploy, "time", SimpleNamespace(
        monotonic=lambda: elapsed[0],
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    ))
    return elapsed


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installed_sdk_and_bounded_client():
    # Model loading/client configuration is local; the fake session never requests credentials.
    deploy.require_v2_sdk()
    session = Mock()
    deploy.control_client(REGION, session)
    arguments = session.client.call_args.kwargs
    assert arguments["region_name"] == REGION
    assert arguments["config"].connect_timeout == 5
    assert arguments["config"].read_timeout == 10
    assert arguments["config"].retries == {"total_max_attempts": 1}


def test_manifest_sdk_mismatch_fails_before_client_creation(monkeypatch):
    def mismatch():
        raise RuntimeError("Deploy SDK mismatch")
    monkeypatch.setattr(deploy.cli_versions, "require_deploy_sdk", mismatch)
    session = Mock()
    with pytest.raises(deploy.RuntimeDeploymentError, match="SDK mismatch"):
        deploy.control_client(REGION, session)
    session.client.assert_not_called()


def test_platform_comes_from_the_canonical_manifest(monkeypatch):
    # Reload only this module so the actual import must consume the shared value.
    # A hardcoded V2 deployment would incorrectly pass with a V1 manifest.
    monkeypatch.setattr(deploy.cli_versions, "runtime_platform_version", lambda: "V1")
    module = load_file("runtime_manifest_preflight", ROOT / "coding-agents/runtime_deploy.py")
    session = Mock()
    with pytest.raises(module.RuntimeDeploymentError, match="requires platform V2"):
        module.control_client(REGION, session)
    session.client.assert_not_called()


@pytest.mark.parametrize("operation", ["CreateAgentRuntime", "UpdateAgentRuntime", "GetAgentRuntime"])
def test_unsupported_sdk_fails_without_client_creation(monkeypatch, operation):
    import botocore.session
    shape = SimpleNamespace(members={"platformVersion": object()})
    missing = SimpleNamespace(members={})
    model = SimpleNamespace(operation_model=lambda name: SimpleNamespace(
        input_shape=missing if name == operation else shape,
        output_shape=missing if name == operation else shape,
    ))
    monkeypatch.setattr(botocore.session, "get_session", lambda: SimpleNamespace(
        get_service_model=lambda name: model))
    with pytest.raises(deploy.RuntimeDeploymentError, match="1.43.95"):
        deploy.require_v2_sdk()


def test_environment_counts_utf8_delimiters_and_does_not_print_values():
    assert deploy.validate_environment({"X": "é" * 1248}) == 2499
    assert deploy.validate_environment({"X": "a" * 2497}) == 2500
    private_value = "é" * 1249
    with pytest.raises(deploy.RuntimeDeploymentError, match="2501 bytes") as exc:
        deploy.validate_environment({"X": private_value})
    assert private_value not in str(exc.value)
    assert deploy.validate_environment({"WORKSHOP_CLAUDE_EFFORT": ""}) > 0


@pytest.mark.parametrize("environment", [
    {"": "x"}, {"OK": None}, {"OK": "bad\0value"},
    {f"K{i}": "x" for i in range(51)},
])
def test_environment_shape_is_checked(environment):
    with pytest.raises(deploy.RuntimeDeploymentError):
        deploy.validate_environment(environment)


def test_wait_accepts_real_v2_ready_immediately(clock):
    control = Mock()
    control.get_agent_runtime.return_value = runtime(platformVersion="V2")
    assert deploy.wait_ready(control, "runtime-kept", expected_version="3")["platformVersion"] == "V2"
    assert clock[0] == 0
    assert control.get_agent_runtime.call_count == 1


def test_wait_handles_several_minutes_and_ignores_old_ready_revision(clock):
    control = Mock()
    control.get_agent_runtime.side_effect = [
        runtime(agentRuntimeVersion="2"),
        *[runtime(status="UPDATING", platformVersion="V2") for _ in range(18)],
        runtime(platformVersion="V2"),
    ]
    deploy.wait_ready(control, "runtime-kept", expected_version="3")
    assert clock[0] == 190
    assert control.get_agent_runtime.call_count == 20


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "DELETING"])
def test_wait_fails_terminal_status_without_sleep(clock, status):
    control = Mock()
    control.get_agent_runtime.return_value = runtime(status=status, failureReason="snapshot startup failed")
    with pytest.raises(deploy.RuntimeDeploymentError, match=status):
        deploy.wait_ready(control, "runtime-kept", expected_version="3")
    assert clock[0] == 0


@pytest.mark.parametrize("reported_version", ["3", "4"])
def test_new_failure_is_immediate_even_if_revision_did_not_advance(clock, reported_version):
    control = Mock()
    control.get_agent_runtime.side_effect = [
        runtime(),
        runtime(status="UPDATE_FAILED", agentRuntimeVersion=reported_version,
                lastUpdatedAt=200, failureReason="new snapshot preparation failed"),
    ]
    control.update_agent_runtime.return_value = runtime(
        status="UPDATING", agentRuntimeVersion="4", lastUpdatedAt=200)
    with pytest.raises(deploy.RuntimeDeploymentError, match="UPDATE_FAILED"):
        deploy.deploy(control, {"agentRuntimeName": "agent"}, runtime_id="runtime-kept")
    assert clock[0] == 0
    assert control.get_agent_runtime.call_count == 2
    assert control.update_agent_runtime.call_count == 1


@pytest.mark.parametrize("path", ["saved-id", "recover-name"])
def test_retry_ignores_old_failed_then_waits_for_the_accepted_update(clock, path):
    old = runtime(status="UPDATE_FAILED", failureReason="previous failed image", lastUpdatedAt=100)
    pending = runtime(status="UPDATING", agentRuntimeVersion="4", platformVersion="V2",
                      lastUpdatedAt=200)
    ready = dict(pending, status="READY", lastUpdatedAt=201)
    control = Mock()
    control.get_agent_runtime.side_effect = [old, old, pending, ready]
    control.update_agent_runtime.return_value = pending
    if path == "recover-name":
        control.create_agent_runtime.side_effect = error("ConflictException", "name exists")
        control.get_paginator.return_value.paginate.return_value = [
            {"agentRuntimes": [{"agentRuntimeName": "agent", "agentRuntimeId": "runtime-kept"}]},
        ]
    result = deploy.deploy(control, {"agentRuntimeName": "agent"},
                           runtime_id="runtime-kept" if path == "saved-id" else None)
    assert result["runtime_version"] == "4" and result["platform_version"] == "V2"
    assert result["runtime_status"] == "READY"
    assert control.update_agent_runtime.call_count == 1
    assert control.get_agent_runtime.call_count == 4
    assert clock[0] == 20


def test_promotion_passes_accepted_timestamp_to_readiness_wait(clock):
    control = Mock()
    pending = runtime(status="UPDATING", agentRuntimeVersion="4", platformVersion="V2",
                      lastUpdatedAt=200)
    control.update_agent_runtime.return_value = pending
    control.get_agent_runtime.side_effect = [
        runtime(),
        runtime(status="UPDATE_FAILED", lastUpdatedAt=100),
        pending,
        dict(pending, status="READY", lastUpdatedAt=201),
    ]
    result = deploy.promote(control, "runtime-kept")
    assert result["runtime_version"] == "4" and result["platform_version"] == "V2"
    assert len(control.update_agent_runtime.call_args_list) == 1
    assert clock[0] == 20


@pytest.mark.parametrize("submitted_at", [
    200, datetime.fromtimestamp(200, tz=timezone.utc), "1970-01-01T01:03:20+01:00",
])
def test_even_matching_revision_needs_fresh_service_timestamp(clock, submitted_at):
    control = Mock()
    control.get_agent_runtime.side_effect = [
        runtime(platformVersion="V2", lastUpdatedAt=100),
        runtime(platformVersion="V2", lastUpdatedAt=datetime.fromtimestamp(201, tz=timezone.utc)),
    ]
    result = deploy.wait_ready(control, "runtime-kept", expected_version="3",
                               submitted_at=submitted_at)
    assert result["lastUpdatedAt"].timestamp() == 201
    assert clock[0] == 10


@pytest.mark.parametrize("timestamp", [None, "not-a-timestamp", float("nan")])
def test_missing_observed_timestamp_fails_clearly_instead_of_guessing_freshness(clock, timestamp):
    control = Mock()
    control.get_agent_runtime.return_value = runtime(
        platformVersion="V2", lastUpdatedAt=timestamp)
    with pytest.raises(deploy.RuntimeDeploymentError, match="timestamp"):
        deploy.wait_ready(control, "runtime-kept", expected_version="3", submitted_at=200)
    assert clock[0] == 0


def test_io_time_counts_toward_ready_deadline(clock):
    control = Mock()
    def slow_get(**kwargs):
        clock[0] += 11
        return runtime(platformVersion="V2")
    control.get_agent_runtime.side_effect = slow_get
    with pytest.raises(deploy.RuntimeDeploymentError, match="deadline"):
        deploy.wait_ready(control, "runtime-kept", expected_version="3", timeout_s=10)
    assert control.get_agent_runtime.call_count == 1


def test_wait_failure_redacts_env_and_signed_url(clock):
    control = Mock()
    control.get_agent_runtime.return_value = runtime(
        status="UPDATE_FAILED", environmentVariables={"TOKEN": "private-credential-value"},
        failureReason="private-credential-value https://image.example/file?Signature=private",
    )
    with pytest.raises(deploy.RuntimeDeploymentError) as exc:
        deploy.wait_ready(control, "runtime-kept", expected_version="3")
    assert "private-credential-value" not in str(exc.value)
    assert "Signature" not in str(exc.value)


def test_wait_timeout_retains_resource_and_cannot_extend_model_limits(clock):
    control = Mock()
    control.get_agent_runtime.return_value = runtime(status="UPDATING", platformVersion="V2")
    with pytest.raises(deploy.RuntimeDeploymentError, match="retained"):
        deploy.wait_ready(control, "runtime-kept", expected_version="3", timeout_s=23)
    assert clock[0] == 23
    assert control.get_agent_runtime.call_count == 3
    control.delete_agent_runtime.assert_not_called()
    control.update_agent_runtime.assert_not_called()


def test_wait_retries_network_errors_within_same_deadline(clock):
    control = Mock()
    control.get_agent_runtime.side_effect = ReadTimeoutError(endpoint_url="https://control.example")
    with pytest.raises(deploy.RuntimeDeploymentError, match="deadline"):
        deploy.wait_ready(control, "runtime-kept", expected_version="3", timeout_s=21)
    assert clock[0] == 21
    assert control.get_agent_runtime.call_count == 3


def test_wait_does_not_retry_access_denied(clock):
    control = Mock()
    control.get_agent_runtime.side_effect = error("AccessDeniedException")
    with pytest.raises(ClientError):
        deploy.wait_ready(control, "runtime-kept", expected_version="3")
    assert clock[0] == 0


def test_revision_three_on_v1_is_not_v2(clock):
    control = Mock()
    control.get_agent_runtime.return_value = runtime()
    with pytest.raises(deploy.RuntimeDeploymentError, match="READY on V1"):
        deploy.wait_ready(control, "runtime-kept", expected_version="3")


@pytest.mark.parametrize("value", [-1, 0, float("inf"), float("nan"), "invalid", 3601])
def test_unbounded_timeout_is_rejected(value):
    with pytest.raises(deploy.RuntimeDeploymentError, match="timeout"):
        deploy.wait_ready(Mock(), "runtime-kept", expected_version="3", timeout_s=value)


def test_promotion_preserves_all_configuration_and_does_not_recreate(clock):
    before = runtime()
    control = Control(before)
    result = deploy.promote(control, "runtime-kept", expected_arn=ARN)
    request = control.updates[0]
    for key in deploy._UPDATE_FIELDS:
        if key != "networkConfiguration":
            assert request.get(key) == before.get(key), key
    assert request["networkConfiguration"]["networkModeConfig"] == {
        "subnets": ["subnet-one", "subnet-two"], "securityGroups": ["sg-one"],
    }
    assert result["runtime_id"] == "runtime-kept"
    assert result["runtime_version"] == "4"
    assert result["platform_version"] == "V2"
    assert control.creates == []
    assert before == runtime(), "preparing a request must not mutate the original receipt"
    assert not {"createdAt", "workloadIdentityDetails", "status"} & request.keys()


def test_preserved_update_matches_the_actual_sdk_input_shape():
    import botocore.session
    from botocore.validate import validate_parameters
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    request = deploy.update_request(runtime())
    validate_parameters(request, model.operation_model("UpdateAgentRuntime").input_shape)
    public = deploy.update_request(runtime(networkConfiguration={
        "networkMode": "PUBLIC", "networkModeConfig": None}))
    assert public["networkConfiguration"] == {"networkMode": "PUBLIC"}
    validate_parameters(public, model.operation_model("UpdateAgentRuntime").input_shape)


def test_role_propagation_budget_cannot_be_unbounded():
    control = Mock()
    with pytest.raises(deploy.RuntimeDeploymentError, match="timeout"):
        deploy.create_runtime_with_role_retry(control, {}, budget_s=float("inf"))
    control.create_agent_runtime.assert_not_called()


def test_existing_v2_promotion_is_noop(clock):
    control = Control(runtime(platformVersion="V2"))
    assert deploy.promote(control, "runtime-kept")["runtime_version"] == "3"
    assert control.updates == []
    assert clock[0] == 0


@pytest.mark.parametrize("platform,status", [
    ("V2", "READY"), ("V1", "READY"), ("V2", "UPDATE_FAILED"),
])
def test_promotion_initial_read_cannot_succeed_or_update_after_deadline(clock, platform, status):
    control = Mock()

    def slow_get(**kwargs):
        clock[0] += 2
        return runtime(platformVersion=platform, status=status)

    control.get_agent_runtime.side_effect = slow_get
    with pytest.raises(deploy.RuntimeDeploymentError, match="deadline"):
        deploy.promote(control, "runtime-kept", timeout_s=1)
    assert control.get_agent_runtime.call_count == 1
    control.update_agent_runtime.assert_not_called()
    control.create_agent_runtime.assert_not_called()


def test_update_preserves_unrelated_env_and_explicit_empty_effort(clock):
    control = Control(runtime())
    result = deploy.deploy(control, {
        "agentRuntimeName": "agent",
        "environmentVariables": {"WORKSHOP_CLAUDE_MODEL": "us.anthropic.claude-opus-5",
                                 "WORKSHOP_CLAUDE_EFFORT": ""},
    }, runtime_id="runtime-kept")
    assert control.updates[0]["environmentVariables"] == {
        "EXISTING": "kept", "WORKSHOP_CLAUDE_MODEL": "us.anthropic.claude-opus-5",
        "WORKSHOP_CLAUDE_EFFORT": "",
    }
    assert result["platform_version"] == "V2"


def test_merged_environment_is_rejected_before_update(clock):
    control = Control(runtime(environmentVariables={"EXISTING": "x" * 2400}))
    with pytest.raises(deploy.RuntimeDeploymentError, match="environment"):
        deploy.deploy(control, {"agentRuntimeName": "agent", "environmentVariables": {"NEW": "y" * 150}},
                      runtime_id="runtime-kept")
    assert control.updates == []


def role_namespace(role, tmp_path, control):
    tree = ast.parse((ROOT / "coding-agents" / role / "deploy.py").read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"_runtime_environment", "deploy_runtime", "main", "_load_runtime_id"}]
    session = SimpleNamespace(client=lambda *args, **kwargs: control)
    return_namespace = {
        "os": os, "json": json, "session": session, "runtime_deploy": deploy,
        "boto3": SimpleNamespace(Session=lambda **kwargs: session),
        "REGION": REGION, "ACCOUNT_ID": ACCOUNT, "AGENT_NAME": role,
        "ECR_URI": "registry/image:pinned", "SUBNET_1": "subnet-one",
        "SUBNET_2": "subnet-two", "SECURITY_GROUP": "sg-one",
        "S3FILES_AP_ARN": AP, "MOUNT_AP_ARN": AP, "S3FILES_MOUNT_PATH": "/mnt/s3files",
        "SCRIPT_DIR": str(tmp_path), "GATEWAY_URL": "", "resolve_gateway_url": lambda: "",
        "require_deploy_prereqs": lambda: None, "create_execution_role": lambda: ROLE,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "actual-role-deploy", "exec"), return_namespace)
    return return_namespace


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("path", ["create", "existing", "recover-name"])
def test_actual_role_paths_use_v2_and_record_real_revision(role, path, clock, tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "never-store-this-value")
    current = runtime(agentRuntimeName=role) if path != "create" else None
    control = Control(current)
    control.name_conflict = path == "recover-name"
    if path == "existing":
        (tmp_path / "runtime_config.json").write_text(json.dumps({"runtime_id": "runtime-kept"}))
    namespace = role_namespace(role, tmp_path, control)
    namespace["main"]()
    written = json.loads((tmp_path / "runtime_config.json").read_text())
    submitted = control.updates[-1] if path != "create" else control.creates[-1]
    assert submitted["platformVersion"] == "V2"
    assert submitted["roleArn"] == ROLE
    assert submitted["filesystemConfigurations"] == [{"s3FilesAccessPoint": {
        "accessPointArn": AP, "mountPath": "/mnt/s3files"}}]
    assert "KIRO_API_KEY" not in submitted["environmentVariables"]
    assert written["runtime_arn"] == ARN
    assert written["runtime_id"] == "runtime-kept"
    assert written["runtime_version"] == ("1" if path == "create" else "4")
    assert written["platform_version"] == "V2"
    assert written["region"] == REGION


def test_gateway_adapter_preserves_state_and_mcp_configuration(clock, tmp_path):
    gateway = load_file("gateway_v2_adapter", ROOT / "coding-agents/gateway_mcp/deploy_runtime.py")
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"runtime_id": "runtime-kept", "gateway_id": "gateway-kept",
                                "github_app_secret_arn": "arn:secret:kept"}))
    args = SimpleNamespace(state=str(path), name="agent", image="registry/mcp:pinned", role=ROLE,
                           region=REGION, secret_arn="arn:secret:kept", network="PUBLIC",
                           protocol="MCP", idle_timeout=600, max_lifetime=3300, timeout=900)
    control = Control(runtime())
    result = gateway.deploy(args, control)
    request = control.updates[0]
    assert request["protocolConfiguration"] == {"serverProtocol": "MCP"}
    assert request["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert request["lifecycleConfiguration"] == {"idleRuntimeSessionTimeout": 600, "maxLifetime": 3300}
    assert request["environmentVariables"]["GITHUB_APP_SECRET_ARN"] == "arn:secret:kept"
    saved = json.loads(path.read_text())
    assert saved["gateway_id"] == "gateway-kept"
    assert saved["github_app_secret_arn"] == "arn:secret:kept"
    assert saved["platform_version"] == result["platform_version"] == "V2"


def project_state(tmp_path, *, account=ACCOUNT, region=REGION):
    config = tmp_path / "agentcore"
    (config / ".cli").mkdir(parents=True)
    (config / "aws-targets.json").write_text(json.dumps([
        {"name": "default", "account": account, "region": region}]))
    state = {"targets": {"default": {"resources": {
        "runtimes": {"orchestrator": {"runtimeId": "runtime-kept", "runtimeArn": ARN,
                                     "runtimeVersion": 3, "sessionId": "session-kept"}},
        "gateways": {"other": {"gatewayId": "gateway-kept"}},
    }}}}
    state_path = config / ".cli/deployed-state.json"
    state_path.write_text(json.dumps(state))
    return state_path


def test_coordinator_promotes_exact_cdk_runtime_and_keeps_cli_state(clock, tmp_path):
    module = load_file("coordinator_v2_promotion", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)
    control = Control(runtime())
    result = module.promote_project(tmp_path, control=control)
    saved = json.loads(state_path.read_text())
    resources = saved["targets"]["default"]["resources"]
    entry = resources["runtimes"]["orchestrator"]
    assert entry["runtimeVersion"] == 4
    assert entry["sessionId"] == "session-kept"
    assert "platformVersion" not in entry, "do not invent fields in the CLI schema"
    assert resources["gateways"] == {"other": {"gatewayId": "gateway-kept"}}
    receipt = json.loads((state_path.parent / "runtime-platforms.json").read_text())
    assert receipt["default"]["orchestrator"] == result
    assert len(control.updates) == 1 and control.creates == []


@pytest.mark.parametrize("stale_status", ["READY", "UPDATE_FAILED"])
def test_coordinator_ignores_initial_get_older_than_cli_accepted_revision(clock, tmp_path, stale_status):
    module = load_file("coordinator_cli_revision_floor", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)
    state = json.loads(state_path.read_text())
    state["targets"]["default"]["resources"]["runtimes"]["orchestrator"]["runtimeVersion"] = 4
    state_path.write_text(json.dumps(state))
    control = Mock()
    control.get_agent_runtime.side_effect = [
        runtime(status=stale_status, platformVersion="V2"),
        runtime(agentRuntimeVersion="4", status="UPDATING", platformVersion="V2", lastUpdatedAt=200),
        runtime(agentRuntimeVersion="4", platformVersion="V2", lastUpdatedAt=201),
    ]
    result = module.promote_project(tmp_path, control=control)
    assert result["runtime_version"] == "4" and result["runtime_status"] == "READY"
    saved = json.loads(state_path.read_text())["targets"]["default"]["resources"]["runtimes"]["orchestrator"]
    assert saved["runtimeVersion"] == 4 and saved["sessionId"] == "session-kept"
    assert control.get_agent_runtime.call_count == 3
    control.update_agent_runtime.assert_not_called()
    control.create_agent_runtime.assert_not_called()
    assert clock[0] == 10


def test_coordinator_stale_initial_revision_has_one_bounded_wait(clock, tmp_path):
    module = load_file("coordinator_stale_revision_deadline", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)
    original = state_path.read_bytes()
    control = Mock()
    control.get_agent_runtime.return_value = runtime(agentRuntimeVersion="2", platformVersion="V2")
    with pytest.raises(deploy.RuntimeDeploymentError, match="deadline"):
        module.promote_project(tmp_path, control=control, timeout_s=11)
    assert clock[0] == 11 and control.get_agent_runtime.call_count == 2
    assert state_path.read_bytes() == original
    assert not (state_path.parent / "runtime-platforms.json").exists()
    control.update_agent_runtime.assert_not_called()


@pytest.mark.parametrize("concurrent_version", [4, 5])
def test_coordinator_refuses_to_replace_concurrently_advanced_cli_revision(
        clock, tmp_path, concurrent_version):
    module = load_file("coordinator_concurrent_cli_revision", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)

    class ConcurrentDeploy(Control):
        def update_agent_runtime(self, **kwargs):
            response = super().update_agent_runtime(**kwargs)
            other = json.loads(state_path.read_text())
            entry = other["targets"]["default"]["resources"]["runtimes"]["orchestrator"]
            entry["runtimeVersion"] = concurrent_version
            entry["sessionId"] = "newer-cli-session"
            state_path.write_text(json.dumps(other))
            return response

    control = ConcurrentDeploy(runtime())
    with pytest.raises(deploy.RuntimeDeploymentError, match="state changed during promotion"):
        module.promote_project(tmp_path, control=control)
    assert len(control.updates) == 1
    saved = json.loads(state_path.read_text())["targets"]["default"]["resources"]["runtimes"]["orchestrator"]
    assert saved["runtimeVersion"] == concurrent_version and saved["sessionId"] == "newer-cli-session"
    assert not (state_path.parent / "runtime-platforms.json").exists()


@pytest.mark.parametrize("version", [None, True, 0, -1, 3.5, "latest"])
def test_coordinator_requires_valid_cli_accepted_revision_before_api_calls(tmp_path, version):
    module = load_file("coordinator_missing_cli_revision", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)
    state = json.loads(state_path.read_text())
    state["targets"]["default"]["resources"]["runtimes"]["orchestrator"]["runtimeVersion"] = version
    state_path.write_text(json.dumps(state))
    control = Mock()
    with pytest.raises(deploy.RuntimeDeploymentError, match="accepted Runtime revision"):
        module.promote_project(tmp_path, control=control)
    assert control.mock_calls == []


@pytest.mark.parametrize("status", ["CREATE_FAILED", "UPDATE_FAILED"])
@pytest.mark.parametrize("platform", ["V1", "V2"])
def test_coordinator_explicit_retry_preserves_failed_runtime_configuration(
        clock, tmp_path, status, platform):
    module = load_file("coordinator_failed_promotion_retry", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)
    before = runtime(status=status, platformVersion=platform)
    control = Control(before)

    result = module.promote_project(tmp_path, control=control)

    assert len(control.updates) == 1 and control.creates == []
    request = control.updates[0]
    for key in deploy._UPDATE_FIELDS:
        if key != "networkConfiguration":
            assert request.get(key) == before.get(key), key
    assert request["networkConfiguration"]["networkModeConfig"] == {
        "subnets": ["subnet-one", "subnet-two"], "securityGroups": ["sg-one"],
    }
    assert request["agentRuntimeId"] == before["agentRuntimeId"]
    assert request["platformVersion"] == result["platform_version"] == "V2"
    assert result["runtime_version"] == "4" and result["runtime_status"] == "READY"
    saved = json.loads(state_path.read_text())["targets"]["default"]["resources"]["runtimes"]["orchestrator"]
    assert saved["runtimeVersion"] == 4 and saved["sessionId"] == "session-kept"
    assert clock[0] == 0


def test_coordinator_failed_promotion_needs_a_separate_explicit_retry(clock, tmp_path):
    module = load_file("coordinator_two_promotion_attempts", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)
    original_state = state_path.read_bytes()
    failed = runtime(agentRuntimeVersion="4", platformVersion="V2",
                     status="UPDATE_FAILED", lastUpdatedAt=101,
                     failureReason="first snapshot startup failed")
    pending = runtime(agentRuntimeVersion="5", platformVersion="V2",
                      status="UPDATING", lastUpdatedAt=200)
    control = Mock()
    control.update_agent_runtime.side_effect = [
        dict(failed, status="UPDATING"), pending,
    ]
    control.get_agent_runtime.side_effect = [
        runtime(), failed,                 # First invocation fails honestly.
        failed, failed, pending, dict(pending, status="READY", lastUpdatedAt=201),
    ]
    with pytest.raises(deploy.RuntimeDeploymentError, match="first snapshot startup failed"):
        module.promote_project(tmp_path, control=control)
    assert control.update_agent_runtime.call_count == 1
    assert state_path.read_bytes() == original_state
    assert not (state_path.parent / "runtime-platforms.json").exists()
    assert clock[0] == 0

    result = module.promote_project(tmp_path, control=control)

    assert control.update_agent_runtime.call_count == 2
    control.create_agent_runtime.assert_not_called()
    control.delete_agent_runtime.assert_not_called()
    requests = [call.kwargs for call in control.update_agent_runtime.call_args_list]
    assert requests[0]["clientToken"] != requests[1]["clientToken"]
    assert {key: value for key, value in requests[0].items() if key != "clientToken"} == {
        key: value for key, value in requests[1].items() if key != "clientToken"
    }
    assert result["runtime_version"] == "5" and result["runtime_status"] == "READY"
    assert clock[0] == 20, "the second invocation must ignore the stale previous failure"
    saved = json.loads(state_path.read_text())["targets"]["default"]["resources"]["runtimes"]["orchestrator"]
    assert saved["runtimeVersion"] == 5 and saved["sessionId"] == "session-kept"


@pytest.mark.parametrize("initial_status", ["CREATING", "UPDATING", "UPDATE_FAILED"])
def test_coordinator_does_not_repeat_a_failure_seen_during_this_invocation(
        clock, tmp_path, initial_status):
    module = load_file("coordinator_no_automatic_retry", ROOT / "orchestrator-agent/promote_runtime.py")
    state_path = project_state(tmp_path)
    original_state = state_path.read_bytes()
    control = Mock()
    control.get_agent_runtime.side_effect = [
        runtime(status=initial_status),
        runtime(status="UPDATE_FAILED", lastUpdatedAt=200,
                failureReason="new operation failed"),
    ]
    control.update_agent_runtime.return_value = runtime(
        status="UPDATING", agentRuntimeVersion="4", lastUpdatedAt=200)
    with pytest.raises(deploy.RuntimeDeploymentError, match="new operation failed"):
        module.promote_project(tmp_path, control=control)
    assert control.update_agent_runtime.call_count == (1 if initial_status == "UPDATE_FAILED" else 0)
    control.create_agent_runtime.assert_not_called()
    control.delete_agent_runtime.assert_not_called()
    assert state_path.read_bytes() == original_state
    assert not (state_path.parent / "runtime-platforms.json").exists()
    assert clock[0] == 0


@pytest.mark.parametrize("status", ["DELETING", "DELETE_FAILED", "UNKNOWN"])
def test_coordinator_refuses_nonretryable_initial_states(clock, tmp_path, status):
    module = load_file("coordinator_nonretryable_state", ROOT / "orchestrator-agent/promote_runtime.py")
    project_state(tmp_path)
    control = Control(runtime(status=status))
    with pytest.raises(deploy.RuntimeDeploymentError, match="cannot be promoted"):
        module.promote_project(tmp_path, control=control)
    assert control.creates == [] and control.updates == []
    assert clock[0] == 0


@pytest.mark.parametrize("account,region", [("999999999999", REGION), (ACCOUNT, "us-east-1")])
def test_coordinator_refuses_cross_target_state_without_api_calls(tmp_path, account, region):
    module = load_file("coordinator_target_guard", ROOT / "orchestrator-agent/promote_runtime.py")
    project_state(tmp_path, account=account, region=region)
    control = Mock()
    with pytest.raises(deploy.RuntimeDeploymentError, match="account and region"):
        module.promote_project(tmp_path, control=control)
    assert control.mock_calls == []
