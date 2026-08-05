"""The role registry: one declarative roster, configurable, with no magic counts.

These tests pin the PROPERTIES the rest of the harness relies on, never the roster's
contents. Asserting "there are three roles named X, Y, Z" would recreate exactly the
hardcoding this registry removed, and would fail the moment an operator ran a
different team, which is a supported thing to do.
"""

from __future__ import annotations

import pytest

import roles


def test_every_registered_role_is_complete():
    """A registry entry carries everything a dispatch needs. A half-declared role
    would fail at dispatch time, in a microVM, instead of here."""
    for r in roles.REGISTRY:
        assert r.id and r.label and r.role_name and r.description, r
        assert r.kind in (roles.BUILDER, roles.CHECKER), r
        assert r.capability, r
        assert r.steering_file and r.harness_dir, r
        assert "{prompt}" in r.cli, r          # the CLI must actually take the task


def test_role_ids_are_unique():
    assert len(roles.BY_ID) == len(roles.REGISTRY)


def test_the_served_roster_can_build_and_check():
    """The structural guarantee behind maker-is-never-checker: the default roster has
    at least one MAKER and at least one CHECKER, and they are disjoint."""
    builders, checkers = set(roles.builder_ids()), set(roles.checker_ids())
    assert builders and checkers
    assert not (builders & checkers)


def test_hidden_roles_are_registered_but_not_served():
    """A kept-but-hidden role stays reachable in the registry and off the served
    roster, which is what makes it a restore path rather than dead code.

    Both hidden roles are hidden for a stated reason:
      * Codex must NOT be served: the GPT-5.x models it needs are unavailable on a
        Workshop Studio account and 401 on every dispatch.
      * The Claude Code validator is the BEDROCK-NATIVE, NO-KEY checker, kept for any
        account without a Kiro subscription. It is off the default roster because the
        event now provisions per-team Kiro subscriptions (central-account.yaml), so
        the served checker is Kiro on the attendee's own ksk_ key.
    """
    hidden = [r.id for r in roles.REGISTRY if r.hidden]
    assert hidden, "the restore paths should stay registered, not deleted"
    served = roles.roster_ids()
    for role_id in hidden:
        assert role_id in roles.BY_ID
        assert role_id not in served
    assert "codex" in hidden, "Codex is disabled at events (no GPT entitlement)"
    assert "claude-code-validator" in hidden, (
        "the Bedrock-native no-key checker must stay a registered restore path")
    # And the flip is pinned in the direction it now runs: Kiro is the served checker.
    assert roles.checker_ids() == ("kiro",), roles.checker_ids()


def test_ordering_puts_makers_before_the_checker():
    """The order the engine's join and the run view both depend on."""
    ordered = roles.roster_ids()
    kinds = [roles.get(r).kind for r in ordered]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == roles.BUILDER else 1)
    assert roles.get(ordered[-1]).kind == roles.CHECKER


def test_dispatch_tool_names_are_unique_per_capability():
    """Two served roles must not claim the same dispatch tool, which would make one
    of them unreachable from the orchestrator."""
    tools = [r.dispatch_tool for r in roles.roster()]
    assert len(tools) == len(set(tools)), tools


def test_workshop_roles_overrides_the_roster(monkeypatch):
    """The roster is CONFIGURABLE: an operator runs a different or smaller team with
    one env var and no code change. This is also how a hidden role is restored: the
    Claude Code validator is the Bedrock-native no-key checker, off the served roster
    but one variable away."""
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,claude-code-validator")
    assert roles.roster_ids() == ("claude-code", "claude-code-validator")
    assert roles.builder_ids() == ("claude-code",)
    assert roles.checker_ids() == ("claude-code-validator",)   # the restored checker


def test_workshop_roles_reorders_to_makers_then_checker(monkeypatch):
    """Order is enforced, not taken from the variable: naming the checker first must
    not put it ahead of the builders it checks."""
    monkeypatch.setenv("WORKSHOP_ROLES", "kiro,claude-code")
    assert roles.roster_ids() == ("claude-code", "kiro")


def test_an_unknown_role_in_the_override_fails_loud(monkeypatch):
    """A typo must not silently shrink the team: a smaller roster still routes and
    would quietly drop a role's work."""
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,typo-here")
    with pytest.raises(roles.UnknownRole) as exc:
        roles.roster()
    assert "typo-here" in str(exc.value)


