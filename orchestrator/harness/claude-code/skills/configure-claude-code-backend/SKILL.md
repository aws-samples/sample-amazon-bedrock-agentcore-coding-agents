---
name: configure-claude-code-backend
description: >-
  Configure Claude Code as the BACKEND builder: wrap the cost_analyzer Skill into a remote
  FastMCP server behind the AgentCore Gateway. Use when the user says "set up Claude Code",
  "configure the backend agent", or "build the backend MCP server". Not for Kiro (validator)
  or Codex (frontend).
---

# Configure Claude Code: Backend MCP Server Builder

On-demand capability for the backend role. The always-on steering lives in the sibling
`CLAUDE.md`, including the `harness:build` spec the orchestrator reads to compose the server.
The full step-by-step (deploy flow, Gateway target, acceptance gate) is the workshop skill
`harness-skills/skills/configure-claude-code-backend/SKILL.md`; this copy ships inside the
harness so the agent carries its own on-demand capability in the format the content describes
(`skills/<name>/SKILL.md` with `name` + `description` frontmatter).

Done = `tools/list` returns the five `cost_analyzer.TOOL_SPECS` tools and `tools/call` returns
the contract values (for example `m5.large` times two equals `140.16`).
