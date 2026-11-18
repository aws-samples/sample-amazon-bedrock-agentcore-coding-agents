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

import codecs
import json
import re
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from gate_diagnostics import extract_failure_lines
from review_build_outputs import declared_outputs
import review_tools


LGTM_TOKEN = "LGTM: no changes needed"   # the exact pass token, kept verbatim
# One bounded re-implement pass, then a human. THE SINGLE SOURCE OF TRUTH for
# the bound: the engine derives its iteration cap from this (cap = rounds + 1),
# so editing this number actually changes behavior.
MAX_REVIEW_ROUNDS = 1

@dataclass
class Verdict:
    """The judge's structured output for one round."""

    state: str = "in_review"        # in_review | approved | changes_requested | unavailable
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



# Sonnet 5.5 and Opus 5.5 under tool choice `auto` (they refuse a forced tool) can
# leave tool-call markup or the review's own field names as XML-style tags inside a
# lens: a design assessment ending in `</design_assessment>`, a stray `</invoke>`
# (October 19-20). Those tags are transport residue, not findings, so they never
# reach the posted assessment. Text inside code spans and fenced blocks is quoted
# code and is left exactly as written.
_TOOL_MARKUP_TAGS = ("invoke", "parameter", "function_calls", "function_results",
                     "tool_use", "tool_call", "tool_calls", "tool_result")


def _stray_tag_pattern(field_names: tuple[str, ...]) -> re.Pattern[str]:
    names = "|".join(re.escape(name) for name in field_names + _TOOL_MARKUP_TAGS)
    return re.compile(
        r"(?P<code>```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)"
        r"|(?P<tick>`+)[^`\n]*?(?P=tick))"
        r"|(?P<tag></?[ \t]*(?:antml:)?(?:" + names + r")(?=[\s/>])[^<>\n]*>)",
        re.IGNORECASE)


def _strip_stray_markup(text: str) -> str:
    """Remove stray tool-call and review-field tags from prose, never from code."""
    if "<" not in text:
        return text
    return _STRAY_TAG_RE.sub(
        lambda m: m.group("code") if m.group("code") is not None else "", text).strip()


def _decode_literal_newlines(text: str) -> str:
    """Undo a model's double-escaped line breaks so a lens renders as Markdown.

    A September 29 review arrived with literal backslash-n sequences between its
    bullets, which GitHub then showed as text. A literal one becomes a line break
    when it starts a list item, or anywhere when the lens has no real line break.
    """
    if "\\n" not in text:
        return text
    if "\n" not in text:
        return text.replace("\\n", "\n")
    return re.sub(r"\\n(?=\s*[-*] )", "\n", text)

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
    """Quote a failure on a red gate, otherwise the last meaningful output.

    Was `lines[-1]` unconditionally, which is wrong for the most natural way a shell
    check ends: printing a results line and then a `====` rule under it. A live run
    then reported its gate summary as `===============================` -- on the PR
    body, in `run_status`, and in the ledger -- so the one human-readable sentence the
    check produced was replaced by its own underline.

    Scans backwards for the last line with real content and falls back to the raw last
    line (then to the exit code) rather than inventing a verdict.

    A failed process may print its runtime version after the actual failure and
    exception. Prefer those recorded diagnostics; only the exit code decides red.
    """
    if code != 0:
        failures = extract_failure_lines(out)
        if failures:
            return failures[0].strip()
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


# What an authored check inherits from the process that runs it. The check is model-written
# code, and this process may hold credentials that can merge a pull request (the
# coordinator's role reaches the GitHub tools), so it gets an allowlist of ordinary
# variables and none of AWS's, GitHub's, or the engine's. It still runs on this host:
# this keeps credentials out of its environment, it does not make it a sandbox.
_CHECK_ENV_NAMES = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "TMPDIR", "TERM",
                    "LD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
                    "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "no_proxy",
                    # The check toolchain's locations (check-tools.sh), never a secret.
                    "PLAYWRIGHT_BROWSERS_PATH", "NODE_PATH"}


