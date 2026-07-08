// The Module 1 coding-agent roles, the single source of truth shared by the
// Agents sidebar sub-nav (App shell) and the Agents page itself. Each is a
// config workspace where you wire that agent's steering files, then deploy it.
// The `id` is the URL segment (/agents/<id>) and matches the `agentId` the
// Workspace + /api/dev/agents backend use. Development is separate: it is the
// main build/deploy workspace, a top-level sidebar item with its own page.
export interface AgentRole {
  id: string;
  label: string;
  blurb: string;
}

export const AGENT_ROLES: AgentRole[] = [
  { id: 'claude-code', label: 'Claude Code', blurb: 'Backend role: wire .claude/ skills + CLAUDE.md, then deploy the backend builder.' },
  { id: 'kiro',        label: 'Kiro',        blurb: 'Validator role: runtime pre-provisioned; stage .kiro/steering and add your Kiro API key.' },
  { id: 'opencode',    label: 'opencode',    blurb: 'Frontend role: runtime pre-provisioned on Bedrock; stage AGENTS.md and wire the runtime ARN.' },
];

export const DEFAULT_AGENT_ROLE = 'claude-code';

export function agentRole(id: string | undefined): AgentRole {
  return AGENT_ROLES.find((e) => e.id === id) ?? AGENT_ROLES[0]!;
}

// The Development workspace's agentId for the backend (PTY, files, sessions).
export const DEV_AGENT_ID = 'dev';
