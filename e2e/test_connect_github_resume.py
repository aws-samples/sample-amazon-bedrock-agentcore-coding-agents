"""Exercise the real shell entrypoint's recovery routing without a GitHub call."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def entrypoint(tmp_path):
    scripts = tmp_path / "coding-agents"
    gateway = scripts / "gateway_mcp"
    gateway.mkdir(parents=True)
    wrapper = scripts / "connect-github.sh"
    shutil.copy2(ROOT / "coding-agents/connect-github.sh", wrapper)
    checkpoint = gateway / "saved-checkpoint"
    checkpoint.write_text("existing setup must survive a recovery error\n")
    calls = tmp_path / "helper-calls.json"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    python = binaries / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['AUDIT_HELPER_CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
        "# The helper refuses recovery; the wrapper must propagate this result.\n"
        "raise SystemExit(73)\n"
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "GITHUB_REPO": "owner/workshop",
        "AUDIT_HELPER_CALLS": str(calls),
    }
    return wrapper, gateway, checkpoint, calls, env


@pytest.mark.parametrize("mode", ["--resume", "--restart"])
@pytest.mark.parametrize("saved_env", [False, True])
def test_recovery_mode_reaches_the_manifest_helper_and_preserves_its_failure(
        entrypoint, mode, saved_env):
    wrapper, gateway, checkpoint, calls, env = entrypoint
    if saved_env:
        (gateway / "github-app.env").write_text(
            "GITHUB_APP_WEBHOOK_SECRET=fixture-only-not-a-real-secret\n")
    original = checkpoint.read_bytes()
    result = subprocess.run(
        ["bash", str(wrapper), mode], env=env, capture_output=True,
        text=True, timeout=10)
    assert result.returncode == 73
    assert json.loads(calls.read_text()) == [
        "create-github-app.py", mode, "--repo", "owner/workshop"]
    assert checkpoint.read_bytes() == original
    assert "Connected." not in result.stdout


def test_conflicting_recovery_options_stop_before_touching_the_saved_setup(entrypoint):
    wrapper, _gateway, checkpoint, calls, env = entrypoint
    original = checkpoint.read_bytes()
    result = subprocess.run(
        ["bash", str(wrapper), "--resume", "--restart", "owner/workshop"],
        env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not calls.exists()
    assert checkpoint.read_bytes() == original
