"""Engine tests: the deterministic glue is unit-testable without an LLM call.

Covers blueprint order, fail-closed admission, bounded iteration, and the
over-the-wire pytest gate.

    python3 -m pytest orchestrator/test_engine.py -v
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    MAX_ITERATIONS,
    PHASES,
    TERMINAL,
    Engine,
    Run,
    public_diff,
    public_result,
    public_run,
)
from fixture_executor import FixtureExecutor  # noqa: E402

ALL_AGENTS = ["claude-code", "claude-code-validator", "opencode"]

# A task the router actually classifies (convert intent). The router is now
# task-agnostic: a filler like "task" matches no intent and fails loud, so tests
# that only need a VALID task (they exercise phases/iteration/admission, not
# routing) use this.
CONVERT_TASK = "convert the cost analyzer module to an MCP server and chatbot UI"


def _engine(**kw) -> Engine:
    """A deterministic engine for the tests: the test-only FixtureExecutor produces
    artifacts via the builders (no model, no live AWS), while the gate / reviewer /
    compose / PR tail runs for real. Only the artifact producer is swapped, injected
    by constructor, never via an env flag on the shipped binary."""
    return Engine(executor_obj=FixtureExecutor(), **kw)


def _wait_terminal(run, timeout_s: float = 60.0):
    deadline = time.monotonic() + timeout_s
    while run.status not in TERMINAL:
        assert time.monotonic() < deadline, f"run stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return run


def test_happy_path_passes_real_gate():
    engine = _engine()
    run = _wait_terminal(engine.submit("Convert the module to an MCP server", ALL_AGENTS))
    result = public_result(run)
    assert result["status"] == "passed"
    assert result["gate"]["passed"] is True
    # the gate's three checks all ran against the live endpoint
    assert {c["check"] for c in result["gate"]["checks"]} == {
        "tool_discovery", "tool_correctness", "input_validation"}
    assert all(c["passed"] for c in result["gate"]["checks"])
    # locally there is no GitHub MCP Gateway wired: pr_url stays null and the PR
    # step FAILS LOUD (a typed error, never a silent local-commit substitute).
    # The composed branch still exists locally; it is just not published.
    assert result["pr_url"] is None
    assert result["pr"] and result["pr"].get("error", "").startswith("PR_NO_GATEWAY")
    assert result["composed_branch"] == f"run/{run.run_id}"
    assert result["composed_commit"] and len(result["composed_commit"]) == 40
    assert result["composed_from"] == ["backend-mcp", "validator", "frontend-builder"]
    assert result["iterations"] == 1
    assert result["artifact_endpoint"].startswith("http://127.0.0.1:")
    # the separate review orchestrator approved with the exact pass token
    assert result["review"]["state"] == "approved" and result["review"]["lgtm"] is True
    # local mode invokes no model: usage is zero, never inferred
    for r in run.progress.values():
        assert r.estimated is False and r.tokens == 0 and r.latency_ms >= 0
    engine.shutdown()


def test_public_diff_is_the_real_composed_change():
    """The session Changes tab reads a REAL per-file unified diff from this run's
    own commit in the composed repo, not a reconstruction. A pre-compose run has
    no commit yet, so files is empty with an honest reason."""
    engine = _engine()

    pending = engine.submit("Convert the module to an MCP server", ALL_AGENTS)
    early = public_diff(pending)  # may race to terminal, but the shape holds either way
    assert early["run_id"] == pending.run_id
    assert "files" in early

    run = _wait_terminal(pending)
    diff = public_diff(run)
    assert diff["commit"] == run.composed_commit
    assert diff["branch"] == f"run/{run.run_id}"
    paths = {f["path"] for f in diff["files"]}
    # the real deliverable the gate graded + the reviewer's artifacts, all under deliverable/
    assert "deliverable/mcp_server.py" in paths
    assert "deliverable/gate_report.json" in paths
    assert any(p.startswith("deliverable/") for p in paths)
    # every file carries a real unified-diff patch with add counts
    server = next(f for f in diff["files"] if f["path"] == "deliverable/mcp_server.py")
    assert server["added"] and server["added"] > 0
    assert "@@" in server["patch"] and "def " in server["patch"]
    engine.shutdown()


def test_router_dispatches_only_the_routed_roles():
    """The router (registry + explicit-intent rule + complexity check) decides
    the dispatch list: agents omitted on submit, never a fixed fan-out."""
    engine = _engine()
    # explicit agent intent: "use codex" -> frontend role only
    fe = _wait_terminal(engine.submit("use codex to refresh the chatbot examples"))
    assert fe.route["workflow_ref"] == "patch/frontend-v1"
    assert fe.agents == ["opencode"] and fe.status == "passed"
    # patch-sized request -> backend only (complexity check: SIMPLE)
    be = _wait_terminal(engine.submit("fix the server version string"))
    assert be.route["workflow_ref"] == "patch/backend-v1"
    assert be.agents == ["claude-code"] and be.status == "passed"
    # full-stack intent -> Critter Lab usecase, all three roles
    fs = _wait_terminal(engine.submit("Build the full-stack Critter Lab app"))
    assert fs.route["workflow_ref"] == "build/fullstack-v1"
    assert fs.usecase == "critter-lab" and fs.agents == ALL_AGENTS
    assert fs.status == "passed"
    engine.shutdown()


def test_unknown_workflow_ref_fails_loud():
    """The fail-closed rule: an unknown workflow_ref is rejected, not guessed."""
    engine = _engine()
    run = _wait_terminal(engine.submit("task", workflow_ref="no/such-workflow-v9"),
                         timeout_s=10)
    assert run.status == "failed"
    assert run.fail_reason == "UNKNOWN_WORKFLOW:no/such-workflow-v9"
    engine.shutdown()


def test_review_workflow_judges_an_existing_run():
    """review/pr-v1 is read-only: it re-serves the target run's artifact, the
    review orchestrator judges it, and nothing new is composed."""
    engine = _engine()
    built = _wait_terminal(engine.submit("Convert the module to an MCP server"))
    assert built.status == "passed"
    rev = _wait_terminal(engine.submit("review the PR from the last run"))
    assert rev.route["workflow_ref"] == "review/pr-v1"
    assert rev.agents == ["claude-code-validator"]
    assert rev._review_target == built.run_id
    assert rev.status == "passed" and rev.review["state"] == "approved"
    assert rev.composed_commit is None  # read-only: no new compose
    engine.shutdown()


def test_terminals_record_real_role_shell_work():
    """Every role leaves a shell transcript: harness self-install (cp the
    steering file), module probes, pytest, with exit codes."""
    engine = _engine()
    run = _wait_terminal(engine.submit("Convert the module to an MCP server"))
    assert set(run.terminals) == {"claude-code", "claude-code-validator", "opencode"}
    backend = run.terminals["claude-code"]
    assert any("CLAUDE.md" in line["cmd"] for line in backend)        # harness install
    assert any("mcp_server.py" in line["cmd"] for line in backend)    # artifact probe
    assert all(line["exit"] == 0 for line in backend)
    validator = run.terminals["claude-code-validator"]
    assert any("pytest" in line["cmd"] for line in validator)         # the real gate
    assert any("passed" in line["output"] for line in validator)
    engine.shutdown()


def test_agent_terminal_is_runtime_session_only_on_shipped_path():
    """On the shipped (agentcore) path the per-agent terminal must show ONLY the
    agent's real Runtime session; the engine's host-side plumbing (harness staging
    ``cp``, module probes, the gate) is recorded under a separate ``orchestrator``
    lane, never mixed into the agent tab. The test-only fixture executor keeps that
    plumbing under the agent (it has no runtime session), so both contracts hold.

    Exercised directly on ``Run.term`` (no live runtime needed): the lane is chosen
    by ``_executor_name``, the same value ``submit`` stamps from the executor."""
    # Shipped path: host plumbing goes to the orchestrator lane, NOT the agent tab.
    shipped = Run(run_id="run_000000_001", task="t", agents=["claude-code"],
                  roles={"claude-code": "backend-mcp"})
    shipped._executor_name = "agentcore"
    out = shipped.term("claude-code", "echo staged-harness")
    assert out.strip() == "staged-harness"        # the command still really runs
    assert "claude-code" not in shipped.terminals, \
        "host staging must not appear in the agent's runtime-session tab"
    assert "orchestrator" in shipped.terminals
    assert any("staged-harness" in e["output"] for e in shipped.terminals["orchestrator"])

    # Test/offline path: no runtime session exists, so plumbing stays under the
    # agent (the offline tests' terminal contract is unchanged).
    offline = Run(run_id="run_000000_002", task="t", agents=["claude-code"],
                  roles={"claude-code": "backend-mcp"})
    offline._executor_name = "fixture"
    offline.term("claude-code", "echo staged")
    assert "claude-code" in offline.terminals
    assert "orchestrator" not in offline.terminals


def test_blueprint_phase_order_in_journal():
    engine = _engine()
    run = _wait_terminal(engine.submit(CONVERT_TASK, ALL_AGENTS))
    seen = [e["phase"] for e in run.events]
    # journal phases appear in blueprint order (dedup preserving order)
    ordered = list(dict.fromkeys(seen))
    assert ordered == [p for p in PHASES if p in ordered]
    assert ordered[0] == "admission" and ordered[-1] == "finalization"
    engine.shutdown()


def test_admission_fail_closed():
    engine = _engine()
    empty = _wait_terminal(engine.submit("   ", ALL_AGENTS), timeout_s=10)
    assert (empty.status, empty.fail_reason) == ("failed", "EMPTY_TASK")
    unknown = _wait_terminal(engine.submit(CONVERT_TASK, ["claude-code", "nope"]), timeout_s=10)
    assert unknown.status == "failed"
    assert unknown.fail_reason.startswith("UNKNOWN_AGENT")
    engine.shutdown()


def test_bounded_iteration_retries_then_passes():
    engine = _engine()
    run = _wait_terminal(
        engine.submit(CONVERT_TASK, ALL_AGENTS, options={"inject_failure": True}),
        timeout_s=90,
    )
    # round 1 review red (sabotaged endpoint) -> one bounded re-implement pass
    # (the one-bounded-pass rule) -> round 2 approved
    assert run.iterations == 2 <= MAX_ITERATIONS
    assert run.status == "passed"
    warns = [e for e in run.events if e["level"] == "warn"]
    assert any("review requested changes" in e["message"] for e in warns)
    engine.shutdown()


def test_run_view_matches_frozen_contract():
    engine = _engine()
    run = _wait_terminal(engine.submit(CONVERT_TASK, ALL_AGENTS))
    view = public_run(run)
    # frozen fields + the additive "route" and "fail_reason" (API_CONTRACT.md
    # "Engine additions"). fail_reason lets the console state WHY a run stopped
    # (e.g. RUNTIME_NOT_WIRED:<role>) instead of a bare status: a fail-loud
    # verdict must be legible, never look like a silent mock.
    assert set(view) == {"run_id", "task", "status", "phase",
                         "created_at", "agents", "roles", "route", "fail_reason"}
    engine.shutdown()


def test_harness_setup_block_extends_a_role():
    """The harness is freely extensible: an optional ``harness:setup`` block
    (MCP servers, extra skills, install commands) is applied in the role's real
    terminal during harness install: the file IS the configuration."""
    import shutil
    import builders

    src = builders.harness_file("claude-code", "sample-to-mcp")
    backup = src + ".bak"
    shutil.copy(src, backup)
    try:
        with open(src, "a", encoding="utf-8") as f:
            f.write("\n```harness:setup\n"
                    "mcp:\n  - name: github\n    url: https://gw.example/mcp\n"
                    "install:\n  - echo custom-install-ran\n```\n")
        engine = _engine()
        run = _wait_terminal(engine.submit("fix the server version string"))
        assert run.status == "passed"
        lines = run.terminals["claude-code"]
        assert any("mcp server github registered" in line["output"] for line in lines)
        assert any("custom-install-ran" in line["output"] for line in lines)
        engine.shutdown()
    finally:
        shutil.move(backup, src)


def test_per_task_model_override_resolves():
    """options.models[agent] overrides the roster default through the alias map
    (a per-task model selector); unknown aliases pass through unchanged."""
    import llm

    engine = _engine()
    run = Run(run_id="run_000000_001", task="t", agents=[], roles={})
    run.options = {"models": {"claude-code": "claude-sonnet-4-6"}}
    assert engine._role_model(run, "claude-code", "claude-opus-4-6") == "claude-sonnet-4-6"
    assert llm.resolve("claude-sonnet-4-6") == "us.anthropic.claude-sonnet-4-6"
    # no override -> roster default; a full Bedrock id passes through resolve()
    run.options = {}
    assert engine._role_model(run, "opencode", "amazon-bedrock/us.anthropic.claude-sonnet-4-6") == "amazon-bedrock/us.anthropic.claude-sonnet-4-6"
    assert llm.resolve("openai.gpt-5.5") == "openai.gpt-5.5"
    engine.shutdown()


def test_role_model_env_override_wires_deploy_time_default(monkeypatch):
    """The roster default is wirable at deploy time for accounts lacking a model:
    WORKSHOP_MODEL_<AGENT> beats generic WORKSHOP_MODEL beats the baked default,
    and a per-task options model still overrides all of them."""
    engine = _engine()
    run = Run(run_id="run_000000_002", task="t", agents=[], roles={})
    run.options = {}

    # generic env override retargets the baked default
    monkeypatch.setenv("WORKSHOP_MODEL", "claude-sonnet-4-6")
    assert engine._role_model(run, "claude-code", "claude-opus-4-6") == "claude-sonnet-4-6"

    # agent-specific env override wins over the generic one (dashes -> underscores)
    monkeypatch.setenv("WORKSHOP_MODEL_CLAUDE_CODE", "us.anthropic.claude-sonnet-4-6")
    assert engine._role_model(run, "claude-code", "claude-opus-4-6") == "us.anthropic.claude-sonnet-4-6"
    # a different agent is unaffected by the claude-code-specific var
    assert engine._role_model(run, "claude-code-validator", "auto") == "claude-sonnet-4-6"

    # a per-task options model still overrides the env-wired default
    run.options = {"models": {"claude-code": "claude-opus-4-6"}}
    assert engine._role_model(run, "claude-code", "claude-opus-4-6") == "claude-opus-4-6"
    engine.shutdown()