def _check_environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _CHECK_ENV_NAMES or k.startswith("LC_")}


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

    The executable runs with the selected source tree as its working directory,
    and with a few facts in its environment,
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

    # cwd and WORKSHOP_WORK_DIR must name the SAME source tree. Keep the executable
    # outside it so a source scan does not classify the check as product code.
    # Resolve both paths before changing cwd; a relative executable path must not
    # accidentally resolve against the application directory.
    authored = os.path.abspath(authored)
    work_dir = os.path.abspath(work_dir or os.path.dirname(authored))
    # GATE_TIMEOUT_S is a FACT about the environment, not a hint about the verdict, so
    # handing it over changes nothing about what the check decides. Withholding it made
    # checks guess: four live runs in a row failed on a readiness poll the author had
    # budgeted 15-30s while the deliverable was still preparing declared
    # dependencies. Those were red gates on working services, which is the one
    # failure this file must not manufacture.
    env = {**_check_environment(),
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
                                    env=env, cwd=work_dir,
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
        # Extract before shortening output: later PASS lines can hide an early
        # failure from both the repair prompt and the pull request.
        "failure_lines": extract_failure_lines(out) if code != 0 else [],
        "output": (out or "")[-4000:]}


# ----------------------------------------------------- integrated read-only review
# The stack sets WORKSHOP_REVIEW_MODEL (ReviewModelId, Opus 5.5 at low effort by
# default); without it the review follows the backend model, then Opus 5.5. On
# October 19-20 Opus 5.5's change requests were real defects (median 17.8 s per
# call), while Sonnet 5.5 asked for changes in 7 of 16 rounds.
# The reviewer is independent from every builder conversation, but it evaluates both
# required lenses in one turn to avoid paying twice for the same candidate context.
_REVIEW_MODEL = (
    os.environ.get("WORKSHOP_REVIEW_MODEL")
    or os.environ.get("WORKSHOP_CLAUDE_MODEL")
    or "global.anthropic.claude-opus-5-5"
)
INTEGRATED_REVIEW_MODEL = _REVIEW_MODEL
# Claude 5 models reason before they answer: a 3,200-token cap ended Opus 5's reply
# before its verdict on 2026-09-23. The verdict now arrives through a tool schema, so
# a long answer cannot break its own JSON, and low effort keeps a review near
# twenty seconds (Opus 5: 20-23 s over three game builds on 2026-09-27).
_REVIEW_MAX_TOKENS = int(os.environ.get("WORKSHOP_REVIEW_MAX_TOKENS", "16000"))
_REVIEW_EFFORT = os.environ.get("WORKSHOP_REVIEW_EFFORT", "low").strip()
_REVIEW_TOOL = {"toolSpec": {
    "name": "submit_review",
    "description": "Submit the complete integrated review verdict for this pull request.",
    "inputSchema": {"json": {
        "type": "object",
        "required": ["approve", "reasons", "work_item_evidence",
                     "adversarial_assessment", "design_assessment"],
        "properties": {
            "approve": {"type": "boolean"},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "work_item_evidence": {"type": "object",
                                   "additionalProperties": {"type": "string"}},
            "adversarial_assessment": {"type": "string"},
            "design_assessment": {"type": "string"},
            "findings": {"type": "array", "items": {
                "type": "object",
                "required": ["priority", "title", "failure"],
                "properties": {
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "title": {"type": "string"},
                    "place": {"type": "string", "description": "path:line"},
                    "failure": {"type": "string"},
                    "fix": {"type": "string"},
                }}},
            "not_verified": {"type": "array", "items": {"type": "string"}},
        },
    }},
}}
# The tags a stray-markup strip removes: this tool's own name and field names.
_STRAY_TAG_RE = _stray_tag_pattern(
    (_REVIEW_TOOL["toolSpec"]["name"],)
    + tuple(_REVIEW_TOOL["toolSpec"]["inputSchema"]["json"]["properties"]))

