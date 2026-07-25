---
name: configure-claude-code-backend
description: >-
  Configure Claude Code as the BACKEND builder: implement the service the request asks for
  and expose it as a remote MCP server behind the AgentCore Gateway. Use when the user says
  "set up Claude Code", "configure the backend agent", or "build the backend MCP server".
  Not for the validator or frontend roles.
---

# Configure Claude Code: Backend MCP Server Builder

On-demand capability for the backend role. The always-on steering lives in the sibling
`CLAUDE.md`. The full step-by-step (deploy flow, Gateway target, acceptance gate) is the
workshop skill `harness-skills/skills/configure-claude-code-backend/SKILL.md`; this copy
ships inside the harness so the agent carries its own on-demand capability in the format
the content describes (`skills/<name>/SKILL.md` with `name` + `description` frontmatter).

Done = the service is running, `tools/list` returns the tools the request called for, and
the validator's authored acceptance check exits 0.
