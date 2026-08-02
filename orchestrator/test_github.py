"""GitHub Gateway tests for the per-pull-request flow.

One pull request per role, each based on the repository's DEFAULT branch, each
checked, reviewed, and merged on its own. There is no assembled candidate, no merge
queue, and no final integration pull request, so nothing here pins an order between
pull requests: a red one never blocks a green sibling.

Every test in this module runs against a fake ``_gateway_rpc``, and the settings and
merge-policy files are redirected into ``tmp_path`` (on top of the suite-wide
``WORKSHOP_GITHUB_SETTINGS`` isolation in ``conftest.py``). No test may reach a real
gateway or open a real pull request.
"""

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
        github, "_MERGE_POLICY_FILE",
        str(tmp_path / "merge_policy.local.json"))


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


def test_settings_keep_repository_and_merge_policy_separate_and_fail_closed(
        monkeypatch, tmp_path):
    """The repository and the merge policy are separate settings, and both fail closed.

    The policy used to govern one final integration pull request; with each role
    pull request standing on its own it governs every reviewed pull request. The
    invariants are unchanged: an unparsable repo is rejected rather than half-saved,
    an unknown policy falls back to ``human_review`` (the human boundary, never
    ``auto``), and neither write clobbers the other.
    """
    _sandbox(monkeypatch, tmp_path)
    status = github.status()
    assert status["connected"] is False
    assert status["workshop_repo"] == github.WORKSHOP_REPO
    assert status["merge_policy"] == "human_review"
    assert "pre-flight" in status["hint"]
    assert "error" in github.save_settings("not-a-repo")

    github.save_settings(
        "octocat/my-repo", gateway_url=_GW, merge_policy_value="auto")
    assert github._gateway_config()["repo"] == "octocat/my-repo"
    assert github.merge_policy() == "auto"
    github.clear_settings()
    assert github._gateway_config() is None
    assert github.merge_policy() == "auto"

    github.save_settings("octocat/my-repo", gateway_url=_GW)
    github.set_merge_policy("auto")
    assert github._load_config_file()["repo"] == "octocat/my-repo"
    github.set_merge_policy("not-a-policy")
    assert github.merge_policy() == "human_review"
    assert github._load_config_file()["repo"] == "octocat/my-repo"


def test_the_merge_policy_env_var_wins_and_the_retired_name_still_reads(
        monkeypatch, tmp_path):
    """``WORKSHOP_MERGE_POLICY`` is the name; the old one keeps working for back-compat.

    An operator who set ``WORKSHOP_FINAL_MERGE_POLICY`` before the final pull request
    was removed must not silently get the opposite of what they asked for, and an
    unknown value from either name still fails closed to the human boundary.
    """
    _sandbox(monkeypatch, tmp_path)
    github.set_merge_policy("human_review")
    monkeypatch.setenv("WORKSHOP_FINAL_MERGE_POLICY", "auto")
    assert github.merge_policy() == "auto"
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "human_review")
    assert github.merge_policy() == "human_review", (
        "the retired env name overrode the current one")
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "merge-everything")
    assert github.merge_policy() == "human_review"


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
    assert status["merge_policy"] == "human_review"


def test_prepare_run_base_reads_the_default_branch_and_creates_nothing(
        monkeypatch, tmp_path):
    """The run's base is the repository's DEFAULT branch, read rather than created.

    This replaces the old run-scoped integration branch. Every role pull request
    targets the branch this returns and merges into it on its own, so there is
    nothing to assemble and nothing to create: ``create_branch`` must not be called
    at all. Reading the branch here (before any agent work) is also what makes a
    missing Gateway fail in seconds instead of after a ten-minute build.
    """
    _wire(monkeypatch, tmp_path)
    calls = []

    def handler(method, tool, args):
        calls.append(tool)
        if tool == "get_repository":
            return {"default_branch": "trunk"}
        if tool == "get_branch_head":
            assert args["branch"] == "trunk"
            return "abc123"
        if tool == "get_repository_archive":
            assert args["ref"] == "trunk"
            return {"archive_base64": _archive({
                "README.md": b"base\n",
                "src/app.py": b"print('base')\n",
            })}
        raise AssertionError(f"unexpected {tool}")

    _fake_gateway(monkeypatch, handler)
    destination = tmp_path / "checkout"
    result = github.prepare_run_base(str(destination))
    assert result["sha"] == "abc123"
    assert result["default_branch"] == "trunk"
    assert result["branch"] == "trunk"
    assert (destination / "src" / "app.py").read_text() == "print('base')\n"
    assert calls == [
        "get_repository", "get_branch_head", "get_repository_archive"]
    assert "create_branch" not in calls, (
        "the run created a branch; every role pull request bases on the default "
        "branch directly, so there is no run-scoped branch to make")


