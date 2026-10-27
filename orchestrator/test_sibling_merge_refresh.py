"""A pull request whose base moved under it gets its ONE bounded refresh.

Live run run_052015_faadb1b489d5 (October 13, merge policy auto): Claude Code's backend
merged first, Codex's frontend had changed the same root package.json, README.md and
.gitignore, and nothing refreshed it. Its gate reported STALE_PATCH, the ordinary
repair re-ran Codex on the OLD base, the same stale patch came back, and a pull request
that only needed rebasing ended ROLE_PR_BLOCKED. The per-PR loop now rebases each later
pull request onto the branch a sibling merged into, and gives its owner one refresh
turn when the paths conflict or a declared dependency just merged.
"""
from __future__ import annotations

import os
import shutil
import time
from types import SimpleNamespace

import pytest

import engine
import reviewer
import work_items
from engine import Engine, TERMINAL, public_result
from fixture_executor import FixtureExecutor

_APPROVE = {"approve": True, "reasons": [],
            "assessment": f"**Assessment**: Approve\n\n{reviewer.LGTM_TOKEN}\n",
            "panels": [], "review_unavailable": False}


def _run_two_builders(monkeypatch, tmp_path, *, frontend_failure="",
                      backend_needs_repair=False):
    numbers = iter(range(1, 100))
    turns: list[tuple[str, int, bool]] = []

    def publish(self, run):
        active = run._active_builders if run._active_builders is not None else {
            i.agent for i in run.work_items.values() if i.kind == "builder"}
        for item in run.work_items.values():
            if item.kind == "builder" and item.agent in active:
                item.state, item.stale = "in_review", False
                if not item.pr:
                    n = next(numbers)
                    item.pr = {"pr_url": f"https://github.com/team/app/pull/{n}",
                               "number": n, "base": item.base_branch, "head": item.branch}

    real_produce = FixtureExecutor.produce

    def produce(self, run, agent_id, role):
        path = real_produce(self, run, agent_id, role)
        if agent_id in ("claude-code", "codex"):
            refreshing = "STALE PATCH" in (run._refresh_context or "")
            repairing = "REPAIR ROUND" in (run._refresh_context or "")
            turns.append((agent_id, run.iterations, refreshing))
            manifest = os.path.join(run.roledir(agent_id), "package.json")
            existing = open(manifest).read() if os.path.exists(manifest) else ""
            line = f'"{agent_id}": "{"reconciled" if refreshing else "first"}"\n'
            # Both builders write the SAME shared manifest, as both did live in an
            # empty repository. On its refresh the later one keeps what merged.
            with open(manifest, "w") as f:
                f.write((existing if refreshing or repairing else "") + line)
            pending = (
                agent_id == "codex" and frontend_failure
                and (not repairing or frontend_failure == "persistent")
            ) or (agent_id == "claude-code" and backend_needs_repair and not repairing)
            if pending:
                # The real fixture executable rejects this builder-owned marker.
                # Repair changes the builder's file, never the authored check.
                with open(path, "a") as f:
                    f.write("fixture repair pending\n")
        return path

    monkeypatch.setattr(Engine, "_publish_active_work_items", publish)
    monkeypatch.setattr(Engine, "_comment_work_item", lambda self, run, item, body: None)
    monkeypatch.setattr(FixtureExecutor, "produce", produce)
    real_assess = reviewer.assess
    monkeypatch.setattr(engine.reviewer, "assess",
                        lambda run, g, n, judge_=None, subject=None, **kw:
                        real_assess(run, g, n, judge=lambda *a: dict(_APPROVE), subject=subject))
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    monkeypatch.delenv("WORKSHOP_SEMANTIC_REFRESH", raising=False)

    e = Engine(executor_obj=FixtureExecutor())
    try:
        r = e.submit("Use preset=web-app.", ["claude-code", "codex", "kiro"])
        deadline = time.time() + 90
        while r.status not in TERMINAL and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.2)
        return r, public_result(r), turns
    finally:
        e.shutdown()


def test_a_later_pull_request_is_refreshed_onto_the_merged_sibling_and_merges(monkeypatch, tmp_path):
    run, result, turns = _run_two_builders(monkeypatch, tmp_path)

    states = {row["agent"]: row.get("state") for row in result["role_prs"]}
    assert states == {"claude-code": "merged", "codex": "merged"}, result.get("fail_reason")
    # Codex ran once to build and exactly once more to reconcile on the new base.
    codex_turns = [t for t in turns if t[0] == "codex"]
    assert [t[2] for t in codex_turns] == [False, True]
    assert [t for t in turns if t[0] == "claude-code"] == [("claude-code", 1, False)]
    merged = open(os.path.join(run.integration_base_dir, "package.json")).read()
    assert '"claude-code": "first"' in merged and '"codex": "reconciled"' in merged
    assert result["status"] == "passed"


