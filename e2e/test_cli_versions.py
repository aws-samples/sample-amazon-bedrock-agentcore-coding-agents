"""Exercise the shared installers and receipts without npm downloads or AWS."""
import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "coding-agents/cli_versions.py"
spec = importlib.util.spec_from_file_location("workshop_cli_versions", HELPER)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def tools(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Every external installer used by a test must be explicitly supplied here.
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.defpath)
    return bin_dir


def version_binary(path, output):
    return executable(path, "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(output) + "\n")


@pytest.mark.parametrize("name", ["claude-code", "codex", "agentcore"])
def test_normal_npm_gets_exact_spec_and_actual_version_is_verified(tmp_path, monkeypatch, tools, name):
    manifest = cli.load_manifest()
    item = manifest["clis"][name]
    log = tmp_path / "npm.json"
    home = tmp_path / "user home"
    monkeypatch.setenv("NPM_TEST_LOG", str(log))
    monkeypatch.setenv("NPM_TEST_BIN", str(tools / item["command"]))
    monkeypatch.setenv("NPM_TEST_VERSION", item["version"])
    executable(tools / "npm", f"""#!{sys.executable}
import json, os, pathlib, sys
pathlib.Path(os.environ["NPM_TEST_LOG"]).write_text(json.dumps(sys.argv[1:]))
target = pathlib.Path(os.environ["NPM_TEST_BIN"])
target.write_text("#!/bin/sh\\nprintf '%s\\\\n' '" + os.environ["NPM_TEST_VERSION"] + "'\\n")
target.chmod(0o755)
""")
    result = cli.install_cli(name, home=home, manifest=manifest)
    arguments = json.loads(log.read_text())
    assert arguments == ["install", "--global", "--no-audit", "--no-fund", cli.npm_spec(name)]
    assert result["executables"][0]["version"] == item["version"]
    assert result["executables"][0]["path"] == str((tools / item["command"]).resolve())


def test_existing_wrong_version_cannot_be_reused_or_reported_success(tmp_path, tools):
    version_binary(tools / "codex", "codex-cli 0.1.0")
    receipt = tmp_path / "receipt.json"
    result = subprocess.run(
        [sys.executable, str(HELPER), "install", "--cli", "codex",
         "--home", str(tmp_path / "home"), "--receipt", str(receipt)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "Refusing reuse" in result.stderr
    assert not receipt.exists()
    assert not (tmp_path / "home/.codex/config.toml").exists()


def test_explicit_prefix_cannot_reuse_another_installation(tmp_path, tools):
    version_binary(tools / "codex", "codex-cli 0.155.1")
    prefix = tmp_path / "selected installation"
    with pytest.raises(RuntimeError, match="executable is missing"):
        cli.verify_cli("codex", home=tmp_path / "home", prefix=prefix)
    log = tmp_path / "npm.json"
    executable(tools / "npm", f"""#!{sys.executable}
import json, pathlib, sys
pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))
target = pathlib.Path({str(prefix / 'bin/codex')!r})
target.parent.mkdir(parents=True)
target.write_text("#!/bin/sh\\necho 'codex-cli 0.155.1'\\n")
target.chmod(0o755)
""")
    receipt = cli.install_cli("codex", home=tmp_path / "home", prefix=prefix)
    assert json.loads(log.read_text())[-3:] == [
        "--prefix", str(prefix), "@openai/codex@0.155.1"]
    assert receipt["executables"][0]["path"] == str(prefix / "bin/codex")


def test_zero_exit_without_the_exact_version_is_not_a_pass(tmp_path, tools):
    version_binary(tools / "codex", "codex-cli 0.155.10")
    with pytest.raises(RuntimeError, match="version mismatch"):
        cli.verify_cli("codex", home=tmp_path)


@pytest.mark.parametrize("version", [
    "0.155.1-alpha.1", "0.155.1+build.1", "0.155.1-alpha.1+build.1",
    "0.155.1rc1", "0.155.1.2",
])
def test_version_suffix_cannot_be_reused_or_receive_a_success_receipt(tmp_path, tools, version):
    version_binary(tools / "codex", "codex-cli " + version)
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    before = b'model = "preserve"\ncheck_for_update_on_startup = true\n'
    config.write_bytes(before)
    receipt = tmp_path / "receipt.json"
    result = subprocess.run(
        [sys.executable, str(HELPER), "install", "--cli", "codex",
         "--home", str(home), "--receipt", str(receipt)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode != 0, result.stdout
    assert "Refusing reuse" in result.stderr
    assert version in result.stderr
    assert not receipt.exists()
    assert config.read_bytes() == before


def test_nonzero_version_probe_stays_a_failure(tmp_path, tools):
    executable(tools / "codex", "#!/bin/sh\necho 'codex-cli 0.155.1'\nexit 8\n")
    with pytest.raises(subprocess.CalledProcessError) as error:
        cli.verify_cli("codex", home=tmp_path)
    assert error.value.returncode == 8


@pytest.mark.parametrize("name,banner", [
    ("claude-code", "2.1.278 (Claude Code)"),
    ("codex", "codex-cli 0.155.1"),
    ("kiro", "kiro-cli 2.22.1"),
    ("agentcore", "0.30.0"),
])
def test_live_readback_banner_formats_verify_exactly(tmp_path, tools, name, banner):
    item = cli.load_manifest()["clis"][name]
    path = (tmp_path / ".local/bin" if name == "kiro" else tools) / item["command"]
    version_binary(path, banner)
    for sibling in item.get("siblings", []):
        version_binary(path.with_name(sibling), sibling + " " + item["version"])
    result = cli.verify_cli(name, home=tmp_path)
    assert all(check["version"] == item["version"] for check in result["executables"])


def test_sdk_specs_and_executing_environment_must_agree(monkeypatch):
    assert cli.sdk_requirements() == ["boto3==1.43.95", "botocore==1.43.95"]
    assert cli.runtime_platform_version() == "V2"
    monkeypatch.setattr(cli.importlib.metadata, "version", lambda name: "1.43.90")
    with pytest.raises(RuntimeError, match="Deploy SDK mismatch"):
        cli.require_deploy_sdk()
    monkeypatch.setattr(cli.importlib.metadata, "version", lambda name: "1.43.95")
    assert cli.require_deploy_sdk() == {"boto3": "1.43.95", "botocore": "1.43.95"}


def test_reader_emits_plain_specs_from_the_selected_manifest(tmp_path):
    manifest = cli.load_manifest()
    manifest["deploy_sdk"] = {"boto3": "1.2.3", "botocore": "1.2.4"}
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(manifest))
    result = subprocess.run(
        [sys.executable, str(HELPER), "--manifest", str(path), "sdk-requirements"],
        capture_output=True, text=True, check=True, timeout=10)
    assert result.stdout == "boto3==1.2.3\nbotocore==1.2.4\n"


def test_config_merges_preserve_user_values_permissions_and_symlinks(tmp_path):
    home = tmp_path / "home"
    target = home / "dotfiles/claude.json"
    target.parent.mkdir(parents=True)
    original = {"env": {"AWS_REGION": "us-east-1"}, "model": "leave-this-model",
                "permissions": {"allow": ["Read"]}}
    target.write_text(json.dumps(original))
    target.chmod(0o640)
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.symlink_to(target)
    cli.configure_home("claude-code", home)
    assert settings.is_symlink()
    observed = json.loads(target.read_text())
    assert observed == dict(original, env=dict(original["env"], DISABLE_AUTOUPDATER="1"))
    assert target.stat().st_mode & 0o777 == 0o640
    codex = home / ".codex/config.toml"
    codex.parent.mkdir()
    before = ('model = "leave-this-model"\ncheck_for_update_on_startup = true # owned\n'
              '[model_providers.custom]\nbase_url = "https://example.invalid"\n'
              '[profiles.other]\ncheck_for_update_on_startup = true\n')
    codex.write_text(before)
    cli.configure_home("codex", home)
    cli.configure_home("codex", home)
    assert codex.read_text() == before.replace("= true # owned", "= false # owned")


def test_codex_does_not_rewrite_a_setting_inside_multiline_text():
    before = 'note = """\ncheck_for_update_on_startup = true\n[not_a_table]\n"""\n'
    assert cli._disable_codex_updates(before) == "check_for_update_on_startup = false\n" + before


@pytest.mark.parametrize("preamble", [
    "developer_instructions = \"Use ''' for multiline strings.\"\n",
    "developer_instructions = 'Use \"\"\" for multiline strings.'\n",
    'developer_instructions = "keep" # """ is documentation, not a string\n',
    "developer_instructions = '''Use \"\"\" in examples.'''\n",
    'developer_instructions = """Use \\""" in examples."""\n',
])
def test_codex_quote_text_cannot_duplicate_the_root_update_setting(tmp_path, preamble):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    before = (preamble + "check_for_update_on_startup=true # keep this comment\n"
              '[profiles.other]\ncheck_for_update_on_startup = true\n')
    config.write_text(before)
    expected = before.replace("startup=true", "startup=false")
    cli.configure_home("codex", tmp_path)
    assert config.read_text() == expected
    cli.configure_home("codex", tmp_path)
    assert config.read_text() == expected


@pytest.mark.parametrize("key", [
    '"check_for_update_on_startup"', "'check_for_update_on_startup'",
    '"check_for_update_on_startu\\u0070"', '"check_for_update_on_startu\\U00000070"',
])
def test_codex_quoted_root_keys_and_crlf_are_preserved(tmp_path, key):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    before = ('# preserve\r\n' + key + ' = true # owned\r\n'
              'model = "unchanged"\r\n').encode()
    config.write_bytes(before)
    config.chmod(0o640)
    cli.configure_home("codex", tmp_path)
    assert config.read_bytes() == before.replace(b"= true", b"= false")
    assert config.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("before", [
    'check_for_update_on_startup = true\n"check_for_update_on_startup" = false\n',
    'check_for_update_on_startup = "false"\n',
    'check_for_update_on_startup.nested = true\n',
    '[check_for_update_on_startup]\nnested = true\n',
    'note = """unterminated\ncheck_for_update_on_startup = true\n',
    'values = [1, 2\n',
])
@pytest.mark.parametrize("without_tomllib", [False, True])
def test_invalid_codex_edit_is_rejected_before_atomic_replacement(
        tmp_path, monkeypatch, before, without_tomllib):
    if without_tomllib:
        monkeypatch.setitem(sys.modules, "tomllib", None)
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(before)
    inode = config.stat().st_ino

    def forbidden_replace(*args, **kwargs):
        pytest.fail("An invalid candidate reached atomic replacement")
    monkeypatch.setattr(cli.os, "replace", forbidden_replace)
    with pytest.raises(ValueError):
        cli.configure_home("codex", tmp_path)
    assert config.read_text() == before
    assert config.stat().st_ino == inode
    assert list(config.parent.iterdir()) == [config]


def test_full_toml_validation_precedes_atomic_replacement(tmp_path, monkeypatch):
    pytest.importorskip("tomllib")
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    before = 'check_for_update_on_startup = true\nmodel = "a"\nmodel = "b"\n'
    config.write_text(before)

    def forbidden_replace(*args, **kwargs):
        pytest.fail("An invalid TOML document reached atomic replacement")
    monkeypatch.setattr(cli.os, "replace", forbidden_replace)
    with pytest.raises(ValueError):
        cli.configure_home("codex", tmp_path)
    assert config.read_text() == before


def test_bootstrap_reader_and_codex_edit_need_only_python_310_stdlib(tmp_path):
    # CFN uses Jammy's system python3 before its later Python install step.
    ast.parse(HELPER.read_text(), filename=str(HELPER), feature_version=(3, 10))
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    before = ("developer_instructions = \"Use ''' for multiline strings.\"\r\n"
              '"check_for_update_on_startu\\u0070" = true # preserve\r\n'
              '[profiles.other]\r\ncheck_for_update_on_startup = true\r\n').encode()
    config.write_bytes(before)
    driver = (
        "import runpy, sys\n"
        "assert sys.flags.no_site\n"
        "sys.modules['tomllib'] = None\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    for arguments in (["sdk-requirements"],
                      ["configure", "--cli", "codex", "--home", str(tmp_path)]):
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", driver, str(HELPER), *arguments],
            capture_output=True, text=True, timeout=10, check=True)
        if arguments == ["sdk-requirements"]:
            assert result.stdout == "boto3==1.43.95\nbotocore==1.43.95\n"
    assert config.read_bytes() == before.replace(b"= true # preserve", b"= false # preserve")


def kiro_archive():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("kirocli/install.sh", """#!/bin/sh
set -eu
test "$KIRO_CLI_SKIP_SETUP" = 1
test -z "${Q_INSTALL_GLOBAL:-}"
mkdir -p "$HOME/.local/bin"
install -m 755 bin/kiro-cli "$HOME/.local/bin/kiro-cli"
install -m 755 bin/kiro-cli-chat "$HOME/.local/bin/kiro-cli-chat"
""")
        for binary in ("kiro-cli", "kiro-cli-chat"):
            archive.writestr("kirocli/bin/" + binary,
                             "#!/bin/sh\nprintf '%s\\n' '" + binary + " 2.22.1'\n")
    return buffer.getvalue()


def prepare_kiro(monkeypatch, raw):
    manifest = copy.deepcopy(cli.load_manifest())
    entry = manifest["clis"]["kiro"]["archives"]["linux-aarch64"]
    entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.platform, "machine", lambda: "aarch64")
    return manifest


def test_verified_kiro_installs_both_executables_without_changing_user_model(tmp_path, monkeypatch, tools):
    raw = kiro_archive()
    manifest = prepare_kiro(monkeypatch, raw)
    monkeypatch.setenv("Q_INSTALL_GLOBAL", "1")
    urls = []
    def download(url, **kwargs):
        urls.append(url)
        return io.BytesIO(raw)
    monkeypatch.setattr(cli.urllib.request, "urlopen", download)
    home = tmp_path / "existing home"
    settings = home / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"chat.defaultModel": "keep", "custom": True}))
    receipt = cli.install_cli("kiro", home=home, manifest=manifest,
                              owner=(os.geteuid(), os.getegid()))
    assert len(receipt["executables"]) == 2
    assert [item["version"] for item in receipt["executables"]] == ["2.22.1", "2.22.1"]
    assert urls == [manifest["clis"]["kiro"]["archives"]["linux-aarch64"]["url"]]
    assert "/2.22.1/" in urls[0] and "/latest/" not in urls[0]
    assert json.loads(settings.read_text()) == {
        "chat.defaultModel": "keep", "custom": True, "app.disableAutoupdates": True}
    cli.install_cli("kiro", home=home, manifest=manifest)
    assert len(urls) == 1


