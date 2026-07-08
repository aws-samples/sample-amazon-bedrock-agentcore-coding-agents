"""E2E: the cross-cutting platform contract every attendee relies on.

This module does NOT exercise a single stage; it exercises the shared chrome that
sits in front of all three: the same `console/server.py` an attendee's browser
loads. It pins the contract for: the ungated health rollup the workshop ops check;
the cookie login wall that fronts `/api/dev|orchestrator|metrics` (the exact thing keeping one
attendee's box off another's); the served SPA + BrowserRouter deep-link fallback
that makes `?view=`/`/agents` reload cleanly; cached `/assets/*`; and the honest
"GitHub not connected; fork the starter" status the orchestrator reports before
anyone pastes a token. If anything here breaks, every stage breaks at the door.

These hit the running server over HTTP through conftest's shared, session-scoped
`console` + `cookie` fixtures (login wall ON, deterministic local engine).
"""
from __future__ import annotations

import urllib.request
from urllib.error import HTTPError

from e2e.conftest import req, expect_status, login


# The workshop template repo the GitHub status must surface (owner/name shape).
# Attendees create their own repo from this template; there is no fork/PAT.
STARTER_HINT = "agentcore-coding-agents-starter"


# ---------------------------------------------------------------------------
# Health: ungated, rolls up all three engines.
# ---------------------------------------------------------------------------
def test_health_ungated_returns_ok_with_three_engine_rollups(console, cookie):
    """Ops opens /api/health (authed); gets ok + a per-engine rollup for s1/s2/s3."""
    code, body = req(console, "GET", "/api/health", headers=cookie)
    assert code == 200
    assert body["status"] == "ok"
    assert body["mode"] == "engine"
    engines = body["engines"]
    assert set(engines) == {"s1", "s2", "s3"}
    for mount, roll in engines.items():
        assert roll["status"] == "ok", f"{mount} engine not ok: {roll}"


def test_health_reachable_without_cookie(console):
    """A health probe with NO session cookie still answers ok (it is never gated)."""
    code, body = req(console, "GET", "/api/health")
    assert code == 200
    assert body["status"] == "ok"


def test_health_anon_omits_engine_internals(console):
    """The ungated (anon) health answer reports liveness only; no engine internals leak."""
    _, body = req(console, "GET", "/api/health")
    # Authed callers get the full rollup; anon callers get a bare liveness ping.
    assert "engines" not in body


# ---------------------------------------------------------------------------
# The login wall: every stage API is 401 without the cookie.
# ---------------------------------------------------------------------------
def test_s1_agents_get_blocked_without_cookie(console):
    """No cookie -> GET /api/dev is the 401 login wall, not the agent list."""
    body = expect_status(lambda: req(console, "GET", "/api/dev/agents"), 401)
    assert "error" in body


def test_s2_workflows_get_blocked_without_cookie(console):
    """No cookie -> GET /api/orchestrator is the 401 login wall, not the workflow registry."""
    expect_status(lambda: req(console, "GET", "/api/orchestrator/workflows"), 401)


def test_s3_dashboard_get_blocked_without_cookie(console):
    """No cookie -> GET /api/metrics is the 401 login wall, not the metrics dashboard."""
    expect_status(lambda: req(console, "GET", "/api/metrics/dashboard"), 401)


def test_s1_post_blocked_without_cookie(console):
    """No cookie -> a POST that would open a Stage 1 session is 401, not a workspace."""
    expect_status(
        lambda: req(console, "POST", "/api/dev/sessions", {"agent_id": "claude-code"}),
        401)


def test_s2_post_run_blocked_without_cookie(console):
    """No cookie -> a POST that would submit a Stage 2 run is 401, not a dispatched run."""
    expect_status(
        lambda: req(console, "POST", "/api/orchestrator/runs", {"task": "convert"}), 401)


def test_bogus_cookie_is_rejected_on_the_api(console):
    """A forged/garbage session cookie does NOT pass the wall; still 401 on the API."""
    forged = {"Cookie": "console_session=not-a-real-token.deadbeef"}
    expect_status(
        lambda: req(console, "GET", "/api/dev/agents", headers=forged), 401)


def test_valid_cookie_passes_the_wall(console, cookie):
    """The signed-in attendee's cookie clears the wall; GET /api/dev returns the agents."""
    code, body = req(console, "GET", "/api/dev/agents", headers=cookie)
    assert code == 200
    ids = {a["agent_id"] for a in body["agents"]}
    assert ids == {"claude-code", "kiro", "opencode"}