@pytest.mark.parametrize("frontend_failure", ["repairable", "persistent"])
@pytest.mark.parametrize("backend_needs_repair", [False, True])
def test_a_conflict_refresh_preserves_exactly_one_repair_per_pull_request(
        monkeypatch, tmp_path, frontend_failure, backend_needs_repair):
    """The October 27 live run spent its repair on a conflict refresh.

    Exercise the real engine, Git worktrees, and executable exit codes. The
    refreshed frontend first fails; exactly one repair either passes or remains
    red, without starting the backend again or re-authoring the check.
    """
    run, result, turns = _run_two_builders(
        monkeypatch, tmp_path, frontend_failure=frontend_failure,
        backend_needs_repair=backend_needs_repair)
    backend, frontend = (run.work_items[a] for a in ("claude-code", "codex"))
    rows = {row["agent"]: row for row in result["role_prs"]}
    assert rows["claude-code"]["state"] == "merged"
    expected = "merged" if frontend_failure == "repairable" else "blocked"
    assert rows["codex"]["state"] == expected, result.get("fail_reason")
    assert frontend.attempt == 3
    assert frontend.refresh_attempts == 1
    assert frontend.repair_attempts == 1
    assert [t[2] for t in turns if t[0] == "codex"] == [False, True, False]
    assert sum(t[0] == "claude-code" for t in turns) == 1 + backend_needs_repair
    assert backend.repair_attempts == int(backend_needs_repair)
    assert backend.refresh_attempts == 0
    assert len({row["pr_url"] for row in result["role_prs"]}) == 2
    gates = [g for g in run.gate_history if g["work_id"] == frontend.work_id]
    assert [g["passed"] for g in gates] == [
        False, frontend_failure == "repairable"]
    assert len({g["check_sha256"] for g in gates}) == 1
    assert len(frontend.review_rounds) == 2
    if frontend_failure == "persistent":
        assert result["status"] == "needs_human"
        assert not result["resubmission_allowed"]
        assert frontend.merge_state == "blocked"
    else:
        assert result["status"] == "passed"


def _refresh_fixture(monkeypatch, tmp_path):
    item = work_items.WorkItem.create(
        "run_refresh_budget", "codex", "frontend-builder", "frontend",
        base_branch="main", token="front")
    merged = work_items.WorkItem.create(
        "run_refresh_budget", "claude-code", "backend-builder", "backend",
        base_branch="main", token="back")
    item.depends_on = [merged.work_id]
    previous, work, latest = (tmp_path / n for n in ("previous", "work", "latest"))
    for path, value in ((previous, "base"), (work, "frontend"), (latest, "backend")):
        path.mkdir()
        (path / "shared.txt").write_text(value)
    run = SimpleNamespace(
        workdir=str(tmp_path), integration_base_dir=str(latest),
        integration_base={"sha": "new-base"}, final_base_branch="main",
        item_base_dir=lambda _: str(previous), roledir=lambda _: str(work),
        _integration_brief_md="", log=lambda *args: None,
    )
    worker = Engine.__new__(Engine)
    worker.executor = SimpleNamespace(name="fixture")
    # This boundary stages bytes for a later owner turn. The real conflict
    # detection and both budget decisions still run; no owner is dispatched here.
    def stage(_run, _item, seed):
        shutil.copytree(seed, work, dirs_exist_ok=True)
        return 1

    monkeypatch.setattr(worker, "_stage_refreshed_work_item", stage)
    return worker, run, item, merged, work, latest


@pytest.mark.parametrize("semantic", [False, True])
def test_a_prior_repair_leaves_one_shared_owner_refresh_allowance(
        monkeypatch, tmp_path, semantic):
    worker, run, item, merged, work, latest = _refresh_fixture(monkeypatch, tmp_path)
    item.attempt = 2
    item.repair_attempts = 1
    if semantic:
        monkeypatch.setenv("WORKSHOP_SEMANTIC_REFRESH", "1")
    else:
        monkeypatch.delenv("WORKSHOP_SEMANTIC_REFRESH", raising=False)
    assert worker._refresh_pending_work(run, [item], merged) == [item]
    assert item.refresh_attempts == 1
    assert item.repair_attempts == 1
    # Moving the base again, with a different kind of refresh, cannot grant a
    # second owner turn. Both refresh kinds share the one base-refresh allowance.
    (work / "shared.txt").write_text("frontend reconciled")
    (latest / "shared.txt").write_text("backend moved again")
    if semantic:
        monkeypatch.delenv("WORKSHOP_SEMANTIC_REFRESH", raising=False)
    else:
        monkeypatch.setenv("WORKSHOP_SEMANTIC_REFRESH", "1")
    with pytest.raises(RuntimeError, match="STALE_PATCH_REFRESH_CAP"):
        worker._refresh_pending_work(run, [item], merged)
    assert item.refresh_attempts == 1
    assert item.repair_attempts == 1


