"""Run all shipped entrypoints: failed initialization must never serve health."""
import json
from pathlib import Path
import sys
import time

import pytest

from e2e.collector_fixture import collector_environment, run_entrypoint

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("claude-code", "claude-code-validator", "codex", "kiro", "opencode")


@pytest.fixture
def startup(tmp_path):
    with collector_environment(tmp_path) as (collector_env, state, probes):
        home = tmp_path / "home"
        home.mkdir()
        config = tmp_path / "opencode.json"
        config.write_text('{"username":"retained"}\n')
        env = {
            **collector_env,
            "HOME": str(home), "AWS_REGION": "us-east-1",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": str(tmp_path / "no-aws-config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-credentials"),
            "OPENCODE_CONFIG": str(config),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        admitted = tmp_path / "health-command-admitted"
        args = [sys.executable, "-c",
                f"from pathlib import Path; Path({str(admitted)!r}).write_text('ready')"]
        yield env, state, probes, admitted, args


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("failure", ("missing_binary", "missing_config", "exit", "unhealthy", "stalled"))
def test_failed_collector_never_admits_health(startup, role, failure):
    env, state, probes, admitted, args = startup
    env["OTELCOL_STARTUP_TIMEOUT_S"] = "2"
    if failure == "missing_binary":
        env["OTELCOL_BIN"] += ".absent"
    elif failure == "missing_config":
        env["OTELCOL_CONFIG"] += ".absent"
    elif failure == "exit":
        env["TEST_COLLECTOR_MODE"] = "exit"
    else:
        state["health"] = failure
    started = time.monotonic()
    result = run_entrypoint(ROOT / "coding-agents" / role / "entrypoint.sh", env, args)
    assert result.returncode != 0
    assert "COLLECTOR_STARTUP_ERROR" in result.stderr
    assert not admitted.exists()
    assert time.monotonic() - started < 7, "the short startup budget must bound stalled I/O too"
    if failure == "stalled":
        assert state["connected"].is_set(), "exercise a peer that accepted real curl's connection"
        calls = [json.loads(line) for line in probes.read_text().splitlines()]
        assert calls
        for call in calls:
            assert float(call[call.index("--connect-timeout") + 1]) <= 1
            assert float(call[call.index("--max-time") + 1]) <= 2


@pytest.mark.parametrize("role", ROLES)
def test_ready_collector_admits_command_and_preserves_its_exit(startup, role):
    env, _, _, admitted, args = startup
    args[-1] += "; raise SystemExit(23)"
    result = run_entrypoint(ROOT / "coding-agents" / role / "entrypoint.sh", env, args)
    assert admitted.read_text() == "ready", result.stderr
    assert result.returncode == 23
    if role == "opencode":
        config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
        assert config["provider"]["amazon-bedrock"]["options"]["region"] == "us-east-1"
        assert config["username"] == "retained"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("budget", ("0", "61", "120", "-1", "1.5"))
def test_startup_budget_cannot_disable_or_extend_the_deadline(startup, role, budget):
    env, _, _, admitted, args = startup
    env["OTELCOL_STARTUP_TIMEOUT_S"] = budget
    result = run_entrypoint(ROOT / "coding-agents" / role / "entrypoint.sh", env, args)
    assert result.returncode != 0
    assert "integer from 1 to 60" in result.stderr
    assert not admitted.exists()


def test_opencode_rewrite_failure_is_fatal(startup, tmp_path):
    env, _, _, admitted, args = startup
    # A directory cannot become a JSON file, regardless of the test user's UID.
    env["OPENCODE_CONFIG"] = str(tmp_path)
    result = run_entrypoint(ROOT / "coding-agents/opencode/entrypoint.sh", env, args)
    assert result.returncode != 0
    assert "OPENCODE_STARTUP_ERROR" in result.stderr
    assert not admitted.exists()


@pytest.mark.parametrize(("role", "binary"), (
    ("claude-code", "claude"), ("claude-code-validator", "claude"),
    ("codex", "codex"), ("kiro", "kiro-cli-chat"),
))
def test_wrong_cli_version_never_admits_health(startup, role, binary):
    env, state, _, admitted, args = startup
    state["cli_binaries"][binary].write_text("#!/bin/sh\nprintf '%s\\n' 'wrong 999.0.0'\n")
    result = run_entrypoint(ROOT / "coding-agents" / role / "entrypoint.sh", env, args)
    assert result.returncode != 0
    assert "CLI toolchain verification failed" in result.stderr
    assert "version mismatch" in result.stderr
    assert not admitted.exists()


@pytest.mark.parametrize(("role", "binary"), (
    ("claude-code", "claude"), ("claude-code-validator", "claude"),
    ("codex", "codex"), ("kiro", "kiro-cli-chat"),
))
def test_collector_exiting_during_successful_version_check_never_admits_health(
        startup, tmp_path, role, binary):
    env, state, _, admitted, args = startup
    cli = "claude-code" if role == "claude-code-validator" else role
    manifest = json.loads((ROOT / "coding-agents/cli-versions.json").read_text())
    version = manifest["clis"][cli]["version"]
    stopped = tmp_path / "collector-stopped-by-version-probe"
    state["cli_binaries"][binary].write_text(f"#!{sys.executable}\n" + f"""
import os, pathlib, signal, time
ready = pathlib.Path(os.environ["TEST_COLLECTOR_READY"])
deadline = time.monotonic() + 3
while not ready.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("fixture collector did not start")
    time.sleep(0.01)
pid = int(ready.read_text())
os.kill(pid, signal.SIGTERM)
while True:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        break
    if time.monotonic() >= deadline:
        raise RuntimeError("fixture collector did not exit")
    time.sleep(0.01)
pathlib.Path({str(stopped)!r}).write_text(str(pid))
print({f"{binary} {version}"!r})
""")
    result = run_entrypoint(ROOT / "coding-agents" / role / "entrypoint.sh", env, args)
    assert stopped.exists(), result.stderr
    assert not admitted.exists(), "a dead collector must not admit the health command"
    assert result.returncode != 0, "a dead collector must not admit the health command"
    assert "COLLECTOR_STARTUP_ERROR" in result.stderr
