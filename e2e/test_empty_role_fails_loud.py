"""A role that writes NOTHING must fail the run, never reach a green gate.

This is the fail-loud guard that replaced the old named-artifact check. When the
engine stopped naming the deliverable's files, "the file is missing" stopped being
a signal, so `_require_work` counts the role's tree instead.

It was dead code. `install_harness` copies the role's steering file (and any
`harness:setup` skills) INTO the same directory before the CLI runs, so by the
time a role finished the tree already held at least `CLAUDE.md` and a
`skills/.../SKILL.md`. `n == 0` could not happen, and a role whose CLI exited 0
having written nothing was reported as a builder that "built" something. With no
builder in the repository to fall back on, that is the worst possible failure
mode: a green gate over an empty deliverable.

`_authored_count` therefore excludes what the harness staged, so the guard counts
only what the ROLE wrote.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import time

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))


def _wait_terminal(run, timeout_s: float = 90.0):
    deadline = time.monotonic() + timeout_s
    while run.status not in ("passed", "failed", "needs_human"):
        assert time.monotonic() < deadline, f"run stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return run


@pytest.fixture()
def engine_mod():
    return importlib.import_module("engine")


def test_harness_files_alone_are_not_work(engine_mod):
    """The steering + skills the harness installs must not count as the role's work."""
    harness_config = importlib.import_module("harness_config")
    run = engine_mod.Run(run_id="run_000000_931", task="t",
                         agents=["claude-code"], roles=["claude-code"])
    roledir = run.roledir("claude-code")
    os.makedirs(roledir, exist_ok=True)
    try:
        src = harness_config.harness_file("claude-code")
        shutil.copy(src, os.path.join(
            roledir, harness_config.steering_filename("claude-code")))
        for skill in harness_config.parse_setup_spec(src).get("skills", []):
            full = os.path.join(os.path.dirname(src), skill)
            if os.path.isdir(full):
                shutil.copytree(full,
                                os.path.join(roledir, "skills",
                                             os.path.basename(full.rstrip("/"))),
                                dirs_exist_ok=True)

        eng = engine_mod.Engine.__new__(engine_mod.Engine)   # no threads needed
        assert os.listdir(roledir), "precondition: the harness did stage files"
        assert eng._authored_count(run, "claude-code") == 0, (
            "harness-installed steering/skills are being counted as the role's own "
            "work, which makes the empty-tree guard unreachable")

        with open(os.path.join(roledir, "service.py"), "w", encoding="utf-8") as f:
            f.write("# the role's actual work\n")
        assert eng._authored_count(run, "claude-code") == 1
    finally:
        shutil.rmtree(os.path.dirname(roledir), ignore_errors=True)


def test_a_role_that_writes_nothing_never_passes(engine_mod):
    """Drive a REAL run whose builder produces no files: it must not pass.

    The validator still authors and runs a real check, so this proves the failure
    comes from the empty builder rather than from a missing gate.
    """
    fixture_executor = importlib.import_module("fixture_executor")

    class SilentBuilder(fixture_executor.FixtureExecutor):
        """Exits 0 and writes nothing for builders; the checker behaves normally.

        Which role CHECKS comes from the registry, not from the spelling of its id: an
        id-suffix guess silenced the checker too on a roster whose validator is not
        named "...validator", so the run failed for the wrong reason and stopped
        proving anything about the empty-builder guard.
        """

        def produce(self, run, agent_id, role):
            if fixture_executor._is_checker(agent_id, role):
                return super().produce(run, agent_id, role)
            run._offline_double = True
            os.makedirs(run.roledir(agent_id), exist_ok=True)
            return None

    eng = engine_mod.Engine(executor_obj=SilentBuilder())
    try:
        run = _wait_terminal(eng.submit(
            "Build a small service that converts between units",
            ["claude-code", "kiro"]))
        assert run.status != "passed", (
            "a builder wrote no files, yet the run passed: the empty-tree guard is "
            f"not firing (gate={run.gate}, fail_reason={run.fail_reason})")
        assert run.fail_reason == "ROLE_EXECUTION_ERROR", run.fail_reason
        # No gate verdict at all: the run never reached one, so there is no green
        # result to mistake for a pass over an empty deliverable.
        assert not run.gate, run.gate
        # The role's own event stream must say why, so the terminal shows it.
        blame = " ".join(str(e) for e in (run.role_events or []))
        assert "wrote no files" in blame or "claude-code" in blame, blame
    finally:
        eng.shutdown()
