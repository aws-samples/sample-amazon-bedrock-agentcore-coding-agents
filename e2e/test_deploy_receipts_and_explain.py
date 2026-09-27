"""Deploy receipts and ``--explain`` previews, exercised on the real deployers.

September 25 attendees said the setup gave "no real connection to how we would set
up or do a similar process elsewhere". So every Runtime deployment now ends with a
receipt of what AWS accepted, and ``deploy-prebuilt.sh <role> --explain`` prints the
exact request before anything exists. These tests pin the two properties that make
that honest: the preview is built by the SAME code as the real request, and it
makes no AWS write at all. Every AWS client here is a recording stub.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import types
import uuid

import botocore.session
import botocore.validate
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coding-agents"))
import runtime_deploy  # noqa: E402

ROLES = ("claude-code", "codex", "kiro", "claude-code-validator", "opencode")
REGION, ACCOUNT = "us-west-2", "111122223333"
AP = f"arn:aws:s3files:{REGION}:{ACCOUNT}:file-system/fs-1/access-point/fsap-1"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/coding-agents-x:pinned"
SECRET_VALUE = "value-that-must-never-be-printed"
WRITES = ("create", "update", "put", "delete", "tag", "attach", "start", "invoke")


class ServiceError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Recorder:
    """Any method call is recorded; the named ones answer like the service."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}
        self.meta = types.SimpleNamespace(region_name=REGION)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            answer = self.answers.get(name)
            if isinstance(answer, Exception):
                raise answer
            return answer(**kwargs) if callable(answer) else answer
        return call

    def writes(self):
        return [name for name, _ in self.calls if name.startswith(WRITES)]


def runtime(runtime_id="rt-kept", version="1", **extra):
    return {
        "agentRuntimeId": runtime_id, "agentRuntimeName": extra.pop("name", "agent"),
        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{runtime_id}",
        "agentRuntimeVersion": version, "status": "READY", "platformVersion": "V1",
        "createdAt": 100, "lastUpdatedAt": 100, **extra,
    }


def empty_listing():
    return types.SimpleNamespace(paginate=lambda: [{"agentRuntimes": []}])


