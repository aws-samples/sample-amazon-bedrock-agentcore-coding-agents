"""The authored check must run BESIDE the work it was authored beside.

Every role shares ONE directory in the runtime workspace (`/mnt/s3files/<run_id>`),
so the validator writes its check next to the builders' files and addresses them as
siblings: `os.path.dirname(__file__) + "/server.py"`, or plain `./server.py`. That is
correct where it was written, and it is what real validators actually emit.

The engine then reads each role's tree back into a SEPARATE `role-<agent>` directory,
which compose needs so every file is attributable to its author. That split used to
leave the check ALONE in the validator's directory with the deliverable one level
away, so the gate ran it somewhere its siblings did not exist: every file check
failed, no service started, and a CORRECT deliverable was graded RED, burning the
bounded retry to `needs_human`.

Verified on a live event box (2026-07-26): the same validator-authored check scored
`0 passed, 4 failed` in the validator's directory and `45 passed, 0 failed` beside
the work.
"""

from __future__ import annotations

import importlib
import inspect
import os
import stat
import sys
import shutil

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))

_SIBLING_CHECK = """#!/usr/bin/env python3
# A REALISTIC authored check: it resolves the deliverable relative to ITSELF, the
# way a validator that wrote it beside the work naturally would.
import os, sys
DIR = os.path.dirname(os.path.abspath(__file__))
ok = os.path.isfile(os.path.join(DIR, "server.py"))
print(("PASS" if ok else "FAIL") + ": server.py exists next to the check")
sys.exit(0 if ok else 1)
"""


def test_supported_toolchains_cross_every_execution_boundary(
        monkeypatch, tmp_path):
    """The advertised languages and their build output reach the real gate."""
    import runtime_exec
    import runtime_stage

    assert "node_modules" in runtime_exec._TREE_EXCLUDES
    assert "dist" not in runtime_exec._TREE_EXCLUDES
    assert "build" not in runtime_exec._TREE_EXCLUDES

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / "dist").mkdir(parents=True)
    (source / "dist" / "app.js").write_text("console.log('ready')\n")
    monkeypatch.setattr(
        shutil, "copystat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(524, "NFS")),
    )
    assert runtime_stage.copy_tree_files(
        str(source), str(destination)) == 1
    assert (destination / "dist" / "app.js").read_text() == (
        "console.log('ready')\n")

    candidate = tmp_path / "candidate"
    (candidate / "dist").mkdir(parents=True)
    (candidate / "dist" / "app.js").write_text("console.log('candidate')\n")
    mount = tmp_path / "mount"
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(mount))
    monkeypatch.setattr(
        shutil, "copytree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(524, "NFS")),
    )
    checker_run = type("CheckerRun", (), {
        "run_id": "run_nfs_checkout",
        "candidate_dir": str(candidate),
        "log": lambda *_args, **_kwargs: None,
        "runtime_subdir": lambda _self, agent: f"work/{agent}",
    })()
    engine = importlib.import_module("engine")
    eng = engine.Engine.__new__(engine.Engine)
    eng.executor = type("Executor", (), {"name": "agentcore"})()
    eng._prepare_checker_checkout(checker_run, "claude-code-validator")
    staged = mount / "work" / "claude-code-validator" / "dist" / "app.js"
    assert staged.read_text() == "console.log('candidate')\n"

    validator = open(os.path.join(
        _REPO, "coding-agents", "claude-code-validator", "Dockerfile"),
        encoding="utf-8").read()
    gate = open(os.path.join(_REPO, "orchestrator-agent", "Dockerfile"),
                encoding="utf-8").read()
    assert "node:22-slim" in validator and "node:22-slim" in gate
    assert "python3" in validator and "python3" in gate

    rule = engine._SUPPORTED_TOOLCHAINS_RULE.lower()
    assert "python" in rule and "node.js 22" in rule
    assert "javascript/typescript" in rule
    for method in (engine.Engine._cli_backend_server,
                   engine.Engine._cli_frontend_work):
        assert "_SUPPORTED_TOOLCHAINS_RULE" in inspect.getsource(method)
    validator_prompt = inspect.getsource(engine.Engine._cli_validator_authors_test)
    assert "Node.js 22" in validator_prompt and "JavaScript/TypeScript" in validator_prompt


