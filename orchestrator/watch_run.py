#!/usr/bin/env python3
"""Watch a build as it happens, including one driven by the DEPLOYED coordinator.

    python3 orchestrator/watch_run.py                 # the most recent run
    python3 orchestrator/watch_run.py <run_id>
    python3 orchestrator/watch_run.py --once          # print one frame and exit
    python3 orchestrator/watch_run.py --plain         # no colour, no cursor tricks

Why this exists. The console renders the live per-role feed in-process, and
``watch_agents.py`` attaches to the console's multiplexed Runtime PTYs -- but the SERVED
Lab 2 path is a coordinator deployed into its own AgentCore Runtime. Its engine runs
somewhere the attendee cannot attach to, so the only window used to be a chat turn per
poll: about a minute of model time to learn one line of state, which is the opposite of
watching. Meanwhile the engine was already recording exactly the right thing.

So this reads the DURABLE RUN RECORD (``run_store``: the local state directory, or the
runtime bucket when the coordinator wrote it there) and redraws it. No model is invoked,
nothing is dispatched, and no session is opened, which is what makes it cheap enough to
leave running for the whole build.

It is strictly READ-ONLY. It cannot start, stop, alter, or grade a run. The worst a bug
in here can do is describe a build badly -- the same rule ``replay.py`` follows, and the
reason this file may not import ``llm`` or ``reviewer``.

Stdlib only, because it runs on the workshop box with nothing installed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_advice  # noqa: E402
import run_store  # noqa: E402

# The same default the engine uses (the repository's .runs), not a path relative to
# wherever the watcher happens to be started.
_RUNS_DIR = os.environ.get("WORKSHOP_RUNS_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".runs"))
_RUN_ID_RE = re.compile(r"run_[0-9]{6}_[0-9a-f]{12}")
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _clean_run_id(raw: str) -> str:
    """A pasted run ID as the store names it; the session ID is refused, not awaited.

    Pages ask people to save both IDs, and a pasted session ID, or a run ID with a
    trailing period or backticks, waited forever for a record that could not exist.
    """
    value = (raw or "").strip().strip("`'\".,;:()[]<>")
    if _SESSION_ID_RE.fullmatch(value):
        sys.exit("That is the coordinator SESSION ID, not a run ID. A run ID looks like "
                 "run_123456_0123456789ab. Run this without an ID to follow the newest build.")
    found = _RUN_ID_RE.search(value)
    return found.group(0) if found else value

# One glyph per event kind, so a glance separates thinking from doing.
_KIND = {"tool_use": "*", "tool_result": "<", "thinking": "~", "text": " ",
         "output": ">"}   # ">" is a line the role's CLI printed, as it printed it

_STATE_ORDER = ("queued", "running", "done", "failed", "blocked", "skipped")


class _Ink:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def dim(self, t): return self(t, "2")
    def bold(self, t): return self(t, "1")
    def green(self, t): return self(t, "32")
    def red(self, t): return self(t, "31")
    def yellow(self, t): return self(t, "33")
    def cyan(self, t): return self(t, "36")


def _attach_to_the_mirror() -> str:
    """Read the deployed coordinator's run-state mirror too (``run_store``)."""
    return run_store.attach_reader_mirror()


def _where_it_looked(bucket: str) -> str:
    local = f"{_RUNS_DIR}/state"
    return f"{local} or s3://{bucket}/{run_store._STATE_PREFIX}" if bucket else local


def _latest_run_id(bucket: str) -> str:
    recent = run_store.recent(_RUNS_DIR, limit=1)
    if not recent:
        sys.exit(f"no runs found in {_where_it_looked(bucket)} yet. A build appears "
                 "here at its first heartbeat, a minute or two after you submit it: "
                 "run this again in a moment. Do not resubmit while you wait.")
    return recent[0].get("run_id", "")


def _state_mark(state: str, ink: _Ink) -> str:
    return {
        "done": ink.green("done"),
        "failed": ink.red("failed"),
        "blocked": ink.yellow("blocked"),
        "running": ink.yellow("running"),
        "queued": ink.dim("queued"),
        "skipped": ink.dim("skipped"),
    }.get(state, state or "?")


def _gate_mark(entry: dict, ink: _Ink) -> str:
    if entry.get("passed"):
        return ink.green("PASS")
    return ink.red("FAIL")


