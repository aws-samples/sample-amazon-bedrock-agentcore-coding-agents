"""The pull request carries the deliverable ONCE, not once per role.

Every role shares ONE directory in the runtime workspace, so each role's
read-back tree is a view of the SAME files. Compose used to copy one directory
per role, so a real 3-role run on a live event box published 21 files that were
really 7: the same `server.py` and the same 979-line `index.html` committed under
`claude-code/` and again under `opencode/`. A reviewer cannot review that.

It also published what the engine and the running service left behind: the
harness steering we installed (`CLAUDE.md`, `skills/`), and the SQLite sidecars
the validator's check created when it started the deliverable (`issues.db-wal`,
`issues.db-shm`).

A genuine conflict (two roles writing DIFFERENT content to the same path) must
still be visible rather than silently resolved.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))


def _seed(run, agent_id, files):
    root = run.roledir(agent_id)
    for rel, body in files.items():
        dest = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(body)


def test_shared_workspace_is_committed_once_without_scaffolding_or_run_state():
    engine = importlib.import_module("engine")
    eng = engine.Engine.__new__(engine.Engine)
    run = engine.Run(run_id="run_000000_881", task="build an issue tracker",
                     agents=["claude-code", "opencode", "claude-code-validator"],
                     roles={"claude-code": "backend-builder",
                            "opencode": "frontend-builder",
                            "claude-code-validator": "validator"})
    try:
        # The SAME workspace, as every role reads it back.
        shared = {"server.py": "# the service\n",
                  "static/index.html": "<!doctype html><title>tracker</title>\n",
                  "start.sh": "#!/bin/sh\npython3 server.py\n"}
        # Plus what the engine installed and what running the code produced.
        noise = {"CLAUDE.md": "harness steering\n",
                 "skills/backend-engineering/SKILL.md": "harness skill\n",
                 "issues.db-wal": "sqlite write-ahead log\n",
                 "issues.db-shm": "sqlite shared memory\n",
                 "__pycache__/server.cpython-311.pyc": "bytecode\n"}
        for agent in ("claude-code", "opencode"):
            _seed(run, agent, {**shared, **noise})
        _seed(run, "claude-code-validator", {"acceptance_check": "#!/bin/sh\nexit 0\n",
                                             "CLAUDE.md": "validator steering\n"})

        run._acceptance_test_file = os.path.join(
            run.roledir("claude-code-validator"), "acceptance_check")
        run.gate = {"passed": True, "summary": "39 passed, 0 failed"}
        eng._compose_commit_locked(run)

        diff = engine.public_diff(run)
        paths = sorted(f["path"] for f in diff["files"])

        # The deliverable, once, at the agents' own paths.
        for rel in shared:
            assert rel in paths, (rel, paths)
            assert sum(1 for p in paths if p.endswith(rel)) == 1, (
                f"{rel} was published more than once: {paths}")
        # No duplicated per-role copies of the same project.
        assert not [p for p in paths if p.startswith("claude-code/")], paths
        assert not [p for p in paths if p.startswith("opencode/")], paths
        # Scaffolding and run-time state stay out of the pull request.
        for junk in ("CLAUDE.md", "skills/backend-engineering/SKILL.md",
                     "issues.db-wal", "issues.db-shm",
                     "__pycache__/server.cpython-311.pyc"):
            assert junk not in paths, (junk, paths)
        # The authored check still ships, so a reviewer can rerun the exact gate.
        assert "acceptance_check" in paths, paths
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_a_real_conflict_between_two_roles_is_reported_not_hidden():
    """Same path, DIFFERENT content: both must survive, one flagged."""
    engine = importlib.import_module("engine")
    eng = engine.Engine.__new__(engine.Engine)
    run = engine.Run(run_id="run_000000_882", task="t",
                     agents=["claude-code", "opencode"],
                     roles={"claude-code": "backend-builder",
                            "opencode": "frontend-builder"})
    try:
        _seed(run, "claude-code", {"README.md": "written by the backend\n"})
        _seed(run, "opencode", {"README.md": "written by the frontend\n"})
        run.gate = {"passed": True, "summary": "ok"}
        eng._compose_commit_locked(run)
        paths = sorted(f["path"] for f in engine.public_diff(run)["files"])
        assert "README.md" in paths, paths
        assert any(p.startswith("CONFLICT-") for p in paths), (
            "two roles wrote different content to the same path and the second was "
            f"silently dropped: {paths}")
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_the_authored_check_ships_once_and_never_as_a_conflict():
    """A re-implement round must not report the check as a role conflict.

    The check lives in the SAME shared mount as the deliverable, so every role
    reads it back as if it were their own file. On a second round the builders
    carry the PREVIOUS round's copy while the validator has just written a new
    one, so comparing them found a difference and a live 3-role run published
    `CONFLICT-claude-code-validator/acceptance_check` beside the real one. The
    check must ship exactly once, from the validator's own artifact.
    """
    engine = importlib.import_module("engine")
    eng = engine.Engine.__new__(engine.Engine)
    run = engine.Run(run_id="run_000000_883", task="t",
                     agents=["claude-code", "opencode", "claude-code-validator"],
                     roles={"claude-code": "backend-builder",
                            "opencode": "frontend-builder",
                            "claude-code-validator": "validator"})
    try:
        # Both builders carry ROUND 1's check back from the shared mount...
        for agent in ("claude-code", "opencode"):
            _seed(run, agent, {"server.py": "# round 2 service\n",
                               "acceptance_check": "#!/bin/sh\n# ROUND 1 check\nexit 0\n"})
        # ...while the validator has authored a DIFFERENT one for round 2.
        _seed(run, "claude-code-validator",
              {"acceptance_check": "#!/bin/sh\n# ROUND 2 check\nexit 0\n"})
        run._acceptance_test_file = os.path.join(
            run.roledir("claude-code-validator"), "acceptance_check")
        run.gate = {"passed": True, "summary": "ok"}
        eng._compose_commit_locked(run)

        paths = sorted(f["path"] for f in engine.public_diff(run)["files"])
        assert "acceptance_check" in paths, paths
        assert not [p for p in paths if "CONFLICT" in p], (
            f"the authored check was reported as a role conflict: {paths}")
        assert sum(1 for p in paths if p.endswith("acceptance_check")) == 1, paths
        # And the shipped one is the round the gate actually ran.
        import subprocess
        body = subprocess.run(
            ["git", "-C", os.path.join(engine._RUNS_DIR, "composed"),
             "show", f"run/{run.run_id}:acceptance_check"],
            capture_output=True, text=True).stdout
        assert "ROUND 2" in body, body
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)