_REVIEW_RESPONSE_CONTRACT = (
    "Reply with STRICT JSON only:\n"
    '{"approve": true|false, "reasons": ["..."], '
    '"work_item_evidence": {"<work id>": "<specific usage evidence>"}, '
    '"adversarial_assessment": "<concise markdown findings>", '
    '"design_assessment": "<concise markdown findings>"}\n'
    "Both assessment fields are required, even when they report no finding. "
    "reasons lists concrete unresolved defects, never approval rationale. "
    "Use an empty reasons array only when neither lens found a defect. Every "
    "confirmed defect described in either assessment must also appear in reasons. "
    "Put every defect in findings, one per ROOT CAUSE (list every affected place "
    "inside that one finding), each with a priority, a place (path:line on this tree), "
    "the concrete failure, and the fix. Priorities: P0 must fix before merging (crash, "
    "data loss, security hole, broken build or start); P1 should fix before merging "
    "(wrong behavior, a case the request names that does not work, a regression "
    "against the base branch, a broken seam with code already on it); P2 worth fixing "
    "(a likely future bug, a missing test for changed logic); P3 minor (style, wording, "
    "a doc that disagrees with the code). Only P0 and P1 block the merge: set "
    "approve=true exactly when no P0 or P1 finding remains, and list only the P0 and P1 "
    "findings, one line each, in reasons. A P0 or P1 you cannot state as a concrete "
    "case is not one: downgrade it or drop it. Before you submit, re-read the cited "
    "lines of every P0 and P1 with read_file and drop any the code does not support. "
    "A claim you could not check is not a finding: put it in not_verified. "
    "Set approve=false when EITHER lens finds a P0 or P1 defect. "
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
    "on BOTH sides of each seam, reading the base-branch code with your read-only "
    "tools rather than assuming the other side matches. A mismatch between what this "
    "pull request "
    "produces and what its counterpart consumes is a material defect even when the "
    "executable passed, because a check can exercise one side alone. Look for edge "
    "cases, persistence failures, security defects, dead paths, and checks that "
    "prove only existence or build success. Follow the primary user interactions "
    "through their state transitions as well as the API: duplicate writes, "
    "unreachable controls, and misleading application state affect correctness. "
    "Trace keyboard and pointer events from the focused control through each "
    "pending, failed, and completed state. Do not assume a hidden control cannot "
    "receive another event, or that a global shortcut leaves form input alone. "
    "When the check names an unexercised interaction, inspect that exact path; "
    "a passing API helper is not evidence for its DOM event wiring. "
    "Separate behavior inferred from source from behavior actually executed by "
    "the supplied check. Claim execution only for steps its evidence records; "
    "an untested path does not become tested because its implementation looks sound. "
    "Do not excuse a confirmed behavioral defect because the executable or the "
    "HTTP contract did not cover it.\n"
    "2. Design and integration: verify that this pull request stays inside its own "
    "ownership, that what it contributes is actually reachable and used, that it "
    "matches the shared contract, and that it composes with the base branch as it "
    "stands rather than adding a disconnected parallel stack. Assess data ownership, "
    "failure handling, migration compatibility, deployment and restart behavior, "
    "accessibility where relevant, and maintainability at the requested scope.\n"
    "Judge THIS pull request, not whether the whole project is finished: a sibling "
    "role's pull request may still be open, and its absence is not a defect in this "
    "one. Assess deployment against the requested environment; do not invent an "
    "additional deployment topology or require external network exposure that was "
    "never requested. Do not demand a particular framework, filename, or layout.\n\n"
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
    if not isinstance(parsed["approve"], bool):
        raise ValueError("reviewer approve verdict must be a JSON boolean")
    raw_reasons = parsed.get("reasons")
    if not isinstance(raw_reasons, list) or any(
        not isinstance(reason, str) or not reason.strip()
        for reason in raw_reasons
    ):
        raise ValueError("reviewer reasons must be an array of nonempty defect strings")
    reasons = [cleaned for cleaned in (
        _strip_stray_markup(reason.strip()) for reason in raw_reasons) if cleaned][:5]

    raw_evidence = parsed.get("work_item_evidence")
    evidence = {
        str(work_id): _strip_stray_markup(str(detail).strip())
        for work_id, detail in (
            raw_evidence.items() if isinstance(raw_evidence, dict) else []
        )
        if _strip_stray_markup(str(detail).strip())
    }
    # The model decides which defects exist. Its approval flag cannot erase a
    # finding it recorded, just as it cannot turn a red executable green.
    approve = parsed["approve"] and not reasons
    findings = _parse_findings(parsed.get("findings"))
    not_verified = [
        cleaned for cleaned in (_strip_stray_markup(str(item).strip())
                                for item in (parsed.get("not_verified") or [])
                                if isinstance(item, str))
        if cleaned][:8]
    if findings is not None:
        # The priorities decide (Foxl's scale): P0 and P1 block, P2 and P3 are posted
        # and never block. The model's approve flag and its free-text reasons cannot
        # turn a recorded P0 or P1 into an approval, or a P3 into a block.
        blocking = [f for f in findings if f["priority"] in ("P0", "P1")]
        approve = not blocking
        reasons = [_finding_line(f) for f in blocking][:5]
    missing = [
        work_id for work_id in required_work_ids
        if not evidence.get(work_id)
    ]
    if approve and missing:
        raise _JudgeEvidenceError(
            "approval omitted concrete integration evidence for "
            + ", ".join(missing)
        )

    adversarial = _strip_stray_markup(_decode_literal_newlines(
        str(parsed.get("adversarial_assessment") or "").strip()))
    design = _strip_stray_markup(_decode_literal_newlines(
        str(parsed.get("design_assessment") or "").strip()))
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
    if findings is not None:
        assessment = _findings_sections(findings, not_verified) + assessment
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
        "reasons": reasons,
        "work_item_evidence": evidence,
        "adversarial_assessment": adversarial,
        "design_assessment": design,
        "assessment": assessment,
        "findings": findings or [],
        "not_verified": not_verified,
    }


