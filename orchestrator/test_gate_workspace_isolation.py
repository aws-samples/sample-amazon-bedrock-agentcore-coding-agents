"""The check is verification infrastructure, not application source.

A real check searched the application tree for endpoint implementations and
mistook its own source for a disconnected server. Exercise the filesystem seen
by an actual executable, including a repair that reuses the same check.
"""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import engine
import reviewer


def test_staged_check_does_not_become_part_of_the_application(tmp_path):
    root = tmp_path / "run"
    base, tree = root / "base", root / "item-tree"
    base.mkdir(parents=True)
    tree.mkdir()
    (base / "source.txt").write_text("base source")
    (tree / "source.txt").write_text("candidate source")
    authored = tmp_path / "validator" / "acceptance_check"
    authored.parent.mkdir()
    authored.write_text(
        '#!/bin/sh\n'
        'test "$PWD" = "$WORKSHOP_WORK_DIR" || exit 11\n'
        'case "$0" in "$WORKSHOP_WORK_DIR"/*) echo "check mixed with app"; exit 12;; esac\n'
        'test -f source.txt || exit 13\n'
        'test ! -f acceptance_check || exit 14\n'
        'test ! -f previous-execution.dat || exit 15\n'
        'echo "application tree and execution directory agree"\n'
    )
    digest = hashlib.sha256(authored.read_bytes()).hexdigest()
    item = SimpleNamespace(work_id="work_backend")
    run = SimpleNamespace(
        workdir=str(root), integration_base_dir=str(base),
        item_tree_dir=lambda _: str(tree), log=lambda _: None,
    )
    runner = engine.Engine.__new__(engine.Engine)
    staged = runner._gate_dir_check_path(run, str(authored), item)
    workspace = root / "gate" / item.work_id
    result = reviewer.run_gate(staged, str(workspace), "fixture for execution isolation")
    assert result["passed"], result
    assert not Path(staged).is_relative_to(workspace)
    assert hashlib.sha256(Path(staged).read_bytes()).hexdigest() == digest

    # Rebuild the source tree for repair without rewriting the check or retaining
    # artifacts left by its previous execution.
    (workspace / "previous-execution.dat").write_text("old runtime output")
    (Path(staged).parent / "previous-execution.dat").write_text("old checker output")
    (tree / "source.txt").write_text("repaired candidate")
    restaged = runner._gate_dir_check_path(run, staged, item)
    result = reviewer.run_gate(restaged, str(workspace), "fixture for execution isolation")
    assert result["passed"], result
    assert (workspace / "source.txt").read_text() == "repaired candidate"
    assert not (Path(restaged).parent / "previous-execution.dat").exists()
    assert hashlib.sha256(Path(restaged).read_bytes()).hexdigest() == digest


def test_review_executes_in_the_selected_tree_not_the_checks_directory(tmp_path):
    workspace = tmp_path / "review-target"
    workspace.mkdir()
    (workspace / "source.txt").write_text("target source")
    checks = tmp_path / "checks"
    checks.mkdir()
    authored = checks / "check"
    authored.write_text(
        '#!/bin/sh\n'
        'test "$PWD" = "$WORKSHOP_WORK_DIR" || exit 21\n'
        'test "$(cat source.txt)" = "target source" || exit 22\n'
        'echo "reviewed the selected target"\n'
    )
    result = reviewer.run_gate(str(authored), str(workspace), "review the target")
    assert result["passed"], result
