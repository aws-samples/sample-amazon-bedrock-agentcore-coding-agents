"""Independent role work: one worktree, one branch, one pull request each.

Each builder gets a stable work id, a local linked Git worktree, and its own GitHub
branch and pull request against the repository's default branch. Builders share a
task and a contract, never a writable tree. That is the ordinary team flow:
separate checkouts, one pull request each, each reviewed and merged on its own. The
common Git metadata stays on local disk; only normalized source archives cross an
AgentCore Runtime boundary.

This module does not decide whether code is correct. It only performs structural
work that must be deterministic:

* issue collision-free work ids and branch names;
* order declared dependencies or reject a cycle;
* materialise ONE pull request's tree (its base plus its own patch) and refuse a
  patch whose base moved under it, rather than silently overwriting a change
  somebody else already merged.

The independent validator remains the checker. It authors an executable for each
pull request, and the engine runs that executable for the verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import fcntl


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return clean or "role"


def worktree_branch(work_id: str) -> str:
    """The local branch checked out for one isolated role turn."""
    return f"worktree-{_slug(work_id)}"


@dataclass
class WorkItem:
    """One role's isolated worktree and pull request lifecycle."""

    work_id: str
    agent: str
    role: str
    capability: str
    kind: str
    branch: str
    base_branch: str
    worktree_branch: str
    depends_on: list[str] = field(default_factory=list)
    state: str = "pending"
    attempt: int = 0
    pr: dict[str, Any] | None = None
    review_rounds: list[dict[str, Any]] = field(default_factory=list)
    merge_state: str | None = None
    base_sha: str | None = None
    base_digest: str | None = None
    patch_digest: str | None = None
    changed_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    head_sha: str | None = None
    stale: bool = False
    refreshes: int = 0
    dependency_refreshes: int = 0
    _patch: "TreePatch | None" = field(default=None, repr=False)

    @classmethod
    def create(cls, run_id: str, agent: str, role: str, capability: str,
               *, kind: str = "builder", base_branch: str = "",
               token: str | None = None) -> "WorkItem":
        """One role's branch and pull request.

        ``base_branch`` is the repository's default branch, passed in rather than
        derived here: every role pull request targets it directly and merges into
        it on its own, so there is no run-scoped branch for this function to name.
        """
        suffix = (token or uuid.uuid4().hex[:10]).lower()
        work_id = f"work_{_slug(agent)}_{suffix}"
        base = base_branch
        return cls(
            work_id=work_id,
            agent=agent,
            role=role,
            capability=capability,
            kind=kind,
            branch=f"workshop/runs/{_slug(run_id)}/{_slug(agent)}-{suffix}",
            base_branch=base,
            worktree_branch=worktree_branch(work_id),
        )

    def runtime_subdir(self, run_id: str) -> str:
        """The role checkout's isolated Runtime exchange name."""
        return f"{run_id}/work/{self.work_id}"

    def public(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "agent": self.agent,
            "role": self.role,
            "capability": self.capability,
            "kind": self.kind,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "worktree_branch": self.worktree_branch,
            "depends_on": list(self.depends_on),
            "state": self.state,
            "attempt": self.attempt,
            "pr": dict(self.pr or {}),
            "review_rounds": list(self.review_rounds),
            "merge_state": self.merge_state,
            "base_sha": self.base_sha,
            "base_digest": self.base_digest,
            "patch_digest": self.patch_digest,
            "changed_files": list(self.changed_files),
            "deleted_files": list(self.deleted_files),
            "head_sha": self.head_sha,
            "stale": self.stale,
            "refreshes": self.refreshes,
            "dependency_refreshes": self.dependency_refreshes,
        }


