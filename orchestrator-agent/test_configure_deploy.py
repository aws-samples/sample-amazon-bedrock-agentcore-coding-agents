import importlib.util
import json
from pathlib import Path

import pytest


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


def _write_infra(root, *, region="us-west-2", account="123456789012",
                 bucket="coding-agents-123456789012-us-west-2-stack1234"):
    folder = root / "coding-agents"
    folder.mkdir(exist_ok=True)
    (folder / "infra.config").write_text(
        f"INFRA_REGION={region}\nINFRA_ACCOUNT_ID={account}\n"
        f"INFRA_BUCKET='{bucket}'\nINFRA_S3FILES_AP_ARN=\n",
        encoding="utf-8",
    )
    return bucket


def test_stack_bucket_reaches_coordinator_archives_and_state_reader(
        monkeypatch, tmp_path):
    """One stack-specific bucket must be used at every Runtime boundary."""
    import run_store
    import runtime_stage

    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET", raising=False)
    monkeypatch.setenv("WORKSHOP_BEDROCK_REGION", "us-west-2")
    monkeypatch.setattr(runtime_stage, "_SOURCE_ROOT", tmp_path)
    monkeypatch.setattr(runtime_stage, "_account_id", lambda region: "123456789012")
    bucket = _write_infra(tmp_path)

    env = _configure(monkeypatch, tmp_path)
    assert env["WORKSHOP_RUNTIME_BUCKET"] == bucket
    assert runtime_stage.archive_uri("run-1/backend").startswith(f"s3://{bucket}/")
    assert run_store.reader_mirror_bucket() == bucket

    # The coordinator has no host infra.config; its forwarded environment must
    # resolve the same destination without consulting STS or a local file.
    (tmp_path / "coding-agents" / "infra.config").unlink()
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", env["WORKSHOP_RUNTIME_BUCKET"])
    monkeypatch.setattr(
        runtime_stage, "_account_id",
        lambda region: pytest.fail("Explicit Runtime wiring must not call STS"),
    )
    assert runtime_stage.archive_uri("run-1/backend").startswith(f"s3://{bucket}/")
    assert run_store.reader_mirror_bucket() == bucket


@pytest.mark.parametrize(
    "region,account,error",
    [
        ("us-east-1", "123456789012", "REGION_MISMATCH"),
        ("us-west-2", "999999999999", "ACCOUNT_MISMATCH"),
    ],
)
def test_stale_infrastructure_cannot_wire_another_account_or_region(
        monkeypatch, tmp_path, region, account, error):
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET", raising=False)
    _write_infra(tmp_path, region=region, account=account)
    with pytest.raises(RuntimeError, match=error):
        _configure(monkeypatch, tmp_path)


def test_explicit_bucket_override_is_forwarded(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", "explicit-workshop-bucket")
    env = _configure(monkeypatch, tmp_path)
    assert env["WORKSHOP_RUNTIME_BUCKET"] == "explicit-workshop-bucket"


def test_incomplete_infrastructure_cannot_silently_select_a_different_bucket(
        monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET", raising=False)
    _write_infra(tmp_path, bucket="")
    with pytest.raises(RuntimeError, match="INFRA_BUCKET"):
        _configure(monkeypatch, tmp_path)
