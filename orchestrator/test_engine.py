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

# Any sentence at all is a valid request now; nothing classifies it.
CONVERT_TASK = "build a small thing and prove it works"


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


def test_happy_path_runs_the_real_gate_and_composes():
    """The MACHINERY, end to end: every routed role produced files, the validator's
    authored check ran as a real subprocess and its real exit code decided, and the
    result composed into a real commit.

    What this deliberately does NOT assert is what the agents produced or what a
    reviewer thought of it. Offline there is no agent and no answer to check, so an
    assertion about content would only be testing the test double, and an assertion
    about the LLM verdict would make the suite depend on a live judge's opinion.
    """
    engine = _engine()
    run = _wait_terminal(engine.submit("Convert the module to an MCP server", ALL_AGENTS))
    result = public_result(run)
    # the gate is one real execution whose exit code decided
    assert result["gate"]["passed"] is True
    assert result["gate"]["checks"] and all(c["passed"] for c in result["gate"]["checks"])
    assert result["gate"]["summary"]
    # every routed role left work behind, and it composed into one real commit
    assert result["composed_branch"] == f"run/{run.run_id}"
    assert result["composed_commit"] and len(result["composed_commit"]) == 40
    # builders first, checker last: the order the join and the run view expect
    # Builders first, checker last, read from the registry rather than pinned as a
    # literal roster: the ORDER is the invariant the join and the run view depend on,
    # while which roles exist is the roster's business (and is configurable).
    import roles as _roles
    assert result["composed_from"] == [
        _roles.get(a).role_name for a in _roles.roster_ids()]
    assert result["composed_from"][-1] == _roles.get(_roles.checker_ids()[0]).role_name
    # locally there is no GitHub MCP Gateway wired: pr_url stays null and the PR step
    # FAILS LOUD (a typed error, never a silent local-commit substitute).
    assert result["pr_url"] is None
    assert result["pr"] and result["pr"].get("error", "").startswith("PR_NO_GATEWAY")
    # local mode invokes no model for the roles: usage is zero, never inferred
    for r in run.progress.values():
        assert r.estimated is False and r.tokens == 0 and r.latency_ms >= 0
    engine.shutdown()


def test_public_diff_carries_whatever_the_roles_wrote():
    """The Changes tab reads a REAL per-file unified diff from this run's own commit.

    The engine names no paths, so this asserts the SHAPE (every routed role's work is
    present, at the paths the role chose, with a real patch) rather than a filename
    the repository would have had to dictate."""
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
    assert paths, "the commit carried no files"
    # EVERY file a role actually wrote is in the commit, at the path the role chose.
    # Asserted against the roles' own trees rather than a directory prefix: the
    # layout is flat now (the roles share one workspace, so a per-role copy
    # published the same project once per role), and this way the test cannot pass
    # by accident when a role's work is silently dropped.
    import os as _os
    import engine as _eng_mod
    for agent_id in run.agents:
        roledir = run.roledir(agent_id)
        if not _os.path.isdir(roledir):
            continue
        for dirpath, dirnames, filenames in _os.walk(roledir):
            dirnames[:] = [d for d in dirnames if d not in _eng_mod._COMPOSE_SKIP_DIRS]
            for fn in filenames:
                rel = _os.path.relpath(_os.path.join(dirpath, fn), roledir)
                if _eng_mod._compose_excluded(rel):
                    continue
                assert rel.replace(_os.sep, "/") in paths, (
                    f"{agent_id} wrote {rel} and the commit does not carry it; "
                    f"paths={sorted(paths)}")
    # every file carries a real unified-diff patch with add counts
    for f in diff["files"]:
        assert f["added"] > 0 and "@@" in f["patch"]
    engine.shutdown()


def test_routing_selects_roles_and_nothing_else():
    """Routing picks ROLES. The request text is the attendee's and is never
    classified, so the same arbitrary sentence runs with whatever roles are named."""
    engine = _engine()
    ODD = "write me a haiku about tuesday and serve it somehow"
    # explicit roles: exactly those roles work (plus nothing else)
    fe = _wait_terminal(engine.submit(ODD, ["opencode", "claude-code-validator"]),
                        timeout_s=120)
    assert fe.agents == ["opencode", "claude-code-validator"]
    assert fe.route["preset"] == "custom" and fe.status == "passed"
    # a preset supplies its own request text and role set
    cli = _wait_terminal(engine.submit("", preset="cli-tool"), timeout_s=120)
    assert cli.agents == ["claude-code", "claude-code-validator"]
    assert cli.route["preset"] == "cli-tool" and cli.task, "preset supplied no task"
    assert cli.status == "passed"
    engine.shutdown()


