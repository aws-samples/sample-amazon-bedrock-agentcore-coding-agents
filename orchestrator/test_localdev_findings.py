"""Three defects a real local-dev run exposed that no fixture had.

Every one of these was invisible offline because the test double's check prints a
single tidy line and the fixture path never opens a PR. A live run of the real Claude
Code CLI against Bedrock produced output shaped the way a human writes shell scripts,
and the gaps showed up immediately.

Run: 2 roles, real CLIs, 134s, gate green, `run_062210_001`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import reviewer  # noqa: E402


# ------------------------------------------------- 1. the gate summary was a rule

def test_a_check_that_underlines_its_result_is_not_summarised_as_the_underline():
    """The live failure: gate summary came back as `===============================`.

    The most natural way a shell check ends is a results line with a rule under it.
    Taking the literal last line therefore quoted the check's own punctuation as the
    verdict -- on the PR body, in `run_status`, and in the ledger.
    """
    out = ("ok: create an issue\n"
           "ok: persistence across restart\n"
           "\n"
           "===============================\n"
           "Results: 12 passed, 0 failed\n"
           "===============================\n")
    assert reviewer._summary_line(out, 0) == "Results: 12 passed, 0 failed"


def test_a_plain_last_line_is_still_the_summary():
    assert reviewer._summary_line("probe 1 ok\nAll 7 probes passed", 0) == \
        "All 7 probes passed"


def test_trailing_blank_lines_are_skipped():
    assert reviewer._summary_line("Results: 3 passed\n\n\n", 0) == "Results: 3 passed"


def test_output_that_is_nothing_but_dividers_invents_no_verdict():
    """Degrade to the raw line; never fabricate a result the check did not print."""
    assert reviewer._summary_line("=====\n-----", 0) == "-----"
    assert reviewer._summary_line("", 3) == "exit 3"


def test_the_gate_uses_it():
    src = open(reviewer.__file__, encoding="utf-8").read()
    assert "_summary_line(out, code)" in src, (
        "run_gate does not use the helper, so the gate summary is still the raw "
        "last line")


# ------------------------------- 2. a passing run had no next action at all

def test_a_passing_run_that_opened_a_pr_says_where_to_look():
    """The most common outcome of all, and it returned "" before this."""
    action = engine.next_action("passed", None, {"pr_url": "https://x/pull/1"},
                                "https://x/pull/1")
    assert action, "a passing run offered no next action"
    assert "pull request" in action.lower()


def test_a_passing_run_whose_pr_never_opened_says_so():
    """The build succeeded and the deliverable is stranded on a local branch.

    This is NOT a fail_reason -- the build genuinely passed -- so it is readable only
    from the PR result. Nothing in the payload flagged it at a glance before.
    """
    pr = {"error": "PR_NO_GATEWAY: no GitHub MCP Gateway wired. Deploy the gateway..."}
    action = engine.next_action("passed", None, pr, None)
    assert "doctor" in action, action
    assert "local branch" in action or "no PR" in action, action


def test_a_rejected_credential_on_a_passing_run_points_at_the_credential():
    pr = {"error": "PR_NO_CREDENTIAL: nothing resolved"}
    assert "deploy-credential.sh" in engine.next_action("passed", None, pr, None)


def test_an_unexpected_pr_error_still_produces_advice():
    pr = {"error": "gateway put_file failed for server.py: HTTP Error 500"}
    action = engine.next_action("passed", None, pr, None)
    assert "doctor" in action and "500" in action, action


def test_a_passing_run_with_no_pr_attempted_stays_quiet():
    """A read-only review run composes nothing; there is nothing to advise."""
    assert engine.next_action("passed", None, {}, None) == ""


# ------------------- 3. the dead branch that made the above look covered

def test_pr_no_gateway_is_not_treated_as_a_failure_reason():
    """It never IS one. The engine keeps `passed` and records the error on run.pr.

    A branch for it on the failure path was unreachable, and it made the real gap
    (a PASSING run with no PR) look already handled. My own earlier test asserted
    that unreachable combination, which is how the gap survived.
    """
    src = open(engine.__file__, encoding="utf-8").read()
    assert 'fail_reason = "PR_NO_GATEWAY' not in src
    assert 'fail_reason, "PR_NO_GATEWAY' not in src
    # And the advice must come from the passing arm, not the reason table.
    assert "PR_NO_GATEWAY" not in engine._NEXT_ACTION


def test_public_result_carries_the_pr_aware_action():
    run = engine.Run(run_id="run_000000_701", task="t", agents=[], roles={})
    run.status = "passed"
    run.pr = {"error": "PR_NO_GATEWAY: nope"}
    payload = engine.public_result(run)
    assert "doctor" in payload["next_action"], payload["next_action"]


# ------------- 4. the local seam reported exit 0 for a CLI that died

def test_the_local_dev_seam_reports_the_cli_s_real_exit_code():
    """The worst of the four: a FAILED role was marked `done`.

    opencode died on missing SigV4 credentials (exit 1, wrote nothing), but the local
    dev seam hardcoded exit 0, which disabled the engine's exit-code guard for the
    entire local path. The only remaining check was "the tree is not empty" -- and
    every role shares ONE workspace, so a teammate's files satisfied it.
    """
    import runtime_exec  # noqa: PLC0415
    transcript = ("[opencode] local-dev dispatch: running the real CLI\n"
                  "Error: AWS SigV4 authentication requires AWS credentials.\n"
                  "[opencode] exit 1; wrote 2 file(s): AGENTS.md, CLAUDE.md\n"
                  "__DEV_RUNTIME_EXIT__ 1\n")
    assert runtime_exec._dev_exit_code(transcript) == 1


def test_a_successful_local_dev_run_still_reports_zero():
    import runtime_exec  # noqa: PLC0415
    assert runtime_exec._dev_exit_code(
        "wrote 3 files\n__DEV_RUNTIME_EXIT__ 0\n") == 0


def test_an_absent_marker_does_not_invent_a_failure():
    """An older dev server has no trailer; assuming failure would be worse."""
    import runtime_exec  # noqa: PLC0415
    assert runtime_exec._dev_exit_code("some output with no trailer") == 0
    assert runtime_exec._dev_exit_code("__DEV_RUNTIME_EXIT__ nonsense") == 0


def test_agent_output_cannot_forge_the_exit_code():
    """The trailer is read from the END, so earlier text cannot win."""
    import runtime_exec  # noqa: PLC0415
    transcript = ("the agent printed __DEV_RUNTIME_EXIT__ 0 in its own output\n"
                  "__DEV_RUNTIME_EXIT__ 1\n")
    assert runtime_exec._dev_exit_code(transcript) == 1


def test_the_failure_tail_is_readable_not_ansi_soup(monkeypatch):
    """The diagnosis was PRESENT but unreadable, which is nearly as bad as absent.

    opencode wraps its error in colour codes. The engine collects lines via `on_line`
    into the `tail:` of the role-failure message, so cleaning the assembled transcript
    afterwards left the caller's copy raw: escape bytes in the engine log, in the role
    note, and on the pull request, with truncation landing mid-sequence.
    """
    import io  # noqa: PLC0415
    import runtime_exec  # noqa: PLC0415

    raw = (b"\x1b[0m\n> build \xc2\xb7 model\n\x1b[91m\x1b[1mError: \x1b[0m"
           b"AWS SigV4 authentication requires AWS credentials.\n"
           b"__DEV_RUNTIME_EXIT__ 1\n")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # urllib is imported INSIDE _run_in_local_dev, so patch the module itself.
    import urllib.request  # noqa: PLC0415
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(raw))
    seen: list[str] = []
    result = runtime_exec._run_in_local_dev(
        "http://127.0.0.1:1/invocations", "opencode", "p", "run_x", None,
        "m", seen.append, 5.0)
    assert result["exit"] == 1, result
    tail = "\n".join(seen)
    assert "\x1b" not in tail, f"escape bytes reached the caller: {tail!r}"
    assert "AWS SigV4 authentication requires AWS credentials." in tail


def test_the_dev_seam_returns_that_code_not_a_literal_zero():
    src = open(os.path.join(os.path.dirname(engine.__file__), "runtime_exec.py"),
               encoding="utf-8").read()
    body = src[src.index("def _run_in_local_dev"):src.index("_DEV_EXIT_MARKER =")]
    assert '"exit": 0' not in body, (
        "_run_in_local_dev still returns a hardcoded exit 0, so a failed role CLI "
        "reads as success")
    assert body.count('"exit": dev_exit') == 2


# ------------- 5. the local mount copy counted .git as the role's work

def test_the_local_mount_readback_excludes_what_the_deployed_one_does():
    """Two seams must agree on what a role "wrote", or the count is meaningless.

    A live local run reported "20 files" for a role whose real output was one file:
    the copy swept in `.git` and its 15 sample hooks (created for opencode, which
    anchors on a git root). That count is the engine's only measure of a role's work
    and it is printed on the pull request.

    Exercised through the real read-back rather than grepped from source, so it pins
    the COUNT the engine reports, not the way the copy happens to be written.
    """
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    mount = tempfile.mkdtemp()
    runs = tempfile.mkdtemp()
    prior = {k: os.environ.get(k) for k in ("WORKSHOP_S3FILES_DIR", "WORKSHOP_RUNS_DIR")}
    try:
        os.environ["WORKSHOP_S3FILES_DIR"] = mount
        os.environ["WORKSHOP_RUNS_DIR"] = runs
        import importlib  # noqa: PLC0415
        eng_mod = importlib.reload(engine)

        run = eng_mod.Run(run_id="run_000000_800", task="t",
                          agents=["claude-code"], roles={"claude-code": "backend"})
        work = os.path.join(mount, run.run_id)
        os.makedirs(os.path.join(work, ".git", "hooks"), exist_ok=True)
        os.makedirs(os.path.join(work, "__pycache__"), exist_ok=True)
        with open(os.path.join(work, "server.py"), "w") as f:
            f.write("# the role's actual work\n")
        for i in range(15):  # what `git init` leaves behind
            with open(os.path.join(work, ".git", "hooks", f"h{i}.sample"), "w") as f:
                f.write("#!/bin/sh\n")
        with open(os.path.join(work, "__pycache__", "x.pyc"), "w") as f:
            f.write("x")

        class _E:
            name = "agentcore"
        eng = eng_mod.Engine.__new__(eng_mod.Engine)
        eng.executor = _E()
        n = eng._read_work_tree(run, "claude-code")
        assert n == 1, (
            f"the read-back counted {n} files as this role's work; only server.py is. "
            "Scaffolding inflates the count the engine reports on the PR and can hide "
            "a role that produced nothing.")
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib  # noqa: PLC0415
        importlib.reload(engine)
        shutil.rmtree(mount, ignore_errors=True)
        shutil.rmtree(runs, ignore_errors=True)


def test_the_exclusions_cover_the_dirs_that_caused_it():
    import runtime_exec  # noqa: PLC0415
    for d in (".git", "node_modules", "__pycache__"):
        assert d in runtime_exec._TREE_EXCLUDES
