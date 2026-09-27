"""The reporting projection of a run: what a person, the console, or a watcher is told.

Split out of engine.py so the views have one home that imports neither the engine,
the model client, nor the reviewer: the watcher, the progress check, and the run
advice can read a run without loading the verdict path. The engine re-exports every
name here, so ``engine.public_result`` and friends keep working for every caller.

It is REPORTING only. Nothing here decides a verdict; the worst a bug here can do
is describe a run badly.
"""
from __future__ import annotations

import re
from typing import Any

# Agent output is arbitrary text, and this tail is persisted to the runtime bucket. No
# credential is on the dispatch path by construction (roles authenticate with the
# Runtime's own IAM role, and a brokered vendor key is command-substituted, never
# echoed), so this is defence in depth for a role that prints one anyway.
_SECRET_RE = re.compile(
    r"\b(?:ksk_[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{8,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{8,}"
    r"|sk-[A-Za-z0-9_-]{12,})")


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


def public_submitter(identity: dict | None) -> str | None:
    """Expose only the user label recorded when the build was admitted."""
    if isinstance(identity, dict):
        for key in ("user_email", "user_id"):
            value = identity.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def public_run(run: Any) -> dict:
    return {
        "run_id": run.run_id,
        "task": run.task,
        "status": run.status,
        "phase": run.phase,
        "created_at": run.created_at,
        "submitted_by": public_submitter(run.user_identity),
        "agents": run.agents,
        "roles": run.roles,
        # additive (API_CONTRACT.md "Engine additions"): the router's verdict
        "route": run.route,
        # Why a run stopped (RUNTIME_NOT_WIRED:<role>, HARNESS_MISSING:<role>, …)
        # so the console states the real reason instead of a bare "needs_human":
        # a fail-loud verdict must be legible, never look like a silent mock.
        "fail_reason": run.fail_reason,
    }


def public_progress(run: Any) -> list[dict]:
    return [
        {"agent": r.agent, "role": r.role, "state": r.state,
         "latency_ms": r.latency_ms, "tokens": r.tokens,
         "cost_usd": r.cost_usd, "note": r.note, "engine": r.engine}
        for r in run.progress.values()
    ]


def public_terminals(run: Any) -> dict:
    """Per-role shell transcripts: the console streams these into xterm panes."""
    with run._lock:
        return {agent: list(lines) for agent, lines in run.terminals.items()}


def public_events(run: Any) -> dict:
    """Per-role events the console renders as one feed, in arrival order.

    Structured events (text/thinking/tool_use/tool_result) first, then the rolling
    tail of what the role's CLI is PRINTING (kind ``output``). Both, for the same
    reason the watcher shows both: on a dispatched run the engine is the only producer
    of structured events, so a feed of those alone showed one summary line per role
    while the role was working.
    """
    with run._lock:
        feeds = {agent: [dict(e) for e in evs]
                 for agent, evs in run.role_events.items()}
        for agent, lines in run.role_output.items():
            feeds.setdefault(agent, []).extend(
                {"kind": "output", "text": _redact(line)} for line in lines)
    return feeds


