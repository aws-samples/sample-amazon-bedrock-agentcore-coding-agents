"""The GitHub App manifest flow, exercised without GitHub.

Lab 2 registers its App from a manifest instead of asking an attendee to type an App
id, an installation id, and to drag a `.pem` across. That convenience only helps if it
works on the first try in a room, so the pieces that have no network dependency are
pinned here: the manifest asks for exactly the permissions the Gateway tools use, the
start page really posts it, a redirect carrying the wrong `state` is refused, and a
successful conversion reaches `main()`.

That last one is not hypothetical. The handler is a new instance per request, so
assigning the conversion result to `self` silently drops it and the script reports
"GitHub returned no private key" on a perfectly good App. It has to land on the class.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1] / "coding-agents" / "gateway_mcp"
          / "create-github-app.py")


def _load():
    spec = importlib.util.spec_from_file_location("create_github_app", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    return _load()


@pytest.fixture()
def server(module):
    """A live one-shot receiver on a free port, with a known manifest and state."""
    module.Handler.manifest = module.build_manifest(
        "https://example.cloudfront.net", 8765, "Test App")
    module.Handler.state = "st4te"
    module.Handler.result = {}
    module.Handler.done = threading.Event()
    httpd = HTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(base: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, resp.read().decode()


def test_the_manifest_asks_for_exactly_the_permissions_the_tools_use(module):
    manifest = module.build_manifest("https://box.example", 8765, "Test App")
    assert manifest["default_permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    # The webhook is required by the schema and unused by this workshop.
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["default_events"] == []
    assert manifest["public"] is False
    # GitHub can only return the code to a URL the BROWSER can open, which is the
    # CloudFront origin plus code-server's proxy path, never a bare localhost port.
    assert manifest["redirect_url"] == "https://box.example/proxy/8765/callback"


def test_the_start_page_posts_the_manifest_with_the_state(module, server):
    status, html = _get(server, "/")
    assert status == 200
    assert 'name="manifest"' in html
    assert "https://github.com/settings/apps/new?state=st4te" in html
    assert json.dumps(module.Handler.manifest) in html


def test_the_start_page_also_answers_the_unstripped_proxy_path(module, server):
    """code-server strips /proxy/<port>, but a 404 here would look like a broken lab."""
    status, html = _get(server, "/proxy/8765/")
    assert status == 200 and 'name="manifest"' in html


def test_a_redirect_with_the_wrong_state_is_refused(module, server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/callback?code=abc&state=not-ours")
    assert excinfo.value.code == 400
    assert not module.Handler.done.is_set()


def test_a_redirect_with_no_code_is_refused(module, server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/callback?state=st4te")
    assert excinfo.value.code == 400


def test_a_successful_conversion_reaches_the_waiting_script(module, server):
    module.github = lambda method, path, token=None: {
        "id": 42, "slug": "test-app", "pem": "-----BEGIN PRIVATE KEY-----"}
    status, html = _get(server, "/callback?code=abc&state=st4te")
    assert status == 200 and "App created" in html
    # On the CLASS, not the discarded handler instance.
    assert module.Handler.result["id"] == 42
    assert module.Handler.result["pem"].startswith("-----BEGIN")
    # wait(), not is_set(): the receiver sets `done` AFTER writing the response,
    # on purpose (main() answers `done` with server.shutdown(), and a shutdown
    # racing the write costs the attendee the install-the-App page). So the
    # client can legitimately see its answer first. A flag that is never set
    # still fails here.
    assert module.Handler.done.wait(5)


def test_a_refused_conversion_is_reported_not_swallowed(module, server):
    def boom(method, path, token=None):
        raise urllib.error.HTTPError(path, 422, "Unprocessable", {}, None)

    module.github = boom
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/callback?code=abc&state=st4te")
    assert excinfo.value.code == 502
    assert "error" in module.Handler.result
    # wait(), not is_set(): the receiver sets `done` AFTER writing the response,
    # on purpose (main() answers `done` with server.shutdown(), and a shutdown
    # racing the write costs the attendee the install-the-App page). So the
    # client can legitimately see its answer first. A flag that is never set
    # still fails here.
    assert module.Handler.done.wait(5)


def test_the_env_file_carries_the_three_values_deploy_all_reads(module, tmp_path,
                                                               monkeypatch):
    env_file = tmp_path / "github-app.env"
    monkeypatch.setattr(module, "ENV_FILE", env_file)
    module.write_env("42", tmp_path / "key.pem", "99")
    written = env_file.read_text()
    assert "export GITHUB_APP_ID=42" in written
    assert f'export GITHUB_APP_PRIVATE_KEY_FILE="{tmp_path / "key.pem"}"' in written
    assert "export GITHUB_APP_INSTALLATION_ID=99" in written
    assert env_file.stat().st_mode & 0o777 == 0o600
