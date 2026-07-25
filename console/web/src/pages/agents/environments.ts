// The Module 1 coding-agent roles, shared by the Agents sidebar sub-nav (App
// shell) and the Agents page itself. Each is a config workspace where you wire
// that agent's steering files, then deploy it. The `id` is the URL segment
// (/agents/<id>) and matches the `agentId` the Workspace + /api/dev/agents
// backend use. Development is separate: it is the main build/deploy workspace, a
// top-level sidebar item with its own page.
//
// These are DERIVED FROM THE SERVED ROSTER (`GET /api/orchestrator/roster`, which
// reads `orchestrator/roles.py`, the one place the team is declared), not written
// out here. That matters because the roster is configurable: a deployment can swap
// a role or run a smaller team, and a hardcoded copy in the console would then
// offer an agent that cannot be dispatched (or hide one that can).
//
// A role can host more than one runtime INSTANCE under one sidebar entry, which is
// why the grouping below is by CLI label: two Claude Code roles (the backend
// builder and the acceptance-gate validator) are two distinct runtimes but one
// sidebar row, switched with a dropdown. Without the grouping the sidebar would
// show two identical "Claude Code" rows nobody could tell apart.
import { listRoster, type RosterRole } from '../../api';

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

/** Title-case a role_name from the registry ("backend-builder" -> "Backend builder"). */
function instanceLabel(role: RosterRole): string {
  const words = role.role_name.replace(/[-_]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Group the flat served roster into sidebar entries, one per CLI label, preserving
 * the roster's own order (builders first, checker last).
 */
export function groupRoster(roster: RosterRole[]): AgentRole[] {
  const byLabel = new Map<string, RosterRole[]>();
  for (const r of roster) {
    const bucket = byLabel.get(r.label);
    if (bucket) bucket.push(r);
    else byLabel.set(r.label, [r]);
  }
  return [...byLabel.entries()].map(([label, members]) => ({
    // The sidebar id is the FIRST member's role id, so /agents/<id> always
    // addresses a real backend role rather than a synthesized group name.
    id: members[0]!.role,
    label,
    blurb: members.length > 1
      ? `${label} runs ${members.length} roles on Bedrock: ${
        members.map((m) => instanceLabel(m).toLowerCase()).join(', ')}.`
      : members[0]!.description,
    instances: members.map((m) => ({
      id: m.role,
      label: instanceLabel(m),
      blurb: m.description,
    })),
  }));
}

// The live roster, cached after the first fetch. Empty until it loads: the console
// shows no agent rows rather than rows this file invented, because a row that does
// not correspond to a served role is one the attendee cannot deploy or wire.
let cached: AgentRole[] = [];
const waiting = new Set<(roles: AgentRole[]) => void>();
let inflight: Promise<AgentRole[]> | null = null;

export function loadAgentRoles(): Promise<AgentRole[]> {
  if (cached.length) return Promise.resolve(cached);
  inflight ??= listRoster()
    .then((roster) => {
      cached = groupRoster(roster);
      waiting.forEach((fn) => fn(cached));
      waiting.clear();
      return cached;
    })
    .catch(() => {
      inflight = null;   // let a later mount retry a transient failure
      return [];
    });
  return inflight;
}

/** The roster as currently known (possibly empty before the first fetch lands). */
export function agentRoles(): AgentRole[] {
  return cached;
}

/** Subscribe to the roster; fires immediately when it is already loaded. */
export function onAgentRoles(fn: (roles: AgentRole[]) => void): () => void {
  if (cached.length) fn(cached);
  else {
    waiting.add(fn);
    void loadAgentRoles();
  }
  return () => waiting.delete(fn);
}

/** The default sidebar/URL segment: the first served role, or '' before load. */
export function defaultAgentRole(): string {
  return cached[0]?.id ?? '';
}

/** The role a URL segment addresses, or undefined until the roster loads. */
export function agentRole(id: string | undefined): AgentRole | undefined {
  return cached.find((e) => e.id === id) ?? cached[0];
}

// Resolve an instance id (e.g. 'claude-code-validator') to its human label, for
// display anywhere a specific runtime instance is named (Settings, Fleet). Falls
// back to the raw id, which is meaningful on its own.
export function agentInstanceLabel(instanceId: string | undefined): string {
  for (const role of cached) {
    const inst = role.instances.find((i) => i.id === instanceId);
    if (inst) return role.instances.length > 1 ? `${role.label} - ${inst.label}` : role.label;
  }
  return instanceId ?? '';
}

// The Development workspace's agentId for the backend (PTY, files, sessions).
export const DEV_AGENT_ID = 'dev';