# ---------------------------------------------------------------------------
# Login form: wrong password -> 401 + no cookie; right password -> cookie.
# ---------------------------------------------------------------------------
def test_wrong_password_login_is_401_and_sets_no_cookie(console):
    """Attendee fat-fingers the password -> 401, and NO session cookie is minted."""
    status, set_cookie, _ = login(console, "ubuntu", "wrong-password")
    assert status == 401
    assert "console_session=" not in set_cookie


def test_wrong_username_login_is_401(console):
    """A bad username (right password) is also rejected -> 401, no cookie."""
    status, set_cookie, _ = login(console, "not-ubuntu", "attendee-pass")
    assert status == 401
    assert "console_session=" not in set_cookie


def test_correct_login_sets_console_session_cookie(console):
    """Correct ubuntu/attendee-pass login mints the console_session cookie (302 to /console/)."""
    status, set_cookie, _ = login(console, "ubuntu", "attendee-pass")
    assert status == 302
    assert "console_session=" in set_cookie
    # The cookie that fronts the box must be HttpOnly so page JS can't read it.
    assert "HttpOnly" in set_cookie


def test_minted_cookie_actually_authorizes_the_api(console):
    """A cookie freshly minted by /login (not the shared fixture) clears the API wall."""
    _, set_cookie, _ = login(console, "ubuntu", "attendee-pass")
    jar = {"Cookie": set_cookie.split(";")[0]}
    code, body = req(console, "GET", "/api/orchestrator/workflows", headers=jar)
    assert code == 200
    assert isinstance(body["workflows"], list) and body["workflows"]


