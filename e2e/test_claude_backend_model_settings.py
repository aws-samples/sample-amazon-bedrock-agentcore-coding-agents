"""Exercise backend model/effort selection at the real shell and AWS boundaries.

The recording Claude executable never invokes a model. The STS executable and
AgentCore client are test doubles; no developer credentials or live AWS are used.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import boto3
from botocore.exceptions import ClientError
import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "coding-agents" / "claude-code"
MODEL_VARIABLES = (
    "WORKSHOP_CLAUDE_MODEL", "WORKSHOP_MODEL", "WORKSHOP_MODEL_CLAUDE_CODE",
    "WORKSHOP_CLAUDE_EFFORT", "ANTHROPIC_MODEL",
)


@pytest.fixture
def launcher(tmp_path):
    """Run the real launcher, replacing only external executables and /proc input."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    record = tmp_path / "cli.json"
    (binaries / "claude").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CLAUDE_TEST_RECORD']).write_text(json.dumps({"
        "'argv': sys.argv[1:], 'cwd': os.getcwd(), "
        "'bedrock': os.environ.get('CLAUDE_CODE_USE_BEDROCK')}))\n"
        "sys.exit(int(os.environ.get('CLAUDE_TEST_EXIT', '0')))\n"
    )
    # Onboarding writes to the image's fixed /home/agent. It has its own execution
    # tests; this executable consumes that block without touching a host home.
    (binaries / "python3").write_text("#!/bin/sh\ncat >/dev/null\n")
    (binaries / "aws").write_text(
        "#!/bin/sh\n"
        "printf 'test-access\\ttest-secret\\ttest-session\\n'\n"
    )
    for path in binaries.iterdir():
        path.chmod(0o755)
    proc_environment = tmp_path / "entrypoint.environ"
    proc_environment.write_bytes(b"")
    for name in ("run.sh", "run-as-user.sh"):
        (tmp_path / name).write_text((HARNESS / name).read_text().replace(
            "/proc/1/environ", str(proc_environment)))
    clean_env = {
        key: value for key, value in os.environ.items()
        if key not in MODEL_VARIABLES
        and not key.startswith(("AWS_", "AGENTCORE_RUNTIME_"))
        and key not in ("GATEWAY_URL", "GITHUB_TOKEN", "KIRO_API_KEY")
    }
    clean_env.update({
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "CLAUDE_TEST_RECORD": str(record),
        "WORKSHOP_AGENT_WORKDIR": str(tmp_path),
        "AWS_REGION": "us-west-2",
        "PERUSER_ROLE_ARN": "arn:aws:iam::123456789012:role/test-user",
    })

    def run(path="run.sh", overrides=None, args=(), inherited=None, prompt="Inspect the task"):
        if inherited is not None:
            proc_environment.write_bytes(b"".join(
                f"{key}={value}\0".encode() for key, value in inherited.items()))
        if path == "run-as-user.sh":
            command = ["bash", str(tmp_path / path), "test-user", prompt]
        else:
            command = ["bash", str(tmp_path / path), *args]
            if prompt is not None:
                command.append(prompt)
        result = subprocess.run(
            command, cwd=tmp_path, env={**clean_env, **(overrides or {})},
            capture_output=True, text=True, timeout=10,
        )
        return result, json.loads(record.read_text()) if record.exists() else None

    return run


@pytest.mark.parametrize("path", ["run.sh", "run-as-user.sh"])
@pytest.mark.parametrize("overrides,expected", [
    ({}, "us.anthropic.claude-opus-5"),
    ({"ANTHROPIC_MODEL": "native-model"}, "native-model"),
    ({"ANTHROPIC_MODEL": "native-model", "WORKSHOP_CLAUDE_MODEL": "stack-model"}, "stack-model"),
    ({"WORKSHOP_CLAUDE_MODEL": "stack-model", "WORKSHOP_MODEL": "generic-model"}, "generic-model"),
    ({"WORKSHOP_CLAUDE_MODEL": "stack-model", "WORKSHOP_MODEL": "generic-model",
      "WORKSHOP_MODEL_CLAUDE_CODE": "role-model"}, "role-model"),
    ({"WORKSHOP_CLAUDE_MODEL": "", "WORKSHOP_MODEL": "",
      "WORKSHOP_MODEL_CLAUDE_CODE": ""}, "us.anthropic.claude-opus-5"),
])
def test_model_environment_precedence_reaches_the_cli(launcher, path, overrides, expected):
    result, record = launcher(path, overrides)
    assert result.returncode == 0, result.stderr
    arguments = record["argv"]
    assert arguments.count("--model") == 1
    assert arguments[arguments.index("--model") + 1] == expected
    assert record["bedrock"] == "1"


