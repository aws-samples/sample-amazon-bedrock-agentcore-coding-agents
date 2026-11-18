"""The GitHub MCP target answers a retried create or merge with what already happened.

November 18 (60-team canary, team 50): each call behind the Gateway landed on a cold
Runtime, the caller's attempt timed out after GitHub had acted, and the retry failed:
`create_pull_request` with 422 "A pull request already exists", and a merge was never
confirmed. The real request functions run here against an httpx mock of GitHub; only
FastMCP registration is stubbed (it lives in the separate MCP image).

    python3 -m pytest e2e/test_github_mcp_idempotent_pr.py -q
"""
import importlib.util
import json
from pathlib import Path
import sys
import types

import httpx
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents/gateway_mcp/app/main.py"


@pytest.fixture
def app(monkeypatch, tmp_path):
    class Registration:
        def __init__(self, *args, **kwargs):
            pass

        def add_middleware(self, middleware):
            pass

        def tool(self):
            return lambda function: function

    fastmcp = types.ModuleType("fastmcp")
    fastmcp.FastMCP = Registration
    middleware = types.ModuleType("fastmcp.server.middleware")
    middleware.Middleware = object
    middleware.MiddlewareContext = object
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "fastmcp.server", types.ModuleType("fastmcp.server"))
    monkeypatch.setitem(sys.modules, "fastmcp.server.middleware", middleware)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("GITHUB_APP_SECRET_ARN",
                       "arn:aws:secretsmanager:us-west-2:123456789012:secret:audit-ABCDEF")
    spec = importlib.util.spec_from_file_location("github_mcp_idempotent_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.GITHUB_TOKEN = "test-token"
    return module


def _github(monkeypatch, module, handler):
    seen = []

    def wrapped(request):
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return handler(request)

    real = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "Client", client)
    return seen


def test_a_retried_create_returns_the_pull_request_the_first_attempt_opened(monkeypatch, app):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(422, json={"message": "Validation Failed", "errors": [
                {"message": "A pull request already exists for didhd:workshop/runs/x."}]})
        return httpx.Response(200, json=[{"number": 16, "html_url": "https://github.com/o/r/pull/16"}])

    seen = _github(monkeypatch, app, handler)
    out = app.create_pull_request("o", "r", "title", "workshop/runs/x", "main", "body")
    assert out == {"number": 16, "url": "https://github.com/o/r/pull/16", "existing": True}
    assert seen[1] == ("GET", "/repos/o/r/pulls",
                       {"head": "o:workshop/runs/x", "base": "main", "state": "open"})


def test_any_other_create_refusal_still_fails(monkeypatch, app):
    _github(monkeypatch, app, lambda request: httpx.Response(
        422, json={"message": "Validation Failed: No commits between main and x"}))
    with pytest.raises(httpx.HTTPStatusError):
        app.create_pull_request("o", "r", "title", "x", "main")


def test_a_retried_merge_reports_the_merge_that_already_happened(monkeypatch, app):
    def handler(request):
        if request.method == "PUT":
            return httpx.Response(405, json={"message": "Pull Request is not mergeable"})
        return httpx.Response(200, json={"merged": True, "merge_commit_sha": "abc123",
                                         "head": {"sha": "head1"}})

    _github(monkeypatch, app, handler)
    assert app.merge_pull_request("o", "r", 15, head_sha="head1") == {
        "merged": True, "sha": "abc123", "existing": True}


def test_a_merge_of_a_different_head_is_not_taken_for_ours(monkeypatch, app):
    def handler(request):
        if request.method == "PUT":
            return httpx.Response(409, json={"message": "Head branch was modified"})
        return httpx.Response(200, json={"merged": True, "merge_commit_sha": "zzz",
                                         "head": {"sha": "someone-else"}})

    _github(monkeypatch, app, handler)
    with pytest.raises(RuntimeError, match="GitHub merge rejected PR #15 \\(409\\)"):
        app.merge_pull_request("o", "r", 15, head_sha="head1")


def test_an_unmerged_refusal_still_fails(monkeypatch, app):
    def handler(request):
        if request.method == "PUT":
            return httpx.Response(405, json={"message": "Pull Request is not mergeable"})
        return httpx.Response(200, json={"merged": False, "head": {"sha": "head1"}})

    _github(monkeypatch, app, handler)
    with pytest.raises(RuntimeError, match="not mergeable"):
        app.merge_pull_request("o", "r", 15, head_sha="head1")


def test_every_gateway_call_waits_past_a_cold_target(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))
    import github  # noqa: PLC0415

    waited = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"result": {"ok": True}}).encode()

    def urlopen(request, timeout):
        waited.append(timeout)
        return Response()

    monkeypatch.setattr(github.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(github, "_sigv4_headers", lambda *args: {})
    cfg = {"gateway_url": "https://gw.example", "region": "us-west-2"}
    assert github._gateway_rpc(cfg, "tools/call", {}, timeout=20.0) == {"ok": True}
    assert waited == [github._GATEWAY_MIN_TIMEOUT_S] and waited[0] >= 90