def load_role(role, tmp_path, monkeypatch, *, saved_runtime_id=None, argv=("deploy.py",)):
    """Load a copy of the real deploy.py beside fixture config files."""
    monkeypatch.setattr(sys, "argv", list(argv))
    coding = tmp_path / "coding-agents"
    role_dir = coding / role
    role_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "coding-agents" / role / "deploy.py", role_dir / "deploy.py")
    (coding / "infra.config").write_text(
        f"INFRA_ACCOUNT_ID={ACCOUNT}\nINFRA_REGION={REGION}\nINFRA_SUBNET_1=subnet-a\n"
        f"INFRA_SUBNET_2=subnet-b\nINFRA_SECURITY_GROUP=sg-1\nINFRA_BUCKET=bucket-1\n"
        f"INFRA_S3FILES_AP_ARN={AP}\n")
    (role_dir / "agent.config").write_text(f"ECR_URI={IMAGE}\nAGENT_NAME={role.replace('-', '_')}\n")
    if saved_runtime_id:
        (role_dir / "runtime_config.json").write_text(json.dumps({"runtime_id": saved_runtime_id}))
    for name in ("GATEWAY_URL", "WORKSHOP_DEFER_MOUNT", "ECR_URI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("WORKSHOP_CLAUDE_MODEL", SECRET_VALUE)
    monkeypatch.setenv("WORKSHOP_CODEX_MODEL", SECRET_VALUE)
    monkeypatch.setenv("WORKSHOP_KIRO_MODEL", SECRET_VALUE)
    spec = importlib.util.spec_from_file_location(f"deploy_{role}_{uuid.uuid4().hex}",
                                                  role_dir / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, role_dir


def wire(module, monkeypatch, control, iam):
    session = types.SimpleNamespace(client=lambda name, **kwargs: iam if name == "iam" else control)
    module.boto3 = types.SimpleNamespace(Session=lambda **kwargs: session)
    if hasattr(module, "session"):
        module.session = session
    monkeypatch.setattr(runtime_deploy, "control_client", lambda region, session=None: control)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)


def shape(operation):
    model = botocore.session.get_session().get_service_model(runtime_deploy.CLI_SERVICE)
    return model.operation_model(operation).input_shape


@pytest.mark.parametrize("role", ROLES)
def test_explain_prints_the_create_request_and_writes_nothing(role, tmp_path, monkeypatch, capsys):
    module, role_dir = load_role(role, tmp_path, monkeypatch, argv=("deploy.py", "--explain"))
    control = Recorder({"get_paginator": lambda *a, **k: empty_listing()})
    iam = Recorder({"get_role": ServiceError("NoSuchEntity")})
    wire(module, monkeypatch, control, iam)
    monkeypatch.setattr(runtime_deploy, "EXPLAIN", True)
    monkeypatch.setattr(runtime_deploy.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))

    module.main()
    out = capsys.readouterr().out

    assert control.writes() == [] and iam.writes() == []
    assert not (role_dir / "runtime_config.json").exists()
    assert "PREVIEW ONLY: nothing is created or changed." in out
    assert "does not exist yet; a real run creates it" in out
    assert "The CreateAgentRuntime request, field by field:" in out
    assert "Deploying" not in out and "Deployment complete!" not in out
    request_path = tmp_path / f"{role.replace('-', '_')}-create-agent-runtime.json"
    assert (f"aws bedrock-agentcore-control create-agent-runtime --region {REGION} "
            f"--cli-input-json file://{request_path}") in out
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    request = json.loads(request_path.read_text())
    botocore.validate.validate_parameters(request, shape("CreateAgentRuntime"))
    assert request["platformVersion"] == runtime_deploy.selected_platform()
    assert request["filesystemConfigurations"][0]["s3FilesAccessPoint"]["accessPointArn"] == AP
    # The screen shows environment NAMES; values stay in the private request file.
    assert SECRET_VALUE not in out
    assert "AWS_REGION" in out


@pytest.mark.parametrize("role", ROLES)
def test_explain_matches_the_request_the_real_deploy_sends(role, tmp_path, monkeypatch, capsys):
    """One builder: the preview and the real create carry identical fields."""
    module, role_dir = load_role(role, tmp_path, monkeypatch)
    listing = {"get_paginator": lambda *a, **k: empty_listing()}
    iam = Recorder({"get_role": ServiceError("NoSuchEntity")})
    wire(module, monkeypatch, Recorder(listing), iam)
    monkeypatch.setattr(runtime_deploy, "EXPLAIN", True)
    monkeypatch.setattr(runtime_deploy.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    module.main()
    preview = json.loads((tmp_path / f"{role.replace('-', '_')}-create-agent-runtime.json").read_text())

    ready = runtime(name=role.replace("-", "_"), roleArn="arn:aws:iam::1:role/r",
                    agentRuntimeArtifact={"containerConfiguration": {"containerUri": IMAGE}})
    control = Recorder({"create_agent_runtime": dict(ready), "get_agent_runtime": dict(ready),
                        **listing})
    iam = Recorder({"create_role": {"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/x"}}})
    wire(module, monkeypatch, control, iam)
    monkeypatch.setattr(runtime_deploy, "EXPLAIN", False)
    module.main()
    sent = next(kwargs for name, kwargs in control.calls if name == "create_agent_runtime")
    sent = {key: value for key, value in sent.items() if key != "clientToken"}
    role_name = f"agentcore-{role.replace('-', '_')}-{REGION}-role"
    assert preview == dict(sent, roleArn=f"arn:aws:iam::{ACCOUNT}:role/{role_name}")
    assert sent["roleArn"] == f"arn:aws:iam::{ACCOUNT}:role/x"
    out = capsys.readouterr().out
    assert "Deployment complete!" in out and "What this created" in out


