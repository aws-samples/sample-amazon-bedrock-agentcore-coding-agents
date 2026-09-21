"""Execute Kiro's launcher with a stripped PATH and isolated Python environments."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import venv

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "coding-agents/kiro/run.sh"
BUNDLED_PYTHON = "/opt/workshop-python/bin/python3"


def python_environment(path, *, with_identity):
    """No pip/network: only the selected interpreter gets the Identity boundary."""
    venv.EnvBuilder(with_pip=False).create(path)
    executable = path / "bin/python3"
    site = Path(subprocess.check_output(
        [str(executable), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
    ).strip())
    (site / "sitecustomize.py").write_text("""
import json, os, sys
if os.environ.get("KIRO_TEST_PYTHON_LOG"):
    with open(os.environ["KIRO_TEST_PYTHON_LOG"], "a") as stream:
        stream.write(json.dumps({"executable": sys.executable, "argv": sys.argv}) + "\\n")
""")
    if with_identity:
        (site / "botocore").mkdir()
        (site / "botocore/__init__.py").write_text("")
        (site / "botocore/config.py").write_text("""
class Config:
    def __init__(self, **kwargs):
        self.options = kwargs
""")
        (site / "boto3.py").write_text("""
import json, os
def record(method, fields):
    with open(os.environ["KIRO_TEST_IDENTITY_LOG"], "a") as stream:
        stream.write(json.dumps({"method": method, "fields": fields}) + "\\n")
class Identity:
    def get_workload_access_token(self, **kwargs):
        record("get_workload_access_token", kwargs)
        if os.environ.get("KIRO_TEST_VAULT_RESULT") == "error":
            raise RuntimeError("inert Identity boundary failure")
        return {"workloadAccessToken": "inert-workload-token"}
    def get_resource_api_key(self, **kwargs):
        record("get_resource_api_key", kwargs)
        key = "" if os.environ.get("KIRO_TEST_VAULT_RESULT") == "empty" else "inert-vault-key"
        return {"apiKey": key}
def client(service, *, region_name, config):
    assert service == "bedrock-agentcore"
    assert region_name == "us-east-1"
    assert config.options == {
        "connect_timeout": 5, "read_timeout": 10, "retries": {"max_attempts": 2},
    }
    return Identity()
""")
    return executable


@pytest.mark.parametrize("bundled", [True, False], ids=["runtime-bundle", "host-path-fallback"])
@pytest.mark.parametrize("key_source", ["vault", "environment"])
def test_launcher_selects_python_with_stripped_exec_path(tmp_path, bundled, key_source):
    system = python_environment(tmp_path / "system python", with_identity=not bundled)
    bundled_path = tmp_path / "bundled python"
    selected = (
        python_environment(bundled_path, with_identity=True) if bundled else system
    )
    bundle_candidate = bundled_path / "bin/python3"
    home = tmp_path / "private home"
    settings = home / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"attendee.preference": "keep"}))
    tools = tmp_path / "tools"
    tools.mkdir()
    report = tmp_path / "child.json"
    child = tools / "kiro-cli"
    child.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote("""