def test_prepare_run_base_fails_before_any_agent_work_without_a_gateway(
        monkeypatch, tmp_path):
    """Pre-flight, not post-mortem: no gateway is a named error, never a silent base."""
    _sandbox(monkeypatch, tmp_path)

    def handler(method, tool, args):
        raise AssertionError("an unwired run reached the gateway")

    _fake_gateway(monkeypatch, handler)
    result = github.prepare_run_base(str(tmp_path / "checkout"))
    assert result["error"].startswith("PR_NO_GATEWAY")
    assert "default_branch" not in result


def test_role_pr_publish_is_atomic_binary_safe_and_labeled(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    base = tmp_path / "base"
    work = tmp_path / "work"
    base.mkdir()
    work.mkdir()
    (base / "old.txt").write_text("remove\n")
    (work / "new.bin").write_bytes(b"\x00\xff")
    item = work_items.WorkItem.create(
        "run_1", "opencode", "frontend-builder", "frontend",
        base_branch="main", token="front")
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
    assert seen["pr"]["base"] == item.base_branch == "main", (
        "a role pull request must target the repository's default branch")
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


def test_role_merge_targets_the_default_branch_and_pins_the_reviewed_head(
        monkeypatch, tmp_path):
    """One role pull request merges into the DEFAULT branch, pinned to what was reviewed.

    This is the only merge in the workshop now, so the three refusals that used to
    guard the final integration pull request live here and every one of them is
    load-bearing: a base that is not the branch the run pinned, a default branch that
    moved under the run, and a missing head SHA (which would let the merge land a
    commit nobody reviewed). Each must refuse WITHOUT calling merge_pull_request.
    """
    _wire(monkeypatch, tmp_path)

    def _item():
        item = work_items.WorkItem.create(
            "run_1", "claude-code", "backend-builder", "backend",
            base_branch="main", token="back")
        item.pr = {"number": 18, "base": item.base_branch}
        item.head_sha = "reviewed-head"
        return item

    class Run:
        final_base_branch = "main"

    seen = {}

    def handler(method, tool, args):
        if tool == "get_repository":
            return {"default_branch": "main"}
        assert tool == "merge_pull_request", f"unexpected {tool}"
        seen.update(args)
        return {"merged": True, "sha": "merged-sha"}

    _fake_gateway(monkeypatch, handler)
    item = _item()
    assert github.merge_work_item(Run(), item)["merged"] is True
    assert seen["number"] == 18
    assert seen["head_sha"] == "reviewed-head"
    assert seen["merge_method"] == "squash"
    assert item.merge_state == "merged"

    def refuse(method, tool, args):
        if tool == "get_repository":
            return {"default_branch": "main"}
        raise AssertionError("merge must not be called")

    _fake_gateway(monkeypatch, refuse)

    # 1. the pull request does not target the branch this run pinned
    wrong_base = _item()
    wrong_base.pr = {"number": 19, "base": "release"}
    error = github.merge_work_item(Run(), wrong_base)["error"]
    assert "refusing" in error and "release" in error

    # 2. the repository's default branch moved under the run
    def moved(method, tool, args):
        if tool == "get_repository":
            return {"default_branch": "trunk"}
        raise AssertionError("merge must not be called")

    _fake_gateway(monkeypatch, moved)
    error = github.merge_work_item(Run(), _item())["error"]
    assert "refusing" in error and "default branch changed" in error

    # 3. nothing reviewed to pin
    _fake_gateway(monkeypatch, refuse)
    unpinned = _item()
    unpinned.head_sha = ""
    error = github.merge_work_item(Run(), unpinned)["error"]
    assert "refusing" in error and "no reviewed head SHA" in error


def test_a_gateway_that_declines_the_merge_is_never_reported_as_merged(
        monkeypatch, tmp_path):
    """Branch protection is not bypassable, and a refusal is never rounded up.

    ``merge_pull_request`` answering "not merged" is exactly what a protected branch
    looks like from here. It must surface as an error on that pull request, never as
    a merge, and it must not mark the item merged.
    """
    _wire(monkeypatch, tmp_path)
    item = work_items.WorkItem.create(
        "run_1", "claude-code", "backend-builder", "backend",
        base_branch="main", token="back")
    item.pr = {"number": 20, "base": "main"}
    item.head_sha = "reviewed-head"

    class Run:
        final_base_branch = "main"

    def handler(method, tool, args):
        if tool == "get_repository":
            return {"default_branch": "main"}
        return {"merged": False, "message": "required status checks are pending"}

    _fake_gateway(monkeypatch, handler)
    result = github.merge_work_item(Run(), item)
    assert "did not complete" in result["error"]
    assert "merged" not in result
    assert item.merge_state is None
    assert item.state != "merged"


def _run_fixture_per_pr(monkeypatch, tmp_path, policy: str, *,
                        red_gate: bool = False, refuse_first_merge: bool = False):
    """Drive the engine's per-pull-request finalization offline.

    The fixture executor merges locally, so no gateway call happens on this path at
    all: the handler asserts on ANY call, which is the isolation proof for this
    module. ``refuse_first_merge`` stands in for the gateway declining ONE merge
    (branch protection), so the sibling's independence can be observed.
    """
    import engine
    from fixture_executor import FixtureExecutor

    _wire(monkeypatch, tmp_path)
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", policy)
    if red_gate:
        monkeypatch.setenv("FIXTURE_CHECK_EXIT", "1")

    def handler(method, tool, args):
        raise AssertionError(
            f"an offline run reached the GitHub gateway: {tool}")

    _fake_gateway(monkeypatch, handler)

    refused: list[str] = []
    real_merge = engine.Engine._merge_work_item

    def one_refusal(self, run, item):
        if refuse_first_merge and not refused:
            refused.append(item.work_id)
            return {"error": "role PR merge did not complete: branch protection"}
        return real_merge(self, run, item)

    monkeypatch.setattr(engine.Engine, "_merge_work_item", one_refusal)
    instance = engine.Engine(executor_obj=FixtureExecutor())
    run = instance.submit(
        "Build a service and interface",
        ["claude-code", "claude-code-validator", "opencode"])
    deadline = time.monotonic() + 180
    while run.status not in engine.TERMINAL:
        assert time.monotonic() < deadline, f"stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return instance, run, refused


def test_the_merge_policy_decides_each_pull_request_and_never_merges_a_red_one(
        monkeypatch, tmp_path):
    """The human boundary, per pull request, and it cannot be talked past.

    Replaces the old final-PR policy matrix: there is no final pull request, so the
    same three questions are asked of EACH role pull request. Under ``human_review``
    a green, approved pull request is left OPEN for a person (success, not a
    failure); under ``auto`` the engine merges it; and under either policy a red gate
    merges nothing and the run ends ``needs_human`` with ROLE_PR_BLOCKED naming the
    work ids. One gate per pull request, and the repair bound stays per pull request.
    """
    instance, run, _ = _run_fixture_per_pr(
        monkeypatch, tmp_path / "human-review", "human_review")
    try:
        assert run.status == "passed", run.fail_reason
        assert [row["state"] for row in run.role_prs] == \
            ["awaiting_review", "awaiting_review"]
        items = [i for i in run.work_items.values() if i.kind == "builder"]
        assert [i.merge_state for i in items] == ["human_review"] * 2
        assert all(i.state != "merged" for i in items), (
            "human_review merged a pull request a person was supposed to")
        assert len(run.gate_history) == len(items), (
            "one authored check ran per pull request")
    finally:
        instance.shutdown()

    instance, run, _ = _run_fixture_per_pr(
        monkeypatch, tmp_path / "auto", "auto")
    try:
        assert run.status == "passed", run.fail_reason
        assert [row["state"] for row in run.role_prs] == ["merged", "merged"]
        items = [i for i in run.work_items.values() if i.kind == "builder"]
        assert [i.merge_state for i in items] == ["merged"] * 2
        assert {row["work_id"] for row in run.role_prs} == \
            {i.work_id for i in items}
    finally:
        instance.shutdown()

    for policy in ("human_review", "auto"):
        instance, run, _ = _run_fixture_per_pr(
            monkeypatch, tmp_path / f"red-{policy}", policy, red_gate=True)
        try:
            assert run.status == "needs_human"
            assert run.fail_reason.startswith("ROLE_PR_BLOCKED:")
            items = [i for i in run.work_items.values() if i.kind == "builder"]
            for item in items:
                assert item.work_id in run.fail_reason
                assert item.merge_state == "blocked"
                assert item.state != "merged", (
                    f"{policy} merged a pull request whose gate was red")
                assert item.attempt <= 2, (
                    "the repair bound is one round PER pull request")
            assert [row["error"] for row in run.role_prs] == ["GATE_RED"] * 2
        finally:
            instance.shutdown()


def test_a_refused_merge_blocks_only_its_own_pull_request(monkeypatch, tmp_path):
    """A red pull request never blocks a green sibling. That is the whole point.

    The old merge queue stopped the whole run at the first refusal, and the final
    pull request made every role's fate one verdict. Now the sibling merges on its
    own and only the refused work id is named for a person.
    """
    instance, run, refused = _run_fixture_per_pr(
        monkeypatch, tmp_path, "auto", refuse_first_merge=True)
    try:
        assert refused, "the scenario never exercised a refusal"
        blocked_id = refused[0]
        states = {row["work_id"]: row["state"] for row in run.role_prs}
        assert states.pop(blocked_id) == "blocked"
        assert set(states.values()) == {"merged"}, (
            f"a refused merge blocked a sibling too: {run.role_prs}")
        assert run.status == "needs_human"
        assert run.fail_reason == f"ROLE_PR_BLOCKED:{blocked_id}"
        assert run.iterations == 1, (
            "a refused merge restarted the build loop instead of reporting")
    finally:
        instance.shutdown()
