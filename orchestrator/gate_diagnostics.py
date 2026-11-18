"""Bounded quotations from check output, never a source of gate verdicts."""

from __future__ import annotations

import re

MAX_FAILURE_LINES = 25
MAX_FAILURE_LINE_CHARS = 300
_EXCEPTION_LINE = re.compile(
    r"^[\w.]*(?:Error|Exception)(?:\s*\[[^\]]+\])?\s*:", re.IGNORECASE)
# One line per check is the authored check's own contract ("print one line per check
# so a human can read what you verified"). Counted, never read as a verdict.
_CHECK_LINE = re.compile(r"^\s*(?:PASS|FAIL|OK|NOT OK)\b|^\s*[\u2713\u2714\u2717\u2718\u00d7]",
                         re.IGNORECASE)


def extract_failure_lines(output: str) -> list[str]:
    """Keep the first failure-style lines in their original wording and order.

    These are the repair prompt's existing heuristics, not a required check
    format. Match stripped text but preserve the captured line, up to the bound.
    """
    hits: list[str] = []
    for raw in (output or "").splitlines():
        upper = raw.strip().upper()
        if not upper or upper.startswith(("PASS", "OK", "INFO", "SKIP")):
            continue
        if (upper.startswith(("FAIL", "ASSERT", "ERROR", "NOT OK", "✗", "×"))
                or ("FAILED" in upper and "0 FAILED" not in upper)
                or _EXCEPTION_LINE.match(raw.strip())):
            hits.append(raw[:MAX_FAILURE_LINE_CHARS])
        if len(hits) >= MAX_FAILURE_LINES:
            break
    return hits


def count_check_lines(output: str) -> int:
    """How many verdict lines (PASS/FAIL/ok/not ok, or a check mark) a run printed.

    Recorded beside the gate so a corrected check can be compared with the one it
    replaced: a correction that runs fewer checks has moved the bar, and is refused.
    It only ever keeps a red result red; it never decides a pass.
    """
    return sum(1 for line in (output or "").splitlines() if _CHECK_LINE.match(line))


def gate_failure_lines(gate: dict) -> list[str]:
    """Read saved diagnostics, falling back to the output of older records.

    A successful check may print failure words while exercising negative cases.
    Only a recorded red gate has failure diagnostics; the words never decide it.
    Reapply the bounds on read without modifying the recorded evidence.
    """
    if gate.get("passed"):
        return []
    recorded = gate.get("failure_lines")
    if not isinstance(recorded, list):
        return extract_failure_lines(str(gate.get("output") or ""))
    hits = []
    for raw in recorded[:MAX_FAILURE_LINES]:
        if isinstance(raw, str) and raw:
            line = raw[:MAX_FAILURE_LINE_CHARS].splitlines()[0]
            if line.strip():
                hits.append(line)
    return hits
