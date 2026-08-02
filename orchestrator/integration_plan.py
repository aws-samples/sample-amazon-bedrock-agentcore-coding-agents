"""Model-authored shared contract for independent builder work.

The plan coordinates people-like work without deciding the implementation. It
records the task verbatim, assigns responsibilities to the routed builders, and
states only the API/UX/data boundaries they must share. Each builder receives the
same brief but a different writable checkout.

This module never grades code. The independent validator still authors the only
acceptance executable, and its real exit code remains the verdict.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

import llm
from work_items import WorkItem, dependency_order


PLAN_MODEL = os.environ.get(
    "WORKSHOP_INTEGRATION_PLAN_MODEL",
    os.environ.get("ORCHESTRATOR_MODEL_ID", "claude-sonnet-4-6"),
)

_SYSTEM = """You are the integration lead for a small software team.
Turn one user request and a dynamic role roster into a shared implementation
brief. Preserve optionality: do not choose filenames, frameworks, languages,
database products, component names, or repository layout unless the user already
required them. Define the smallest concrete interface the independent builders
need to agree on. That interface may include endpoint paths, payload schemas, and
runtime configuration when those facts are necessary for separately built parts
to connect. Assign exclusive outcomes: no role may duplicate another role's
capability merely to make its isolated checkout standalone. Builders start
independently and do not see one another's implementation.

Return strict JSON only:
{
  "summary": "one sentence",
  "shared_contract": ["observable boundary or invariant", "..."],
  "role_assignments": {
    "<agent id>": {
      "objective": "what this role owns",
      "provides": ["capability/interface it promises"],
      "consumes": ["capability/interface it relies on"]
    }
  },
  "merge_order": ["<builder agent id>", "..."],
  "open_questions": ["only a material ambiguity that cannot be left flexible"]
}

