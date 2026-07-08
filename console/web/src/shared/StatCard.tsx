import { cn } from '@foxl/ui';

/**
 * A single headline metric: a small uppercase label, a large tabular value, and
 * an optional trend pill + hint underneath. `accent` lifts the border to the
 * primary tone so the lead KPI on a row reads first. Entry animation is the
 * shared `animate-enter-up`; pass a `delay` to stagger a row of cards.
 */
export function StatCard({
  label,
  value,
  hint,
  trend,
  accent,
  delay = 0,
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  trend?: { pct: number; label?: string };
  accent?: boolean;
  delay?: number;
  className?: string;
}) {
  const up = trend ? trend.pct >= 0 : null;
  return (
    <div
      className={cn(
        'animate-enter-up relative overflow-hidden rounded-lg border bg-card px-5 py-4 shadow-sm',
        accent ? 'border-primary/30' : 'border-border',
        className,
      )}
      style={{ animationDelay: `${delay}ms` }}
    >
      {/* Mono eyebrow label: the technical-platform voice on a metric. */}
      <div className="eyebrow">{label}</div>
      {/* Display value: weight 600, tabular, negative tracking. */}
      <div className="mt-2 text-[26px] font-semibold leading-none tracking-[-0.02em] tabular-nums text-foreground">{value}</div>
      {(trend || hint) && (
        <div className="mt-2 flex items-center gap-2 text-[11px]">
          {trend && (
            <span
              className={cn(
                'inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 font-medium',
                up ? 'bg-success/10 text-success' : 'bg-destructive/10 text-destructive',
              )}
            >
              {up ? '↑' : '↓'} {Math.abs(trend.pct).toFixed(1)}%{trend.label ? ` ${trend.label}` : ''}
            </span>
          )}
          {hint && <span className="text-muted-foreground">{hint}</span>}
        </div>
      )}
    </div>
  );
}
