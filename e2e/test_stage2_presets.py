"""Stage 2 routing over HTTP: roles are chosen, requests are never classified.

Workshop step: an attendee submits ONE request. It can be ANY request, because nothing
in this repository knows what they will ask for. Admission therefore does exactly one
thing before any agent runs, it resolves which ROLES work, and then only those roles
dispatch.

This replaced a suite that asserted a regex ladder mapping the attendee's prose onto
bundled sample use cases. That ladder is gone: it answered the question before the
agents saw it, and every one of its tests was really a test of our keyword list.

What is worth asserting, and what these tests cover:
  * an arbitrary sentence runs (the headline property)
  * a preset supplies its own request text and role set
  * only the routed roles get a lane, and a build always routes the checker
  * bad input fails LOUD (unknown preset, unknown role, nothing specified), never a
    nearest match and never an invented task

Stage 2 runs in the deterministic LOCAL engine (no model). A submitted run routes on a
worker thread, so route facts come from poll_route; a run rejected at admission never
attaches a route, so those are asserted via the failed terminal status + reason.
"""
from __future__ import annotations

import pytest

from e2e.conftest import (
    poll_route,
    poll_terminal,
    req,
    submit_run,
)

ALL_THREE = ["claude-code", "opencode", "kiro"]
VALIDATOR = "kiro"


# --------------------------------------------------------------- any request runs
def test_an_arbitrary_request_routes_and_runs(console, cookie):
    """The headline: a sentence that matches no sample, no keyword, and no preset is a
    perfectly good request."""
    run = submit_run(console, cookie, task="write me a haiku about tuesday",
                     agents=ALL_THREE)
    route = poll_route(console, cookie, run["run_id"])
    assert route["preset"] == "custom"
    assert route["agents"] == ALL_THREE
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "passed", final.get("fail_reason")


def test_named_roles_are_the_only_roles_that_run(console, cookie):
    """Focusing a run means choosing which BUILDER works. Nothing else gets a lane."""
    run = submit_run(console, cookie, task="change something small",
                     agents=["opencode", VALIDATOR])
    route = poll_route(console, cookie, run["run_id"])
    assert route["agents"] == ["opencode", VALIDATOR]
    final = poll_terminal(console, cookie, run["run_id"])
    assert "claude-code" not in (final.get("roles") or {}), final.get("roles")


# ------------------------------------------------------------------- the presets
@pytest.mark.parametrize("preset,expected_roles", [
    ("game-from-scratch", ["claude-code", VALIDATOR]),
    ("service-from-scratch", ["claude-code", VALIDATOR]),
    ("web-app", ["claude-code", "opencode", VALIDATOR]),
    ("cli-tool", ["claude-code", VALIDATOR]),
])
def test_a_preset_supplies_its_request_text_and_roles(console, cookie, preset, expected_roles):
    """Starting points exist so an attendee with no idea begins in a minute: the
    preset carries the request, so submitting one with no task still runs."""
    run = submit_run(console, cookie, preset=preset)
    route = poll_route(console, cookie, run["run_id"])
    assert route["preset"] == preset
    assert route["agents"] == expected_roles
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["task"].strip(), "the preset supplied no request text"
    assert final["status"] == "passed", final.get("fail_reason")


def test_presets_endpoint_is_the_one_source_the_console_renders(console, cookie):
    code, body = req(console, "GET", "/api/orchestrator/presets", headers=cookie)
    assert code == 200
    items = body["presets"]
    assert items, body
    for p in items:
        assert {"preset", "title", "roles", "task", "read_only"} <= set(p), p
        if not p["read_only"]:
            assert VALIDATOR in p["roles"], f"{p['preset']} builds with no checker"
    assert "your-own" in {p["preset"] for p in items}


# ----------------------------------------------------------------- fail loud only
@pytest.mark.parametrize("body,expected", [
    ({"task": "anything at all", "preset": "no/such-preset"}, "UNKNOWN_PRESET"),
    ({"task": "anything at all", "agents": ["claude-code", "nope"]}, "UNKNOWN_ROLE"),
    ({"task": "anything at all", "agents": ["claude-code"]}, "NO_CHECKER_ROUTED"),
    ({"task": "   ", "agents": ["claude-code", VALIDATOR]}, "EMPTY_TASK"),
])
def test_admission_fails_loud_rather_than_guessing(console, cookie, body, expected):
    """Never a nearest match, never an invented task, never an unverifiable build."""
    _, run = req(console, "POST", "/api/orchestrator/runs", body, headers=cookie)
    final = poll_terminal(console, cookie, run["run_id"])
    assert final["status"] == "failed", final
    assert final["fail_reason"].startswith(expected), final["fail_reason"]


def test_a_bare_request_is_routed_rather_than_refused(console, cookie):
    """A request with no preset and no explicit roles is ROUTED, not rejected.

    Naming roles is the console's job, not the attendee's: they type a sentence. Two
    wrong answers this pins out. Refusing it (the old ``PRESET_NOT_SPECIFIED``) makes
    the plainest possible request an error, and handing it the WHOLE roster dispatches
    a frontend builder for a command line tool, which is the defect a keyword table
    has. Only the roles the work needs get a lane, and the run records WHY.
    """
    run = submit_run(console, cookie, task="a command line tool that counts words")
    route = poll_route(console, cookie, run["run_id"])
    assert route["agents"], route
    assert VALIDATOR in route["agents"], "a build always routes its checker"
    assert route.get("rule"), "the run must record why these roles were chosen"
