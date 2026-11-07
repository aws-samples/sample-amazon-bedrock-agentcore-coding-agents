#!/usr/bin/env python3
"""Install the pinned official Claude Code extension for the workshop host user.

Run after code-server and synedra.auto-run-command are installed. The extension
reads the host's existing ~/.claude/settings.json for Bedrock model and region.
Use --check to inspect installation and sidebar settings without changing them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import zipfile


MANIFEST = Path(__file__).with_name("cli-versions.json")
SIDEBAR_COMMAND = "claude-vscode.sidebar.open"
SETTINGS = {
    "extensions.autoUpdate": "off",
    "extensions.autoCheckUpdates": False,
    "claudeCode.preferredLocation": "sidebar",
    "claudeCode.useTerminal": False,
    "claudeCode.disableLoginPrompt": True,
    "claudeCode.initialPermissionMode": "default",
    "claudeCode.allowDangerouslySkipPermissions": False,
}


def extension_spec():
    manifest = json.loads(MANIFEST.read_text())
    extension = manifest["vscode_extensions"]["claude-code"]
    if extension["version"] != manifest["clis"]["claude-code"]["version"]:
        raise ValueError("Claude Code extension and CLI pins must match")
    return extension


def read_settings(path):
    settings = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("VS Code settings must be an object")
    rules = settings.get("auto-run-command.rules", [])
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        raise ValueError("auto-run-command.rules must be a list of command objects")
    return settings


def sidebar_settings(settings):
    updated = dict(settings, **SETTINGS)
    rules = list(settings.get("auto-run-command.rules", []))
    if not any(rule.get("command") == SIDEBAR_COMMAND for rule in rules):
        rules.append({"command": SIDEBAR_COMMAND})
    updated["auto-run-command.rules"] = rules
    return updated


def verify_archive(path, artifact, extension):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if path.stat().st_size != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
        raise RuntimeError("Claude Code VSIX checksum/size mismatch; installation refused")
    with zipfile.ZipFile(path) as archive:
        package = json.loads(archive.read("extension/package.json"))
    identity = f'{package.get("publisher", "")}.{package.get("name", "")}'.lower()
    if identity != extension["id"] or package.get("version") != extension["version"]:
        raise RuntimeError("Claude Code VSIX identity/version mismatch")


def verify_installation(extension, *, code_server="code-server"):
    result = subprocess.run(
        [code_server, "--list-extensions", "--show-versions"],
        check=True, capture_output=True, text=True, timeout=60)
    installed = [line.strip().lower() for line in result.stdout.splitlines()
                 if line.strip().lower().startswith(extension["id"] + "@")]
    expected = extension["id"] + "@" + extension["version"]
    if installed != [expected]:
        raise RuntimeError(f"Claude Code extension mismatch: expected {expected}, got {installed}")
    return installed[0]


def install(*, home=None, check=False, code_server="code-server"):
    home = Path(home or Path.home())
    extension = extension_spec()
    settings_path = home / ".local/share/code-server/User/settings.json"
    settings = read_settings(settings_path)
    if check:
        installed = verify_installation(extension, code_server=code_server)
        # VS Code migrates the old boolean to "off" when the first window opens.
        # Both forms disable updates; checking must not modify the user's file.
        checked = dict(settings)
        if checked.get("extensions.autoUpdate") is False:
            checked["extensions.autoUpdate"] = "off"
        if sidebar_settings(checked) != checked:
            raise RuntimeError("Claude Code sidebar settings are not configured")
        return {"extension": installed, "sidebar": "right", "permission_mode": "default"}

    machine = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
    target = platform.system().lower() + "-" + machine
    if target not in extension["archives"]:
        raise RuntimeError("No pinned Claude Code VSIX for " + target)
    artifact = extension["archives"][target]
    expected_url = (
        "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/"
        "anthropic/vsextensions/claude-code/" + extension["version"] + "/vspackage?"
    )
    if not artifact["url"].startswith(expected_url):
        raise ValueError("Use the pinned official Visual Studio Marketplace package")
    with tempfile.TemporaryDirectory(prefix="workshop-claude-extension-") as directory:
        archive = Path(directory) / "claude-code.vsix"
        # Marketplace may return Content-Encoding: gzip even without a request
        # header. --compressed verifies the actual VSIX, not its transport bytes.
        subprocess.run(
            ["curl", "-fLsS", "--compressed", "--retry", "3",
             "--connect-timeout", "10", "--max-time", "120",
             artifact["url"], "--output", str(archive)],
            check=True, timeout=540)
        verify_archive(archive, artifact, extension)
        subprocess.run(
            [code_server, "--install-extension", str(archive), "--force"],
            check=True, timeout=180)
    installed = verify_installation(extension, code_server=code_server)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".settings-", dir=settings_path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(sidebar_settings(settings), output, indent=2)
            output.write("\n")
        os.replace(temporary, settings_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"extension": installed, "sidebar": "right", "permission_mode": "default",
            "vsix_sha256": artifact["sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(check=args.check), indent=2))


if __name__ == "__main__":
    main()
