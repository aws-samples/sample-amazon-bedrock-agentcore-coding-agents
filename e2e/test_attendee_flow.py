"""The attendee's journey, end to end, through the ONE console origin.

test_workshop_e2e.py proves the engines; THIS file proves the workshop; it
drives the exact requests the console UI fires when an attendee follows the
content pages, in content order, against a real `console/server.py` process
(same-origin /api/dev|orchestrator|metrics mounts, the login gate, the Settings GitHub card).
If a step here breaks, a page in content/ is telling attendees to do something
that doesn't work.

Local engine mode (deterministic, no LLM); the same pytest gate the workshop
grades with. Run: python3 -m pytest e2e/test_attendee_flow.py -q
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

from e2e.conftest import seed_skill

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


def test_stage1_shell_convert_and_verify(console, cookie):
    """Stage 1 pages 1-3: set up the agent, open a session on an EMPTY workspace,
    CREATE the input module in the editor (New File → paste cost_analyzer.py),
    convert it, run the live verify: the exact card flow."""
    _, agents = _req(console, "GET", "/api/dev/agents", headers=cookie)
    assert any(a["agent_id"] == "claude-code" for a in agents["agents"])

    code, sess = _req(console, "POST", "/api/dev/sessions",
                      {"agent_id": "claude-code"}, headers=cookie)
    # the session-create contract: 201 Created, an open session on the S3 Files mount
    assert code == 201
    assert sess["status"] == "open"
    assert sess["session_id"]
    sid = sess["session_id"]
    assert sess["workspace"] == "/mnt/s3files"

    # content 2-open-a-shell: the workspace starts EMPTY; nothing is pre-seeded.
    _, out = _req(console, "POST", f"/api/dev/sessions/{sid}/input",
                  {"input": "ls /mnt/s3files"}, headers=cookie)
    assert "cost_analyzer.py" not in out["output"]

    # content 3-convert-by-hand, step 1: the participant creates the input module
    # themselves in the editor (New File → paste cost_analyzer.py → Save). We do it
    # the same way (real file-write API), under sample/, so the rest of the flow has it.
    seed_skill(console, cookie, sid)
    _, out = _req(console, "POST", f"/api/dev/sessions/{sid}/input",
                  {"input": "ls /mnt/s3files/sample"}, headers=cookie)
    assert "cost_analyzer.py" in out["output"]

    # the editor: free-named file write -> read -> rename -> delete (explorer ops)
    _, w = _req(console, "POST", f"/api/dev/sessions/{sid}/file",
                {"path": "notes/my-plan.md", "content": "convert the module"},
                headers=cookie)
    assert "error" not in w
    # a write returns the FRESH tree so the explorer can re-render the new file
    assert "tree" in w and isinstance(w["tree"], list)
    assert any(n["path"] == "/mnt/s3files/notes/my-plan.md" for n in w["tree"])
    _, mv = _req(console, "POST", f"/api/dev/sessions/{sid}/file",
                 {"path": "notes/my-plan.md", "op": "rename", "to": "notes/plan.md"},
                 headers=cookie)
    # tree paths are virtual (/mnt/s3files/...), exactly what the explorer shows
    assert mv.get("ok")
    assert any(n["path"] == "/mnt/s3files/notes/plan.md" for n in mv["tree"])
    assert not any(n["path"].endswith("my-plan.md") for n in mv["tree"])
    _, rm = _req(console, "POST", f"/api/dev/sessions/{sid}/file",
                 {"path": "notes/plan.md", "op": "delete"}, headers=cookie)
    assert rm.get("ok")
    # delete returns the fresh tree, and the deleted file is gone from it
    assert "tree" in rm and isinstance(rm["tree"], list)
    assert not any(n["path"] == "/mnt/s3files/notes/plan.md" for n in rm["tree"])

    # content 3-convert-a-skill-by-hand: convert + verify over the wire. The
    # converted server's own sample call returns the EXACT 140.16 fixture
    # (m5.large x2); not a stub; and the verify card's 4 named checks are all green.
    _, conv = _req(console, "POST", f"/api/dev/sessions/{sid}/convert-skill",
                   {"tool": "estimate_ec2_monthly_cost"}, headers=cookie)
    assert conv["verified"] is True
    assert conv["server_file"] == "/mnt/s3files/mcp_server.py"
    assert conv["sample_call"]["result"]["monthly_cost"] == 140.16
    _, ver = _req(console, "POST", f"/api/dev/sessions/{sid}/verify", {},
                  headers=cookie)
    assert ver["passed"] is True
    checks = {c["check"]: c["passed"] for c in ver["checks"]}
    assert checks == {"server_live": True, "tools_list": True,
                      "tool_call": True, "input_validation": True}, checks
    assert ver["sample"]["monthly_cost"] == 140.16
    _req(console, "DELETE", f"/api/dev/sessions/{sid}", headers=cookie)


def test_stage2_submit_watch_and_review(console, cookie):
    """Stage 2 run page: type the default task, submit, watch the phases, read
    the role terminals, and meet the review orchestrator's LGTM."""
    _, run = _req(console, "POST", "/api/orchestrator/runs",
                  {"task": "Convert /mnt/s3files/sample/cost_analyzer.py to a "
                           "remote MCP server with tests + a chatbot UI"},
                  headers=cookie)
    rid = run["run_id"]
    assert run["route"]["workflow_ref"] == "convert/sample-to-mcp-v1"

    for _ in range(120):                          # local mode: well under 2 min
        _, r = _req(console, "GET", f"/api/orchestrator/runs/{rid}", headers=cookie)
        if r["status"] in ("passed", "failed", "needs_human"):
            break
        time.sleep(1)
    assert r["status"] == "passed", r

    _, res = _req(console, "GET", f"/api/orchestrator/runs/{rid}/result", headers=cookie)
    assert res["gate"]["passed"] is True
    assert res["review"]["lgtm"] is True          # "LGTM: no changes needed"
    assert res["composed_from"] == ["backend-mcp", "validator", "frontend-builder"]

    _, terms = _req(console, "GET", f"/api/orchestrator/runs/{rid}/terminals",
                    headers=cookie)
    assert set(terms["terminals"]) == {"claude-code", "kiro", "opencode"}
    assert all(terms["terminals"][a] for a in terms["terminals"])


def test_settings_github_card_round_trip(console, cookie):
    """Getting Started tells the attendee to connect GitHub in Settings; the
    card validates the repo shape and clears cleanly (local store in tests)."""
    with pytest.raises(HTTPError) as e:
        _req(console, "POST", "/api/orchestrator/github",
             {"token": "ghp_x", "repo": "not-a-repo"}, headers=cookie)
    assert e.value.code == 400                    # owner/name enforced
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
    assert set(cost["breakdown"]) & {"claude-code", "kiro", "opencode"}
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