@pytest.mark.parametrize("path", ["run.sh", "run-as-user.sh"])
@pytest.mark.parametrize("effort", [None, "max", ""])
def test_effort_default_override_and_omission_reach_the_cli(launcher, path, effort):
    overrides = {} if effort is None else {"WORKSHOP_CLAUDE_EFFORT": effort}
    result, record = launcher(path, overrides)
    assert result.returncode == 0, result.stderr
    arguments = record["argv"]
    if effort == "":
        assert "--effort" not in arguments
    else:
        assert arguments.count("--effort") == 1
        assert arguments[arguments.index("--effort") + 1] == (effort or "high")


@pytest.mark.parametrize("args", [
    ("--model", "explicit-model", "--effort", "low"),
    ("--model=explicit-model", "--effort=low"),
])
def test_explicit_launcher_flags_win_and_preserve_prompt_and_exit_code(launcher, args):
    prompt = "Keep 'quotes' and $(touch do-not-create) as task text"
    result, record = launcher(overrides={
        "WORKSHOP_MODEL_CLAUDE_CODE": "role-model",
        "WORKSHOP_MODEL": "generic-model",
        "WORKSHOP_CLAUDE_MODEL": "stack-model",
        "WORKSHOP_CLAUDE_EFFORT": "max",
        "CLAUDE_TEST_EXIT": "23",
    }, args=args, prompt=prompt)
    assert result.returncode == 23
    arguments = record["argv"]
    assert arguments[arguments.index("--model") + 1] == "explicit-model"
    assert arguments[arguments.index("--effort") + 1] == "low"
    assert arguments.count("--model") == arguments.count("--effort") == 1
    assert arguments[-1] == prompt
    assert not (Path(record["cwd"]) / "do-not-create").exists()


@pytest.mark.parametrize("args", [(), ("--model", "explicit-model", "--effort", "max")])
def test_interactive_launcher_honors_settings_without_switching_to_print_mode(launcher, args):
    result, record = launcher(args=args, prompt=None)
    assert result.returncode == 0, result.stderr
    arguments = record["argv"]
    assert "--print" not in arguments
    assert arguments[arguments.index("--model") + 1] == (
        "explicit-model" if args else "us.anthropic.claude-opus-5")
    assert arguments[arguments.index("--effort") + 1] == ("max" if args else "high")


@pytest.mark.parametrize("path", ["run.sh", "run-as-user.sh"])
def test_container_settings_reach_an_empty_command_shell_without_replacing_overrides(launcher, path):
    inherited = {
        "WORKSHOP_CLAUDE_MODEL": "container-model",
        "WORKSHOP_CLAUDE_EFFORT": "max",
    }
    result, record = launcher(path, inherited=inherited)
    assert result.returncode == 0, result.stderr
    arguments = record["argv"]
    assert arguments[arguments.index("--model") + 1] == "container-model"
    assert arguments[arguments.index("--effort") + 1] == "max"

    result, record = launcher(
        path,
        inherited=inherited,
        overrides={"WORKSHOP_CLAUDE_MODEL": "shell-model", "WORKSHOP_CLAUDE_EFFORT": ""},
    )
    assert result.returncode == 0, result.stderr
    arguments = record["argv"]
    assert arguments[arguments.index("--model") + 1] == "shell-model"
    assert "--effort" not in arguments


