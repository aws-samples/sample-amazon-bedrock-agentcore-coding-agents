"""Task router: decides WHICH workflow and WHICH agents handle a submitted task.

The previous engine always dispatched all three agents in parallel. This module
instead picks a route, synthesizing the three references this workshop builds on:

  * A registry of versioned workflow descriptors (`coding/new-task-v1`,
    `coding/pr-review-v1`, ...). A task carries a `workflow_ref`; an unknown ref
    fails loud (400), never a guess.
  * A complexity check classifies the request before dispatching: simple issues
    take the short inline path, complex ones the full subagent pipeline. Our
    content rules (patch vs full build) are that check, made deterministic.
  * Explicit user intent wins the backend choice ("use opencode", "use validator");
    an unavailable explicit choice fails loud, and only silent defaults fall back.

The router is pure and deterministic: same task string in, same route out, no LLM.
On AgentCore the same ladder runs inside the orchestrator agent's tool-use loop:
the registry and the fail-closed rules do not change, only who evaluates them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))


def _repo_root() -> str:
    """Directory that contains the usecase packages.

    ``orchestrator/`` and ``usecase-*`` are siblings in the single repository.
    A deployed bundle can still override the location.
    """
    return os.environ.get("WORKSHOP_REPO_ROOT", os.path.dirname(_HERE))


class RouteError(ValueError):
    """An explicit routing request that cannot be honored. Fails loud (a 400, never a guess)."""


# ------------------------------------------------------------------ usecases
# Each usecase is a sample module package (a relative subpath under the workspace
# root) with its own deterministic grading contract. The route picks one; the
# engine builds and grades against it. Paths resolve at call time off the wirable
# root, so the same code runs from the repo and from a deployed bundle.
USECASES: dict[str, dict[str, str]] = {
    "sample-to-mcp": {
        "subdir": "usecase-sample-to-mcp",
        "module": "cost_analyzer",
        "label": "AWS sizing/pricing module → remote MCP server",
    },
    "critter-lab": {
        "subdir": "usecase-critter-lab",
        "module": "critter_lab",
        "label": "Critter Lab: the fun full-stack final project",
    },
}


def usecase_paths(usecase: str) -> dict[str, str]:
    """Resolve {dir, module, label, grading} for a usecase id, off the wirable
    workspace root. Fail loud on unknown."""
    if usecase not in USECASES:
        raise RouteError(f"UNKNOWN_USECASE:{usecase}")
    uc = USECASES[usecase]
    dir_ = os.path.join(_repo_root(), uc["subdir"])
    return {"dir": dir_, "module": uc["module"], "label": uc["label"],
            "grading": os.path.join(dir_, "grading")}


# ------------------------------------------------------------------ registry
# The workflow registry: versioned descriptors for our three-role harness.
# `agents` is the dispatch list (NOT always all three); `read_only` marks
# review-style workflows that must never produce a new artifact.
WORKFLOWS: dict[str, dict[str, Any]] = {
    "convert/sample-to-mcp-v1": {
        "version": "1.0.0",
        "agents": ["claude-code", "claude-code-validator", "opencode"],
        "usecase": "sample-to-mcp",
        "read_only": False,
        "description": "Full conversion: backend MCP server + chatbot UI + review gate.",
    },
    "build/fullstack-v1": {
        "version": "1.0.0",
        "agents": ["claude-code", "claude-code-validator", "opencode"],
        "usecase": "critter-lab",
        "read_only": False,
        "description": "Fun full-stack build: Critter Lab backend + frontend + review gate.",
    },
    "patch/backend-v1": {
        "version": "1.0.0",
        "agents": ["claude-code"],
        "usecase": "sample-to-mcp",
        "read_only": False,
        "description": "Small backend-only change; no frontend role is dispatched.",
    },
    "patch/frontend-v1": {
        "version": "1.0.0",
        "agents": ["opencode"],
        "usecase": "sample-to-mcp",
        "read_only": False,
        "description": "Frontend-only change; the chatbot UI is rebuilt, nothing else.",
    },
    "review/pr-v1": {
        "version": "1.0.0",
        "agents": ["claude-code-validator"],
        "usecase": "sample-to-mcp",
        "read_only": True,
        "description": "Review an existing run branch; the gate + critique run, no build.",
    },
}


@dataclass
class Route:
    """The router's verdict: which workflow, which agents, and WHY (the rule)."""

    workflow_ref: str
    rule: str                              # human-readable matched rule
    agents: list[str] = field(default_factory=list)
    usecase: str = "sample-to-mcp"
    read_only: bool = False
    version: str = "1.0.0"

    def public(self) -> dict[str, Any]:
        return {
            "workflow_ref": self.workflow_ref,
            "version": self.version,
            "rule": self.rule,
            "agents": self.agents,
            "usecase": self.usecase,
            "read_only": self.read_only,
        }


