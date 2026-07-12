/**
 * Visual identity for the CLI backend that ran (or will run) a coding
 * task. The icons are IMPORTED as module assets (not referenced by a
 * /agents/ public URL): each file is a few hundred bytes to ~1.7 KB, all
 * under Vite's 4 KB threshold, so the bundler inlines them as base64 data
 * URIs straight into the JS. That means zero extra HTTP round-trips: the
 * mark paints with the bundle instead of waiting on a separate request that,
 * in the dev reverse-proxy, was routed through the backend and felt slow.
 *
 * Tasks created before INC-017 have agent_backend === undefined; we treat
 * those as 'claudecode' since that's what they actually ran on.
 */
import type { AgentBackend } from '@foxl/types';
import { cn } from '@foxl/ui';
// `?inline` forces Vite to return a base64 data URI in BOTH dev and prod. A
// bare asset import only inlines at BUILD time; in dev it serves a /src/assets
// URL, and here that fetch is reverse-proxied through the backend, which is
// exactly why the Fleet logos loaded slowly. `?inline` bakes the bytes into the
// module so the mark paints with zero round-trips in every mode.
import claudecodeIcon from '../../../assets/agents/claudecode-color.png?inline';
import kiroIcon from '../../../assets/agents/kiro-color.png?inline';
import codexIcon from '../../../assets/agents/codex-color.svg?inline';
import cursorIcon from '../../../assets/agents/cursor-color.svg?inline';
import hermesIcon from '../../../assets/agents/hermes-color.svg?inline';
import opencodeIcon from '../../../assets/agents/opencode-color.svg?inline';

interface AgentMeta {
  label: string;
  icon: string;
  description: string;
}

export const AGENT_BACKENDS: Record<AgentBackend, AgentMeta> = {
  claudecode: {
    label: 'Claude Code',
    icon: claudecodeIcon,
    description: 'Anthropic Claude Code CLI in a node-pty PTY.',
  },
  kiro: {
    label: 'Kiro CLI',
    icon: kiroIcon,
    description: 'Kiro CLI (kiro.dev) - spec-first agent steering.',
  },
  codex: {
    label: 'Codex',
    icon: codexIcon,
    description: 'OpenAI Codex CLI on Amazon Bedrock (GPT-5.5/5.4).',
  },
  cursor: {
    label: 'Cursor',
    icon: cursorIcon,
    description: 'Cursor Agent CLI (cursor.com) - bring your own Cursor key.',
  },
  hermes: {
    label: 'Hermes',
    icon: hermesIcon,
    description: 'Nous Research Hermes CLI on Amazon Bedrock.',
  },
  opencode: {
    label: 'OpenCode',
    icon: opencodeIcon,
    description: 'OpenCode CLI (opencode.ai) on Amazon Bedrock (claude-sonnet-4-6).',
  },
};

const KNOWN: AgentBackend[] = ['claudecode', 'kiro', 'codex', 'cursor', 'hermes', 'opencode'];

export function resolveBackend(b: AgentBackend | string | null | undefined): AgentBackend {
  return (typeof b === 'string' && (KNOWN as string[]).includes(b)) ? (b as AgentBackend) : 'claudecode';
}

interface AgentBackendIconProps {
  backend: AgentBackend | string | null | undefined;
  /** Pixel size for both width + height. */
  size?: number;
  className?: string;
  showLabel?: boolean;
}

export function AgentBackendIcon({ backend, size = 16, className, showLabel = false }: AgentBackendIconProps) {
  const resolved = resolveBackend(backend);
  const meta = AGENT_BACKENDS[resolved];
  return (
    <span className={className ? `inline-flex items-center gap-1.5 ${className}` : 'inline-flex items-center gap-1.5'}>
      <img
        src={meta.icon}
        alt={meta.label}
        title={meta.label}
        width={size}
        height={size}
        className="shrink-0"
        style={{ width: size, height: size }}
      />
      {showLabel && <span className="text-[11px] text-muted-foreground">{meta.label}</span>}
    </span>
  );
}

interface AgentBackendAvatarProps {
  backend: AgentBackend | string | null | undefined;
  /** Lifecycle status from the task. Drives the dot color. */
  status?: string;
  /** Pixel size for the agent mark. */
  size?: number;
  className?: string;
}

/**
 * Single combined badge for task rows: the backend mascot is the lead
 * identity, and a tiny status dot overlays the bottom-right corner so
 * users can read both signals in one glance without two competing
 * icons.
 */
export function AgentBackendAvatar({
  backend,
  status,
  size = 18,
  className,
}: AgentBackendAvatarProps) {
  const resolved = resolveBackend(backend);
  const meta = AGENT_BACKENDS[resolved];
  const dotColor =
    status === 'running' || status === 'queued' ? 'bg-blue-500'
    : status === 'review' ? 'bg-amber-500'
    : status === 'completed' || status === 'merged' ? 'bg-emerald-500'
    : status === 'failed' ? 'bg-destructive'
    : status === 'cancelled' ? 'bg-muted-foreground/40'
    : 'bg-muted-foreground/40';
  const dotSize = Math.max(6, Math.round(size * 0.4));
  return (
    <span
      className={cn('relative inline-flex shrink-0', className)}
      style={{ width: size, height: size }}
    >
      <img
        src={meta.icon}
        alt={meta.label}
        title={meta.label}
        width={size}
        height={size}
        className="rounded-[3px]"
        style={{ width: size, height: size }}
      />
      {status && (
        <span
          aria-hidden
          className={cn(
            'absolute -bottom-0.5 -right-0.5 rounded-full',
            dotColor,
            (status === 'running' || status === 'queued') && 'animate-pulse',
          )}
          style={{ width: dotSize, height: dotSize }}
        />
      )}
    </span>
  );
}
