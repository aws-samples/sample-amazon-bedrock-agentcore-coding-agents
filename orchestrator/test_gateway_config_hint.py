"""A missing GitHub destination must name the half that is missing.

`_gateway_config()` needs BOTH a gateway URL and an owner/name repo. The failure used
to blame the Gateway either way and send the reader to `github.py doctor`, which
passes in a shell that exports GITHUB_REPO -- so a console run with an auto-discovered
gateway and no configured repo produced a red run, a green check, and no way to tell
which was lying. Found live on 2026-09-03.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "orchestrator"))

import github  # noqa: E402


def _hint(monkeypatch, *, url, repo, discovered=None):
    monkeypatch.setattr(github, "_load_config_file", lambda: {})
    monkeypatch.setattr(github, "_discover_gateway_url", lambda: discovered)
    for name, value in (("GITHUB_GATEWAY_URL", url), ("GITHUB_REPO", repo)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return github._missing_gateway_config_hint()


def test_gateway_wired_but_no_repo_blames_the_repo(monkeypatch):
    hint = _hint(monkeypatch, url=None, repo=None,
                 discovered="https://gw.example/mcp")
    assert "no target repository is set" in hint
    assert "Settings" in hint and "GITHUB_REPO" in hint
    assert "doctor" not in hint, "do not send the reader to a check that passes"


def test_repo_set_but_no_gateway_blames_the_gateway(monkeypatch):
    hint = _hint(monkeypatch, url=None, repo="owner/name")
    assert "no GitHub MCP Gateway URL" in hint
    assert "deployed-state.json" in hint or ".deployed-state.json" in hint


def test_a_malformed_repo_is_reported_as_malformed(monkeypatch):
    hint = _hint(monkeypatch, url=None, repo="not-a-repo",
                 discovered="https://gw.example/mcp")
    assert "not owner/name" in hint
    assert "not-a-repo" in hint


def test_neither_present_names_both(monkeypatch):
    hint = _hint(monkeypatch, url=None, repo=None)
    assert "neither" in hint.lower()
    assert "doctor" in hint


def test_the_hint_reaches_the_run_as_a_preflight_error(monkeypatch):
    """prepare_run_base must carry the specific hint, not the old blanket sentence."""
    monkeypatch.setattr(github, "_gateway_config", lambda: None)
    monkeypatch.setattr(github, "_load_config_file", lambda: {})
    monkeypatch.setattr(github, "_discover_gateway_url",
                        lambda: "https://gw.example/mcp")
    monkeypatch.delenv("GITHUB_GATEWAY_URL", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    out = github.prepare_run_base("/tmp/does-not-matter")
    assert out["error"].startswith("PR_NO_GATEWAY: ")
    assert "no target repository is set" in out["error"]