def test_explain_shows_the_update_for_a_saved_runtime(tmp_path, monkeypatch, capsys):
    module, role_dir = load_role("claude-code", tmp_path, monkeypatch, saved_runtime_id="rt-kept")
    current = runtime(name="claude_code", roleArn="arn:aws:iam::1:role/r",
                      agentRuntimeArtifact={"containerConfiguration": {"containerUri": "old"}},
                      networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
                          "subnets": ["subnet-a"], "securityGroups": ["sg-1"]}},
                      environmentVariables={"KEPT": "x"})
    control = Recorder({"get_agent_runtime": dict(current)})
    iam = Recorder({"get_role": {"Role": {"Arn": "arn"}}})
    wire(module, monkeypatch, control, iam)
    monkeypatch.setattr(runtime_deploy, "EXPLAIN", True)
    monkeypatch.setattr(runtime_deploy.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))

    module.main()
    out = capsys.readouterr().out

    assert control.writes() == [] and iam.writes() == []
    assert "exists; a real run keeps its trust policy and rewrites" in out
    assert "The UpdateAgentRuntime request, field by field:" in out
    request = json.loads((tmp_path / "claude_code-update-agent-runtime.json").read_text())
    botocore.validate.validate_parameters(request, shape("UpdateAgentRuntime"))
    assert request["agentRuntimeId"] == "rt-kept"
    assert request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == IMAGE
    assert request["environmentVariables"]["KEPT"] == "x"
    assert "clientToken" not in request
    assert "get-agent-runtime --region us-west-2 --agent-runtime-id rt-kept --query status" in out


def test_explain_refuses_a_saved_id_of_a_different_runtime(tmp_path, monkeypatch, capsys):
    """The real deploy stops on a saved ID that names another Runtime, so must the preview.

    Otherwise it would print a pasteable update-agent-runtime that overwrites that Runtime.
    """
    module, _ = load_role("claude-code", tmp_path, monkeypatch, saved_runtime_id="rt-other")
    control = Recorder({"get_agent_runtime": dict(runtime(name="someone_else"))})
    wire(module, monkeypatch, control, Recorder({"get_role": {"Role": {"Arn": "arn"}}}))
    monkeypatch.setattr(runtime_deploy, "EXPLAIN", True)
    monkeypatch.setattr(runtime_deploy.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))

    with pytest.raises(runtime_deploy.RuntimeDeploymentError, match="different Runtime name"):
        module.main()
    assert control.writes() == []
    assert not list(tmp_path.glob("*-update-agent-runtime.json"))
    assert "update-agent-runtime" not in capsys.readouterr().out


def test_explain_lists_every_policy_statement_with_its_reason(tmp_path, monkeypatch, capsys):
    module, _ = load_role("claude-code", tmp_path, monkeypatch)
    wire(module, monkeypatch, Recorder({"get_paginator": lambda *a, **k: empty_listing()}),
         Recorder({"get_role": ServiceError("NoSuchEntity")}))
    monkeypatch.setattr(runtime_deploy, "EXPLAIN", True)
    monkeypatch.setattr(runtime_deploy.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    module.main()
    out = capsys.readouterr().out
    _, trust, policy = module.execution_role_documents()
    for statement in policy["Statement"]:
        line = next(text for text in out.splitlines() if text.strip().startswith(statement["Sid"] + " "))
        assert runtime_deploy._POLICY_WHY[statement["Sid"]] in line
    assert "bedrock-agentcore.amazonaws.com" in out


def test_receipt_reports_what_aws_accepted_and_never_env_values():
    accepted = runtime(
        name="claude_code", roleArn=f"arn:aws:iam::{ACCOUNT}:role/agentcore-claude_code",
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": IMAGE}},
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
            "subnets": ["subnet-a", "subnet-b"], "securityGroups": ["sg-1"]}},
        protocolConfiguration={"serverProtocol": "HTTP"},
        filesystemConfigurations=[{"s3FilesAccessPoint": {
            "accessPointArn": AP, "mountPath": "/mnt/s3files"}}],
        environmentVariables={"AWS_REGION": REGION, "WORKSHOP_CLAUDE_MODEL": SECRET_VALUE})
    record = runtime_deploy.runtime_record(accepted)
    buffer = io.StringIO()
    runtime_deploy.print_receipt(record, region=REGION, file=buffer,
                                 saved_to="coding-agents/claude-code/runtime_config.json")
    text = buffer.getvalue()
    assert "What this created: an Amazon Bedrock AgentCore Runtime" in text
    assert f"Runtime ARN:     {accepted['agentRuntimeArn']}" in text
    assert "Revision:        1, platform V1, READY" in text
    assert IMAGE in text and accepted["roleArn"] in text
    assert "VPC, 2 subnets, 1 security group" in text
    assert f"/mnt/s3files from {AP}" in text
    assert "AWS_REGION, WORKSHOP_CLAUDE_MODEL  (names only)" in text
    assert SECRET_VALUE not in text
    assert "coding-agents/claude-code/runtime_config.json" in text
    assert (f"aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id rt-kept "
            f"--region {REGION}") in text
    # The saved connection is unchanged: a plain dict of the same four or five fields.
    assert json.loads(json.dumps(record)) == {
        "runtime_id": "rt-kept", "runtime_arn": accepted["agentRuntimeArn"],
        "runtime_version": "1", "runtime_status": "READY", "platform_version": "V1"}


