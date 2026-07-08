"""critter_lab: the sample Python module the workshop converts into a remote MCP server.

The second use case for the workshop's full-stack final project: "create your own
critter". Like ``cost_analyzer`` it is plain, dependency-free Python (stdlib only),
the kind of small utility every team has, and it ships with the same MCP bridge
(``TOOL_SPECS`` + ``dispatch``) so the same orchestrator / builder / grader machinery
drives it. Stage 2's agents wrap these handlers in a FastMCP server on AgentCore
Runtime; the front-end agent builds a "critter creator" chatbot UI on top.

Why a creature generator? It demos well (everyone makes a critter from their own
name), it justifies a full-stack build (backend tools + a visual UI), and it is
deterministic:

* every output is derived from ``sha256(name.lower().strip())``, with no ``random`` module,
  no clock, no ``time``/``date``. The same name always yields the same critter.

That determinism lets the grader (``grading/``) assert exact expected values, the same
way the pricing use case asserts exact dollar figures. "Create your own critter" feels
generative, but it is a pure function of the input string.

There is no game IP here on purpose: these are generic "critters", not any branded
monster franchise.
"""

from __future__ import annotations

import hashlib
from typing import Any

# --- The fixed catalog the generator draws from (all derived from the name hash). ---

# Eight species archetypes. Index = first hash byte % 8.
ARCHETYPES: list[str] = [
    "Sproutling",
    "Emberkit",
    "Aquafin",
    "Boltpup",
    "Stonecub",
    "Galewing",
    "Frostfox",
    "Shadowmoth",
]

# Eight elements, positionally paired with the archetypes above (same index).
ELEMENTS: list[str] = [
    "leaf",
    "flame",
    "water",
    "volt",
    "stone",
    "gale",
    "frost",
    "shadow",
]

# Rarity tiers, chosen from a hash byte by threshold (see generate_critter).
RARITIES: list[str] = ["common", "uncommon", "rare", "legendary"]

MAX_NAME_LEN = 40
MAX_TEAM_SIZE = 6

# Element type chart: ELEMENT_CHART[attacker][defender] -> damage multiplier.
# Rock-paper-scissors style: each element is strong (2.0) vs the next two in a ring,
# weak (0.5) vs the two before it, and neutral (1.0) otherwise. Fixed so matchups are
# deterministic and easy to grade.
#
# Ring order: leaf -> water -> flame -> volt -> stone -> gale -> frost -> shadow -> (leaf)
# Each element beats the two elements that follow it in the ring.
_RING: list[str] = ["leaf", "water", "flame", "volt", "stone", "gale", "frost", "shadow"]


def _build_element_chart() -> dict[str, dict[str, float]]:
    chart: dict[str, dict[str, float]] = {}
    n = len(_RING)
    for i, atk in enumerate(_RING):
        row: dict[str, float] = {}
        for j, dfn in enumerate(_RING):
            if i == j:
                row[dfn] = 1.0  # same element: neutral
                continue
            # forward distance from attacker to defender around the ring
            fwd = (j - i) % n
            if fwd in (1, 2):
                row[dfn] = 2.0  # attacker is strong vs the next two
            elif fwd in (n - 1, n - 2):
                row[dfn] = 0.5  # attacker is weak vs the previous two
            else:
                row[dfn] = 1.0  # neutral otherwise
        chart[atk] = row
    return chart


# The fully materialized chart (inspect or render it in the UI).
ELEMENT_CHART: dict[str, dict[str, float]] = _build_element_chart()


class UnknownElementError(ValueError):
    """Raised when an element outside the fixed 8-element set is supplied."""


class UnknownToolError(ValueError):
    """Raised when ``dispatch`` is called with a tool name not in the registry."""


def _digest(name: str) -> bytes:
    """The single deterministic source of entropy: sha256 of the normalized name.

    Normalization (lowercase + strip) is intentional so "Sparky", " sparky " and
    "sparky" all produce the SAME critter, but the echoed ``name`` preserves the
    caller's original spelling.
    """
    return hashlib.sha256(name.lower().strip().encode("utf-8")).digest()


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty, non-whitespace string")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name must be <= {MAX_NAME_LEN} characters")
    return name


