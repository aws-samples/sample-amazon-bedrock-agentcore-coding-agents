"""Execute the launcher against temporary paths and observe its child process."""
import json
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def launch(tmp_path, *, staged=True, explicit_worktree=False):
    private_home = tmp_path / "private home"
    shared = tmp_path / "shared workspace"
    private_steering = private_home / ".kiro/steering/validator.md"
    staged_steering = shared / ".kiro/steering/validator.md"
    private_steering.parent.mkdir(parents=True)
    staged_steering.parent.mkdir(parents=True)
    private_steering.write_text("Baked checker instructions\n")
    if staged:
        staged_steering.write_text("Attendee-edited checker instructions\n")
    (shared / "AGENTS.md").write_text("You are the FRONTEND BUILDER\n")
    (shared / "notes.md").write_text("Shared note remains available\n")
    worktree = tmp_path / "dispatched worktree"
    worktree.mkdir()
    (worktree / ".kiro/steering").mkdir(parents=True)
    (worktree / ".kiro/steering/validator.md").write_text("Dispatched checker instructions\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    child = bin_dir / "kiro-cli"
    child.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
cwd = pathlib.Path.cwd()
record = {
    "cwd": str(cwd),
    "steering": (cwd / ".kiro/steering/validator.md").read_text(),
    "frontend_in_project": (cwd / "AGENTS.md").exists(),
    "args": sys.argv[1:],
}
pathlib.Path(os.environ["WORKSHOP_TEST_REPORT"]).write_text(json.dumps(record))
""")
    child.chmod(0o700)
    # Relocate fixed container paths, leaving the actual launcher's logic intact.
    source = (ROOT / "coding-agents/kiro/run.sh").read_text()
    source = source.replace('export HOME="/home/agent"',
                            "export HOME=" + shlex.quote(str(private_home)))
    source = source.replace("/mnt/s3files/.kiro/steering/validator.md",
                            shlex.quote(str(staged_steering)))
    launcher = tmp_path / "run.sh"
    launcher.write_text(source)
    report = tmp_path / "child.json"
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "GATEWAY_URL": "https://gateway.example",
        "KIRO_API_KEY": "fake-token-for-launcher-test",
        "WORKSHOP_TEST_REPORT": str(report),
    }
    if explicit_worktree:
        env["WORKSHOP_AGENT_WORKDIR"] = str(worktree)
    result = subprocess.run(
        ["bash", str(launcher), "chat", "Describe your role"],
        cwd=shared, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (shared / "AGENTS.md").read_text() == "You are the FRONTEND BUILDER\n"
    assert (shared / "notes.md").read_text() == "Shared note remains available\n"
    assert not (private_home / "AGENTS.md").exists()
    return json.loads(report.read_text()), private_home, worktree, private_steering


def test_manual_shell_reads_staged_checker_without_shared_frontend_context(tmp_path):
    child, private_home, _, steering = launch(tmp_path)
    assert child["cwd"] == str(private_home)
    assert child["steering"] == "Attendee-edited checker instructions\n"
    assert not child["frontend_in_project"]
    assert child["args"] == [
        "chat", "--no-interactive", "--trust-all-tools", "Describe your role"]
    assert steering.read_text() == child["steering"]


def test_unstaged_manual_shell_keeps_baked_checker(tmp_path):
    child, private_home, _, _ = launch(tmp_path, staged=False)
    assert child["cwd"] == str(private_home)
    assert child["steering"] == "Baked checker instructions\n"
    assert not child["frontend_in_project"]


def test_dispatched_worktree_keeps_its_own_context(tmp_path):
    child, _, worktree, private_steering = launch(tmp_path, explicit_worktree=True)
    assert child["cwd"] == str(worktree)
    assert child["steering"] == "Dispatched checker instructions\n"
    assert private_steering.read_text() == "Baked checker instructions\n"
