"""github.py tests: the GATEWAY config ladder + the MCP-tool PR path, no network.

The GitHub credential model is a GitHub App installation held inside the GitHub
MCP Gateway, never a PAT. These tests exercise the config ladder (no gateway ->
local mode) and the connected PR path by mocking the ONE network boundary: the
SigV4-signed JSON-RPC call to the gateway (``github._gateway_rpc``). No real
gateway, App, or repo is ever touched (protects the e2e PR-leak hazard).

    python3 -m pytest orchestrator/test_github.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github  # noqa: E402


def _clear_env(monkeypatch):
    for k in ("GITHUB_GATEWAY_URL", "GITHUB_REPO", "GITHUB_GATEWAY_TARGET",
              "WORKSHOP_MERGE_POLICY"):
        monkeypatch.delenv(k, raising=False)


def _sandbox(monkeypatch, tmp_path):
    """No env config, config/policy files under tmp: pure local mode."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(github, "_SETTINGS", str(tmp_path / "github_gateway.local.json"))
    monkeypatch.setattr(github, "_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(github, "_MERGE_POLICY_FILE", str(tmp_path / "merge_policy.local.json"))


# ---- local mode (no gateway wired) ------------------------------------------

def test_status_local_mode_advertises_the_template_repo(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    s = github.status()
    assert s["connected"] is False and s["mode"] == "local"
    assert s["connection_method"] == "gateway"
    # the template repo attendees "Use this template" from
    assert s["workshop_repo"] == github.WORKSHOP_REPO
    assert "template" in s["hint"].lower()


def test_ensure_compose_base_is_local_without_a_gateway(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    base = github.ensure_compose_base()
    # never raises; composes locally, publishes via the gateway later
    assert base["mode"] == "local" and "no gateway" in base["reason"]


def test_open_pr_fails_loud_without_a_gateway(monkeypatch, tmp_path):
    """Real-only contract: with no gateway wired the PR step FAILS LOUD
    (PR_NO_GATEWAY), never a benign skip or a fake url. pr_url stays null."""
    _sandbox(monkeypatch, tmp_path)

    class _Run:
        composed_branch = "run/run_x"
        run_id = "run_x"
        task = "t"
    out = github.open_pr(_Run(), "report")
    assert "skipped" not in out
    assert out.get("error", "").startswith("PR_NO_GATEWAY")


# ---- Settings surface (no token; only repo + optional gateway_url) ----------

def test_save_settings_validates_repo_shape(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    assert "error" in github.save_settings("not-a-repo")
    # a well-formed repo saves without a token; status() health-checks the gateway
    # (which is unwired here, so connected stays False, but no error on save).
    out = github.save_settings("octocat/my-repo")
    assert "error" not in out
    saved = json.loads(open(github._SETTINGS).read())
    assert saved["repo"] == "octocat/my-repo"


def test_save_settings_persists_gateway_url_and_repo(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    gw = "https://bedrock-agentcore.us-west-2.amazonaws.com/gateways/gw-abc/mcp"
    github.save_settings("octocat/my-repo", gateway_url=gw)
    cfg = github._gateway_config()
    assert cfg is not None
    assert cfg["repo"] == "octocat/my-repo" and cfg["gateway_url"] == gw
    assert cfg["region"] == "us-west-2" and cfg["source"] == "settings"


def test_clear_settings_disconnects_but_keeps_merge_policy(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    github.save_settings("octocat/my-repo", merge_policy="auto")
    assert github.merge_policy() == "auto"
    github.clear_settings()
    assert github._gateway_config() is None
    # merge_policy is an independent, secret-free preference: it survives disconnect
    assert github.merge_policy() == "auto"


# ---- gateway transport helpers ----------------------------------------------

_GW = "https://bedrock-agentcore.us-west-2.amazonaws.com/gateways/gw-abc/mcp"


def _wire_gateway(monkeypatch, tmp_path, repo="octocat/critter-lab", policy=None):
    """Put a gateway on the ENV rung and point composed/ at a real git repo whose
    branch carries deliverable files, so open_pr's guards pass offline."""
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_GATEWAY_URL", _GW)
    monkeypatch.setenv("GITHUB_REPO", repo)
    if policy:
        monkeypatch.setenv("WORKSHOP_MERGE_POLICY", policy)
    composed = tmp_path / "composed"
    monkeypatch.setattr(github, "_COMPOSED", str(composed))
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(composed)], check=True)
    subprocess.run(["git", "-C", str(composed), "commit", "-q", "--allow-empty",
                    "-m", "init"], check=True, env=env)
    deliver = composed / "deliverable"
    deliver.mkdir()
    (deliver / "mcp_server.py").write_text("print('server')\n")
    (deliver / "critique.md").write_text("LGTM: no changes needed\n")
    subprocess.run(["git", "-C", str(composed), "checkout", "-q", "-B", "run/run_x"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(composed), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(composed), "commit", "-qm", "compose"],
                   check=True, env=env)
    return composed


class _Run:
    def __init__(self, branch="run/run_x", run_id="run_x", task="convert the module"):
        self.composed_branch = branch
        self.run_id = run_id
        self.task = task


def _fake_gateway(monkeypatch, handler):
    """Replace the ONLY network boundary: the signed JSON-RPC call. `handler(method,
    tool, arguments)` returns the tool result (or raises github.GatewayError)."""
    def _rpc(cfg, method, params, timeout=30.0):
        if method == "tools/list":
            return handler("tools/list", None, {})
        name = params["name"]  # "<target>___<tool>"
        tool = name.split("___", 1)[1]
        return handler("tools/call", tool, params.get("arguments", {}))
    monkeypatch.setattr(github, "_gateway_rpc", _rpc)


# ---- status health via tools/list -------------------------------------------

def test_status_connected_reports_gateway_health(monkeypatch, tmp_path):
    _wire_gateway(monkeypatch, tmp_path)
    _fake_gateway(monkeypatch, lambda m, t, a: {"tools": [
        {"name": "GitHubMCP___create_branch"}, {"name": "GitHubMCP___put_file"},
        {"name": "GitHubMCP___create_pull_request"}]})
    s = github.status()
    assert s["connected"] is True and s["mode"] == "gateway"
    assert s["connection_method"] == "gateway"
    assert s["repo"] == "octocat/critter-lab" and s["target"] == "GitHubMCP"
    assert s["tool_count"] == 3
    assert "token" not in json.dumps(s).lower() or "token_tail" not in s


def test_status_reports_unhealthy_gateway(monkeypatch, tmp_path):
    _wire_gateway(monkeypatch, tmp_path)

    def _boom(m, t, a):
        raise github.GatewayError("403: not signed")
    _fake_gateway(monkeypatch, _boom)
    s = github.status()
    assert s["connected"] is False and s["mode"] == "gateway"
    assert "health check failed" in s["error"]


# ---- the PR path (create_branch + put_file + create_pull_request) -----------

def test_open_pr_success_publishes_via_gateway_tools(monkeypatch, tmp_path):
    """open_pr creates the branch, puts each deliverable file, and opens the PR,
    all via gateway MCP tools. human_review -> PR targets the default branch."""
    _wire_gateway(monkeypatch, tmp_path)
    calls = []

    def _handler(method, tool, args):
        calls.append((tool, args.get("path")))
        if tool == "create_branch":
            assert args["from_branch"] == "main"  # human_review base
            return "refs/heads/run/run_x"
        if tool == "put_file":
            return "abc123"
        if tool == "create_pull_request":
            assert args["head"] == "run/run_x" and args["base"] == "main"
            assert "LGTM" in args["body"]
            return {"number": 7, "url": "https://github.com/octocat/critter-lab/pull/7"}
        raise AssertionError(f"unexpected tool {tool}")
    _fake_gateway(monkeypatch, _handler)

    out = github.open_pr(_Run(), "## review\nLGTM: no changes needed")
    assert out["pr_url"] == "https://github.com/octocat/critter-lab/pull/7"
    assert out["number"] == 7 and out["base"] == "main" and out["source"] == "environment"
    # both deliverable files were published
    put = [p for (t, p) in calls if t == "put_file"]
    assert "deliverable/mcp_server.py" in put and "deliverable/critique.md" in put


def test_open_pr_auto_mode_targets_integration_branch_not_main(monkeypatch, tmp_path):
    """auto mode: the PR base is the stable integration branch (created off the
    default branch if missing), never the default branch. Structural guarantee."""
    _wire_gateway(monkeypatch, tmp_path, policy="auto")
    seen = {"branches": []}

    def _handler(method, tool, args):
        if tool == "create_branch":
            seen["branches"].append(args["branch"])
            return f"refs/heads/{args['branch']}"
        if tool == "put_file":
            return "sha"
        if tool == "create_pull_request":
            assert args["base"] == github.INTEGRATION_BRANCH, "auto PR must NOT target main"
            return {"number": 8, "url": "https://github.com/octocat/critter-lab/pull/8"}
        raise AssertionError(f"unexpected tool {tool}")
    _fake_gateway(monkeypatch, _handler)

    out = github.open_pr(_Run(), "report")
    assert out["base"] == github.INTEGRATION_BRANCH and out["default_branch"] == "main"
    assert github.INTEGRATION_BRANCH in seen["branches"]  # integration branch ensured


def test_open_pr_reports_gateway_failure_honestly(monkeypatch, tmp_path):
    """A gateway tool failure is surfaced as an error, never a fake url."""
    _wire_gateway(monkeypatch, tmp_path)

    def _handler(method, tool, args):
        if tool == "create_branch":
            raise github.GatewayError("500: internal")
        raise AssertionError("should not proceed past create_branch")
    _fake_gateway(monkeypatch, _handler)
    out = github.open_pr(_Run(), "report")
    assert "error" in out and "create_branch" in out["error"]


def test_open_pr_tolerates_existing_branch(monkeypatch, tmp_path):
    """Re-running a run whose branch already exists is idempotent: create_branch's
    'already exists' is swallowed and the PR still opens."""
    _wire_gateway(monkeypatch, tmp_path)

    def _handler(method, tool, args):
        if tool == "create_branch":
            raise github.GatewayError("422: Reference already exists")
        if tool == "put_file":
            return "sha"
        if tool == "create_pull_request":
            return {"number": 9, "url": "https://github.com/octocat/critter-lab/pull/9"}
        raise AssertionError(f"unexpected {tool}")
    _fake_gateway(monkeypatch, _handler)
    out = github.open_pr(_Run(), "report")
    assert out["number"] == 9


# ---- merge_policy ladder -----------------------------------------------------

def test_merge_policy_ladder_fail_closed(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    assert github.merge_policy() == "human_review"
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    assert github.merge_policy() == "auto"
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "YOLO")
    assert github.merge_policy() == "human_review"


def test_save_settings_persists_merge_policy(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    github.save_settings("octocat/repo", merge_policy="auto")
    assert github.merge_policy() == "auto"
    github.save_settings("octocat/repo")  # omitted -> fail-closed
    assert github.merge_policy() == "human_review"


def test_set_merge_policy_flips_without_touching_the_config(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    github.save_settings("octocat/repo")
    github.set_merge_policy("auto")
    assert github.merge_policy() == "auto"
    # the gateway config (repo) survives a policy-only flip
    assert github._load_config_file()["repo"] == "octocat/repo"
    github.set_merge_policy("nonsense")
    assert github.merge_policy() == "human_review"


# ---- post_review (PR comment, not APPROVE) + merge_pr ------------------------

def test_post_review_posts_a_pr_comment(monkeypatch, tmp_path):
    """post_review posts the verdict as a PR COMMENT via comment_on_issue -- which
    works even when the App installation authored the PR (an APPROVE review would
    422 on your own PR)."""
    _wire_gateway(monkeypatch, tmp_path)
    seen = {}

    def _handler(method, tool, args):
        if tool == "comment_on_issue":
            seen["number"] = args["issue_number"]
            seen["body"] = args["body"]
            return {"url": "https://github.com/octocat/critter-lab/pull/8#comment-1"}
        raise AssertionError(f"unexpected {tool}")
    _fake_gateway(monkeypatch, _handler)

    run = _Run()
    run.pr = {"number": 8}
    out = github.post_review(run, "## Critique\nLGTM: no changes needed")
    assert out["reviewed"] is True
    assert seen["number"] == 8 and "LGTM: no changes needed" in seen["body"]


def test_post_review_skips_without_a_gateway(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    run = _Run()
    run.pr = {"number": 8}
    assert "skipped" in github.post_review(run, "report")


def test_merge_pr_squash_merges_into_integration(monkeypatch, tmp_path):
    _wire_gateway(monkeypatch, tmp_path)

    def _handler(method, tool, args):
        if tool == "merge_pull_request":
            assert args["merge_method"] == "squash" and args["number"] == 8
            return {"merged": True, "sha": "cafef00d"}
        raise AssertionError(f"unexpected {tool}")
    _fake_gateway(monkeypatch, _handler)

    run = _Run()
    run.pr = {"number": 8, "base": github.INTEGRATION_BRANCH, "default_branch": "main"}
    out = github.merge_pr(run)
    assert out["merged"] is True and out["sha"] == "cafef00d"


def test_merge_pr_refuses_to_merge_into_default_branch(monkeypatch, tmp_path):
    """Defense in depth: refuses a PR whose base IS the default branch, so
    auto-merge can never close into main even if the base was set wrong."""
    _wire_gateway(monkeypatch, tmp_path)

    def _handler(method, tool, args):
        raise AssertionError("merge attempted despite default-branch base!")
    _fake_gateway(monkeypatch, _handler)
    run = _Run()
    run.pr = {"number": 8, "base": "main", "default_branch": "main"}
    out = github.merge_pr(run)
    assert "skipped" in out and "default branch" in out["skipped"]


def test_merge_pr_skips_without_a_gateway(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    run = _Run()
    run.pr = {"number": 8, "base": "x", "default_branch": "main"}
    assert "skipped" in github.merge_pr(run)


# ---- the ENGINE wires open_pr after LGTM (the auto-open PR contract) ---------
# After the reviewer returns LGTM on an engine run, _finalize calls github.open_pr
# and surfaces its url as run.pr_url. Exercised offline: a gateway on the env rung
# so open_pr proceeds; the gateway JSON-RPC boundary mocked; compose is real.

def test_engine_opens_pr_automatically_after_lgtm(monkeypatch, tmp_path):
    import engine  # noqa: PLC0415
    from fixture_executor import FixtureExecutor  # noqa: PLC0415

    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_GATEWAY_URL", _GW)
    monkeypatch.setenv("GITHUB_REPO", "octocat/critter-lab")
    pr_url = "https://github.com/octocat/critter-lab/pull/42"

    def _handler(method, tool, args):
        if method == "tools/list":
            return {"tools": []}
        if tool == "create_branch":
            return "refs/heads/" + args["branch"]
        if tool == "put_file":
            return "sha"
        if tool == "create_pull_request":
            return {"number": 42, "url": pr_url}
        if tool == "comment_on_issue":
            return {"url": pr_url + "#comment"}
        raise AssertionError(f"unexpected {tool}")
    _fake_gateway(monkeypatch, _handler)

    captured: dict = {}
    real_open_pr = github.open_pr

    def _spy(run, report_md):
        captured["called"] = True
        captured["report"] = report_md
        return real_open_pr(run, report_md)
    monkeypatch.setattr(github, "open_pr", _spy)
    monkeypatch.setattr(engine.github, "open_pr", _spy)

    eng = engine.Engine(executor_obj=FixtureExecutor())
    try:
        run = eng.submit("Convert the module to a remote MCP server with tests "
                         "+ a chatbot UI", ["claude-code", "claude-code-validator", "opencode"])
        deadline = __import__("time").monotonic() + 90
        while run.status not in engine.TERMINAL:
            assert __import__("time").monotonic() < deadline, f"stuck in {run.status}"
            __import__("time").sleep(0.2)
        assert run.status == "passed", (run.status, run.fail_reason)
        assert captured.get("called") is True, "engine did not call github.open_pr after LGTM"
        assert run.pr_url == pr_url
        assert (run.pr or {}).get("pr_url") == pr_url
        assert "LGTM: no changes needed" in captured["report"]
    finally:
        eng.shutdown()


def test_result_payload_surfaces_pr_url_after_auto_open(monkeypatch, tmp_path):
    import engine  # noqa: PLC0415
    from fixture_executor import FixtureExecutor  # noqa: PLC0415

    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_GATEWAY_URL", _GW)
    monkeypatch.setenv("GITHUB_REPO", "octocat/critter-lab")
    pr_url = "https://github.com/octocat/critter-lab/pull/99"

    def _handler(method, tool, args):
        if method == "tools/list":
            return {"tools": []}
        if tool == "create_branch":
            return "refs/heads/" + args["branch"]
        if tool == "put_file":
            return "sha"
        if tool == "create_pull_request":
            return {"number": 99, "url": pr_url}
        if tool == "comment_on_issue":
            return {"url": pr_url + "#comment"}
        raise AssertionError(f"unexpected {tool}")
    _fake_gateway(monkeypatch, _handler)

    eng = engine.Engine(executor_obj=FixtureExecutor())
    try:
        run = eng.submit("Convert the module to a remote MCP server with tests "
                         "+ a chatbot UI", ["claude-code", "claude-code-validator", "opencode"])
        deadline = __import__("time").monotonic() + 90
        while run.status not in engine.TERMINAL:
            assert __import__("time").monotonic() < deadline, f"stuck in {run.status}"
            __import__("time").sleep(0.2)
        assert run.status == "passed", (run.status, run.fail_reason)
        res = engine.public_result(run)
        assert res["pr_url"] == pr_url, res
        assert (res.get("pr") or {}).get("pr_url") == pr_url, res["pr"]
        assert res["merge_state"] == "human_review", res["merge_state"]
        assert res["pr"].get("source") == "environment", res["pr"]
    finally:
        eng.shutdown()