def _resolved(ref: str, rule: str) -> Route:
    wf = WORKFLOWS[ref]
    return Route(workflow_ref=ref, rule=rule, agents=list(wf["agents"]),
                 usecase=wf["usecase"], read_only=wf["read_only"],
                 version=wf["version"])


# Intent patterns, checked in ladder order. Each is (regex, workflow_ref, rule).
# Explicit agent words win; review/patch classification comes next;
# the full convert workflow is the default (so existing behavior is preserved).
_AGENT_INTENT = [
    (re.compile(r"\buse opencode\b|\bwith opencode\b|opencode로|opencode만"
                r"|\buse codex\b|\bwith codex\b|codex로|codex만", re.I),
     "patch/frontend-v1", 'explicit agent intent: "use opencode" → frontend role only'),
    (re.compile(r"\buse claude\b|\buse claude.code\b|claude.?code로|claude.?code만", re.I),
     "patch/backend-v1", 'explicit agent intent: "use claude code" → backend role only'),
    # The validator is a Claude Code steered by the acceptance contract. "use
    # validator" is the current phrasing; "use kiro" stays as a hidden back-compat
    # alias (mirroring the retained "use codex" → frontend), routing to the same
    # validator/review workflow so old prompts and tests keep working.
    (re.compile(r"\buse validator\b|\bwith validator\b|validator로|validator만"
                r"|\buse kiro\b|\bwith kiro\b|kiro로|kiro만", re.I),
     "review/pr-v1", 'explicit agent intent: "use validator" → validator/review role only'),
]
_REVIEW = re.compile(r"\breview\b.{0,40}\b(pr|pull request|branch|diff|run)\b"
                     r"|\b(pr|pull request)\b.{0,40}\breview\b", re.I | re.S)
_FULLSTACK = re.compile(r"full.?stack|critter|frontend and backend|"
                        r"backend and frontend|end.to.end app", re.I)
_PATCH = re.compile(r"^\s*(fix|patch|rename|bump|tweak|typo|adjust)\b", re.I)
_CONVERT = re.compile(r"\bconvert\b|\bmcp server\b|\bmodule\b|\bskill\b", re.I)


def route(task: str, workflow_ref: str | None = None) -> Route:
    """Resolve a task to a Route via the ladder. Deterministic; fails loud.

    Ladder (first match wins):
      1. explicit ``workflow_ref``        : unknown ref raises (fail-closed)
      2. explicit agent intent in text    : "use opencode" / "use validator"
      3. review intent                    : review an existing PR/branch (validator only)
      4. full-stack / fun-build intent    : Critter Lab usecase, all three roles
      5. patch intent                     : small change, backend role only (the
                                            SIMPLE path of the complexity check)
      6. convert intent                    : the full sample-to-mcp conversion
      7. no match                          : fail loud (RouteError), never default
                                            to a specific usecase (task-agnostic)
    """
    if workflow_ref:
        if workflow_ref not in WORKFLOWS:
            raise RouteError(f"UNKNOWN_WORKFLOW:{workflow_ref}")
        return _resolved(workflow_ref, "explicit workflow_ref (validated against the registry)")
    text = task or ""
    for pattern, ref, rule in _AGENT_INTENT:
        if pattern.search(text):
            return _resolved(ref, rule)
    if _REVIEW.search(text):
        return _resolved("review/pr-v1", "review intent → review workflow, validator role only")
    if _FULLSTACK.search(text):
        return _resolved("build/fullstack-v1",
                         "full-stack intent → Critter Lab usecase, all three roles")
    if _PATCH.search(text):
        return _resolved("patch/backend-v1",
                         "patch-sized request → backend role only (complexity check: SIMPLE)")
    if _CONVERT.search(text):
        return _resolved("convert/sample-to-mcp-v1",
                         "conversion request → full workflow (complexity check: COMPLEX)")
    # Task-agnostic: DO NOT silently default an unrecognized task to the
    # cost-analyzer conversion. The sample-to-mcp convert is one example usecase,
    # not a catch-all. A task that matches no intent fails loud so the caller
    # (engine -> fail_reason; the chat orchestrator -> a clarifying question)
    # can ask what to do instead of fabricating the cost_analyzer build.
    raise RouteError(
        "NO_ROUTE: could not classify this task to a workflow. Name the target "
        "(e.g. convert a module to an MCP server, patch the backend, build the "
        "full-stack app, or review a PR), or pass an explicit workflow_ref.")


def public_workflows() -> list[dict[str, Any]]:
    """Registry view for GET /api/workflows: the console renders this."""
    return [{"workflow_ref": ref, **{k: v for k, v in wf.items()}}
            for ref, wf in WORKFLOWS.items()]