@pytest.mark.parametrize("corruption", ["checksum", "truncated", "oversized"])
def test_invalid_kiro_download_never_runs_its_installer(tmp_path, monkeypatch, tools, corruption):
    raw = kiro_archive()
    manifest = prepare_kiro(monkeypatch, raw)
    broken = {"checksum": raw[:-1] + bytes([raw[-1] ^ 1]),
              "truncated": raw[:-1], "oversized": raw + b"x"}[corruption]
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(broken))
    with pytest.raises(RuntimeError):
        cli.install_cli("kiro", home=tmp_path / "home", manifest=manifest)
    assert not (tmp_path / "home/.local/bin").exists()


def test_kiro_main_binary_alone_is_not_a_complete_installation(tmp_path, tools):
    home = tmp_path / "home"
    version_binary(home / ".local/bin/kiro-cli", "kiro-cli 2.22.1")
    with pytest.raises(RuntimeError, match="kiro-cli-chat"):
        cli.install_cli("kiro", home=home)


def test_build_context_excludes_untracked_private_files_and_keeps_exact_pins(tmp_path):
    repo = tmp_path / "repo"
    role = repo / "coding-agents/claude-code"
    role.mkdir(parents=True)
    skill = repo / "harness-skills/skills/backend-engineering/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("principles\n")
    (role / "Dockerfile").write_text("COPY _toolchain/ /opt/workshop-cli/\n")
    (role / "run.sh").write_text("exit 0\n")
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    (repo / "root-secret.private.json").write_text("must stay out")
    (role / "agent.config").write_text("must stay out")
    (role / "nested").mkdir()
    (role / "nested/secret.private.json").write_text("must stay out")
    stage = tmp_path / "stage"
    result = cli.stage_context(role, stage)
    assert sorted(str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file()) == [
        "Dockerfile", "_toolchain/cli-versions.json", "_toolchain/cli_versions.py",
        "run.sh", "skills/backend-engineering/SKILL.md"]
    assert (stage / "_toolchain/cli-versions.json").read_bytes() == cli.MANIFEST_PATH.read_bytes()
    assert result["manifest_sha256"] == cli.manifest_sha256()


