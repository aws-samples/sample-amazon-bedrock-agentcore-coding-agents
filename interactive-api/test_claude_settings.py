"""Exercise Claude session settings through real shells, without model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).absolute().parents[1]
_CHILD = """
import json, os, time
from pathlib import Path
import interactive_api as ia

options = json.loads(os.environ["CLAUDE_SESSION_TEST_OPTIONS"])
role_id = options["role"]
code, catalog = ia.dispatch("GET", "/api/agents", None)
assert code == 200, catalog
code, agent = ia.dispatch("POST", "/api/agents/deploy", {
    "agent_id": role_id, "model": options.get("selected_model"),
})
assert code == 202, agent
code, session = ia.dispatch("POST", "/api/sessions", {"agent_id": role_id})
assert code == 201, session
sid = session["session_id"]
record_path = Path(os.environ["CLAUDE_SESSION_TEST_RECORD"])
try:
    if options.get("saved_effort"):
        # A prior CLI session has saved a native effort preference. Opening the
        # PTY again must not replace it with the deployment's initial default.
        code, previous = ia.dispatch("POST", f"/api/sessions/{sid}/input", {"input": "claude"})
        assert code == 200, previous
        record_path.unlink()
        code, saved = ia.dispatch("POST", f"/api/sessions/{sid}/file", {
            "path": ".claude/settings.json",
            "content": json.dumps({"effortLevel": options["saved_effort"],
                                  "language": "Korean"}),
        })
        assert code == 200, saved
    if options["mode"] == "pty":
        code, opened = ia.dispatch("POST", f"/api/sessions/{sid}/pty", {"open": True})
        assert code == 200, opened
        code, output = ia.dispatch("POST", f"/api/sessions/{sid}/pty", {"input": "claude\\n"})
        assert code == 200, output
        deadline = time.monotonic() + 5
        while not record_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert record_path.is_file(), output
    else:
        code, output = ia.dispatch("POST", f"/api/sessions/{sid}/input", {"input": "claude"})
        assert code == 200, output
        assert record_path.is_file(), output
    record = json.loads(record_path.read_text())
    role = ia._roles.get(role_id)
    record["role"] = {"kind": role.kind, "hidden": role.hidden,
                      "default_model": role.default_model}
    record["catalog_models"] = {row["agent_id"]: row["model"] for row in catalog["agents"]}
    print(json.dumps(record))
finally:
    ia.dispatch("DELETE", f"/api/sessions/{sid}", None)