def _interrupted(rec: dict) -> bool:
    """A non-terminal record whose heartbeat stopped: its coordinator is gone.

    The engine already recognises this (``run_store.active_snapshot_is_stale``, which
    ``chat.py`` turns into ``COORDINATOR_SESSION_INTERRUPTED``), but that verdict is
    computed when the coordinator is ASKED and is never written back to the record. A
    watcher reading the record therefore sat on "running" forever, which is the one thing
    this repository refuses to do: a run that cannot advance is a failure to report, not
    a wait. Observed live -- a coordinator microVM was recycled mid-repair, `run_status`
    said `needs_human`, and this watcher still said `running`.
    """
    return run_store.active_snapshot_is_stale(rec)


_TERMINAL = ("passed", "failed", "needs_human")
_REPO_DIR = "~/sample-amazon-bedrock-agentcore-coding-agents"


def _effective(rec: dict) -> dict:
    """The record as the reader should see it: an interrupted run is needs_human.

    Reading a record can never CHANGE one: this is the reporting path. It reports the
    same reason the coordinator would, and says what to do. A recycled coordinator
    does not un-open the pull requests it already published (they may be recorded
    only under ``work_items`` if the recycle came before the check finished), so with
    one open the advice is to continue from that PR, the same rule as
    ``engine.next_action``, never to resubmit the whole build.
    """
    if not _interrupted(rec):
        return rec
    if run_advice.pr_urls(rec):
        advice = ("the coordinator Runtime was recycled, but the pull request(s) it "
                  "already opened are unaffected. Open each one, read its check and "
                  "Assessment, and continue as a person; do NOT resubmit the whole build.")
    else:
        advice = ("the coordinator Runtime was recycled before this run opened a pull "
                  "request, so nothing can advance it; submit the request again.")
    return {**rec, "status": "needs_human",
            "fail_reason": "COORDINATOR_SESSION_INTERRUPTED", "next_action": advice}


def _home() -> str:
    return os.environ.get("HOME") or os.path.expanduser("~")


def _start_hint(target: str) -> str:
    port = run_advice.port_for(target)
    if target == "~/game":
        return "Continue with Play Your Game (Lab 2)."
    if port:
        return (f"Start it on port {port}, as Lab 3 shows; ~/game keeps its own copy "
                "and saved scores.")
    return ("Start it on a free port (not 8000 or 8001, where the earlier games run), "
            f"then print its URL: python3 {_REPO_DIR}/orchestrator/workshop_urls.py <port>")


def _checkout_steps(rec: dict, para, lead: str) -> list[str]:
    """The checkout command for this run's lab, or why the natural folder is taken."""
    target, taken = run_advice.checkout_target(rec, _home())
    natural = "~/game-lab3" if run_advice.is_chat_build(rec) else "~/game"
    lines: list[str] = []
    command = f"python3 orchestrator/github.py checkout {target}"
    if taken:
        lines += para(f"{natural} already holds a checkout. If it already contains this "
                      "merge, keep using it. For a separate copy, check out an unused "
                      "folder instead (checkout replaces its destination):", lead=lead)
        lines += [f"     {command}"]
    else:
        lines += [f"{lead}{command}"]
    lines += para(_start_hint(target), lead="     ")
    return lines


