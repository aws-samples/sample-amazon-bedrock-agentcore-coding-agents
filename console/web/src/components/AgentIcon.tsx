import { AgentBackendIcon } from '@foxl/code/components/AgentBackendIcon';

/**
 * Maps a role id to the auto-detected backend icon. The icon set
 * (claudecode/kiro/codex/cursor/hermes/opencode) ships under /agents/; an unknown
 * id falls back to the Claude Code mark via resolveBackend.
 *
 * Resolved by PREFIX rather than by an entry per role, because several roles can
 * run the same CLI (the backend builder and the validator are both Claude Code) and
 * the roster is configurable: a role can be added, hidden, or swapped without an
 * edit here. A missing entry used to mean a role silently rendered the wrong mark.
 */
const BACKENDS = ['claude-code', 'opencode', 'kiro', 'codex', 'cursor', 'hermes'];

export function resolveIconBackend(agentId: string): string {
  const id = agentId.toLowerCase();
  // Longest match first, so a longer CLI name always wins over a shorter prefix.
  const hit = [...BACKENDS].sort((a, b) => b.length - a.length).find((b) => id.startsWith(b));
  // The icon set spells Claude Code without the hyphen.
  return hit ? hit.replace('claude-code', 'claudecode') : agentId;
}

export function AgentIcon({ agentId, size = 16, showLabel = false }: {
  agentId: string; size?: number; showLabel?: boolean;
}) {
  return <AgentBackendIcon backend={resolveIconBackend(agentId)} size={size} showLabel={showLabel} />;
}
