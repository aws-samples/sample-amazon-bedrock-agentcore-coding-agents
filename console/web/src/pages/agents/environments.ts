// The Module 1 coding-agent roles, the single source of truth shared by the
// Agents sidebar sub-nav (App shell) and the Agents page itself. Each is a
// config workspace where you wire that agent's steering files, then deploy it.
// The `id` is the URL segment (/agents/<id>) and matches the `agentId` the
// Workspace + /api/dev/agents backend use. Development is separate: it is the
// main build/deploy workspace, a top-level sidebar item with its own page.
//
// A role can host more than one runtime INSTANCE under one sidebar entry (e.g.
// Claude Code runs BOTH the backend builder and the acceptance-gate validator).
// Each instance keeps its own agentId (the backend role id the /api/dev + wiring
// endpoints use), so the two stay distinct runtimes; the page shows them under a
// single "Claude Code" entry and switches between them with a dropdown, labelled
// by role + ARN. This is why the sidebar shows one Claude Code, not two identical
// rows nobody could tell apart.
export interface AgentInstance {
  /** Backend role id (the /api/dev + runtime-wiring key). Distinct per instance. */
  id: string;
  /** Human label for this instance inside the role (e.g. "Backend builder"). */
  label: string;
  /** One-line description of what this instance does. */
  blurb: string;
}

export interface AgentRole {
  /** The sidebar/URL id. For a single-instance role this equals its one instance id. */
  id: string;
  label: string;
  blurb: string;
  /** The runtime instances hosted under this one sidebar entry (>=1). */
  instances: AgentInstance[];
}

export const AGENT_ROLES: AgentRole[] = [
  {
    id: 'claude-code',
    label: 'Claude Code',
    blurb: 'Claude Code runs two roles on Bedrock: the backend builder you deploy, and the acceptance-gate validator.',
    instances: [
      { id: 'claude-code',           label: 'Backend builder',   blurb: 'Wire .claude/ skills + CLAUDE.md, then deploy the backend builder.' },
      { id: 'claude-code-validator', label: 'Validator (gate)',  blurb: 'A second Claude Code, runtime pre-provisioned on Bedrock; stage its acceptance-contract CLAUDE.md.' },
    ],
  },
  {
    id: 'opencode',
    label: 'opencode',
    blurb: 'Frontend role: runtime pre-provisioned on Bedrock; stage AGENTS.md and wire the runtime ARN.',
    instances: [
      { id: 'opencode',              label: 'Frontend builder',  blurb: 'Frontend role: runtime pre-provisioned on Bedrock; stage AGENTS.md and wire the runtime ARN.' },
    ],
  },
];

export const DEFAULT_AGENT_ROLE = 'claude-code';

export function agentRole(id: string | undefined): AgentRole {
  return AGENT_ROLES.find((e) => e.id === id) ?? AGENT_ROLES[0]!;
}

// Resolve an instance id (e.g. 'claude-code-validator') to its human label, for
// display anywhere a specific runtime instance is named (Settings, Fleet).
export function agentInstanceLabel(instanceId: string | undefined): string {
  for (const role of AGENT_ROLES) {
    const inst = role.instances.find((i) => i.id === instanceId);
    if (inst) return role.instances.length > 1 ? `${role.label} - ${inst.label}` : role.label;
  }
  return instanceId ?? '';
}

// The Development workspace's agentId for the backend (PTY, files, sessions).
export const DEV_AGENT_ID = 'dev';
