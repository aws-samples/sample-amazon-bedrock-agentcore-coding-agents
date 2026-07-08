import { cn } from '@foxl/ui';

/**
 * Shared motion primitives: the production-level loading + liveness
 * affordances used across the console (chat, run cards, role rows). Each
 * leans on a keyframe defined in styles.css that animates only
 * transform/opacity, and every one is neutralized under
 * `prefers-reduced-motion: reduce`.
 *
 * These are deliberately tiny and prop-light: a Claude/Codex-grade "the agent
 * is working" treatment is mostly restraint: one moving thing, soft easing,
 * the same rhythm everywhere.
 */

/**
 * The three-dot "Working" wave: a crest that travels left→right across three
 * dots. The canonical "the model is doing something" indicator. Pair it with a
 * label (e.g. <WorkingDots /> <Shimmer>Dispatching…</Shimmer>) or use it bare.
 */
export function WorkingDots({ className, size = 4 }: { className?: string; size?: number }) {
  const dot = 'inline-block rounded-full bg-current animate-working-dot';
  const style = { width: size, height: size };
  return (
    <span
      className={cn('inline-flex items-center gap-[3px] align-middle text-muted-foreground', className)}
      role="status"
      aria-label="Working"
    >
      <span className={dot} style={style} />
      <span className={dot} style={{ ...style, animationDelay: '0.15s' }} />
      <span className={dot} style={{ ...style, animationDelay: '0.3s' }} />
    </span>
  );
}

/**
 * A live status node: a small solid dot wrapped in a breathing halo. Reads as
 * "this thing is active right now". When `live` is false it renders a static
 * dot (color set by `tone`) with no animation, so the same component covers the
 * running → settled transition without a layout shift.
 */
export function PulseDot({
  live,
  tone = 'info',
  size = 8,
  className,
}: {
  live?: boolean;
  tone?: 'info' | 'success' | 'danger' | 'muted';
  size?: number;
  className?: string;
}) {
  // Brand success IS blue (#0070f3), so both "info" (active/running) and
  // "success" (ready/done) map to the same success token; there is no green.
  const color =
    tone === 'success' ? 'bg-success'
    : tone === 'danger' ? 'bg-destructive'
    : tone === 'muted' ? 'bg-muted-foreground/50'
    : 'bg-success';
  return (
    <span className={cn('relative inline-flex shrink-0', className)} style={{ width: size, height: size }}>
      {live && (
        <span
          className={cn('absolute inset-0 rounded-full animate-pulse-ring', color)}
          aria-hidden
        />
      )}
      <span className={cn('relative inline-block rounded-full', color)} style={{ width: size, height: size }} />
    </span>
  );
}
