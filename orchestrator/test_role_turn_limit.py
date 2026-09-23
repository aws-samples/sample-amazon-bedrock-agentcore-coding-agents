"""A recorded CLI cap is a human handoff, not an empty/transient role.

The terminal line below is the complete text-mode error from both Claude Code
2.1.278 dispatches in the September 22 run. Its real exit was 1. These tests
replay that adapter input offline; they do not replay a model or change the run.
The whole-engine cases keep admission, worktrees, graph, persistence and reporting
real. Only the native Runtime I/O and fixture artifact producer are substituted.
"""
from __future__ import annotations

import hashlib
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import chat
import engine
import roles
import runtime_config
import runtime_exec
import runtime_stage
from fixture_executor import FixtureExecutor


NATIVE_LIMIT_TAIL = "Error: Reached max turns (50)"
_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/offline-test"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Turn-limit regressions must stay offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(runtime_config, "pick", lambda agent: (_ARN, {}))
    monkeypatch.setattr(runtime_stage, "archive_uri",
                        lambda *a, **kw: "s3://offline-test/source.tar.gz")
    monkeypatch.setattr(runtime_exec, "_live_session_for", lambda *a, **kw: None)
    monkeypatch.setattr(engine.llm, "resolve", lambda model: model)


def _script_native(monkeypatch, outcomes):
    calls = []

    def dispatch(runtime_arn, agent_id, prompt, run_subdir, artifact_rel,
                 model, region, on_line, timeout_s):
        calls.append({"agent": agent_id, "timeout_s": timeout_s, "model": model})
        assert len(calls) <= len(outcomes), "Unexpected additional CLI invocation"
        exit_code, transcript = outcomes[len(calls) - 1]
        begin = runtime_exec._RUN_BEGIN + "-offline"
        end = runtime_exec._RUN_END + "-offline"
        if on_line:
            for line in [begin, *transcript.splitlines(), end]:
                on_line(line)
        return {
            "exit": exit_code,
            "transcript": runtime_exec._slice(
                f"{begin}\n{transcript}\n{end}\n", begin, end),
            "session_id": "00000000-0000-4000-8000-000000000001",
        }

    monkeypatch.setattr(runtime_exec, "_dispatch_once", dispatch)
    return calls


def _invoke(agent="claude-code"):
    return runtime_exec.run_in_runtime(
        runtime_arn=_ARN, agent_id=agent, prompt="offline request",
        run_subdir="offline/work", artifact_rel=None,
        model=roles.get(agent).default_model)


@pytest.mark.parametrize("agent", ["claude-code", "claude-code-validator"])
@pytest.mark.parametrize("prefix", ["", "earlier output\n" * 100])
def test_exact_native_cap_is_typed_before_display_truncation(monkeypatch, agent, prefix):
    calls = _script_native(monkeypatch, [(1, prefix + NATIVE_LIMIT_TAIL)])
    with pytest.raises(runtime_exec.RoleTurnLimitError) as caught:
        _invoke(agent)
    error = caught.value
    assert (error.agent_id, error.turn_limit, error.exit_code) == (agent, 50, 1)
    assert str(error).startswith("ROLE_TURN_LIMIT:")
    assert str(error).endswith(NATIVE_LIMIT_TAIL)
    assert len(calls) == 1


@pytest.mark.parametrize(("agent", "exit_code", "tail"), [
    ("claude-code", 0, NATIVE_LIMIT_TAIL),
    ("claude-code", None, NATIVE_LIMIT_TAIL),
    ("claude-code", 1, f'The example says "{NATIVE_LIMIT_TAIL}"'),
    ("claude-code", 1, f"```\n{NATIVE_LIMIT_TAIL}\n```"),
    ("claude-code", 1, NATIVE_LIMIT_TAIL + "\nAnother error"),
    ("claude-code", 1, "Error: Reached max turns (fifty)"),
    ("claude-code", 1, "request exceeded a turn budget"),
    ("codex", 1, NATIVE_LIMIT_TAIL),
    ("kiro", 1, NATIVE_LIMIT_TAIL),
])
def test_prose_zero_exit_and_other_clis_are_not_native_caps(
        monkeypatch, agent, exit_code, tail):
    _script_native(monkeypatch, [(exit_code, tail)])
    if exit_code == 0:
        assert _invoke(agent)["exit"] == 0
    else:
        with pytest.raises(runtime_exec.RoleExecutionError) as caught:
            _invoke(agent)
        assert type(caught.value) is runtime_exec.RoleExecutionError