# What an attendee should DO about each terminal reason. Idea from
# awslabs/aidlc-workflows v2, whose stage checkboxes say WHO IS BLOCKING at a glance
# (`[?]` awaiting you, `[R]` revising) rather than making you decode a state name.
#
# Our `needs_human` covers two very different situations: validation stayed blocked
# on real work (read the gate and review evidence) and a role never produced anything
# (a transport or turn failure, just resubmit). Same status, opposite next action,
# and the raw token said neither.
_NEXT_ACTION = {
    "ROLE_PR_PUBLISH_ERROR":
        "The builder finished, but GitHub refused to open its pull request (for "
        "example, the App lacks Pull requests: write, or cannot see the repository). "
        "Run `python3 orchestrator/github.py doctor`, fix what it reports, and only "
        "then submit the request again.",
    # A blocked run may already have merged some pull requests while another stayed
    # red. ITERATION_CAP can mean a red executable OR a required review finding.
    "ITERATION_CAP":
        "Do not resubmit this request. The pull request still had blocking gate or "
        "review evidence after its one bounded repair. Read the latest gate.summary "
        "and the review evidence on that pull request, then decide as a person.",
    "ROLE_EXECUTION_ERROR":
        "A role's turn produced no usable work. This is usually transient: submit the "
        "SAME request again. Do not try to finish it by dispatching one role by hand.",
    "ROLE_TURN_LIMIT":
        "A role reached its configured CLI turn limit. Do not resubmit this "
        "request automatically. Read the role's recorded limit error and original "
        "request. If pull requests exist, inspect their recorded checks and reviews. "
        "A person must decide how to narrow the request or continue the work. "
        "A fresh shell would restart the same bounded task.",
    # Distinct from the above ON PURPOSE: the agent SUCCEEDED and its work is still
    # in the runtime workspace. Saying "the role produced nothing" here would send
    # the reader to debug an agent that did its job.
    "ARTIFACT_TRANSFER_ERROR":
        "The role's work exists in its runtime workspace but the engine could not "
        "read it back, so this is our transport failure and not a failed agent turn. "
        "Submit the SAME request again; the workspace listing is in the fail reason.",
    "ROLE_TOTAL_FAILURE":
        "EVERY routed role failed, which points at the harness or the environment "
        "rather than the request: check that each role's runtime is wired and READY, "
        "then resubmit.",
    "MODEL_QUOTA_EXHAUSTED":
        "The selected model's daily token allowance is exhausted. Do not resubmit "
        "now. Wait for the allowance to reset or choose a model with available "
        "capacity, then submit the SAME request once.",
    "ROLE_PR_BLOCKED":
        "One or more pull requests did not become mergeable: their check stayed red, "
        "a review finding remained after the one bounded repair, the repair itself did "
        "not complete, or the merge was refused. The work ids are in the reason. Open each named pull request, "
        "read its executable check and Assessment comment, and continue from there as "
        "a person. Pull requests that did pass are already settled; do not resubmit "
        "the whole build to retry one of them.",
    "STALE_PATCH_REFRESH_CAP":
        "A pull request fell behind the default branch and used up its one bounded "
        "refresh. Open the named pull request, update its branch yourself, and "
        "re-run its check as a person.",
    "STALE_PATCH_EMPTY_REFRESH":
        "After a sibling merged, the named pull request had nothing left to "
        "contribute: its change is already on the default branch. Close it, or open "
        "it and confirm nothing is missing.",
    "WORK_TREE_MISSING":
        "The validator authored a check but the pull request tree it was authored "
        "against is gone, so nothing could be executed. This is an engine-side "
        "failure, not a failed agent turn: retry the same request once.",
    "REVIEW_UNAVAILABLE":
        "The executable check passed, but the required review did not produce a "
        "valid decision for the named pull request(s). Keep them open and inspect "
        "the recorded review reason. Resolve the access, response, or context "
        "problem, then retry review of the same pull request; do not rebuild the "
        "application for a review failure.",
    "ENGINE_STALL":
        "The run ended without reaching a verdict. Resubmit; if it stalls again, the "
        "engine log for this run id is the place to look.",
    "COORDINATOR_SESSION_INTERRUPTED":
        "The coordinator Runtime was recycled while the background build was active. "
        "Submit the SAME request again and keep polling that session until it reaches "
        "a terminal status.",
    "NO_RUN_TO_REVIEW":
        "A review-only request needs an earlier run to review. Submit a build first.",
    "PRESET_NOT_SPECIFIED":
        "No task text and no preset. Say what you want built, or name a preset from "
        "list_presets.",
    "NO_BUILDER_ROUTED":
        "The route selected no maker, so nothing would be built. Name a preset or a "
        "role set that includes a builder.",
    "NO_CHECKER_ROUTED":
        "The route selected no checker, so nothing would verify the work. Every build "
        "route must carry the validator.",
    "NO_ROLES_ROUTED":
        "The route selected no roles at all. Name a preset or an explicit role set.",
}


def _published_rows(role_prs: list[dict] | None, work_items: Any = None) -> list[dict]:
    """Every pull request a run published, not only the ones ``_finalize`` recorded.

    Builders' pull requests open BEFORE the checker runs, but ``role_prs`` is filled
    only when the run finalizes. A run that stopped in between (Kiro failed, the
    coordinator was recycled) therefore looked PR-less, and every reader was told to
    "submit the SAME request again", duplicating a build whose PR was already open.
    """
    rows = [dict(r) for r in (role_prs or []) if isinstance(r, dict)]
    known = {r.get("pr_url") for r in rows if r.get("pr_url")}
    items = work_items.values() if isinstance(work_items, dict) else (work_items or [])
    for item in items:
        pr = item.get("pr") if isinstance(item, dict) else getattr(item, "pr", None)
        url = (pr or {}).get("pr_url") if isinstance(pr, dict) else None
        if url and url not in known:
            known.add(url)
            work_id = item.get("work_id") if isinstance(item, dict) else getattr(item, "work_id", None)
            rows.append({"work_id": work_id, "pr_url": url, "state": "published"})
    return rows