def test_receipt_names_a_runtime_without_a_mount():
    record = runtime_deploy.runtime_record(runtime(networkConfiguration={"networkMode": "PUBLIC"},
                                                   protocolConfiguration={"serverProtocol": "MCP"}))
    text = "\n".join(runtime_deploy.receipt_lines(record, region=REGION))
    assert "Mount:           no mount" in text
    assert "PUBLIC (no VPC)" in text and "MCP (a tool server, called through the Gateway)" in text


def test_the_read_only_commands_name_real_operations():
    model = botocore.session.get_session().get_service_model(runtime_deploy.CLI_SERVICE)
    for operation, member in (("GetAgentRuntime", "agentRuntimeId"), ("GetGateway", "gatewayIdentifier"),
                              ("ListGatewayTargets", "gatewayIdentifier")):
        assert member in model.operation_model(operation).input_shape.required_members
    for operation in ("CreateAgentRuntime", "UpdateAgentRuntime"):
        model.operation_model(operation)
    gateway = (ROOT / "coding-agents/gateway_mcp/deploy-all.sh").read_text()
    assert "get-gateway --gateway-identifier" in gateway
    assert "list-gateway-targets --gateway-identifier" in gateway
    assert "get-agent-runtime --agent-runtime-id" in gateway


def test_prebuilt_explain_never_builds_or_deploys(tmp_path):
    """The wrapper passes --explain through and skips the self-healing image build."""
    coding = tmp_path / "coding-agents"
    role = coding / "claude-code"
    role.mkdir(parents=True)
    for name in ("runtime_deploy.py", "cli_versions.py", "cli-versions.json", "deploy-prebuilt.sh"):
        shutil.copy2(ROOT / "coding-agents" / name, coding / name)
    (coding / "infra.config").write_text(f"INFRA_S3FILES_AP_ARN={AP}\n")
    log = tmp_path / "log.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = f"""#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(args[0]).name if pathlib.Path(sys.argv[0]).name == "python3" else pathlib.Path(sys.argv[0]).name
open(os.environ["LOG"], "a").write(json.dumps([name, args]) + "\\n")
if name == "runtime_deploy.py":
    os.execv(sys.executable, [sys.executable, "-B", *args])