@pytest.fixture
def run_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "_RUNS_DIR", str(tmp_path / "runs"))

    def invoke(*, agents=("claude-code", "kiro"), cap_role="claude-code",
               outcomes=((1, NATIVE_LIMIT_TAIL),), native_from_attempt=1,
               cap_subject=None, before_repair=None, options=None):
        monkeypatch.setenv("WORKSHOP_ROLES", ",".join(agents))
        monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "human_review")
        # These views are captured at engine import in a real deployment.
        # Re-project them after selecting a restored roster in this process.
        monkeypatch.setattr(engine, "ROLE_BY_AGENT", roles.role_names())
        calls = _script_native(monkeypatch, outcomes)
        dispatches, produced, published, gated, reviewed = [], [], [], [], []
        check_preparations, check_snapshots = [], []
        current_subject = None

        class NativeFailureFixture(FixtureExecutor):
            def dispatch(self, run, agent, role, local_dispatch):
                dispatches.append(agent)
                super().dispatch(run, agent, role, local_dispatch)

            def produce(self, run, agent, role):
                produced.append(agent)
                if (agent == cap_role
                        and (cap_subject is None or current_subject == cap_subject)
                        and run.work_items[agent].attempt >= native_from_attempt):
                    worker._runtime_cli(
                        run, agent, role, run.task, roles.get(agent).default_model)
                return super().produce(run, agent, role)

        worker = engine.Engine(executor_obj=NativeFailureFixture())
        publish = worker._publish_active_work_items
        prepare = worker._prepare_checker_checkout

        def record_prepare(run, agent, subject):
            nonlocal current_subject
            current_subject = subject.agent
            check_preparations.append(subject.agent)
            return prepare(run, agent, subject)

        monkeypatch.setattr(worker, "_prepare_checker_checkout", record_prepare)

        def record_publish(run):
            published.append(True)
            return publish(run)

        monkeypatch.setattr(worker, "_publish_active_work_items", record_publish)
        gate = worker._gate_one_pull_request
        assess = worker._assess_pull_request

        def record_gate(run, item, stage):
            verdict = gate(run, item, stage)
            gated.append((item.agent, run.status, verdict))
            check_snapshots.append({
                work_id: (path, hashlib.sha256(Path(path).read_bytes()).hexdigest())
                for work_id, path in run._item_checks.items()
            })
            return verdict

        def record_review(run, item, gate, stage):
            approved = assess(run, item, gate, stage)
            reviewed.append((item.agent, run.status, approved))
            return approved

        monkeypatch.setattr(worker, "_gate_one_pull_request", record_gate)
        monkeypatch.setattr(worker, "_assess_pull_request", record_review)
        if before_repair is not None:
            repair = worker._repair_pull_request

            def record_repair(run, item, gate, stage):
                before_repair(worker, run, item)
                return repair(run, item, gate, stage)

            monkeypatch.setattr(worker, "_repair_pull_request", record_repair)
        run = engine.Run(
            run_id="run_000000_000000000001", task="offline turn-limit regression",
            agents=list(agents), roles={}, options=options or {},
            created_at="2026-09-22T00:00:00Z", _executor_name="fixture",
        )
        run._explicit_agents = True
        run._t0 = time.monotonic()
        worker._drive(run)
        saved = json.loads(
            (tmp_path / "runs/state" / (run.run_id + ".json")).read_text())
        worker.shutdown()
        return SimpleNamespace(
            run=run, saved=saved, calls=calls, dispatches=dispatches,
            produced=produced, published=published, gated=gated, reviewed=reviewed,
            check_preparations=check_preparations, check_snapshots=check_snapshots,
            saved_path=tmp_path / "runs/state" / (run.run_id + ".json"),
        )

    return invoke