def _run(agents):
    engine = importlib.import_module("engine")
    run = engine.Run(run_id="run_000000_771", task="build a service",
                     agents=list(agents),
                     roles={agent: agent for agent in agents})
    return engine, run


def _assemble_candidate(engine, run, builder):
    """Follow the production boundary: exact base + role patch -> candidate."""
    import work_items

    os.makedirs(run.integration_base_dir, exist_ok=True)
    item = run.work_items.get("claude-code")
    if item is None:
        item = work_items.WorkItem.create(
            run.run_id, "claude-code", "backend-builder", "backend",
            token="gate-test")
        run.work_items["claude-code"] = item
    item_base = run.item_base_dir("claude-code")
    os.makedirs(item_base, exist_ok=True)
    candidate = work_items.assemble_candidate(
        run.integration_base_dir,
        [(item, item_base, builder)],
        run.candidate_dir,
        exclude=engine._work_patch_excluded,
    )
    run.integration_candidate = candidate.public()


def test_gate_dir_puts_the_check_beside_every_role_file(tmp_path):
    engine, run = _run(["claude-code", "claude-code-validator"])
    eng = engine.Engine.__new__(engine.Engine)     # no threads needed

    builder = run.roledir("claude-code")
    validator = run.roledir("claude-code-validator")
    os.makedirs(builder, exist_ok=True)
    os.makedirs(validator, exist_ok=True)
    try:
        # The builder's own files, plus the harness steering the engine installed.
        with open(os.path.join(builder, "server.py"), "w") as f:
            f.write("# the deliverable\n")
        with open(os.path.join(builder, "CLAUDE.md"), "w") as f:
            f.write("harness steering, not the deliverable\n")
        os.makedirs(os.path.join(builder, "skills", "backend-engineering"), exist_ok=True)
        with open(os.path.join(builder, "skills", "backend-engineering", "SKILL.md"), "w") as f:
            f.write("harness skill\n")
        # The validator's authored check, alone in its own role directory.
        authored = os.path.join(validator, "acceptance_check")
        with open(authored, "w") as f:
            f.write(_SIBLING_CHECK)
        os.chmod(authored, os.stat(authored).st_mode | stat.S_IEXEC)

        _assemble_candidate(engine, run, builder)
        staged = eng._gate_dir_check_path(run, authored)

        gate_dir = os.path.dirname(staged)
        assert os.path.isfile(os.path.join(gate_dir, "server.py")), (
            "the builder's file is not beside the check, so a check that resolves "
            "its siblings will fail on a correct deliverable")
        assert os.path.basename(staged) == "acceptance_check"
        # The harness is ours, not the deliverable: it must not be presented as work.
        assert not os.path.exists(os.path.join(gate_dir, "CLAUDE.md"))
        assert not os.path.exists(os.path.join(gate_dir, "skills"))
        # The per-role dirs are untouched, so compose still attributes every file.
        assert os.path.isfile(os.path.join(builder, "server.py"))
        assert os.path.isfile(authored)
    finally:
        import shutil
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_a_sibling_resolving_check_passes_through_the_real_gate():
    """End to end: the gate must score a correct deliverable GREEN.

    This is the regression that matters. Before the fix this exact shape returned
    `passed: False` with the file check failing, which is a red gate on work that
    met the request.
    """
    engine, run = _run(["claude-code", "claude-code-validator"])
    reviewer = importlib.import_module("reviewer")
    eng = engine.Engine.__new__(engine.Engine)

    builder = run.roledir("claude-code")
    validator = run.roledir("claude-code-validator")
    os.makedirs(builder, exist_ok=True)
    os.makedirs(validator, exist_ok=True)
    try:
        with open(os.path.join(builder, "server.py"), "w") as f:
            f.write("# the deliverable\n")
        authored = os.path.join(validator, "acceptance_check")
        with open(authored, "w") as f:
            f.write(_SIBLING_CHECK)
        os.chmod(authored, os.stat(authored).st_mode | stat.S_IEXEC)

        _assemble_candidate(engine, run, builder)
        run._acceptance_test_file = eng._gate_dir_check_path(run, authored)
        gate = reviewer.run_gate(run)

        assert gate["passed"] is True, (
            "a correct deliverable was graded RED because the check could not see "
            f"the work it was authored beside: {gate}")
        assert "server.py exists" in (gate.get("output") or "")
    finally:
        import shutil
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_a_genuinely_broken_deliverable_still_fails():
    """The reunion must not become a way to pass: no work, still red."""
    engine, run = _run(["claude-code", "claude-code-validator"])
    reviewer = importlib.import_module("reviewer")
    eng = engine.Engine.__new__(engine.Engine)

    builder = run.roledir("claude-code")
    validator = run.roledir("claude-code-validator")
    os.makedirs(builder, exist_ok=True)
    os.makedirs(validator, exist_ok=True)
    try:
        # The builder wrote something, but NOT what the check requires.
        with open(os.path.join(builder, "notes.txt"), "w") as f:
            f.write("no server here\n")
        authored = os.path.join(validator, "acceptance_check")
        with open(authored, "w") as f:
            f.write(_SIBLING_CHECK)
        os.chmod(authored, os.stat(authored).st_mode | stat.S_IEXEC)

        _assemble_candidate(engine, run, builder)
        run._acceptance_test_file = eng._gate_dir_check_path(run, authored)
        gate = reviewer.run_gate(run)
        assert gate["passed"] is False, gate
    finally:
        import shutil
        shutil.rmtree(run.workdir, ignore_errors=True)