_REPO_LOCKS: dict[str, threading.Lock] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def _thread_lock(repo_dir: str) -> threading.Lock:
    key = os.path.realpath(repo_dir)
    with _REPO_LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _repo_lock(repo_dir: str):
    """Serialize common Git metadata changes across threads and processes."""
    lock = _thread_lock(repo_dir)
    lock_path = os.path.join(os.path.dirname(repo_dir), "worktree.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with lock, open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git(*args: str, cwd: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"WORKTREE_GIT_ERROR: git {' '.join(args)} failed: {detail}")
    return result


def _copy_worktree_source(source: str, destination: str) -> int:
    """Copy a tracked tree while never importing another repository's metadata."""
    if not os.path.isdir(source):
        raise RuntimeError(f"worktree source does not exist: {source}")
    os.makedirs(destination, exist_ok=True)
    count = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        rel_dir = os.path.relpath(dirpath, source)
        target_dir = (
            destination if rel_dir == "."
            else os.path.join(destination, rel_dir)
        )
        os.makedirs(target_dir, exist_ok=True)
        for filename in sorted(filenames):
            if filename == ".git":
                continue
            src = os.path.join(dirpath, filename)
            if os.path.islink(src):
                raise RuntimeError(
                    f"symbolic links are not portable worktree input: {src}")
            dest = os.path.join(target_dir, filename)
            shutil.copyfile(src, dest)
            shutil.copymode(src, dest)
            count += 1
    return count


def initialize_worktree_repo(repo_dir: str, source_root: str) -> str:
    """Create one run-local common repository from the exact integration base."""
    if os.path.exists(repo_dir):
        raise RuntimeError(
            f"WORKTREE_REPO_EXISTS: refusing to replace {repo_dir}")
    parent = os.path.dirname(repo_dir)
    os.makedirs(parent, exist_ok=True)
    seed = os.path.join(parent, f"seed-{uuid.uuid4().hex[:10]}")
    shutil.rmtree(seed, ignore_errors=True)
    try:
        _copy_worktree_source(source_root, seed)
        _git("init", "-q", "-b", "workshop-base", seed)
        _git("-C", seed, "config", "user.name", "Workshop Coordinator")
        _git("-C", seed, "config", "user.email",
             "workshop-coordinator@example.invalid")
        _git("-C", seed, "add", "-A")
        _git("-C", seed, "commit", "-qm", "Seed integration base",
             "--allow-empty")
        _git("clone", "-q", "--bare", "--no-hardlinks", seed, repo_dir)
        _git("--git-dir", repo_dir, "config", "gc.auto", "0")
        return _git(
            "--git-dir", repo_dir, "rev-parse", "HEAD").stdout.strip()
    finally:
        shutil.rmtree(seed, ignore_errors=True)


def add_worktree(repo_dir: str, destination: str, branch: str,
                 start: str = "HEAD") -> None:
    """Add one role-owned worktree under a unique local branch."""
    if not os.path.isdir(repo_dir):
        raise RuntimeError(f"WORKTREE_REPO_MISSING:{repo_dir}")
    if not branch.startswith("worktree-") or "/" in branch:
        raise RuntimeError(f"unsafe worktree branch: {branch!r}")
    with _repo_lock(repo_dir):
        _git("--git-dir", repo_dir, "worktree", "prune")
        shutil.rmtree(destination, ignore_errors=True)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        _git("--git-dir", repo_dir, "worktree", "add", "-q", "-b", branch,
             destination, start)
        _git("-C", destination, "config", "user.name",
             "Workshop Coordinator")
        _git("-C", destination, "config", "user.email",
             "workshop-coordinator@example.invalid")


def reset_worktree(repo_dir: str, destination: str, source_root: str,
                   message: str) -> int:
    """Replace a role tree with a new clean baseline without breaking its gitlink."""
    gitlink = os.path.join(destination, ".git")
    if not os.path.isfile(gitlink):
        raise RuntimeError(
            f"WORKTREE_GITLINK_MISSING: {destination} is not a linked worktree")
    with _repo_lock(repo_dir):
        _git("-C", destination, "reset", "--hard", "-q")
        for name in os.listdir(destination):
            if name == ".git":
                continue
            victim = os.path.join(destination, name)
            if os.path.isdir(victim):
                shutil.rmtree(victim, ignore_errors=True)
            else:
                os.remove(victim)
        count = _copy_worktree_source(source_root, destination)
        _git("-C", destination, "add", "-A")
        changed = _git(
            "-C", destination, "diff", "--cached", "--quiet",
            check=False).returncode
        if changed:
            _git("-C", destination, "commit", "-qm", message)
        return count


class DependencyCycle(ValueError):
    """The integration plan contains a dependency cycle."""


def dependency_order(items: Iterable[WorkItem]) -> list[WorkItem]:
    """Topologically order work items, preserving input order between peers."""
    ordered_input = list(items)
    by_id = {item.work_id: item for item in ordered_input}
    missing = sorted({
        dep
        for item in ordered_input
        for dep in item.depends_on
        if dep not in by_id
    })
    if missing:
        raise ValueError(f"UNKNOWN_WORK_DEPENDENCY:{','.join(missing)}")

    out: list[WorkItem] = []
    done: set[str] = set()
    while len(out) < len(ordered_input):
        ready = [
            item for item in ordered_input
            if item.work_id not in done
            and all(dep in done for dep in item.depends_on)
        ]
        if not ready:
            blocked = [item.work_id for item in ordered_input
                       if item.work_id not in done]
            raise DependencyCycle(
                "WORK_DEPENDENCY_CYCLE:" + ",".join(blocked))
        for item in ready:
            out.append(item)
            done.add(item.work_id)
    return out


class StalePatch(RuntimeError):
    """A pull request's base moved under it, or its tree is not portable.

    Raised when the default branch changed a path this pull request also changed
    since the role received its base (the role must refresh and re-check, exactly
    as a person would), and for a tree carrying something a patch cannot represent.
    """

    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        self.conflicts = conflicts
        paths = ", ".join(c["path"] for c in conflicts[:8])
        super().__init__(f"STALE_PATCH:{paths}")


@dataclass
class TreePatch:
    """One work item's changes relative to the exact base it received."""

    work_id: str
    changes: dict[str, bytes | None]
    digest: str

    @property
    def changed_files(self) -> list[str]:
        return sorted(path for path, value in self.changes.items()
                      if value is not None)

    @property
    def deleted_files(self) -> list[str]:
        return sorted(path for path, value in self.changes.items()
                      if value is None)

    def public(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "digest": self.digest,
            "changed_files": self.changed_files,
            "deleted_files": self.deleted_files,
        }


_MISSING = object()


def _tree_files(root: str, exclude: Callable[[str], bool] | None = None
                ) -> dict[str, bytes]:
    """Read a regular-file tree without guessing which source files matter."""
    out: dict[str, bytes] = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if exclude and exclude(rel):
                continue
            if os.path.islink(full):
                raise StalePatch([{
                    "path": rel,
                    "first_work_id": "",
                    "second_work_id": "",
                    "reason": "symbolic links are not portable integration changes",
                }])
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out


def tree_digest(root: str,
                exclude: Callable[[str], bool] | None = None) -> str:
    """Content digest for deciding whether code changed between reviews."""
    h = hashlib.sha256()
    for path, content in sorted(_tree_files(root, exclude).items()):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()


def diff_trees(item: WorkItem, base_root: str, work_root: str,
               *, exclude: Callable[[str], bool] | None = None) -> TreePatch:
    """Compute additions, edits, and deletions from the base the role received."""
    base = _tree_files(base_root, exclude)
    work = _tree_files(work_root, exclude)
    changes: dict[str, bytes | None] = {}
    for path in sorted(set(base) | set(work)):
        before = base.get(path, _MISSING)
        after = work.get(path, _MISSING)
        if before == after:
            continue
        changes[path] = None if after is _MISSING else after

    h = hashlib.sha256()
    for path, content in changes.items():
        h.update(path.encode("utf-8"))
        h.update(b"\0delete\0" if content is None else b"\0write\0")
        if content is not None:
            h.update(content)
        h.update(b"\0")
    patch = TreePatch(item.work_id, changes, h.hexdigest())
    item._patch = patch
    item.base_digest = tree_digest(base_root, exclude)
    item.patch_digest = patch.digest
    item.changed_files = patch.changed_files
    item.deleted_files = patch.deleted_files
    return patch


def apply_patch(
    base_root: str,
    item: WorkItem,
    item_base_root: str,
    work_root: str,
    destination: str,
    *,
    exclude: Callable[[str], bool] | None = None,
) -> str:
    """Materialise ONE role's pull request tree: a base plus that role's patch.

    This is what the checker gates and the reviewer reads: exactly what merging
    this one pull request would produce, and nothing from any other role. There is
    no assembled multi-role candidate any more, because a pull request is the unit
    a person reviews and merges.

    ``base_root`` is the repository's default branch as it stands NOW, which is why
    a cross-role defect is still catchable: once one role's pull request has merged,
    the next role's check and review run against a tree that already contains it.

    The three-way staleness guard is kept: the patch was computed against the exact
    base the role received, so if the default branch has since changed a path this
    role also changed, the role's work is stale and must be refreshed rather than
    quietly overwriting someone else's merged change. Returns the tree digest.
    """
    shutil.rmtree(destination, ignore_errors=True)
    scratch = destination + f".tmp-{uuid.uuid4().hex[:10]}"
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)

    conflicts: list[dict[str, str]] = []
    try:
        base_files = _tree_files(base_root, exclude)
        for rel, content in base_files.items():
            dest = os.path.join(scratch, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content)

        item_base = _tree_files(item_base_root, exclude)
        patch = diff_trees(item, item_base_root, work_root, exclude=exclude)
        for rel, proposed in patch.changes.items():
            original = item_base.get(rel, _MISSING)
            now = base_files.get(rel, _MISSING)
            wanted = _MISSING if proposed is None else proposed
            if now == wanted:
                continue
            if now != original:
                conflicts.append({
                    "path": rel,
                    "first_work_id": "",
                    "second_work_id": item.work_id,
                    "reason": "the base branch and this pull request changed the "
                              "path differently since this role received it",
                })
                continue
            dest = os.path.join(scratch, *rel.split("/"))
            if proposed is None:
                try:
                    os.remove(dest)
                except FileNotFoundError:
                    pass
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(proposed)
        if conflicts:
            raise StalePatch(conflicts)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        os.replace(scratch, destination)
        return tree_digest(destination)
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def prepare_refresh_checkout(
    item: WorkItem,
    previous_base: str,
    previous_work: str,
    latest_base: str,
    destination: str,
    *,
    exclude: Callable[[str], bool] | None = None,
) -> list[dict[str, str]]:
    """Rebase prior role work onto the latest integration for its owner to revise.

    Non-conflicting changes are carried forward. A conflicting path keeps the
    latest integration version in place and stores the role's previous proposal
    under ``.workshop/prior-work`` with a manifest. That gives the owning role
    both sides without putting conflict markers or a guessed winner into source.
    The coordination directory is excluded from every published patch.
    """
    old_base = _tree_files(previous_base, exclude)
    old_work = _tree_files(previous_work, exclude)
    latest = _tree_files(latest_base, exclude)
    patch = diff_trees(
        item, previous_base, previous_work, exclude=exclude)

    scratch = destination + f".tmp-{uuid.uuid4().hex[:10]}"
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)
    conflicts: list[dict[str, str]] = []
    try:
        for rel, content in latest.items():
            dest = os.path.join(scratch, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content)

        for rel, proposed in patch.changes.items():
            original = old_base.get(rel, _MISSING)
            current = latest.get(rel, _MISSING)
            wanted = _MISSING if proposed is None else proposed
            dest = os.path.join(scratch, *rel.split("/"))
            if current not in (original, wanted):
                conflicts.append({
                    "path": rel,
                    "work_id": item.work_id,
                    "reason": "latest integration and this role changed the path "
                              "differently",
                })
                if proposed is not None:
                    prior = os.path.join(
                        scratch, ".workshop", "prior-work", *rel.split("/"))
                    os.makedirs(os.path.dirname(prior), exist_ok=True)
                    with open(prior, "wb") as f:
                        f.write(proposed)
                continue
            if proposed is None:
                try:
                    os.remove(dest)
                except FileNotFoundError:
                    pass
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(proposed)

        coordination = os.path.join(scratch, ".workshop")
        os.makedirs(coordination, exist_ok=True)
        with open(os.path.join(coordination, "refresh.json"),
                  "w", encoding="utf-8") as f:
            json.dump({
                "work_id": item.work_id,
                "previous_patch": patch.public(),
                "conflicts": conflicts,
                "instruction": (
                    "Inspect latest integration and preserve this role's ownership. "
                    "Resolve listed paths in the deliverable; do not publish files "
                    "under .workshop."
                ),
            }, f, indent=2)

        shutil.rmtree(destination, ignore_errors=True)
        os.replace(scratch, destination)
        item.stale = True
        item.refreshes += 1
        item.state = "refreshing"
        return conflicts
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
