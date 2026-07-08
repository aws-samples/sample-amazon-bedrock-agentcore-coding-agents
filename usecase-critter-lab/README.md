# src/usecase-critter-lab/: the *fun* full-stack use case ("create your own critter")

A second, playful sample module for the workshop's full-stack final goal. It follows the
EXACT same structural pattern as `../usecase-sample-to-mcp/` (pure functions + a
`TOOL_SPECS` / `list_tools()` / `dispatch()` MCP bridge + a deterministic `grading/`
acceptance gate) so the same orchestrator / builder / grader machinery drives it.

## The task
Take an existing Python module (`critter_lab.py`, a deterministic creature generator +
battle/team toolkit) and **convert it into a deployed remote MCP server with tests + a
"create your own critter" chatbot UI** so end users can generate a critter from their own
name, see its stats and trading card, and run battles in natural language.

Why this one: it demos well (everyone makes a critter from their name), it justifies a
full-stack build (backend tools + a visual UI), and it stays role-differentiated (backend
/ validator / frontend are different jobs). No game IP: these are generic "critters", not
any branded monster franchise.

## Why deterministic
Every output is derived from `sha256(name.lower().strip())`: **no `random`, no clock, no
date.** The same name ALWAYS yields the same critter. "Create your own critter" *feels*
generative, but it is a pure function of the input string, so the grader can assert exact
expected values the same way the pricing use case asserts exact dollar figures.

## The 5 tools
1. `generate_critter(name)`: species archetype, element, stats (hp/attack/defense/speed),
   a 3-color palette, and rarity, all derived from the name hash.
2. `element_matchup(attacker, defender)`: fixed rock-paper-scissors type chart over the
   8 elements; multiplier ∈ {0.5, 1.0, 2.0}.
3. `battle_score(name_a, name_b)`: scores a head-to-head and names the winner
   (deterministic tie-break: lexicographically smaller name).
4. `build_team(names)`: up to 6 critters; returns the team, `team_power` (summed stats),
   and `element_coverage`.
5. `critter_card(name)`: `generate_critter` plus a rendered ASCII trading-card string.

## grading/: the deterministic pytest acceptance gate
The contract is written against an `MCPClient` protocol so the IDENTICAL tests run two
ways (in-process pre-deploy, and over the wire against the deployed endpoint via
`MCP_ENDPOINT_URL`). Checks: `tool_discovery`, `tool_correctness` (hardcoded fixtures),
`input_validation` (empty name / unknown element / oversized team all rejected), and
`card_renders`. `adapters.py` ships both the `InProcessClient` and the JSON-RPC
`RemoteMCPClient` (SigV4-signed when `MCP_SIGV4=1`).

## Run it (no AWS, no pip install)
```bash
python3 src/usecase-critter-lab/critter_lab.py            # smoke: prints a critter, a battle, a team, a card
python3 -m pytest src/usecase-critter-lab/grading/ -q     # 14 passed
```
