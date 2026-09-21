#!/usr/bin/env python3
"""Shared CLI pins, ordinary npm installation, and verified Kiro installation.

Host: python3 coding-agents/cli_versions.py install-host --home /home/ubuntu --user ubuntu
Image: python3 /opt/workshop-cli/cli_versions.py install --cli codex --home /home/agent
Read: python3 coding-agents/cli_versions.py get deploy_sdk.boto3

Imports use only the standard library and never create an AWS client. Docker
contexts receive this file and the same manifest, not the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


MANIFEST_PATH = Path(__file__).with_name("cli-versions.json")
# Keep the whole whitespace-delimited token: suffixes are not the stable pin.
VERSION_RE = re.compile(r"(?<!\S)\d+\.\d+\.\d+\S*")
_CODEX_UPDATE_KEY = "check_for_update_on_startup"
_TOML_TOKEN_RE = re.compile(
    r'"""(?:\\[\s\S]|"{1,2}(?!")|[^"\\])*"{3,5}'
    r"|'''(?:'{1,2}(?!')|[^'])*'{3,5}"
    r'|"(?:\\[^\r\n]|[^"\\\r\n])*"'
    r"|'[^'\r\n]*'"
    r"|#[^\r\n]*|[ \t\r]+|\n|[\[\]{}=.,]"
    r"""|[^ \t\r\n"'#\[\]{}=.,]+"""
)


def load_manifest(path=None):
    data = json.loads(Path(path or MANIFEST_PATH).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported CLI manifest schema")
    for name, cli in data["clis"].items():
        if not re.fullmatch(r"\d+\.\d+\.\d+", cli["version"]):
            raise ValueError("An exact CLI version is required: " + name)
    for name, version in data["deploy_sdk"].items():
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("An exact deploy SDK version is required: " + name)
    return data


def manifest_sha256(path=None):
    return hashlib.sha256(Path(path or MANIFEST_PATH).read_bytes()).hexdigest()


def npm_spec(name, manifest=None):
    cli = (manifest or load_manifest())["clis"][name]
    return cli["npm_package"] + "@" + cli["version"]


def sdk_requirements(manifest=None):
    return [name + "==" + version
            for name, version in (manifest or load_manifest())["deploy_sdk"].items()]


def runtime_platform_version(manifest=None):
    return (manifest or load_manifest())["runtime"]["platform_version"]


def require_deploy_sdk(manifest=None):
    """Fail before deployment when the executing Python uses a different SDK."""
    expected = (manifest or load_manifest())["deploy_sdk"]
    observed = {name: importlib.metadata.version(name) for name in expected}
    if observed != expected:
        raise RuntimeError("Deploy SDK mismatch: expected "
                           + json.dumps(expected) + "; observed " + json.dumps(observed))
    return observed


def _run(command, *, env=None, timeout=20, cwd=None, owner=None):
    identity = {}
    if owner and os.geteuid() == 0:
        identity = {"user": owner[0], "group": owner[1], "extra_groups": []}
    elif owner and owner != (os.geteuid(), os.getegid()):
        raise RuntimeError("Run the installer as the selected user or as root")
    return subprocess.run(command, env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout, check=True, **identity)


def _env(home):
    return dict(os.environ, HOME=str(home))


def _command_path(command, home, prefix=None):
    if command.startswith("kiro-cli"):
        candidate = Path(home) / ".local/bin" / command
        return candidate if candidate.exists() else None
    search = str(Path(prefix) / "bin") if prefix else os.environ.get("PATH", "")
    located = shutil.which(command, path=search)
    return Path(located) if located else None


def verify_cli(name, *, home=None, prefix=None, manifest=None):
    """Probe actual executables; never report a requested version as observed."""
    data = manifest or load_manifest()
    cli = data["clis"][name]
    home = Path(home or Path.home())
    checks = []
    for command in [cli["command"], *cli.get("siblings", [])]:
        executable = _command_path(command, home, prefix)
        if not executable or not os.access(executable, os.X_OK):
            raise RuntimeError("Pinned CLI executable is missing: " + command)
        reply = _run([str(executable), "--version"],
                     env=dict(_env(home), **cli.get("update_env", {})))
        output = reply.stdout.strip()
        versions = VERSION_RE.findall(output)
        if versions != [cli["version"]]:
            raise RuntimeError(f"{command} version mismatch: expected {cli['version']}; "
                               f"observed {output!r}. Refusing reuse.")
        checks.append({"command": command, "path": str(executable.resolve()),
                       "version": versions[0], "output": output, "exit_code": reply.returncode})
    return {"cli": name, "expected_version": cli["version"], "executables": checks}


def _write_config(path, text, owner=None):
    """Atomic update preserving a preexisting file's permissions and ownership."""
    path = Path(path)
    if path.is_symlink():
        path = path.resolve(strict=True)
    created = []
    directory = path.parent
    while not directory.exists():
        created.append(directory)
        directory = directory.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, stat.S_IMODE(previous.st_mode) if previous else 0o600)
        identity = (previous.st_uid, previous.st_gid) if previous else owner
        if identity and os.geteuid() == 0:
            os.chown(temporary, *identity)
        if owner and os.geteuid() == 0:
            for directory in created:
                os.chown(directory, *owner)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _merge_json(path, updates, owner=None):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Expected a settings object: " + str(path))
    for key, value in updates.items():
        if isinstance(value, dict):
            current = data.get(key, {})
            if not isinstance(current, dict):
                raise ValueError("Expected a settings object: " + key)
            data[key] = dict(current, **value)
        else:
            data[key] = value
    _write_config(path, json.dumps(data, indent=2) + "\n", owner)


