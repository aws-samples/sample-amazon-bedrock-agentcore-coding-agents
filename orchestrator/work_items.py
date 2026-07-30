"""Independent role work and deterministic integration candidates.

Each builder gets a stable work id, an isolated Runtime directory, and its own
GitHub branch. Builders may share a task and an integration brief, but they never
share a writable tree. That mirrors a human team: separate checkouts, explicit
pull requests, and one integration queue.

This module does not decide whether code is correct. It only performs structural
work that must be deterministic:

* issue collision-free work ids and branch names;
* order declared dependencies or reject a cycle;
* overlay role trees into a candidate, rejecting conflicting paths instead of
  selecting a winner.

The independent validator remains the checker. It authors an executable for the
assembled candidate, and the engine runs that executable for the verdict.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return clean or "role"


def integration_branch(run_id: str) -> str:
    """The private queue branch all role PRs for one run target."""
    return f"workshop/runs/{_slug(run_id)}/integration"


@dataclass
class WorkItem:
    """One builder's isolated unit of work and pull request lifecycle."""

    work_id: str
    agent: str
    role: str
    capability: str
    kind: str
    branch: str
    base_branch: str
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
               *, kind: str = "builder",
               token: str | None = None) -> "WorkItem":
        suffix = (token or uuid.uuid4().hex[:10]).lower()
        work_id = f"work_{_slug(agent)}_{suffix}"
        base = integration_branch(run_id)
        return cls(
            work_id=work_id,
            agent=agent,
            role=role,
            capability=capability,
            kind=kind,
            branch=f"workshop/runs/{_slug(run_id)}/{_slug(agent)}-{suffix}",
            base_branch=base,
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


@dataclass
class IntegrationCandidate:
    root: str
    files: list[str]
    owners: dict[str, list[str]]
    digest: str

    def public(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "owners": {path: list(ids) for path, ids in self.owners.items()},
            "digest": self.digest,
        }


class IntegrationConflict(RuntimeError):
    """Two isolated work items changed the same path differently."""

    def __init__(self, conflicts: list[dict[str, str]]) -> None:
        self.conflicts = conflicts
        paths = ", ".join(c["path"] for c in conflicts[:8])
        super().__init__(f"INTEGRATION_CONFLICT:{paths}")


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
                raise IntegrationConflict([{
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


def assemble_candidate(
    base_root: str,
    work: Iterable[tuple[WorkItem, str, str]],
    destination: str,
    *,
    exclude: Callable[[str], bool] | None = None,
) -> IntegrationCandidate:
    """Apply isolated role patches to a base or reject three-way conflicts.

    Every patch is calculated from the exact base that role received. A change
    applies when the current integration value still equals that base value.
    Identical changes coalesce. If integration and the role both changed a path
    differently, the item is stale/conflicted and no candidate is published.
    """
    entries = list(work)
    shutil.rmtree(destination, ignore_errors=True)
    scratch = destination + f".tmp-{uuid.uuid4().hex[:10]}"
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)

    owners: dict[str, list[str]] = {}
    conflicts: list[dict[str, str]] = []
    try:
        base_files = _tree_files(base_root, exclude)
        for rel, content in base_files.items():
            dest = os.path.join(scratch, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content)

        current = dict(base_files)
        origin: dict[str, str] = {}
        for item, item_base_root, work_root in entries:
            item_base = _tree_files(item_base_root, exclude)
            patch = diff_trees(item, item_base_root, work_root, exclude=exclude)
            for rel, proposed in patch.changes.items():
                original = item_base.get(rel, _MISSING)
                now = current.get(rel, _MISSING)
                wanted = _MISSING if proposed is None else proposed
                if now == wanted:
                    owners.setdefault(rel, []).append(item.work_id)
                    continue
                if now != original:
                    conflicts.append({
                        "path": rel,
                        "first_work_id": origin.get(rel, owners.get(rel, [""])[0]),
                        "second_work_id": item.work_id,
                        "reason": "the integration base and this work item changed "
                                  "the path differently",
                    })
                    continue

                dest = os.path.join(scratch, *rel.split("/"))
                if proposed is None:
                    current.pop(rel, None)
                    try:
                        os.remove(dest)
                    except FileNotFoundError:
                        pass
                else:
                    current[rel] = proposed
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(proposed)
                owners.setdefault(rel, []).append(item.work_id)
                origin.setdefault(rel, item.work_id)
        if conflicts:
            raise IntegrationConflict(conflicts)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        os.replace(scratch, destination)
        return IntegrationCandidate(
            root=destination,
            files=sorted(current),
            owners=owners,
            digest=tree_digest(destination),
        )
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
