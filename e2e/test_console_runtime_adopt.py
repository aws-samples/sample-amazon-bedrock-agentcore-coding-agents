"""The console path for Lab 1's backend Runtime: --prepare, then --adopt.

A person creates the Runtime in the AgentCore console from the values --prepare
prints; --adopt checks what they built against the request the scripted path
sends, and records it only when every check passes. These tests pin that the
check catches each field a person can get wrong in the form, that a failing
Runtime is never recorded, and that the sheet names every field it checks.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "coding-agents"))
import runtime_deploy  # noqa: E402

REGION, ACCOUNT = "us-west-2", "111122223333"
AP = f"arn:aws:s3files:{REGION}:{ACCOUNT}:file-system/fs-1/access-point/fsap-1"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/coding-agents-claude-code:pinned"
ROLE = f"agentcore-claude_code-{REGION}-role"
EXPECTED = dict(image=IMAGE, role_name=ROLE, subnets=["subnet-a", "subnet-b"],
                security_groups=["sg-1"], mount_ap_arn=AP, mount_path="/mnt/s3files",
                environment={"AWS_REGION": REGION, "WORKSHOP_AGENT_NAME": "claude_code"})

BUILT = {
    "agentRuntimeId": "claude_code-abc", "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/claude_code-abc",
    "agentRuntimeName": "claude_code", "agentRuntimeVersion": "1", "status": "READY",
    "platformVersion": "V1",
    "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
    "roleArn": f"arn:aws:iam::{ACCOUNT}:role/{ROLE}",
    "protocolConfiguration": {"serverProtocol": "HTTP"},
    "networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
        "subnets": ["subnet-b", "subnet-a"], "securityGroups": ["sg-1"]}},
    # The console appends a trailing slash to the mount path; that is the same mount.
    "filesystemConfigurations": [{"s3FilesAccessPoint": {"accessPointArn": AP,
                                                        "mountPath": "/mnt/s3files/"}}],
    "environmentVariables": {"AWS_REGION": REGION, "WORKSHOP_AGENT_NAME": "claude_code",
                             "EXTRA": "allowed"},
}


class Control:
    def __init__(self, runtimes):
        self.runtimes = runtimes

    def get_paginator(self, name):
        assert name == "list_agent_runtimes"
        runtimes = self.runtimes

        class Pages:
            def paginate(self):
                yield {"agentRuntimes": [{"agentRuntimeName": r["agentRuntimeName"],
                                          "agentRuntimeId": r["agentRuntimeId"]} for r in runtimes]}
        return Pages()

    def get_agent_runtime(self, agentRuntimeId):
        return next(r for r in self.runtimes if r["agentRuntimeId"] == agentRuntimeId)


def failing(checks):
    return [label for ok, label, _ in checks if not ok]


def test_a_correctly_built_runtime_passes_and_is_recorded():
    record, checks = runtime_deploy.adopt_console_runtime(
        Control([BUILT]), name="claude_code", platform="V1", wait_s=0, **EXPECTED)
    assert failing(checks) == []
    assert record["runtime_arn"] == BUILT["agentRuntimeArn"]
    assert record["platform_version"] == "V1"


def test_each_form_mistake_is_caught_with_a_fix_and_never_recorded():
    mistakes = {
        "image": lambda r: r["agentRuntimeArtifact"]["containerConfiguration"].update(containerUri="other:latest"),
        "role": lambda r: r.update(roleArn=f"arn:aws:iam::{ACCOUNT}:role/AmazonBedrockAgentCoreRuntimeDefault"),
        "protocol": lambda r: r["protocolConfiguration"].update(serverProtocol="MCP"),
        "public network": lambda r: r["networkConfiguration"].update(networkMode="PUBLIC"),
        "public subnet": lambda r: r["networkConfiguration"]["networkModeConfig"].update(subnets=["subnet-a", "subnet-public"]),
        "default security group": lambda r: r["networkConfiguration"]["networkModeConfig"].update(securityGroups=["sg-default"]),
        "no mount": lambda r: r.update(filesystemConfigurations=[]),
        "wrong mount path": lambda r: r["filesystemConfigurations"][0]["s3FilesAccessPoint"].update(mountPath="/mnt/data"),
        "missing variable": lambda r: r["environmentVariables"].pop("WORKSHOP_AGENT_NAME"),
        "V2 instead of V1": lambda r: r.update(platformVersion="V2"),
    }
    for label, mutate in mistakes.items():
        built = copy.deepcopy(BUILT)
        mutate(built)
        record, checks = runtime_deploy.adopt_console_runtime(
            Control([built]), name="claude_code", platform="V1", wait_s=0, **EXPECTED)
        assert record is None, label
        bad = [(ok, what, fix) for ok, what, fix in checks if not ok]
        assert len(bad) == 1, (label, bad)
        assert bad[0][2], f"{label}: a failing check must say what to change"


def test_a_missing_or_unfinished_runtime_is_not_recorded():
    record, checks = runtime_deploy.adopt_console_runtime(
        Control([]), name="claude_code", platform="V1", wait_s=0, **EXPECTED)
    assert record is None and failing(checks) == ["Runtime exists"]
    creating = dict(copy.deepcopy(BUILT), status="CREATING")
    record, checks = runtime_deploy.adopt_console_runtime(
        Control([creating]), name="claude_code", platform="V1", wait_s=0, **EXPECTED)
    assert record is None and any("status READY" in label for label in failing(checks))


def test_the_sheet_names_every_value_the_check_requires():
    names = {"vpc": "vpc-1 (cfn-ws-VPC)", "file_system": "cfn-ws-workspace (fs-1)",
             "access_point": "fsap-1",
             "subnets": {"subnet-a": "subnet-a (cfn-ws-agent-private-1)",
                         "subnet-b": "subnet-b (cfn-ws-agent-private-2)"},
             "security_groups": {"sg-1": "sg-1 (cfn-ws-AgentSecurityGroup)"}}
    sheet = "\n".join(runtime_deploy.console_sheet(
        name="claude_code", platform="V1", image=IMAGE, role_name=ROLE, names=names,
        mount_path="/mnt/s3files", environment=EXPECTED["environment"]))
    for value in (IMAGE, ROLE, "ECR Container", "HTTP", "Use IAM permissions", "S3 files",
                  "cfn-ws-workspace", "fsap-1", "/mnt/s3files", "cfn-ws-VPC",
                  "cfn-ws-agent-private-1", "cfn-ws-agent-private-2",
                  "cfn-ws-AgentSecurityGroup", "V1", "microVMs",
                  "--adopt", *EXPECTED["environment"], *EXPECTED["environment"].values()):
        assert value in sheet, value
