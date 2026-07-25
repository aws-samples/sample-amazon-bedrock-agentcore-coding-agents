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
    """A kept-but-hidden role (the Codex / Kiro restore paths) stays reachable in the
    registry and off the served roster, which is what makes it a restore path rather
    than dead code. Codex in particular must NOT be served: the GPT models it needs
    are unavailable on a Workshop Studio account."""
    hidden = [r.id for r in roles.REGISTRY if r.hidden]
    assert hidden, "the restore paths should stay registered, not deleted"
    served = roles.roster_ids()
    for role_id in hidden:
        assert role_id in roles.BY_ID
        assert role_id not in served
    assert "codex" in hidden, "Codex is disabled at events (no GPT entitlement)"


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
    one env var and no code change. This is also how a hidden role is restored."""
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,kiro")
    assert roles.roster_ids() == ("claude-code", "kiro")
    assert roles.builder_ids() == ("claude-code",)
    assert roles.checker_ids() == ("kiro",)          # the restored checker


def test_workshop_roles_reorders_to_makers_then_checker(monkeypatch):
    """Order is enforced, not taken from the variable: naming the checker first must
    not put it ahead of the builders it checks."""
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code-validator,claude-code")
    assert roles.roster_ids() == ("claude-code", "claude-code-validator")


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
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,claude-code-validator")
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
