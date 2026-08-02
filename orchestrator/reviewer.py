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
  * The REVIEW runs one read-only model turn over ONE pull request. That response
    must contain two explicit lenses: adversarial verification tries to falsify the
    green result at runtime and across every seam the change touches, while design
    and integration review checks ownership, operability, and maintainability. A
    finding under either lens requests changes. An unavailable review is recorded
    and stops THAT pull request instead of inventing evidence or sending builders to
    repair an infrastructure failure. The review can withhold approval on a green
    gate; it can never turn a red gate green.

Approve makes that one pull request mergeable. Request changes routes evidence to
the builder that owns it, bounded by ``MAX_REVIEW_ROUNDS`` PER PULL REQUEST, then
hands to a human. The exact pass token ``LGTM: no changes needed`` closes an
approving assessment, so approval is a literal, checkable string, never a vibe.
Each pull request is checked and reviewed against the default branch as it stands,
and merges on its own.
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

# New runs use a random 48-bit suffix so two coordinator sessions started in the
# same second cannot share a branch or workspace. Keep the old form readable so
# existing pull requests remain reviewable.
_RUN_BRANCH = re.compile(
    r"^run/(run_[0-9]{6}_(?:[0-9]{3}|[0-9a-f]{12}))$"
)


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
    panels: list[dict[str, Any]] = field(default_factory=list)
    review_unavailable: bool = False
    lgtm: bool = False
    round: int = 1

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "lgtm": self.lgtm, "round": self.round,
                "gate": self.gate, "reasons": self.reasons,
                "assessment": self.assessment, "panels": self.panels,
                "review_unavailable": self.review_unavailable}


# ------------------------------------------------------------------ the gate
# The wall clock for the authored check. It has to cover the check's own readiness
# poll PLUS every assertion it then makes. A first start may create an environment
# and install declared dependencies. At 120s a check that waits the 60s its steering
# requires had barely a minute left for the actual checks, so the budget itself was
# pushing checks to under-wait.
#
# Wirable through its OWN name, deliberately not WORKSHOP_GATE_TIMEOUT_S (that is what
# the engine HANDS the check; one name for both directions would read as though a
# check could extend its own deadline). Floored at 180s, because a budget below the
# steering's required 60s poll plus real assertions would re-create the bug.
GATE_TIMEOUT_S = max(180, int(os.environ.get("WORKSHOP_GATE_BUDGET_S") or 240))
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


# Lines that are decoration rather than a result: a rule, a box border, a blank. The
# validator writes its own output format, so we cannot ask it for a machine-readable
# verdict, but we can decline to quote its punctuation back to a human.
# Box-drawing characters too: a check that draws its rule with U+2550 is common.
_DIVIDER_CHARS = set("=-_*#~+ \t." + "".join(chr(c) for c in range(0x2500, 0x2580)))

# Lines that belong to the SERVICE the check started, not to the check's own report.
# A check that boots the deliverable and shuts it down cleanly prints its results and
# THEN the server's teardown, so the last content line is the server's. A live run
# published `INFO:     Finished server process [25985]` as its gate summary -- on the
# pull request, in the assessment comment, in run_status and in the ledger -- while the
# check's actual verdict, `TOTAL: 30 checks | PASS: 30 | FAIL: 0`, sat four lines above
# it. Same failure shape as the `====` case below: the one human-readable sentence the
# check produced was replaced by noise around it.
_SERVICE_NOISE_PREFIXES = (
    "info:", "warning:", "error:", "critical:", "debug:",   # uvicorn / logging
    "* running on", "* serving", "press ctrl+c",             # flask / werkzeug
    "listening on", "server listening", "[nodemon]",         # node
)


def _is_service_noise(line: str) -> bool:
    low = line.strip().lower()
    return any(low.startswith(p) for p in _SERVICE_NOISE_PREFIXES)