def _what_now(rec: dict, ink: _Ink, width: int) -> list[str]:
    """What to DO about a terminal run, as commands, not only as a sentence.

    The September 25 survey: an attendee whose build failed "was unsure how to
    recover" while the room moved on, and others "never knew if it built". The
    record already says which case this is; this turns it into the next command.
    The engine's own ``next_action`` stays authoritative: the generic red-gate
    choice is offered only when the check or review stayed red after the one
    repair (``run_advice.red_after_repair``); every other stop prints the engine's
    advice in full. Reporting only: it reads the record and the local filesystem.
    """
    rec = _effective(rec)
    status = rec.get("status")
    if status not in _TERMINAL:
        return []
    run_id = str(rec.get("run_id") or "")
    wrap = max(40, min(width, 100) - 7)

    def para(text: str, lead: str = "  ", hang: str = "     ") -> list[str]:
        return textwrap.wrap(text, wrap, initial_indent=lead, subsequent_indent=hang)

    prs = run_advice.pr_rows(rec)
    chat = run_advice.is_chat_build(rec)
    reason = str(rec.get("fail_reason") or "")
    lines = ["", ink.bold("  what to do now")]
    if status == "passed" and prs:
        merged = [p for p in prs if str(p.get("state", "")).lower() == "merged"]
        waiting = [p for p in prs if p not in merged]
        if waiting:
            lines.append("  " + ink.green("BUILD PASSED") +
                         f": {len(waiting)} pull request(s) open for you to merge.")
        else:
            lines.append("  " + ink.green("BUILD PASSED") + ": merged into the default branch.")
        lines += [f"    {p['url']}" for p in prs]
        if waiting:
            lines += para("Open it, read its check and Assessment comments, then choose "
                          "Merge pull request (skip this if you already merged it).",
                          lead="  1. ")
        lines += _checkout_steps(rec, para, lead=f"  {2 if waiting else 1}. ")
        return lines
    if prs:
        lines.append("  " + ink.yellow("BUILD NEEDS YOU") + f": {reason or status}")
        for p in prs:
            state = p.get("state") or ""
            lines.append(f"    {p['url']}" + (f"  ({state})" if state else ""))
        if reason == "COORDINATOR_SESSION_INTERRUPTED":
            lines += para("The pull request(s) above are the durable record. Open each "
                          "one and read its check and Assessment. Do not resubmit the "
                          "whole build to finish one pull request. This watcher keeps "
                          "checking in case the record moves again; Ctrl+C stops watching.")
            return lines
        if not run_advice.red_after_repair(rec):
            # A review outage, a quota stop, a stale branch, an execution error: the
            # engine's advice for each differs, and the generic red-gate choice would
            # contradict it ("do not rebuild for a review failure", "do not resubmit now").
            nxt = rec.get("next_action") or ("Open the pull request and read its latest "
                                             "comments before deciding anything.")
            lines += para(" ".join(str(nxt).split()))
            return lines
        lines += para("The loop stopped as designed: the check or review stayed red "
                      "after its one repair. Choose one:")
        lines += para("If you judge the game good enough, open the PR, read the latest "
                      "check comment and Assessment, and merge it yourself (the engine "
                      "never merges a red PR; a person can). Then run:", lead="  a. ")
        lines += _checkout_steps(rec, para, lead="     ")
        if chat:
            lines += para("Or leave it open: read Next action in Agent Studio, keep the "
                          "evidence, and ask your facilitator if the check itself looks "
                          "wrong.", lead="  b. ")
        else:
            lines += para("Or rerun Run a Build step 1 with a simpler direction: one core "
                          "mechanic, one short round. It creates a NEW session ID; this "
                          "run stays recorded.", lead="  b. ")
        lines += para("Do not resubmit the same request to retry a red pull request.")
        return lines
    head = "BUILD PASSED, but no pull request opened" if status == "passed" \
        else "NO PULL REQUEST OPENED"
    lines.append("  " + ink.red(head) + (f": {reason}" if reason else "."))
    if rec.get("next_action"):
        lines += para(" ".join(str(rec["next_action"]).split()))
    if reason == "COORDINATOR_SESSION_INTERRUPTED" and not chat:
        lines += para("Submit it again with Run a Build step 1, which creates a new "
                      "session ID.")
    lines += para("For help, send this output and the read-only progress check to a "
                  "helper:")
    lines += [f"    python3 {_REPO_DIR}/orchestrator/progress.py"]
    return lines


