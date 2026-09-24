"""A fresh terminal uses the same saved GitHub configuration as doctor.

Run the actual Bash wrapper, Python resolver and platform preflight. Stop at
the staging boundary so these regressions cannot start a cloud deployment.
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
GATEWAY = "https://saved.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
OVERRIDE = "https://explicit.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def run_preflight(tmp_path, settings, state, overrides=None):
    checkout = tmp_path / "checkout with spaces"
    for relative in (
        "orchestrator-agent/deploy-coordinator.sh",
        "orchestrator/github.py",
        "orchestrator/roles.py",
        "coding-agents/runtime_deploy.py",
        "coding-agents/cli_versions.py",
        "coding-agents/cli-versions.json",
    ):
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    role = checkout / "coding-agents/claude-code"
    role.mkdir()
    (role / "runtime_config.json").write_text(
        '{"runtime_arn":"arn:fixture:runtime"}\n')
    settings_file = tmp_path / "settings.json"
    state_file = tmp_path / "state.json"
    settings_file.write_text(json.dumps(settings))
    state_file.write_text(json.dumps(state))
    receipt = tmp_path / "staging.json"
    (checkout / "orchestrator-agent/stage_engine.py").write_text(
        "import json, os, pathlib\n"
        "pathlib.Path(os.environ['PREFLIGHT_RECEIPT']).write_text(json.dumps({"
        "'gateway': os.environ['GITHUB_GATEWAY_URL'],"
        "'repo': os.environ['GITHUB_REPO']}))\n"
        "raise SystemExit(31)\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n")
    python.chmod(0o755)
    jq = shutil.which("jq")
    assert jq, "jq is a deployment-wrapper prerequisite"
    (bin_dir / "jq").symlink_to(jq)
    for name in ("agentcore", "aws"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 99\n")
        stub.chmod(0o755)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "AWS_REGION": "us-east-1",
        "WORKSHOP_ROLES": "claude-code",
        "WORKSHOP_GITHUB_SETTINGS": str(settings_file),
        "WORKSHOP_GATEWAY_STATE": str(state_file),
        "PREFLIGHT_RECEIPT": str(receipt),
        **(overrides or {}),
    }
    result = subprocess.run(
        ["/bin/bash", str(checkout / "orchestrator-agent/deploy-coordinator.sh")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20,
    )
    return result, json.loads(receipt.read_text()) if receipt.exists() else None


@pytest.mark.parametrize("settings,state,overrides,expected", [
    ({"repo": "saved/app"}, {"gateway_url": GATEWAY}, {},
     {"gateway": GATEWAY, "repo": "saved/app"}),
    ({"repo": "saved/app", "gateway_url": OVERRIDE}, {"gateway_url": GATEWAY}, {},
     {"gateway": OVERRIDE, "repo": "saved/app"}),
    ({"repo": "saved/app"}, {"gateway_url": GATEWAY},
     {"GITHUB_GATEWAY_URL": OVERRIDE},
     {"gateway": OVERRIDE, "repo": "saved/app"}),
    ({"repo": "saved/app"}, {"gateway_url": GATEWAY},
     {"GITHUB_REPO": "explicit/app"},
     {"gateway": GATEWAY, "repo": "explicit/app"}),
    ({}, {}, {"GITHUB_GATEWAY_URL": OVERRIDE, "GITHUB_REPO": "explicit/app"},
     {"gateway": OVERRIDE, "repo": "explicit/app"}),
])
def test_saved_configuration_reaches_staging_without_overriding_env(
        tmp_path, settings, state, overrides, expected):
    result, receipt = run_preflight(tmp_path, settings, state, overrides)
    assert result.returncode == 31, result.stdout + result.stderr
    assert receipt == expected


@pytest.mark.parametrize("settings,state,overrides", [
    ({}, {}, {}),
    ({"repo": "saved/app"}, {}, {}),
    ({}, {"gateway_url": GATEWAY}, {}),
    ({"repo": "invalid-repository"}, {"gateway_url": GATEWAY}, {}),
    ({"repo": "saved/app"}, {"gateway_url": GATEWAY},
     {"GITHUB_REPO": "invalid-repository"}),
])
def test_incomplete_or_invalid_configuration_stops_before_staging(
        tmp_path, settings, state, overrides):
    result, receipt = run_preflight(tmp_path, settings, state, overrides)
    assert result.returncode != 0
    assert receipt is None
    assert "ERROR:" in result.stderr