@pytest.mark.parametrize("checker", ["kiro", "claude-code-validator"])
def test_whole_engine_stops_after_one_cap_and_never_dispatches_checker(
        run_engine, checker):
    result = run_engine(agents=("claude-code", checker))
    run = result.run
    assert result.dispatches == result.produced == ["claude-code"], (
        run.status, run.fail_reason)
    assert result.published == []
    assert len(result.calls) == 1
    assert result.calls[0]["timeout_s"] == engine.HARNESS_ROLE_TIMEOUT_S
    assert "--max-turns" not in roles.get("claude-code").cli
    assert "--print " in roles.get("claude-code").cli
    assert run.iterations == 1
    assert run.progress["claude-code"].state == "error"
    assert NATIVE_LIMIT_TAIL in run.progress["claude-code"].note
    blocked = run.progress[checker]
    assert blocked.state == "blocked"
    assert blocked.engine == "" and blocked.latency_ms == 0
    assert blocked.runtime_session_id is None
    assert run.work_items["claude-code"].work_id in blocked.note
    assert run.work_items[checker].attempt == 0
    assert run.work_items[checker].state == "blocked"
    assert run.work_items["claude-code"].patch_digest is None
    assert run.gate_history == [] and run.gate is None and run.review is None
    assert run.role_prs == [] and run.pr_url is None
    assert (result.saved["status"], result.saved["fail_reason"]) == (
        "needs_human", "ROLE_TURN_LIMIT")
    assert result.saved["resubmission_allowed"] is False
    assert "Do not resubmit" in result.saved["next_action"]
    messages = "\n".join(row["message"] for row in run.events)
    for false_claim in ("transient failure", "systemic", "WORK_PATCH_MISSING",
                        "one bounded re-dispatch", "ALL 2 routed roles failed"):
        assert false_claim not in messages


@pytest.mark.parametrize("checker", ["kiro", "claude-code-validator"])
@pytest.mark.parametrize("repair_sibling", [False, True])
def test_capped_builder_does_not_stop_sibling_gate_review_or_bounded_repair(
        run_engine, checker, repair_sibling):
    result = run_engine(
        agents=("claude-code", "codex", checker),
        options={"fail_first_check": repair_sibling})
    run = result.run
    assert (run.status, run.fail_reason) == ("needs_human", "ROLE_TURN_LIMIT")
    assert len(result.calls) == 1
    assert result.produced.count(checker) == 1, "Repair must reuse the authored check"
    assert run.progress[checker].state == "done"
    assert run.progress["codex"].state == "done"
    assert run.work_items["codex"].patch_digest
    assert run.work_items["codex"].pr
    assert run.work_items["codex"].merge_state == "human_review"
    assert run.work_items["codex"].review_rounds[-1]["state"] == "approved"
    assert run.work_items["codex"].attempt == (2 if repair_sibling else 1)
    assert run.work_items["claude-code"].attempt == 1
    assert not run.work_items["claude-code"].pr
    assert set(run._item_checks) == {run.work_items["codex"].work_id}
    # These are real subprocess gates and the real fixture review path. Merely
    # authoring an executable must not count as verification of the sibling.
    assert [row["passed"] for row in run.gate_history] == (
        [False, True] if repair_sibling else [True])
    assert {row["work_id"] for row in run.gate_history} == {
        run.work_items["codex"].work_id}
    assert len({row["check_sha256"] for row in run.gate_history}) == 1
    assert run.gate["passed"] is True and run.review["lgtm"] is True
    assert result.reviewed[-1] == ("codex", "running", True)
    assert all(agent == "codex" and state == "running"
               for agent, state, _ in result.gated + result.reviewed)
    assert [(row["agent"], row["state"]) for row in run.role_prs] == [
        ("codex", "awaiting_review")]
    assert result.saved["gate_history"] == run.gate_history
    assert result.saved["role_prs"] == run.role_prs
    assert result.saved["resubmission_allowed"] is False


