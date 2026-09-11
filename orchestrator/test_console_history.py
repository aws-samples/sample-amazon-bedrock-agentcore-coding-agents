"""A real persisted checkpoint remains readable after the host engine restarts.

The reporting API may read old evidence; it must never revive a worker, change a
red gate, or attach a terminal which belonged to the previous process.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import connection_api as api
import engine
from fixture_executor import FixtureExecutor
import run_store


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(run_store, "_s3", lambda: None)
    monkeypatch.setattr(api, "ENGINE", engine.Engine(executor_obj=FixtureExecutor()))
    return tmp_path


def checkpoint(run_id="run_120000_saved", **changes):
    run = engine.Run(
        run_id=run_id, task="Document the existing service",
        agents=["claude-code"], roles={"claude-code": "backend"},
        options={"private_dispatch_option": "not public"},
        created_at="2026-09-10T12:00:00Z",
        user_identity={"email": "not-part-of-history@example.invalid"},
    )
    run.status, run.phase = "passed", "finalization"
    run.role_prs = [{
        "agent": "claude-code", "pr_url": "https://github.com/example/test/pull/3",
        "state": "awaiting_review", "attempt": 1,
    }]
    run.gate = {"passed": True, "checks": [], "summary": "Executable completed"}
    run.gate_history = [{"passed": True, "output": "Observed output from the check"}]
    run.role_events = {"claude-code": [{"kind": "text", "text": "Pull request opened"}]}
    for key, value in changes.items():
        setattr(run, key, value)
    api.ENGINE._persist_run(run)
    return run


def test_restart_keeps_pr_and_check_evidence_without_reviving_workers(history):
    run = checkpoint()
    api.ENGINE = engine.Engine(executor_obj=FixtureExecutor())

    code, detail = api.dispatch("GET", f"/api/runs/{run.run_id}", None)
    assert code == 200
    assert detail["source"] == "persisted"
    assert detail["status"] == run.status
    assert detail["role_prs"] == run.role_prs
    assert detail["gate_history"] == run.gate_history
    assert detail["gate"] == run.gate
    assert "user_identity" not in detail and "options" not in detail

    code, result = api.dispatch("GET", f"/api/runs/{run.run_id}/result", None)
    assert code == 200 and result["role_prs"] == run.role_prs
    assert api.ENGINE.list() == [], "History must never create executable Run objects"


def test_history_pages_across_dates_and_live_state_wins(history):
    old = checkpoint("run_235959_old", created_at="2026-09-10T23:59:59Z")
    middle = checkpoint("run_000001_middle", created_at="2026-09-11T00:00:01Z")
    latest = checkpoint("run_000002_latest", created_at="2026-09-11T00:00:02Z")
    latest.status = "running"
    api.ENGINE._runs[latest.run_id] = latest

    _, all_rows = api.dispatch("GET", "/api/runs", None)
    assert all_rows["total"] == 3
    assert [r["run_id"] for r in all_rows["runs"]] == [
        latest.run_id, middle.run_id, old.run_id,
    ]
    assert all_rows["runs"][0]["status"] == "running"
    assert "source" not in all_rows["runs"][0]
    assert all("gate_history" not in row and "work_items" not in row
               for row in all_rows["runs"]), "The history index must not fetch full check transcripts"
    _, page = api.dispatch("GET", "/api/runs", None, "limit=1&offset=1")
    assert page["total"] == 3 and page["offset"] == 1
    assert [r["run_id"] for r in page["runs"]] == [middle.run_id]
    _, empty = api.dispatch("GET", "/api/runs", None, "limit=0")
    assert empty["runs"] == [] and empty["total"] == 3


def test_red_gate_remains_red_and_no_second_build_is_authorized(history):
    run = checkpoint(
        status="needs_human", fail_reason="ITERATION_CAP",
        gate={"passed": False, "checks": [], "summary": "One assertion failed"},
    )
    _, detail = api.dispatch("GET", f"/api/runs/{run.run_id}", None)
    assert detail["status"] == "needs_human"
    assert detail["gate"]["passed"] is False
    assert detail["fail_reason"] == "ITERATION_CAP"
    assert detail["resubmission_allowed"] is False


def test_interrupted_snapshot_reports_interruption_without_rewriting_evidence(history):
    run = checkpoint(status="running")
    path = history / "state" / f"{run.run_id}.json"
    saved = json.loads(path.read_text())
    saved["_saved_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(saved))
    before = path.read_bytes()

    _, detail = api.dispatch("GET", f"/api/runs/{run.run_id}", None)
    assert detail["status"] == "needs_human"
    assert detail["fail_reason"] == "COORDINATOR_SESSION_INTERRUPTED"
    assert detail["resubmission_allowed"] is False, "An existing PR must not be duplicated"
    assert detail["role_prs"] == saved["role_prs"]
    assert path.read_bytes() == before
    assert api.ENGINE.list() == []


def test_recent_active_snapshot_is_not_misreported_as_completed(history):
    run = checkpoint(status="running")
    code, result = api.dispatch("GET", f"/api/runs/{run.run_id}/result", None)
    assert code == 409 and result["status"] == "running"


def test_saved_activity_is_available_but_terminals_are_not_fabricated(history):
    run = checkpoint()
    _, feed = api.dispatch("GET", f"/api/runs/{run.run_id}/terminals", None)
    assert feed["terminals"] == {}
    assert feed["source"] == "persisted"
    assert feed["events"]["claude-code"][0]["text"] == "Pull request opened"
    _, diff = api.dispatch("GET", f"/api/runs/{run.run_id}/diff", None)
    assert diff["files"] == [] and "pull request" in diff["reason"]


@pytest.mark.parametrize("run_id", ["..", "run_..", "run_%2Fsecret", "run_missing"])
def test_unknown_and_invalid_run_ids_stay_not_found(history, run_id):
    code, _ = api.dispatch("GET", f"/api/runs/{run_id}", None)
    assert code == 404
