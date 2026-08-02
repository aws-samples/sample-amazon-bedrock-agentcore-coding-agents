"""Tests for isolated role work and ONE pull request's tree.

There is no assembled multi-role candidate any more: each role gets one pull
request against the repository's default branch, and ``apply_patch`` materialises
exactly what merging THAT one pull request would produce. So the load-bearing
property here is no longer "who wins when two roles touch a path", it is "a patch
whose base moved under it is STALE and never overwrites what somebody already
merged".
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import work_items  # noqa: E402


def _item(run_id: str, agent: str, token: str,
          base_branch: str = "main") -> work_items.WorkItem:
    return work_items.WorkItem.create(
        run_id, agent, agent.replace("-", " "), agent.split("-")[0],
        base_branch=base_branch, token=token,
    )


def test_dependency_order_is_stable_and_rejects_cycles():
    backend = _item("run_1", "backend", "a")
    frontend = _item("run_1", "frontend", "b")
    frontend.depends_on = [backend.work_id]
    assert work_items.dependency_order([frontend, backend]) == [backend, frontend]

    backend.depends_on = [frontend.work_id]
    with pytest.raises(work_items.DependencyCycle, match="WORK_DEPENDENCY_CYCLE"):
        work_items.dependency_order([backend, frontend])


def test_pull_request_tree_is_the_base_plus_only_this_roles_patch(tmp_path):
    """``apply_patch`` materialises exactly what merging ONE pull request produces.

    Every role targets the repository's default branch directly, gets its own
    collision-free work id, branch, and local worktree branch, and its tree is the
    base as it stands plus its own additions, edits, and deletions. Nothing from a
    sibling role appears, because a pull request is the unit a person merges.
    """
    backend = _item("run_1", "backend", "a")
    frontend = _item("run_1", "frontend", "b")
    assert backend.work_id != frontend.work_id
    assert backend.branch != frontend.branch
    # Both pull requests target the SAME default branch: there is no run-scoped
    # integration branch above them.
    assert backend.base_branch == frontend.base_branch == "main"
    assert backend.worktree_branch == "worktree-work-backend-a"
    assert frontend.worktree_branch == "worktree-work-frontend-b"
    assert backend.runtime_subdir("run_1").endswith(backend.work_id)
    assert frontend.runtime_subdir("run_1").endswith(frontend.work_id)
    assert "/work/" in backend.runtime_subdir("run_1")

    base = tmp_path / "base"
    back_root = tmp_path / "backend"
    base.mkdir()
    (back_root / "service").mkdir(parents=True)
    (base / "keep.txt").write_text("before\n")
    (base / "delete.txt").write_text("remove me\n")
    (back_root / "keep.txt").write_text("after\n")
    (back_root / "README.md").write_text("shared contract\n")
    (back_root / "service" / "app.py").write_text("print('api')\n")

    destination = tmp_path / "pr-backend"
    digest = work_items.apply_patch(
        str(base), backend, str(base), str(back_root), str(destination))

    assert (destination / "keep.txt").read_text() == "after\n"
    assert (destination / "README.md").read_text() == "shared contract\n"
    assert (destination / "service" / "app.py").is_file()
    assert not (destination / "delete.txt").exists()
    assert backend.changed_files == ["README.md", "keep.txt", "service/app.py"]
    assert backend.deleted_files == ["delete.txt"]
    assert digest == work_items.tree_digest(str(destination))

    # A sibling's pull request is built from the SAME base and carries only its own
    # files: the two trees never see each other.
    front_root = tmp_path / "frontend"
    (front_root / "ui").mkdir(parents=True)
    (front_root / "keep.txt").write_text("before\n")
    (front_root / "delete.txt").write_text("remove me\n")
    (front_root / "ui" / "app.ts").write_text("console.log('ui')\n")
    front_dest = tmp_path / "pr-frontend"
    work_items.apply_patch(
        str(base), frontend, str(base), str(front_root), str(front_dest))
    assert frontend.changed_files == ["ui/app.ts"]
    assert frontend.deleted_files == []
    assert (front_dest / "ui" / "app.ts").is_file()
    assert not (front_dest / "service").exists()
    assert not (front_dest / "README.md").exists()
    assert (front_dest / "keep.txt").read_text() == "before\n"
    assert (front_dest / "delete.txt").is_file()


def test_a_base_that_moved_under_a_pull_request_is_stale_never_a_winner(tmp_path):
    """This is how "somebody merged before you" is detected, and it must never
    resolve itself.

    The patch was computed against the exact base the role received. If the default
    branch has since changed a path this role also changed, applying the patch would
    quietly overwrite a change that is already merged. That is a STALE pull request:
    the owning role refreshes and gets re-checked, exactly as a person would. No
    winner is picked, and the tree is never published.
    """
    stale = _item("run_1", "frontend", "b")
    received = tmp_path / "received"        # the base this role was handed
    moved = tmp_path / "moved"              # the default branch as it stands now
    work = tmp_path / "work"                # what the role wrote
    destination = tmp_path / "pr-frontend"
    for root in (received, moved, work):
        root.mkdir()
    (received / "contract.json").write_text('{"version":1}\n')
    (moved / "contract.json").write_text('{"version":2,"owner":"backend"}\n')
    (work / "contract.json").write_text('{"version":2,"owner":"frontend"}\n')

    with pytest.raises(work_items.StalePatch) as caught:
        work_items.apply_patch(
            str(moved), stale, str(received), str(work), str(destination))

    assert str(caught.value).startswith("STALE_PATCH:")
    assert caught.value.conflicts[0]["path"] == "contract.json"
    assert caught.value.conflicts[0]["second_work_id"] == stale.work_id
    assert "since this role received it" in caught.value.conflicts[0]["reason"]
    assert not destination.exists(), (
        "a stale pull request tree must never be published")
    # Neither side won: the merged version on the base is untouched, and the role's
    # proposal was not adopted.
    assert (moved / "contract.json").read_text() == '{"version":2,"owner":"backend"}\n'

    # Sharp contrast: the same path, but the base already holds EXACTLY what this
    # role wanted. Nothing was overwritten, so nothing is stale.
    agreeing = _item("run_1", "frontend", "c")
    agreed_work = tmp_path / "agreed"
    agreed_work.mkdir()
    (agreed_work / "contract.json").write_text('{"version":2,"owner":"backend"}\n')
    agreed_dest = tmp_path / "pr-agreeing"
    work_items.apply_patch(
        str(moved), agreeing, str(received), str(agreed_work), str(agreed_dest))
    assert (agreed_dest / "contract.json").read_text() == (
        '{"version":2,"owner":"backend"}\n')


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
