import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

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
    assert env["AGENTCORE_RUNTIME_CODEX"].endswith("/codex")
    assert "AGENTCORE_RUNTIME_OPENCODE" not in env
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


def test_stack_model_settings_reach_a_fresh_coordinator_process(monkeypatch, tmp_path):
    """The host's stack model parameters must survive the deployment boundary."""
    settings = {
        "WORKSHOP_CLAUDE_MODEL": "us.anthropic.claude-sonnet-4-6",
        "WORKSHOP_CODEX_MODEL": "us.openai.gpt-5.6-sol",
        "WORKSHOP_OPENCODE_MODEL": "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "WORKSHOP_SMALL_MODEL": "us.anthropic.claude-sonnet-4-6",
        "ORCHESTRATOR_MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KIRO_API_KEY", "must-not-enter-the-coordinator")
    env = _configure(monkeypatch, tmp_path)
    assert {name: env.get(name) for name in settings} == settings
    assert "KIRO_API_KEY" not in env

    # Inheriting the host's model variables would conceal the deployment bug.
    child_env = {
        name: value for name, value in os.environ.items()
        if name not in settings and name != "KIRO_API_KEY"
    }
    child_env.update(env)
    child_env["PYTHONPATH"] = str(HERE.parent / "orchestrator")
    result = subprocess.check_output(
        [sys.executable, "-c",
         "import json, roles; print(json.dumps({"
         "role: roles.get(role).default_model "
         "for role in ('claude-code', 'codex', 'opencode')}))"],
        env=child_env, text=True,
    )
    assert json.loads(result) == {
        "claude-code": settings["WORKSHOP_CLAUDE_MODEL"],
        "codex": settings["WORKSHOP_CODEX_MODEL"],
        "opencode": settings["WORKSHOP_OPENCODE_MODEL"],
    }


@pytest.mark.parametrize("effort", ["high", "max", ""])
def test_backend_model_and_effort_survive_a_fresh_coordinator_process(
        monkeypatch, tmp_path, effort):
    """Both headless and interactive backend dispatch use the forwarded effort."""
    monkeypatch.setenv("WORKSHOP_CLAUDE_MODEL", "us.anthropic.backend-override")
    monkeypatch.setenv("WORKSHOP_CLAUDE_EFFORT", effort)
    monkeypatch.setenv("WORKSHOP_MODEL", "us.anthropic.shared-override")
    monkeypatch.setenv("WORKSHOP_MODEL_CLAUDE_CODE", "us.anthropic.role-override")
    env = _configure(monkeypatch, tmp_path)
    assert env["WORKSHOP_CLAUDE_EFFORT"] == effort
    assert env["WORKSHOP_MODEL"] == "us.anthropic.shared-override"
    assert env["WORKSHOP_MODEL_CLAUDE_CODE"] == "us.anthropic.role-override"

    # Only the generated deployment environment may supply model/effort settings.
    child_env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("WORKSHOP_CLAUDE_", "WORKSHOP_MODEL"))
    }
    child_env.update(env)
    child_env["PYTHONPATH"] = str(HERE.parent / "orchestrator")
    result = subprocess.check_output(
        [sys.executable, "-c",
         "import json, roles; role = roles.get('claude-code'); "
         "print(json.dumps({'model': role.default_model, "
         "'command': role.command('PROMPT', '', '/tmp/work'), "
         "'environment': role.env}))"],
        env=child_env, text=True,
    )
    backend = json.loads(result)
    assert backend["model"] == "us.anthropic.backend-override"
    assert "--model us.anthropic.backend-override" in backend["command"]
    assert backend["environment"]["WORKSHOP_CLAUDE_EFFORT"] == effort
    if effort:
        assert f"--effort {effort}" in backend["command"]
    else:
        assert "--effort" not in backend["command"]