Every routed builder id must appear exactly once in role_assignments and
merge_order. Keep open_questions empty when the task is executable as written.
The merge order is an integration sequence, not an execution dependency: all
builders still work in parallel. The assignments must partition ownership while
the provides/consumes lists make the integration seam explicit."""

_REPAIR_SYSTEM = """You triage evidence from an executable integration gate or
code review back to the builder who owns the affected work. Return strict JSON:
{"agents": ["<builder agent id>", "..."], "reason": "one sentence"}.
Choose only from the supplied builder ids. Select every builder whose work may
need to change; do not select the validator, which re-authors its check
independently. An empty agents list is allowed only when the evidence clearly
shows the check itself is wrong and no builder code should change. This routes
repair work; it never changes or interprets the gate verdict."""


_ROUTE_SYSTEM = """You choose which CAPABILITIES a build request needs. Return strict
JSON: {"capabilities": ["<capability>", "..."], "reason": "one sentence"}.
Choose only from the supplied capability list. Judge the REQUEST, not its wording:
select a capability when the request genuinely needs that kind of work, and leave it
out when it does not. A command line tool or a service with no interface needs no
frontend; a page with no data of its own needs no backend. Select at least one.
Never select the checker capability: every build gets it structurally, because a
build nobody can verify is not a smaller build. You are choosing WHO WORKS, never
what they build, what language they use, or what a correct answer looks like."""


def select_capabilities(
    task: str,
    available: list[str],
    *,
    offline_fixture: bool = False,
) -> tuple[list[str], str]:
    """Ask the model which maker capabilities this request needs.

    This is the routing decision, and it is the model's: nothing here pattern-matches
    the attendee's prose. That matters because the request is whatever they typed, so
    a keyword table would answer the question before any agent saw it -- and a router
    that always returns every role is the same mistake wearing a different hat (it
    dispatches a frontend for a command line tool).

    Fails OPEN to every capability, and says so in the reason. An unreachable model
    must not silently narrow a build: routing too wide wastes a turn, routing too
    narrow drops work the attendee asked for.
    """
    allowed = list(dict.fromkeys(available))
    if not allowed:
        return [], "this roster serves no maker capability"
    if offline_fixture:
        return allowed, "offline fixture routes every capability"
    try:
        response = llm.invoke(
            PLAN_MODEL,
            "Choose the capabilities this request needs.\n\n"
            f"REQUEST:\n{task}\n\n"
            f"AVAILABLE CAPABILITIES: {json.dumps(allowed)}",
            system=_ROUTE_SYSTEM,
            max_tokens=400,
        )
        parsed = _json_object(response.get("text") or "")
        chosen = parsed.get("capabilities")
        if not isinstance(chosen, list) or not chosen:
            raise IntegrationPlanError("ROUTE_INVALID: capabilities is not a list")
        selected = [str(c) for c in chosen]
        unknown = [c for c in selected if c not in allowed]
        if unknown or len(selected) != len(set(selected)):
            raise IntegrationPlanError(
                f"ROUTE_INVALID: unknown or duplicate capability {unknown}")
        return selected, str(parsed.get("reason") or "").strip()
    except Exception as exc:  # noqa: BLE001 (routing fails OPEN, never narrower)
        return allowed, (
            "capability routing was unavailable, so every maker is routed: "
            f"{type(exc).__name__}")


class IntegrationPlanError(RuntimeError):
    """The coordinator could not produce a usable shared contract."""


def _json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise IntegrationPlanError("INTEGRATION_BRIEF_INVALID: no JSON object")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise IntegrationPlanError(
            f"INTEGRATION_BRIEF_INVALID: {exc}") from exc
    if not isinstance(value, dict):
        raise IntegrationPlanError(
            "INTEGRATION_BRIEF_INVALID: response is not an object")
    return value


def _validate(plan: dict[str, Any], items: list[WorkItem]) -> dict[str, Any]:
    builder_ids = [item.agent for item in items]
    assignments = plan.get("role_assignments")
    order = plan.get("merge_order")
    contract = plan.get("shared_contract")
    if not isinstance(assignments, dict) or set(assignments) != set(builder_ids):
        raise IntegrationPlanError(
            "INTEGRATION_BRIEF_INVALID: role_assignments must name every builder")
    if not isinstance(order, list) or len(order) != len(builder_ids) \
            or set(order) != set(builder_ids):
        raise IntegrationPlanError(
            "INTEGRATION_BRIEF_INVALID: merge_order must name every builder once")
    if not isinstance(contract, list) or not all(
            isinstance(row, str) and row.strip() for row in contract):
        raise IntegrationPlanError(
            "INTEGRATION_BRIEF_INVALID: shared_contract must be a string list")

    normalized_assignments: dict[str, dict[str, Any]] = {}
    for agent_id in builder_ids:
        row = assignments[agent_id]
        if not isinstance(row, dict) or not str(row.get("objective") or "").strip():
            raise IntegrationPlanError(
                f"INTEGRATION_BRIEF_INVALID: {agent_id} has no objective")
        normalized_assignments[agent_id] = {
            "objective": str(row["objective"]).strip(),
            "provides": [str(v).strip() for v in (row.get("provides") or [])
                         if str(v).strip()],
            "consumes": [str(v).strip() for v in (row.get("consumes") or [])
                         if str(v).strip()],
        }

    return {
        "summary": str(plan.get("summary") or "").strip(),
        "shared_contract": [str(v).strip() for v in contract],
        "role_assignments": normalized_assignments,
        "merge_order": list(order),
        "open_questions": [str(v).strip()
                           for v in (plan.get("open_questions") or [])
                           if str(v).strip()],
    }


def create(task: str, items: Iterable[WorkItem],
           *, offline_fixture: bool = False) -> dict[str, Any]:
    """Create and validate one shared brief.

    Offline tests use a labelled neutral plan so they can exercise plumbing
    without a model. The shipped path fails loud when planning cannot run; it
    never invents a contract with deterministic endpoint or file conventions.
    """
    builders = list(items)
    if offline_fixture:
        plan = {
            "summary": "Offline fixture plan for orchestration machinery only.",
            "shared_contract": [
                "Implement the request as written and expose enough evidence for "
                "the independent validator to execute.",
            ],
            "role_assignments": {
                item.agent: {
                    "objective": item.role,
                    "provides": [item.capability],
                    "consumes": [],
                }
                for item in builders
            },
            "merge_order": [item.agent for item in builders],
            "open_questions": [],
        }
    else:
        roster = [{
            "agent": item.agent,
            "work_id": item.work_id,
            "role": item.role,
            "capability": item.capability,
        } for item in builders]
        base_prompt = (
            "USER REQUEST (verbatim):\n"
            f"{task}\n\n"
            "ROUTED BUILDERS:\n"
            f"{json.dumps(roster, indent=2)}\n\n"
            "Write the shared integration brief now."
        )
        prior_text = ""
        prior_error = ""
        for attempt in range(2):
            prompt = base_prompt
            if attempt:
                prompt += (
                    "\n\nYOUR PREVIOUS RESPONSE WAS INVALID.\n"
                    f"Validation error: {prior_error}\n"
                    "Required builder ids, exactly once each: "
                    f"{json.dumps([item.agent for item in builders])}\n"
                    "Repair the JSON while preserving the user's request and "
                    "implementation flexibility. Return the complete object.\n\n"
                    f"Previous response:\n{prior_text}"
                )
            try:
                response = llm.invoke(
                    PLAN_MODEL, prompt, system=_SYSTEM, max_tokens=2500)
            except Exception as exc:
                raise IntegrationPlanError(
                    f"INTEGRATION_BRIEF_UNAVAILABLE: {exc}") from exc
            prior_text = response.get("text") or ""
            try:
                plan = _validate(_json_object(prior_text), builders)
                break
            except IntegrationPlanError as exc:
                prior_error = str(exc)
                if attempt:
                    raise IntegrationPlanError(
                        f"{prior_error} (repair attempt exhausted)") from exc
        else:  # pragma: no cover - the bounded loop always breaks or raises
            raise IntegrationPlanError("INTEGRATION_BRIEF_INVALID")

    normalized = plan if not offline_fixture else _validate(plan, builders)
    by_agent = {item.agent: item for item in builders}
    previous: WorkItem | None = None
    for agent_id in normalized["merge_order"]:
        item = by_agent[agent_id]
        item.depends_on = [previous.work_id] if previous else []
        previous = item
    dependency_order(builders)
    return normalized


def markdown(task: str, plan: dict[str, Any], items: Iterable[WorkItem]) -> str:
    """Human- and agent-readable copy staged beside each independent checkout."""
    by_agent = {item.agent: item for item in items}
    lines = [
        "# Integration Brief",
        "",
        "## Request",
        "",
        task.strip(),
        "",
        "## Shared Contract",
        "",
    ]
    lines.extend(f"- {row}" for row in plan.get("shared_contract") or [])
    lines += ["", "## Role Ownership", ""]
    for agent_id in plan.get("merge_order") or []:
        row = (plan.get("role_assignments") or {}).get(agent_id, {})
        item = by_agent[agent_id]
        lines += [
            f"### {item.role} (`{agent_id}`, `{item.work_id}`)",
            "",
            str(row.get("objective") or ""),
            "",
        ]
        provides = row.get("provides") or []
        consumes = row.get("consumes") or []
        if provides:
            lines.append("Provides:")
            lines.extend(f"- {value}" for value in provides)
            lines.append("")
        if consumes:
            lines.append("Consumes:")
            lines.extend(f"- {value}" for value in consumes)
            lines.append("")
    lines += [
        "## Merge Order",
        "",
        " -> ".join(plan.get("merge_order") or []),
        "",
        "Builders execute independently and each opens its OWN pull request against "
        "the default branch. Nothing is assembled into a combined candidate. This "
        "order is only the sequence the coordinator prefers when two pull requests "
        "are ready at once, and it decides who refreshes if a merge moves a path "
        "someone else also changed.",
        "",
        "Ownership is exclusive. Implement the outcome assigned to your role, not "
        "the entire request in isolation. Your checkout is intentionally incomplete "
        "until the coordinator combines it with the other role checkouts. Do not "
        "ship a stand-in or second implementation of a sibling role's capability. "
        "If you need a local substitute while developing, keep it out of your "
        "submitted tree. Shared manifests and entrypoints are allowed when the "
        "integration needs them; the later role in the merge order reconciles those "
        "files against the earlier role's actual patch.",
    ]
    questions = plan.get("open_questions") or []
    if questions:
        lines += ["", "## Open Questions", ""]
        lines.extend(f"- {value}" for value in questions)
    return "\n".join(lines).rstrip() + "\n"


def review_context(plan: dict[str, Any],
                   items: Iterable[WorkItem]) -> dict[str, Any]:
    """Dynamic ownership and provenance for the checker and code reviewer.

    These are facts recorded by this run, not an answer key. They let independent
    reviewers detect a disconnected duplicate implementation without teaching the
    engine what any particular deliverable should look like.
    """
    assignments = plan.get("role_assignments") or {}
    contributions = []
    builders = [item for item in items if item.kind == "builder"]
    for item in dependency_order(builders):
        assignment = assignments.get(item.agent) or {}
        contributions.append({
            "agent": item.agent,
            "work_id": item.work_id,
            "role": item.role,
            "capability": item.capability,
            "objective": str(assignment.get("objective") or item.role),
            "provides": list(assignment.get("provides") or []),
            "consumes": list(assignment.get("consumes") or []),
            "changed_files": list(item.changed_files),
            "deleted_files": list(item.deleted_files),
        })
    return {
        "summary": str(plan.get("summary") or ""),
        "shared_contract": list(plan.get("shared_contract") or []),
        "contributions": contributions,
    }


def select_repair_agents(
    task: str,
    plan: dict[str, Any],
    items: Iterable[WorkItem],
    evidence: dict[str, Any],
    *,
    offline_fixture: bool = False,
) -> tuple[list[str], str]:
    """Route red evidence to owners, with an honest all-builders fallback."""
    builders = list(items)
    allowed = [item.agent for item in builders]
    if not allowed:
        return [], "no builder work items exist"
    if offline_fixture:
        return allowed, "offline fixture routes repair to every builder"

    payload = {
        "task": task,
        "shared_contract": plan.get("shared_contract") or [],
        "role_assignments": plan.get("role_assignments") or {},
        "builders": [{
            "agent": item.agent,
            "work_id": item.work_id,
            "capability": item.capability,
            "changed_files": list(item.changed_files),
            "deleted_files": list(item.deleted_files),
        } for item in builders],
        "evidence": evidence,
    }
    try:
        response = llm.invoke(
            PLAN_MODEL,
            "Route this evidence to its owning builders:\n"
            + json.dumps(payload, indent=2),
            system=_REPAIR_SYSTEM,
            max_tokens=800,
        )
        parsed = _json_object(response.get("text") or "")
        chosen = parsed.get("agents")
        if not isinstance(chosen, list):
            raise IntegrationPlanError(
                "REPAIR_ROUTING_INVALID: agents is not a list")
        selected = [str(agent) for agent in chosen]
        if len(selected) != len(set(selected)) or any(
                agent not in allowed for agent in selected):
            raise IntegrationPlanError(
                "REPAIR_ROUTING_INVALID: unknown or duplicate builder")
        return selected, str(parsed.get("reason") or "").strip()
    except Exception as exc:  # noqa: BLE001 (routing fails open to all owners)
        return allowed, (
            "repair routing was unavailable, so every builder receives the "
            f"evidence: {type(exc).__name__}")
