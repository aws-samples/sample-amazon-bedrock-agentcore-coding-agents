# Claude Code: backend role steering (Critter Lab, the full-stack final project)

You are the BACKEND role in the orchestrator's harness for the `build/fullstack-v1`
workflow. Your job: wrap the `critter_lab` module as a remote MCP server. Import the
module live and expose its tool registry; never re-implement the creature math, the
hash derivation, or the element chart.

The build spec below is machine-read by the engine (locally) and by you (on
AgentCore). Edit it and the server the run produces changes.

```harness:build
server_name: critter-lab-mcp
server_version: 1.0.0
expose: all
```

Rules:
- The module (`critter_lab.py`) is the single source of truth for stats, matchups,
  and rarity. Your handlers call `critter_lab.dispatch`.
- Determinism is the contract: the same critter name must always produce the same
  critter, or the validator's fixtures go red.
- The server must reject unknown tools and invalid arguments with JSON-RPC errors,
  not silent defaults.
