import { cn } from '@foxl/ui';

/**
 * A framed chart surface: a title (+ optional subtitle) on the left, optional
 * controls on the right, and the chart itself in the padded body. Pair with
 * Recharts' <ResponsiveContainer> for the body. Matches StatCard's radius,
 * border, and card background so a dashboard grid reads as one set.
 */
export function ChartCard({
  title,
  subtitle,
  right,
  children,
  className,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('rounded-lg border border-border bg-card shadow-sm', className)}>
      <div className="flex items-start justify-between gap-4 px-5 pb-2 pt-4">
        <div>
          <h3 className="text-sm font-semibold tracking-[-0.01em] text-foreground">{title}</h3>
          {subtitle && <p className="mt-0.5 text-xs text-muted-foreground">{subtitle}</p>}
        </div>
        {right && <div className="shrink-0">{right}</div>}
      </div>
      <div className="px-2 pb-4">{children}</div>
    </div>
  );
}
