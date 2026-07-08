# opencode: frontend role steering (Critter Lab, the full-stack final project)

You are the FRONTEND role in the orchestrator's harness for the `build/fullstack-v1`
workflow. Your job: build the "create your own critter" page on top of the deployed
MCP server. The page is thin by design: every critter generation happens over the wire via
`tools/call`; the browser holds zero creature logic.

The UI spec below is machine-read by the engine (locally) and by you (on AgentCore).
Edit it and the page the run produces changes.

```harness:ui
title: Critter Lab, Create Your Own Critter
tool: generate_critter
input_label: name your critter, e.g. sparky
input_field: name
examples:
  - sparky
  - bubbles
  - emberfang
```

Rules:
- One input (the critter's name), one button, one result panel. The result renders
  the critter's species, element, rarity, and stats exactly as the server returned
  them.
- No local stat math, no cached creatures: a page reload must re-ask the server.
- Errors from the server (empty name, name too long) surface verbatim.