"""


@pytest.fixture
def session_cli(tmp_path):
    """The API starts its real shell; only the Claude executable is replaced."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    recorder = binaries / "claude"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "from pathlib import Path\n"
        "settings = Path.home() / '.claude' / 'settings.json'\n"
        "record = {'model': os.environ.get('ANTHROPIC_MODEL'),\n"
        "          'bedrock': os.environ.get('CLAUDE_CODE_USE_BEDROCK'),\n"
        "          'workshop_effort': os.environ.get('WORKSHOP_CLAUDE_EFFORT'),\n"
        "          'forced_effort': os.environ.get('CLAUDE_CODE_EFFORT_LEVEL'),\n"
        "          'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'),\n"
        "          'settings': json.loads(settings.read_text())}\n"
        "target = Path(os.environ['CLAUDE_SESSION_TEST_RECORD'])\n"
        "pending = target.with_suffix('.pending')\n"
        "pending.write_text(json.dumps(record))\n"
        "pending.replace(target)\n"
    )
    recorder.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    host_config = fake_home / "host-config"
    host_config.mkdir()
    inherited = host_config / "settings.json"
    inherited.write_text('{"model":"host-model","effortLevel":"max"}\n')
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AWS_", "AGENTCORE_", "ANTHROPIC_", "CLAUDE_",
                               "WORKSHOP_MODEL", "WORKSHOP_CLAUDE_"))
        and key not in ("BASH_ENV", "ENV", "GITHUB_TOKEN", "KIRO_API_KEY")
    }
    env.update({
        "HOME": str(fake_home),
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(ROOT / "interactive-api"),
        "AWS_REGION": "us-west-2",
        "AWS_EC2_METADATA_DISABLED": "true",
        "WORKSHOP_E2E_LIVE": "0",
        "WORKSHOP_ROLES": "claude-code,codex,claude-code-validator",
        "WORKSHOP_RUNS_DIR": str(tmp_path / "runs"),
        "WORKSHOP_CODING_AGENTS_DIR": str(tmp_path / "coding-agents"),
        "CLAUDE_CONFIG_DIR": str(host_config),
        "CLAUDE_SESSION_TEST_RECORD": str(tmp_path / "record.json"),
    })

    def run(role="claude-code", mode="input", overrides=None, **options):
        result = subprocess.run(
            [sys.executable, "-c", _CHILD], cwd=ROOT, capture_output=True,
            text=True, timeout=15,
            env={**env, **(overrides or {}), "CLAUDE_SESSION_TEST_OPTIONS": json.dumps({
                "role": role, "mode": mode, **options,
            })},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert inherited.read_text() == '{"model":"host-model","effortLevel":"max"}\n'
        return json.loads(result.stdout)

    return run


@pytest.mark.parametrize("mode", ["input", "pty"])
@pytest.mark.parametrize("role,model,effort,kind,hidden", [
    ("claude-code", "us.anthropic.claude-opus-4-6-v1", "high", "builder", False),
    ("claude-code-validator", "us.anthropic.claude-opus-4-6-v1", "xhigh", "checker", True),
])
def test_session_uses_its_own_registry_defaults(
        session_cli, mode, role, model, effort, kind, hidden):
    record = session_cli(role, mode)
    assert record["model"] == record["settings"]["model"] == model
    assert record["workshop_effort"] == record["settings"]["effortLevel"] == effort
    assert record["bedrock"] == "1"
    assert record["config_dir"] is None
    assert record["forced_effort"] is None  # --effort and /effort stay available
    assert record["role"] == {"kind": kind, "hidden": hidden, "default_model": model}


@pytest.mark.parametrize("mode", ["input", "pty"])
@pytest.mark.parametrize("role", ["claude-code", "claude-code-validator"])
@pytest.mark.parametrize("effort", ["max", ""])
def test_backend_stack_model_is_isolated_from_validator_with_shared_effort(
        session_cli, mode, role, effort):
    record = session_cli(role, mode, {
        "WORKSHOP_CLAUDE_MODEL": "configured-stack-model",
        "WORKSHOP_CLAUDE_EFFORT": effort,
    })
    expected = ("configured-stack-model" if role == "claude-code"
                else "us.anthropic.claude-opus-4-6-v1")
    assert record["model"] == record["settings"]["model"] == expected
    assert record["workshop_effort"] == effort
    assert record["settings"].get("effortLevel") == (effort or None)
    assert record["forced_effort"] is None


@pytest.mark.parametrize("mode", ["input", "pty"])
@pytest.mark.parametrize("role,model,effort", [
    ("claude-code", "us.anthropic.claude-opus-5", "high"),
    ("claude-code-validator", "us.anthropic.claude-opus-4-6-v1", "xhigh"),
])
def test_cfn_backend_default_keeps_restored_shelf_and_session_models_separate(
        session_cli, mode, role, model, effort):
    record = session_cli(role, mode, {
        "WORKSHOP_CLAUDE_MODEL": "us.anthropic.claude-opus-5",
    })
    assert list(record["catalog_models"]) == [
        "claude-code", "codex", "claude-code-validator",
    ]
    assert record["catalog_models"]["claude-code"] == "us.anthropic.claude-opus-5"
    assert record["catalog_models"]["claude-code-validator"] == "us.anthropic.claude-opus-4-6-v1"
    assert record["model"] == record["settings"]["model"] == model
    assert record["workshop_effort"] == record["settings"]["effortLevel"] == effort


@pytest.mark.parametrize("mode", ["input", "pty"])
def test_restored_validator_model_override_reaches_both_session_paths(session_cli, mode):
    selected = "us.anthropic.claude-sonnet-4-6"
    record = session_cli("claude-code-validator", mode, {
        "WORKSHOP_CLAUDE_MODEL": "us.anthropic.claude-opus-5",
        "WORKSHOP_MODEL_CLAUDE_CODE_VALIDATOR": selected,
    })
    assert record["model"] == record["settings"]["model"] == selected
    assert record["workshop_effort"] == record["settings"]["effortLevel"] == "xhigh"
    assert record["catalog_models"]["claude-code"] == "us.anthropic.claude-opus-5"


@pytest.mark.parametrize("role", ["claude-code", "claude-code-validator"])
@pytest.mark.parametrize("layer", ["stack", "shelf", "generic", "role"])
def test_model_selection_precedence_reaches_the_command(session_cli, role, layer):
    overrides = {"WORKSHOP_CLAUDE_MODEL": "stack-model"}
    options = {}
    if layer in ("shelf", "generic", "role"):
        options["selected_model"] = "shelf-model"
    if layer in ("generic", "role"):
        overrides["WORKSHOP_MODEL"] = "generic-model"
    if layer == "role":
        overrides[f"WORKSHOP_MODEL_{role.replace('-', '_').upper()}"] = "role-model"
    # A backend-specific override must never retarget the restored checker.
    if role == "claude-code-validator":
        overrides["WORKSHOP_MODEL_CLAUDE_CODE"] = "backend-only-model"
    record = session_cli(role, overrides=overrides, **options)
    expected = ("us.anthropic.claude-opus-4-6-v1"
                if role == "claude-code-validator" and layer == "stack"
                else f"{layer}-model")
    assert record["model"] == record["settings"]["model"] == expected


@pytest.mark.parametrize("role", ["claude-code", "claude-code-validator"])
def test_reopening_a_pty_keeps_the_users_native_effort_selection(session_cli, role):
    record = session_cli(role, "pty", saved_effort="low")
    assert record["settings"] == {"effortLevel": "low", "language": "Korean"}
    assert record["forced_effort"] is None
