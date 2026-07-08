"""Auth-guard regression tests for the console API surface.

Locks the fix for the Cognito-only bypass: the /api guards must gate on
``_authed(request)`` DIRECTLY, not ``AUTH_ENABLED and _authed(...)``. The old
form short-circuited to False whenever CONSOLE_PASSWORD was unset (the documented
Cognito-first deployment), leaving every /api route -- PTY open/input/stream,
orchestrator chat, per-user metrics -- reachable with no login on the public URL.

These tests import ``server`` in a subprocess-clean way per auth mode (the guard
reads ``AUTH_ENABLED`` / ``COGNITO_ENABLED`` at import), via importlib.reload with
the env set first, then drive the real app with FastAPI's TestClient.

    python3 -m pytest console/test_auth_guard.py -q
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for p in (_HERE, os.path.join(_REPO, "orchestrator"),
          os.path.join(_REPO, "interactive-api"), os.path.join(_REPO, "metrics-api")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_app(env: dict):
    """(Re)import cognito_auth + server with a specific auth env, return the app.

    The auth mode is import-time state, so set env, drop the cached modules, and
    re-import. Runtime/GitHub paths are isolated so nothing real is touched."""
    for k in ("CONSOLE_PASSWORD", "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID",
              "COGNITO_DOMAIN", "COGNITO_REGION"):
        os.environ.pop(k, None)
    os.environ.update(env)
    os.environ.setdefault("WORKSHOP_GITHUB_STORE", "local")
    os.environ.setdefault("WORKSHOP_GITHUB_SETTINGS", "/tmp/_authtest-github.json")
    os.environ.setdefault("WORKSHOP_RUNTIME_CONFIG", "/tmp/_authtest-runtime.json")
    for mod in ("server", "cognito_auth"):
        sys.modules.pop(mod, None)
    import cognito_auth  # noqa: F401
    server = importlib.import_module("server")
    return server


# A representative slice of the guarded surface: an engine action (orchestrator
# chat), a PTY-open, and the metrics API. All three must be gated the same way.
GUARDED = [
    ("POST", "/api/orchestrator/chat", {"message": "hi"}),
    ("POST", "/api/dev/runtime-sessions", {"agent_id": "claude-code"}),
    ("GET", "/api/metrics/sessions", None),
]


@pytest.fixture
def client_factory():
    from fastapi.testclient import TestClient
    made = []

    def make(env):
        server = _load_app(env)
        c = TestClient(server.app)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close()


def test_cognito_only_mode_rejects_unauthenticated_api(client_factory):
    """The regression: Cognito enabled, CONSOLE_PASSWORD unset. Every guarded /api
    route must reject an anonymous caller (401/302/403), NEVER serve it. The old
    `AUTH_ENABLED and not _authed` guard let all of these through with 200."""
    c = client_factory({
        "COGNITO_USER_POOL_ID": "us-west-2_test", "COGNITO_CLIENT_ID": "testclient",
        "COGNITO_DOMAIN": "https://example.auth.us-west-2.amazoncognito.com",
        "COGNITO_REGION": "us-west-2",
    })
    for method, path, body in GUARDED:
        r = c.request(method, path, json=body, follow_redirects=False)
        assert r.status_code in (401, 302, 403), (
            f"{method} {path} was reachable UNAUTHENTICATED in Cognito-only mode "
            f"(status {r.status_code}) -- the auth guard regressed")


def test_password_mode_rejects_unauthenticated_api(client_factory):
    """Password gate set, no cookie: guarded routes still reject (unchanged)."""
    c = client_factory({"CONSOLE_PASSWORD": "s3cret"})
    for method, path, body in GUARDED:
        r = c.request(method, path, json=body, follow_redirects=False)
        assert r.status_code in (401, 302, 403), (
            f"{method} {path} reachable unauthenticated in password mode "
            f"(status {r.status_code})")


def test_fully_open_mode_allows_api(client_factory):
    """No password AND no Cognito (local dev / the e2e suite): _authed() returns
    True, so guarded routes are reachable. Proves the fix did not over-lock."""
    c = client_factory({})
    # Health returns the full payload (not the minimal anon one) when open.
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "engines" in body, f"open-mode health should return full details: {body}"
