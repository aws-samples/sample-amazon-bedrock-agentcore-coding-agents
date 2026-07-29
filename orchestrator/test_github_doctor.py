"""The GitHub preflight must catch the mistakes Lab 2 actually produces.

`status()` answers "does the gateway respond", which is a different question from
"can this App open a pull request on this repo". The Lab 2 mistakes that cost the
most time all pass `tools/list` and then fail at `create_branch`: the App installed
on a different repository, `GITHUB_REPO` naming the wrong owner, a `.pem` that
belongs to another App. Each of those surfaces only AFTER the coordinator is
deployed and a build has already run its agents, which is the most expensive
possible moment to learn about it.

Idea from awslabs/aidlc-workflows v2, which ships `/aidlc --doctor` as its own
step with a named result per check.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Point the settings file at a scratch path BEFORE importing, so the tests can
# never read a developer's real wired gateway (and never touch a real repo).
os.environ.setdefault("WORKSHOP_GITHUB_SETTINGS",
                      os.path.join(tempfile.mkdtemp(), "gh.json"))

import github  # noqa: E402

_CFG = {"gateway_url": "https://gw.example/mcp", "repo": "me/my-repo",
        "target": "GitHubMCP", "region": "us-west-2",
        "source": "env"}

_ALL_TOOLS = [{"name": f"GitHubMCP___{t}"} for t in
              ("create_branch", "get_repository", "get_repository_archive",
               "get_branch_head",
               "reset_branch", "commit_changes", "create_pull_request",
               "list_files", "comment_on_issue", "ensure_labels",
               "merge_pull_request")]


def _wire(monkeypatch, tools=None, tool_fn=None, cfg=_CFG):
    monkeypatch.setattr(github, "_gateway_config", lambda: cfg)
    monkeypatch.setattr(github, "_tools_list",
                        lambda c, timeout=15.0: _ALL_TOOLS if tools is None else tools)
    if tool_fn is None:
        def tool_fn(c, tool, args, timeout=30.0):
            if tool == "get_repository":
                return {"default_branch": "main"}
            return ["README.md", "server.py"]
    monkeypatch.setattr(github, "_tool", tool_fn)


def _failed(result):
    return {c["check"]: c["detail"] for c in result["checks"] if not c["passed"]}


def test_a_healthy_setup_passes_every_check(monkeypatch):
    _wire(monkeypatch)
    r = github.doctor()
    assert r["ok"] is True, _failed(r)
    names = [c["check"] for c in r["checks"]]
    # The check that distinguishes this from status(): the App reached the REPO.
    assert "app_can_reach_repo" in names, names


def test_nothing_wired_says_exactly_what_to_export(monkeypatch):
    monkeypatch.setattr(github, "_gateway_config", lambda: None)
    r = github.doctor()
    assert r["ok"] is False
    assert "config_resolved" in _failed(r)
    assert "GITHUB_GATEWAY_URL" in r["hint"] and "GITHUB_REPO" in r["hint"]


def test_the_app_on_the_wrong_repo_is_named_as_such(monkeypatch):
    """A 404 through the gateway is an installation/repo mismatch, not a gateway fault."""
    def not_found(c, tool, args, timeout=30.0):
        raise github.GatewayError("HTTP Error 404: Not Found")
    _wire(monkeypatch, tool_fn=not_found)
    r = github.doctor()
    assert r["ok"] is False
    detail = _failed(r)["app_can_reach_repo"]
    assert "cannot see" in detail and "me/my-repo" in detail, detail
    assert "installed on a different repository" in detail, detail


def test_a_rejected_credential_points_at_the_credential_deploy(monkeypatch):
    def unauthorized(c, tool, args, timeout=30.0):
        raise github.GatewayError("401 Unauthorized: bad credential")
    _wire(monkeypatch, tool_fn=unauthorized)
    r = github.doctor()
    detail = _failed(r)["app_can_reach_repo"]
    assert "deploy-credential.sh" in detail, detail


def test_an_unreachable_gateway_stops_before_blaming_the_repo(monkeypatch):
    """Order matters: a dead gateway must not be reported as a repo problem."""
    monkeypatch.setattr(github, "_gateway_config", lambda: _CFG)

    def dead(c, timeout=15.0):
        raise github.GatewayError("signing failed")
    monkeypatch.setattr(github, "_tools_list", dead)
    r = github.doctor()
    assert r["ok"] is False
    failed = _failed(r)
    assert "gateway_reachable" in failed
    assert "app_can_reach_repo" not in failed, (
        "the repo check ran against a gateway that never answered, so its verdict "
        "is meaningless and would send the attendee to the wrong place")


def test_a_gateway_missing_the_pr_tools_is_reported(monkeypatch):
    _wire(monkeypatch, tools=[{"name": "GitHubMCP___get_issue"}])
    r = github.doctor()
    assert r["ok"] is False
    detail = _failed(r)["pr_tools_present"]
    for t in ("create_branch", "commit_changes", "create_pull_request",
              "merge_pull_request"):
        assert t in detail, detail


def test_the_doctor_is_idempotent_and_leaves_no_litter(monkeypatch):
    """Safe to run repeatedly on a real repo. NOT the same as "read-only".

    It deliberately performs ONE write: creating the doctor branch. Reading a repo
    does not prove the App may write to it, and a beginner leaving "Pull requests" on
    read-only passes every read check and then fails at `create_branch` after a
    ten-minute build. So the write is probed here, while it is cheap.

    What must still hold: no content is written, nothing is opened or merged, and the one
    branch it touches is the stable `workshop/doctor` probe (`create_branch` treats
    "already exists" as success), so re-running changes nothing.
    """
    called: list[str] = []

    def record(c, tool, args, timeout=30.0):
        called.append(tool)
        if tool == "get_repository":
            return {"default_branch": "main"}
        if tool == "create_branch":
            assert args["branch"] == github.DOCTOR_BRANCH, (
                f"doctor created a branch other than the doctor branch: {args}")
            return "ok"
        return ["README.md"]
    _wire(monkeypatch, tool_fn=record)
    github.doctor()
    for forbidden in ("put_file", "create_pull_request", "merge_pull_request",
                      "comment_on_issue"):
        assert forbidden not in called, (
            f"doctor called {forbidden}, which changes the attendee's repo content or "
            f"opens something: {called}")


def test_a_read_only_app_permission_is_caught_before_the_coordinator_deploys(monkeypatch):
    """The expensive mistake: readable repo, unwritable repo.

    Every read check passes (the gateway answers, all tools are listed, the repo is
    visible) and the failure lands at `create_branch` AFTER a build has run its agents.
    """
    def forbidden(c, tool, args, timeout=30.0):
        if tool == "get_repository":
            return {"default_branch": "main"}
        if tool == "create_branch":
            raise github.GatewayError("gateway HTTP 403: Resource not accessible by "
                                      "integration")
        return ["README.md"]
    _wire(monkeypatch, tool_fn=forbidden)
    r = github.doctor()
    assert r["ok"] is False
    detail = _failed(r)["app_can_write_repo"]
    assert all(name in detail for name in ("Contents", "Issues", "Pull requests")), detail
    assert "re-install" in detail, "must say the App needs re-installing to take effect"


def test_an_already_existing_doctor_branch_is_success(monkeypatch):
    """Second and later checks: the branch exists, which proves write access."""
    def exists(c, tool, args, timeout=30.0):
        if tool == "get_repository":
            return {"default_branch": "main"}
        if tool == "create_branch":
            raise github.GatewayError("422: Reference already exists")
        return ["README.md"]
    _wire(monkeypatch, tool_fn=exists)
    r = github.doctor()
    assert r["ok"] is True, _failed(r)


def test_the_cli_exits_nonzero_when_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(github, "_gateway_config", lambda: None)
    assert github._main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "NOT READY" in out, out


def test_the_cli_exits_zero_when_ready(monkeypatch, capsys):
    _wire(monkeypatch)
    assert github._main(["doctor"]) == 0
    assert "ready" in capsys.readouterr().out.lower()