def test_host_command_uses_selected_home_and_sdk_python(tmp_path, monkeypatch, tools, capsys):
    home = tmp_path / "attendee"
    data = cli.load_manifest()
    for name, item in data["clis"].items():
        for command in [item["command"], *item.get("siblings", [])]:
            location = home / ".local/bin" / command if name == "kiro" else tools / command
            version_binary(location, item["version"])
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda name: SimpleNamespace(
        pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home)))
    sdk_log = tmp_path / "sdk.json"
    sdk_python = executable(tools / "sdk-python", f"""#!{sys.executable}
import json, pathlib, sys
if sys.argv[1:3] == ["-m", "pip"]:
    pathlib.Path({str(sdk_log)!r}).write_text(json.dumps(sys.argv[1:]))
else:
    print(json.dumps({{"checks": {data['deploy_sdk']!r}}}))
""")
    receipt = tmp_path / "host.json"
    assert cli.main(["install-host", "--home", str(home), "--user", "attendee",
                     "--sdk-python", str(sdk_python), "--receipt", str(receipt)]) == 0
    assert json.loads(sdk_log.read_text())[-2:] == cli.sdk_requirements()
    saved = json.loads(receipt.read_text())
    assert {record["cli"] for record in saved["checks"]["clis"]} == set(data["clis"])
    assert saved["checks"]["deploy_sdk"] == data["deploy_sdk"]
    assert saved["manifest_sha256"] == cli.manifest_sha256()
    assert (home / ".claude/settings.json").exists()
    assert (home / ".codex/config.toml").exists()