def test_a_clean_rebase_does_not_replenish_a_spent_repair(monkeypatch, tmp_path):
    worker, run, item, merged, work, latest = _refresh_fixture(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKSHOP_SEMANTIC_REFRESH", raising=False)
    (work / "shared.txt").write_text("base")
    (work / "frontend.txt").write_text("frontend-only change")
    item.repair_attempts = 1
    assert worker._refresh_pending_work(run, [item], merged) == []
    assert item.refreshes == 1
    assert item.refresh_attempts == 0
    monkeypatch.setattr(worker, "_route_repair",
                        lambda *args, **kw: pytest.fail("spent repair was routed again"))
    assert worker._repair_pull_request(run, item, {}, "after clean rebase") is False
    assert item.repair_attempts == 1


@pytest.mark.parametrize("completed", [False, True])
def test_a_repair_attempt_cannot_be_reentered_even_when_execution_fails(
        monkeypatch, completed):
    worker = Engine.__new__(Engine)
    item = work_items.WorkItem.create(
        "run_repair_budget", "codex", "frontend-builder", "frontend", token="front")
    run = SimpleNamespace(
        _active_builders={item.agent}, iterations=1, log=lambda *args: None)
    calls = []
    monkeypatch.setattr(worker, "_route_repair",
                        lambda *args, **kw: calls.append("route"))
    monkeypatch.setattr(worker, "_execute",
                        lambda *args, **kw: calls.append("execute") or completed)
    assert worker._repair_pull_request(run, item, {}, "first failure") is completed
    assert worker._repair_pull_request(run, item, {}, "reentered") is False
    assert calls == ["route", "execute"]
    assert item.repair_attempts == 1


@pytest.mark.parametrize("legacy", [
    {"attempt": 2, "review_rounds": [{"round": 1}, {"round": 2}]},
    # The actual October 27 frontend: one refresh, one gate, run-wide round 3.
    {"attempt": 2, "refreshes": 1, "review_rounds": [{"round": 3}]},
    {"attempt": 1, "merge_state": "blocked", "review_rounds": [{"round": 1}]},
])
def test_legacy_readback_cannot_revive_a_spent_or_ambiguous_allowance(
        monkeypatch, tmp_path, legacy):
    worker, run, item, merged, _work, _latest = _refresh_fixture(monkeypatch, tmp_path)
    stored = item.public()
    for key in ("repair_attempts", "refresh_attempts", "retry_budget_inferred"):
        del stored[key]
    stored.update(legacy)
    restored = work_items.WorkItem(**stored)
    assert restored.repair_attempts == restored.refresh_attempts == 1
    assert restored.retry_budget_inferred
    assert restored.public()["retry_budget_inferred"] is True
    monkeypatch.setattr(worker, "_route_repair",
                        lambda *args, **kw: pytest.fail("legacy repair was revived"))
    assert worker._repair_pull_request(run, restored, {}, "readback") is False
    with pytest.raises(RuntimeError, match="STALE_PATCH_REFRESH_CAP"):
        worker._refresh_pending_work(run, [restored], merged)
    # The supplied historical record remains evidence, not a migrated live run.
    assert "repair_attempts" not in stored and "refresh_attempts" not in stored


@pytest.mark.parametrize("repair,refresh", [(1, 0), (0, 1)])
def test_new_explicit_counters_survive_readback_without_spending_the_other_budget(
        repair, refresh):
    item = work_items.WorkItem.create(
        "run_new_budget", "codex", "frontend-builder", "frontend", token="front")
    item.attempt = 2
    item.repair_attempts, item.refresh_attempts = repair, refresh
    restored = work_items.WorkItem(**item.public())
    assert (restored.repair_attempts, restored.refresh_attempts) == (repair, refresh)
    assert not restored.retry_budget_inferred


@pytest.mark.parametrize("refresh_field", ["refreshes", "dependency_refreshes"])
def test_a_legacy_refresh_reservation_is_not_read_back_as_unused(refresh_field):
    item = work_items.WorkItem.create(
        "run_old_refresh", "codex", "frontend-builder", "frontend", token="front")
    stored = item.public()
    for key in ("repair_attempts", "refresh_attempts", "retry_budget_inferred"):
        del stored[key]
    stored.update({"attempt": 1, refresh_field: 1})
    restored = work_items.WorkItem(**stored)
    assert restored.refresh_attempts == 1
    assert restored.repair_attempts == 0
    assert restored.retry_budget_inferred
