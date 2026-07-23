"""The reviewer: a separate review pen whose verdict lands ON the pull request.

The build engine never approves its own work. This module owns the verdict,
in two layers that make one loop:

  * The ACCEPTANCE GATE is dynamic, not pinned. The validator role AUTHORS an
    EXECUTABLE acceptance test for each deliverable (loop-engineering: the
    checker writes a runnable check for the maker's work) and the gate RUNS
    that executable; its real exit code decides. Nothing here assumes the
    deliverable's language, the test's language, or a test framework: the
    authored executable (shebang line, any interpreter in the container)
    probes the live endpoint over the wire and exits 0 to accept. Offline
    (fixture executor, no deployed validator) the usecase's shipped grading
    contract runs in-process as the floor. A red gate can never pass, and
    nothing fabricates a verdict.
  * The LLM ASSESSMENT reviews the artifacts the way a senior engineer reviews
    a pull request, and the engine posts it DIRECTLY on the GitHub PR as an
    Assessment comment (approve / request changes). It is FAIL-OPEN: with no
    model reachable it abstains and the gate stands. It can withhold approval
    on a green gate; it can never turn a red gate green.

Approve ends the run (the auto merge policy may then squash-merge). Request
changes loops: the engine re-dispatches the routed roles with the assessment's
reasons as feedback and UPDATES THE SAME pull request, bounded by
``MAX_REVIEW_ROUNDS``, then hands to a human. The exact pass token
``LGTM: no changes needed`` closes an approving assessment, so approval is a
literal, checkable string, never a vibe.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))

LGTM_TOKEN = "LGTM: no changes needed"   # the exact pass token, kept verbatim
# One bounded re-implement pass, then a human. THE SINGLE SOURCE OF TRUTH for
# the bound: the engine derives its iteration cap from this (cap = rounds + 1),
# so editing this number actually changes behavior.
MAX_REVIEW_ROUNDS = 1

_RUN_BRANCH = re.compile(r"^run/(run_[0-9]{6}_[0-9]{3})$")

# Grading contracts are per-usecase modules that share names (adapters, contract).
# All imports of them go through load_grading() under this lock so two concurrent
# runs on different usecases never cross-import each other's contract.
_IMPORT_LOCK = threading.Lock()


def load_grading(grading_dir: str):
    """Import (grade, InProcessClient, RemoteMCPClient) from a usecase's grading dir."""
    with _IMPORT_LOCK:
        for stale in ("adapters", "contract"):
            sys.modules.pop(stale, None)
        if grading_dir in sys.path:
            sys.path.remove(grading_dir)
        sys.path.insert(0, grading_dir)
        import adapters  # noqa: PLC0415
        import contract  # noqa: PLC0415
        return contract.grade, adapters.InProcessClient, adapters.RemoteMCPClient


def branch_run_id(branch: str | None) -> str | None:
    """Map a branch name back to the exact run that produced it, or None.

    A strict branch-suffix guard: the engine always branches as
    ``run/<run_id>`` and run ids match a strict pattern, so anything else,
    including SQL-LIKE-wildcard or lookalike branches, refuses to match rather
    than falling back to a most-recent heuristic.
    """
    if not branch:
        return None
    m = _RUN_BRANCH.match(branch)
    return m.group(1) if m else None


@dataclass
class Verdict:
    """The judge's structured output for one round."""

    state: str = "in_review"        # in_review | approved | changes_requested
    gate: dict | None = None        # the acceptance-gate result (real execution)
    assessment: str = ""            # the Assessment markdown posted on the PR
    reasons: list[str] = field(default_factory=list)  # feedback for the loop
    lgtm: bool = False
    round: int = 1

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "lgtm": self.lgtm, "round": self.round,
                "gate": self.gate, "reasons": self.reasons,
                "assessment": self.assessment}


