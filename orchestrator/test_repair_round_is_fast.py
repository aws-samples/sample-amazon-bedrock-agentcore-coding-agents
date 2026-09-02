"""A repair round must re-run the check it already has, and must be told what failed.

Both properties come from one measured live run (2026-09-02, real event account, real
repository, `service-from-scratch`):

    submit -> PR open (backend build)          5m21s
    PR -> round 1 gate REJECT (author + run)   5m49s
    gate -> repair requested                      5s
    repair commit pushed                      11m03s
    -> round 2 gate PASS                       9m44s
                                              ------
                                              32m02s, before review or merge

The authored check EXECUTES in 4.9 seconds. So round 2's 9m44s was almost entirely the
validator writing a second check for a request that had not changed, and the repair's
11m03s was largely the builder rediscovering which of 141 assertions failed -- because
it received only the one-line summary, while the check's own FAIL lines went to the
pull request and nowhere else.

Neither fix loosens the gate. Reusing the check is STRICTER than re-authoring it: a
check written with the repaired code in view can be softer than the one that caught the
defect, and nothing would reveal that. Passing the FAIL lines to the builder reports
what the checker OBSERVED, exactly as a CI log does; the check still decides.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402


class _Item:
    def __init__(self, work_id):
        self.work_id = work_id
        self.role = "claude-code"


class _Run:
    def __init__(self, workdir):
        self.workdir = workdir


# ------------------------------------------------------- the check survives a round

def test_a_kept_check_is_found_again_next_round():
    with tempfile.TemporaryDirectory() as tmp:
        eng = engine.Engine.__new__(engine.Engine)
        run, item = _Run(tmp), _Item("work_claude-code_2a27acdf70")

        assert eng._prior_check(run, item) == "", \
            "round 1 has no prior check, so the validator must author one"

        authored = os.path.join(tmp, "acceptance_check")
        with open(authored, "w") as handle:
            handle.write("#!/usr/bin/env node\n// 141 assertions\n")
        eng._keep_check_for_later_rounds(run, item, authored)

        kept = eng._prior_check(run, item)
        assert kept, "a repair round must find the check the previous round authored"
        with open(kept) as handle:
            assert "141 assertions" in handle.read(), \
                "it must be the SAME check, byte for byte, not a new one"


def test_each_pull_request_keeps_its_own_check():
    """One validator serves every pull request, so the copies must not collide."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = engine.Engine.__new__(engine.Engine)
        run = _Run(tmp)
        for work_id, body in (("work_claude-code_aaa", "backend check"),
                              ("work_opencode_bbb", "frontend check")):
            src = os.path.join(tmp, work_id)
            with open(src, "w") as handle:
                handle.write(body)
            eng._keep_check_for_later_rounds(run, _Item(work_id), src)
        for work_id, body in (("work_claude-code_aaa", "backend check"),
                              ("work_opencode_bbb", "frontend check")):
            with open(eng._prior_check(run, _Item(work_id))) as handle:
                assert handle.read() == body


def test_an_empty_or_missing_copy_never_becomes_a_skipped_gate():
    """Fail-loud is unchanged: no usable copy means author again, never 'no check'."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = engine.Engine.__new__(engine.Engine)
        run, item = _Run(tmp), _Item("work_claude-code_ccc")
        kept = eng._kept_check_path(run, item)
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        open(kept, "w").close()                     # zero bytes
        assert eng._prior_check(run, item) == "", \
            "an empty kept copy must send the validator back to author a real check"


# ------------------------------------------- the builder learns WHICH assertion broke

def test_the_builder_is_given_the_checks_own_failing_lines():
    # Shaped like the real output from the live run.
    output = "\n".join([
        "PASS  docs: spec documents POST /books",
        "INFO  restart: started a second process on port 43341",
        "FAIL  books listing does not hold exactly the 3 valid books",
        "PASS  persistence: summary counts after restart equal the counts before",
        "141 checks run, 1 failed, elapsed 4.9s",
        "VERDICT: REJECT - see the FAIL lines above.",
    ])
    lines = engine._gate_fail_lines(output)
    assert any("does not hold exactly the 3 valid books" in line for line in lines), \
        "the one failing assertion is the whole point of sending anything"
    assert not any(line.startswith(("PASS", "INFO")) for line in lines), \
        "passing lines are noise in a repair prompt"


def test_a_green_check_sends_nothing_back():
    assert engine._gate_fail_lines(
        "PASS one\nPASS two\n10 checks run, 0 failed, elapsed 1.1s") == []
    assert engine._gate_fail_lines("") == []


def test_no_format_is_assumed_of_a_validator_authored_check():
    """The validator picks its own language and reporting style."""
    assert engine._gate_fail_lines("✗ the summary endpoint returned 500")
    assert engine._gate_fail_lines("not ok 7 - deleted book came back after restart")
    assert engine._gate_fail_lines("AssertionError: expected 409, got 200")


def test_the_extract_is_bounded():
    flood = "\n".join(f"FAIL  assertion {i}" for i in range(200))
    assert len(engine._gate_fail_lines(flood)) == engine._MAX_FAIL_LINES
