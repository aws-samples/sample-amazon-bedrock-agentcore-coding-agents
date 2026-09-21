"""Offline coverage of Codex's boot, CLI, region and IAM boundaries.

The real shell scripts run against a recording CLI. No model, credentials, AWS
endpoint or application contract is involved.
"""

import importlib.util
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib
from types import SimpleNamespace
import uuid

import pytest

from e2e.collector_fixture import collector_environment, run_entrypoint

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "coding-agents" / "codex"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = _load(HARNESS / "configure_codex.py", "codex_config_test")


@pytest.fixture
def cli_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorder = bin_dir / "codex"
    recorder.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
config_dir = pathlib.Path(os.environ.get("CODEX_HOME") or
                          str(pathlib.Path.home() / ".codex"))
print(json.dumps({
    "argv": sys.argv[1:], "cwd": os.getcwd(),
    "codex_home": os.environ.get("CODEX_HOME"),
    "region": os.environ.get("AWS_REGION"),
    "default_region": os.environ.get("AWS_DEFAULT_REGION"),
    "config": (config_dir / "config.toml").read_text(),
}))
raise SystemExit(int(os.environ.get("RECORDING_EXIT", "0")))
""")
    recorder.chmod(0o755)
    # No inherited credential locations or live Runtime settings.
    env = {
        "HOME": str(home),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": str(tmp_path / "no-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-credentials"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with collector_environment(tmp_path) as (collector_env, _, _):
        env.update(collector_env)
        env["PATH"] = str(bin_dir) + os.pathsep + collector_env["PATH"]
        yield env


def _boot(env, *args, cwd=None):
    return run_entrypoint(HARNESS / "entrypoint.sh", env, args, cwd=cwd)


def _record(result):
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("region", ["us-west-2", "us-east-1"])
@pytest.mark.parametrize("region_var", ["AWS_REGION", "AWS_DEFAULT_REGION"])
def test_boot_configures_a_direct_binary_without_the_launcher(cli_env, region, region_var):
    cli_env.update({region_var: region, "WORKSHOP_CODEX_MODEL": "us.openai.test-model"})
    result = _boot(cli_env, "codex", "exec", "--", "read the supplied request")
    assert result.returncode == 0, result.stderr
    record = _record(result)
    config = tomllib.loads(record["config"])
    assert config["model_provider"] == "amazon-bedrock-runtime"
    assert config["model"] == "us.openai.test-model"
    assert config["model_providers"] == {
        "amazon-bedrock-runtime": {"aws": {"region": region}}}
    assert record["region"] == record["default_region"] == region
    assert record["codex_home"] is None


def test_boot_refuses_a_missing_region_before_starting_a_process(cli_env, tmp_path):
    target = tmp_path / "must-not-run"
    result = _boot(cli_env, "touch", str(target))
    assert result.returncode != 0
    assert "No AWS region" in result.stderr
    assert not target.exists()


def test_real_codex_home_means_the_config_directory(cli_env, tmp_path):
    config_dir = tmp_path / "separate-config"
    cli_env.update(AWS_REGION="us-east-1", CODEX_HOME=str(config_dir))
    result = _boot(cli_env, "codex", "features", "list")
    assert result.returncode == 0, result.stderr
    assert (config_dir / "config.toml").is_file()
    assert not (config_dir / ".codex").exists()
    assert _record(result)["codex_home"] == str(config_dir)


def test_host_gateway_settings_never_configure_a_worker_mcp(cli_env):
    cli_env.update(AWS_REGION="us-east-1", GATEWAY_URL="https://example.invalid/mcp",
                   GITHUB_GATEWAY_URL="https://example.invalid/github",
                   GITHUB_TOKEN="fake-do-not-copy")
    result = _boot(cli_env, "codex", "features", "list")
    assert result.returncode == 0, result.stderr
    config = tomllib.loads(_record(result)["config"])
    assert "mcp_servers" not in config
    assert "example.invalid" not in _record(result)["config"]
    assert "fake-do-not-copy" not in _record(result)["config"]


@pytest.mark.parametrize("overrides,expected", [
    ({}, "us.openai.gpt-5.6-sol"),
    ({"WORKSHOP_CODEX_MODEL": "stack-model"}, "stack-model"),
    ({"WORKSHOP_CODEX_MODEL": "stack", "WORKSHOP_MODEL": "all-role"}, "all-role"),
    ({"WORKSHOP_CODEX_MODEL": "stack", "WORKSHOP_MODEL": "all-role",
      "WORKSHOP_MODEL_CODEX": "role-model"}, "role-model"),
])
def test_model_layers_match_in_boot_config_and_launcher(cli_env, overrides, expected):
    cli_env.update(AWS_REGION="us-east-1", **overrides)
    result = _boot(cli_env, "bash", str(HARNESS / "run.sh"), cwd=cli_env["HOME"])
    assert result.returncode == 0, result.stderr
    record = _record(result)
    assert tomllib.loads(record["config"])["model"] == expected
    assert record["argv"][record["argv"].index("--model") + 1] == expected


def test_launcher_keeps_worktree_and_quotes_trust_prompt_and_model(cli_env, tmp_path):
    workdir = tmp_path / 'local.worktree "quoted" 🎛'
    workdir.mkdir()
    prompt = '--not-a-flag\nRead the request; do not expand $(touch injected)'
    model = 'custom-model "quoted"'
    cli_env.update(AWS_REGION="us-east-1", WORKSHOP_AGENT_WORKDIR=str(workdir),
                   WORKSHOP_MODEL_CODEX="less-specific", RECORDING_EXIT="23")
    result = _boot(cli_env, "bash", str(HARNESS / "run.sh"),
                   "--model", model, "--", prompt, cwd=cli_env["HOME"])
    # The actual CLI exit survives both shell wrappers.
    assert result.returncode == 23, result.stderr
    record = _record(result)
    argv = record["argv"]
    assert argv[0] == "exec" and argv[-2:] == ["--", prompt]
    assert argv[argv.index("--model") + 1] == model
    assert argv[argv.index("--cd") + 1] == str(workdir)
    assert Path(record["cwd"]).resolve() == workdir.resolve()
    settings = {}
    for index, arg in enumerate(argv[:-1]):
        if arg == "-c":
            settings.update(tomllib.loads(argv[index + 1]))
    assert settings["projects"] == {
        str(workdir.resolve()): {"trust_level": "trusted"}}
    assert settings["model_providers"]["amazon-bedrock-runtime"]["aws"]["region"] == "us-east-1"
    assert settings["model_provider"] == "amazon-bedrock-runtime"
    assert record["codex_home"] is None
    assert "projects" not in tomllib.loads(record["config"])  # process-only trust
    assert not (workdir / "injected").exists()


@pytest.mark.parametrize("entry,explicit_workdir,mount_attached,selected", [
    ("root", False, True, "shared"),
    ("root", False, False, "home"),
    ("chosen", False, True, "chosen"),
    ("root", True, True, "chosen"),
])
def test_launcher_root_entry_selects_workspace_without_overriding_chosen_cwd(
        cli_env, tmp_path, entry, explicit_workdir, mount_attached, selected):
    """The taught command shell really enters at /; dispatch may choose a worktree."""
    shared = tmp_path / "shared mount"
    if mount_attached:
        shared.mkdir()
    chosen = tmp_path / "chosen worktree"
    chosen.mkdir()
    # Execute the unchanged launcher from the actual /. Redirect only the mount's
    # directory check and chdir into this test's filesystem, without creating or
    # touching a host /mnt/s3files. The selection logic remains in run.sh.
    shell_env = tmp_path / "mount-fixture.bash"
    shell_env.write_text("""