# ------------------------------------------------------------------ the gate
def run_gate(run: Any, grading_dir: str) -> dict:
    """The deterministic acceptance gate: run the authored executable, read its
    real exit code.

    Shipped path: the validator role authored an EXECUTABLE acceptance test for
    THIS deliverable (``run._acceptance_test_file``); execute it against the
    live endpoint (``MCP_ENDPOINT_URL`` in its env) and read its exit code.
    The authored test is the acceptance authority: any language, any shape, as
    long as it runs and exits 0 to accept. No test framework is assumed and no
    contract pinned in the repo is consulted.

    Offline floor (fixture executor / no deployed validator): the usecase's
    shipped grading contract runs IN-PROCESS over the wire adapter. Either way
    a real execution decides and a red run can never be presented as a pass.
    The gate dict's ``summary`` field carries the one-line outcome.
    """
    endpoint = getattr(run, "artifact_endpoint", "") or ""
    env = {**os.environ, "MCP_ENDPOINT_URL": endpoint}

    authored = getattr(run, "_acceptance_test_file", None)
    if authored and os.path.isfile(authored):
        try:
            os.chmod(authored, os.stat(authored).st_mode | 0o755)
        except OSError:
            pass
        proc = subprocess.run([authored], capture_output=True, text=True,
                              env=env, timeout=90, cwd=os.path.dirname(authored))
        tail = (proc.stdout or proc.stderr).strip().splitlines()
        summary = tail[-1] if tail else f"exit {proc.returncode}"
        return {
            "passed": proc.returncode == 0,
            "checks": [{"check": "acceptance_test_authored",
                        "passed": proc.returncode == 0,
                        "detail": ("validator-authored acceptance test passed "
                                   "against the live endpoint" if proc.returncode == 0
                                   else f"validator-authored acceptance test failed "
                                        f"(exit {proc.returncode}): {summary}")}],
            "summary": summary}

    grade, _, RemoteMCPClient = load_grading(grading_dir)
    try:
        graded = grade(RemoteMCPClient(endpoint))
    except Exception as exc:
        graded = {"passed": False,
                  "checks": [{"check": "endpoint_reachable", "passed": False,
                              "detail": f"{type(exc).__name__}: {exc}"}]}
    n_green = sum(1 for c in graded["checks"] if c.get("passed"))
    return {"passed": bool(graded["passed"]),
            "checks": list(graded["checks"]),
            "summary": f"{n_green}/{len(graded['checks'])} contract checks green"}


# ------------------------------------------------------- the LLM assessment
# The judge model is wirable (same surface as the orchestrator's own model id),
# defaulting to a fast mid-tier Claude; the review is a read, not a build.
JUDGE_MODEL = os.environ.get("WORKSHOP_REVIEW_MODEL", "claude-sonnet-4-6")

_JUDGE_SYSTEM = (
    "You are a meticulous senior engineer reviewing a pull request opened by a "
    "multi-agent coding system. The deliverable ALREADY passed a deterministic "
    "acceptance test (authored by a separate validator agent and executed for "
    "real); you respect that gate and never contradict it. Your job is what "
    "tests miss: wrong logic despite green tests, security problems, dead or "
    "copied code, and real quality defects. You do NOT rewrite code.\n"
    "Reply with STRICT JSON only:\n"
    '{"approve": true|false, "reasons": ["..."], "assessment": "<markdown>"}\n'
    "The assessment markdown is the review COMMENT posted on the PR. Format it "
    "exactly like a human bot review:\n"
    "**Assessment**: Approve   (or: Request changes)\n\n"
    "<one short paragraph: what the change does and why it is (not) shippable>\n\n"
    "<details><summary>Review notes</summary>\n\n- bullet per finding with a "
    "verdict emoji\n\n</details>\n"
    "Be decisive; approve when the gate is green and you see no real defect."
)