@pytest.mark.parametrize("changed_base", [False, True])
def test_capped_sibling_checker_is_not_retried_during_owner_repair(
        run_engine, changed_base):
    """A check exists, B's checker caps, then A must finish its own repair."""
    previous_base = []

    def prepare_repair(worker, run, item):
        assert item.agent == "claude-code"
        evidence = Path(worker._kept_check_path(run, item) + ".json")
        previous_base.append(json.loads(evidence.read_text())["base_digest"])
        if changed_base:
            # A different PR advanced the real base source. The selected owner's
            # check must be authored again, without redispatching B's capped work.
            (Path(run.integration_base_dir) / "merged-sibling.txt").write_text(
                "A separately merged source change.\n")

    result = run_engine(
        agents=("claude-code", "codex", "claude-code-validator"),
        cap_role="claude-code-validator", cap_subject="codex",
        outcomes=((1, NATIVE_LIMIT_TAIL), (1, NATIVE_LIMIT_TAIL)),
        before_repair=prepare_repair, options={"fail_first_check": True})
    run = result.run
    assert len(result.calls) == 1, "A's repair must not redispatch B's capped checker"
    assert result.check_preparations == (
        ["claude-code", "codex", "claude-code"] if changed_base
        else ["claude-code", "codex"])
    assert [(row["agent"], row["passed"]) for row in run.gate_history] == [
        ("claude-code", False), ("claude-code", True)]
    assert [(row["agent"], row["state"]) for row in run.role_prs] == [
        ("claude-code", "awaiting_review"), ("codex", "blocked")]
    assert run.role_prs[1]["error"] == "ROLE_TURN_LIMIT"
    assert run.work_items["claude-code"].review_rounds[-1]["state"] == "approved"
    assert run.work_items["claude-code"].merge_state == "human_review"
    assert run.work_items["codex"].attempt == 1
    assert run.work_items["codex"].work_id not in run._item_checks
    assert result.reviewed[-1] == ("claude-code", "running", True)
    assert (result.saved["status"], result.saved["fail_reason"]) == (
        "needs_human", "ROLE_TURN_LIMIT")
    assert result.saved["resubmission_allowed"] is False
    item = run.work_items["claude-code"]
    evidence_path = Path(run.workdir) / "authored-checks" / item.work_id / (
        "acceptance_check.json")
    evidence = json.loads(evidence_path.read_text())
    assert (evidence["base_digest"] != previous_base[0]) is changed_base
    assert evidence["check_sha256"] == run.gate_history[-1]["check_sha256"]
    if not changed_base:
        assert len({row["check_sha256"] for row in run.gate_history}) == 1


@pytest.mark.parametrize("checker", ["kiro", "claude-code-validator"])
def test_active_repair_preserves_inactive_pr_authored_checks(run_engine, checker):
    result = run_engine(
        agents=("claude-code", "codex", checker), cap_role=None,
        options={"fail_first_check": True})
    run = result.run
    assert result.calls == []
    assert result.check_preparations == ["claude-code", "codex"]
    assert [(row["agent"], row["passed"]) for row in run.gate_history] == [
        ("claude-code", False), ("claude-code", True),
        ("codex", False), ("codex", True)]
    backend = run.work_items["claude-code"].work_id
    frontend = run.work_items["codex"].work_id
    assert result.check_snapshots[0][frontend] == result.check_snapshots[1][frontend]
    assert result.check_snapshots[2][backend] == result.check_snapshots[3][backend]
    for agent in ("claude-code", "codex"):
        item = run.work_items[agent]
        assert item.merge_state == "human_review"
        assert item.review_rounds[-1]["state"] == "approved"
        assert len({row["check_sha256"] for row in run.gate_history
                    if row["agent"] == agent}) == 1
    assert result.saved["status"] == "passed"
    assert result.saved["resubmission_allowed"] is False


