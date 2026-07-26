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
import os
import stat
import sys

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


def _run(agents):
    engine = importlib.import_module("engine")
    run = engine.Run(run_id="run_000000_771", task="build a service",
                     agents=list(agents), roles=list(agents))
    return engine, run


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

        run._acceptance_test_file = eng._gate_dir_check_path(run, authored)
        gate = reviewer.run_gate(run)
        assert gate["passed"] is False, gate
    finally:
        import shutil
        shutil.rmtree(run.workdir, ignore_errors=True)