def _default_judge(run: Any, gate: dict) -> dict | None:
    """The LLM judge: one model call over the artifacts + gate result.

    FAIL-OPEN: returns ``None`` (abstain) whenever a model cannot be reached
    (no credentials, no access, or any transport error), so offline/unit runs
    behave exactly like the deterministic gate. Returns
    ``{"approve": bool, "reasons": [...], "assessment": md}`` when it ran.
    """
    try:
        import llm  # noqa: PLC0415 (lazy; offline tests never import boto3)
    except Exception:
        return None
    if not llm.available():
        return None

    parts: list[str] = [f"Task: {getattr(run, 'task', '')!r}",
                        f"acceptance gate passed: {gate.get('passed')}",
                        f"gate: {json.dumps(gate.get('checks', []))[:2000]}"]
    # Hand the judge every deliverable artifact it can read: the backend server,
    # the authored acceptance test, and each file of the UI project.
    for label, path in _artifact_files(run):
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                parts.append(f"--- {label} ---\n{f.read()[:6000]}")
    prompt = ("Review this pull request's deliverable and decide whether it is "
              "correct and shippable.\n\n" + "\n\n".join(parts))
    try:
        out = llm.invoke(JUDGE_MODEL, prompt, system=_JUDGE_SYSTEM, max_tokens=1500)
    except Exception:
        return None  # fail-open: a judge outage never blocks the deterministic gate
    text = (out.get("text") or "").strip()
    try:
        start, end = text.find("{"), text.rfind("}")
        parsed = json.loads(text[start:end + 1]) if start != -1 and end != -1 else {}
    except Exception:
        return None
    if not isinstance(parsed, dict) or "approve" not in parsed:
        return None
    return {"approve": bool(parsed.get("approve")),
            "reasons": [str(r) for r in (parsed.get("reasons") or [])][:5],
            "assessment": str(parsed.get("assessment") or "").strip()}


def _artifact_files(run: Any) -> list[tuple[str, str]]:
    """(label, path) for every reviewable artifact the run produced."""
    files: list[tuple[str, str]] = []
    server = getattr(run, "_server_file", None)
    if server:
        files.append(("backend server (mcp_server.py)", server))
    authored = getattr(run, "_acceptance_test_file", None)
    if authored:
        files.append(("validator-authored acceptance_test.py", authored))
    ui_dir = getattr(run, "_ui_dir", None)
    if ui_dir and os.path.isdir(ui_dir):
        for dp, dns, fns in os.walk(ui_dir):
            dns[:] = sorted(d for d in dns if not d.startswith("."))
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, ui_dir)
                files.append((f"ui/{rel}", full))
    else:
        page = getattr(run, "_chatbot_file", None)
        if page:
            files.append(("ui page", page))
    return files


def _abstained_assessment(gate: dict, approve: bool) -> str:
    """The deterministic assessment used when the LLM judge abstains: a short,
    honest summary of the gate. Never invents review findings."""
    line = gate.get("summary") or ("green" if gate.get("passed") else "red")
    if approve:
        return ("**Assessment**: Approve\n\n"
                f"The validator-authored acceptance test passed ({line}). "
                "No LLM reviewer was reachable, so the deterministic gate stands "
                "as the verdict.")
    return ("**Assessment**: Request changes\n\n"
            f"The acceptance gate is red ({line}); see the failing checks. "
            "A red gate can never be approved.")


def assess(run: Any, gate: dict, round_no: int,
           judge: Any = _default_judge) -> Verdict:
    """One review round: take the gate result, layer the LLM assessment, and
    return the verdict whose markdown the engine posts on the PR.

    The ``judge`` is injectable (tests pass a fake or ``None`` to disable it);
    it defaults to the real LLM judge, which is FAIL-OPEN, so a missing model
    never changes the deterministic verdict. A red gate is never assessed as
    approvable; a green gate may still get changes requested.
    """
    verdict = Verdict(round=round_no, gate=gate)
    if not gate.get("passed"):
        verdict.lgtm = False
        verdict.state = "changes_requested"
        verdict.reasons = [c["detail"] for c in gate.get("checks", [])
                           if not c.get("passed")][:5]
        verdict.assessment = _abstained_assessment(gate, approve=False)
        return verdict

    jv = None
    if judge is not None:
        try:
            jv = judge(run, gate)
        except Exception:
            jv = None  # fail-open at the call site too
    if jv is None:
        verdict.lgtm = True
        verdict.assessment = _abstained_assessment(gate, approve=True)
    else:
        verdict.lgtm = bool(jv.get("approve"))
        verdict.reasons = list(jv.get("reasons") or [])
        verdict.assessment = (jv.get("assessment")
                              or _abstained_assessment(gate, verdict.lgtm))
    verdict.state = "approved" if verdict.lgtm else "changes_requested"
    if verdict.lgtm and LGTM_TOKEN not in verdict.assessment:
        # Approval is a literal, checkable token, never a paraphrase.
        verdict.assessment = verdict.assessment.rstrip() + f"\n\n{LGTM_TOKEN}\n"
    return verdict