def test_active_repair_still_rejects_changed_kept_check_after_sibling_cap(run_engine):
    def tamper(worker, run, item):
        kept = Path(worker._kept_check_path(run, item))
        kept.write_text(kept.read_text() + "\n# changed after the recorded gate\n")

    result = run_engine(
        agents=("claude-code", "codex", "claude-code-validator"),
        cap_role="claude-code-validator", cap_subject="codex",
        before_repair=tamper, options={"fail_first_check": True})
    run = result.run
    assert len(result.calls) == 1
    assert result.check_preparations == ["claude-code", "codex"]
    assert "CHECK_EVIDENCE_CHANGED" in run.progress["claude-code-validator"].note
    assert [(row["agent"], row["passed"]) for row in run.gate_history] == [
        ("claude-code", False)]
    assert all(row["state"] == "blocked" for row in run.role_prs)
    assert run.work_items["claude-code"].work_id not in run._item_checks
    assert result.saved["fail_reason"] == "ROLE_TURN_LIMIT"
    assert result.saved["resubmission_allowed"] is False


def test_generic_failure_keeps_one_retry_without_blame_for_unstarted_checker(run_engine):
    result = run_engine(outcomes=((1, "transport failed"), (1, "transport failed")))
    assert len(result.calls) == 2
    assert result.dispatches == ["claude-code"]
    assert result.run.progress["kiro"].state == "blocked"
    assert result.run.work_items["kiro"].attempt == 0
    assert result.saved["fail_reason"] == "ROLE_EXECUTION_ERROR"
    assert result.saved["resubmission_allowed"] is True


def test_checker_native_cap_is_an_executed_failure_not_dependency_blocked(run_engine):
    result = run_engine(
        agents=("claude-code", "claude-code-validator"),
        cap_role="claude-code-validator")
    assert result.dispatches == ["claude-code", "claude-code-validator"]
    assert len(result.calls) == 1
    assert result.run.progress["claude-code-validator"].state == "error"
    assert result.run.work_items["claude-code-validator"].attempt == 1
    assert result.saved["fail_reason"] == "ROLE_TURN_LIMIT"
    assert result.saved["resubmission_allowed"] is False
    assert result.run.gate_history == []


def test_repair_cap_preserves_existing_red_gate_and_reports_the_actual_limit(run_engine):
    result = run_engine(
        native_from_attempt=2, options={"fail_first_check": True})
    run = result.run
    assert len(result.calls) == 1
    assert result.dispatches == ["claude-code", "kiro", "claude-code"]
    assert run.work_items["claude-code"].attempt == 2
    assert run.work_items["kiro"].attempt == 1
    assert run.progress["kiro"].state == "blocked"
    assert len(run.gate_history) == 1 and run.gate_history[0]["passed"] is False
    assert run.role_prs[0]["error"] == "ROLE_TURN_LIMIT"
    assert run.role_prs[0]["state"] == "blocked"
    assert result.saved["fail_reason"] == "ROLE_TURN_LIMIT"
    assert result.saved["resubmission_allowed"] is False
    assert "transient failure" not in "\n".join(row["message"] for row in run.events)


def test_a_repair_cap_does_not_stop_the_next_siblings_repair_and_review(run_engine):
    result = run_engine(
        agents=("claude-code", "codex", "kiro"),
        native_from_attempt=2, options={"fail_first_check": True})
    run = result.run
    assert len(result.calls) == 1
    assert result.dispatches.count("claude-code") == 2  # initial work, then capped repair
    assert result.dispatches.count("codex") == 2
    assert run.work_items["claude-code"].merge_state == "blocked"
    assert run.work_items["codex"].merge_state == "human_review"
    assert run.work_items["codex"].review_rounds[-1]["state"] == "approved"
    assert [(row["agent"], row["passed"]) for row in run.gate_history] == [
        ("claude-code", False), ("codex", False), ("codex", True)]
    assert [(row["agent"], row["state"]) for row in run.role_prs] == [
        ("claude-code", "blocked"), ("codex", "awaiting_review")]
    assert run.role_prs[0]["error"] == "ROLE_TURN_LIMIT"
    assert all(state == "running" for _, state, _ in result.gated + result.reviewed)
    assert (result.saved["status"], result.saved["fail_reason"]) == (
        "needs_human", "ROLE_TURN_LIMIT")
    assert result.saved["resubmission_allowed"] is False


