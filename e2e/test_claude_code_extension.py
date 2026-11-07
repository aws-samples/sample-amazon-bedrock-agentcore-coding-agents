"""Installation must reject unverified packages and report the real installed version."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zipfile

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "coding-agents/install_claude_code_extension.py"
spec = importlib.util.spec_from_file_location("workshop_claude_extension", SOURCE)
extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extension)


def test_archive_checksum_and_identity_are_checked_before_installing(tmp_path):
    package = tmp_path / "extension.vsix"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("extension/package.json", json.dumps(
            {"publisher": "Anthropic", "name": "claude-code", "version": "2.1.304"}))
    artifact = {"bytes": package.stat().st_size,
                "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}
    pinned = extension.extension_spec()
    extension.verify_archive(package, artifact, pinned)
    with pytest.raises(RuntimeError, match="identity/version"):
        extension.verify_archive(package, artifact, dict(pinned, version="0.0.0"))
    package.write_bytes(package.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="checksum/size"):
        extension.verify_archive(package, artifact, pinned)


@pytest.mark.parametrize("output", ["", "anthropic.claude-code@0.0.0\n",
                                   "anthropic.claude-code@2.1.304-preview\n"])
def test_successful_command_without_exact_installed_version_is_a_failure(monkeypatch, output):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=output))
    with pytest.raises(RuntimeError, match="extension mismatch"):
        extension.verify_installation(extension.extension_spec())


def test_check_is_read_only_and_requires_sidebar_settings(tmp_path, monkeypatch):
    path = tmp_path / ".local/share/code-server/User/settings.json"
    path.parent.mkdir(parents=True)
    original = {"editor.fontSize": 18,
                "auto-run-command.rules": [{"command": "workbench.action.terminal.new"}]}
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout="Anthropic.claude-code@2.1.304\n"))
    with pytest.raises(RuntimeError, match="sidebar settings"):
        extension.install(home=tmp_path, check=True)
    assert path.read_bytes() == before
    configured = extension.sidebar_settings(original)
    assert configured["editor.fontSize"] == 18
    assert configured["auto-run-command.rules"][0] == original["auto-run-command.rules"][0]
    assert extension.sidebar_settings(configured) == configured
    path.write_text(json.dumps(configured))
    before = path.read_bytes()
    assert extension.install(home=tmp_path, check=True)["permission_mode"] == "default"
    assert path.read_bytes() == before


def test_invalid_settings_are_preserved_without_running_installer(tmp_path, monkeypatch):
    path = tmp_path / ".local/share/code-server/User/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"auto-run-command.rules": "not a list"}')
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("No external command may run with invalid settings")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="list of command objects"):
        extension.install(home=tmp_path)
    assert path.read_bytes() == before
