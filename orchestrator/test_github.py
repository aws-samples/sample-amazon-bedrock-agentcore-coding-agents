"""GitHub Gateway tests for role PRs, the merge queue, and the final PR."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github  # noqa: E402
import work_items  # noqa: E402


_GW = "https://bedrock-agentcore.us-west-2.amazonaws.com/gateways/gw-abc/mcp"


def _clear_env(monkeypatch):
    for key in (
        "GITHUB_GATEWAY_URL",
        "GITHUB_REPO",
        "GITHUB_GATEWAY_TARGET",
        "WORKSHOP_FINAL_MERGE_POLICY",
        "WORKSHOP_MERGE_POLICY",
    ):
        monkeypatch.delenv(key, raising=False)


def _sandbox(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(
        github, "_SETTINGS", str(tmp_path / "github_gateway.local.json"))
    monkeypatch.setattr(github, "_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(
        github, "_FINAL_MERGE_POLICY_FILE",
        str(tmp_path / "final_merge_policy.local.json"))


def _wire(monkeypatch, tmp_path, repo="octocat/critter-lab"):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_GATEWAY_URL", _GW)
    monkeypatch.setenv("GITHUB_REPO", repo)


def _fake_gateway(monkeypatch, handler):
    def _rpc(cfg, method, params, timeout=30.0):
        if method == "tools/list":
            return handler("tools/list", None, {})
        name = params["name"]
        return handler(
            "tools/call", name.split("___", 1)[1],
            params.get("arguments", {}))
    monkeypatch.setattr(github, "_gateway_rpc", _rpc)


def _archive(files: dict[str, bytes]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(f"repo-sha/{path}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_settings_keep_repository_and_final_policy_separate_and_fail_closed(
        monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    status = github.status()
    assert status["connected"] is False
    assert status["workshop_repo"] == github.WORKSHOP_REPO
    assert status["final_merge_policy"] == "human_review"
    assert "pre-flight" in status["hint"]
    assert "error" in github.save_settings("not-a-repo")

    github.save_settings(
        "octocat/my-repo", gateway_url=_GW, final_policy="auto")
    assert github._gateway_config()["repo"] == "octocat/my-repo"
    assert github.final_merge_policy() == "auto"
    github.clear_settings()
    assert github._gateway_config() is None
    assert github.final_merge_policy() == "auto"

    github.save_settings("octocat/my-repo", gateway_url=_GW)
    github.set_final_merge_policy("auto")
    assert github._load_config_file()["repo"] == "octocat/my-repo"
    github.set_final_merge_policy("not-a-policy")
    assert github.final_merge_policy() == "human_review"
    assert github._load_config_file()["repo"] == "octocat/my-repo"


def test_status_reports_gateway_health(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    _fake_gateway(monkeypatch, lambda method, tool, args: (
        {"default_branch": "trunk"}
        if tool == "get_repository"
        else {"tools": [{"name": "GitHubMCP___create_pull_request"}]}))
    status = github.status()
    assert status["connected"] is True
    assert status["repo"] == "octocat/critter-lab"
    assert status["default_branch"] == "trunk"
    assert status["final_merge_policy"] == "human_review"


def test_prepare_run_integration_snapshots_private_branch(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    calls = []

    def handler(method, tool, args):
        calls.append(tool)
        if tool == "get_repository":
            return {"default_branch": "trunk"}
        if tool == "create_branch":
            assert args["from_branch"] == "trunk"
            return "refs/heads/" + args["branch"]
        if tool == "get_branch_head":
            return "abc123"
        if tool == "get_repository_archive":
            return {"archive_base64": _archive({
                "README.md": b"base\n",
                "src/app.py": b"print('base')\n",
            })}
        raise AssertionError(f"unexpected {tool}")

    _fake_gateway(monkeypatch, handler)
    destination = tmp_path / "checkout"
    result = github.prepare_run_integration(
        "workshop/runs/run-1/integration", str(destination))
    assert result["sha"] == "abc123"
    assert result["default_branch"] == "trunk"
    assert (destination / "src" / "app.py").read_text() == "print('base')\n"
    assert calls == [
        "get_repository", "create_branch", "get_branch_head",
        "get_repository_archive"]


def test_role_pr_publish_is_atomic_binary_safe_and_labeled(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    base = tmp_path / "base"
    work = tmp_path / "work"
    base.mkdir()
    work.mkdir()
    (base / "old.txt").write_text("remove\n")
    (work / "new.bin").write_bytes(b"\x00\xff")
    item = work_items.WorkItem.create(
        "run_1", "opencode", "frontend-builder", "frontend", token="front")
    work_items.diff_trees(item, str(base), str(work))

    class Run:
        run_id = "run_1"
        task = "build a UI"

    seen = {}
    commit_calls = []
    pr_calls = []

    def handler(method, tool, args):
        if tool == "commit_changes":
            commit_calls.append(args)
            seen["commit"] = args
            return {
                "sha": f"head-sha-{len(commit_calls)}",
                "base_sha": "base-sha",
                "changed": True,
            }
        if tool == "create_pull_request":
            pr_calls.append(args)
            seen["pr"] = args
            return {"number": 17, "url": "https://github.test/pull/17"}
        if tool == "get_issue":
            return {"number": 17, "state": "open"}
        if tool == "ensure_labels":
            seen["labels"] = [row["name"] for row in args["labels"]]
            return seen["labels"]
        raise AssertionError(f"unexpected {tool}")

    _fake_gateway(monkeypatch, handler)
    result = github.publish_work_item(Run(), item, "body")
    assert result["pr_url"].endswith("/17")
    assert seen["pr"]["base"] == item.base_branch
    assert seen["commit"]["expected_parent"] == ""
    assert seen["commit"]["from_branch"] == item.base_branch
    assert seen["commit"]["deletions"] == ["old.txt"]
    assert base64.b64decode(
        seen["commit"]["files"][0]["content_base64"]) == b"\x00\xff"
    assert seen["labels"] == [
        "run:run_1", "role:frontend", f"work:{item.work_id}"]

    item.attempt = 2
    refreshed = github.publish_work_item(Run(), item, "refreshed body")
    assert refreshed["number"] == 17
    assert len(pr_calls) == 1, "an open role PR was replaced during refresh"
    assert commit_calls[1]["expected_parent"] == "head-sha-1"
    assert item.head_sha == "head-sha-2"


def test_role_merge_targets_private_integration_and_pins_reviewed_head(
        monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    item = work_items.WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend", token="back")
    item.pr = {"number": 18, "base": item.base_branch}
    item.head_sha = "reviewed-head"

    class Run:
        integration_branch = item.base_branch

    seen = {}

    def handler(method, tool, args):
        assert tool == "merge_pull_request"
        seen.update(args)
        return {"merged": True, "sha": "merged-sha"}

    _fake_gateway(monkeypatch, handler)
    assert github.merge_work_item(Run(), item)["merged"] is True
    assert seen["head_sha"] == "reviewed-head"
    assert seen["merge_method"] == "squash"
    assert item.merge_state == "merged"


def test_final_auto_merge_refuses_an_unexpected_base(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    class Run:
        integration_branch = "workshop/runs/run-1/integration"
        pr = {
            "number": 21,
            "base": "release",
            "head": integration_branch,
            "head_sha": "reviewed-integration-head",
        }

    def handler(method, tool, args):
        if tool == "get_repository":
            return {"default_branch": "main"}
        raise AssertionError("merge must not be called")

    _fake_gateway(monkeypatch, handler)
    assert "refusing" in github.merge_integration_pr(Run())["error"]


def _run_fixture_with_final_pr(
        monkeypatch, tmp_path, policy: str, *, merge_success: bool = True):
    import engine
    from fixture_executor import FixtureExecutor

    _wire(monkeypatch, tmp_path)
    monkeypatch.setenv("WORKSHOP_FINAL_MERGE_POLICY", policy)
    final_url = "https://github.test/pull/99"
    calls = {"pull_requests": [], "merges": []}

    def handler(method, tool, args):
        if method == "tools/list":
            return {"tools": []}
        if tool == "get_repository":
            return {"default_branch": "main"}
        if tool == "get_branch_head":
            return "reviewed-final-head"
        if tool == "create_pull_request":
            calls["pull_requests"].append(args)
            return {"number": 99, "url": final_url}
        if tool == "ensure_labels":
            return [row["name"] for row in args["labels"]]
        if tool == "merge_pull_request":
            calls["merges"].append(args)
            return {"merged": merge_success,
                    "sha": "main-head" if merge_success else ""}
        raise AssertionError(f"unexpected {tool}")

    _fake_gateway(monkeypatch, handler)
    instance = engine.Engine(executor_obj=FixtureExecutor())
    run = instance.submit(
        "Build a service and interface",
        ["claude-code", "claude-code-validator", "opencode"])
    deadline = time.monotonic() + 120
    while run.status not in engine.TERMINAL:
        assert time.monotonic() < deadline, f"stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return instance, run, calls


def test_engine_final_pr_policy_matrix_is_guarded(monkeypatch, tmp_path):
    scenarios = [
        ("human_review", True, "passed", "human_review", 0),
        ("auto", True, "passed", "merged", 1),
        ("auto", False, "needs_human", "human_review", 1),
    ]
    for index, (policy, merge_success, status, merge_state, merge_count) in \
            enumerate(scenarios):
        instance, run, calls = _run_fixture_with_final_pr(
            monkeypatch, tmp_path / f"scenario-{index}", policy,
            merge_success=merge_success)
        try:
            assert run.status == status, run.fail_reason
            assert run.merge_state == merge_state
            assert run.pr_url == "https://github.test/pull/99"
            assert calls["pull_requests"][0]["head"] == run.integration_branch
            assert calls["pull_requests"][0]["base"] == "main"
            assert run.pr["head_sha"] == "reviewed-final-head"
            assert len(calls["merges"]) == merge_count
            assert len(run.gate_history) == 3
            if calls["merges"]:
                assert calls["merges"][0]["head_sha"] == "reviewed-final-head"
                assert calls["merges"][0]["merge_method"] == "squash"
            if not merge_success:
                assert run.fail_reason.startswith("FINAL_MERGE_ERROR:")
                assert run.iterations == 1, (
                    "a final merge failure restarted the build loop")
        finally:
            instance.shutdown()
