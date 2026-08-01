"""Tests for isolated role work and integration candidate assembly."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import work_items  # noqa: E402


def _item(run_id: str, agent: str, token: str) -> work_items.WorkItem:
    return work_items.WorkItem.create(
        run_id, agent, agent.replace("-", " "), agent.split("-")[0],
        token=token,
    )


def test_dependency_order_is_stable_and_rejects_cycles():
    backend = _item("run_1", "backend", "a")
    frontend = _item("run_1", "frontend", "b")
    frontend.depends_on = [backend.work_id]
    assert work_items.dependency_order([frontend, backend]) == [backend, frontend]

    backend.depends_on = [frontend.work_id]
    with pytest.raises(work_items.DependencyCycle, match="WORK_DEPENDENCY_CYCLE"):
        work_items.dependency_order([backend, frontend])


def test_candidate_applies_disjoint_and_identical_role_changes(tmp_path):
    backend = _item("run_1", "backend", "a")
    frontend = _item("run_1", "frontend", "b")
    assert backend.work_id != frontend.work_id
    assert backend.branch != frontend.branch
    assert backend.base_branch == frontend.base_branch
    assert backend.worktree_branch == "worktree-work-backend-a"
    assert frontend.worktree_branch == "worktree-work-frontend-b"
    assert backend.runtime_subdir("run_1").endswith(backend.work_id)
    assert frontend.runtime_subdir("run_1").endswith(frontend.work_id)
    assert "/work/" in backend.runtime_subdir("run_1")

    base = tmp_path / "base"
    back_root = tmp_path / "backend"
    front_root = tmp_path / "frontend"
    base.mkdir()
    (back_root / "service").mkdir(parents=True)
    (front_root / "ui").mkdir(parents=True)
    (base / "keep.txt").write_text("before\n")
    (base / "delete.txt").write_text("remove me\n")
    (back_root / "keep.txt").write_text("after\n")
    (back_root / "README.md").write_text("shared contract\n")
    (back_root / "service" / "app.py").write_text("print('api')\n")
    (front_root / "keep.txt").write_text("before\n")
    (front_root / "delete.txt").write_text("remove me\n")
    (front_root / "README.md").write_text("shared contract\n")
    (front_root / "ui" / "app.ts").write_text("console.log('ui')\n")

    candidate = work_items.assemble_candidate(
        str(base),
        [(backend, str(base), str(back_root)),
         (frontend, str(base), str(front_root))],
        str(tmp_path / "candidate"),
    )

    assert candidate.files == [
        "README.md", "keep.txt", "service/app.py", "ui/app.ts"]
    assert (tmp_path / "candidate" / "keep.txt").read_text() == "after\n"
    assert not (tmp_path / "candidate" / "delete.txt").exists()
    assert (tmp_path / "candidate" / "service" / "app.py").is_file()
    assert candidate.owners["README.md"] == [
        backend.work_id, frontend.work_id]
    assert candidate.owners["service/app.py"] == [backend.work_id]
    assert candidate.owners["ui/app.ts"] == [frontend.work_id]
    assert backend.changed_files == ["README.md", "keep.txt", "service/app.py"]
    assert backend.deleted_files == ["delete.txt"]
    assert candidate.digest == work_items.tree_digest(str(tmp_path / "candidate"))


def test_conflicting_role_changes_never_pick_a_winner(tmp_path):
    backend = _item("run_1", "backend", "a")
    frontend = _item("run_1", "frontend", "b")
    back_root = tmp_path / "backend"
    front_root = tmp_path / "frontend"
    back_root.mkdir()
    front_root.mkdir()
    (back_root / "package.json").write_text('{"name":"backend"}\n')
    (front_root / "package.json").write_text('{"name":"frontend"}\n')
    destination = tmp_path / "candidate"
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(work_items.IntegrationConflict) as caught:
        work_items.assemble_candidate(
            str(base),
            [(backend, str(base), str(back_root)),
             (frontend, str(base), str(front_root))],
            str(destination),
        )

    assert caught.value.conflicts == [{
        "path": "package.json",
        "first_work_id": backend.work_id,
        "second_work_id": frontend.work_id,
        "reason": "the integration base and this work item changed the path differently",
    }]
    assert not destination.exists(), "a conflicted candidate must never be published"

    first = _item("run_1", "backend", "a")
    stale = _item("run_1", "frontend", "b")
    original = tmp_path / "original"
    integrated = tmp_path / "integrated"
    first_work = tmp_path / "first"
    stale_work = tmp_path / "stale"
    for root in (original, integrated, first_work, stale_work):
        root.mkdir()
    (original / "contract.json").write_text('{"version":1}\n')
    (integrated / "contract.json").write_text('{"version":2,"owner":"backend"}\n')
    (first_work / "contract.json").write_text('{"version":2,"owner":"backend"}\n')
    (stale_work / "contract.json").write_text('{"version":2,"owner":"frontend"}\n')

    with pytest.raises(work_items.IntegrationConflict) as caught:
        work_items.assemble_candidate(
            str(integrated),
            [(stale, str(original), str(stale_work))],
            str(tmp_path / "stale-candidate"),
        )

    assert caught.value.conflicts[0]["path"] == "contract.json"
    assert caught.value.conflicts[0]["second_work_id"] == stale.work_id


def test_refresh_preserves_clean_work_and_surfaces_same_path_conflicts(tmp_path):
    item = _item("run_1", "frontend", "b")
    previous = tmp_path / "previous"
    work = tmp_path / "work"
    latest = tmp_path / "latest"
    refreshed = tmp_path / "refreshed"
    for root in (previous, work, latest):
        root.mkdir()
    (previous / "shared.txt").write_text("v1\n")
    (work / "shared.txt").write_text("v1\n")
    (work / "ui.ts").write_text("export const ui = true\n")
    (latest / "shared.txt").write_text("v2 from backend\n")

    conflicts = work_items.prepare_refresh_checkout(
        item, str(previous), str(work), str(latest), str(refreshed))

    assert conflicts == []
    assert (refreshed / "shared.txt").read_text() == "v2 from backend\n"
    assert (refreshed / "ui.ts").read_text() == "export const ui = true\n"
    assert (refreshed / ".workshop" / "refresh.json").is_file()
    assert item.stale is True and item.refreshes == 1

    item = _item("run_1", "frontend", "conflict")
    previous = tmp_path / "previous-conflict"
    work = tmp_path / "work-conflict"
    latest = tmp_path / "latest-conflict"
    refreshed = tmp_path / "refreshed-conflict"
    for root in (previous, work, latest):
        root.mkdir()
    (previous / "contract.json").write_text('{"version":1}\n')
    (work / "contract.json").write_text('{"owner":"frontend"}\n')
    (latest / "contract.json").write_text('{"owner":"backend"}\n')

    conflicts = work_items.prepare_refresh_checkout(
        item, str(previous), str(work), str(latest), str(refreshed))

    assert conflicts[0]["path"] == "contract.json"
    # Latest integration remains the live source; the role's prior proposal is
    # evidence for the owning role to reconcile, not an automatic winner.
    assert (refreshed / "contract.json").read_text() == '{"owner":"backend"}\n'
    assert (refreshed / ".workshop" / "prior-work" /
            "contract.json").read_text() == '{"owner":"frontend"}\n'


def test_linked_worktrees_isolate_roles_and_survive_refresh(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "shared.txt").write_text("base\n")
    repo = tmp_path / "git" / "repo.git"
    backend = _item("run_1", "backend", "a")
    frontend = _item("run_1", "frontend", "b")
    backend_dir = tmp_path / "worktrees" / backend.work_id
    frontend_dir = tmp_path / "worktrees" / frontend.work_id

    commit = work_items.initialize_worktree_repo(str(repo), str(source))
    work_items.add_worktree(
        str(repo), str(backend_dir), backend.worktree_branch, commit)
    work_items.add_worktree(
        str(repo), str(frontend_dir), frontend.worktree_branch, commit)

    assert (backend_dir / ".git").is_file()
    assert (frontend_dir / ".git").is_file()
    assert subprocess.check_output(
        ["git", "-C", str(backend_dir), "branch", "--show-current"],
        text=True,
    ).strip() == backend.worktree_branch

    (backend_dir / "backend.py").write_text("print('backend')\n")
    assert not (frontend_dir / "backend.py").exists()

    refreshed = tmp_path / "refreshed"
    refreshed.mkdir()
    (refreshed / "shared.txt").write_text("latest\n")
    (refreshed / "frontend.ts").write_text("export const ready = true\n")
    gitlink = (frontend_dir / ".git").read_text()

    assert work_items.reset_worktree(
        str(repo),
        str(frontend_dir),
        str(refreshed),
        "Refresh frontend baseline",
    ) == 2
    assert (frontend_dir / ".git").read_text() == gitlink
    assert (frontend_dir / "shared.txt").read_text() == "latest\n"
    assert (frontend_dir / "frontend.ts").is_file()
    assert subprocess.check_output(
        ["git", "-C", str(frontend_dir), "status", "--porcelain"],
        text=True,
    ) == ""
