"""Read the project selected in Settings, never an implicit workshop checkout."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

import github


class ProjectUnavailable(RuntimeError):
    pass


@dataclass
class Workspace:
    root: Path
    source: dict

    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ProjectUnavailable(f"path escapes the workspace: {relative}")
        return path


@contextmanager
def inspect_project():
    """A bounded source archive for one inspection, removed when the tool ends.

    Resolve the default branch on each inspection, including when a deployed
    coordinator reuses its Agent object across turns. No stale per-Agent cache,
    no GitHub credential in a Runtime, and no writes to the remote repository.
    """
    repo = github.configured_repository()
    if repo:
        with tempfile.TemporaryDirectory(prefix="workshop-chat-project-") as scratch:
            destination = Path(scratch) / "source"
            try:
                snapshot = github.prepare_run_base(str(destination))
            except github.GatewayError as exc:
                raise ProjectUnavailable(f"Cannot inspect the selected repository: {exc}") from exc
            if snapshot.get("error"):
                raise ProjectUnavailable(snapshot["error"])
            if snapshot.get("repo") != repo:
                raise ProjectUnavailable("The selected repository changed during inspection; inspect it again.")
            yield Workspace(destination.resolve(), {
                "repository": repo, "branch": snapshot["branch"],
                "commit": snapshot["sha"], "source": "github-default-branch",
            })
        return
    # An explicit path remains useful to a local developer and to offline tests.
    # Missing configuration must never silently substitute the host's harness.
    explicit = os.environ.get("WORKSHOP_REPO_ROOT", "").strip()
    if explicit:
        yield Workspace(Path(explicit).expanduser().resolve(), {"source": "explicit-local-workspace"})
        return
    raise ProjectUnavailable(
        "PROJECT_REPOSITORY_NOT_CONFIGURED: set the target repository in Settings "
        "before inspecting project code."
    )
