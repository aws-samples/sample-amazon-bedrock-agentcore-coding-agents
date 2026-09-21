"""Execute native deployment wrappers with the real platform/SDK preflight.

Cloud, image builds and deployment children are strict process-boundary fixtures;
only the actual shell and local selection/preflight execute.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
WRAPPERS = (
    ("coding-agents/deploy-all.sh", [], "deploy.py"),
    ("coding-agents/deploy-prebuilt.sh", ["claude-code"], "deploy.py"),
    ("coding-agents/gateway_mcp/deploy-all.sh", [], "deploy-runtime.sh"),
    ("orchestrator-agent/deploy-coordinator.sh", [], "promote_runtime.py"),
)


def run_wrapper(directory, relative, arguments, platform=None, fail_at=""):
    checkout = directory / "checkout with spaces"
    coding = checkout / "coding-agents"
    coding.mkdir(parents=True)
    for name in ("runtime_deploy.py", "cli_versions.py", "cli-versions.json"):
        shutil.copy2(ROOT / "coding-agents" / name, coding / name)
    script = checkout / relative
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / relative, script)
    role = coding / "claude-code"
    role.mkdir()
    (role / "agent.config").write_text("ECR_URI=fixture/image:pinned\n")
    (role / "runtime_config.json").write_text('{"runtime_arn":"arn:fixture:runtime"}\n')
    (coding / "infra.config").write_text("INFRA_S3FILES_AP_ARN=arn:fixture:mount\n")
    project = checkout / "CodingAgents/agentcore"
    project.mkdir(parents=True)
    (project / "agentcore.json").write_text("{}\n")
    gateway = coding / "gateway_mcp"
    gateway.mkdir(exist_ok=True)
    (gateway / "config.sh").write_text(
        'STATE_FILE=fixture-state\nstate_get() { printf "%s\\n" https://fixture.invalid/mcp; }\n')
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    log = directory / "commands.jsonl"
    boundary = f"""#!{sys.executable}
import json, os, pathlib, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
if name == "python3":
    name = pathlib.Path(args[0]).name
with open(os.environ["PLATFORM_LOG"], "a") as stream:
    stream.write(json.dumps({{"name": name, "args": args,
                            "platform": os.environ.get("WORKSHOP_RUNTIME_PLATFORM_VERSION")}}) + "\\n")
if name == os.environ["PLATFORM_FAIL_AT"]:
    raise SystemExit(37)
if name == "runtime_deploy.py":
    os.execv(sys.executable, [sys.executable, "-B", *args])
elif name == "-c":
    assert "from roles import roster_ids" in args[1]
    print("claude-code")
elif name == "jq":
    print("arn:fixture:runtime")
elif name == "agentcore":
    assert args[0] in ("validate", "deploy", "status"), args
elif name not in ("setup.sh", "deploy.py", "stage_engine.py", "configure_deploy.py",
                  "promote_runtime.py", "probe_coordinator.py", "deploy-credential.sh",
                  "deploy-runtime.sh", "deploy-gateway.sh", "verify-gateway.sh"):
    raise SystemExit("Unexpected fixture process: " + name + repr(args))
"""
    # The wrapper itself stays unchanged; intercept only its external commands.
    paths = [bin_dir / name for name in ("python3", "agentcore", "jq", "aws", "docker")]
    paths.append(role / "setup.sh")
    paths.extend(gateway / name for name in (
        "deploy-credential.sh", "deploy-runtime.sh", "deploy-gateway.sh", "verify-gateway.sh"))
    for path in paths:
        path.write_text(boundary)
        path.chmod(0o755)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.defpath,
        "HOME": str(directory),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AWS_REGION": "us-west-2",
        "GITHUB_GATEWAY_URL": "https://fixture.invalid/mcp",
        "GITHUB_REPO": "fixture/repository",
        "PLATFORM_LOG": str(log),
        "PLATFORM_FAIL_AT": fail_at,
    }
    if platform is not None:
        env["WORKSHOP_RUNTIME_PLATFORM_VERSION"] = platform
    result = subprocess.run(["bash", str(script), *arguments], cwd=checkout,
                            env=env, capture_output=True, text=True, timeout=20)
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    return result, commands


@pytest.mark.parametrize("relative,arguments,child", WRAPPERS)
@pytest.mark.parametrize("platform", [None, "V1", "V2", "v2"])
def test_native_wrappers_validate_then_forward_selected_platform(
        tmp_path, relative, arguments, child, platform):
    result, commands = run_wrapper(tmp_path, relative, arguments, platform)
    assert commands[0]["name"] == "runtime_deploy.py"
    assert commands[0]["args"][1:] == ["--print-platform"]
    if platform == "v2":
        assert result.returncode != 0 and "WORKSHOP_RUNTIME_PLATFORM_VERSION" in result.stderr
        assert len(commands) == 1, "invalid selection must stop before any deployment child"
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert any(row["name"] == child for row in commands)
        assert all(row["platform"] == (platform or "V1") for row in commands[1:])
        if child == "promote_runtime.py":
            assert f"Verifying platform {platform or 'V1'} READY" in result.stdout
            assert [row["name"] for row in commands].index(child) < (
                [row["name"] for row in commands].index("probe_coordinator.py"))


@pytest.mark.parametrize("relative,arguments,child", WRAPPERS)
def test_native_wrappers_propagate_deployment_failure(tmp_path, relative, arguments, child):
    result, commands = run_wrapper(tmp_path, relative, arguments, "V2", fail_at=child)
    assert result.returncode == 37
    assert commands[-1]["name"] == child
    assert "Deployment Complete" not in result.stdout
    assert "Coordinator deployed." not in result.stdout
