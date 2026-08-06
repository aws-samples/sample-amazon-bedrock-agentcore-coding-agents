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


def test_configure_wires_role_arns_execution_role_and_runtime_environment(
        monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "human_review")
    monkeypatch.setenv("WORKSHOP_FINAL_MERGE_POLICY", "auto")
    project = tmp_path / "CodingAgents" / "agentcore" / "agentcore.json"
    project.parent.mkdir(parents=True)
    project.write_text(json.dumps({
        "runtimes": [{"name": "orchestrator", "build": "Container"}]
    }), encoding="utf-8")

    for role in configure_deploy.ROLES():
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
    assert env["AGENTCORE_RUNTIME_KIRO"].endswith("/kiro")
    assert env["WORKSHOP_RUNTIME_BUCKET"] == "coding-agents-123456789012-us-west-2"
    assert env["WORKSHOP_GITHUB_STORE"] == "secretsmanager"
    assert env["WORKSHOP_FINAL_MERGE_POLICY"] == "auto"
    assert "WORKSHOP_MERGE_POLICY" not in env

    # The deploy target must be pinned to the workshop region: `agentcore deploy`
    # otherwise creates its default target in us-east-1 regardless of AWS_DEFAULT_REGION.
    targets = json.loads((project.parent / "aws-targets.json").read_text(encoding="utf-8"))
    assert targets == [{"name": "default",
                        "description": "Workshop target (us-west-2)",
                        "account": "123456789012",
                        "region": "us-west-2"}]


def _configure(monkeypatch, tmp_path):
    """Run configure() over a minimal project and return its envVars mapping."""
    project = tmp_path / "CodingAgents" / "agentcore" / "agentcore.json"
    project.parent.mkdir(parents=True)
    project.write_text(json.dumps({
        "runtimes": [{"name": "orchestrator", "build": "Container"}]
    }), encoding="utf-8")
    for role in configure_deploy.ROLES():
        role_dir = tmp_path / "coding-agents" / role
        role_dir.mkdir(parents=True)
        (role_dir / "runtime_config.json").write_text(json.dumps({
            "runtime_arn": f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{role}"
        }), encoding="utf-8")
    configure_deploy.configure(
        project, tmp_path,
        {"OrchestratorRuntimeRoleArn": "arn:aws:iam::123456789012:role/orchestrator",
         "PerUserRoleArn": "arn:aws:iam::123456789012:role/peruser"},
        "us-west-2", "123456789012",
    )
    runtime = json.loads(project.read_text(encoding="utf-8"))["runtimes"][0]
    return {item["name"]: item["value"] for item in runtime["envVars"]}


def test_roster_and_model_overrides_reach_the_coordinator(monkeypatch, tmp_path):
    """An exported WORKSHOP_ROLES must ride into the coordinator's envVars.

    This was a REAL defect, and the documented Kiro fallback depended on it: the
    roster is read from the coordinator's OWN process env (``roles.roster()``), and
    ``agentcore deploy`` has no env flag, so this file is the only place the override
    can enter. Without it a facilitator could export
    ``WORKSHOP_ROLES=claude-code,opencode,claude-code-validator``, redeploy, and still
    get a coordinator serving the DEFAULT roster, which then fails pre-flight with
    ``RUNTIME_NOT_WIRED:kiro`` -- the exact failure the fallback exists to avoid.
    """
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,opencode,claude-code-validator")
    monkeypatch.setenv("WORKSHOP_KIRO_MODEL", "claude-opus-5")
    env = _configure(monkeypatch, tmp_path)
    assert env["WORKSHOP_ROLES"] == "claude-code,opencode,claude-code-validator"
    assert env["WORKSHOP_KIRO_MODEL"] == "claude-opus-5"


def test_unset_roster_override_is_not_forwarded(monkeypatch, tmp_path):
    """The default deploy must be unchanged: no empty WORKSHOP_ROLES entry.

    An empty value is NOT the same as absent. ``roles.roster()`` treats a blank
    override as "use the default", but shipping the key anyway would make every
    deployed coordinator look overridden to anyone reading its envVars.
    """
    monkeypatch.delenv("WORKSHOP_ROLES", raising=False)
    monkeypatch.delenv("WORKSHOP_KIRO_MODEL", raising=False)
    env = _configure(monkeypatch, tmp_path)
    assert "WORKSHOP_ROLES" not in env
    assert "WORKSHOP_KIRO_MODEL" not in env