def test_unset_backend_effort_is_not_forwarded(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSHOP_CLAUDE_EFFORT", raising=False)
    env = _configure(monkeypatch, tmp_path)
    assert "WORKSHOP_CLAUDE_EFFORT" not in env


@pytest.mark.parametrize("overrides,options,backend_model,validator_model", [
    ({}, {}, "us.anthropic.claude-opus-5", "us.anthropic.claude-opus-4-6-v1"),
    ({"WORKSHOP_MODEL_CLAUDE_CODE_VALIDATOR": "us.anthropic.claude-sonnet-4-6"},
     {}, "us.anthropic.claude-opus-5", "us.anthropic.claude-sonnet-4-6"),
    ({"WORKSHOP_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
      "WORKSHOP_MODEL_CLAUDE_CODE_VALIDATOR": "us.anthropic.claude-sonnet-4-6"},
     {}, "us.anthropic.claude-haiku-4-5-20251001-v1:0", "us.anthropic.claude-sonnet-4-6"),
    ({"WORKSHOP_MODEL_CLAUDE_CODE_VALIDATOR": "us.anthropic.claude-sonnet-4-6"},
     {"models": {"claude-code-validator": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}},
     "us.anthropic.claude-opus-5", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
])
def test_restored_validator_model_is_resolved_after_coordinator_configuration(
        monkeypatch, tmp_path, overrides, options, backend_model, validator_model):
    """The automatic backend stack default must not select the restored checker."""
    for name in list(os.environ):
        if name.startswith(("WORKSHOP_CLAUDE_", "WORKSHOP_MODEL")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("WORKSHOP_CLAUDE_MODEL", "us.anthropic.claude-opus-5")
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,codex,claude-code-validator")
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    env = _configure(monkeypatch, tmp_path)
    assert env["WORKSHOP_CLAUDE_MODEL"] == "us.anthropic.claude-opus-5"
    assert {name: env.get(name) for name in overrides} == overrides

    child_env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("WORKSHOP_CLAUDE_", "WORKSHOP_MODEL"))
        and name != "WORKSHOP_ROLES"
    }
    child_env.update(env)
    child_env["PYTHONPATH"] = str(HERE.parent / "orchestrator")
    result = subprocess.check_output(
        [sys.executable, "-c", """
import json, shlex, sys
from types import SimpleNamespace
import engine, roles, runtime_exec
run = SimpleNamespace(options=json.loads(sys.argv[1]))
records = {}
for role_id in ("claude-code", "claude-code-validator"):
    role = roles.get(role_id)
    selected = engine.Engine._role_model(run, role_id, role.default_model)
    records[role_id] = {
        "default": role.default_model,
        "argv": shlex.split(runtime_exec._cli_invocation(
            role_id, "PROMPT", selected, "/tmp/work")),
        "effort": role.env["WORKSHOP_CLAUDE_EFFORT"],
        "kind": role.kind, "hidden": role.hidden,
    }
print(json.dumps({"roster": roles.roster_ids(), "roles": records}))
""", json.dumps(options)],
        env=child_env, text=True, timeout=15,
    )
    resolved = json.loads(result)
    assert resolved["roster"] == ["claude-code", "codex", "claude-code-validator"]
    backend = resolved["roles"]["claude-code"]
    validator = resolved["roles"]["claude-code-validator"]
    assert backend["default"] == "us.anthropic.claude-opus-5"
    assert validator["default"] == "us.anthropic.claude-opus-4-6-v1"
    for role, expected, effort in (
            (backend, backend_model, "high"), (validator, validator_model, "xhigh")):
        argv = role["argv"]
        assert argv[argv.index("--model") + 1] == expected
        assert argv[argv.index("--effort") + 1] == role["effort"] == effort
    assert backend["kind"] == "builder" and backend["hidden"] is False
    assert validator["kind"] == "checker" and validator["hidden"] is True


@pytest.mark.parametrize("value", [None, "", "  "])
def test_absent_model_settings_keep_coordinator_defaults(monkeypatch, tmp_path, value):
    names = (
        "WORKSHOP_CLAUDE_MODEL", "WORKSHOP_CODEX_MODEL", "WORKSHOP_OPENCODE_MODEL",
        "WORKSHOP_SMALL_MODEL", "ORCHESTRATOR_MODEL_ID",
    )
    for name in names:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    env = _configure(monkeypatch, tmp_path)
    assert not set(names).intersection(env)


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