def _summary_line(out: str, code: int) -> str:
    """The last line of the check's output that actually SAYS something.

    Was `lines[-1]` unconditionally, which is wrong for the most natural way a shell
    check ends: printing a results line and then a `====` rule under it. A live run
    then reported its gate summary as `===============================` -- on the PR
    body, in `run_status`, and in the ledger -- so the one human-readable sentence the
    check produced was replaced by its own underline.

    Scans backwards for the last line with real content and falls back to the raw last
    line (then to the exit code) rather than inventing a verdict.
    """
    lines = (out or "").strip().splitlines()
    if not lines:
        return f"exit {code}"
    # Pass 1: skip decoration AND the started service's own log lines.
    for line in reversed(lines):
        stripped = line.strip()
        if (stripped and not set(stripped) <= _DIVIDER_CHARS
                and not _is_service_noise(stripped)):
            return stripped
    # Pass 2: a check whose entire output looks like service logs still gets quoted
    # rather than replaced by a number we invented.
    for line in reversed(lines):
        stripped = line.strip()
        if stripped and not set(stripped) <= _DIVIDER_CHARS:
            return stripped
    return lines[-1].strip() or f"exit {code}"


def run_gate(check_path: str, work_dir: str, task: str, url: str = "") -> dict:
    """The acceptance gate: run the check the VALIDATOR ROLE authored, read its
    real exit code. That is the whole gate.

    Takes the four facts it needs rather than a whole run, because there is one
    gate PER PULL REQUEST now: each pull request has its own authored check and its
    own tree, so N calls per run is the normal case.

    Validation here is AGENTIC ONLY. The validator decided what "acceptable"
    means for this task and wrote one self-contained executable to prove it; this
    function executes that file and reports its exit code. Nothing in this
    repository knows what the deliverable does, what language it is in, or what a
    correct answer looks like, so there is no pinned contract to consult and no
    stand-in grade to fall back to.

    The executable is run from its own directory with a few facts in its environment,
    and nothing else the engine knows: ``WORKSHOP_WORK_DIR`` (the tree the builders
    wrote, so a check can inspect files), ``WORKSHOP_TASK`` (the request, so a check
    can re-read what was asked), ``WORKSHOP_GATE_TIMEOUT_S`` (how long it has before
    the wall clock kills it, so its own readiness poll can be sized against a real
    budget instead of a guess), and ``DELIVERABLE_URL`` (a live URL when one exists,
    with ``MCP_ENDPOINT_URL`` kept as a compatible alias). None of them tells the
    check WHAT to verify or what a correct answer looks like.

    Fail-loud, with no exceptions: no authored check means NO PASS. A run that
    reaches the gate without one is a red gate with a reason, never a courtesy
    pass and never a substituted verdict.
    """
    authored = check_path
    if not authored or not os.path.isfile(authored):
        return {
            "passed": False,
            "checks": [{"check": "acceptance_check_authored", "passed": False,
                        "detail": "the validator role produced no acceptance check, "
                                  "so nothing proved this deliverable; validation is "
                                  "agentic only, and there is no fallback grade"}],
            "summary": "no validator-authored acceptance check to run"}

    # The check runs from its OWN directory (cwd below), which the engine assembles
    # to hold that pull request's files plus the check, i.e. the exact workspace it
    # was authored in. WORKSHOP_WORK_DIR points at that same directory, so a check
    # that reads the env var and a check that walks its own directory see the SAME
    # tree. They disagreed before, and the env var pointed one level above the files.
    work_dir = work_dir or os.path.dirname(authored)
    # GATE_TIMEOUT_S is a FACT about the environment, not a hint about the verdict, so
    # handing it over changes nothing about what the check decides. Withholding it made
    # checks guess: four live runs in a row failed on a readiness poll the author had
    # budgeted 15-30s while the deliverable was still preparing declared
    # dependencies. Those were red gates on working services, which is the one
    # failure this file must not manufacture.
    env = {**os.environ,
           "WORKSHOP_WORK_DIR": work_dir,
           "WORKSHOP_TASK": task or "",
           "WORKSHOP_GATE_TIMEOUT_S": str(GATE_TIMEOUT_S),
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

    summary = _summary_line(out, code)
    return {
        "passed": code == 0,
        "checks": [{"check": "acceptance_check_authored", "passed": code == 0,
                    "detail": ("the validator's authored check passed against the "
                               "live deliverable" if code == 0 else
                               f"the validator's authored check FAILED (exit {code}): "
                               f"{summary}")}],
        "summary": summary,
        "output": (out or "")[-4000:]}


# ----------------------------------------------------- integrated read-only review
# At an event the backend model is the strongest Bedrock-native model known to be
# enabled, so use it unless the operator explicitly picks a cheaper review model.
# The reviewer is independent from every builder conversation, but it evaluates both
# required lenses in one turn to avoid paying twice for the same candidate context.
_REVIEW_MODEL = (
    os.environ.get("WORKSHOP_REVIEW_MODEL")
    or os.environ.get("WORKSHOP_CLAUDE_MODEL")
    or "claude-sonnet-4-6"
)
INTEGRATED_REVIEW_MODEL = _REVIEW_MODEL

_REVIEW_RESPONSE_CONTRACT = (
    "Reply with STRICT JSON only:\n"
    '{"approve": true|false, "reasons": ["..."], '
    '"work_item_evidence": {"<work id>": "<specific usage evidence>"}, '
    '"adversarial_assessment": "<concise markdown findings>", '
    '"design_assessment": "<concise markdown findings>"}\n'
    "Both assessment fields are required, even when they report no finding. "
    "Set approve=false when EITHER lens finds a material defect. "
    "An approval must identify concrete usage evidence for the work id under "
    "review. Evidence must say how that contribution participates in the running "
    "product; a changed-file list alone is not evidence. If you cannot establish "
    "use, request changes instead of guessing. Be decisive and report only defects "
    "that affect correctness, security, operability, integration, or "
    "maintainability."
)

_INTEGRATED_REVIEW_SYSTEM = (
    "You are the independent, read-only reviewer of ONE pull request. The makers are "
    "not allowed to grade their own work, and you do not reuse a builder "
    "conversation or edit the code. A separate validator-authored executable is "
    "green, but that is evidence rather than permission to approve. Apply BOTH of "
    "these lenses in this one review:\n"
    "1. Adversarial verification: try to falsify the result from the task, the "
    "shared contract, and the files. Trace at least one nontrivial value from this "
    "pull request's code ACROSS EVERY SEAM IT TOUCHES -- both the seams the contract "
    "assigns it and any code already on the base branch that it calls or is called "
    "by. Compare field names, enums, null semantics, errors, and state transitions "
    "on BOTH sides of each seam, using the base-branch files you were given rather "
    "than assuming the other side matches. A mismatch between what this pull request "
    "produces and what its counterpart consumes is a material defect even when the "
    "executable passed, because a check can exercise one side alone. Look for edge "
    "cases, persistence failures, security defects, dead paths, and checks that "
    "prove only existence or build success.\n"
    "2. Design and integration: verify that this pull request stays inside its own "
    "ownership, that what it contributes is actually reachable and used, that it "
    "matches the shared contract, and that it composes with the base branch as it "
    "stands rather than adding a disconnected parallel stack. Assess data ownership, "
    "failure handling, migration compatibility, deployment and restart behavior, "
    "accessibility where relevant, and maintainability at the requested scope.\n"
    "Judge THIS pull request, not whether the whole project is finished: a sibling "
    "role's pull request may still be open, and its absence is not a defect in this "
    "one. Do not demand a particular framework, filename, or layout.\n\n"
    + _REVIEW_RESPONSE_CONTRACT
)

class _JudgeEvidenceError(ValueError):
    """A reachable judge tried to approve without proving every contribution."""


def _builder_work_ids(run: Any) -> list[str]:
    return [
        str(item.work_id)
        for item in (getattr(run, "work_items", None) or {}).values()
        if getattr(item, "kind", "") == "builder"
    ]


def _parse_judge_response(text: str, required_work_ids: list[str]) -> dict:
    """Validate reviewer JSON and make its per-work-item evidence visible on the PR."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("reviewer response contains no JSON object")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict) or "approve" not in parsed:
        raise ValueError("reviewer response has no approve verdict")

    raw_evidence = parsed.get("work_item_evidence")
    evidence = {
        str(work_id): str(detail).strip()
        for work_id, detail in (
            raw_evidence.items() if isinstance(raw_evidence, dict) else []
        )
        if str(detail).strip()
    }
    approve = bool(parsed.get("approve"))
    missing = [
        work_id for work_id in required_work_ids
        if not evidence.get(work_id)
    ]
    if approve and missing:
        raise _JudgeEvidenceError(
            "approval omitted concrete integration evidence for "
            + ", ".join(missing)
        )

    adversarial = str(parsed.get("adversarial_assessment") or "").strip()
    design = str(parsed.get("design_assessment") or "").strip()
    missing_lenses = [
        label for label, value in (
            ("adversarial_assessment", adversarial),
            ("design_assessment", design),
        )
        if not value
    ]
    if missing_lenses:
        raise ValueError(
            "reviewer response omitted required lens: "
            + ", ".join(missing_lenses)
        )

    assessment = (
        "#### Adversarial verification\n\n"
        f"{adversarial}\n\n"
        "#### Design & integration\n\n"
        f"{design}"
    )
    if evidence:
        rows = [
            f"- `{work_id}`: {evidence[work_id]}"
            for work_id in required_work_ids
            if evidence.get(work_id)
        ]
        rows.extend(
            f"- `{work_id}`: {detail}"
            for work_id, detail in evidence.items()
            if work_id not in required_work_ids
        )
        assessment = (
            assessment.rstrip()
            + "\n\n<details><summary>Integration evidence</summary>\n\n"
            + "\n".join(rows)
            + "\n\n</details>"
        ).strip()

    return {
        "approve": approve,
        "reasons": [str(r) for r in (parsed.get("reasons") or [])][:5],
        "work_item_evidence": evidence,
        "adversarial_assessment": adversarial,
        "design_assessment": design,
        "assessment": assessment,
    }


def _review_record(model: str, *, state: str = "abstained",
                   note: str = "") -> dict[str, Any]:
    return {
        "name": "integrated",
        "label": "Integrated review",
        "state": state,
        "model": model,
        "reasons": [],
        "assessment": "",
        "lenses": {},
        "note": note,
    }


def _run_review_turn(
    llm_module: Any,
    *,
    model: str,
    base_prompt: str,
    required_work_ids: list[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run one integrated review turn with one bounded response repair."""
    record = _review_record(model)
    prior_text = ""
    prior_error = ""
    had_response = False
    for attempt in range(2):
        prompt = base_prompt
        if attempt:
            if had_response:
                prompt += (
                    "\n\nYOUR PREVIOUS REVIEW RESPONSE WAS INCOMPLETE.\n"
                    f"Validation error: {prior_error}\n"
                    "Return the complete strict JSON object with both required "
                    "assessment fields. An approval also needs "
                    "specific usage evidence for every one of these work ids: "
                    f"{json.dumps(required_work_ids)}.\n\n"
                    f"Previous response:\n{prior_text}"
                )
            else:
                prompt += (
                    "\n\nTHE FIRST MODEL CALL DID NOT COMPLETE.\n"
                    "Retry the review from the supplied evidence and return only "
                    "the complete strict JSON object."
                )
        try:
            out = llm_module.invoke(
                model, prompt, system=_INTEGRATED_REVIEW_SYSTEM, max_tokens=3200)
        except Exception as exc:  # noqa: BLE001 (record the model boundary)
            prior_error = f"model invocation unavailable ({type(exc).__name__})"
            if not attempt:
                continue
            record["note"] = prior_error
            return record, None

        record["model"] = str(out.get("model_id") or model)
        prior_text = (out.get("text") or "").strip()
        had_response = True
        try:
            parsed = _parse_judge_response(prior_text, required_work_ids)
        except ValueError as exc:
            prior_error = str(exc)
            if not attempt:
                continue
            if isinstance(exc, _JudgeEvidenceError):
                parsed = {
                    "approve": False,
                    "reasons": [prior_error],
                    "work_item_evidence": {},
                    "assessment": (
                        "#### Adversarial verification\n\n"
                        "The review could not prove this pull request's "
                        "contribution participates in the running behavior.\n\n"
                        "#### Design & integration\n\n"
                        "Concrete usage evidence was missing, so this pull request "
                        "cannot be approved."
                    ),
                }
            else:
                record["note"] = (
                    "response remained invalid after one repair attempt")
                return record, None

        record["state"] = (
            "approved" if parsed.get("approve") else "changes_requested")
        record["reasons"] = list(parsed.get("reasons") or [])
        record["assessment"] = str(parsed.get("assessment") or "")
        record["lenses"] = {
            "adversarial": str(parsed.get("adversarial_assessment") or ""),
            "design": str(parsed.get("design_assessment") or ""),
        }
        return record, parsed
    return record, None  # pragma: no cover - both attempts return above


def _combine_review(
    gate: dict,
    record: dict[str, Any],
    decision: dict[str, Any] | None,
) -> dict[str, Any]:
    """Turn the single structured review into the public verdict shape."""
    unavailable = record.get("state") == "abstained" or decision is None
    approve = bool(
        decision is not None
        and not unavailable
        and decision.get("approve")
    )

    reasons = [
        f"integrated: {reason}"
        for reason in ((decision or {}).get("reasons") or [])
    ][:8]
    if unavailable:
        reasons.append(
            "integrated: review unavailable"
            + (f" ({record['note']})" if record.get("note") else "")
        )
    if not approve and not reasons:
        reasons = ["integrated: requested changes"]

    if unavailable:
        summary = (
            "The validator's executable passed, but the required review did not "
            "finish. This pull request is not merged, and no builder is sent back to "
            "change code for a review-service failure."
        )
    elif approve:
        summary = (
            "The validator's executable passed. The review approved this pull "
            "request under both its adversarial and design lenses."
        )
    else:
        summary = (
            "The executable passed, but the integrated review found a problem "
            "under at least one required lens. The responsible role pull request "
            "must be updated and checked again."
        )

    body = str(record.get("assessment") or "").strip()
    if not body:
        body = str(record.get("note") or "No review output was available.")
    section = (
        f"<details><summary>{record['label']}: "
        f"{str(record['state']).replace('_', ' ')}</summary>\n\n"
        f"{body}\n\n"
        f"Model: `{record.get('model') or 'unavailable'}`\n\n"
        "</details>"
    )
    state = "Approve" if approve else "Request changes"
    return {
        "approve": approve,
        "reasons": reasons,
        "assessment": (
            f"**Assessment**: {state}\n\n{summary}\n\n"
            + section
        ),
        # Keep the list-shaped field so persisted runs and existing clients remain
        # readable. New runs record exactly one integrated review entry.
        "panels": [record],
        "all_abstained": unavailable,
        "review_unavailable": unavailable,
        "gate_summary": gate.get("summary"),
    }


def _default_judge(run: Any, gate: dict, subject: Any = None) -> dict | None:
    """Run one review turn over ONE pull request, covering both required lenses.

    The reviewer sees that pull request's tree on the base branch as it stands, and
    no maker conversation. A request-changes verdict stops THAT pull request from
    merging; an unreachable reviewer is recorded and also stops it. Neither can stop
    a sibling.
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
        record = _review_record(
            INTEGRATED_REVIEW_MODEL, note="review module unavailable")
        return _combine_review(gate, record, None)
    if not llm.available():
        record = _review_record(
            INTEGRATED_REVIEW_MODEL, note="no model credentials available")
        return _combine_review(gate, record, None)

    parts: list[str] = [f"Task: {getattr(run, 'task', '')!r}",
                        f"acceptance gate passed: {gate.get('passed')}",
                        f"gate: {json.dumps(gate.get('checks', []))[:2000]}"]
    if subject is not None:
        parts.insert(0, (
            f"YOU ARE REVIEWING ONE PULL REQUEST: {subject.work_id} "
            f"({subject.role}), which merges into "
            f"{getattr(run, 'final_base_branch', None)!r}. The files below are that "
            "pull request's own changes, and the check ran against the base branch "
            "as it stands plus those changes. If another role's pull request already "
            "merged, its code is on that base: judge whether THIS pull request works "
            "with what is there, not whether the whole project is finished."))
    try:
        import integration_plan  # noqa: PLC0415
        context = integration_plan.review_context(
            getattr(run, "integration_brief", None) or {},
            (getattr(run, "work_items", None) or {}).values(),
        )
        parts.append(
            "Recorded integration ownership and provenance:\n"
            + json.dumps(context, indent=2))
    except Exception:
        pass
    # Hand the judge the run's actual changed artifacts before any unchanged base
    # files. The complete changed-path inventory above remains visible even when a
    # large generated project exceeds the content excerpt budget.
    for label, path in _artifact_files(run, subject):
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                parts.append(f"--- {label} ---\n{f.read()[:3000]}")
    base_prompt = (
        "Review this pull request through both required lenses. Do not trust a "
        "builder's self-assessment or infer success from the green gate alone.\n\n"
        + "\n\n".join(parts)
    )
    required_work_ids = ([subject.work_id] if subject is not None
                         else _builder_work_ids(run))
    record, decision = _run_review_turn(
        llm,
        model=INTEGRATED_REVIEW_MODEL,
        base_prompt=base_prompt,
        required_work_ids=required_work_ids,
    )

    logger = getattr(run, "log", None)
    if callable(logger):
        logger(f"integrated review: {record['state']}")
    return _combine_review(gate, record, decision)


_MAX_JUDGE_FILES = 48


def _artifact_files(run: Any, subject: Any = None) -> list[tuple[str, str]]:
    """(label, path) for the artifacts of ONE pull request under review.

    READS THE TREE, names nothing. This used to look for `_server_file`, `_ui_dir`,
    and `_chatbot_file`, attributes from the deleted fixed-shape design that nothing
    has set since; the engine never assigned them, so the judge only ever saw the
    authored check and reviewed a pull request whose deliverable it had not read.

    Reads the tree the executable gate actually inspected: that pull request's own
    tree, built on the base branch as it stands. A role checkout is not used, because
    it includes its private base and may be stale.
    """
    files: list[tuple[str, str]] = []
    authored = getattr(run, "_acceptance_test_file", None)
    if subject is not None:
        authored = (getattr(run, "_item_checks", None) or {}).get(
            subject.work_id, authored)
    if authored:
        files.append(("the validator's authored acceptance check", authored))

    seen = {os.path.abspath(authored)} if authored else set()
    root = getattr(run, "_review_work_dir", "") or ""
    if not root and subject is not None:
        getter = getattr(run, "item_tree_dir", None)
        root = getter(subject.work_id) if callable(getter) else ""
    if not os.path.isdir(root):
        return files

    # This pull request's OWN changed paths first, then the rest of the tree if the
    # budget allows: the reviewer must see what this change does before it sees the
    # base it sits on.
    changed = sorted(getattr(subject, "changed_files", None) or []) if subject else []
    for rel in changed:
        if len(files) >= _MAX_JUDGE_FILES:
            break
        full = os.path.join(root, *rel.replace("\\", "/").split("/"))
        if not os.path.isfile(full) or os.path.abspath(full) in seen:
            continue
        seen.add(os.path.abspath(full))
        label = (f"{subject.work_id} ({subject.capability}) changed {rel}"
                 if subject is not None else rel)
        files.append((label, full))

    if len(files) >= _MAX_JUDGE_FILES:
        files.append((
            f"NOTE: changed-path contents were capped at {_MAX_JUDGE_FILES}; "
            "the complete inventory is in the provenance above",
            "",
        ))
        return files

    # Fill the remaining budget from the tree, so a read-only review of an older run
    # (which carries no per-item provenance) still sees real code.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d != "__pycache__")
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.abspath(full) in seen:
                continue
            seen.add(os.path.abspath(full))
            rel = os.path.relpath(full, root)
            files.append((f"on the base branch: {rel}", full))
            if len(files) >= _MAX_JUDGE_FILES:
                files.append((
                    f"NOTE: artifact contents were capped at {_MAX_JUDGE_FILES}; "
                    "the complete changed-path inventory is in the provenance above",
                    "",
                ))
                return files
    return files


def _abstained_assessment(gate: dict, approve: bool) -> str:
    """The deterministic assessment used when the integrated review abstains: a short,
    honest summary of the gate. Never invents review findings."""
    line = gate.get("summary") or ("green" if gate.get("passed") else "red")
    if approve:
        return ("**Assessment**: Approve\n\n"
                f"The validator's executable passed ({line}). "
                "The external integrated review was not run for this offline "
                "fixture, so that status is recorded explicitly.")
    return ("**Assessment**: Request changes\n\n"
            f"The executable failed ({line}). See the failing checks. "
            "A failed executable cannot be approved.")


def assess(run: Any, gate: dict, round_no: int,
           judge: Any = _default_judge, subject: Any = None) -> Verdict:
    """One review round over ONE pull request: take that pull request's gate result,
    layer the independent review, and return the verdict whose markdown the engine
    posts on that pull request.

    ``subject`` is the work item under review. It is what makes this a per-pull-request
    decision instead of a verdict about an assembled tree: the reviewer sees that one
    pull request's files, on the base branch as it stands, so a sibling that already
    merged is visible and a sibling that has not is not pretended to be.

    The ``judge`` is injectable (tests pass a fake or ``None`` to disable it);
    it defaults to one read-only model turn whose response must cover both
    adversarial and design/integration lenses. An unavailable review is recorded
    without fabricating a finding or asking a builder to repair the outage. A red
    gate is never assessed as approvable; a green gate may still get changes
    requested.
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
            jv = judge(run, gate, subject)
        except Exception:
            jv = None
    if jv is None:
        if getattr(run, "_offline_double", False):
            # Fixture runs test engine plumbing without an external model. This
            # branch is unreachable in the shipped AgentCore executor.
            verdict.lgtm = True
            verdict.assessment = _abstained_assessment(gate, approve=True)
        else:
            record = _review_record(
                INTEGRATED_REVIEW_MODEL, note="review call unavailable")
            jv = _combine_review(gate, record, None)
            verdict.lgtm = False
    else:
        verdict.lgtm = bool(jv.get("approve"))
    if jv is not None:
        verdict.reasons = list(jv.get("reasons") or [])
        verdict.panels = list(jv.get("panels") or [])
        verdict.review_unavailable = bool(jv.get("review_unavailable"))
        verdict.assessment = (jv.get("assessment")
                              or _abstained_assessment(gate, verdict.lgtm))
    verdict.state = "approved" if verdict.lgtm else "changes_requested"
    if verdict.lgtm and LGTM_TOKEN not in verdict.assessment:
        # Approval is a literal, checkable token, never a paraphrase.
        verdict.assessment = verdict.assessment.rstrip() + f"\n\n{LGTM_TOKEN}\n"
    return verdict