def _toml_statements(text):
    """Find edit boundaries, treating strings/comments as indivisible tokens."""
    position, statement, closing = 0, [], []
    while position < len(text):
        token = _TOML_TOKEN_RE.match(text, position)
        if token is None:
            raise ValueError("Unterminated TOML string; Codex config was not changed")
        position = token.end()
        value = token.group()
        if value == "\n" and not closing:
            if statement:
                yield statement
                statement = []
        elif value[0] in " \t\r\n#":
            continue
        else:
            if value in ("[", "{"):
                closing.append("]" if value == "[" else "}")
            elif value in ("]", "}"):
                if not closing or closing.pop() != value:
                    raise ValueError("Unbalanced TOML brackets; Codex config was not changed")
            statement.append(token)
    if closing:
        raise ValueError("Unclosed TOML brackets; Codex config was not changed")
    if statement:
        yield statement


def _toml_key_parts(tokens):
    """Decode keys only, including quoted/escaped spellings of the owned key."""
    def unescape(match):
        value = match.group(1)
        if value[0] in "uU":
            digits = value[1:]
            if len(digits) != (4 if value[0] == "u" else 8):
                raise ValueError("Invalid TOML key escape")
            codepoint = int(digits, 16)
            if 0xD800 <= codepoint <= 0xDFFF:
                raise ValueError("Invalid TOML key escape")
            return chr(codepoint)
        return {'"': '"', "\\": "\\", "b": "\b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t"}[value]

    if not tokens or len(tokens) % 2 != 1:
        raise ValueError("Invalid TOML key; Codex config was not changed")
    parts = []
    for index, token in enumerate(tokens):
        value = token.group()
        if index % 2:
            if value != ".":
                raise ValueError("Invalid TOML key; Codex config was not changed")
        elif value.startswith(('"""', "'''")):
            raise ValueError("Multiline TOML keys are not allowed")
        elif value.startswith('"'):
            parts.append(re.sub(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)",
                                unescape, value[1:-1]))
        elif value.startswith("'"):
            parts.append(value[1:-1])
        elif re.fullmatch(r"[A-Za-z0-9_-]+", value):
            parts.append(value)
        else:
            raise ValueError("Invalid TOML key; Codex config was not changed")
    return parts


def _codex_update_span(text):
    """Validate the owned root key and return its boolean's character span."""
    root, found = True, None
    for statement in _toml_statements(text):
        values = [token.group() for token in statement]
        if values[0] == "[":
            width = 2 if len(values) > 1 and values[1] == "[" else 1
            if values[-width:] != ["]"] * width:
                raise ValueError("Invalid TOML table; Codex config was not changed")
            parts = _toml_key_parts(statement[width:-width])
            if parts[0] == _CODEX_UPDATE_KEY:
                raise ValueError("Codex update setting must be a top-level boolean")
            root = False
            continue
        if "=" not in values:
            raise ValueError("Invalid TOML assignment; Codex config was not changed")
        equal = values.index("=")
        parts = _toml_key_parts(statement[:equal])
        if root and parts[0] == _CODEX_UPDATE_KEY:
            if len(parts) != 1 or values[equal + 1:] not in (["true"], ["false"]):
                raise ValueError("Codex update setting must be a top-level boolean")
            if found is not None:
                raise ValueError("Duplicate Codex update setting; config was not changed")
            found = statement[equal + 1].span()
    return found