def _stat(byte: int, lo: int, hi: int) -> int:
    """Map a hash byte (0-255) into the inclusive range [lo, hi] deterministically."""
    span = hi - lo + 1
    return lo + (byte % span)


def generate_critter(name: str) -> dict[str, Any]:
    """Generate a deterministic critter from ``name``.

    EVERYTHING is derived from ``sha256(name.lower().strip())`` so the same name always
    yields the same critter (no randomness, no clock). The original ``name`` is echoed
    back unchanged. Raises ``ValueError`` on empty/whitespace names or names > 40 chars.

    Stat derivation (documented so a port can reproduce it byte-for-byte):
      * species/element : digest[0] % 8 indexes ARCHETYPES / ELEMENTS (paired)
      * hp              : 20 + (digest[1] % 81)   -> 20..100
      * attack          : 5  + (digest[2] % 46)   -> 5..50
      * defense         : 5  + (digest[3] % 46)   -> 5..50
      * speed           : 5  + (digest[4] % 46)   -> 5..50
      * palette         : 3 hex colors from digest[5:8], digest[8:11], digest[11:14]
      * rarity          : digest[14] thresholds -> legendary>=240, rare>=208,
                          uncommon>=144, else common
    """
    name = _validate_name(name)
    d = _digest(name)

    idx = d[0] % len(ARCHETYPES)
    species = ARCHETYPES[idx]
    element = ELEMENTS[idx]

    stats = {
        "hp": _stat(d[1], 20, 100),
        "attack": _stat(d[2], 5, 50),
        "defense": _stat(d[3], 5, 50),
        "speed": _stat(d[4], 5, 50),
    }

    palette = [
        "#{:02x}{:02x}{:02x}".format(d[5], d[6], d[7]),
        "#{:02x}{:02x}{:02x}".format(d[8], d[9], d[10]),
        "#{:02x}{:02x}{:02x}".format(d[11], d[12], d[13]),
    ]

    r = d[14]
    if r >= 240:
        rarity = "legendary"
    elif r >= 208:
        rarity = "rare"
    elif r >= 144:
        rarity = "uncommon"
    else:
        rarity = "common"

    return {
        "name": name,
        "species": species,
        "element": element,
        "stats": stats,
        "palette": palette,
        "rarity": rarity,
    }


def element_matchup(attacker: str, defender: str) -> dict[str, Any]:
    """Look up the fixed type-chart multiplier for ``attacker`` attacking ``defender``.

    ``multiplier`` is one of {0.5, 1.0, 2.0} (see ELEMENT_CHART). Raises
    ``UnknownElementError`` if either element is outside the 8-element set.
    """
    if attacker not in ELEMENT_CHART:
        raise UnknownElementError(f"Unknown element: {attacker!r}")
    if defender not in ELEMENT_CHART:
        raise UnknownElementError(f"Unknown element: {defender!r}")
    return {
        "attacker": attacker,
        "defender": defender,
        "multiplier": ELEMENT_CHART[attacker][defender],
    }


def battle_score(name_a: str, name_b: str) -> dict[str, Any]:
    """Score a head-to-head between two critters and name the winner.

    For each side: ``score = attack * matchup_multiplier + speed/2 - opp_defense/2``
    (rounded to 2 decimals), where the multiplier comes from the attacker's element
    against the opponent's element. The ``winner`` is the higher score; ties break to
    the lexicographically smaller name (deterministic).
    """
    a = generate_critter(name_a)
    b = generate_critter(name_b)

    mult_a = ELEMENT_CHART[a["element"]][b["element"]]
    mult_b = ELEMENT_CHART[b["element"]][a["element"]]

    score_a = round(
        a["stats"]["attack"] * mult_a + a["stats"]["speed"] / 2 - b["stats"]["defense"] / 2,
        2,
    )
    score_b = round(
        b["stats"]["attack"] * mult_b + b["stats"]["speed"] / 2 - a["stats"]["defense"] / 2,
        2,
    )

    if score_a > score_b:
        winner = a["name"]
    elif score_b > score_a:
        winner = b["name"]
    else:
        winner = min(a["name"], b["name"])  # deterministic tie-break

    return {
        "name_a": a["name"],
        "name_b": b["name"],
        "score_a": score_a,
        "score_b": score_b,
        "winner": winner,
    }


