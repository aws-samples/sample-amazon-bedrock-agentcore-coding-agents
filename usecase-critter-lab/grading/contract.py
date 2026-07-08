"""The deterministic grading contract, the single source of truth for "did the
agent succeed?" for the *Critter Lab* full-stack use case.

There is NO LLM judge here, and no contest; this is the autonomous orchestrator's
**acceptance gate**, the deterministic "definition of done" (AGENTS.md §4). The composed
result either satisfies these checks or it does not; if it does, the orchestrator opens a PR.

The contract is defined against an abstract ``MCPClient`` protocol so the SAME tests
run two ways:

  * **Local / pre-deploy**: an in-process adapter over ``critter_lab`` (so the
    grader is provably green before any AgentCore deployment exists).
  * **Deployed**: an adapter over the agent's remote MCP endpoint on AgentCore
    Runtime/Gateway (``tools/list`` + ``tools/call`` over the wire).

Every expected value below is HARDCODED from the reference ``critter_lab`` output (the
module is 100% deterministic, derived from ``sha256(name)``, so a correct port matches
exactly). The orchestrator runs these as pytest in its finalization phase against the
composed result; green → open the PR autonomously, red → bounded retry then a human.
"""

from __future__ import annotations

from typing import Any, Protocol


# The five tools every valid submission must expose. This is the acceptance set.
REQUIRED_TOOLS: set[str] = {
    "generate_critter",
    "element_matchup",
    "battle_score",
    "build_team",
    "critter_card",
}


class MCPClient(Protocol):
    """Minimal MCP surface the grader needs. Adapters implement this."""

    def list_tools(self) -> list[dict[str, Any]]:
        """Return tool specs, MCP ``tools/list`` shape ({name, description, inputSchema})."""
        ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool, MCP ``tools/call`` shape; return the structured result dict."""
        ...


# --- Fixture cases: (tool, arguments, key, expected). ---
# Each expected value is the ACTUAL output of the reference critter_lab (verified by
# running it). Because the module is sha256-deterministic, a correct port reproduces
# these byte-for-byte. Strings/ints/floats compare exactly here.
CASES: list[tuple[str, dict[str, Any], str, Any]] = [
    # generate_critter("sparky") -> species Sproutling / element leaf / rarity uncommon
    ("generate_critter", {"name": "sparky"}, "element", "leaf"),
    ("generate_critter", {"name": "sparky"}, "rarity", "uncommon"),
    ("generate_critter", {"name": "sparky"}, "species", "Sproutling"),
    # element_matchup: leaf is STRONG (2.0) vs water in the fixed ring chart.
    ("element_matchup", {"attacker": "leaf", "defender": "water"}, "multiplier", 2.0),
    # battle_score("sparky","bubbles") -> bubbles wins (deterministic).
    ("battle_score", {"name_a": "sparky", "name_b": "bubbles"}, "winner", "bubbles"),
    # build_team(["a","b"]) -> summed stats across the team.
    ("build_team", {"names": ["a", "b"]}, "team_power", 272),
]


def check_tool_discovery(client: MCPClient) -> tuple[bool, str]:
    """Check 1: all REQUIRED_TOOLS are discoverable via tools/list."""
    names = {t.get("name") for t in client.list_tools()}
    missing = REQUIRED_TOOLS - names
    if missing:
        return False, f"missing tools: {sorted(missing)}"
    return True, "all required tools discoverable"


def check_tool_correctness(client: MCPClient) -> tuple[bool, str]:
    """Check 2: each fixture case returns the correct deterministic result."""
    for name, args, key, expected in CASES:
        result = client.call_tool(name, args)
        if key not in result:
            return False, f"{name}{args}: result missing key {key!r} (got {sorted(result)})"
        actual = result[key]
        if actual != expected:
            return False, f"{name}{args}: {key}={actual!r}, expected {expected!r}"
    return True, f"{len(CASES)} fixture cases correct"


def check_input_validation(client: MCPClient) -> tuple[bool, str]:
    """Check 3: bad inputs are rejected, not silently mangled.

    Three guards must all raise: an empty/whitespace name, an unknown element, and an
    over-sized team (> 6). A correct port surfaces these as errors (in-process they are
    Python exceptions; over the wire they are JSON-RPC errors -> MCPRemoteError).
    """
    guards = [
        ("generate_critter", {"name": "   "}, "empty/whitespace name"),
        ("element_matchup", {"attacker": "leaf", "defender": "plasma"}, "unknown element"),
        ("build_team", {"names": ["a", "b", "c", "d", "e", "f", "g"]}, "team > 6"),
    ]
    for name, args, label in guards:
        try:
            client.call_tool(name, args)
        except Exception:
            continue  # correctly rejected
        return False, f"{label} was NOT rejected (should raise)"
    return True, "empty name, unknown element, and oversized team all rejected"


def check_card_renders(client: MCPClient) -> tuple[bool, str]:
    """Check 4: critter_card returns a 'card' string naming the critter and its element."""
    result = client.call_tool("critter_card", {"name": "sparky"})
    card = result.get("card")
    if not isinstance(card, str) or not card.strip():
        return False, f"critter_card: missing/blank 'card' string (got {card!r})"
    if "sparky" not in card:
        return False, "critter_card: 'card' does not contain the critter name"
    if result.get("element", "") not in card:
        return False, "critter_card: 'card' does not contain the critter element"
    return True, "card renders with name + element"


# Ordered list of (check_id, callable). The orchestrator iterates this.
CHECKS = [
    ("tool_discovery", check_tool_discovery),
    ("tool_correctness", check_tool_correctness),
    ("input_validation", check_input_validation),
    ("card_renders", check_card_renders),
]


def grade(client: MCPClient) -> dict[str, Any]:
    """Run every check against a client. Returns a structured verdict.

    ``passed`` is True only if ALL checks pass. This is the boolean the
    orchestrator's finalization phase uses to decide "done"; green opens the PR.
    """
    results = []
    for check_id, fn in CHECKS:
        try:
            ok, detail = fn(client)
        except Exception as exc:  # a crashing check is a failed check, never a crash
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"check": check_id, "passed": ok, "detail": detail})
    return {"passed": all(r["passed"] for r in results), "checks": results}
