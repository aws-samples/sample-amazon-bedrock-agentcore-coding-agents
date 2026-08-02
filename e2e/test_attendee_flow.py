"""The attendee's journey, end to end, through the ONE console origin.

test_workshop_e2e.py proves the engines; THIS file proves the workshop; it
drives the exact requests the console UI fires when an attendee follows the
content pages, in content order, against a real `console/server.py` process
(same-origin /api/dev|orchestrator|metrics mounts, the login gate, the Settings GitHub card).
If a step here breaks, a page in content/ is telling attendees to do something
that doesn't work.

The test server uses the explicit FixtureExecutor, so this suite checks the HTTP
journey without contacting a model, Runtime, or GitHub. AgentCore and GitHub are
proved separately by the live workshop traverse.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.error import HTTPError

import pytest

from e2e.conftest import seed_file

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The TEST entrypoint (same app, deterministic FixtureExecutor-backed Stage-2
# engine), not the shipped real-only server.py; see conftest.py for the rationale.
_SERVER = os.path.join(_REPO, "console", "test_server.py")
# Empty coding-agents dir (Stage-1 shelf starts deployed-free) so this server never
# reads the dev's real runtime_config.json.
_CODING_AGENTS_DIR = tempfile.mkdtemp(prefix="af-coding-agents-")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _req(base: str, method: str, path: str, body: dict | None = None,
         headers: dict | None = None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    with urllib.request.urlopen(r, timeout=30) as resp:
        payload = resp.read()
        return resp.status, (payload if raw else json.loads(payload or b"{}"))


@pytest.fixture(scope="module")
def console():
    """One real console server, exactly as the CFN systemd unit runs it,
    plus CONSOLE_PASSWORD so the login wall is part of the journey."""
    port = _free_port()
    env = {**os.environ, "CONSOLE_PORT": str(port),
           "WORKSHOP_CODING_AGENTS_DIR": _CODING_AGENTS_DIR,  # empty shelf by default
           # GitHub + runtime-ARN isolation: empty tmp files so no run reads the dev's
           # real wired PAT (would open a REAL PR) or real runtime ARNs.
           "WORKSHOP_GITHUB_STORE": "local",
           "WORKSHOP_GITHUB_SETTINGS": os.path.join(
               tempfile.mkdtemp(prefix="af-gh-"), "github.local.json"),
           "WORKSHOP_RUNTIME_CONFIG": os.path.join(
               tempfile.mkdtemp(prefix="af-rt-"), "runtime.local.json"),
           "CONSOLE_USER": "ubuntu", "CONSOLE_PASSWORD": "attendee-pass"}
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_REPO", None)
    for _k in [k for k in env if k.startswith("AGENTCORE_RUNTIME_")]:
        env.pop(_k, None)
    proc = subprocess.Popen([sys.executable, _SERVER], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            _req(base, "GET", "/api/health")
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("console server never came up")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows the login 302 to /console/ (a 404 on the raw server;
    nginx owns that prefix in prod) and reports THAT response's headers. The
    Set-Cookie we assert on lives on the 302 itself, so don't follow it."""
    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


