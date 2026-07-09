import importlib.util
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "configure_deploy", HERE / "configure_deploy.py"
)
configure_deploy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(configure_deploy)


def test_configure_wires_role_arns_execution_role_and_runtime_environment(tmp_path):
    project = tmp_path / "CodingAgents" / "agentcore" / "agentcore.json"
    project.parent.mkdir(parents=True)
    project.write_text(json.dumps({
        "runtimes": [{"name": "orchestrator", "build": "Container"}]
    }), encoding="utf-8")

    for role in configure_deploy.ROLES:
        role_dir = tmp_path / "coding-agents" / role
        role_dir.mkdir(parents=True)
        (role_dir / "runtime_config.json").write_text(json.dumps({
            "runtime_arn": f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{role}"
        }), encoding="utf-8")

    configure_deploy.configure(
        project,
        tmp_path,
        {
            "OrchestratorRuntimeRoleArn": "arn:aws:iam::123456789012:role/orchestrator",
            "PerUserRoleArn": "arn:aws:iam::123456789012:role/peruser",
        },
        "us-west-2",
        "123456789012",
    )

    runtime = json.loads(project.read_text(encoding="utf-8"))["runtimes"][0]
    env = {item["name"]: item["value"] for item in runtime["envVars"]}
    assert runtime["executionRoleArn"].endswith(":role/orchestrator")
    assert env["AGENTCORE_RUNTIME_CLAUDE_CODE"].endswith("/claude-code")
    assert env["AGENTCORE_RUNTIME_OPENCODE"].endswith("/opencode")
    assert env["AGENTCORE_RUNTIME_CLAUDE_CODE_VALIDATOR"].endswith("/claude-code-validator")
    assert env["WORKSHOP_RUNTIME_BUCKET"] == "coding-agents-123456789012-us-west-2"
    assert env["WORKSHOP_GITHUB_STORE"] == "secretsmanager"

    # The deploy target must be pinned to the workshop region: `agentcore deploy`
    # otherwise creates its default target in us-east-1 regardless of AWS_DEFAULT_REGION.
    targets = json.loads((project.parent / "aws-targets.json").read_text(encoding="utf-8"))
    assert targets == [{"name": "default",
                        "description": "Workshop target (us-west-2)",
                        "account": "123456789012",
                        "region": "us-west-2"}]