def next_action(status: str, fail_reason: str | None,
                pr: dict | None = None, pr_url: str | None = None,
                role_prs: list[dict] | None = None, *, work_items: Any = None) -> str:
    """One sentence telling the reader what to do about this outcome.

    Derived, never stored: the reason is the fact, this is how to read it. An
    unrecognised reason returns "" rather than inventing advice.

    ``pr`` matters because the most common outcome is a run that PASSED, and a passing
    run still has two very different endings: the pull request opened (go read it) or
    it did not (the work is real but stranded in a local branch, and nothing else in
    the payload says so at a glance). The PR failure is NOT a fail_reason -- the build
    genuinely succeeded -- so it can only be read from the PR result.
    """
    role_prs = _published_rows(role_prs, work_items)
    if status == "passed":
        pr = pr or {}
        rows = list(role_prs or [])
        merged = [r for r in rows if r.get("state") == "merged"]
        waiting = [r for r in rows if r.get("state") == "awaiting_review"]
        if waiting:
            return (
                f"{len(waiting)} pull request(s) passed their check and review and "
                "are open for you to merge. Read each one's Assessment comment, then "
                "merge it on GitHub.")
        if merged:
            return (
                f"{len(merged)} pull request(s) passed their own check and review "
                "and merged into the default branch. Read each one's Assessment "
                "comment for the evidence.")
        if pr_url:
            return ("Open the role pull request and read its executable check and "
                    "Assessment comment.")
        pr_error = str(pr.get("error") or "")
        if pr_error.startswith("PR_NO_GATEWAY"):
            return (
                "The build completed without a GitHub side effect. Wire the GitHub "
                "MCP Gateway and submit a new run to create real pull requests.")
        if pr_error.startswith("PR_NO_CREDENTIAL"):
            return ("The build passed but the App credential did not resolve, so no PR "
                    "was opened. Re-run deploy-credential.sh, then resubmit.")
        if pr_error:
            return (f"The build passed but the PR step failed: {pr_error[:160]}. Run "
                    "`python3 orchestrator/github.py doctor`.")
        return ""
    reason = (fail_reason or "").split(":")[0].strip()
    if reason in {"ROLE_EXECUTION_ERROR", "ROLE_TOTAL_FAILURE",
                  "ARTIFACT_TRANSFER_ERROR"}:
        opened = [r for r in (role_prs or []) if r.get("pr_url")]
        if opened:
            return (
                f"A role failed, but {len(opened)} existing pull request(s) retain "
                "their own recorded check and review evidence. Read that evidence "
                "and the failed role's error before continuing as a person. "
                "Do not resubmit the whole build to finish one role.")
    if reason == "COORDINATOR_SESSION_INTERRUPTED":
        # A recycled coordinator does not un-open the pull requests it already
        # published, and telling the reader to "submit the SAME request again" when
        # one of them is green and approved is how you get a duplicate sixty-minute
        # three-role build. Seen live (2026-09-03): the backend pull request had
        # passed 65 checks and been approved, the frontend one was red, and this
        # sentence still said resubmit. Same principle the ITERATION_CAP text
        # already states: pull requests that did pass are settled.
        rows = list(role_prs or [])
        opened = [r for r in rows if r.get("pr_url")]
        if opened:
            return (
                f"The coordinator Runtime was recycled, but {len(opened)} pull "
                "request(s) it already opened are unaffected. Open each one and read "
                "its executable check and Assessment comment: that is the durable "
                "record. Continue from there as a person, and do NOT resubmit the "
                "whole build to finish one pull request.")
    if reason in _NEXT_ACTION:
        return _NEXT_ACTION[reason]
    if reason.startswith("PR_PREFLIGHT_ERROR"):
        # The reason string carries the specific cause (which half of the GitHub
        # config is missing). Without this branch a terminal needs_human/failed run
        # answered next_action: "" -- the one field whose whole job is to say what to
        # do, empty, on a run that stopped before any agent started.
        detail = (fail_reason or "").split(":", 2)[-1].strip()
        return ("The run stopped in pre-flight, before any agent work or cost: "
                + (detail or "the GitHub pull-request destination did not resolve.")
                + " Fix that, then resubmit.")
    if reason.startswith("RUNTIME_NOT_WIRED"):
        return ("A routed role has no wired runtime ARN. Deploy that role (Lab 1) or "
                "wire its ARN, then resubmit; the engine never falls back to a local "
                "build.")
    if reason.startswith(("UNKNOWN_PRESET", "UNKNOWN_ROLE")):
        return "That preset or role does not exist. Call list_presets for what does."
    # No PR_NO_GATEWAY branch here on purpose: a failed PR step never becomes a
    # fail_reason (the BUILD succeeded), it lands in run.pr["error"], which the
    # status == "passed" arm above reads. A branch here would be unreachable.
    return ""


