"""A build must be watchable while it runs, including from a DEPLOYED coordinator.

The console renders the per-role feed in-process and `watch_agents.py` attaches to its
multiplexed Runtime PTYs, but the served Lab 2 path is a coordinator inside its own
AgentCore Runtime. Before this, the only window into such a run was a chat turn per
poll -- about a minute of model time per line of state.

Three halves, all found broken by a live run and each pinned below:

1. The snapshot carries a bounded activity feed.
2. That feed includes what the role is PRINTING. `role_events` alone could not serve
   this: the engine is its only producer, so a live run's activity was one summary line
   per role while the page promised a window into the work. The dispatch had the lines
   all along (`on_line`), and threw them away.
3. The watcher can FIND a deployed coordinator's run. It reads the S3 mirror the
   coordinator writes, which nothing on the workshop host names, so the taught command
   used to answer "no runs found" mid-build.

And the standing rule: the watcher renders all of it without invoking anything.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import run_store  # noqa: E402
import watch_run  # noqa: E402


class _FakeRun:
    """Only what _persistable_activity touches."""
    def __init__(self, feeds, output=None):
        import threading
        self._lock = threading.Lock()
        self.role_events = feeds
        self.role_output = output or {}


# ------------------------------------------------------------------ the feed persists

def test_the_snapshot_carries_what_each_role_is_doing():
    run = _FakeRun({"claude-code": [
        {"kind": "thinking", "text": "the request asks for persistence, so SQLite"},
        {"kind": "tool_use", "name": "Write", "input": "src/storage/database.js"},
        {"kind": "tool_result", "result": "wrote 41 lines"},
    ]})
    activity = engine._persistable_activity(run)
    assert list(activity) == ["claude-code"]
    kinds = [e["kind"] for e in activity["claude-code"]]
    assert kinds == ["thinking", "tool_use", "tool_result"], \
        "the KIND is what makes the feed legible as work rather than log spam"
    assert activity["claude-code"][1]["name"] == "Write"


def test_the_feed_is_bounded_so_a_heartbeat_stays_small():
    # A chatty role: more events than the cap, each longer than the cap.
    flood = [{"kind": "text", "text": "x" * 5000} for _ in range(500)]
    activity = engine._persistable_activity(_FakeRun({"kiro": flood}))
    assert len(activity["kiro"]) == engine._ACTIVITY_EVENTS_PER_ROLE
    assert all(len(e["text"]) <= engine._ACTIVITY_TEXT_CAP for e in activity["kiro"])
    # The snapshot is rewritten on every heartbeat, so the whole feed must stay tiny.
    assert len(json.dumps(activity)) < 8000


def test_it_keeps_the_NEWEST_events():
    feed = [{"kind": "text", "text": f"step {i}"} for i in range(40)]
    activity = engine._persistable_activity(_FakeRun({"opencode": feed}))
    assert activity["opencode"][-1]["text"] == "step 39", \
        "watching means seeing what is happening NOW, not how the run opened"


def test_a_role_with_no_events_is_omitted_rather_than_empty():
    assert engine._persistable_activity(_FakeRun({"kiro": []})) == {}


# ------------------------------------------------- what the role is actually printing

def _dispatched_run():
    """A real Run, so add_output is exercised rather than a stand-in dict."""
    return engine.Run(run_id="run_181257_a2d15cfb6265", task="build an API",
                      agents=["claude-code", "kiro"],
                      roles={"claude-code": "backend-builder", "kiro": "validator"})


def test_the_feed_shows_the_lines_the_role_printed():
    run = _dispatched_run()
    for line in ("Reading the request", "Writing src/storage/database.js", "done"):
        run.add_output("claude-code", line)
    activity = engine._persistable_activity(run)
    assert [e["kind"] for e in activity["claude-code"]] == ["output"] * 3
    assert activity["claude-code"][-1]["text"] == "done", \
        "a live window shows the newest line last"


def test_a_role_that_only_printed_still_appears():
    """The live-run defect: a role with no structured event was invisible."""
    run = _dispatched_run()
    run.add_output("kiro", "authoring the acceptance check")
    assert list(engine._persistable_activity(run)) == ["kiro"]


def test_the_output_tail_rolls_instead_of_freezing():
    """role_events freezes at its cap by design. A live window must not: after the cap
    a watcher would re-read the oldest lines for the rest of a 20-minute build."""
    run = _dispatched_run()
    for i in range(500):
        run.add_output("opencode", f"line {i}")
    activity = engine._persistable_activity(run)
    assert len(activity["opencode"]) == engine._OUTPUT_TAIL_PER_ROLE
    assert activity["opencode"][-1]["text"] == "line 499"


def test_the_echoed_dispatch_command_is_not_mistaken_for_work():
    run = _dispatched_run()
    run.add_output("claude-code", "x" * (engine._OUTPUT_LINE_MAX + 1))
    run.add_output("claude-code", "   ")
    assert engine._persistable_activity(run) == {}


def test_terminal_control_residue_and_prompt_furniture_are_dropped():
    """All three were real on a live run: a frame boundary split the ESC byte off
    `[?2004h`, and a TUI redraws its `>` prompt constantly."""
    run = _dispatched_run()
    for noise in ("[?2004h", "[?2004l", ">", "  >  ", "[2K", "[1A",
                  "[38;5;244m", "[0m"):
        run.add_output("claude-code", noise)
    assert engine._persistable_activity(run) == {}, "noise reached the feed"
    run.add_output("claude-code", "[?2004hWriting src/app.js")
    run.add_output("claude-code", "[38;5;244m - Completed in 0.18s")
    assert [e["text"] for e in engine._persistable_activity(run)["claude-code"]] == [
        "Writing src/app.js", "- Completed in 0.18s"], \
        "residue must be stripped from a real line, not drop the line"


def test_no_raw_escape_byte_reaches_the_feed():
    """A bare ESC in a persisted line makes the READER's terminal eat the characters
    after it. Live: `'\\x1b - Completed in 0.12s'` rendered as "ompleted in 0.12s"."""
    import runtime_exec
    raw = "\x1b[38;5;244m - Completed in 0.12s\x1b[0m"
    cleaned = runtime_exec._clean(raw)
    assert "\x1b" not in cleaned and cleaned.strip() == "- Completed in 0.12s"
    run = _dispatched_run()
    run.add_output("kiro", cleaned)
    text = engine._persistable_activity(run)["kiro"][0]["text"]
    assert text == "- Completed in 0.12s", text


def test_bracketed_prose_is_not_mistaken_for_an_escape_sequence():
    """The residue pattern requires a digit before the final letter, so ordinary output
    survives. An agent prints a great deal of it."""
    run = _dispatched_run()
    for real in ("[abc] chose SQLite", "array[10] holds the rows", "see [docs]"):
        run.add_output("kiro", real)
    assert [e["text"] for e in engine._persistable_activity(run)["kiro"]] == [
        "[abc] chose SQLite", "array[10] holds the rows", "see [docs]"]


def test_a_secret_a_role_printed_never_reaches_the_durable_snapshot():
    """No credential is on the dispatch path by construction; this is the net under it,
    because the tail is persisted to the runtime bucket.

    The samples are ASSEMBLED from pieces rather than written as literals. A credential
    scanner matches on shape, not on meaning, so a literal `ASIA...` or `gho_...` here is
    a hard-coded-secret finding in every clone of this repository -- it BLOCKED a commit
    on a workshop box. Concatenation keeps the value the code under test sees identical
    while leaving nothing secret-shaped in the file."""
    fake_vendor_key = "ksk" + "_" + "EXAMPLEONLYNOTAREALKEY0123"
    fake_gh_token = "gho" + "_" + "EXAMPLEONLYNOTAREALTOKEN01"
    fake_aws_key_id = "ASI" + "A" + "EXAMPLEONLYNOTREAL01"
    run = _dispatched_run()
    run.add_output("kiro", f"KIRO_API_KEY={fake_vendor_key}")
    run.add_output("kiro", f"token {fake_gh_token}")
    run.add_output("kiro", f"key {fake_aws_key_id}")
    printed = json.dumps(engine._persistable_activity(run))
    for leaked in (fake_vendor_key, fake_gh_token, fake_aws_key_id):
        assert leaked not in printed, f"{leaked!r} must be redacted"
        assert leaked[:12] not in printed, "not even the identifying prefix may survive"
    assert printed.count("[redacted]") == 3


def test_both_kinds_are_bounded_together():
    run = _dispatched_run()
    for i in range(500):
        run.add_event("claude-code", {"kind": "text", "text": f"event {i}"})
        run.add_output("claude-code", f"line {i}")
    activity = engine._persistable_activity(run)
    assert len(activity["claude-code"]) <= (engine._ACTIVITY_EVENTS_PER_ROLE
                                           + engine._OUTPUT_TAIL_PER_ROLE)
    assert len(json.dumps(activity)) < 16000, \
        "the snapshot is rewritten on every heartbeat, so it must stay small"


def test_the_watched_window_is_the_same_one_the_engine_captures():
    """The feed shows the CLI's own output, so its window must be the sentinels
    runtime_exec already slices on. If the two ever drift, the watcher starts showing
    archive downloads and worktree setup again, which is what filled the whole 12-line
    window on a live run."""
    import runtime_exec
    begin, end = runtime_exec._RUN_BEGIN, runtime_exec._RUN_END
    assert runtime_exec.run_window_marker(f"{begin}-abc123") == "begin"
    assert runtime_exec.run_window_marker(f"  {end}-abc123  ") == "end"
    assert runtime_exec.run_window_marker("Writing src/app.js") is None
    # And the slice really is delimited by them, nonce suffix and all.
    raw = (f"downloading the archive\n{begin}-abc123\nWriting src/app.js\n"
           f"{end}-abc123\npacking the result\n")
    assert runtime_exec._slice(raw, f"{begin}-abc123", f"{end}-abc123") == \
        "Writing src/app.js"


def test_the_echoed_command_does_not_open_the_window():
    """Live defect: the dispatch echo carries the sentinel VALUES mid-line, in the
    `B1=...; E1=...` assignment. A substring match opened the window on the echo, so the
    watcher showed the tar command's own arguments as the role's work."""
    import runtime_exec
    echo = (f"B1={runtime_exec._RUN_BEGIN}-abc123; E1={runtime_exec._RUN_END}-abc123; "
            "echo \"$B1\"; tar --exclude=.cache -czf /tmp/workshop-result-abc123.tar.gz .")
    assert runtime_exec.run_window_marker(echo) is None, \
        "only a sentinel ALONE on its line may open or close the window"


def test_a_line_cut_in_half_by_a_frame_boundary_arrives_whole():
    """Live defect: a frame boundary falls wherever the network put it, so a streaming
    TUI's sentence reached the watcher as a dozen one-word "lines" and filled the whole
    window with fragments. _drive_shell holds the tail until the next frame closes it."""
    import runtime_exec

    got: list[str] = []
    frames = ["Writing sr", "c/app.js\r\nWriting src/", "routes.js\n",
              # A TUI redrawing one line with a bare CR: two separate lines, not one
              # glued together (gluing them ate the first letter of a word live).
              "Searching for package.json\rSuccessfully found 3 matches\n",
              "no newline yet"]
    pending = ""
    for text in frames:                       # the loop _drive_shell now runs
        pending += text.replace("\r\n", "\n").replace("\r", "\n")
        if "\n" in pending:
            *complete, pending = pending.split("\n")
            got.extend(complete)
    if pending:
        got.append(pending)                   # the flush when the shell ends
    assert got == ["Writing src/app.js", "Writing src/routes.js",
                   "Searching for package.json", "Successfully found 3 matches",
                   "no newline yet"]
    # And the real function still splits on newlines, so the buffer is the only change.
    assert "pending" in runtime_exec._drive_shell.__code__.co_varnames


def test_the_console_feed_carries_the_printed_output_too():
    """The console had the same blind spot as the watcher: its run view renders
    role_events, so a dispatched role looked idle while it worked."""
    run = _dispatched_run()
    run.add_event("kiro", {"kind": "text", "text": "[validator] built on Runtime"})
    run.add_output("kiro", "authoring the acceptance check")
    feed = engine.public_events(run)
    assert [e["kind"] for e in feed["kiro"]] == ["text", "output"], \
        "structured events first, then what the role printed"


# ------------------------------------------------------- finding a coordinator's run

def test_a_reader_derives_the_same_bucket_the_writer_was_told():
    import runtime_stage
    saved_env = os.environ.get("WORKSHOP_RUNTIME_BUCKET")
    saved_fn = runtime_stage.runtime_bucket
    try:
        os.environ["WORKSHOP_RUNTIME_BUCKET"] = "explicitly-wired"
        assert run_store.reader_mirror_bucket() == "explicitly-wired", \
            "an explicit mirror always wins"
        os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
        runtime_stage.runtime_bucket = lambda *a, **k: "coding-agents-1234-us-west-2"
        assert run_store.reader_mirror_bucket() == "coding-agents-1234-us-west-2", \
            "with nothing wired, a reader derives the writer's own convention"

        def _unresolvable(*_a, **_k):
            raise RuntimeError("REGION_NOT_RESOLVED")
        runtime_stage.runtime_bucket = _unresolvable
        assert run_store.reader_mirror_bucket() == "", \
            "an unreachable mirror must still leave local runs watchable"
    finally:
        runtime_stage.runtime_bucket = saved_fn
        os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
        if saved_env is not None:
            os.environ["WORKSHOP_RUNTIME_BUCKET"] = saved_env


def test_the_watcher_attaches_to_the_mirror_and_says_where_it_looked():
    saved_env = os.environ.get("WORKSHOP_RUNTIME_BUCKET")
    saved_fn = run_store.reader_mirror_bucket
    try:
        os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
        run_store.reader_mirror_bucket = lambda: "coding-agents-1234-us-west-2"
        bucket = watch_run._attach_to_the_mirror()
        assert bucket == "coding-agents-1234-us-west-2"
        assert os.environ["WORKSHOP_RUNTIME_BUCKET"] == bucket, \
            "run_store reads the mirror from the environment, so attaching must set it"
        where = watch_run._where_it_looked(bucket)
        assert "state" in where and bucket in where, \
            f"a miss must name BOTH places it looked, not just local disk: {where}"
        assert watch_run._where_it_looked("") == f"{watch_run._RUNS_DIR}/state"
    finally:
        run_store.reader_mirror_bucket = saved_fn
        os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
        if saved_env is not None:
            os.environ["WORKSHOP_RUNTIME_BUCKET"] = saved_env


# ------------------------------------------------------------------- the watcher reads

def _persist(tmp, payload):
    run_store.save(tmp, payload["run_id"], payload, lambda *_a, **_k: None)


def test_the_watcher_renders_a_live_run_without_invoking_anything():
    with tempfile.TemporaryDirectory() as tmp:
        _persist(tmp, {
            "run_id": "run_015138_e9cfbe87b3a8",
            "status": "running", "phase": "gate", "iterations": 2,
            "task": "Build an HTTP API for a personal library",
            "progress": [
                {"agent": "claude-code", "role": "backend", "state": "done",
                 "note": "prepared 18 change(s)"},
                {"agent": "kiro", "role": "validator", "state": "running", "note": ""},
            ],
            "activity": {"kiro": [
                {"kind": "tool_use", "name": "Write", "text": "acceptance_check"},
                {"kind": "text", "text": "141 checks run, 1 failed"},
            ]},
            "role_prs": [{"number": 8, "role": "backend", "state": "OPEN",
                          "url": "https://github.com/o/r/pull/8"}],
            # A real gate entry has `sequence`, never `round`; asking for a round
            # printed "round ?" on every line of a live capture.
            "gate_history": [{"work_id": "work_claude-code_2a27", "sequence": 1,
                              "passed": False, "summary": "VERDICT: REJECT"}],
        })
        out, rc = _watch_once(tmp, "run_015138_e9cfbe87b3a8")
        assert rc == 0
        for expected in ("run_015138_e9cfbe87b3a8", "claude-code", "kiro",
                         "acceptance_check", "141 checks run, 1 failed", "#8",
                         "FAIL"):
            assert expected in out, f"the frame must show {expected!r}\n{out}"
        assert "?" not in out.split("gate work_claude-code_2a27")[1][:8], \
            f"the gate row must number itself from its real field:\n{out}"


def _watch_once(runs_dir: str, run_id: str) -> tuple[str, int]:
    """One --once --plain frame, captured, with no mirror so no AWS call is made."""
    watch_run._RUNS_DIR = runs_dir
    sys.argv = ["watch_run.py", run_id, "--once", "--plain"]
    saved_env = os.environ.get("WORKSHOP_RUNTIME_BUCKET")
    saved_fn = run_store.reader_mirror_bucket
    os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
    run_store.reader_mirror_bucket = lambda: ""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = watch_run.main()
    finally:
        run_store.reader_mirror_bucket = saved_fn
        os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
        if saved_env is not None:
            os.environ["WORKSHOP_RUNTIME_BUCKET"] = saved_env
    return buf.getvalue(), rc


def test_a_run_with_no_record_yet_says_so_instead_of_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        out, rc = _watch_once(tmp, "run_does_not_exist")
        assert rc == 1
        assert "no durable record" in out


def test_the_watcher_never_reaches_for_a_model_or_a_verdict():
    """Same rule as replay.py: this is reporting, never the verdict path."""
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "watch_run.py")).read()
    for forbidden in ("import llm", "import reviewer", "from llm", "from reviewer",
                      "boto3.client('bedrock", "invoke_agent_runtime"):
        assert forbidden not in source, \
            f"watch_run.py must not {forbidden!r}: watching may not influence a run"