def _frame(rec: dict, ink: _Ink, width: int) -> list[str]:
    lines: list[str] = []
    rec = _effective(rec)
    status = rec.get("status", "?")
    colour = {"passed": ink.green, "failed": ink.red,
              "needs_human": ink.yellow}.get(status, ink.cyan)
    # No run-wide "round": it counts every PR's rounds together, so two PRs with one
    # repair each read as "round=3", the overstatement this watcher exists to avoid.
    # Each gate line below carries its own PR's round instead.
    lines.append(f"{ink.bold(rec.get('run_id', '?'))}   {colour(status)}"
                 f"   phase={rec.get('phase') or '-'}"
                 f"   source={rec.get('source', 'saved record')}")
    task = " ".join(str(rec.get("task") or "").split())
    if task:
        lines.append(ink.dim("  " + task[:width - 2]))
    # The REASON, not only the advice: a reader who sees `needs_human` needs the label to
    # look up, and the frame rendered next_action while dropping fail_reason entirely.
    reason = rec.get("fail_reason")
    if reason:
        lines.append("  " + ink.red(str(reason)))
    lines.append("")

    # --- roles: what each one is, and what state it is in
    progress = rec.get("progress") or []
    for role in sorted(progress, key=lambda r: _STATE_ORDER.index(r.get("state", "queued"))
                       if r.get("state") in _STATE_ORDER else 9):
        agent = role.get("agent", "?")
        note = " ".join(str(role.get("note") or "").split())
        lines.append(f"  {ink.bold(agent):<28} {_state_mark(role.get('state', ''), ink)}"
                     f"  {ink.dim(note[:width - 45])}")
        # --- and what it is actually doing, newest last
        for ev in (rec.get("activity") or {}).get(agent, []):
            glyph = _KIND.get(ev.get("kind", "text"), " ")
            name = ev.get("name")
            body = ev.get("text", "")
            head = f"{name}: " if name else ""
            lines.append(ink.dim(f"      {glyph} {head}{body}")[:width + 20])
    if not progress:
        lines.append(ink.dim("  (no role has started yet)"))
    lines.append("")

    # --- the per-pull-request verdicts, which are the actual result
    prs = rec.get("role_prs") or [{"url": url} for url in run_advice.pr_urls(rec)]
    if prs:
        lines.append(ink.bold("  pull requests"))
        for pr in prs:
            url = pr.get("url") or pr.get("pr_url") or ""
            num = f"#{pr['number']}" if pr.get("number") else (url.rsplit("/", 1)[-1] or "?")
            lines.append(f"    {num:<6} {pr.get('role', pr.get('agent', '?')):<12}"
                         f" {pr.get('state', '') or ''}  {ink.dim(url)}")
    for entry in (rec.get("gate_history") or [])[-6:]:
        # A gate entry records `sequence` (the nth check of the run) and puts the round
        # in `stage`; there is no `round` key, so asking for one printed "round ?" on
        # every line. Prefer the sequence, which is the number this row actually has.
        stage = re.search(r"round (\d+)", str(entry.get("stage") or ""))
        nth = (f"round {stage.group(1)}" if stage
               else f"#{entry.get('round', entry.get('sequence', '?'))}")
        lines.append(f"    gate {entry.get('work_id', '?')} {nth}: "
                     f"{_gate_mark(entry, ink)}"
                     f"  {ink.dim(' '.join(str(entry.get('summary', '')).split())[:70])}")

    nxt = rec.get("next_action")
    if nxt:
        lines.append("")
        lines.append("  " + ink.bold("next: ") + " ".join(str(nxt).split())[:width - 8])
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id", nargs="?", default="")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between redraws (default 5; the engine's own "
                         "heartbeat is 10s, so faster than that just re-reads)")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--plain", action="store_true", help="no colour or cursor control")
    args = ap.parse_args()

    ink = _Ink(not args.plain and sys.stdout.isatty())
    bucket = _attach_to_the_mirror()
    run_id = _clean_run_id(args.run_id) if args.run_id else _latest_run_id(bucket)
    last = ""
    misses = 0
    while True:
        rec = run_store.load(_RUNS_DIR, run_id)
        if rec is None:
            misses += 1
            print(f"no durable record for {run_id} yet in {_where_it_looked(bucket)} "
                  f"(a run appears here at its first heartbeat)")
            if misses == 12:          # about a minute: say which runs DO exist
                known = [r.get("run_id") for r in run_store.recent(_RUNS_DIR, limit=5)
                         if r.get("run_id")]
                print("  recent runs here: " + (", ".join(known) or "none") +
                      ". Check the ID, or run this without one to follow the newest.")
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        width = shutil.get_terminal_size((100, 30)).columns
        body = "\n".join(_frame(rec, ink, width) + _what_now(rec, ink, width))
        if args.once:
            print(body)
            return 0
        if body != last:                       # redraw only on change: no flicker
            if ink.on:
                sys.stdout.write("\033[2J\033[H")
            else:
                print("-" * min(width, 100))
            print(body, flush=True)
            last = body
        # Stop only on the record's own terminal status. An interrupted-looking run is
        # reported (the frame and "what to do now" say so) but still watched: a
        # heartbeat write can fail transiently and recover, and a watcher that had
        # exited would then miss the build finishing.
        if rec.get("status") in _TERMINAL:
            print()
            print(ink.bold("run is terminal; watching stops here."))
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Ctrl-C stops WATCHING, never the build. Say so, because the two are easy to
        # confuse and an attendee who thinks they killed their run will start another.
        print("\nstopped watching. That does not stop or change the build; run this "
              "again to keep following it.")
        raise SystemExit(0) from None
