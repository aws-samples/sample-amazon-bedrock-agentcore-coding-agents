"""Observe Kiro controls at real launcher, registry, and deployment boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from e2e.test_kiro_launcher_context import launch

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("arguments,model,effort", [
    ([], "claude-opus-5", ""),
    (["--model", "claude-opus-4.6"], "claude-opus-4.6", ""),
    (["--effort", "medium"], "claude-opus-5", "medium"),
    (["--model", "claude-opus-4.6", "--effort", "low"],
     "claude-opus-4.6", "low"),
])
def test_bare_and_option_only_launches_open_interactive_chat(
        tmp_path, arguments, model, effort):
    child, private_home, _, _ = launch(tmp_path, arguments=arguments)
    expected = ["chat", "--trust-all-tools"]
    if effort:
        expected += ["--effort", effort]
    assert child["args"] == expected
    settings = json.loads((private_home / ".kiro/settings/cli.json").read_text())
    assert settings["chat.defaultModel"] == model
    assert child["cwd"] == str(private_home)
    assert not child["frontend_in_project"]


@pytest.mark.parametrize("mode", ["chat", "interactive"])
@pytest.mark.parametrize("environment,arguments,inherited,expected", [
    ({}, [], {"WORKSHOP_KIRO_EFFORT": "medium"}, "medium"),
    ({"WORKSHOP_KIRO_EFFORT": "high"}, [], {"WORKSHOP_KIRO_EFFORT": "medium"}, "high"),
    ({"WORKSHOP_KIRO_EFFORT": ""}, [], {"WORKSHOP_KIRO_EFFORT": "medium"}, ""),
    ({"WORKSHOP_KIRO_EFFORT": "high"}, ["--effort", "low"], {}, "low"),
])
def test_named_effort_reaches_manual_and_dispatched_launchers(
        tmp_path, mode, environment, arguments, inherited, expected):
    prompt = "Keep 'quotes' and $(touch must-not-run) as task text"
    args = [*arguments, mode, *([prompt] if mode == "chat" else [])]
    child, _, _, _ = launch(
        tmp_path, environment=environment, arguments=args,
        inherited={**inherited, "UNRELATED_PID1_SETTING": "must-not-inherit"},
        explicit_worktree=mode == "chat")
    actual = child["args"]
    if expected:
        assert actual.count("--effort") == 1
        assert actual[actual.index("--effort") + 1] == expected
    else:
        assert "--effort" not in actual
    if mode == "chat":
        assert actual[-1] == prompt
        assert "--no-interactive" in actual
    else:
        assert "--no-interactive" not in actual
    assert not (tmp_path / "must-not-run").exists()


@pytest.mark.parametrize("effort", ["medium", "", "invalid; touch must-not-run"])
def test_effort_reaches_fresh_registry_or_is_rejected_before_dispatch(effort, tmp_path):
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("WORKSHOP_")}
    env.update(WORKSHOP_KIRO_EFFORT=effort, WORKSHOP_CLAUDE_EFFORT="high",
               PYTHONPATH=str(ROOT / "orchestrator"))
    result = subprocess.run([
        sys.executable, "-c",
        "import json, roles; r=roles.get('kiro'); print(json.dumps({"
        "'command':r.command('PROMPT','','/tmp/work'), 'env':r.env,"
        "'backend':roles.get('claude-code').cli}))"],
        cwd=tmp_path, env=env, text=True, capture_output=True)
    if effort.startswith("invalid"):
        assert result.returncode != 0
        assert "WORKSHOP_KIRO_EFFORT must be" in result.stderr
        assert not result.stdout
        assert not (tmp_path / "must-not-run").exists()
        return
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["env"]["WORKSHOP_KIRO_EFFORT"] == effort
    assert "--effort high" in observed["backend"]
    if effort:
        assert "--effort medium" in observed["command"]
    else:
        assert "--effort" not in observed["command"]
