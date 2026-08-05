"""Engine tests: the deterministic glue is unit-testable without an LLM call.

Covers blueprint order, fail-closed admission, bounded iteration, and the
over-the-wire pytest gate.

    python3 -m pytest orchestrator/test_engine.py -v
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    MAX_ITERATIONS,
    PHASES,
    TERMINAL,
    Engine,
    Run,
    _new_run_id,
    public_diff,
    public_result,
    public_run,
)
import roles  # noqa: E402
from fixture_executor import FixtureExecutor  # noqa: E402

# The served roster: two makers plus the served CHECKER (Kiro today, the Claude Code
# validator on the restore path). `presets.resolve` rejects an unserved id at admission,
# so these must be roles the registry actually serves.
ALL_AGENTS = ["claude-code", "kiro", "opencode"]

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


def test_checker_stays_pending_until_the_builder_finishes():
    """The public run state must describe the graph rather than claim the checker
    is working while it is still behind the builder join."""
    fixture = FixtureExecutor()
    produce = fixture.produce
    builder_started = threading.Event()
    release_builder = threading.Event()

    def blocked_builder(run, agent_id, role):
        if agent_id == "claude-code":
            builder_started.set()
            assert release_builder.wait(timeout=10)
        return produce(run, agent_id, role)

    fixture.produce = blocked_builder
    engine = Engine(executor_obj=fixture)
    run = engine.submit(
        "build a small command line tool",
        ["claude-code", "kiro"],
    )
    try:
        assert builder_started.wait(timeout=10)
        assert run.progress["claude-code"].state == "working"
        checker = run.progress["kiro"]
        assert checker.state == "pending"
        assert "waiting for the selected builders" in checker.note
    finally:
        release_builder.set()
        _wait_terminal(run)
        engine.shutdown()


def test_run_ids_do_not_collide_across_coordinator_instances():
    run_ids = {_new_run_id() for _ in range(100)}
    assert len(run_ids) == 100
    assert all(len(run_id.rsplit("_", 1)[-1]) == 12 for run_id in run_ids)


def test_happy_path_runs_the_real_gate_and_composes(monkeypatch):
    """The MACHINERY, end to end: every routed role produced files, the validator's
    authored check ran as a real subprocess and its real exit code decided, and the
    result composed into a real commit.

    What this deliberately does NOT assert is what the agents produced or what a
    reviewer thought of it. Offline there is no agent and no answer to check, so an
    assertion about content would only be testing the test double, and an assertion
    about the LLM verdict would make the suite depend on a live judge's opinion.
    """
    task = "exercise the engine happy path without grading fixture content"
    review_calls = []
    real_assess = __import__("reviewer").assess

    def counted_assess(*args, **kwargs):
        if args[0].task == task:
            review_calls.append(args[1].get("summary"))
        return real_assess(*args, **kwargs)

    monkeypatch.setattr("engine.reviewer.assess", counted_assess)
    engine = _engine()
    run = _wait_terminal(engine.submit(task, ALL_AGENTS))
    result = public_result(run)
    assert result["phase"] == run.phase
    assert {r["agent"] for r in result["progress"]} == set(run.agents)
    assert all(r["state"] == "done" for r in result["progress"])
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
    # Each pull request is checked and reviewed ON ITS OWN. Nothing waits in line, so
    # a builder whose work is clean spends exactly ONE turn: the extra owner turn the
    # old merge queue forced on every downstream role is gone.
    builders = [
        item for item in run.work_items.values()
        if item.kind == "builder"
    ]
    assert all(item.attempt == 1 for item in builders)
    # One executable per pull request, and one review per pull request. Each gate row
    # names the work id it judged, which is what makes the evidence attributable.
    assert len(run.gate_history) == len(builders)
    assert {row["work_id"] for row in run.gate_history} == {
        item.work_id for item in builders}
    assert len(review_calls) == len(builders), [
        (row["stage"], row["work_id"]) for row in run.gate_history
    ]
    # Every pull request settled, and every one of them targeted the DEFAULT branch.
    assert run.final_base_branch
    assert all(item.base_branch == run.final_base_branch for item in builders)
    assert len(run.role_prs) == len(builders)
    assert all(row["state"] in ("merged", "awaiting_review")
               for row in run.role_prs), run.role_prs
    engine.shutdown()


def test_each_pull_request_is_checked_and_merged_independently(monkeypatch):
    """One pull request per role, each gated and merged ON ITS OWN.

    Replaces a test that pinned the deleted merge queue (positions, one combined
    candidate gate, then a re-gate after every merge). What survives from it, and is
    asserted here, is the part that was ever a real guarantee: every role gets its own
    work id, its own executable check attributed to that work id, and its own merge --
    with no ordering imposed between the verdicts.
    """
    review_calls = []
    real_assess = __import__("reviewer").assess

    def counted_assess(*args, **kwargs):
        review_calls.append(kwargs.get("subject") or (args[3] if len(args) > 3 else None))
        return real_assess(*args, **kwargs)

    monkeypatch.setattr("engine.reviewer.assess", counted_assess)
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    engine = Engine(executor_obj=FixtureExecutor())
    run = _wait_terminal(
        engine.submit("build any useful tool", ALL_AGENTS), timeout_s=120)
    builders = [
        item for item in run.work_items.values()
        if item.kind == "builder"
    ]

    assert run.status == "passed"
    assert len({item.work_id for item in builders}) == len(builders)
    # Every pull request based on the DEFAULT branch -- never a run-scoped branch.
    assert run.final_base_branch
    assert all(item.base_branch == run.final_base_branch for item in builders)
    # Each one merged on its own, and each has exactly one row of its own.
    assert all(item.merge_state == "merged" for item in builders)
    assert len(run.role_prs) == len(builders)
    assert [row["state"] for row in run.role_prs] == ["merged"] * len(builders)
    # ONE executable per pull request (not one combined gate plus a re-gate per
    # merge), every row green and attributed to the work id it judged.
    assert len(run.gate_history) == len(builders)
    assert all(row["passed"] for row in run.gate_history)
    assert {row["work_id"] for row in run.gate_history} == {
        item.work_id for item in builders}
    # ONE review per pull request, each handed its own subject.
    assert len(review_calls) == len(builders)
    assert {getattr(subject, "work_id", None) for subject in review_calls} == {
        item.work_id for item in builders}
    engine.shutdown()


def test_a_red_pull_request_does_not_block_a_green_sibling(monkeypatch):
    """Independence is the whole point: one role's red verdict is not another's.

    Under the old merge queue a blocked head stopped everything behind it. Now a
    pull request that cannot become acceptable is recorded and the loop CONTINUES,
    so a sibling that passed still settles, and the run reports which one is open.
    """
    import reviewer as _reviewer
    real_assess = _reviewer.assess
    engine = Engine(executor_obj=FixtureExecutor())
    victim: dict[str, str] = {}

    def reject_one_role(run, gate, round_no, *args, **kwargs):
        subject = kwargs.get("subject") or (args[1] if len(args) > 1 else None)
        work_id = getattr(subject, "work_id", "")
        # Always reject the SAME pull request, including on its bounded repair, so it
        # really reaches a terminal blocked state while its sibling passes.
        if work_id and (not victim or victim.get("work_id") == work_id):
            victim.setdefault("work_id", work_id)
            return _reviewer.Verdict(
                state="changes_requested", gate=gate, round=round_no,
                reasons=["this pull request is not acceptable"],
                assessment="**Assessment**: Request changes\n\nheld for the test",
            )
        return real_assess(run, gate, round_no, *args, **kwargs)

    monkeypatch.setattr("engine.reviewer.assess", reject_one_role)
    monkeypatch.setenv("WORKSHOP_MERGE_POLICY", "auto")
    run = _wait_terminal(
        engine.submit("build any useful tool", ALL_AGENTS), timeout_s=180)
    builders = [
        item for item in run.work_items.values()
        if item.kind == "builder"
    ]
    if len(builders) < 2:  # a single-builder roster cannot show independence
        engine.shutdown()
        return

    blocked = [row for row in run.role_prs if row["state"] == "blocked"]
    settled = [row for row in run.role_prs
               if row["state"] in ("merged", "awaiting_review")]
    assert blocked, run.role_prs
    assert settled, "a red pull request must not stop a green sibling"
    # The run is needs_human, and next_action NAMES the work id still open, because
    # "some merged, one red" is a real terminal shape a person has to act on.
    assert run.status == "needs_human"
    assert run.fail_reason.startswith("ROLE_PR_BLOCKED")
    assert blocked[0]["work_id"] in run.fail_reason
    assert public_result(run)["next_action"]
    # The blocked pull request used its ONE bounded repair and no more.
    owner = next(item for item in builders
                 if item.work_id == blocked[0]["work_id"])
    assert 1 <= owner.attempt <= MAX_ITERATIONS
    engine.shutdown()


def test_review_outage_holds_the_pull_request_without_rebuilding(monkeypatch):
    """A reviewer outage is infrastructure, not a reason to rewrite green work.

    It holds the affected pull request open and asks NO builder to change code: a
    model outage is not a defect in the work, and sending a builder back for one
    would burn its single bounded repair on nothing.
    """
    import reviewer

    def unavailable(_run, gate, round_no, *_args, **_kwargs):
        return reviewer.Verdict(
            state="changes_requested",
            gate=gate,
            round=round_no,
            reasons=["integrated: review unavailable"],
            panels=[{
                "name": "integrated",
                "label": "Integrated review",
                "state": "abstained",
                "model": "test-model",
                "reasons": [],
                "assessment": "",
                "note": "model invocation unavailable",
            }],
            review_unavailable=True,
        )

    monkeypatch.setattr("engine.reviewer.assess", unavailable)
    engine = _engine()
    run = _wait_terminal(
        engine.submit("build any useful tool", ALL_AGENTS), timeout_s=120)

    assert run.status == "needs_human"
    assert run.fail_reason.startswith("REVIEW_UNAVAILABLE")
    # Nothing merged, and no builder was sent back: every builder spent exactly the
    # one turn it was given.
    assert not [row for row in run.role_prs if row["state"] == "merged"]
    assert all(
        item.attempt == 1
        for item in run.work_items.values()
        if item.kind == "builder"
    )
    # The affected pull requests are NAMED, so a person knows which ones to look at.
    blocked = [row for row in run.role_prs if row["state"] == "blocked"]
    assert blocked and all(row["error"] == "REVIEW_UNAVAILABLE" for row in blocked)
    assert "retry" in public_result(run)["next_action"].lower()
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
    fe = _wait_terminal(engine.submit(ODD, ["opencode", "kiro"]),
                        timeout_s=120)
    assert fe.agents == ["opencode", "kiro"]
    assert fe.route["preset"] == "custom" and fe.status == "passed"
    # a preset supplies its own request text and role set
    cli = _wait_terminal(engine.submit("", preset="cli-tool"), timeout_s=120)
    assert cli.agents == ["claude-code", "kiro"]
    assert cli.route["preset"] == "cli-tool" and cli.task, "preset supplied no task"
    assert cli.status == "passed"
    engine.shutdown()


def test_routing_fails_loud_rather_than_guessing():
    """Never invent a ROLE that does not exist: an unknown preset and an unknown role
    both fail closed, with the offending name in the reason.

    A request with no preset and no named roles is NOT a failure any more: it is the
    normal case, and the model routes it (see
    test_a_typed_request_is_routed_not_handed_the_whole_roster). What must never
    happen is a nearest-match guess at a name nobody registered.
    """
    engine = _engine()
    bad = _wait_terminal(engine.submit("anything at all", preset="no/such-preset"),
                         timeout_s=10)
    assert bad.status == "failed" and bad.fail_reason == "UNKNOWN_PRESET:no/such-preset"
    unknown = _wait_terminal(engine.submit("anything", ["claude-code", "nope"]),
                             timeout_s=10)
    assert unknown.status == "failed" and unknown.fail_reason == "UNKNOWN_ROLE:nope"
    engine.shutdown()


def test_a_typed_request_is_routed_not_handed_the_whole_roster(monkeypatch):
    """A request with no preset is ROUTED by the model, and an empty one is refused.

    Routing every role unconditionally is what this replaced: it dispatched a frontend
    builder for a command line tool, which is the same defect a keyword table has.
    """
    import integration_plan
    import presets
    import pytest
    import roles as _roles

    monkeypatch.setattr(
        integration_plan, "select_capabilities",
        lambda task, available, **kw: ([available[0]], "one kind of work"))
    route = presets.resolve(task="write a command line tool that counts words")
    assert route.preset == "routed"
    assert len(route.agents) < len(_roles.roster_ids()), route.agents
    assert route.rule, "the run must record WHY these roles"

    # Nothing to route is still a refusal: ask, never invent a task.
    with pytest.raises(presets.RouteError) as excinfo:
        presets.resolve(task="")
    assert "EMPTY_TASK" in str(excinfo.value)


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
    assert rev.agents == ["kiro"]
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
    assert set(run.terminals) == {"claude-code", "kiro", "opencode"}
    for agent_id, lines in run.terminals.items():
        assert lines, f"{agent_id} recorded no shell work"
        assert all(line["exit"] == 0 for line in lines), f"{agent_id} had a failing command"
        # the harness install step names the role's own steering file, which is the
        # one filename that IS part of the contract (it is the role's identity).
        # Read from the registry, not a per-id literal: each role declares its own
        # native steering filename and a roster swap must not need an edit here.
        steering = os.path.basename(roles.get(agent_id).steering_file)
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
    assert run.status == "passed"
    builders = [
        item for item in run.work_items.values()
        if item.kind == "builder"
    ]
    # The bound is PER PULL REQUEST. Every builder gets at most MAX_ITERATIONS turns
    # (its first, plus at most one bounded repair), so N builders allow at most N
    # repair rounds -- one each, never re-entrant. This is the property that keeps a
    # red gate from becoming a runaway, so assert the CEILING, not a sequence: which
    # pull request needed its repair depends on what the agents actually wrote.
    assert builders, "the preset must route builders for this to mean anything"
    for item in builders:
        assert 1 <= item.attempt <= MAX_ITERATIONS, (
            f"{item.work_id} spent {item.attempt} turns; the per-PR bound is "
            f"{MAX_ITERATIONS}")
    assert any(item.attempt > 1 for item in builders), (
        "a red gate must really have sent one pull request back")
    assert run.iterations <= len(builders) * MAX_ITERATIONS
    # Every pull request still settled, and every recorded check is attributed.
    assert all(row["state"] in ("merged", "awaiting_review")
               for row in run.role_prs), run.role_prs
    assert all(row["work_id"] for row in run.gate_history)
    warns = [e for e in run.events if e["level"] == "warn"]
    assert any("bounded repair" in e["message"] for e in warns), [
        e["message"] for e in warns]
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
        run = _wait_terminal(engine.submit("fix whatever needs fixing", ["claude-code", "kiro"]))
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
    assert engine._role_model(run, "kiro", "auto") == "claude-sonnet-4-6"

    # a per-task options model still overrides the env-wired default
    run.options = {"models": {"claude-code": "claude-opus-4-6"}}
    assert engine._role_model(run, "claude-code", "claude-opus-4-6") == "claude-opus-4-6"
    engine.shutdown()