def test_the_gate_workspace_is_rebuilt_each_round_not_added_to():
    """A re-implement round must not be graded against the previous round's files.

    The gate directory is assembled per round. A leftover is a file the NEW check
    never saw, and it can satisfy a check the fixed deliverable no longer
    satisfies: round 2 passing on round 1's evidence. A live run left round 1's
    `issues.db` (created when the check started the service) in place for round 2.
    """
    engine = importlib.import_module("engine")
    eng = engine.Engine.__new__(engine.Engine)
    run = engine.Run(run_id="run_000000_773", task="t",
                     agents=["claude-code", "claude-code-validator"],
                     roles={"claude-code": "backend-builder",
                            "claude-code-validator": "validator"})
    builder = run.roledir("claude-code")
    validator = run.roledir("claude-code-validator")
    os.makedirs(builder, exist_ok=True)
    os.makedirs(validator, exist_ok=True)
    try:
        with open(os.path.join(builder, "server.py"), "w") as f:
            f.write("# round 1\n")
        authored = os.path.join(validator, "acceptance_check")
        with open(authored, "w") as f:
            f.write(_SIBLING_CHECK)
        os.chmod(authored, os.stat(authored).st_mode | stat.S_IEXEC)

        _assemble_candidate(engine, run, builder)
        staged = eng._gate_dir_check_path(run, authored)
        gate_dir = os.path.dirname(staged)
        # Round 1's check started the service, which dropped state in the gate dir.
        with open(os.path.join(gate_dir, "issues.db"), "w") as f:
            f.write("round 1 state\n")

        # Round 2: the builder replaced its file; the leftover must be gone.
        os.remove(os.path.join(builder, "server.py"))
        with open(os.path.join(builder, "server2.py"), "w") as f:
            f.write("# round 2\n")
        _assemble_candidate(engine, run, builder)
        staged2 = eng._gate_dir_check_path(run, authored)
        gate_dir2 = os.path.dirname(staged2)
        assert not os.path.exists(os.path.join(gate_dir2, "issues.db")), (
            "round 1's run-time state survived into round 2's gate workspace")
        assert not os.path.exists(os.path.join(gate_dir2, "server.py")), (
            "a file the builder deleted is still being graded")
        assert os.path.isfile(os.path.join(gate_dir2, "server2.py"))


        # And a stale check a BUILDER read back from the shared mount must never
        # shadow the validator's freshly authored one. Belt and braces: the authored
        # copy is written LAST, so it already wins; this pins that it keeps winning
        # if that ordering is ever changed.
        with open(os.path.join(builder, "acceptance_check"), "w") as f:
            f.write("#!/bin/sh\necho stale round-1 check\nexit 0\n")
        _assemble_candidate(engine, run, builder)
        staged3 = eng._gate_dir_check_path(run, authored)
        with open(staged3, encoding="utf-8") as f:
            body = f.read()
        assert "stale round-1 check" not in body, (
            "the gate is about to run a stale check a builder carried back, not the "
            "one the validator authored this round")
    finally:
        shutil.rmtree(run.workdir, ignore_errors=True)
