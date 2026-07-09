import { AgentBackendIcon } from '@foxl/code/components/AgentBackendIcon';

/**
 * Maps our Module 1 agent_id to the auto-detected backend icon. The icon set
 * (claudecode/kiro/codex/cursor/hermes/opencode) ships under /agents/; an
 * unknown id falls back to the Claude Code mark via resolveBackend.
 */
const ID_TO_BACKEND: Record<string, string> = {
  'claude-code': 'claudecode',
  // The validator is a second Claude Code, so it renders the Claude Code mark.
  'claude-code-validator': 'claudecode',
  kiro: 'kiro',
  codex: 'codex',
  cursor: 'cursor',
  hermes: 'hermes',
  opencode: 'opencode',
};

export function AgentIcon({ agentId, size = 16, showLabel = false }: {
  agentId: string; size?: number; showLabel?: boolean;
}) {
  return <AgentBackendIcon backend={ID_TO_BACKEND[agentId] ?? agentId} size={size} showLabel={showLabel} />;
}
