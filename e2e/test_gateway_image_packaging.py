"""Execute the Gateway deploy shell at its Docker/AWS process boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "coding-agents/gateway_mcp/deploy-runtime.sh"
REGISTRY = "000000000000.dkr.ecr.us-east-1.amazonaws.com"
IMAGE = REGISTRY + "/github-mcp:latest"


def run_deploy_fixture(directory: Path, fail_at: str = "", platform: str | None = None):
    """Run the unchanged script with strict external-command fixtures."""
    gateway = directory / "gateway with spaces"
    app = gateway / "app"
    app.mkdir(parents=True)
    script = gateway / "deploy-runtime.sh"
    shutil.copy2(DEPLOY, script)
    (gateway / "config.sh").write_text("""
AWS_REGION=us-east-1
AWS_ACCOUNT_ID=000000000000
RUNTIME_NAME=github_mcp_runtime
ECR_REPO_NAME=github-mcp
ECR_URI=000000000000.dkr.ecr.us-east-1.amazonaws.com/github-mcp
IAM_ROLE_NAME=agentcore-github-mcp-role
APP_DIR="$PACKAGING_APP_DIR"
STATE_FILE="$PACKAGING_STATE"
RUNTIME_NETWORK_MODE=PUBLIC
RUNTIME_PROTOCOL=MCP
RUNTIME_IDLE_TIMEOUT=600
RUNTIME_MAX_LIFETIME=3300
state_set() { printf '%s\\t%s\\n' "$1" "$2" >> "$PACKAGING_STATE"; }
state_get() {
  [ "$1" = github_app_secret_arn ] || return 99
  printf '%s\\n' arn:aws:secretsmanager:us-east-1:000000000000:secret:fixture
}
""")
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    log = directory / "commands.jsonl"
    state = directory / "state.tsv"
    boundary = f"""#!{sys.executable}
import json, os, pathlib, sys
tool, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
fd = os.open(os.environ["PACKAGING_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.write(fd, (json.dumps({{"tool": tool, "args": args,
                         "platform": os.environ.get("WORKSHOP_RUNTIME_PLATFORM_VERSION")}}) + "\\n").encode())
os.close(fd)
if tool == "aws":
    if args[:2] == ["ecr", "describe-repositories"]:
        pass
    elif args[:2] == ["ecr", "get-login-password"]:
        print("fixture-password")
    elif args[:2] == ["iam", "get-role"]:
        print("arn:aws:iam::000000000000:role/agentcore-github-mcp-role")
    else:
        raise SystemExit("Unexpected AWS fixture command: " + repr(args))
elif tool == "docker":
    if args[0] == "login":
        assert sys.stdin.read().strip() == "fixture-password"
    elif args[0] not in ("build", "tag", "push"):
        raise SystemExit("Unexpected Docker fixture command: " + repr(args))
    if args[0] == os.environ["PACKAGING_FAIL_AT"]:
        raise SystemExit(23)
elif tool == "python3":
    if pathlib.Path(args[0]).name not in ("runtime_deploy.py", "deploy_runtime.py"):
        raise SystemExit("Unexpected Python fixture command: " + repr(args))
    if pathlib.Path(args[0]).name == "runtime_deploy.py":
        os.execv(sys.executable, [sys.executable, "-B",
                 {str(ROOT / "coding-agents/runtime_deploy.py")!r}, *args[1:]])
else:
    raise SystemExit("Unexpected fixture executable: " + tool)
"""
    for name in ("aws", "docker", "python3"):
        executable = bin_dir / name
        executable.write_text(boundary)
        executable.chmod(0o755)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.defpath,
        "HOME": str(directory),
        "PACKAGING_APP_DIR": str(app),
        "PACKAGING_LOG": str(log),
        "PACKAGING_STATE": str(state),
        "PACKAGING_FAIL_AT": fail_at,
        "DOCKER_DEFAULT_PLATFORM": "linux/amd64",
    }
    if platform is not None:
        env["WORKSHOP_RUNTIME_PLATFORM_VERSION"] = platform
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=15,
    )
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    return result, commands, state.read_text() if state.exists() else "", app


def test_gateway_build_explicitly_targets_arm64_and_preserves_deploy_flow(tmp_path):
    result, commands, state, app = run_deploy_fixture(tmp_path)
    assert result.returncode == 0, result.stderr
    docker = [row["args"] for row in commands if row["tool"] == "docker"]
    assert docker == [
        ["login", "--username", "AWS", "--password-stdin", REGISTRY],
        ["build", "--platform", "linux/arm64", "-t", "github-mcp:latest", str(app)],
        ["tag", "github-mcp:latest", IMAGE],
        ["push", IMAGE],
    ]
    python = [row["args"] for row in commands if row["tool"] == "python3"]
    assert [Path(args[0]).name for args in python] == ["runtime_deploy.py", "deploy_runtime.py"]
    assert python[-1][python[-1].index("--image") + 1] == IMAGE
    assert python[-1][python[-1].index("--role") + 1] == (
        "arn:aws:iam::000000000000:role/agentcore-github-mcp-role"
    )
    assert "image_uri\t" + IMAGE in state
    assert "Runtime deployment complete." in result.stdout


def _assert_failed_docker_step_stops_deployment(directory, step):
    result, commands, state, _ = run_deploy_fixture(directory, fail_at=step)
    assert result.returncode == 23, result.stdout + result.stderr
    docker = [row["args"][0] for row in commands if row["tool"] == "docker"]
    assert docker == (["login", "build"] if step == "build" else ["login", "build", "tag", "push"])
    assert not any(row["tool"] == "aws" and row["args"][0] == "iam" for row in commands)
    python = [row["args"] for row in commands if row["tool"] == "python3"]
    assert [Path(args[0]).name for args in python] == ["runtime_deploy.py"]
    assert "image_uri\t" not in state
    assert "Runtime deployment complete." not in result.stdout


def test_gateway_build_failure_stops_before_tag_push_or_deploy(tmp_path):
    _assert_failed_docker_step_stops_deployment(tmp_path, "build")


def test_gateway_push_failure_stops_before_iam_or_deploy(tmp_path):
    _assert_failed_docker_step_stops_deployment(tmp_path, "push")


@pytest.mark.parametrize("platform", [None, "V1", "V2"])
def test_gateway_runtime_shell_carries_selection_to_real_adapter_argv(tmp_path, platform):
    result, commands, _, _ = run_deploy_fixture(tmp_path, platform=platform)
    assert result.returncode == 0, result.stderr
    expected = platform or "V1"
    assert commands[0]["tool"] == "python3"
    assert Path(commands[0]["args"][0]).name == "runtime_deploy.py"
    assert commands[0]["args"][1:] == ["--print-platform"]
    assert all(row["platform"] == expected for row in commands[1:])
    assert Path(commands[-1]["args"][0]).name == "deploy_runtime.py"


def test_gateway_invalid_platform_stops_before_config_build_or_iam(tmp_path):
    result, commands, state, _ = run_deploy_fixture(tmp_path, platform="v2")
    assert result.returncode != 0 and "WORKSHOP_RUNTIME_PLATFORM_VERSION" in result.stderr
    assert len(commands) == 1 and commands[0]["tool"] == "python3"
    assert state == ""
