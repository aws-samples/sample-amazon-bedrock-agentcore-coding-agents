"""A two-builder team build does each check once, on the branch it merges into.

October 13, run_080920_e788d0efcc9d (auto merge, 12.7 min): the checker waited for
the slowest builder, wrote both checks one after another, and finalization waited for
both. After the backend merged, Codex was re-run on the new base and produced a
byte-identical patch, and its check was written a second time because the base moved.
Under auto merge a pull request whose dependency merges first now gets its check
written once, after that merge, and a clean rebase is republished without re-running
its builder. Every verdict rule is unchanged: each pull request is still checked and
reviewed against the branch as it stands, and a red check still gets one repair.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import engine
import reviewer
import role_graph
from engine import Engine, TERMINAL, public_result
from fixture_executor import FixtureExecutor

_APPROVE = {"approve": True, "reasons": [],
            "assessment": f"**Assessment**: Approve\n\n{reviewer.LGTM_TOKEN}\n",
            "panels": [], "review_unavailable": False}


def _team_build(monkeypatch, *, policy="auto", semantic_refresh=False, options=None):
    numbers = iter(range(1, 100))
    turns: list[str] = []

    def publish(self, run):
        active = run._active_builders if run._active_builders is not None else {
            i.agent for i in run.work_items.values() if i.kind == "builder"}
        for item in run.work_items.values():
            state = run.progress[item.agent].state
            if item.kind == "builder" and item.agent in active and state == "done":
                item.state, item.stale = "in_review", False
                if not item.pr:
                    n = next(numbers)
                    item.pr = {"pr_url": f"https://github.com/team/app/pull/{n}",
                               "number": n, "base": item.base_branch, "head": item.branch}

    real_produce = FixtureExecutor.produce

    def produce(self, run, agent_id, role):
        path = real_produce(self, run, agent_id, role)
        if agent_id in ("claude-code", "codex"):
            turns.append(agent_id)
            # Separate paths: the merge is a clean rebase for the later pull request.
            with open(os.path.join(run.roledir(agent_id), f"{agent_id}.txt"), "w") as f:
                f.write(f"{agent_id} work\n")
        return path

    monkeypatch.setattr(Engine, "_publish_active_work_items", publish)
    monkeypatch.setattr(Engine, "_comment_work_item", lambda self, run, item, body: None)
    monkeypatch.setattr(FixtureExecutor, "produce", produce)
    real_assess = reviewer.assess
    monkeypatch.setattr(engine.reviewer, "assess",
                        lambda run, g, n, judge_=None, subject=None, **kw:
                        real_assess(run, g, n, judge=lambda *a: dict(_APPROVE), subject=subject))
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", policy)
    if semantic_refresh:
        monkeypatch.setenv("WORKSHOP_SEMANTIC_REFRESH", "1")
    else:
        monkeypatch.delenv("WORKSHOP_SEMANTIC_REFRESH", raising=False)

    e = Engine(executor_obj=FixtureExecutor())
    try:
        r = e.submit("Use preset=web-app.", ["claude-code", "codex", "kiro"], options=options)
        deadline = time.time() + 90
        while r.status not in TERMINAL and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.2)
        messages = [str(row.get("message")) for row in r.events]
        return r, public_result(r), turns, messages
    finally:
        e.shutdown()


def _index(messages, needle):
    return next(i for i, m in enumerate(messages) if needle in m)


def test_the_dependent_check_is_written_once_after_its_dependency_merges(monkeypatch):
    run, result, turns, messages = _team_build(monkeypatch)
    states = {row["agent"]: row.get("state") for row in result["role_prs"]}
    assert states == {"claude-code": "merged", "codex": "merged"}, result.get("fail_reason")
    assert result["status"] == "passed"
    backend = run.work_items["claude-code"].work_id
    frontend = run.work_items["codex"].work_id
    # Each builder ran once: the clean rebase onto the merged backend re-ran nobody.
    assert sorted(turns) == ["claude-code", "codex"]
    assert any(f"{frontend}: clean refresh published without rerunning codex" in m
               for m in messages)
    assert not any("SEMANTIC INTEGRATION REFRESH" in m for m in messages)
    # One authored check per pull request, the frontend's only after the merge.
    authored = [m for m in messages if "authored the acceptance check for" in m]
    assert [m.split("for ", 1)[1].split(";")[0] for m in authored] == [backend, frontend]
    assert _index(messages, f"{frontend}'s check is written after") < \
        _index(messages, f"{backend}: merged into")
    assert _index(messages, f"{backend}: merged into") < \
        _index(messages, f"writing {frontend}'s check now") < \
        _index(messages, f"authored the acceptance check for {frontend}")
    # Both gates ran against the branch as it stood, one round each; the backend's
    # during execution, while the frontend's builder could still be working.
    assert [g.get("stage") for g in run.gate_history] == [
        f"{backend} round 1", f"{frontend} round 1"]
    phases = [str(row.get("phase")) for row in run.events]
    merged_at = _index(messages, f"{backend}: merged into")
    assert "now, while the other builders work" in messages[merged_at - 3] + \
        messages[merged_at - 2] + messages[merged_at - 1] + \
        messages[_index(messages, f"{backend}'s authored check on")]
    assert phases[merged_at] == "agent_execution", "merged during execution, not after it"


def test_an_early_red_gate_is_repaired_in_finalization_with_the_same_evidence(monkeypatch):
    run, result, turns, messages = _team_build(monkeypatch, options={"fail_first_check": True})
    backend = run.work_items["claude-code"].work_id
    stages = [g.get("stage") for g in run.gate_history]
    # The red round 1 ran once (early), was not re-run, and the one bounded repair
    # re-ran the SAME check.
    assert stages.count(f"{backend} round 1") == 1, stages
    assert f"{backend} round 2" in stages, stages
    hashes = [g.get("check_sha256") for g in run.gate_history if g.get("stage", "").startswith(backend)]
    assert len(set(hashes)) == 1, hashes
    assert turns.count("claude-code") == 2
    assert {row["agent"]: row.get("state") for row in result["role_prs"]}["claude-code"] == "merged", \
        result.get("fail_reason")


def test_human_review_writes_both_checks_up_front_as_before(monkeypatch):
    run, result, turns, messages = _team_build(monkeypatch, policy="human_review")
    assert {row["agent"]: row.get("state") for row in result["role_prs"]} == {
        "claude-code": "awaiting_review", "codex": "awaiting_review"}
    assert not any("is written after" in m for m in messages)
    assert len([m for m in messages if "authored the acceptance check for" in m]) == 2
    assert sorted(turns) == ["claude-code", "codex"]


def test_the_semantic_owner_refresh_can_be_restored(monkeypatch):
    run, result, turns, messages = _team_build(monkeypatch, semantic_refresh=True)
    assert {row["agent"]: row.get("state") for row in result["role_prs"]} == {
        "claude-code": "merged", "codex": "merged"}, result.get("fail_reason")
    assert turns.count("codex") == 2
    assert any("SEMANTIC INTEGRATION REFRESH" in m for m in messages)


def test_the_checker_can_start_before_a_builder_it_does_not_grade_yet():
    finished: dict[str, float] = {}
    started: dict[str, float] = {}
    slow = threading.Event()

    def dispatch_for(agent):
        def run():
            started[agent] = time.monotonic()
            if agent == "codex":
                slow.wait(2)
            finished[agent] = time.monotonic()
            if agent == "kiro":
                slow.set()
        return run

    graph, _ = role_graph.build_graph(["claude-code", "codex", "kiro"], dispatch_for,
                                      execution_timeout=10, checker_waits_for={"claude-code"})
    asyncio.new_event_loop().run_until_complete(graph.invoke_async(""))
    assert started["kiro"] >= finished["claude-code"]
    assert started["kiro"] < finished["codex"], "the checker waited for the builder it does not grade"
    assert set(finished) == {"claude-code", "codex", "kiro"}


def test_by_default_the_checker_still_waits_for_every_builder():
    finished: dict[str, float] = {}
    started: dict[str, float] = {}

    def dispatch_for(agent):
        def run():
            started[agent] = time.monotonic()
            if agent == "codex":
                time.sleep(0.3)
            finished[agent] = time.monotonic()
        return run

    graph, _ = role_graph.build_graph(["claude-code", "codex", "kiro"], dispatch_for,
                                      execution_timeout=10)
    asyncio.new_event_loop().run_until_complete(graph.invoke_async(""))
    assert started["kiro"] >= max(finished["claude-code"], finished["codex"])


def test_a_check_waits_for_its_builder_when_the_dependency_failed(monkeypatch):
    """November 18, team 8: the backend's Runtime failed at 34 s, the frontend's
    dependency wait no longer held, and its check was written against an EMPTY tree
    while Codex was still building. The check is written once the work exists."""
    real_produce = FixtureExecutor.produce

    def produce(self, run, agent_id, role):
        if agent_id == "claude-code":
            raise RuntimeError("ROLE_EXECUTION_ERROR: the backend's shell closed")
        if agent_id == "codex":
            time.sleep(1.5)
            path = real_produce(self, run, agent_id, role)
            with open(os.path.join(run.roledir(agent_id), "codex.txt"), "w") as f:
                f.write("codex work\n")
            return path
        return real_produce(self, run, agent_id, role)

    def publish(self, run):
        for item in run.work_items.values():
            if item.kind == "builder" and run.progress[item.agent].state == "done":
                item.state, item.stale = "in_review", False
                if not item.pr:
                    item.pr = {"pr_url": "https://github.com/team/app/pull/1", "number": 1,
                               "base": item.base_branch, "head": item.branch}

    monkeypatch.setattr(Engine, "_publish_active_work_items", publish)
    monkeypatch.setattr(Engine, "_comment_work_item", lambda self, run, item, body: None)
    monkeypatch.setattr(FixtureExecutor, "produce", produce)
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    e = Engine(executor_obj=FixtureExecutor())
    try:
        r = e.submit("Use preset=web-app.", ["claude-code", "codex", "kiro"])
        deadline = time.time() + 90
        while r.status not in TERMINAL and time.time() < deadline:
            time.sleep(0.1)
        messages = [str(row.get("message")) for row in r.events]
        frontend = r.work_items["codex"].work_id
        published = _index(messages, "frontend: prepared")
        authored = [i for i, m in enumerate(messages)
                    if f"authored the acceptance check for {frontend}" in m]
        assert authored and all(i > published for i in authored), messages
        trees = [m for m in messages if f"{frontend}: pull request tree built" in m]
        assert trees and not any("(0 file(s))" in m for m in trees), trees
    finally:
        e.shutdown()