def build_team(names: list[str]) -> dict[str, Any]:
    """Generate a critter per name (max 6) and summarize the team.

    Returns the ``team`` list plus ``team_power`` (int sum of every stat across the
    team) and ``element_coverage`` (sorted unique elements). Raises ``ValueError`` on
    an empty list or more than 6 names.
    """
    if not isinstance(names, list) or len(names) == 0:
        raise ValueError("names must be a non-empty list")
    if len(names) > MAX_TEAM_SIZE:
        raise ValueError(f"team may have at most {MAX_TEAM_SIZE} critters")

    team = [generate_critter(n) for n in names]
    team_power = sum(
        c["stats"]["hp"] + c["stats"]["attack"] + c["stats"]["defense"] + c["stats"]["speed"]
        for c in team
    )
    element_coverage = sorted({c["element"] for c in team})
    return {
        "team": team,
        "team_power": team_power,
        "element_coverage": element_coverage,
    }


def critter_card(name: str) -> dict[str, Any]:
    """Return ``generate_critter`` plus a rendered ASCII "card" under the ``card`` key.

    The ``card`` string is a small box-drawing-character panel that includes the
    critter's name, species, element, rarity, and stats; the front-end agent can
    render it verbatim or use the structured fields. Determinism is preserved (the card
    is a pure function of the critter).
    """
    critter = generate_critter(name)
    s = critter["stats"]
    width = 30
    lines = [
        "+" + "-" * width + "+",
        "| {:<{w}}|".format(critter["name"], w=width - 1),
        "+" + "-" * width + "+",
        "| species : {:<{w}}|".format(critter["species"], w=width - 11),
        "| element : {:<{w}}|".format(critter["element"], w=width - 11),
        "| rarity  : {:<{w}}|".format(critter["rarity"], w=width - 11),
        "+" + "-" * width + "+",
        "| {:<{w}}|".format(
            "HP {hp:>3}  ATK {attack:>2}  DEF {defense:>2}  SPD {speed:>2}".format(**s),
            w=width - 1,
        ),
        "+" + "-" * width + "+",
    ]
    critter["card"] = "\n".join(lines)
    return critter


# ---------------------------------------------------------------------------
# Tool registry: the MCP bridge (same shape as cost_analyzer's).
#
# A tool spec is shaped like an MCP tool: {name, description, inputSchema}.
# ``dispatch(name, arguments)`` is the ``tools/call`` equivalent; ``list_tools()``
# is ``tools/list``. Stage 2's agents implement against this; the grader checks it.
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "generate_critter",
        "description": "Deterministically generate a critter (species, element, stats, palette, rarity) from a name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "element_matchup",
        "description": "Look up the fixed type-chart damage multiplier for one element attacking another.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "attacker": {"type": "string"},
                "defender": {"type": "string"},
            },
            "required": ["attacker", "defender"],
        },
    },
    {
        "name": "battle_score",
        "description": "Score a head-to-head between two named critters and return the winner.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name_a": {"type": "string"},
                "name_b": {"type": "string"},
            },
            "required": ["name_a", "name_b"],
        },
    },
    {
        "name": "build_team",
        "description": "Build a team of up to 6 critters and summarize team power and element coverage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_TEAM_SIZE,
                },
            },
            "required": ["names"],
        },
    },
    {
        "name": "critter_card",
        "description": "Generate a critter plus a rendered ASCII trading-card string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
]

_HANDLERS = {
    "generate_critter": generate_critter,
    "element_matchup": element_matchup,
    "battle_score": battle_score,
    "build_team": build_team,
    "critter_card": critter_card,
}


def list_tools() -> list[dict[str, Any]]:
    """Return the tool specs (the ``tools/list`` equivalent)."""
    return TOOL_SPECS


def dispatch(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool by name with keyword arguments (the ``tools/call`` equivalent)."""
    if name not in _HANDLERS:
        raise UnknownToolError(f"Unknown tool: {name!r}")
    return _HANDLERS[name](**(arguments or {}))


if __name__ == "__main__":
    # Tiny manual smoke check: python critter_lab.py
    import json

    print(json.dumps(generate_critter("sparky"), indent=2))
    print(json.dumps(battle_score("sparky", "bubbles"), indent=2))
    print(json.dumps(build_team(["sparky", "bubbles"]), indent=2))
    print(critter_card("sparky")["card"])