@pytest.mark.parametrize("builder,fail", [("docker", False), ("finch", False), ("finch", True)])
def test_real_build_helper_stages_safe_context_and_propagates_failure(
        tmp_path, monkeypatch, tools, builder, fail):
    repo = tmp_path / "repo"
    role = repo / "coding-agents/codex"
    role.mkdir(parents=True)
    (role / "Dockerfile").write_text("ARG WORKSHOP_RUNTIME_BASE_IMAGE\nFROM ${WORKSHOP_RUNTIME_BASE_IMAGE}\n")
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    (role / "agent.config").write_text("excluded")
    log = tmp_path / "build.json"
    monkeypatch.setenv("BUILDER_TEST_LOG", str(log))
    monkeypatch.setenv("BUILDER_TEST_KIND", builder)
    monkeypatch.setenv("BUILDER_TEST_FAIL", "1" if fail else "0")
    executable(tools / "aws", "#!/bin/sh\nprintf 'fixture-login\\n'\n")
    # Use the session interpreter; never accidentally import host Python deps.
    (tools / "python3").symlink_to(sys.executable)
    builder_body = f"""#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["buildx", "version"]:
    raise SystemExit(0 if os.environ["BUILDER_TEST_KIND"] == "docker" else 1)
if args[0] == "login":
    sys.stdin.read()
    raise SystemExit(0)
if args[0] == "push":
    raise SystemExit(0)
context = pathlib.Path(args[-2] if args[-1] == "--push" else args[-1])
record = {{"args": args, "context": str(context),
          "manifest": json.loads((context / "_toolchain/cli-versions.json").read_text()),
          "files": sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file())}}
pathlib.Path(os.environ["BUILDER_TEST_LOG"]).write_text(json.dumps(record))
raise SystemExit(23 if os.environ["BUILDER_TEST_FAIL"] == "1" else 0)
"""
    executable(tools / "docker", builder_body)
    executable(tools / "finch", builder_body)
    command = ('set -euo pipefail\nsource "$1"\n'
               'build_and_push_arm64 fixture/image "$2/Dockerfile" "$2" us-east-1 000000000000 --toolchain\n')
    result = subprocess.run(
        ["bash", "-c", command, "fixture", str(ROOT / "coding-agents/_build_push.sh"), str(role)],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == (23 if fail else 0), result.stderr
    record = json.loads(log.read_text())
    assert record["manifest"] == cli.load_manifest()
    assert "WORKSHOP_RUNTIME_BASE_IMAGE=" + cli.load_manifest()["runtime"]["base_image"] in record["args"]
    assert record["files"] == ["Dockerfile", "_toolchain/cli-versions.json", "_toolchain/cli_versions.py"]
    assert not Path(record["context"]).exists()