def _parse_findings(raw: Any) -> list[dict[str, str]] | None:
    """The structured findings, or None when the response has none (older shape)."""
    if not isinstance(raw, list):
        return None
    findings = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        priority = str(item.get("priority") or "").strip().upper()
        title = _strip_stray_markup(str(item.get("title") or "").strip())
        failure = _strip_stray_markup(str(item.get("failure") or "").strip())
        if priority not in ("P0", "P1", "P2", "P3") or not (title or failure):
            raise ValueError("every finding needs a priority P0-P3 and a title or failure")
        findings.append({
            "priority": priority, "title": title or failure[:80], "failure": failure,
            "place": _strip_stray_markup(str(item.get("place") or "").strip()),
            "fix": _strip_stray_markup(str(item.get("fix") or "").strip()),
        })
    return sorted(findings, key=lambda f: f["priority"])


def _finding_line(f: dict[str, str]) -> str:
    place = f" - `{f['place']}`" if f["place"] else ""
    fix = f" Fix: {f['fix']}" if f["fix"] else ""
    return f"{f['priority']}: {f['title']}{place}: {f['failure']}{fix}"


def _findings_sections(findings: list[dict[str, str]], not_verified: list[str]) -> str:
    """Must fix (P0, P1), Worth fixing (P2, P3), Not verified: what a person acts on first."""
    must = [f for f in findings if f["priority"] in ("P0", "P1")]
    worth = [f for f in findings if f["priority"] in ("P2", "P3")]
    out = []
    if must:
        out.append("### Must fix\n\n" + "\n".join(
            f"{i}. **{f['title']}** ({f['priority']})"
            + (f" - `{f['place']}`" if f["place"] else "")
            + (f"\n   {f['failure']}" if f["failure"] else "")
            + (f"\n   Fix: {f['fix']}" if f["fix"] else "")
            for i, f in enumerate(must, 1)))
    if worth:
        out.append("### Worth fixing (does not block the merge)\n\n" + "\n".join(
            f"- **{f['title']}** ({f['priority']})"
            + (f" - `{f['place']}`" if f["place"] else "")
            + (f": {f['failure']}" if f["failure"] else "")
            + (f" Fix: {f['fix']}" if f["fix"] else "")
            for f in worth))
    if not_verified:
        out.append("### Not verified\n\n" + "\n".join(f"- {item}" for item in not_verified))
    return ("\n\n".join(out) + "\n\n") if out else ""


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


def _review_conversation(llm_module: Any, model: str, prompt: str,
                         tree: Any = None) -> dict[str, Any]:
    """One review: the model reads with the read-only tools until it submits.

    Without a tree (no checkout to read) it is one call, as before. Each tool round
    returns every requested read at once; after ``_REVIEW_MAX_TOOL_ROUNDS`` rounds the
    reviewer is told to submit from what it has read.
    """
    tools = review_tools.READ_TOOLS if tree is not None else None
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": prompt}]}]
    out: dict[str, Any] = {}
    for round_no in range(_REVIEW_MAX_TOOL_ROUNDS + 1):
        out = llm_module.invoke(
            model, prompt, system=_INTEGRATED_REVIEW_SYSTEM,
            max_tokens=_REVIEW_MAX_TOKENS, tool=_REVIEW_TOOL, effort=_REVIEW_EFFORT,
            tools=tools, messages=messages if tools else None)
        calls = [c for c in (out.get("tool_calls") or [])
                 if c.get("name") in review_tools.TOOL_NAMES]
        if tools is None or out.get("tool_input") is not None or not calls:
            return out
        messages.append({"role": "assistant", "content": out.get("content") or []})
        results = [{"toolResult": {"toolUseId": c.get("id"),
                                   "content": [{"text": tree.run(c.get("name"), c.get("input"))}]}}
                   for c in calls]
        if round_no + 1 >= _REVIEW_MAX_TOOL_ROUNDS:
            results.append({"text": "That was your last read. Call submit_review now, "
                                    "judging from what you have read; list anything you "
                                    "could not check under not_verified."})
        messages.append({"role": "user", "content": results})
    return out