# ---------------------------------------------------------------------------
# Logout: clears the cookie, and the cleared cookie no longer authorizes.
# ---------------------------------------------------------------------------
def test_logout_emits_a_clearing_cookie(console):
    """Attendee signs out -> logout emits the empty, Max-Age=0 clearing cookie."""
    # GET /logout is a 302 with the clearing Set-Cookie; don't follow the redirect.
    # urllib surfaces the un-followed 302 as an HTTPError whose headers carry the cookie.
    r = urllib.request.Request(console + "/logout", method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        resp = opener.open(r, timeout=10)
        set_cookie = resp.headers.get("Set-Cookie", "")
    except HTTPError as e:
        assert e.code == 302, f"expected a 302 logout redirect, got {e.code}"
        set_cookie = e.headers.get("Set-Cookie", "")
    assert "console_session=;" in set_cookie or "console_session=" in set_cookie
    assert "Max-Age=0" in set_cookie


def test_cleared_cookie_is_rejected_on_the_api(console):
    """The empty value the logout cookie carries does NOT authorize the API -> 401."""
    cleared = {"Cookie": "console_session="}
    expect_status(
        lambda: req(console, "GET", "/api/dev/agents", headers=cleared), 401)


# ---------------------------------------------------------------------------
# The served SPA: bare / + deep client routes return the same shell.
# ---------------------------------------------------------------------------
def test_root_returns_the_spa_shell(console, cookie):
    """An authed attendee hits / and gets the React SPA shell (id=root + /assets/)."""
    code, raw = req(console, "GET", "/", headers=cookie, raw=True)
    html = raw.decode("utf-8", "replace")
    assert code == 200
    assert 'id="root"' in html
    assert "/assets/" in html


def test_deep_client_route_agents_returns_spa(console, cookie):
    """Reloading on /agents serves the SPA shell so BrowserRouter owns the route."""
    _assert_spa(console, cookie, "/agents")


def test_deep_client_route_fleets_returns_spa(console, cookie):
    """Reloading on /fleets serves the SPA shell (deep-link fallback)."""
    _assert_spa(console, cookie, "/fleets")


def test_deep_client_route_governance_returns_spa(console, cookie):
    """Reloading on /governance serves the SPA shell (deep-link fallback)."""
    _assert_spa(console, cookie, "/governance")


def test_deep_client_route_settings_returns_spa(console, cookie):
    """Reloading on /settings serves the SPA shell (deep-link fallback)."""
    _assert_spa(console, cookie, "/settings")


def test_anon_deep_route_does_not_leak_spa(console):
    """Without a cookie a client route is NOT the SPA; the wall answers 401 instead."""
    # The spa fallback gates non-root paths behind auth (only the login page is anon).
    expect_status(lambda: req(console, "GET", "/governance"), 401)


# ---------------------------------------------------------------------------
# Static assets: served with a long-lived cache header.
# ---------------------------------------------------------------------------
def test_assets_serve_with_cache_header(console):
    """A built /assets/* file serves 200 with an immutable long-max-age Cache-Control."""
    asset = _first_asset(console)
    r = urllib.request.Request(console + asset, method="GET")
    with urllib.request.urlopen(r, timeout=10) as resp:
        assert resp.status == 200
        cache = resp.headers.get("Cache-Control", "")
        assert "max-age" in cache and "immutable" in cache


def test_missing_asset_is_404(console):
    """A request for an asset that isn't on disk is a clean 404, never the SPA shell."""
    expect_status(
        lambda: req(console, "GET", "/assets/does-not-exist-zzz.js"), 404)


# ---------------------------------------------------------------------------
# Bad input + wrong methods: the contract degrades cleanly.
# ---------------------------------------------------------------------------
def test_invalid_json_body_is_400(console, cookie):
    """A malformed JSON body on an authed API POST is rejected 'bad json' -> 400."""
    # Send raw non-JSON bytes the server can't parse.
    r = urllib.request.Request(
        console + "/api/orchestrator/runs", data=b"{not valid json", method="POST",
        headers={"Content-Type": "application/json", **cookie})
    body = _expect_http(lambda: urllib.request.urlopen(r, timeout=10), 400)
    assert "error" in body


def test_unknown_api_mount_is_404(console, cookie):
    """An unknown /api/sN mount (s9) returns the engine's 404, not a 500."""
    body = expect_status(
        lambda: req(console, "GET", "/api/s9/api/whatever", headers=cookie), 404)
    assert "error" in body


def test_unknown_subpath_within_a_mount_is_404(console, cookie):
    """A known mount with an unknown subpath returns the engine's 'not found' 404."""
    body = expect_status(
        lambda: req(console, "GET", "/api/dev/nope", headers=cookie), 404)
    assert body.get("error")


def test_agents_edit_is_post_only_get_is_404(console, cookie):
    """GET on the POST-only edit endpoint is 404 (the contract: edit is POST-only)."""
    expect_status(
        lambda: req(console, "GET", "/api/dev/agents/claude-code/edit", headers=cookie),
        404)


def test_put_method_on_api_is_not_allowed(console, cookie):
    """A PUT to an authed API path is method-not-allowed (405); the router only does GET/POST/DELETE."""
    r = urllib.request.Request(
        console + "/api/dev/agents", data=b"{}", method="PUT",
        headers={"Content-Type": "application/json", **cookie})
    _expect_http(lambda: urllib.request.urlopen(r, timeout=10), 405)


# ---------------------------------------------------------------------------
# GitHub status: the honest not-connected state + the workshop-repo hint.
# ---------------------------------------------------------------------------
def test_github_status_is_honest_not_connected(console, cookie):
    """Before any token paste, GET /api/orchestrator github status reports connected=false, local mode."""
    code, gh = req(console, "GET", "/api/orchestrator/github", headers=cookie)
    assert code == 200
    assert gh["connected"] is False
    assert gh["mode"] == "local"


def test_github_status_surfaces_workshop_repo_hint(console, cookie):
    """The not-connected status points at the workshop template repo (no PAT/fork:
    attendees create their own repo from the template and the gateway holds the App)."""
    _, gh = req(console, "GET", "/api/orchestrator/github", headers=cookie)
    assert STARTER_HINT in gh["workshop_repo"]
    assert "template" in gh.get("hint", "").lower()


# ---------------------------------------------------------------------------
# Small local helpers (no fixtures redefined; just request plumbing).
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't auto-follow the logout/login 302; the Set-Cookie we assert rides the 302."""
    def redirect_request(self, *a, **kw):
        return None


def _expect_http(call, code: int) -> dict:
    """Assert `call()` raises HTTPError with `code`; return the parsed body (or {})."""
    import json
    try:
        call()
    except HTTPError as e:
        assert e.code == code, f"expected {code}, got {e.code}"
        try:
            return json.loads(e.read() or b"{}")
        except (ValueError, OSError):
            return {}
    raise AssertionError(f"expected HTTP {code}, but the call succeeded")


def _assert_spa(console, cookie, path: str) -> None:
    code, raw = req(console, "GET", path, headers=cookie, raw=True)
    html = raw.decode("utf-8", "replace")
    assert code == 200, f"{path} did not 200"
    assert 'id="root"' in html, f"{path} is not the SPA shell"
    assert "/assets/" in html, f"{path} did not reference built assets"


def _first_asset(console) -> str:
    """Pull a real built asset path out of the served SPA shell so the cache-header
    assertion runs against a file that exists, not a guessed name."""
    import re
    # The shell is gated; read it via a fresh authed login so this helper is self-contained.
    _, set_cookie, _ = login(console, "ubuntu", "attendee-pass")
    jar = {"Cookie": set_cookie.split(";")[0]}
    _, raw = req(console, "GET", "/", headers=jar, raw=True)
    html = raw.decode("utf-8", "replace")
    m = re.search(r'/assets/[^"\'\s>]+', html)
    assert m, "no /assets/* reference found in the served shell"
    return m.group(0)
