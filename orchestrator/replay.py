"""The run, told as a story a reviewer who wasn't there can follow.

A pull request opened by this system arrives with no context. The reviewer sees a
diff and a title, and everything that would let them TRUST the diff -- which roles
ran, what the validator decided to check, whether the gate went red first and why
round 2 happened -- lives in the engine's event log on a box they cannot reach, or
in a coordinator session that has already expired.

So the PR body is the narrative. Not a log dump: a short account of what happened,
in the order it happened, naming who did what and what was proven.

Borrowed from awslabs/aidlc-workflows v2, whose `aidlc-replay` skill exists for
exactly this reason -- it renders their audit trail into a readable story so a
person who wasn't in the room can review the outcome. Same intent here, at our
much smaller scale: their trail spans 32 stages, ours is one bounded loop.

Two constraints shape every line of this module:

  * **It reports; it never judges.** Nothing here decides anything. The gate's
    exit code is the verdict and the reviewer's assessment is the opinion; this
    file reads both after the fact. It is on the PR-body path, never the verdict
    path, so a bug in it cannot turn a red gate green -- the worst it can do is
    describe a run badly.
  * **It states only what the run recorded.** Where a fact is absent (no usage
    reported, no round 2, no endpoint) the narrative says so or omits the line. It
    never infers a number, and it never implies the engine knew something about
    the deliverable that it does not: the engine's whole knowledge is "start what I
    was told to start, run the check I was given".

The body is written ONCE, at `create_pull_request` time (the gateway exposes no
`update_pull_request`), which is why a re-implement round appends its own comment
instead of rewriting history.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Cap the check excerpt. The whole point is to show a reviewer WHAT was asserted
# without pasting a file that already ships in the diff beside it.
_CHECK_HEAD_LINES = 22
_CHECK_LINE_CHARS = 160


def _cell(text: str, limit: int = 120) -> str:
    """One markdown TABLE CELL, from text that was never written to be one.

    A role's note is an internal status string, and on the failure path it is the
    exception -- which carries the tail of the agent's CLI output, complete with
    newlines and pipes. Dropped into a table verbatim, a single newline ends the row
    and the rest of the message becomes body text, so the reviewer gets a shredded
    table instead of the reason their build failed.
    """
    flat = " ".join((text or "").split()).replace("|", "\\|")
    if len(flat) <= limit:
        return flat
    # Keep BOTH ends. A failure note is "ROLE_EXECUTION_ERROR: ... tail: <traceback>",
    # where the front is boilerplate and the actual root cause is the last line of the
    # tail. Truncating from the front only would show a reviewer the error's category
    # and cut off its cause, which is the one thing they came for.
    head = limit * 2 // 3
    return flat[:head] + " ... " + flat[-(limit - head):]


def _fmt_secs(ms: int) -> str:
    """A latency a person reads at a glance, from the milliseconds we record."""
    if ms <= 0:
        return ""
    if ms < 1000:
        return f"{ms}ms"
    secs = ms / 1000.0
    if secs < 90:
        return f"{secs:.0f}s"
    return f"{int(secs // 60)}m{int(secs % 60):02d}s"


def _role_rows(run: Any) -> list[dict[str, Any]]:
    """One row per role that actually ran, in dispatch order.

    Reads `run.progress`, which the engine fills as each role finishes, rather than
    the roster: a role the router did not dispatch has no place in the story.
    """
    rows: list[dict[str, Any]] = []
    progress = getattr(run, "progress", None) or {}
    for agent_id in getattr(run, "agents", []) or []:
        result = progress.get(agent_id)
        if result is None:
            continue
        rows.append({
            "agent": agent_id,
            "role": getattr(result, "role", "") or agent_id,
            "state": getattr(result, "state", "") or "unknown",
            "note": getattr(result, "note", "") or "",
            "latency_ms": int(getattr(result, "latency_ms", 0) or 0),
            "tokens": int(getattr(result, "tokens", 0) or 0),
            "engine": getattr(result, "engine", "") or "",
        })
    return rows


def _check_excerpt(run: Any) -> tuple[str, int] | None:
    """(excerpt, total_lines) of the validator's authored check, or None.

    The check is the most interesting artifact in the whole run and the least
    likely to be read, because a reviewer has to know it exists first. Show its
    head so the PR itself says what "the gate passed" MEANT for this task.
    """
    path = getattr(run, "_acceptance_test_file", None)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if not lines:
        return None
    shown = [ln[:_CHECK_LINE_CHARS] for ln in lines[:_CHECK_HEAD_LINES]]
    return "\n".join(shown), len(lines)


def _rounds(run: Any) -> list[dict[str, Any]]:
    """What happened per round, reconstructed from the run's own event log.

    The engine logs one line per gate execution (`gate: running the validator's
    authored check (round N)`) and the outcome follows it. Rather than parse prose,
    take the structural facts we already have -- how many iterations ran, and what
    the final gate said -- and let the event log supply the per-round reasons that
    are only recorded there.
    """
    out: list[dict[str, Any]] = []
    events = getattr(run, "events", None) or []
    for ev in events:
        msg = str(ev.get("message", ""))
        if msg.startswith("gate: running the validator's authored check"):
            out.append({"at_s": ev.get("elapsed_s", 0), "detail": ""})
        elif out and msg.startswith("gate green"):
            out[-1]["detail"] = "green"
        elif out and msg.startswith("changes requested"):
            out[-1]["detail"] = "changes requested"
    return out


def narrative(run: Any) -> str:
    """The PR body: what this run did, in order, for a reviewer who wasn't there.

    Markdown, and deliberately front-loaded -- GitHub collapses long bodies, so the
    request and the verdict come first and the evidence follows in `<details>`.
    """
    task = (getattr(run, "task", "") or "").strip()
    gate = getattr(run, "gate", None) or {}
    route = getattr(run, "route", None) or {}
    rows = _role_rows(run)

    parts: list[str] = []

    # 1. What was asked. Quoted verbatim: the attendee's own words are the spec,
    #    and paraphrasing them here would be the engine pretending to understand
    #    the request.
    parts.append("## What was requested\n")
    parts.append("> " + (task.replace("\n", "\n> ") if task else "(no task recorded)"))

    # 2. Who did it. The roster is not fixed (WORKSHOP_ROLES selects it), so this
    #    is generated from the roles that ran, never from a literal.
    parts.append("\n## How it was built\n")
    if rows:
        parts.append("| role | agent | outcome | took |")
        parts.append("|---|---|---|---|")
        for r in rows:
            took = _fmt_secs(r["latency_ms"]) or "-"
            note = _cell(r["note"] or r["state"])
            parts.append(f"| {_cell(r['role'], 40)} | `{_cell(r['agent'], 40)}` "
                         f"| {note} | {took} |")
    else:
        parts.append("_No role results were recorded for this run._")

    preset = route.get("preset")
    if preset:
        parts.append(f"\nRouted as `{preset}`"
                     + (f" ({route['rule']})" if route.get("rule") else "")
                     + ".")

    # 3. What was PROVEN, and by what. This is the section that earns the PR its
    #    trust, so it is explicit that the check was authored for this task and
    #    executed for real, and that its exit code is the whole gate.
    parts.append("\n## What proved it\n")
    passed = bool(gate.get("passed"))
    summary = (gate.get("summary") or "").strip()
    parts.append(
        f"The acceptance gate {'PASSED' if passed else 'FAILED'}"
        + (f": `{summary}`" if summary else "")
        + ".")
    parts.append(
        "\nThat gate is not a fixed test suite. A separate validator agent read "
        "this request, wrote one self-contained executable check for it, and the "
        "orchestrator ran that file and used its real exit code as the verdict. "
        "Nothing in the system holds a reference answer for this task, so the "
        "check below is the only definition of \"correct\" this run had."
    )
    hit = _check_excerpt(run)
    if hit:
        excerpt, total = hit
        more = (f"\n\n_(first {_CHECK_HEAD_LINES} of {total} lines; the full check "
                "ships in this pull request so you can re-run the exact gate.)_"
                if total > _CHECK_HEAD_LINES else
                "\n\n_(the full check ships in this pull request.)_")
        parts.append(f"\n<details><summary>The check that ran</summary>\n\n"
                     f"```\n{excerpt}\n```{more}\n\n</details>")

    # 3b. Where two roles wrote the SAME path. Compose keeps one copy at the root
    #     and commits the other under CONFLICT-<role>/, which shows up in the diff
    #     as a directory a reviewer cannot account for. A live PR shipped
    #     CONFLICT-opencode/server.py with no mention of it anywhere, and the
    #     reviewing agent called the difference cosmetic instead of noticing two
    #     competing servers. Reported, never judged: this says what happened and
    #     leaves whether it matters to the reviewer.
    conflicts = list(getattr(run, "compose_conflicts", None) or [])
    if conflicts:
        n_c = len(conflicts)
        parts.append(f"\n## {n_c} file{'' if n_c == 1 else 's'} written by more "
                     "than one role\n")
        parts.append(
            "Two roles wrote the same path with different content. The engine kept "
            "the more recently written copy at the root (the one the gate above "
            "actually ran against) and committed the other under a `CONFLICT-` "
            "directory so nothing is hidden from you. **Worth a look: the roles may "
            "have duplicated work, and only the root copy was graded.**"
        )
        parts.append("\n| path | kept from | also written by | the other copy |")
        parts.append("|---|---|---|---|")
        for c in conflicts[:10]:
            parts.append(
                f"| `{_cell(c.get('path', '?'), 60)}` "
                f"| `{_cell(c.get('kept_from') or '?', 40)}` "
                f"| `{_cell(c.get('also_written_by') or '?', 40)}` "
                f"| `{_cell(c.get('flagged_under', '?'), 70)}` |")
        if len(conflicts) > 10:
            parts.append(f"\n_({len(conflicts) - 10} more not listed.)_")

    # 4. The loop, but only when there WAS a loop. A one-round run saying
    #    "1 round" is noise; a second round is the interesting event and needs its
    #    reason attached.
    iterations = int(getattr(run, "iterations", 0) or 0)
    if iterations > 1:
        parts.append(f"\n## Why there were {iterations} rounds\n")
        parts.append(
            "The first round did not end approved, so the same roles were sent "
            "back with the reasons as feedback and this branch was updated in "
            "place (a re-implement round pushes new commits to the same pull "
            "request rather than opening another one)."
        )
        # `retry_reasons` and NOT `review`: review holds only the latest verdict, so on
        # a run that ended green it carries the APPROVAL notes. A live 2-round run
        # printed those under "what came back as feedback", which states the opposite
        # of what happened.
        for entry in (getattr(run, "retry_reasons", None) or []):
            summary = entry.get("gate_summary") or ""
            parts.append(f"\nAfter round {entry.get('round', '?')}"
                         + (f", the check reported `{summary}`" if summary else "")
                         + ", the roles were sent back with:\n")
            reasons = entry.get("reasons") or []
            if reasons:
                for reason in reasons[:5]:
                    parts.append(f"- {reason}")
            else:
                parts.append("- (no specific reasons were recorded for this round)")
        for i, rnd in enumerate(_rounds(run), start=1):
            if rnd.get("detail"):
                parts.append(f"\n- round {i}: gate {rnd['detail']}"
                             + (f" (at {rnd['at_s']}s)" if rnd.get("at_s") else ""))

    # 5. What a reviewer should do. The assessment lands as a separate comment
    #    (the App installation cannot APPROVE its own PR), so say where to look.
    parts.append("\n## What happens next\n")
    parts.append(
        "A reviewer agent assesses this pull request and posts its verdict as a "
        "comment below. An approving assessment ends with a literal token; a "
        "changes-requested assessment sends the roles back around the loop once "
        "and then hands off to a human. Merging is a human's decision unless this "
        "workshop was explicitly switched to auto-merge, which never targets the "
        "default branch."
    )
    parts.append(f"\n<sub>{getattr(run, 'run_id', 'run')} - built by a coding-agent "
                 "team on Amazon Bedrock AgentCore. This body is generated from the "
                 "run's own record; it reports the loop, it does not judge it.</sub>")
    return "\n".join(parts) + "\n"


def round_comment(run: Any) -> str:
    """The comment a re-implement round posts, since a PR body is write-once.

    The gateway exposes no `update_pull_request`, so the story of round 2 cannot go
    back into the body that round 1 wrote. It goes on the timeline instead, which is
    arguably where an update belongs anyway.
    """
    gate = getattr(run, "gate", None) or {}
    summary = (gate.get("summary") or "").strip()
    # The reasons that CAUSED this round, not whatever the newest verdict says (see
    # narrative(): `review` is overwritten each round).
    history = getattr(run, "retry_reasons", None) or []
    reasons = (history[-1].get("reasons") or []) if history else []
    lines = [f"### Round {getattr(run, 'iterations', '?')}: this branch was updated\n",
             "The previous round was not approved, so the same roles ran again with "
             "the review feedback and pushed new commits to this branch.\n"]
    if reasons:
        lines.append("What they were told to fix:\n")
        lines += [f"- {r}" for r in reasons[:5]]
        lines.append("")
    lines.append(f"The re-authored acceptance check "
                 f"{'passed' if gate.get('passed') else 'did not pass'}"
                 + (f": `{summary}`" if summary else "") + ".")
    return "\n".join(lines) + "\n"


def as_json(run: Any) -> str:
    """The same facts as data, for the diagnostic bundle. Never used on the PR."""
    return json.dumps({
        "run_id": getattr(run, "run_id", ""),
        "task": getattr(run, "task", ""),
        "status": getattr(run, "status", ""),
        "iterations": getattr(run, "iterations", 0),
        "roles": _role_rows(run),
        "gate": {"passed": bool((getattr(run, "gate", None) or {}).get("passed")),
                 "summary": (getattr(run, "gate", None) or {}).get("summary", "")},
        "pr_url": getattr(run, "pr_url", None),
    }, indent=2)
