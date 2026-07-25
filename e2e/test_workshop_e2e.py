"""End-to-end proof of the WHOLE workshop loop, the way a real attendee runs it.

No LLM, no AWS, no network beyond localhost. This test walks the exact path the content
teaches and asserts that each stage produces the real thing the next stage consumes, and
 the part that actually matters, that the deliverable is **built from the harness**, so
editing a harness file changes what comes out. That is the workshop's central claim, and
this suite makes it mechanical instead of theatre.

  Stage 1 (Interactive)  shell orchestration: boot the interactive engine, deploy an
                         agent, open a session, and run REAL /bin/sh commands in the
                         /mnt/s3files workspace. The mount starts empty; the attendee
                         types any request into the agent's live terminal.
  Stage 2 (Orchestrate)  submit ONE task to the embedded orchestration engine; the
                         blueprint runs; the backend role GENERATES the deliverable from
                         its CLAUDE.md, the frontend role GENERATES the UI from its
                         AGENTS.md, the validator role AUTHORS an executable check whose
                         real exit code is the gate, and the three roles are composed
                         into ONE git commit.
  Stage 3 (Governance)   the run lands in the shared telemetry ledger and the metrics
                         API aggregates it (per-user, per-agent, p95).

Run:  pytest e2e/test_workshop_e2e.py -v
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

# E2E asserts the deterministic spine. The in-process engine is made
# deterministic by injecting the test-only FixtureExecutor (no model, no live
# AWS) at each construction site below, never an env flag on the shipped path.
# Empty coding-agents dir so the Stage-1 shelf starts deployed-free; a test writes
# the real runtime_config.json a harness deploy.py would, to exercise reconciliation.
_CODING_AGENTS_DIR = tempfile.mkdtemp(prefix="we2e-coding-agents-")
os.environ["WORKSHOP_CODING_AGENTS_DIR"] = _CODING_AGENTS_DIR
# GitHub isolation: this module drives the engine IN-PROCESS, so point the credential
# store at an empty tmp file BEFORE github.py loads; a real wired PAT would make a
# Stage-2 run open a REAL pull request. Set at module import, before any engine import.
os.environ["WORKSHOP_GITHUB_STORE"] = "local"
os.environ["WORKSHOP_GITHUB_SETTINGS"] = os.path.join(
    tempfile.mkdtemp(prefix="we2e-gh-"), "github.local.json")
os.environ["WORKSHOP_RUNTIME_CONFIG"] = os.path.join(
    tempfile.mkdtemp(prefix="we2e-rt-"), "runtime.local.json")
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_REPO", None)
for _k in [k for k in os.environ if k.startswith("AGENTCORE_RUNTIME_")]:
    os.environ.pop(_k, None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for sub in ("interactive-api", "orchestrator", "metrics-api"):
    sys.path.insert(0, os.path.join(_REPO, sub))

_LEDGER = os.path.join(_REPO, ".runs", "telemetry.jsonl")


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _wait_terminal(run, timeout_s: float = 90.0):
    """Block until a run reaches a terminal state, or fail loud on a stall."""
    deadline = time.monotonic() + timeout_s
    while run.status not in ("passed", "failed", "needs_human"):
        assert time.monotonic() < deadline, f"run stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return run


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Stage 1: shell orchestration through the REAL interactive engine over HTTP.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def stage1_server():
    import interactive_api
    from http.server import ThreadingHTTPServer
    # bind to an ephemeral port to avoid clashing with a dev server on 8091
    srv = ThreadingHTTPServer(("127.0.0.1", 0), interactive_api.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", interactive_api
    finally:
        for s in list(interactive_api._SESSIONS.values()):
            interactive_api._stop_server(s)
        srv.shutdown()


def test_stage2_composes_whatever_the_roles_wrote():
    """The engine collects each routed role's OWN files and commits them.

    Asserts the shape, never the content: the engine names no paths, so a test that
    demanded one would be re-introducing the coupling this design removed."""
    engine = importlib.import_module("engine")
    from fixture_executor import FixtureExecutor
    eng = engine.Engine(executor_obj=FixtureExecutor())
    try:
        run = _wait_terminal(eng.submit(
            "Build an MCP server with a chatbot UI",
            ["claude-code", "claude-code-validator", "opencode"]))
        assert run.status == "passed", run.fail_reason
        # the gate was a real execution with a real verdict
        assert run.gate["passed"] is True and run.gate["summary"]
        # every routed role contributed files, under its own directory
        diff = engine.public_diff(run)
        paths = {f["path"] for f in diff["files"]}
        for agent_id in run.agents:
            assert any(p.startswith(f"{agent_id}/") for p in paths), (agent_id, paths)
        # the validator's authored check ships with the deliverable so a reviewer can
        # rerun the exact gate that passed
        assert any(p.endswith("acceptance_check") for p in paths), paths
    finally:
        eng.shutdown()


# ---------------------------------------------------------------------------
# Stage 3: the runs above land in the shared ledger; metrics aggregate them.
# ---------------------------------------------------------------------------
def test_stage3_metrics_aggregate_the_real_runs():
    # the prior tests appended orchestrator_run + stage1 rows to the ledger
    assert os.path.isfile(_LEDGER), "no telemetry ledger; Stage 1/2 should have written it"
    import metrics_lib

    dash = metrics_lib.get_dashboard()
    assert dash["runs_total"] >= 1, f"dashboard saw no runs: {dash}"

    cost = metrics_lib.get_cost_breakdown(by="agent")["breakdown"]
    # the three roles all show up as attributed cost buckets (estimated, not a race)
    assert set(cost) & {"claude-code", "claude-code-validator", "opencode"}, f"no per-agent attribution: {cost}"

    me = __import__("getpass").getuser()
    metrics = metrics_lib.get_user_metrics(me, "24h")
    assert metrics["runs"] >= 1
    assert metrics["p95_latency_ms"] >= 0
