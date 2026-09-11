"""Chat inspects the application selected in Settings, not its own platform."""
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat
import github
from project_workspace import inspect_project, ProjectUnavailable


def call(tools, name, **args):
    tool = next(tool for tool in tools if getattr(tool, "tool_name", "") == name)
    return json.loads(tool(**args))


@pytest.fixture
def project(tmp_path, monkeypatch):
    platform = tmp_path / "workshop"
    (platform / "console").mkdir(parents=True)
    (platform / "console/server.py").write_text("PLATFORM_ONLY = True")
    monkeypatch.setenv("WORKSHOP_REPO_ROOT", str(platform))
    monkeypatch.setattr(github, "configured_repository", lambda: "example/customer-app")
    roots = []

    def snapshot(destination):
        root = Path(destination)
        (root / "server").mkdir(parents=True)
        (root / "package.json").write_text('{"name":"customer-app","dependencies":{"express":"5"}}')
        (root / "server/app.js").write_text("app.get('/health', healthHandler);\n")
        roots.append(root)
        return {"repo": "example/customer-app", "branch": "main",
                "sha": f"commit-{len(roots)}", "files": 2}

    monkeypatch.setattr(github, "prepare_run_base", snapshot)
    return roots


def test_every_inspection_tool_reads_the_selected_project(project):
    tools = chat.build_tools()
    listed = call(tools, "list_files")
    assert listed["entries"] == ["package.json", "server/"]
    assert listed["repository"] == "example/customer-app"
    assert listed["commit"] == "commit-1"
    package = call(tools, "read_file", path="package.json")
    assert "express" in package["content"]
    assert call(tools, "read_file", path="console/server.py").get("error")
    matches = call(tools, "grep_workspace", pattern="healthHandler")
    assert matches["matches"] == ["server/app.js:1:app.get('/health', healthHandler);"]
    command = call(tools, "exec_command", command="cat package.json")
    assert command["exit"] == 0 and "customer-app" in command["stdout"]
    assert all(not root.exists() for root in project), "Inspection snapshots must be removed"


def test_cached_agent_tools_see_the_next_default_branch_snapshot(project):
    tools = chat.build_tools()
    first = call(tools, "read_file", path="package.json")
    second = call(tools, "read_file", path="package.json")
    assert first["commit"] != second["commit"]
    assert len(project) == 2


def test_unreachable_target_never_substitutes_the_workshop(project, monkeypatch):
    monkeypatch.setattr(github, "prepare_run_base", lambda _: {"error": "Repository access denied"})
    tools = chat.build_tools()
    for name, arguments in (
        ("read_file", {"path": "console/server.py"}),
        ("list_files", {}),
        ("grep_workspace", {"pattern": "PLATFORM_ONLY"}),
        ("exec_command", {"command": "cat console/server.py"}),
    ):
        result = call(tools, name, **arguments)
        assert "Repository access denied" in result["error"]
        assert "PLATFORM_ONLY" not in json.dumps(result)


def test_no_implicit_home_checkout_when_project_is_unconfigured(monkeypatch):
    monkeypatch.delenv("WORKSHOP_REPO_ROOT", raising=False)
    monkeypatch.setattr(github, "configured_repository", lambda: "")
    with pytest.raises(ProjectUnavailable, match="PROJECT_REPOSITORY_NOT_CONFIGURED"):
        with inspect_project():
            pytest.fail("An unconfigured project must not become the workshop")


def test_remote_snapshot_cannot_read_a_path_outside_it(project):
    result = call(chat.build_tools(), "read_file", path="../../outside.txt")
    assert "escapes the workspace" in result["error"]