def test_routing_fails_loud_rather_than_guessing():
    """Never invent a task or a role set: an unknown preset, an unknown role, and
    "neither given" all fail closed."""
    engine = _engine()
    bad = _wait_terminal(engine.submit("anything at all", preset="no/such-preset"),
                         timeout_s=10)
    assert bad.status == "failed" and bad.fail_reason == "UNKNOWN_PRESET:no/such-preset"
    unknown = _wait_terminal(engine.submit("anything", ["claude-code", "nope"]),
                             timeout_s=10)
    assert unknown.status == "failed" and unknown.fail_reason == "UNKNOWN_ROLE:nope"
    none = _wait_terminal(engine.submit("anything at all"), timeout_s=10)
    assert none.status == "failed"
    assert none.fail_reason.startswith("PRESET_NOT_SPECIFIED")
    engine.shutdown()


def test_a_build_always_routes_a_checker():
    """Structural: a builder alone would produce work with no authored acceptance
    check, and with no check the gate is red by definition. Refuse the route."""
    engine = _engine()
    run = _wait_terminal(engine.submit("anything", ["claude-code"]), timeout_s=10)
    assert run.status == "failed"
    assert run.fail_reason.startswith("NO_CHECKER_ROUTED")
    engine.shutdown()


def test_review_preset_judges_an_existing_run():
    """The review preset is read-only: it re-runs the TARGET's own authored check and
    posts an assessment; nothing new is composed."""
    engine = _engine()
    built = _wait_terminal(engine.submit("build any small thing", ALL_AGENTS))
    assert built.status == "passed"
    rev = _wait_terminal(engine.submit("", preset="review-a-run"))
    assert rev.route["preset"] == "review-a-run"
    assert rev.agents == ["claude-code-validator"]
    assert rev._review_target == built.run_id
    assert rev.status == "passed" and rev.review["state"] == "approved"
    assert rev.composed_commit is None  # read-only: no new compose
    engine.shutdown()


def test_terminals_record_real_role_shell_work():
    """Every routed role leaves a real shell transcript with real exit codes.

    Asserts the transcript EXISTS and is honest, not what it contains: the commands a
    role runs follow from what it decided to build, which the engine does not know."""
    engine = _engine()
    run = _wait_terminal(engine.submit("build any small thing", ALL_AGENTS))
    assert set(run.terminals) == {"claude-code", "claude-code-validator", "opencode"}
    for agent_id, lines in run.terminals.items():
        assert lines, f"{agent_id} recorded no shell work"
        assert all(line["exit"] == 0 for line in lines), f"{agent_id} had a failing command"
        # the harness install step names the role's own steering file, which is the
        # one filename that IS part of the contract (it is the role's identity)
        steering = "AGENTS.md" if agent_id == "opencode" else "CLAUDE.md"
        assert any(steering in line["cmd"] for line in lines), (
            f"{agent_id} never installed its steering file")
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
                  roles={"claude-code": "backend-builder"})
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
                  roles={"claude-code": "backend-builder"})
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
    assert unknown.fail_reason.startswith("UNKNOWN_ROLE")
    engine.shutdown()


def test_bounded_iteration_retries_then_passes():
    """A genuinely RED gate (the authored check exits nonzero for real) triggers
    exactly one bounded re-implement pass, then the second round passes.

    The red comes from a real exit code, not from a faked broken endpoint: the whole
    point of the gate is that its verdict is a real execution."""
    engine = _engine()
    run = _wait_terminal(
        engine.submit(CONVERT_TASK, ALL_AGENTS, options={"fail_first_check": True}),
        timeout_s=90,
    )
    assert run.iterations == 2 <= MAX_ITERATIONS
    assert run.status == "passed"
    warns = [e for e in run.events if e["level"] == "warn"]
    assert any("changes requested" in e["message"] for e in warns)
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
    import harness_config

    src = harness_config.harness_file("claude-code")
    backup = src + ".bak"
    shutil.copy(src, backup)
    try:
        with open(src, "a", encoding="utf-8") as f:
            f.write("\n```harness:setup\n"
                    "mcp:\n  - name: github\n    url: https://gw.example/mcp\n"
                    "install:\n  - echo custom-install-ran\n```\n")
        engine = _engine()
        run = _wait_terminal(engine.submit("fix whatever needs fixing", ["claude-code", "claude-code-validator"]))
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