def test_blocked_checker_survives_snapshot_api_and_watcher_without_restarting_work(
        run_engine, monkeypatch):
    import connection_api
    import run_store
    import watch_run

    result = run_engine()
    before = result.saved_path.read_bytes()
    empty_engine = engine.Engine(executor_obj=FixtureExecutor())
    monkeypatch.setattr(connection_api, "ENGINE", empty_engine)
    monkeypatch.setattr(run_store, "_s3", lambda: None)
    for suffix in ("", "/result"):
        code, payload = connection_api.dispatch(
            "GET", f"/api/runs/{result.run.run_id}{suffix}", None)
        assert code == 200 and payload["source"] == "persisted"
        checker = next(row for row in payload["progress"] if row["agent"] == "kiro")
        assert checker["state"] == "blocked"
        assert "Not dispatched this round" in checker["note"]
        assert payload["work_items"]["kiro"]["attempt"] == 0
        assert payload["fail_reason"] == "ROLE_TURN_LIMIT"
        assert payload["resubmission_allowed"] is False
        lines = watch_run._frame(payload, watch_run._Ink(False), 240)
        assert any("kiro" in line and "blocked" in line for line in lines)
        assert not any("kiro" in line and "failed" in line.split("blocked")[0]
                       for line in lines)
    assert watch_run._state_mark("blocked", watch_run._Ink(True)) == (
        "\033[33mblocked\033[0m")
    assert result.saved_path.read_bytes() == before
    assert empty_engine.list() == []
    assert len(result.calls) == 1


def test_successful_builder_with_missing_patch_remains_an_invariant_failure(monkeypatch):
    """Do not turn a coordinator bookkeeping defect into a blocked dependency."""
    worker = engine.Engine(executor_obj=SimpleNamespace(name="agentcore"))
    run = engine.Run(run_id="offline", task="offline", agents=["claude-code"], roles={})
    item = engine._work_items.WorkItem.create(
        run.run_id, "claude-code", "backend-builder", "backend")
    run.work_items[item.agent] = item
    run.progress[item.agent] = engine.RoleResult(item.agent, item.role, state="done")
    monkeypatch.setattr(engine.github, "_gateway_config",
                        lambda: ("https://offline.invalid", "owner/repo"))
    with pytest.raises(RuntimeError, match="WORK_PATCH_MISSING"):
        worker._publish_active_work_items(run)
    worker.shutdown()


def test_reporting_and_coordinator_steering_stop_known_caps():
    action = engine.next_action("needs_human", "ROLE_TURN_LIMIT")
    assert "Do not resubmit" in action and "recorded limit error" in action
    assert "original request" in action
    assert "saved partial" not in action and "archive" not in action
    assert not engine.resubmission_allowed("needs_human", "ROLE_TURN_LIMIT")
    assert not engine.resubmission_allowed("failed", "ROLE_TURN_LIMIT:claude-code")
    prompt = chat.SYSTEM_PROMPT
    section = prompt[prompt.index("(`ROLE_TURN_LIMIT`)"):].split("\n* ", 1)[0]
    assert "Do not resubmit" in section and "stop" in section
    assert "checker marked blocked did not run" in section


def test_a_generic_partial_failure_does_not_invite_duplicating_published_siblings():
    prs = [{"pr_url": "https://github.com/example/offline/pull/1",
            "state": "awaiting_review"}]
    action = engine.next_action(
        "needs_human", "ROLE_EXECUTION_ERROR", role_prs=prs)
    assert "Do not resubmit" in action
    assert not engine.resubmission_allowed(
        "needs_human", "ROLE_EXECUTION_ERROR", role_prs=prs)
