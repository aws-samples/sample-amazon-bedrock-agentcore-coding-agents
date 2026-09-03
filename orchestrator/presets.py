"""Routing: which ROLES handle a request. That is the entire job.

The attendee types any request they like. Nothing in this repository knows what it
will be, so routing cannot and must not decide anything about the work itself: no
target shape, no language, no protocol, no sample module, no expected answer. It picks
a role set and stops.

This replaced a router that pattern-matched the attendee's prose against a registry of
canned workflows, each bound to a bundled sample use case. That design answered the
question before the agents saw it, which is the opposite of what the workshop teaches.

Three ways in:

  * ``roles``: name the roles directly, with any request text at all.
  * ``preset``: one of the starting points below, which is a REQUEST TEXT plus a role
    set. Presets exist so an attendee who arrives without an idea starts in a minute;
    they are examples, not a menu the system is limited to.
  * a REQUEST ALONE, which is the ordinary case: the attendee types a sentence and
    names nothing. The MODEL then reads it and picks the capabilities the work calls
    for, so a command line tool gets no frontend builder. That decision is a judgement
    and it is made by a model, never by a keyword table here.

Fail-loud rules, all of them structural, and none of them a judgement about the work:

  * An unknown preset or an unknown role raises. Never a nearest match.
  * An EMPTY request raises, so the coordinator ASKS instead of inventing a task.
  * A build always routes the CHECKER. Validation is agentic: with no validator there
    is no authored acceptance check, and with no check the gate is red by definition. A
    build nobody can verify is not a smaller build, it is an unverifiable one.
  * A build always routes at least one BUILDER. A checker alone has nothing to check.
  * An unreachable model routes EVERY builder and records why. Silently dropping work
    the attendee asked for is worse than running a role that turns out to have nothing
    to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import roles as _roles

# The roles this harness can dispatch come from the registry (``roles.py``), which
# is the ONE place the roster is declared and is configurable at runtime
# (WORKSHOP_ROLES). Nothing here names an agent or counts them: a preset asks for
# CAPABILITIES ("a backend", "a frontend", "a checker") and the registry answers
# with whatever roles this deployment serves. Swapping opencode for Codex, or
# running a two-role team, needs no edit in this file.


def ROLES() -> dict[str, str]:
    """Dispatchable role id -> the job it plays. Read from the registry."""
    return _roles.role_names()


def BUILDERS() -> tuple[str, ...]:
    """The MAKER role ids. However many this roster serves."""
    return _roles.builder_ids()


def CHECKERS() -> tuple[str, ...]:
    """The CHECKER role ids. Disjoint from BUILDERS by construction."""
    return _roles.checker_ids()


def _cap(capability: str) -> list[str]:
    """The served role ids offering a capability, as a list a preset can splice in.
    Empty when this roster serves none, which routing then reports as a real
    failure rather than substituting something the attendee did not ask for."""
    return list(_roles.by_capability(capability))


class RouteError(ValueError):
    """A routing request that cannot be honored. Fails loud (a 400, never a guess)."""


# --------------------------------------------------------------------------- presets
# STARTING POINTS, not a catalogue of what the system supports. Each is a request an
# attendee can send as-is, edit, or ignore entirely. They deliberately differ in the
# SHAPE of the work rather than being variations on one task, because the point is to
# show that the same loop closes around any shape:
#
#   * one needs no HTTP at all (so the gate is proven transport-agnostic)
#   * one has no service to probe (so the gate is proven to work on files alone)
#   * one starts from an existing tree (so "do not regress it" becomes the bar)
#   * one starts from a failure (so the reproduction itself is the bar)
#
# `task` is the text the attendee submits. The repository ships THAT SENTENCE and
# nothing else: no module, no scaffold, no contract, no reference solution.
#
# `needs` lists CAPABILITIES, not agent ids. "backend" means "whichever role this
# deployment serves for the service side", so a roster swap (opencode -> Codex, or
# a two-role team) changes nothing here. The checker is NOT listed: every build
# route gets it structurally in `_resolve_needs`, because a build nobody can verify
# is not a valid route.
PRESETS: dict[str, dict[str, Any]] = {
    # THE ROOM'S BUILD. One service, so one pull request and the same ~11-minute clock
    # as the library API, but the deliverable is something the whole room can PLAY at
    # the end of Lab 2, each team its own game on its own box through code-server's
    # /proxy/<port>/ path.
    #
    # The prompt is deliberately three sentences. Everything that makes the result
    # runnable where the room runs it (one service serving page and API, relative URLs
    # behind a path prefix, no CDN, PORT, persistence, validation, documentation) is the
    # HARNESS's job -- `harness-skills/skills/backend-engineering/SKILL.md` and the
    # validator steering -- not the prompt's. That is the point the workshop makes: a
    # person states the goal once, and the loop carries the standards. A prompt that
    # spelled all of that out would be prompting in disguise, and the attendee would
    # rightly conclude the agents only work when you write their spec for them.
    "game-from-scratch": {
        "title": "Build a browser game (the room's build)",
        "needs": ["backend"],
        "task": (
            "Build a small browser arcade game with a persistent high-score table. "
            "Pick a classic (Snake, Breakout, 2048, or something equally simple and "
            "real-time) and make it genuinely fun to play in a browser tab."),
    },
    "service-from-scratch": {
        "title": "Build an HTTP API (backend only)",
        "needs": ["backend"],
        "task": (
            "Build an HTTP API for a personal library: books with a title, author, "
            "and read/unread status.\n"
            "\n"
            "It must support: adding a book; listing books with a filter by status "
            "and a search across title and author; updating a book; deleting one; "
            "and a summary endpoint with counts by status.\n"
            "\n"
            "Requirements that matter more than the endpoint list: the data must "
            "survive a restart; invalid input must be refused with a clear error and "
            "a correct status code (empty title, unknown status, updating or deleting "
            "an id that does not exist); and the API must be documented enough that a "
            "caller can use it without reading the source.\n"
            "\n"
            "Build it as a maintained service. Use a web framework rather "
            "than a hand-rolled request handler, dependencies declared in the "
            "manifest for your ecosystem, and the concerns (routing, domain logic, "
            "storage, validation) in separate modules. Decide the language, the "
            "framework, and the storage yourselves."),
    },
    "web-app": {
        "title": "Build a web app, front and back",
        "needs": ["backend", "frontend"],
        "task": (
            "Build an app for keeping a shopping list that a household could share.\n"
            "\n"
            "It must support: adding an item with a quantity; ticking an item off and "
            "back on; editing an item; removing one; filtering to what is still "
            "needed; and clearing everything already bought.\n"
            "\n"
            "The data must survive a restart of the service, invalid input must be "
            "refused with a clear error rather than accepted, and the page must do "
            "all of the above against that same service (the page never owns the "
            "data).\n"
            "\n"
            "Build both sides as a maintained project. Use a web framework on "
            "the service side, a component-based UI rather than one hand-written file "
            "on the page side, and dependencies declared in the manifest for your "
            "ecosystem. Decide the languages, the frameworks, and the storage "
            "yourselves."),
    },
    # The FLAGSHIP request: deliberately the largest thing on this list, because a
    # team of agents is only interesting when the job is bigger than one obvious
    # file. It names OUTCOMES and CONSTRAINTS, never files, frameworks, or
    # endpoints: several features that must hold together, real persistence, real
    # input rejection, and a UI over the same service. That gives both builders
    # substantial parallel work and gives the validator something worth checking,
    # so the sample console's Agents tabs show a team building a system.
    "project-from-scratch": {
        "title": "Build a whole project (the big one)",
        "needs": ["backend", "frontend"],
        "task": (
            "Build an issue tracker that a team could actually use.\n"
            "\n"
            "It must support: creating an issue with a title and description; "
            "listing issues with a filter by status; changing an issue's status "
            "through the lifecycle open -> in progress -> done; adding comments to "
            "an issue; and a per-status count summary.\n"
            "\n"
            "Requirements that matter more than the feature list: the data must "
            "survive a restart of the service; invalid input must be refused with a "
            "clear error rather than accepted (an unknown status, an empty title, a "
            "comment on an issue that does not exist); and an illegal status "
            "transition must be rejected. Include a page a person can use to do all "
            "of this against the same service.\n"
            "\n"
            "Build it as a team project. Use a web framework "
            "rather than a hand-rolled request handler, a component-based UI rather "
            "than one hand-written file, dependencies declared in the manifest for "
            "your ecosystem, and split the concerns into clearly named modules. A "
            "single file that happens to pass is not the deliverable.\n"
            "\n"
            "Decide the language, the framework, the storage, the protocol, the file "
            "layout, and the shape of the interface yourselves."),
    },
    "cli-tool": {
        "title": "Build a command line tool",
        "needs": ["backend"],
        "task": (
            "Write a command line tool that reads UTF-8 text on stdin and reports "
            "line, word, and character counts. For valid input, use the same counting "
            "rules and order as `wc -l -w -m`, then print the three integers on one "
            "space-separated line. Exit nonzero with a clear stderr message when the "
            "input is not valid UTF-8."),
    },
    "add-a-feature": {
        "title": "Extend something that already exists",
        "needs": ["backend"],
        "task": ("Add a history feature to the service in this workspace: callers can "
                 "list their recent requests. Existing behavior must not change."),
    },
    "fix-from-a-repro": {
        "title": "Fix a bug from a reproduction",
        "needs": ["backend"],
        "task": ("Here is a failing case: asking for a conversion with a negative "
                 "value returns a result instead of refusing. Fix it so invalid input "
                 "is rejected, and do not break the valid cases."),
    },
    "your-own": {
        "title": "Type your own request",
        # Every maker on the roster, whatever it is: the attendee's request is
        # unknown, so nothing here may narrow who works on it.
        "needs": ["*"],
        "task": "",   # the attendee writes this; there is no default answer
    },
    "review-a-run": {
        "title": "Review an earlier run",
        "needs": [],            # no maker: the checker alone reads an earlier run
        "read_only": True,
        "task": "Review the previous run and post an assessment on its pull request.",
    },
}


def _resolve_needs(spec: dict[str, Any]) -> list[str]:
    """Turn a preset's CAPABILITIES into the role ids this deployment serves.

    ``"*"`` means every maker on the roster. The checker is appended for any build
    route (never for a read-only one), which is what makes maker-checker structural
    rather than something each preset has to remember.
    """
    needs = spec.get("needs") or []
    ids: list[str] = []
    if "*" in needs:
        ids.extend(_roles.builder_ids())
    else:
        for capability in needs:
            ids.extend(_cap(capability))
    # The checker rides every route: on a build it decides the gate, and a
    # read-only route is the checker's alone (it reads an earlier run and assesses
    # it, and it never edits a workspace).
    ids.extend(_roles.checker_ids())
    return list(dict.fromkeys(ids))


@dataclass
class Route:
    """The routing verdict: which roles, and WHY."""

    preset: str                            # the preset id, or "custom"
    rule: str                              # human-readable reason, for the run log
    agents: list[str] = field(default_factory=list)
    read_only: bool = False

    def public(self) -> dict[str, Any]:
        return {"preset": self.preset, "rule": self.rule,
                "agents": self.agents, "read_only": self.read_only}


def _validate(agents: list[str], read_only: bool, where: str) -> None:
    """The structural guarantees, expressed in KINDS rather than role names, so they
    hold for any roster this deployment serves. Raise rather than route something
    unverifiable."""
    served = ROLES()
    for a in agents:
        if a not in served:
            raise RouteError(f"UNKNOWN_ROLE:{a}")
    if not agents:
        raise RouteError(f"NO_ROLES_ROUTED:{where}")
    if read_only:
        return
    if not [a for a in agents if a in CHECKERS()]:
        raise RouteError(
            "NO_CHECKER_ROUTED: a build must route a checker. Validation is "
            "agentic: with no authored acceptance check the gate is red by definition, "
            "so a build nobody can verify is not a valid route.")
    if not [a for a in agents if a in BUILDERS()]:
        raise RouteError("NO_BUILDER_ROUTED: the checker has nothing to check.")


def resolve(task: str = "", preset: str | None = None,
            roles: list[str] | None = None, *,
            offline_fixture: bool = False) -> Route:
    """Resolve a request to a role set.

    Three ways in, in precedence order, and none of them pattern-matches prose:

    * ``roles``: the attendee named them. Nothing to decide.
    * ``preset``: a starting point, whose capabilities are declared with it.
    * ``task`` alone: ASK THE MODEL which capabilities the request needs. This is the
      real surface, because the attendee types whatever they like. It used to route
      every role on the roster unconditionally, which dispatched a frontend builder
      for a command line tool -- deterministic, and wrong for the same reason a
      keyword table would be.

    Routing decides WHO WORKS and nothing else: no target shape, no language, no
    protocol, no expected answer. The structural rules still hold on every path (a
    build always gets a checker and at least one maker), because those are invariants
    rather than judgements, and the model is not asked to weigh them.
    """
    if roles:
        agents = _ordered(roles)
        _validate(agents, read_only=False, where="roles")
        return Route(preset="custom", agents=agents,
                     rule=f"explicit roles: {', '.join(agents)}")
    if preset:
        if preset not in PRESETS:
            raise RouteError(f"UNKNOWN_PRESET:{preset}")
        spec = PRESETS[preset]
        # A preset that carries no request text of its own (``your-own``) is not really
        # a preset: the attendee supplies the request, so fall through and route it the
        # way a typed request is routed. Otherwise "type your own request" would be the
        # one path that ignores what was typed and dispatched the whole roster.
        supplies_its_own_task = bool(str(spec.get("task") or "").strip())
        if supplies_its_own_task or not task.strip():
            agents = _ordered(_resolve_needs(spec))
            read_only = bool(spec.get("read_only"))
            _validate(agents, read_only, where=preset)
            return Route(preset=preset, agents=agents, read_only=read_only,
                         rule=f"preset {preset!r}: {spec['title']}")
    if task.strip():
        import integration_plan  # noqa: PLC0415 (lazy: offline tests skip boto3)

        capabilities = list(_roles.maker_capabilities())
        chosen, reason = integration_plan.select_capabilities(
            task, capabilities, offline_fixture=offline_fixture)
        ids: list[str] = []
        for capability in chosen:
            ids.extend(_cap(capability))
        ids.extend(_roles.checker_ids())
        agents = _ordered(list(dict.fromkeys(ids)))
        _validate(agents, read_only=False, where="routed request")
        return Route(preset="routed", agents=agents,
                     rule=f"model routed {', '.join(chosen)}: {reason}")
    raise RouteError(
        "EMPTY_TASK: there is nothing to route. Ask what should be built, in the "
        "user's own words, or offer a starting point.")


def _ordered(role_ids: list[str]) -> list[str]:
    """Builders first, checker last: the order the run view reads in, and the order the
    engine's join expects (the checker runs after the builders it checks). Ordering is
    the registry's (``roles.order``), so it holds for any roster; an unregistered id is
    preserved at the end so ``_validate`` is the thing that rejects it, with a name."""
    seen = list(dict.fromkeys(role_ids))
    known = [_roles.get(r) for r in seen if r in _roles.BY_ID]
    ordered = [r.id for r in _roles.order(known)]
    return ordered + [r for r in seen if r not in _roles.BY_ID]


def default_task(preset: str) -> str:
    """The request text a preset starts from ("" for ``your-own``)."""
    if preset not in PRESETS:
        raise RouteError(f"UNKNOWN_PRESET:{preset}")
    return PRESETS[preset]["task"]


def public_presets() -> list[dict[str, Any]]:
    """The starting points, for GET /api/presets. ONE source: the console renders
    these rather than keeping its own copy, so they cannot drift. ``roles`` is
    resolved against the SERVED roster, so a preset never advertises an agent this
    deployment does not run."""
    return [{"preset": pid, "title": spec["title"],
             "roles": _ordered(_resolve_needs(spec)),
             "task": spec["task"], "read_only": bool(spec.get("read_only"))}
            for pid, spec in PRESETS.items()]
