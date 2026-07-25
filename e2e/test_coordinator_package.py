"""The coordinator container must be able to IMPORT the engine it ships.

`orchestrator-agent/` is the container build context, so `../orchestrator` does
NOT exist inside the image: `stage_engine.py` copies the engine in first. That
makes the staged bundle the only thing the deployed coordinator can import, and
it is invisible to every other test in this suite, because the repo checkout has
`orchestrator/` importable one level up either way.

That gap shipped a real defect: `roles.py` and `role_graph.py` were added to the
engine and imported at module scope, but the hand-maintained `_MODULES` tuple in
stage_engine.py was not updated, so `agentcore deploy` produced a container that
raised `ModuleNotFoundError: No module named 'roles'` on boot while the full
local suite stayed green. These tests reproduce the container's import
conditions, so the same class of omission fails here instead of at an event.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_DIR = os.path.join(_REPO, "orchestrator-agent")
_ENGINE_DIR = os.path.join(_REPO, "orchestrator")


def _stage_engine_module():
    sys.path.insert(0, _AGENT_DIR)
    try:
        import stage_engine  # noqa: PLC0415
        return stage_engine
    finally:
        sys.path.remove(_AGENT_DIR)


def test_every_module_scope_import_of_the_engine_is_staged():
    """The staged set must be CLOSED under the engine's own local imports."""
    stage_engine = _stage_engine_module()
    staged = set(stage_engine._closure())
    local = stage_engine._local_modules()

    missing: list[str] = []
    for name in sorted(staged):
        path = os.path.join(_ENGINE_DIR, name)
        for dep in sorted(stage_engine._imports_of(path, local)):
            if dep.startswith(stage_engine._SKIP_PREFIXES):
                continue
            if f"{dep}.py" not in staged:
                missing.append(f"{name} imports {dep}, but {dep}.py is not staged")
    assert not missing, (
        "the staged coordinator bundle is not import-closed, so the deployed "
        "container will raise ModuleNotFoundError on boot:\n  "
        + "\n  ".join(missing))


def test_roles_and_role_graph_ship():
    """Pin the two modules whose absence broke the deploy, by name.

    The closure test above is the general guard; this one names the specific
    regression so a future refactor that drops the roster or the Strands role
    graph from the bundle fails with an obvious message.
    """
    staged = set(_stage_engine_module()._closure())
    for required in ("roles.py", "role_graph.py"):
        assert required in staged, (
            f"{required} is not staged into the coordinator container, but the "
            "engine imports it at module scope")


def test_staged_bundle_imports_without_the_repo_engine_dir(tmp_path):
    """Import the staged bundle the way the CONTAINER does, and prove it works.

    Copies `orchestrator-agent/` plus the engine to a scratch tree, stages, then
    DELETES the sibling `orchestrator/` before importing. That deletion is the
    whole point: it reproduces the image, where only the staged copy exists.
    """
    app = tmp_path / "app"
    shutil.copytree(_AGENT_DIR, app,
                    ignore=shutil.ignore_patterns("__pycache__", "orchestrator"))
    shutil.copytree(_ENGINE_DIR, tmp_path / "orchestrator",
                    ignore=shutil.ignore_patterns("__pycache__"))

    staged = subprocess.run([sys.executable, "stage_engine.py"],
                            cwd=app, capture_output=True, text=True)
    assert staged.returncode == 0, f"stage_engine.py failed:\n{staged.stderr}"

    # The container has no ../orchestrator. Remove it so a stale path cannot
    # satisfy the import and hide a missing module.
    shutil.rmtree(tmp_path / "orchestrator")

    probe = (
        "import sys; sys.path.insert(0, 'orchestrator');"
        "import engine, chat, presets, reviewer;"
        "import roles, role_graph;"
        "print('OK', len(roles.roster_ids()))"
    )
    res = subprocess.run([sys.executable, "-c", probe],
                         cwd=app, capture_output=True, text=True)
    if res.returncode != 0:
        pytest.fail(
            "the staged coordinator bundle cannot be imported with only the "
            "staged copy present, which is exactly the deployed container's "
            f"state:\n{res.stderr}")
    assert "OK" in res.stdout


def test_tests_are_never_staged():
    """Test modules are box-only; shipping them would bloat the image."""
    staged = _stage_engine_module()._closure()
    assert not [m for m in staged if m.startswith("test_")], staged
