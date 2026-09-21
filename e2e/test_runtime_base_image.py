"""Keep every selected Runtime base consistent with the canonical toolchain."""
import importlib.util
import json
from pathlib import Path
import re
import shlex

import pytest


ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "coding-agents/cli-versions.json"
ROLES = ("claude-code", "claude-code-validator", "codex", "kiro", "opencode")


def runtime_base(dockerfile):
    # The MCP Dockerfile also has a uv tool stage; inspect its final Runtime stage.
    return [
        line.split()[1]
        for line in dockerfile.read_text().splitlines()
        if line.startswith("FROM ")
    ][-1]


@pytest.mark.parametrize("role", ROLES)
def test_role_base_is_supplied_by_the_shared_builder(role):
    folder = ROOT / "coding-agents" / role
    assert runtime_base(folder / "Dockerfile") == "${WORKSHOP_RUNTIME_BASE_IMAGE}"
    assert (folder / "Dockerfile").read_text().splitlines()[0] == "ARG WORKSHOP_RUNTIME_BASE_IMAGE"
    setup = (folder / "setup.sh").read_text().replace("\\\n", "")
    calls = re.findall(r"^\s*build_and_push_arm64[^\n]+", setup, re.MULTILINE)
    assert len(calls) == 1
    assert calls[0].rstrip().endswith("--toolchain")
    # test_cli_versions executes this real shared helper through Docker and Finch,
    # and verifies that its build argument equals the manifest's base_image.


@pytest.mark.parametrize("role", ROLES)
def test_role_context_contains_the_canonical_toolchain_and_all_copy_inputs(role, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "context_toolchain", ROOT / "coding-agents/cli_versions.py")
    toolchain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toolchain)
    target = tmp_path / role
    toolchain.stage_context(ROOT / "coding-agents" / role, target)
    assert (target / "_toolchain/cli-versions.json").read_bytes() == PINS.read_bytes()
    assert (target / "_toolchain/cli_versions.py").read_bytes() == (
        ROOT / "coding-agents/cli_versions.py").read_bytes()
    paths = [str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()]
    assert not any(path.endswith("agent.config") or "private.json" in path for path in paths)
    for line in (target / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            words = [word for word in shlex.split(line)[1:] if not word.startswith("--")]
            for source in words[:-1]:
                assert list(target.glob(source)), f"{role}: COPY input missing: {source}"


@pytest.mark.parametrize("path", (
    "coding-agents/gateway_mcp/app/Dockerfile",
    "orchestrator-agent/Dockerfile",
))
def test_separately_built_runtime_base_matches_the_manifest(path):
    # These have separate build systems. Keep their explicit final-stage pins
    # synchronized without expanding their build context or changing the uv tool.
    expected = json.loads(PINS.read_text())["runtime"]["base_image"]
    assert runtime_base(ROOT / path) == expected, (
        f"{path} must track coding-agents/cli-versions.json runtime.base_image"
    )
