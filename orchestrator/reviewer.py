"""The reviewer: a separate review pen whose verdict lands ON the pull request.

The build engine never approves its own work. This module owns the verdict,
in two layers that make one loop:

  * The ACCEPTANCE GATE is AGENTIC ONLY. The validator role decides what
    "acceptable" means for the task in front of it and AUTHORS one executable
    check (loop-engineering: the checker writes a runnable check for the maker's
    work); the gate RUNS that executable and its real exit code decides.
    Nothing here assumes the deliverable's language, the check's language, or a
    test framework, and no contract pinned in this repository is ever consulted.
    No authored check means no pass: there is no fallback grade, because a
    fallback would be this repository deciding correctness, which is the thing
    the design forbids. A red gate can never pass, and nothing fabricates a
    verdict.
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
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))

LGTM_TOKEN = "LGTM: no changes needed"   # the exact pass token, kept verbatim
# One bounded re-implement pass, then a human. THE SINGLE SOURCE OF TRUTH for
# the bound: the engine derives its iteration cap from this (cap = rounds + 1),
# so editing this number actually changes behavior.
MAX_REVIEW_ROUNDS = 1

_RUN_BRANCH = re.compile(r"^run/(run_[0-9]{6}_[0-9]{3})$")


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
GATE_TIMEOUT_S = 120
# How long a killed check's group gets to die before we stop waiting on it. Short on
# purpose: this runs on the verdict path, and a wedged reap must not hold up the run.
_GROUP_REAP_S = 5


def _kill_process_group(pgid: int | None) -> None:
    """Tear down the check's whole process group (SIGTERM, then SIGKILL).

    The group is the unit because the check may have STARTED THE DELIVERABLE as a
    child of itself, and a service process never exits on its own. Reaping only the
    direct child would leave that service running after every single run.

    Takes the PGID, not the Popen, and that is the load-bearing detail: once the
    direct child has been waited on, its pid is reaped and ``os.getpgid(pid)`` raises
    ProcessLookupError, so a group looked up too late resolves to nothing and the
    surviving service is missed entirely. The caller captures the pgid BEFORE waiting.

    Never raises: the verdict is already decided by the time this runs, and a cleanup
    failure must not turn a real exit code into an exception.
    """
    if pgid is None:
        return
    import signal  # noqa: PLC0415 (only needed on this path)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return          # already gone (or not ours): nothing left to reap
        # Give the group a moment to die on the gentler signal before escalating.
        deadline = time.monotonic() + _GROUP_REAP_S
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)          # probe: does anything still live here?
            except OSError:
                return                      # the whole group is gone
            time.sleep(0.05)


def run_gate(run: Any) -> dict:
    """The acceptance gate: run the check the VALIDATOR ROLE authored, read its
    real exit code. That is the whole gate.

    Validation here is AGENTIC ONLY. The validator decided what "acceptable"
    means for this task and wrote one self-contained executable to prove it; this
    function executes that file and reports its exit code. Nothing in this
    repository knows what the deliverable does, what language it is in, or what a
    correct answer looks like, so there is no pinned contract to consult and no
    stand-in grade to fall back to.

    The executable is run from its own directory with three things in its
    environment, and nothing else the engine knows: ``WORKSHOP_WORK_DIR`` (the tree
    the builders wrote, so a check can inspect files), ``WORKSHOP_TASK`` (the request,
    so a check can re-read what was asked), and ``DELIVERABLE_URL`` (a live URL when
    one exists, with ``MCP_ENDPOINT_URL`` kept as a compatible alias). None of them
    tells the check what to verify.

    Fail-loud, with no exceptions: no authored check means NO PASS. A run that
    reaches the gate without one is a red gate with a reason, never a courtesy
    pass and never a substituted verdict.
    """
    authored = getattr(run, "_acceptance_test_file", None)
    if not authored or not os.path.isfile(authored):
        return {
            "passed": False,
            "checks": [{"check": "acceptance_check_authored", "passed": False,
                        "detail": "the validator role produced no acceptance check, "
                                  "so nothing proved this deliverable; validation is "
                                  "agentic only, and there is no fallback grade"}],
            "summary": "no validator-authored acceptance check to run"}

    url = getattr(run, "artifact_endpoint", "") or ""
    # A read-only review run inspects ANOTHER run's work, so the check must be
    # pointed at that tree rather than this run's empty one.
    work_dir = getattr(run, "_review_work_dir", "") or getattr(run, "workdir", "") or ""
    env = {**os.environ,
           "WORKSHOP_WORK_DIR": work_dir,
           "WORKSHOP_TASK": getattr(run, "task", "") or "",
           "DELIVERABLE_URL": url, "MCP_ENDPOINT_URL": url}
    try:
        os.chmod(authored, os.stat(authored).st_mode | 0o755)
    except OSError:
        pass
    # Run the check in its OWN PROCESS GROUP, and tear that whole group down when it
    # is over. This is load-bearing, not tidiness: the validator is told to START the
    # deliverable if it needs to be running, so the check routinely spawns a service
    # as a CHILD of itself. `subprocess.run` reaps only the direct child, so a service
    # the check left running (or everything it spawned, when the check times out and
    # only IT is killed) would survive the gate forever. A server process never exits
    # on its own, so unbounded that is one orphan per run: the failure mode that used
    # to wedge the box with thousands of leaked processes. Killing the group means the
    # engine still needs to know nothing about what the check started.
    # Output goes to a FILE, not a pipe, and we wait on the PROCESS, not on end-of-
    # output. That distinction is the whole correctness of this gate: a pipe is only
    # closed when every writer lets go of it, and a service the check started inherits
    # that pipe, so reading to EOF would block until the SERVICE exits (it never
    # does). Waiting on the pipe therefore turned an honest `exit 0` into a timeout,
    # which is a RED gate on a passing deliverable: the worst failure this file could
    # have, since the check that gets punished is exactly the one that followed its
    # instructions and started the thing it was asked to probe.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
        pgid = None
        try:
            proc = subprocess.Popen([authored], stdout=sink, stderr=subprocess.STDOUT,
                                    env=env, cwd=os.path.dirname(authored),
                                    start_new_session=True)
            # Capture the group NOW: after proc.wait() the pid is reaped and the group
            # can no longer be resolved from it, which would leave a started service
            # running with nothing to kill it.
            pgid = os.getpgid(proc.pid)
        except OSError as exc:
            # Not executable, bad interpreter, etc. A check that cannot run is a red
            # gate: the deliverable is unproven, which is exactly what red means.
            code, out = 126, f"could not execute the authored check: {exc}"
        else:
            try:
                code = proc.wait(timeout=GATE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                code = 124
            finally:
                # Always tear the group down: on a timeout to stop a hung check, and on
                # success to stop whatever it left running.
                _kill_process_group(pgid)
            sink.seek(0)
            out = sink.read()
            if code == 124:
                out = (f"{out}\nthe acceptance check did not finish within "
                       f"{GATE_TIMEOUT_S}s and was killed")

    tail = (out or "").strip().splitlines()
    summary = tail[-1] if tail else f"exit {code}"
    return {
        "passed": code == 0,
        "checks": [{"check": "acceptance_check_authored", "passed": code == 0,
                    "detail": ("the validator's authored check passed against the "
                               "live deliverable" if code == 0 else
                               f"the validator's authored check FAILED (exit {code}): "
                               f"{summary}")}],
        "summary": summary,
        "output": (out or "")[-4000:]}


# ------------------------------------------------------- the LLM assessment
# The judge model is wirable (same surface as the orchestrator's own model id),
# defaulting to a fast mid-tier Claude; the review is a read, not a build.
JUDGE_MODEL = os.environ.get("WORKSHOP_REVIEW_MODEL", "claude-sonnet-4-6")

_JUDGE_SYSTEM = (
    "You are a meticulous senior engineer reviewing a pull request opened by a "
    "multi-agent coding system. The deliverable ALREADY passed an acceptance check "
    "that a separate validator agent wrote for this specific task and that was "
    "executed for real; you respect that result and never contradict it. Your job "
    "is what a check like that misses: wrong logic despite a green run, security "
    "problems, dead or copied code, work that does not actually answer the request, "
    "and real quality defects. You do NOT rewrite code.\n"
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
    behave exactly like the gate alone. Returns
    ``{"approve": bool, "reasons": [...], "assessment": md}`` when it ran.
    """
    # A run whose work came from the offline test double has nothing to review: the
    # files say so themselves. Abstaining is the honest answer, and it keeps the
    # offline suite from asserting a live judge's opinion of a stub. A real dispatch
    # never sets this, so the shipped path always gets a real review.
    if getattr(run, "_offline_double", False):
        return None
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
