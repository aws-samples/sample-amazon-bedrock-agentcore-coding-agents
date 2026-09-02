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
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_store  # noqa: E402

_RUNS_DIR = os.environ.get("WORKSHOP_RUNS_DIR", ".runs")

# One glyph per event kind, so a glance separates thinking from doing.
_KIND = {"tool_use": "*", "tool_result": "<", "thinking": "~", "text": " ",
         "output": ">"}   # ">" is a line the role's CLI printed, as it printed it

_STATE_ORDER = ("queued", "running", "done", "failed", "skipped")


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
    """Point this process at the deployed coordinator's run-state mirror.

    The SERVED Lab 2 build runs inside the coordinator's own Runtime, whose
    filesystem dies with the microVM, so it mirrors every snapshot to S3. Nothing on
    the workshop host sets ``WORKSHOP_RUNTIME_BUCKET`` (the stack exports the region,
    the account id and the model ids, not the bucket), and a live run showed the
    cost: with a build underway, this command answered "no runs found" while its
    snapshot sat in the mirror. So resolve the same bucket the writer was handed and
    read from it too. Never fatal: an unresolvable mirror still leaves local runs
    watchable.

    Returns the bucket in use, or "" when only local disk is readable.
    """
    if not os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip():
        bucket = run_store.reader_mirror_bucket()
        if bucket:
            os.environ["WORKSHOP_RUNTIME_BUCKET"] = bucket
    return os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()


def _where_it_looked(bucket: str) -> str:
    local = f"{_RUNS_DIR}/state"
    return f"{local} or s3://{bucket}/{run_store._STATE_PREFIX}" if bucket else local


def _latest_run_id(bucket: str) -> str:
    recent = run_store.recent(_RUNS_DIR, limit=1)
    if not recent:
        sys.exit(f"no runs found in {_where_it_looked(bucket)}. Submit a build "
                 "first, or pass a run id.")
    return recent[0].get("run_id", "")


def _state_mark(state: str, ink: _Ink) -> str:
    return {
        "done": ink.green("done"),
        "failed": ink.red("failed"),
        "running": ink.yellow("running"),
        "queued": ink.dim("queued"),
        "skipped": ink.dim("skipped"),
    }.get(state, state or "?")


def _gate_mark(entry: dict, ink: _Ink) -> str:
    if entry.get("passed"):
        return ink.green("PASS")
    return ink.red("FAIL")


def _frame(rec: dict, ink: _Ink, width: int) -> list[str]:
    lines: list[str] = []
    status = rec.get("status", "?")
    colour = {"passed": ink.green, "failed": ink.red,
              "needs_human": ink.yellow}.get(status, ink.cyan)
    lines.append(f"{ink.bold(rec.get('run_id', '?'))}   {colour(status)}"
                 f"   phase={rec.get('phase') or '-'}"
                 f"   round={rec.get('iterations', 1)}"
                 f"   source={rec.get('source', 'live')}")
    task = " ".join(str(rec.get("task") or "").split())
    if task:
        lines.append(ink.dim("  " + task[:width - 2]))
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
    prs = rec.get("role_prs") or []
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
        nth = entry.get("round", entry.get("sequence", "?"))
        lines.append(f"    gate {entry.get('work_id', '?')} #{nth}: "
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
    run_id = args.run_id or _latest_run_id(bucket)
    terminal = ("passed", "failed", "needs_human")
    last = ""
    while True:
        rec = run_store.load(_RUNS_DIR, run_id)
        if rec is None:
            print(f"no durable record for {run_id} yet in {_where_it_looked(bucket)} "
                  f"(a run appears here at its first heartbeat)")
            if args.once:
                return 1
            time.sleep(args.interval)
            continue
        width = shutil.get_terminal_size((100, 30)).columns
        body = "\n".join(_frame(rec, ink, width))
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
        if rec.get("status") in terminal:
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
        print("\nstopped watching. The build is unaffected and still running.")
        raise SystemExit(0) from None