def _run_review_turn(
    llm_module: Any,
    *,
    model: str,
    base_prompt: str,
    required_work_ids: list[str],
    tree: Any = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run one integrated review with one bounded response repair."""
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
            out = _review_conversation(
                llm_module, model,
                prompt + "\n\nSubmit the verdict by calling the submit_review tool once.",
                tree)
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
    # A review that never produced a decision requests nothing: headlining it
    # "Request changes" on the PR contradicted the run's own advice not to rebuild.
    state = ("Approve" if approve else
             "Review unavailable (no decision)" if unavailable else "Request changes")
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

    # The request as the builders read it, not a Python repr with escaped newlines.
    parts: list[str] = [f"Task:\n{getattr(run, 'task', '') or ''}",
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
    # What the reviewer starts from (November 18, after Foxl's reviewer, RV1): the
    # check that decided the gate, then THIS pull request's own changed files. The base
    # branch is NOT pasted in; the reviewer reads, lists and searches the merged tree
    # with read-only tools (review_tools.py), the way a person opens the files a diff
    # touches. So no review is refused or cut for size: a long changed file arrives as
    # its first lines plus how to read the rest, and every file left out of the opening
    # message (binary, generated) is listed and can still be read.
    omitted: list[tuple[str, int, str]] = []
    previews: list[str] = []
    for label, path in _artifact_files(run, subject, omitted=omitted, changed_only=True):
        if not path:
            parts.append(label)
            continue
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            raw = f.read()
        if b"\0" in raw[:8192] and label != _AUTHORED_CHECK_LABEL:
            omitted.append((label, len(raw), "binary file"))
            continue
        content = raw.decode("utf-8", errors="replace")
        if label != _AUTHORED_CHECK_LABEL and len(content) > _OPENING_FILE_CHARS:
            lines = content[:_OPENING_FILE_CHARS].splitlines()[:-1] or [content[:_OPENING_FILE_CHARS]]
            total = content.count("\n") + 1
            parts.append(f"--- {label} (first {len(lines)} of {total} lines; read_file "
                         f"with start_line={len(lines) + 1} for the rest) ---\n" + "\n".join(lines))
            previews.append(label)
            continue
        parts.append(f"--- {label} ---\n{content}")
    if omitted:
        parts.append(_omitted_section(omitted))
    root = _review_root(run, subject)
    tree = review_tools.ReviewTree(root) if root else None
    base_prompt = (
        "Review this pull request through both required lenses. Do not trust a "
        "builder's self-assessment or infer success from the green gate alone.\n\n"
        + "\n\n".join(parts)
        + ("\n\nYou have read-only tools over this pull request's tree exactly as it "
           "would merge (the base branch as it stands plus these changes): read_file, "
           "list_files and search_code. Read the rest of any file shown only in part, and "
           "open the base-branch code this pull request calls or is called by before you "
           "judge a seam. Ask for several files in one turn when you can."
           if tree else "")
    )
    required_work_ids = ([subject.work_id] if subject is not None
                         else _builder_work_ids(run))
    record, decision = _run_review_turn(
        llm,
        model=INTEGRATED_REVIEW_MODEL,
        base_prompt=base_prompt,
        required_work_ids=required_work_ids,
        tree=tree,
    )

    logger = getattr(run, "log", None)
    if callable(logger):
        logger(f"integrated review: {record['state']}"
               + (f" ({len(omitted)} binary or generated files left out)"
                  if omitted else ""))
    return _combine_review(gate, record, decision)


_MAX_JUDGE_FILES = 200
# A changed file longer than this opens with its first lines and how to read the rest;
# the reviewer pages through it with read_file. Nothing is refused for size.
_OPENING_FILE_CHARS = 60_000
# Tool rounds one review may spend reading before it must submit its verdict.
_REVIEW_MAX_TOOL_ROUNDS = int(os.environ.get("WORKSHOP_REVIEW_MAX_TOOL_ROUNDS", "12"))


def _review_root(run: Any, subject: Any = None) -> str:
    """The merged tree the executable gate inspected, which the reviewer's tools read."""
    root = getattr(run, "_review_work_dir", "") or ""
    if not root and subject is not None:
        getter = getattr(run, "item_tree_dir", None)
        root = getter(subject.work_id) if callable(getter) else ""
    return root if root and os.path.isdir(root) else ""
# The review input budget, in characters. It was 192,000, and on November 17 nine of
# sixty team builds ended REVIEW_UNAVAILABLE on a GREEN gate because one committed
# 203-236 KB frontend bundle exceeded it. The review model accepts far more: a measured
# Opus 5.5 Converse call took 800,000 characters (498,128 input tokens of dense script)
# in 10.2 s on November 18. Source still arrives complete; nothing is hidden by shape,
# and a tree past the budget is reviewed with its cut marked, never abstained on.
_MAX_JUDGE_CONTENT_CHARS = 800_000

# What the review leaves out, because it is not source a reviewer can read: binary
# files and generated build output. October 19-20: a GPT-6.1 frontend committed its
# Vite bundle and a .ttf font, the font was read as text, and the review abstained on
# the context limit (REVIEW_UNAVAILABLE) over two green pull requests. Everything
# else still arrives COMPLETE, and every omitted file is listed with its size, so
# the reviewer knows it exists and can still call committed build output a finding.
_GENERATED_DIRS = frozenset({"dist", "build", ".vite"})
_GENERATED_SUFFIXES = (".min.js", ".min.mjs", ".min.css", ".map")
_BINARY_EXTENSIONS = frozenset({
    # fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".avif", ".tif",
    ".tiff", ".psd",
    # audio and video
    ".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".mp4", ".m4v",
    ".webm", ".mov", ".avi", ".mkv",
    # archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".zst",
    # documents, compiled code and databases
    ".pdf", ".wasm", ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
    ".pyc", ".class", ".sqlite", ".sqlite3", ".db",
})
_SNIFF_BYTES = 8192
_MAX_OMITTED_LISTED = 60
_AUTHORED_CHECK_LABEL = "the validator's authored acceptance check"


def _review_omission(rel: str, full: str,
                     generated: dict[str, str] | None = None) -> str:
    """Why a file stays out of the review context, or "" when it is source to read.

    By path first (generated bundles, binary extensions), then by content: a NUL
    byte or bytes that are not UTF-8 text mean a binary file whatever its name.
    """
    parts = [part for part in rel.replace("\\", "/").split("/") if part]
    name = parts[-1].lower() if parts else ""
    if (any(part.lower() in _GENERATED_DIRS for part in parts[:-1])
            or name.endswith(_GENERATED_SUFFIXES)):
        return "generated build output"
    declared = (generated or {}).get(os.path.normpath(rel).replace("\\", "/"))
    if declared:
        return declared
    if os.path.splitext(name)[1] in _BINARY_EXTENSIONS:
        return "binary file"
    try:
        with open(full, "rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return ""   # the reader reports an unreadable file; do not hide it here
    if b"\0" in head:
        return "binary file"
    try:
        # A multi-byte character cut at the sniff boundary is not a decode failure.
        codecs.getincrementaldecoder("utf-8")().decode(
            head, final=len(head) < _SNIFF_BYTES)
    except UnicodeDecodeError:
        return "binary file (not UTF-8 text)"
    return ""


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _reviewable_entry(root: str, rel: str) -> bool:
    """A declaration cannot hide output when its source entry is unavailable."""
    path = os.path.join(root, rel)
    if _review_omission(rel, path):
        return False
    try:
        with open(path, "rb") as stream:
            raw = stream.read(4 * _MAX_JUDGE_CONTENT_CHARS + 1)
        return (b"\0" not in raw
                and len(raw.decode("utf-8")) <= _MAX_JUDGE_CONTENT_CHARS)
    except (OSError, UnicodeError):
        return False


def _omitted_section(omitted: list[tuple[str, int, str]]) -> str:
    """The review-context note naming every file left out, with its size."""
    lines = [
        "Left out of this review (listed so you know they exist; committed build "
        "output can itself be a finding):"]
    lines.extend(f"- {label}: {size:,} bytes, {reason}"
                 for label, size, reason in omitted[:_MAX_OMITTED_LISTED])
    rest = omitted[_MAX_OMITTED_LISTED:]
    if rest:
        lines.append(f"- and {len(rest)} more files, "
                     f"{sum(size for _label, size, _reason in rest):,} bytes in total")
    return "\n".join(lines)


def _artifact_files(run: Any, subject: Any = None,
                    omitted: list[tuple[str, int, str]] | None = None,
                    changed_only: bool = False,
                    ) -> list[tuple[str, str]]:
    """(label, path) for the artifacts of ONE pull request under review.

    READS THE TREE, names nothing. This used to look for `_server_file`, `_ui_dir`,
    and `_chatbot_file`, attributes from the deleted fixed-shape design that nothing
    has set since; the engine never assigned them, so the judge only ever saw the
    authored check and reviewed a pull request whose deliverable it had not read.

    Reads the tree the executable gate actually inspected: that pull request's own
    tree, built on the base branch as it stands. A role checkout is not used, because
    it includes its private base and may be stale.

    Binary files and generated build output are not listed; each one is appended to
    ``omitted`` as (label, size in bytes, reason) instead, and does not count toward
    the file limit.
    """
    if omitted is None:
        omitted = []
    files: list[tuple[str, str]] = []
    authored = getattr(run, "_acceptance_test_file", None)
    if subject is not None:
        authored = (getattr(run, "_item_checks", None) or {}).get(
            subject.work_id, authored)
    if authored:
        files.append((_AUTHORED_CHECK_LABEL, authored))

    seen = {os.path.abspath(authored)} if authored else set()
    root = getattr(run, "_review_work_dir", "") or ""
    if not root and subject is not None:
        getter = getattr(run, "item_tree_dir", None)
        root = getter(subject.work_id) if callable(getter) else ""
    if not os.path.isdir(root):
        return files
    # A file need not live under dist/ or end in .min.js to be generated. Read
    # explicit build declarations from this exact gate tree, never run a project
    # command here, and keep the declaration's source entry in the review.
    generated = {
        output: claim.reason for output, claim in declared_outputs(root).items()
        if _reviewable_entry(root, claim.source)
    }

    # This pull request's OWN changed paths first, then the rest of the tree if the
    # budget allows: the reviewer must see what this change does before it sees the
    # base it sits on.
    changed = sorted(getattr(subject, "changed_files", None) or []) if subject else []
    for rel in changed:
        full = os.path.join(root, *rel.replace("\\", "/").split("/"))
        if not os.path.isfile(full) or os.path.abspath(full) in seen:
            continue
        label = (f"{subject.work_id} ({subject.capability}) changed {rel}"
                 if subject is not None else rel)
        reason = _review_omission(rel, full, generated)
        if reason:
            seen.add(os.path.abspath(full))
            omitted.append((label, _file_size(full), reason))
            continue
        if len(files) >= _MAX_JUDGE_FILES:
            files.append((
                f"NOTE: changed-path contents exceed {_MAX_JUDGE_FILES} files; "
                "the complete inventory is in the provenance above", ""))
            return files
        seen.add(os.path.abspath(full))
        files.append((label, full))

    if changed_only and changed:
        # The reviewer opens the rest of the tree with its read-only tools.
        return files
    # Fill the remaining budget from the tree, so a read-only review of an older run
    # (which carries no per-item provenance) still sees real code.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".")
            and d not in {"__pycache__", "node_modules", "venv"})
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.abspath(full) in seen:
                continue
            rel = os.path.relpath(full, root)
            reason = _review_omission(rel, full, generated)
            if reason:
                seen.add(os.path.abspath(full))
                omitted.append((f"on the base branch: {rel}", _file_size(full), reason))
                continue
            if len(files) >= _MAX_JUDGE_FILES:
                files.append((
                    f"NOTE: artifact contents exceed {_MAX_JUDGE_FILES} files; "
                    "the complete changed-path inventory is in the provenance above",
                    "",
                ))
                return files
            seen.add(os.path.abspath(full))
            files.append((f"on the base branch: {rel}", full))
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
    verdict.state = ("approved" if verdict.lgtm else
                     "unavailable" if verdict.review_unavailable else "changes_requested")
    if verdict.lgtm and LGTM_TOKEN not in verdict.assessment:
        # Approval is a literal, checkable token, never a paraphrase.
        verdict.assessment = verdict.assessment.rstrip() + f"\n\n{LGTM_TOKEN}\n"
    return verdict
