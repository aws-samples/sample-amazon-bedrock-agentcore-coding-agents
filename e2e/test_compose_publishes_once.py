"""Compose publishes each gated pull request's work exactly once.

There is no assembled multi-role candidate any more: each pull request has its OWN
tree, and compose writes a local scratch commit for the console's Changes tab from
what the run actually produced. What still matters, and is asserted here, is that the
FRESHLY authored check ships beside the work and a stale copy a builder happened to
write never shadows it.
"""

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
        agents=["claude-code", "opencode", "kiro"],
        roles={
            "claude-code": "backend-builder",
            "opencode": "frontend-builder",
            "kiro": "validator",
        },
    )
    os.makedirs(run.integration_base_dir, exist_ok=True)
    for agent, role, capability, kind, token in (
        ("claude-code", "backend-builder", "backend", roles.BUILDER, "back"),
        ("opencode", "frontend-builder", "frontend", roles.BUILDER, "front"),
        ("kiro", "validator", "validator", roles.CHECKER, "check"),
    ):
        item = work_items.WorkItem.create(
            run.run_id, agent, role, capability, kind=kind, token=token)
        run.work_items[agent] = item
        if kind == roles.BUILDER:
            os.makedirs(run.item_base_dir(agent), exist_ok=True)
    return run


def _build_trees(engine, run):
    """Build EACH builder's own pull request tree, as the engine does per PR."""
    import work_items

    builders = [
        run.work_items["claude-code"],
        run.work_items["opencode"],
    ]
    for item in builders:
        work_items.apply_patch(
            run.integration_base_dir,
            item,
            run.item_base_dir(item.agent),
            run.roledir(item.agent),
            run.item_tree_dir(item.work_id),
            exclude=engine._work_patch_excluded,
        )
    return builders


def _author_check(run, body: str = "#!/bin/sh\nexit 0\n") -> str:
    path = os.path.join(run.roledir("kiro"),
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
        builders = _build_trees(engine, run)
        check = _author_check(run)
        run._acceptance_test_file = eng._gate_dir_check_path(
            run, check, builders[0])
        run.gate = {"passed": True, "summary": "green"}

        eng._compose_commit_locked(run)
        paths = sorted(f["path"] for f in engine.public_diff(run)["files"])

        # Every role's real files, the authored check beside them, and NOTHING the
        # patch excludes (harness steering, runtime state, coordination files).
        assert paths == [
            "acceptance_check", "server.py", "start.sh",
            "static/index.html",
        ]
        assert {item.work_id for item in builders} == {
            item.work_id for item in builders}
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_a_patch_whose_base_moved_is_refused_not_overwritten():
    """Two roles touching one path is NOT a conflict any more: each pull request is
    built on the base branch alone, so they simply do not see each other.

    What IS refused is the real hazard: a pull request whose base changed the very
    path it also changed. That is "someone merged before you", and silently
    overwriting their merged work is the failure this guard prevents.
    """
    engine = importlib.import_module("engine")
    import work_items

    run = _run(engine, "run_000000_882")
    try:
        # Both roles change the same path. Built independently, BOTH succeed: they
        # are separate pull requests, and neither one's tree contains the other.
        _write(run.roledir("claude-code"), {
            "package.json": '{"name":"backend"}\n'})
        _write(run.roledir("opencode"), {
            "package.json": '{"name":"frontend"}\n'})
        builders = _build_trees(engine, run)
        for item in builders:
            assert os.path.isdir(run.item_tree_dir(item.work_id))

        # Now the BASE moves under one of them: the branch it merges into changed the
        # same path since it received its base. That must be refused.
        item = run.work_items["opencode"]
        _write(run.integration_base_dir, {
            "package.json": '{"name":"already-merged-by-someone-else"}\n'})
        with pytest.raises(work_items.StalePatch):
            work_items.apply_patch(
                run.integration_base_dir,
                item,
                run.item_base_dir(item.agent),
                run.roledir(item.agent),
                run.item_tree_dir(item.work_id),
                exclude=engine._work_patch_excluded,
            )
        assert not os.path.isdir(run.item_tree_dir(item.work_id))
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
        builders = _build_trees(engine, run)
        authored = _author_check(
            run, "#!/bin/sh\necho current validator check\nexit 0\n")
        run._acceptance_test_file = eng._gate_dir_check_path(
            run, authored, builders[0])
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


def test_compose_recovers_a_shared_worktree_left_dirty_by_a_previous_run(
        tmp_path, monkeypatch):
    """A dirty composed worktree must not wedge every later run in the same runs dir.

    ``.runs/composed`` is SHARED by every run, and ``git checkout -B`` REFUSES to
    switch branches while a tracked file it would overwrite is modified ("Your local
    changes to the following files would be overwritten by checkout"). A compose
    interrupted between its file copies and its commit leaves exactly that state, so
    every subsequent compose died with a bare ``returned non-zero exit status 1``
    and looked like a concurrency race. It is not a race: it is deterministic, it
    persists until someone cleans the directory by hand, and it takes down runs that
    have nothing to do with the one that crashed.

    Reproduced here by composing once, dirtying a tracked file in the shared repo,
    and composing again in the SAME runs dir.
    """
    import importlib

    monkeypatch.setenv("WORKSHOP_RUNS_DIR", str(tmp_path))
    engine = importlib.reload(importlib.import_module("engine"))
    eng = engine.Engine.__new__(engine.Engine)

    def compose(run_id: str) -> None:
        run = _run(engine, run_id)
        try:
            _write(run.roledir("claude-code"), {"server.py": "# service\n"})
            _write(run.roledir("opencode"), {"static/index.html": "<title>x</title>\n"})
            builders = _build_trees(engine, run)
            check = _author_check(run)
            run._acceptance_test_file = eng._gate_dir_check_path(run, check, builders[0])
            run.gate = {"passed": True, "summary": "green"}
            eng._compose_commit_locked(run)
        finally:
            shutil.rmtree(run.workdir, ignore_errors=True)

    try:
        compose("run_000000_991")

        repo = os.path.join(str(tmp_path), "composed")
        tracked = os.path.join(repo, "server.py")
        assert os.path.isfile(tracked), "first compose did not commit the work"
        with open(tracked, "a", encoding="utf-8") as f:
            f.write("# an interrupted compose left this behind\n")

        # The second run is a different run in the same runs dir. Before the fix this
        # raised CalledProcessError from `git checkout -B`.
        compose("run_000000_992")

        assert engine.subprocess.run(
            ["git", "-C", repo, "status", "--porcelain"],
            capture_output=True, text=True, timeout=20).stdout.strip() == ""
    finally:
        # Restore the module for the rest of the session (its _RUNS_DIR is read at
        # import time, so the reload above rebound it to tmp_path).
        monkeypatch.delenv("WORKSHOP_RUNS_DIR", raising=False)
        importlib.reload(importlib.import_module("engine"))