"""
    for path in (bin_dir / "python3", role / "setup.sh"):
        path.write_text(stub)
        path.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "LOG": str(log), "HOME": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1"}

    missing = subprocess.run(["bash", str(coding / "deploy-prebuilt.sh"), "claude-code", "--explain"],
                             env=env, capture_output=True, text=True, timeout=20)
    assert missing.returncode == 1 and "--explain never builds" in missing.stderr
    assert [row[0] for row in map(json.loads, log.read_text().splitlines())] == ["runtime_deploy.py"]

    log.unlink()
    (role / "agent.config").write_text(f"ECR_URI={IMAGE}\n")
    ok = subprocess.run(["bash", str(coding / "deploy-prebuilt.sh"), "claude-code", "--explain"],
                        env=env, capture_output=True, text=True, timeout=20)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert [row[0] for row in rows] == ["runtime_deploy.py", "deploy.py"]
    assert rows[-1][1] == ["deploy.py", "--explain"]
    assert "Done." not in ok.stdout and "Deploying pre-built" not in ok.stdout + ok.stderr

    bad = subprocess.run(["bash", str(coding / "deploy-prebuilt.sh"), "claude-code", "--now"],
                         env=env, capture_output=True, text=True, timeout=20)
    assert bad.returncode == 2 and "[--explain]" in bad.stderr


def test_coordinator_receipt_reads_local_state_only(tmp_path):
    spec = importlib.util.spec_from_file_location("promote_receipt", ROOT / "orchestrator-agent/promote_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "CodingAgents"
    cli = project / "agentcore" / ".cli"
    cli.mkdir(parents=True)
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/orch-1"
    (project / "agentcore" / "aws-targets.json").write_text(json.dumps(
        [{"name": "default", "region": REGION, "account": ACCOUNT}]))
    (project / "agentcore" / "agentcore.json").write_text(json.dumps({"runtimes": [{
        "name": "orchestrator", "envVars": [{"name": "ORCHESTRATOR_MODEL_ID", "value": "us.model"},
                                            {"name": "GITHUB_REPO", "value": SECRET_VALUE}]}]}))
    (cli / "deployed-state.json").write_text(json.dumps({"targets": {"default": {"resources": {
        "stackName": "AgentCore-CodingAgents-default",
        "runtimes": {"orchestrator": {"runtimeId": "orch-1", "runtimeArn": arn,
                                      "roleArn": "arn:aws:iam::1:role/orch"}}}}}}))
    (cli / "runtime-platforms.json").write_text(json.dumps({"default": {"orchestrator": {
        "runtime_id": "orch-1", "runtime_arn": arn, "runtime_version": "2",
        "runtime_status": "READY", "platform_version": "V1"}}}))

    text = "\n".join(module.receipt_lines(project))
    assert "the coordinator, an Amazon Bedrock AgentCore Runtime" in text
    assert f"Runtime ARN:     {arn}" in text and "Revision:        2, platform V1, READY" in text
    assert "us.model (ORCHESTRATOR_MODEL_ID)" in text
    assert "arn:aws:iam::1:role/orch" in text
    assert "CloudFormation stack AgentCore-CodingAgents-default" in text
    assert f"get-agent-runtime --agent-runtime-id orch-1 --region {REGION}" in text
    assert f"describe-stacks --stack-name AgentCore-CodingAgents-default --region {REGION}" in text
    assert SECRET_VALUE not in text

    (project / "agentcore" / "agentcore.json").write_text(json.dumps({"runtimes": []}))
    for name in ("deployed-state.json", "runtime-platforms.json"):
        (cli / name).unlink()
    sparse = "\n".join(module.receipt_lines(project))
    assert "Runtime ARN:     not recorded" in sparse
    assert "(the engine default)" in sparse and "global.anthropic." in sparse


def test_coordinator_receipt_never_fails_on_an_unexpected_local_shape(tmp_path):
    """`deploy-coordinator.sh` prints this after a deploy that already passed."""
    project = tmp_path / "CodingAgents"
    cli = project / "agentcore" / ".cli"
    cli.mkdir(parents=True)
    (cli / "runtime-platforms.json").write_text("[]")
    (project / "agentcore" / "agentcore.json").write_text(json.dumps({"runtimes": None}))
    result = subprocess.run(
        [sys.executable, str(ROOT / "orchestrator-agent/promote_runtime.py"), "--receipt",
         "--project", str(project)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert "the deployment itself is unaffected" in result.stdout or "What this created" in result.stdout