def _disable_codex_updates(text):
    """Replace only a root boolean, then validate the candidate before writing.

    Jammy bootstraps with Python 3.10. The scanner validates the edit boundary,
    duplicate/aliased owned keys and the resulting boolean without dependencies.
    On Python 3.11+, also validate the entire document with stdlib tomllib.
    """
    span = _codex_update_span(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    candidate = (text[:span[0]] + "false" + text[span[1]:] if span else
                 _CODEX_UPDATE_KEY + " = false" + newline + text)
    checked = _codex_update_span(candidate)
    if checked is None or candidate[checked[0]:checked[1]] != "false":
        raise ValueError("Codex update setting validation failed")
    try:
        import tomllib
    except ModuleNotFoundError:
        pass
    else:
        if tomllib.loads(candidate).get(_CODEX_UPDATE_KEY) is not False:
            raise ValueError("Codex update setting validation failed")
    return candidate


def configure_home(name, home, *, owner=None, manifest=None, kiro_model=None):
    data = manifest or load_manifest()
    cli = data["clis"][name]
    home = Path(home)
    if name == "claude-code":
        _merge_json(home / ".claude/settings.json", {"env": cli["update_env"]}, owner)
    elif name == "codex":
        target = home / ".codex/config.toml"
        existing = ""
        if target.exists():
            with target.open(encoding="utf-8", newline="") as stream:
                existing = stream.read()
        _write_config(target, _disable_codex_updates(existing), owner)
    elif name == "kiro":
        settings = dict(cli["update_config"])
        if kiro_model is not None:
            settings.update({"chat.defaultModel": kiro_model,
                             "chat.disableTrustAllConfirmation": True})
        _merge_json(home / ".kiro/settings/cli.json", settings, owner)


def _download_archive(artifact, destination):
    url = artifact["url"]
    if not url.startswith("https://") or not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"]):
        raise ValueError("A versioned HTTPS archive and SHA-256 are required")
    digest = hashlib.sha256()
    count = 0
    deadline = time.monotonic() + 300
    with urllib.request.urlopen(url, timeout=20) as response, Path(destination).open("wb") as stream:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            stream.write(chunk)
            digest.update(chunk)
            count += len(chunk)
            if count > artifact["bytes"] or time.monotonic() > deadline:
                raise RuntimeError("Kiro archive exceeded its size or download time limit")
    if count != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
        raise RuntimeError("Kiro archive checksum/size mismatch; installer was not executed")


def _install_kiro(cli, home, owner=None):
    machine = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
    target = platform.system().lower() + "-" + machine
    if target not in cli["archives"]:
        raise RuntimeError("No pinned Kiro artifact for " + target)
    artifact = cli["archives"][target]
    if "/" + cli["version"] + "/" not in artifact["url"]:
        raise ValueError("Kiro download URL must contain its exact version")
    with tempfile.TemporaryDirectory(prefix="workshop-kiro-") as temporary:
        archive_path = Path(temporary) / "kiro.zip"
        _download_archive(artifact, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                parts = Path(entry.filename).parts
                if (Path(entry.filename).is_absolute() or ".." in parts
                        or stat.S_ISLNK(entry.external_attr >> 16)):
                    raise ValueError("Unsafe Kiro archive member")
            archive.extractall(temporary)
        installer = Path(temporary) / "kirocli/install.sh"
        if not installer.is_file():
            raise RuntimeError("Pinned Kiro archive has no installer")
        # No setup/login prompt, and no key or user configuration is read here.
        command = ["bash", str(installer)]
        if owner and os.geteuid() == 0:
            for directory, _, files in os.walk(temporary):
                os.chown(directory, *owner)
                for name in files:
                    os.chown(Path(directory) / name, *owner)
        installer_env = dict(_env(home), KIRO_CLI_SKIP_SETUP="1")
        installer_env.pop("Q_INSTALL_GLOBAL", None)
        _run(command, env=installer_env,
             timeout=300, cwd=installer.parent, owner=owner)


def install_cli(name, *, home=None, prefix=None, owner=None, manifest=None):
    data = manifest or load_manifest()
    home = Path(home or Path.home())
    cli = data["clis"][name]
    existing = _command_path(cli["command"], home, prefix)
    if existing:
        receipt = verify_cli(name, home=home, prefix=prefix, manifest=data)
    else:
        if "npm_package" in cli:
            command = ["npm", "install", "--global", "--no-audit", "--no-fund"]
            if prefix:
                command.extend(["--prefix", str(prefix)])
            command.append(npm_spec(name, data))
            _run(command, timeout=600)
        else:
            _install_kiro(cli, home, owner)
        receipt = verify_cli(name, home=home, prefix=prefix, manifest=data)
    configure_home(name, home, owner=owner, manifest=data)
    return receipt


def _copy_tracked_tree(source, destination):
    """Copy only this public tree's tracked files, never adjacent run/config data."""
    source, destination = Path(source).resolve(), Path(destination)
    files = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "-z", "--", "."]
    ).decode().split("\0")
    for name in filter(None, files):
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Build inputs must be ordinary tracked files: " + str(path))
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def stage_context(source, destination, manifest_path=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("Build staging destination must not already exist")
    destination.mkdir(parents=True)
    _copy_tracked_tree(source, destination)
    tools = destination / "_toolchain"
    tools.mkdir()
    shutil.copy2(manifest_path or MANIFEST_PATH, tools / MANIFEST_PATH.name)
    shutil.copy2(__file__, tools / Path(__file__).name)
    if source.name == "claude-code":
        skill = source.parents[1] / "harness-skills/skills/backend-engineering"
        _copy_tracked_tree(skill, destination / "skills/backend-engineering")
    return {"manifest_sha256": manifest_sha256(manifest_path), "context": str(destination)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("sdk-requirements")
    sub.add_parser("check-sdk")
    get = sub.add_parser("get")
    get.add_argument("key")
    spec = sub.add_parser("npm-spec")
    spec.add_argument("name")
    stage = sub.add_parser("stage-context")
    stage.add_argument("--source", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    for action in ("install", "verify", "configure"):
        child = sub.add_parser(action)
        child.add_argument("--cli", required=True)
        child.add_argument("--home", type=Path, default=Path.home())
        child.add_argument("--prefix", type=Path)
        child.add_argument("--receipt", type=Path)
        if action == "install":
            child.add_argument("--user")
        if action == "configure":
            child.add_argument("--kiro-model")
    host = sub.add_parser("install-host")
    host.add_argument("--home", type=Path, required=True)
    host.add_argument("--user", required=True)
    host.add_argument("--prefix", type=Path)
    host.add_argument("--sdk-python", type=Path)
    host.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    data = load_manifest(args.manifest)
    result = None
    if args.action == "get":
        result = data
        for part in args.key.split("."):
            result = result[part]
        print(result if isinstance(result, str) else json.dumps(result))
        return 0
    if args.action == "npm-spec":
        print(npm_spec(args.name, data))
        return 0
    if args.action == "sdk-requirements":
        print("\n".join(sdk_requirements(data)))
        return 0
    if args.action == "check-sdk":
        result = require_deploy_sdk(data)
    elif args.action == "stage-context":
        result = stage_context(args.source, args.destination, args.manifest)
    elif args.action == "install-host":
        user = pwd.getpwnam(args.user)
        owner = (user.pw_uid, user.pw_gid)
        if args.home.resolve() != Path(user.pw_dir).resolve():
            raise ValueError("--home must match the selected user's existing HOME")
        result = {"clis": [
            install_cli(name, home=args.home, prefix=args.prefix, owner=owner, manifest=data)
            for name in data["clis"]
        ]}
        if args.sdk_python:
            _run([str(args.sdk_python), "-m", "pip", "install", "--disable-pip-version-check",
                  *sdk_requirements(data)], timeout=600)
            command = [str(args.sdk_python), str(Path(__file__).resolve())]
            if args.manifest:
                command.extend(["--manifest", str(args.manifest.resolve())])
            result["deploy_sdk"] = json.loads(_run([*command, "check-sdk"]).stdout)["checks"]
    elif args.action == "configure":
        configure_home(args.cli, args.home, manifest=data, kiro_model=args.kiro_model)
        result = {"cli": args.cli, "configured_home": str(args.home)}
    elif args.action == "verify":
        result = verify_cli(args.cli, home=args.home, prefix=args.prefix, manifest=data)
    elif args.action == "install":
        owner = None
        if args.user:
            user = pwd.getpwnam(args.user)
            if args.home.resolve() != Path(user.pw_dir).resolve():
                raise ValueError("--home must match the selected user's existing HOME")
            owner = (user.pw_uid, user.pw_gid)
        result = install_cli(args.cli, home=args.home, prefix=args.prefix,
                             owner=owner, manifest=data)
    receipt = {"manifest_sha256": manifest_sha256(args.manifest), "checks": result}
    rendered = json.dumps(receipt, indent=2) + "\n"
    if getattr(args, "receipt", None):
        _write_config(args.receipt, rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print("CLI toolchain verification failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