def _deploy_module(tmp_path, monkeypatch, control):
    agents = tmp_path / "coding-agents"
    backend = agents / "claude-code"
    backend.mkdir(parents=True)
    source = backend / "deploy.py"
    shutil.copyfile(HARNESS / "deploy.py", source)
    for shared in ("runtime_deploy.py", "cli_versions.py", "cli-versions.json"):
        shutil.copyfile(ROOT / "coding-agents" / shared, agents / shared)
    (agents / "infra.config").write_text(
        "INFRA_REGION=us-west-2\nINFRA_ACCOUNT_ID=123456789012\n"
        "INFRA_SUBNET_1=subnet-one\nINFRA_SUBNET_2=subnet-two\n"
        "INFRA_SECURITY_GROUP=sg-test\nINFRA_BUCKET=test-workspace\n"
    )
    (backend / "agent.config").write_text(
        "AGENT_NAME=claude_code\nECR_URI=123456789012.dkr.ecr.us-west-2.amazonaws.com/backend:pinned\n"
    )
    session = SimpleNamespace(client=lambda *args, **kwargs: control)
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: session)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    spec = importlib.util.spec_from_file_location("claude_backend_deploy_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, backend


@pytest.mark.parametrize("existing", [False, True, "conflict"])
@pytest.mark.parametrize("effort", ["max", ""])
@pytest.mark.parametrize("platform", ["V1", "V2"])
def test_deploy_forwards_only_public_backend_settings_on_create_and_update(
        tmp_path, monkeypatch, existing, effort, platform):
    monkeypatch.setenv("WORKSHOP_RUNTIME_PLATFORM_VERSION", platform)
    calls = []
    class Control:
        revision = "1"

        def create_agent_runtime(self, **kwargs):
            calls.append(kwargs)
            if existing == "conflict":
                raise ClientError({"Error": {"Code": "ConflictException", "Message": "name exists"}},
                                  "CreateAgentRuntime")
            return {"agentRuntimeId": "backend-test", "agentRuntimeArn": "arn:test",
                    "agentRuntimeVersion": "1", "status": "CREATING", "createdAt": 100}

        def update_agent_runtime(self, **kwargs):
            calls.append(kwargs)
            self.revision = "2"
            return {"agentRuntimeId": "backend-test", "agentRuntimeArn": "arn:test",
                    "agentRuntimeVersion": "2", "status": "UPDATING",
                    "createdAt": 100, "lastUpdatedAt": 200}

        def get_agent_runtime(self, **kwargs):
            return {"status": "READY", "platformVersion": platform,
                    "agentRuntimeVersion": self.revision, "agentRuntimeName": "claude_code",
                    "createdAt": 100, "lastUpdatedAt": 200 if self.revision == "2" else 100,
                    "agentRuntimeId": "backend-test", "agentRuntimeArn": "arn:test"}

        def get_paginator(self, name):
            return SimpleNamespace(paginate=lambda: [{
                "agentRuntimes": [{"agentRuntimeName": "claude_code", "agentRuntimeId": "backend-test"}],
            }])

    module, backend = _deploy_module(tmp_path, monkeypatch, Control())
    if existing is True:
        (backend / "runtime_config.json").write_text('{"runtime_id":"backend-test"}')
    settings = {
        "WORKSHOP_CLAUDE_MODEL": "us.anthropic.stack-model",
        "WORKSHOP_MODEL": "us.anthropic.generic-model",
        "WORKSHOP_MODEL_CLAUDE_CODE": "us.anthropic.role-model",
        "WORKSHOP_CLAUDE_EFFORT": effort,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "GITHUB_TOKEN", "KIRO_API_KEY", "ORCHESTRATOR_MODEL_ID"):
        monkeypatch.setenv(name, "test-do-not-forward")
    result = module.deploy_runtime("arn:aws:iam::123456789012:role/backend")
    assert result["runtime_id"] == "backend-test"
    assert result["platform_version"] == platform
    for arguments in calls:
        assert arguments["platformVersion"] == platform
        assert arguments["environmentVariables"] == {
            "AWS_REGION": "us-west-2", "WORKSHOP_AGENT_NAME": "claude_code", **settings,
        }
