"""Bounded quotations from check output, never a source of gate verdicts."""

from __future__ import annotations

MAX_FAILURE_LINES = 25
MAX_FAILURE_LINE_CHARS = 300


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
        if upper.startswith(("FAIL", "ASSERT", "ERROR", "NOT OK", "✗", "×")) or (
                "FAILED" in upper and "0 FAILED" not in upper):
            hits.append(raw[:MAX_FAILURE_LINE_CHARS])
        if len(hits) >= MAX_FAILURE_LINES:
            break
    return hits


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
