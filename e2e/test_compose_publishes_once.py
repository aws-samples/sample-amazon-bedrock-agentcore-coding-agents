"""The pull request publishes the validated integration candidate exactly once."""

from __future__ import annotations

import importlib
import os
import shutil
import stat
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))


def _write(root: str, files: dict[str, str]) -> None:
    for rel, body in files.items():
        dest = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(body)


def _run(engine, run_id: str):
    import roles
    import work_items

    run = engine.Run(
        run_id=run_id,
        task="build an issue tracker",
        agents=["claude-code", "opencode", "claude-code-validator"],
        roles={
            "claude-code": "backend-builder",
            "opencode": "frontend-builder",
            "claude-code-validator": "validator",
        },
    )
    os.makedirs(run.integration_base_dir, exist_ok=True)
    for agent, role, capability, kind, token in (
        ("claude-code", "backend-builder", "backend", roles.BUILDER, "back"),
        ("opencode", "frontend-builder", "frontend", roles.BUILDER, "front"),
        ("claude-code-validator", "validator", "validator", roles.CHECKER, "check"),
    ):
        item = work_items.WorkItem.create(
            run.run_id, agent, role, capability, kind=kind, token=token)
        run.work_items[agent] = item
        if kind == roles.BUILDER:
            os.makedirs(run.item_base_dir(agent), exist_ok=True)
    return run


def _assemble(engine, run):
    import work_items

    builders = [
        run.work_items["claude-code"],
        run.work_items["opencode"],
    ]
    candidate = work_items.assemble_candidate(
        run.integration_base_dir,
        [
            (item, run.item_base_dir(item.agent), run.roledir(item.agent))
            for item in builders
        ],
        run.candidate_dir,
        exclude=engine._work_patch_excluded,
    )
    run.integration_candidate = candidate.public()
    return candidate


def _author_check(run, body: str = "#!/bin/sh\nexit 0\n") -> str:
    path = os.path.join(run.roledir("claude-code-validator"),
                        "acceptance_check")
    _write(os.path.dirname(path), {"acceptance_check": body})
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    run._acceptance_test_file = path
    return path


def test_candidate_is_committed_once_without_coordination_or_run_state():
    engine = importlib.import_module("engine")
    eng = engine.Engine.__new__(engine.Engine)
    run = _run(engine, "run_000000_881")
    try:
        _write(run.roledir("claude-code"), {
            "server.py": "# service\n",
            "start.sh": "#!/bin/sh\npython3 server.py\n",
            "CLAUDE.md": "harness steering\n",
            "issues.db-wal": "runtime state\n",
        })
        _write(run.roledir("opencode"), {
            "static/index.html": "<!doctype html><title>tracker</title>\n",
            ".workshop/refresh.json": "{}\n",
            "AGENTS.md": "harness steering\n",
        })
        candidate = _assemble(engine, run)
        check = _author_check(run)
        run._acceptance_test_file = eng._gate_dir_check_path(run, check)
        run.gate = {"passed": True, "summary": "green"}

        eng._compose_commit_locked(run)
        paths = sorted(f["path"] for f in engine.public_diff(run)["files"])

        assert paths == [
            "acceptance_check", "server.py", "start.sh",
            "static/index.html",
        ]
        assert candidate.files == [
            "server.py", "start.sh", "static/index.html"]
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_conflicting_role_patches_never_reach_compose():
    engine = importlib.import_module("engine")
    import work_items

    run = _run(engine, "run_000000_882")
    try:
        _write(run.roledir("claude-code"), {
            "package.json": '{"name":"backend"}\n'})
        _write(run.roledir("opencode"), {
            "package.json": '{"name":"frontend"}\n'})

        with pytest.raises(work_items.IntegrationConflict):
            _assemble(engine, run)
        assert not os.path.isdir(run.candidate_dir)
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_validator_check_ships_once_from_the_executed_gate_workspace():
    engine = importlib.import_module("engine")
    eng = engine.Engine.__new__(engine.Engine)
    run = _run(engine, "run_000000_883")
    try:
        _write(run.roledir("claude-code"), {
            "server.py": "# service\n",
            "acceptance_check": "#!/bin/sh\necho stale\nexit 0\n",
        })
        _write(run.roledir("opencode"), {"index.html": "<h1>UI</h1>\n"})
        _assemble(engine, run)
        authored = _author_check(
            run, "#!/bin/sh\necho current validator check\nexit 0\n")
        run._acceptance_test_file = eng._gate_dir_check_path(run, authored)
        run.gate = {"passed": True, "summary": "green"}

        eng._compose_commit_locked(run)
        diff = engine.public_diff(run)
        paths = [row["path"] for row in diff["files"]]
        assert paths.count("acceptance_check") == 1
        import subprocess
        body = subprocess.run(
            ["git", "-C", os.path.join(engine._RUNS_DIR, "composed"),
             "show", f"run/{run.run_id}:acceptance_check"],
            capture_output=True, text=True, check=True).stdout
        assert "current validator check" in body
        assert "stale" not in body
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_gate_and_pull_request_exclusions_have_different_jobs():
    engine = importlib.import_module("engine")
    for rel in ("issues.db", "issues.db-wal", "issues.db-shm", "app.log"):
        assert engine._compose_excluded(rel)
        assert not engine._gate_excluded(rel)
    for rel in (
        "CLAUDE.md",
        "AGENTS.md",
        "skills/frontend-design/SKILL.md",
        ".workshop/integration-brief.md",
    ):
        assert engine._compose_excluded(rel)