def test_get_raises_for_an_unregistered_role():
    with pytest.raises(roles.UnknownRole):
        roles.get("nope")


def test_by_capability_reads_the_served_roster(monkeypatch):
    """A capability nobody serves returns empty rather than a nearest match, so the
    caller reports a real routing failure instead of substituting an agent."""
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,kiro")
    assert roles.by_capability("backend") == ("claude-code",)
    assert roles.by_capability("frontend") == ()


def test_wirable_ids_lead_with_the_orchestrator():
    """The orchestrator is wirable but is NOT a dispatch target, so it appears in the
    wiring surface and never in the roster."""
    wirable = roles.wirable_ids()
    assert wirable[0] == roles.ORCHESTRATOR
    assert roles.ORCHESTRATOR not in roles.roster_ids()
    assert set(roles.roster_ids()) < set(wirable)


def test_command_fills_the_template_and_quotes_the_workdir():
    """Role.command builds the real headless invocation; the model falls back to the
    role's own default, and paths are shell-quoted (a workspace path is untrusted)."""
    r = roles.get("opencode")
    cmd = r.command("P", "", "/mnt/s3 files/run 1")
    assert r.default_model in cmd
    assert "'/mnt/s3 files/run 1'" in cmd
    assert '"$P"' in cmd


def test_command_honors_an_explicit_model():
    r = roles.get("claude-code")
    assert "my.model.id" in r.command("P", "my.model.id", "/w")


def test_public_roster_leaks_no_credentials_or_env():
    """The API projection is presentation only."""
    for row in roles.public_roster():
        assert set(row) == {"role", "label", "kind", "capability", "role_name",
                            "description", "steering_file", "model", "credential"}


def test_maker_capabilities_come_from_the_registry():
    """The router's choice list is DERIVED, never a literal.

    The model is handed these to pick from, so a roster swap or a smaller team must
    change what can be routed with no edit outside the registry.
    """
    caps = roles.maker_capabilities()
    assert caps, "a roster with no maker capability could never route a build"
    assert len(caps) == len(set(caps)), caps
    # Only MAKER capabilities: the checker rides every build structurally, and asking
    # a model whether to include it would make an invariant negotiable.
    checker_caps = {r.capability for r in roles.checkers()}
    assert not (set(caps) & checker_caps), (caps, checker_caps)
    assert set(caps) == {r.capability for r in roles.builders()}


def test_routing_fails_open_to_every_maker_when_the_model_is_unreachable(
        monkeypatch):
    """An unreachable router must route WIDER, never narrower.

    Routing too wide wastes a turn. Routing too narrow silently drops work the
    attendee asked for, which is the worse failure and the one that must be
    impossible -- so the fallback is every maker, and it says so in the reason.
    """
    import integration_plan

    def unreachable(*_args, **_kwargs):
        raise RuntimeError("no model credentials available")

    monkeypatch.setattr(integration_plan.llm, "invoke", unreachable)
    available = list(roles.maker_capabilities())
    chosen, reason = integration_plan.select_capabilities(
        "build anything at all", available)
    assert chosen == available, chosen
    assert "unavailable" in reason.lower(), reason


def test_a_routed_build_always_carries_a_maker_and_a_checker(monkeypatch):
    """The structural rules hold on the MODEL-routed path too.

    The model chooses which makers work. It is never asked whether a build needs a
    checker, and it cannot return an empty route: those are invariants, so they are
    enforced after the model answers rather than delegated to it.
    """
    import integration_plan
    import presets

    # The model picks exactly one maker capability.
    monkeypatch.setattr(
        integration_plan, "select_capabilities",
        lambda task, available, **kw: ([available[0]], "one kind of work"))
    route = presets.resolve(task="write me any small program")
    assert [a for a in route.agents if a in roles.builder_ids()]
    assert [a for a in route.agents if a in roles.checker_ids()]

    # A model that returns nothing usable must not produce an unbuildable route.
    monkeypatch.setattr(
        integration_plan, "select_capabilities",
        lambda task, available, **kw: ([], "returned nothing"))
    with pytest.raises(presets.RouteError):
        presets.resolve(task="write me any small program")


def test_an_empty_request_is_not_routed():
    """With no request text there is nothing to route: ASK, never invent a task."""
    import presets

    with pytest.raises(presets.RouteError) as excinfo:
        presets.resolve(task="   ")
    assert "EMPTY_TASK" in str(excinfo.value)