@pytest.fixture(scope="module")
def cookie(console):
    """Getting Started: the attendee signs in (same password as VS Code)."""
    body = "username=ubuntu&password=attendee-pass"
    r = urllib.request.Request(console + "/login", data=body.encode(),
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(r, timeout=10)
        set_cookie = resp.headers.get("Set-Cookie", "")
    except HTTPError as e:                       # the unfollowed 302
        set_cookie = e.headers.get("Set-Cookie", "")
    assert "console_session=" in set_cookie, "login must set the session cookie"
    return {"Cookie": set_cookie.split(";")[0]}


def test_getting_started_login_wall(console):
    """Before sign-in: the console serves the login page, not the workbench;
    the API is gated; only /api/health stays open (stack health check)."""
    _, html = _req(console, "GET", "/", raw=True)
    assert b"Sign in" in html and b"view-s1" not in html
    code, _ = _req(console, "GET", "/api/health")
    assert code == 200
    with pytest.raises(HTTPError) as e:
        _req(console, "GET", "/api/orchestrator/agents")
    assert e.value.code == 401


def test_getting_started_workbench_opens_on_stage1(console, cookie):
    """After sign-in the React console shell is served; the client router lands
    on the Agents (Stage 1) view. Bare / returns index.html with the app bundle;
    the default-stage redirect runs client-side."""
    _, html = _req(console, "GET", "/", headers=cookie, raw=True)
    assert b'id="root"' in html
    assert b'/assets/' in html


def test_stage2_submit_watch_and_review(console, cookie):
    """Stage 2 run page: type the default task, submit, watch the phases, read
    the role terminals, and meet the review orchestrator's LGTM."""
    # ANY request works; the roles are what must be chosen.
    _, run = _req(console, "POST", "/api/orchestrator/runs",
                  {"task": "build a small thing and prove it works",
                   "agents": ["claude-code", "opencode", "claude-code-validator"]},
                  headers=cookie)
    rid = run["run_id"]
    assert run["route"]["preset"] == "custom"

    for _ in range(120):                          # local mode: well under 2 min
        _, r = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        assert "final_base_branch" in r
        if r["status"] in ("passed", "failed", "needs_human"):
            break
        time.sleep(1)
    assert r["status"] == "passed", r

    _, res = _req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
    assert res["gate"]["passed"] is True
    assert res["review"]["lgtm"] is True          # "LGTM: no changes needed"
    items = res["work_items"]
    builder_items = [
        item for item in items.values() if item["kind"] == "builder"
    ]
    assert len({item["work_id"] for item in items.values()}) == len(items)
    # Every pull request targets the repository's DEFAULT branch, not a run branch.
    assert res["final_base_branch"]
    assert all(
        item["base_branch"] == res["final_base_branch"]
        for item in builder_items
    )
    # Each one settled on its own: merged, or green and left open for a person.
    assert all(item["merge_state"] in ("merged", "human_review")
               for item in builder_items)
    assert len(res["role_prs"]) == len(builder_items)
    assert all(row["state"] in ("merged", "awaiting_review")
               for row in res["role_prs"])
    # ONE executable per pull request, each attributed to the work id it judged.
    assert len(res["gate_history"]) == len(builder_items)
    assert all(row["passed"] for row in res["gate_history"])
    assert {row["work_id"] for row in res["gate_history"]} == {
        item["work_id"] for item in builder_items}

    _, terms = _req(console, "GET", f"/api/orchestrator/runs/{rid}/terminals",
                    headers=cookie)
    assert set(terms["terminals"]) == {"claude-code", "claude-code-validator", "opencode"}
    assert all(terms["terminals"][a] for a in terms["terminals"])


def test_settings_github_card_round_trip(console, cookie):
    """Getting Started tells the attendee to connect GitHub in Settings; the
    card validates the repo shape and clears cleanly (local store in tests)."""
    with pytest.raises(HTTPError) as e:
        _req(console, "POST", "/api/orchestrator/github",
             {"repo": "not-a-repo"}, headers=cookie)
    assert e.value.code == 400                    # owner/name enforced

    _, policy = _req(
        console, "POST", "/api/orchestrator/github",
        {"merge_policy": "auto"}, headers=cookie)
    assert policy["merge_policy"] == "auto"
    assert policy["connected"] is False

    _, closed = _req(
        console, "POST", "/api/orchestrator/github",
        {"merge_policy": "not-a-policy"}, headers=cookie)
    assert closed["merge_policy"] == "human_review"

    code, st = _req(console, "POST", "/api/orchestrator/github", {"clear": True},
                    headers=cookie)
    assert code == 200                            # a clean clear is a 200, not a 4xx
    _, st = _req(console, "GET", "/api/orchestrator/github", headers=cookie)
    assert st["connected"] is False and st["mode"] == "local"


def test_stage3_metrics_reflect_what_just_happened(console, cookie):
    """Stage 3 pages: the dashboard and per-user cost APIs aggregate the REAL
    runs the attendee just executed; no seeded numbers."""
    _, dash = _req(console, "GET", "/api/metrics/dashboard", headers=cookie)
    assert dash["runs_total"] >= 1
    _, cost = _req(console, "GET", "/api/metrics/cost-breakdown?by=agent",
                   headers=cookie)
    assert set(cost["breakdown"]) & {"claude-code", "claude-code-validator", "opencode"}
    _, pol = _req(console, "GET", "/api/metrics/policies", headers=cookie)
    # every Cedar policy row carries the real required fields (metrics_lib.get_policies):
    # tier (hard|soft), a rule_id, an effect, and a human summary; not just a flag.
    assert pol["policies"]
    for p in pol["policies"]:
        assert {"tier", "rule_id", "effect", "summary"} <= set(p), p
        assert p["tier"] in ("hard", "soft"), p
        assert p["rule_id"] and isinstance(p["rule_id"], str)
        assert p["effect"] and isinstance(p["effect"], str)
        assert p["summary"] and isinstance(p["summary"], str)
    assert any(p["tier"] == "hard" for p in pol["policies"])