[() {
  if [[ $# -eq 3 && $1 == -d && $2 == /mnt/s3files ]]; then
    builtin [ -d "$TEST_SHARED_DIR" ]
  else
    # The original argv already includes the closing bracket.
    builtin [ "$@"
  fi
}
cd() {
  if [[ ${1:-} == /mnt/s3files ]]; then
    builtin cd "$TEST_SHARED_DIR"
  else
    builtin cd "$@"
  fi
}
""")
    cli_env.update(AWS_REGION="us-east-1", BASH_ENV=str(shell_env),
                   TEST_SHARED_DIR=str(shared))
    if explicit_workdir:
        cli_env["WORKSHOP_AGENT_WORKDIR"] = str(chosen)
    result = _boot(cli_env, "bash", str(HARNESS / "run.sh"),
                   cwd="/" if entry == "root" else chosen)
    assert result.returncode == 0, result.stderr
    record = _record(result)
    expected = {"shared": shared, "home": Path(cli_env["HOME"]),
                "chosen": chosen}[selected].resolve()
    assert Path(record["cwd"]).resolve() == expected
    argv = record["argv"]
    assert argv[0] != "exec"  # exercise the native interactive launch path
    assert Path(argv[argv.index("--cd") + 1]).resolve() == expected
    settings = {}
    for index, arg in enumerate(argv[:-1]):
        if arg == "-c":
            settings.update(tomllib.loads(argv[index + 1]))
    assert settings["projects"] == {str(expected): {"trust_level": "trusted"}}


def test_missing_model_argument_fails_before_codex(cli_env):
    cli_env["AWS_REGION"] = "us-east-1"
    result = _boot(cli_env, "bash", str(HARNESS / "run.sh"), "--model",
                   cwd=cli_env["HOME"])
    assert result.returncode == 2
    assert "--model needs a value" in result.stderr
    assert not result.stdout.strip()


def test_direct_dispatch_executes_in_a_local_worktree_and_returns_its_real_exit(
        cli_env, tmp_path, monkeypatch):
    """Exercise hydration, baked steering, direct CLI and archive return together."""
    sys.path.insert(0, str(ROOT / "orchestrator"))
    import runtime_exec

    monkeypatch.delenv("PERUSER_ROLE_ARN", raising=False)
    seed = tmp_path / "source"
    seed.mkdir()
    (seed / "input.txt").write_text("a request's arbitrary source bytes\n")
    archive = tmp_path / "exchange.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(seed / "input.txt", arcname="input.txt")
    home = Path(cli_env["HOME"])
    shutil.copyfile(HARNESS / "AGENTS.md", home / "AGENTS.md")
    bin_dir = Path(cli_env["PATH"].split(os.pathsep)[0])
    if sys.platform == "darwin":
        # The Runtime has GNU tar. BSD tar accepts the same "do not restore
        # mtimes" operation as -m, but not GNU's --touch spelling.
        tar_binary = shutil.which("tar")
        (bin_dir / "tar").write_text(f"#!{sys.executable}\n" + (
            "import os, sys\n"
            f"os.execv({tar_binary!r}, ['tar', *"
            "('-m' if a == '--touch' else a for a in sys.argv[1:])])\n"))
        (bin_dir / "tar").chmod(0o755)
    (bin_dir / "aws").write_text(f"#!{sys.executable}\n" + """
import os, pathlib, shutil, sys
args = sys.argv[1:]
assert args[:2] == ["s3", "cp"], args
assert args[args.index("--region") + 1] == "us-east-1", args
source, target = args[2:4]
exchange = os.environ["TEST_EXCHANGE_ARCHIVE"]
shutil.copyfile(exchange if source.startswith("s3://") else source,
                exchange if target.startswith("s3://") else target)
""")
    (bin_dir / "aws").chmod(0o755)
    # Record the boundary inside the actual hydrated Git worktree.
    (bin_dir / "codex").write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, subprocess, sys, tomllib
p = pathlib.Path.cwd()
cfg = tomllib.loads((pathlib.Path.home()/".codex"/"config.toml").read_text())
data = {
    "argv": sys.argv[1:], "cwd": str(p), "region": os.environ["AWS_REGION"],
    "provider": cfg["model_provider"],
    "provider_region": cfg["model_providers"]["amazon-bedrock-runtime"]["aws"]["region"],
    "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(),
    "steering": (p/"AGENTS.md").read_text(), "input": (p/"input.txt").read_text(),
}
(p/"boundary-receipt.json").write_text(json.dumps(data))
raise SystemExit(29)
""")
    (bin_dir / "codex").chmod(0o755)
    nonce = uuid.uuid4().hex
    command = runtime_exec._build_command(
        "codex", "a request with $(touch must-not-run) and 'quotes'",
        "run_offline/work/frontend", None, "us.openai.task-override", "us-east-1",
        nonce, archive_uri="s3://offline/exchange.tar.gz")
    cli_env.update(AWS_REGION="us-east-1", TEST_EXCHANGE_ARCHIVE=str(archive))
    try:
        result = _boot(cli_env, "bash", "-c", command)
        assert result.returncode == 29, result.stderr
        with tarfile.open(archive) as handle:
            members = handle.getnames()
            receipt_name = next(n for n in members
                                if Path(n).name == "boundary-receipt.json")
            receipt = json.load(handle.extractfile(receipt_name))
        assert receipt["cwd"] == f"/private/tmp/workshop-{nonce}" or receipt["cwd"] == f"/tmp/workshop-{nonce}"
        assert receipt["branch"] == "worktree-frontend"
        assert receipt["steering"] == (HARNESS / "AGENTS.md").read_text()
        assert receipt["input"] == "a request's arbitrary source bytes\n"
        assert receipt["provider"] == "amazon-bedrock-runtime"
        assert receipt["region"] == receipt["provider_region"] == "us-east-1"
        argv = receipt["argv"]
        assert argv[0] == "exec"
        assert argv[argv.index("-m") + 1] == "us.openai.task-override"
        assert not any(".git" in Path(n).parts for n in members)
        assert not any(n.endswith("must-not-run") for n in members)
        assert not Path(f"/tmp/workshop-{nonce}").exists()
    finally:
        for path in (f"/tmp/workshop-{nonce}", f"/tmp/workshop-seed-{nonce}"):
            shutil.rmtree(path, ignore_errors=True)
        for path in (f"/tmp/workshop-source-{nonce}.tar.gz",
                     f"/tmp/workshop-result-{nonce}.tar.gz"):
            Path(path).unlink(missing_ok=True)


def _deploy_module(tmp_path, monkeypatch, *, region="us-east-1", ap_region=None,
                   defer_mount=False):
    folder = tmp_path / "coding-agents"
    role = folder / "codex"
    role.mkdir(parents=True)
    shutil.copyfile(HARNESS / "deploy.py", role / "deploy.py")
    for name in ("runtime_deploy.py", "cli_versions.py", "cli-versions.json"):
        shutil.copyfile(ROOT / "coding-agents" / name, folder / name)
    ap = f"arn:aws:s3files:{ap_region or region}:123456789012:file-system/fs-one/access-point/ap-one"
    (folder / "infra.config").write_text(
        f"INFRA_REGION={region}\nINFRA_ACCOUNT_ID=123456789012\n"
        f"INFRA_S3FILES_AP_ARN={ap}\nINFRA_BUCKET=test-exchange\n")
    (role / "agent.config").write_text(
        f"ECR_URI=123456789012.dkr.ecr.{region}.amazonaws.com/coding-agents-codex:tested\n")
    monkeypatch.setenv("AWS_REGION", region)
    monkeypatch.setenv("WORKSHOP_DEFER_MOUNT", "1" if defer_mount else "0")
    return _load(role / "deploy.py", "codex_deploy_test")


def test_cross_region_mount_fails_before_aws_even_when_deferred(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="REGION_MISMATCH"):
        _deploy_module(tmp_path, monkeypatch, ap_region="us-west-2", defer_mount=True)


@pytest.mark.parametrize("defer_mount", [False, True])
def test_deploy_passes_model_and_region_and_honors_deferred_mount(tmp_path, monkeypatch, defer_mount):
    module = _deploy_module(tmp_path, monkeypatch, defer_mount=defer_mount)
    calls = []

    class Control:
        exceptions = SimpleNamespace(ConflictException=type("Conflict", (Exception,), {}))

        def create_agent_runtime(self, **kwargs):
            calls.append(kwargs)
            return {"agentRuntimeId": "codex-test", "agentRuntimeArn": "arn:test",
                    "agentRuntimeVersion": "1", "status": "CREATING", "createdAt": 100}

        def get_agent_runtime(self, **kwargs):
            return {"status": "READY", "platformVersion": "V2", "agentRuntimeVersion": "1",
                    "createdAt": 100, "lastUpdatedAt": 100,
                    "agentRuntimeId": "codex-test", "agentRuntimeArn": "arn:test"}

    module.boto3 = SimpleNamespace(Session=lambda **kw: SimpleNamespace(
        client=lambda *a, **k: Control()))
    monkeypatch.setenv("WORKSHOP_CODEX_MODEL", "us.openai.stack-model")
    monkeypatch.setenv("WORKSHOP_MODEL_CODEX", "us.openai.role-model")
    monkeypatch.setenv("ORCHESTRATOR_MODEL_ID", "coordinator-only")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-do-not-forward")
    monkeypatch.setenv("GATEWAY_URL", "https://example.invalid/mcp")
    module.deploy_runtime("arn:aws:iam::123456789012:role/codex")
    args = calls[0]
    assert args["platformVersion"] == "V2"
    env = args["environmentVariables"]
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert env["WORKSHOP_CODEX_MODEL"] == "us.openai.stack-model"
    assert env["WORKSHOP_MODEL_CODEX"] == "us.openai.role-model"
    assert not {"ORCHESTRATOR_MODEL_ID", "GITHUB_TOKEN", "GATEWAY_URL",
                "GITHUB_GATEWAY_URL", "BEDROCK_MANTLE_REGION"} & env.keys()
    assert ("filesystemConfigurations" in args) is not defer_mount
    if not defer_mount:
        assert args["filesystemConfigurations"][0]["s3FilesAccessPoint"]["accessPointArn"] == module.S3FILES_AP_ARN


@pytest.mark.parametrize("region", ["us-west-2", "us-east-1"])
def test_iam_grants_openai_inference_streaming_and_project_without_gateway(
        tmp_path, monkeypatch, region):
    module = _deploy_module(tmp_path, monkeypatch, region=region)
    policies = []
    exists = type("AlreadyExists", (Exception,), {})

    class IAM:
        exceptions = SimpleNamespace(EntityAlreadyExistsException=exists)

        def create_role(self, **kwargs):
            raise exists()

        def put_role_policy(self, **kwargs):
            policies.append(json.loads(kwargs["PolicyDocument"]))

    module.boto3 = SimpleNamespace(Session=lambda **kw: SimpleNamespace(
        client=lambda *a, **k: IAM()))
    module.create_execution_role()
    statements = policies[0]["Statement"]
    def allows(action, resource):
        return any(s["Effect"] == "Allow" and
                   any(fnmatchcase(action, a) for a in s["Action"]) and
                   any(fnmatchcase(resource, r) for r in s["Resource"])
                   for s in statements)

    project = f"arn:aws:bedrock:{region}:123456789012:project/default"
    profile = f"arn:aws:bedrock:{region}:123456789012:inference-profile/us.openai.gpt-5.6-sol"
    target = "arn:aws:bedrock:us-west-2::foundation-model/openai.gpt-5.6-sol"
    assert allows("bedrock:InvokeModel", project)
    for action in ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"):
        assert allows(action, profile)
        assert allows(action, target)
        assert not allows(action, target.replace("openai.gpt-5.6-sol", "anthropic.claude-sonnet-4-6"))
    assert not allows("bedrock:InvokeModel", project.replace("/default", "/other"))
    assert not any(action.startswith(("secretsmanager:", "bedrock-mantle:"))
                   or action.endswith(("GetResourceApiKey", "GetWorkloadAccessToken", "InvokeGateway"))
                   for s in statements for action in s["Action"])