import json, os, pathlib, sys
pathlib.Path(os.environ["KIRO_TEST_CHILD"]).write_text(json.dumps({
    "args": sys.argv[1:],
    "cwd": os.getcwd(),
    "key_matches": os.environ["KIRO_API_KEY"] == os.environ["KIRO_TEST_EXPECTED_KEY"],
    "unrelated_pid1_value": os.environ.get("UNRELATED_PID1_VALUE"),
}))
""") + ' "$@"\n'
    )
    child.chmod(0o755)
    # Relocate fixed container paths only; execute the real launcher logic.
    pid1 = tmp_path / "pid1-environ"
    pid1.write_bytes(b"AWS_REGION=us-east-1\0UNRELATED_PID1_VALUE=must-not-inherit\0")
    source = LAUNCHER.read_text()
    source = source.replace(
        '"' + BUNDLED_PYTHON + '"', shlex.quote(str(bundle_candidate)),
    )
    source = source.replace('export HOME="/home/agent"', "export HOME=" + shlex.quote(str(home)))
    source = source.replace("/proc/1/environ", shlex.quote(str(pid1)))
    source = source.replace(
        "/mnt/s3files/.kiro/steering/validator.md", str(tmp_path / "no-staged-steering"),
    )
    launcher = tmp_path / "run.sh"
    launcher.write_text(source)
    python_log = tmp_path / "python.jsonl"
    identity_log = tmp_path / "identity.jsonl"
    env = {
        # Deliberately omit the bundled environment from AgentCore exec's PATH.
        "PATH": os.pathsep.join((str(tools), str(system.parent), os.defpath)),
        "HOME": str(home),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "KIRO_TEST_PYTHON_LOG": str(python_log),
        "KIRO_TEST_IDENTITY_LOG": str(identity_log),
        "KIRO_TEST_CHILD": str(report),
        "KIRO_TEST_EXPECTED_KEY": "inert-vault-key" if key_source == "vault" else "inert-env-key",
        "WORKSHOP_CLI_HELPER": str(ROOT / "coding-agents/cli_versions.py"),
    }
    if key_source == "environment":
        env["KIRO_API_KEY"] = env["KIRO_TEST_EXPECTED_KEY"]
    result = subprocess.run(
        ["/bin/bash", str(launcher), "chat", "Describe your role"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    executions = [json.loads(line) for line in python_log.read_text().splitlines()]
    assert len(executions) == (2 if key_source == "vault" else 1)
    assert {row["executable"] for row in executions} == {str(selected)}
    assert executions[-1]["argv"][1:3] == ["configure", "--cli"]
    calls = (
        [json.loads(line) for line in identity_log.read_text().splitlines()]
        if identity_log.exists() else []
    )
    assert [call["method"] for call in calls] == (
        ["get_workload_access_token", "get_resource_api_key"] if key_source == "vault" else []
    )
    child_result = json.loads(report.read_text())
    assert child_result == {
        "args": ["chat", "--no-interactive", "--trust-all-tools", "Describe your role"],
        "cwd": str(home),
        "key_matches": True,
        "unrelated_pid1_value": None,
    }
    configured = json.loads(settings.read_text())
    assert configured["attendee.preference"] == "keep"
    assert configured["app.disableAutoupdates"] is True


@pytest.mark.parametrize("dispatch", ["headless", "interactive"])
@pytest.mark.parametrize(
    ("bundled", "auth_result"),
    [(True, "success"), (False, "success"), (True, "empty"), (True, "error")],
    ids=["runtime-bundle", "host-path-fallback", "empty-key", "identity-error"],
)
def test_dispatch_vault_prelude_executes_with_stripped_path(
    tmp_path, monkeypatch, dispatch, bundled, auth_result,
):
    monkeypatch.syspath_prepend(str(ROOT / "orchestrator"))
    import runtime_exec

    system = python_environment(tmp_path / "system python", with_identity=not bundled)
    bundled_path = tmp_path / "bundled python"
    selected = python_environment(bundled_path, with_identity=True) if bundled else system
    bundle_candidate = bundled_path / "bin/python3"
    role = runtime_exec._role("kiro")
    prelude = runtime_exec._vault_key_prelude(role, "us-east-1")
    if dispatch == "headless":
        generated = runtime_exec._build_command(
            role.id, "check", "run_1/work/validate", None,
            role.default_model, "us-east-1", "python-probe",
        )
    else:
        generated = runtime_exec._interactive_dispatch_commands(
            role.id, "run_1/work/validate", role.default_model,
            "us-east-1", "python-probe", "s3://fixture/source.tar.gz",
        )["launch"]
    assert generated.count(prelude) == 1
    assert "\n" not in prelude and generated.count("\n") == 1
    assert "/proc/1/environ" not in prelude
    # The generated dispatch itself contains AWS/Git work outside this boundary.
    # Execute its exact Vault prelude, relocating only the fixed interpreter path.
    executable_prelude = prelude.replace(
        BUNDLED_PYTHON, shlex.quote(str(bundle_candidate)),
    )
    python_log = tmp_path / "python.jsonl"
    identity_log = tmp_path / "identity.jsonl"
    report = tmp_path / "cli-started.json"
    observe_cli = (
        shlex.quote(sys.executable) + " -c " + shlex.quote(
            "import json,os,pathlib; "
            "pathlib.Path(os.environ['KIRO_TEST_CHILD']).write_text(json.dumps("
            "{'key_matches':os.environ['KIRO_API_KEY']=='inert-vault-key'}))"
        )
    )
    env = {
        "PATH": str(system.parent) + os.pathsep + os.defpath,
        "HOME": str(tmp_path),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "BOTO_CONFIG": "/dev/null",
        "KIRO_TEST_PYTHON_LOG": str(python_log),
        "KIRO_TEST_IDENTITY_LOG": str(identity_log),
        "KIRO_TEST_VAULT_RESULT": auth_result,
        "KIRO_TEST_CHILD": str(report),
    }
    result = subprocess.run(
        ["/bin/bash", "-c", executable_prelude + observe_cli],
        env=env, capture_output=True, text=True, timeout=15,
    )
    executions = [json.loads(line) for line in python_log.read_text().splitlines()]
    assert len(executions) == 1
    assert executions[0]["executable"] == str(selected)
    calls = [json.loads(line) for line in identity_log.read_text().splitlines()]
    assert [call["method"] for call in calls] == (
        ["get_workload_access_token"] if auth_result == "error"
        else ["get_workload_access_token", "get_resource_api_key"]
    )
    workload, provider = role.vault_names()
    assert calls[0]["fields"] == {"workloadName": workload}
    if len(calls) == 2:
        assert calls[1]["fields"] == {
            "workloadIdentityToken": "inert-workload-token",
            "resourceCredentialProviderName": provider,
        }
    assert "inert-vault-key" not in result.stdout + result.stderr
    assert "inert-workload-token" not in result.stdout + result.stderr
    if auth_result == "success":
        assert result.returncode == 0, result.stderr
        assert json.loads(report.read_text()) == {"key_matches": True}
    else:
        assert result.returncode == 1
        assert not report.exists(), "A failed Vault fetch must not reach the CLI"
        assert "[auth] ERROR: no KIRO_API_KEY" in result.stderr
