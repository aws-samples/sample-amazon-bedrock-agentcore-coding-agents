"""Regression tests for the attendee-facing Gateway deployment scripts."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "coding-agents" / "gateway_mcp"


def test_validator_setup_is_executable():
    mode = (ROOT / "coding-agents" / "claude-code-validator" / "setup.sh").stat().st_mode
    assert mode & stat.S_IXUSR


def test_mounted_role_guidance_overrides_the_image_fallback():
    """The files attendees stage must be the project guidance the CLI reads."""
    validator = (ROOT / "coding-agents" / "claude-code-validator" / "run.sh").read_text()
    assert 'VALIDATOR_WORKDIR="/mnt/s3files/validator"' in validator
    assert 'if [ -f "$VALIDATOR_WORKDIR/CLAUDE.md" ]; then' in validator
    assert 'cd "$VALIDATOR_WORKDIR"' in validator

    opencode = (ROOT / "coding-agents" / "opencode" / "run.sh").read_text()
    assert 'elif [ -f /mnt/s3files/AGENTS.md ]; then' in opencode
    assert 'RUN_DIR="/mnt/s3files"' in opencode


def test_runtime_mcp_endpoint_uses_encoded_full_arn(tmp_path):
    runtime_arn = (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:"
        "runtime/github_mcp_runtime-AbCdEf1234"
    )
    command = (
        f"source {GATEWAY / 'config.sh'}; "
        f"agentcore_runtime_mcp_endpoint {runtime_arn}"
    )
    env = {
        **os.environ,
        "AWS_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-west-2",
        "STATE_FILE": str(tmp_path / "state.json"),
    }
    result = subprocess.run(
        ["bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout == (
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123456789012%3A"
        "runtime%2Fgithub_mcp_runtime-AbCdEf1234/invocations?qualifier=DEFAULT"
    )
    assert "accountId=" not in result.stdout


def test_gateway_target_update_uses_target_id():
    script = (GATEWAY / "deploy-gateway.sh").read_text()
    assert "list-gateway-targets" in script
    assert '--target-id "$TARGET_ID"' in script
    assert 'agentcore_runtime_mcp_endpoint "$RUNTIME_ARN"' in script
    assert "accountId=" not in script


def test_gateway_deploy_updates_an_existing_target(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        """{
  "runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/github_mcp_runtime-AbCdEf1234",
  "gateway_id": "github-mcp-gateway-abcdefghij",
  "gateway_url": "https://example.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
}
"""
    )
    command_log = tmp_path / "aws.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$AWS_COMMAND_LOG"
case "$*" in
  "iam get-role --role-name github-mcp-gateway-role --query Role.Arn --output text")
    echo "arn:aws:iam::123456789012:role/github-mcp-gateway-role" ;;
  "iam get-role --role-name github-mcp-gateway-role")
    echo '{"Role":{"Arn":"arn:aws:iam::123456789012:role/github-mcp-gateway-role"}}' ;;
  bedrock-agentcore-control\\ get-gateway\\ *)
    echo '{"gatewayId":"github-mcp-gateway-abcdefghij","gatewayUrl":"https://example.gateway/mcp"}' ;;
  bedrock-agentcore-control\\ list-gateway-targets\\ *)
    echo "AbCdEf1234" ;;
  bedrock-agentcore-control\\ update-gateway-target\\ *)
    echo '{"targetId":"AbCdEf1234","status":"UPDATING"}' ;;
  bedrock-agentcore-control\\ get-gateway-target\\ *)
    echo '{"targetId":"AbCdEf1234","status":"READY"}' ;;
  *) : ;;
esac
"""
    )
    fake_aws.chmod(0o755)
    env = {
        **os.environ,
        "AWS_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-west-2",
        "AWS_COMMAND_LOG": str(command_log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STATE_FILE": str(state_file),
    }

    subprocess.run(
        ["bash", str(GATEWAY / "deploy-gateway.sh")],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    commands = command_log.read_text()
    assert "update-gateway-target" in commands
    assert "--target-id AbCdEf1234" in commands
    assert "create-gateway-target" not in commands
    assert "accountId=" not in commands
    state = state_file.read_text()
    assert '"gateway_target_id": "AbCdEf1234"' in state


def test_full_deploy_fails_if_tools_are_not_discoverable():
    script = (GATEWAY / "deploy-all.sh").read_text()
    assert '"$SCRIPT_DIR/verify-gateway.sh"' in script


def test_attendee_shell_scripts_parse():
    scripts = [
        ROOT / "coding-agents" / "deploy-prebuilt.sh",
        GATEWAY / "config.sh",
        GATEWAY / "deploy-credential.sh",
        GATEWAY / "deploy-gateway.sh",
        GATEWAY / "deploy-all.sh",
        GATEWAY / "verify-gateway.sh",
        ROOT / "coding-agents" / "claude-code-validator" / "run.sh",
        ROOT / "coding-agents" / "opencode" / "run.sh",
        ROOT / "usecase-sample-to-mcp" / "deploy" / "deploy.sh",
    ]
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)
