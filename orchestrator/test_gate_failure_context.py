"""A check's early failure must survive capture, persistence, repair, and reporting."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import engine  # noqa: E402
import replay  # noqa: E402
import reviewer  # noqa: E402
import run_store  # noqa: E402
from work_items import WorkItem  # noqa: E402


def _execute(tmp_path, output, code=1):
    # The executable source contains no diagnostic wording. A source excerpt on
    # the PR therefore cannot accidentally satisfy an assertion about its output.
    data = tmp_path / "check-output.txt"
    data.write_text(output)
    check = tmp_path / "acceptance_check"
    check.write_text(
        "#!/bin/sh\n"
        f"cat {shlex.quote(str(data))}\n"
        f"exit {code}\n"
    )
    return reviewer.run_gate(str(check), str(tmp_path), "exercise the check")


def _report_and_prepare_repair(tmp_path, monkeypatch, gate, agent="claude-code"):
    monkeypatch.setattr(engine, "_RUNS_DIR", str(tmp_path / "runs"))
    worker = engine.Engine(executor_obj=SimpleNamespace(name="fixture"))
    run = engine.Run(
        run_id="run_120000_001", task="exercise the check",
        agents=[agent], roles={agent: "builder"}, iterations=1,
        status="needs_human", final_base_branch="main",
    )
    item = WorkItem.create(
        run.run_id, agent, "builder", engine.roles.get(agent).capability,
        base_branch="main", token="diagnostics",
    )
    run.work_items[agent] = item
    comments = []
    monkeypatch.setattr(
        worker, "_comment_work_item",
        lambda _run, _item, body: comments.append(body),
    )
    worker._record_gate(run, gate, "round 1", item)
    assert worker._assess_pull_request(run, item, gate, "round 1") is False
    worker._persist_run(run)
    saved = run_store.load(engine._RUNS_DIR, run.run_id)
    assert saved is not None

    # Feed the real prompt builder the record read back from disk. Only the
    # external Runtime dispatch is intercepted; no role or model is invoked.
    run.iterations = 2
    run.review = saved["review"]
    prompts = []

    def capture_cli(_run, _agent, _role, prompt, _model):
        prompts.append(prompt)

    monkeypatch.setattr(worker, "_runtime_cli", capture_cli)
    role = engine.RoleResult(agent=agent, role="builder")
    if engine.roles.get(agent).capability == "frontend":
        worker._cli_frontend_work(run, "", role)
    else:
        worker._cli_backend_server(run, role)
    return saved, prompts[0], comments[0]


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_early_failure_survives_saved_gate_repair_prompt_and_pr(
    tmp_path, monkeypatch, agent,
):
    failure = "  FAIL probe 1: expected conflict status 409; got 200  "
    trailing = [
        f"PASS probe {i}: " + "later successful assertion " * 5
        for i in range(2, 68)
    ]
    output = "\n".join([failure, *trailing, "67 checks, 1 failure"]) + "\n"
    assert len(output) > 4000 and len(trailing) > 40
    gate = _execute(tmp_path, output)
    assert gate["passed"] is False
    assert "exit 1" in gate["checks"][0]["detail"]
    assert gate["output"] == output[-4000:]
    assert failure not in gate["output"]
    assert failure not in replay._gate_output_excerpt(gate)[0]

    saved, prompt, comment = _report_and_prepare_repair(
        tmp_path, monkeypatch, gate, agent,
    )
    assert failure in prompt
    assert failure in comment
    saved_gate = saved["review"]["gate"]
    assert saved_gate["failure_lines"] == [failure]
    assert saved["work_items"][agent]["review_rounds"][0]["gate"] == saved_gate
    assert saved["gate_history"][0]["failure_lines"] == [failure]
    assert saved["gate_history"][0]["passed"] is False
    assert "output" not in saved["gate_history"][0]
    assert saved["gate"]["passed"] is False
    assert saved["review"]["state"] == "changes_requested"
    assert saved["review"]["lgtm"] is False
    assert saved_gate["output"] == output[-4000:]
    assert "Executed check: **FAILED**" in comment
    assert "**Assessment**: Request changes" in comment
    assert "up to 25" in prompt and "300 characters" in prompt
    assert "up to 25" in comment and "300 characters" in comment
    assert len(comment) <= 20_000


def test_failure_diagnostics_keep_wording_order_and_explicit_bounds(
    tmp_path, monkeypatch,
):
    failures = [
        f"  FAIL unique-probe-{i:02}: " + "observed value " * 80
        for i in range(40)
    ]
    output = "\n".join([
        *failures,
        *("PASS later assertion " + "detail " * 20 for _ in range(80)),
        "40 unsuccessful probes",
    ]) + "\n"
    gate = _execute(tmp_path, output)
    saved, prompt, comment = _report_and_prepare_repair(
        tmp_path, monkeypatch, gate,
    )
    expected = [line[:300] for line in failures[:25]]
    assert saved["review"]["gate"]["failure_lines"] == expected
    assert saved["gate_history"][0]["failure_lines"] == expected
    assert len(saved["review"]["gate"]["output"]) == 4000
    assert saved["gate"]["passed"] is False
    for rendered in (prompt, comment):
        positions = [rendered.index(line) for line in expected]
        assert positions == sorted(positions)
        assert all(f"unique-probe-{i:02}" not in rendered for i in range(25, 40))
        assert failures[0] not in rendered
    assert len(comment) <= 20_000


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_older_records_use_the_output_they_still_have(
    tmp_path, monkeypatch, agent,
):
    failure = "FAIL original wording: value is absent"
    output = "\n".join([
        failure, *(f"PASS probe {i}" for i in range(66)), "67 checks, 1 failure",
    ]) + "\n"
    assert len(output) < 4000
    gate = _execute(tmp_path, output)
    gate.pop("failure_lines", None)
    before = json.dumps(gate, sort_keys=True)
    assert failure not in replay._gate_output_excerpt(gate)[0]
    saved, prompt, comment = _report_and_prepare_repair(
        tmp_path, monkeypatch, gate, agent,
    )
    assert failure in prompt and failure in comment
    assert saved["review"]["gate"] == gate
    assert saved["gate_history"][0]["failure_lines"] == [failure]
    assert json.dumps(gate, sort_keys=True) == before
    assert "failure_lines" not in gate
    assert "Executed check: **FAILED**" in comment


def test_each_saved_review_round_retains_its_own_failure_diagnostics(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(engine, "_RUNS_DIR", str(tmp_path / "runs"))
    worker = engine.Engine(executor_obj=SimpleNamespace(name="fixture"))
    run = engine.Run(
        run_id="run_120000_002", task="exercise the check",
        agents=["codex"], roles={"codex": "builder"}, status="needs_human",
    )
    item = WorkItem.create(
        run.run_id, "codex", "builder", "frontend", token="saved-rounds",
    )
    run.work_items["codex"] = item
    historic = {
        "sequence": 1, "stage": "previously recorded", "passed": False,
        "summary": "no diagnostic field was recorded", "checks": [],
    }
    run.gate_history.append(dict(historic))
    failures = ["FAIL initial observed value", "FAIL repair still differs"]
    outputs = []
    for round_no, failure in enumerate(failures, 1):
        output = "\n".join([
            failure, *("PASS " + "later detail " * 20 for _ in range(80)),
        ]) + "\n"
        outputs.append(output[-4000:])
        gate = _execute(tmp_path, output)
        run.iterations = round_no
        worker._record_gate(run, gate, f"round {round_no}", item)
        assert worker._assess_pull_request(run, item, gate, f"round {round_no}") is False
    worker._persist_run(run)
    saved = run_store.load(engine._RUNS_DIR, run.run_id)
    rounds = saved["work_items"]["codex"]["review_rounds"]
    assert [row["gate"]["failure_lines"] for row in rounds] == [
        [failure] for failure in failures
    ]
    assert [row["gate"]["output"] for row in rounds] == outputs
    assert all(row["gate"]["passed"] is False and row["lgtm"] is False for row in rounds)
    assert saved["review"]["gate"] == rounds[-1]["gate"]
    assert saved["gate_history"][0] == historic
    assert [row["failure_lines"] for row in saved["gate_history"][1:]] == [
        [failure] for failure in failures
    ]
    assert all("output" not in row for row in saved["gate_history"])


def test_existing_failure_styles_remain_quotations_not_a_new_check_format(tmp_path):
    failures = [
        "\tFAIL  original spacing\t",
        "AssertionError: expected 409, got 200",
        "ERROR: required value was absent",
        "not ok 7 - value disappeared after restart",
        "✗ the probe returned 500",
        "× the probe omitted its result",
        "comparison FAILED: values differ",
    ]
    ignored = [
        "PASS negative case rejected an ERROR",
        "OK a failed request was handled",
        "INFO failed request details",
        "SKIP assertion failed in a disabled case",
        "10 checks run, 0 failed",
    ]
    output = "\n".join([*failures, *ignored]) + "\n"
    gate = _execute(tmp_path, output)
    assert gate["failure_lines"] == failures
    assert gate["output"] == output
    assert gate["passed"] is False


@pytest.mark.parametrize(
    ("output", "code", "diagnostics"),
    [
        ("", 0, []),
        ("", 1, []),
        ("all probes completed\n", 0, []),
        ("FAIL expected negative case\n", 0, []),
        ("no matchable diagnostic format\n", 3, []),
        ("ERROR: missing input\n", 2, ["ERROR: missing input"]),
    ],
)
def test_exit_code_alone_decides_quiet_error_and_success_cases(
    tmp_path, output, code, diagnostics,
):
    gate = _execute(tmp_path, output, code)
    assert gate["passed"] is (code == 0)
    assert gate["checks"][0]["passed"] is (code == 0)
    assert gate["failure_lines"] == diagnostics
    assert gate["output"] == output
    if not output:
        assert gate["summary"] == f"exit {code}"
    if code:
        assert f"exit {code}" in gate["checks"][0]["detail"]
    comment = replay.gate_evidence_comment(
        SimpleNamespace(), gate, stage="recorded check",
    )
    assert f"Executed check: **{'PASSED' if code == 0 else 'FAILED'}**" in comment
    assert ("Failure diagnostics" in comment) is bool(diagnostics)
    if code == 0:
        assert engine._gate_failure_feedback(gate) == ""


@pytest.mark.parametrize("missing", [True, False])
def test_missing_and_unexecutable_checks_keep_their_original_red_result(
    tmp_path, missing,
):
    check = tmp_path / "unexecutable_check"
    if not missing:
        check.write_text("#!/nonexistent/interpreter\n")
    gate = reviewer.run_gate(str(check), str(tmp_path), "exercise the check")
    before = json.dumps(gate, sort_keys=True)
    comment = replay.gate_evidence_comment(
        SimpleNamespace(), gate, stage="recorded check",
    )
    assert gate["passed"] is False
    assert "Executed check: **FAILED**" in comment
    if missing:
        assert "no validator-authored acceptance check" in gate["summary"]
        assert "output" not in gate
    else:
        assert "exit 126" in gate["checks"][0]["detail"]
        assert "could not execute the authored check" in gate["output"]
    assert json.dumps(gate, sort_keys=True) == before


def test_long_comment_keeps_early_diagnostics_and_has_an_overall_bound(tmp_path):
    failure = "FAIL early result must remain visible"
    gate = _execute(
        tmp_path,
        "\n".join([failure, *("PASS " + "detail " * 40 for _ in range(80))]),
    )
    source = tmp_path / "long_check_source"
    source.write_text(("source excerpt " * 40 + "\n") * 100)
    comment = replay.gate_evidence_comment(
        SimpleNamespace(_acceptance_test_file=str(source)),
        gate, stage="round 1",
        assessment="**Assessment**: Request changes\n" + "review detail\n" * 4000,
    )
    assert failure in comment
    assert "Executed check: **FAILED**" in comment
    assert len(comment) <= 20_000
    assert comment.endswith("[Comment truncated at 20,000 characters.]\n")


def test_replay_can_render_diagnostics_without_loading_execution_or_model_code(
    tmp_path,
):
    failure = "FAIL saved diagnostic is still available"
    gate = _execute(
        tmp_path,
        "\n".join([failure, *("PASS " + "detail " * 40 for _ in range(80))]),
    )
    script = """
import builtins
import json
import sys
from types import SimpleNamespace

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in {
        "engine", "reviewer", "llm", "boto3", "botocore", "strands",
    }:
        raise AssertionError("reporting imported execution or model code: " + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
sys.path.insert(0, sys.argv[1])
import replay
gate = json.loads(sys.stdin.read())
before = json.dumps(gate, sort_keys=True)
print(replay.gate_evidence_comment(SimpleNamespace(), gate, stage="saved result"))
assert json.dumps(gate, sort_keys=True) == before
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(Path(__file__).parent)],
        input=json.dumps(gate), text=True, capture_output=True,
        cwd=tmp_path, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert failure in result.stdout
    assert "Executed check: **FAILED**" in result.stdout