_RESUBMITTABLE_REASONS = {
    "ROLE_EXECUTION_ERROR",
    "ARTIFACT_TRANSFER_ERROR",
    "ROLE_TOTAL_FAILURE",
    "ENGINE_STALL",
    "COORDINATOR_SESSION_INTERRUPTED",
    # The validator authored a check and the engine then lost the tree it was
    # authored against, so nothing was ever executed. That is OUR bookkeeping
    # failure, not a judged outcome: no verdict was reached, so there is nothing
    # for a person to read and repeating the request is the honest recovery. It is
    # deliberately NOT grouped with ROLE_PR_BLOCKED / ITERATION_CAP, which mean a
    # real gate or review decided against real work.
    "WORK_TREE_MISSING",
}


def resubmission_allowed(status: str, fail_reason: str | None,
                         role_prs: list[dict] | None = None, *,
                         work_items: Any = None) -> bool:
    """Whether immediately repeating the same request can recover this outcome.

    ``role_prs`` is what makes this honest for an interrupted coordinator: a run that
    already published pull requests has real, judged work on GitHub, so repeating the
    request duplicates it rather than recovering it. Resubmission stays allowed only
    while nothing was published yet.
    """
    if status not in ("failed", "needs_human"):
        return False
    reason = (fail_reason or "").split(":")[0].strip()
    if reason not in _RESUBMITTABLE_REASONS:
        return False
    if any(row.get("pr_url") for row in _published_rows(role_prs, work_items)):
        return False
    return True


def public_result(run: Any) -> dict:
    return {
        "run_id": run.run_id,
        "submitted_by": public_submitter(run.user_identity),
        "status": run.status,
        # CLI users poll this payload for 10-20 minutes. A bare "running" makes a
        # healthy build indistinguishable from a stuck one even though the engine
        # already tracks each role. Keep the same phase/progress facts the console
        # exposes on its Run endpoint.
        "phase": run.phase,
        "progress": public_progress(run),
        "work_items": {
            agent: item.public()
            for agent, item in run.work_items.items()
        },
        "integration_brief": run.integration_brief,
        "integration_base": run.integration_base,

        "final_base_branch": run.final_base_branch,
        "role_prs": run.role_prs,
        "gate_history": run.gate_history,
        # The gate's summary is the authored check's own last line: the closest thing
        # to a human-readable verdict, so it belongs in the public payload.
        # `executed` separates "no check ran" from a red check: a run that stopped
        # before its checker wrote anything was shown as "gate RED" / "Failed".
        "gate": {"passed": bool(run.gate and run.gate["passed"]),
                 "executed": bool(run.gate),
                 "checks": (run.gate or {}).get("checks", []),
                 "summary": (run.gate or {}).get("summary", "")},
        "pr_url": run.pr_url,
        "merge_state": run.merge_state,
        # A run rejected at admission (empty task, bad roles) has `agents` from the
        # request but no `roles` yet, so read through `roles` rather than indexing it:
        # a failed run must still render its result, not 500 the API.
        "composed_from": [run.roles[a] for a in run.agents if a in run.roles],
        "iterations": run.iterations,
        # additive fields (API_CONTRACT.md "Engine additions"):
        "artifact_endpoint": run.artifact_endpoint,
        "composed_branch": run.composed_branch,
        "composed_commit": run.composed_commit,
        "fail_reason": run.fail_reason,
        "route": run.route,
        "review": run.review,
        "pr": run.pr,
        "compose_base": run.compose_base,
        # What to DO about this outcome, in one sentence. `needs_human` alone cannot
        # tell "the gate stayed red on real work" from "a role produced nothing",
        # and those have opposite next steps.
        "next_action": next_action(
            run.status, run.fail_reason, run.pr, run.pr_url, run.role_prs,
            work_items=run.work_items),
        "resubmission_allowed": resubmission_allowed(
            run.status, run.fail_reason, run.role_prs, work_items=run.work_items),
    }
