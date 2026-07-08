import { cn } from '@foxl/ui';

/**
 * The header at the top of a dashboard section: a title, an optional subtitle,
 * an optional right-aligned control slot, and an optional data-source pill. The
 * pill states where the numbers come from ("live" for the real engine path,
 * "ledger" for the shared telemetry file), so a reader always knows the source
 * of what they are looking at.
 */
export function SectionHeader({
  title,
  subtitle,
  eyebrow,
  right,
  source,
  note,
  className,
}: {
  title: string;
  subtitle?: string;
  /** Optional mono eyebrow above the title: the brand's "technical" voice. */
  eyebrow?: string;
  right?: React.ReactNode;
  source?: 'live' | 'ledger';
  note?: string;
  className?: string;
}) {
  return (
    <div className={cn('flex items-start justify-between gap-6', className)}>
      <div className="min-w-0">
        {eyebrow && <div className="eyebrow mb-1.5">{eyebrow}</div>}
        {/* Display heading: weight 600, aggressive negative tracking (DESIGN.md). */}
        <h1 className="text-2xl font-semibold tracking-[-0.03em] text-foreground">{title}</h1>
        {subtitle && <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-muted-foreground">{subtitle}</p>}
        {source && (
          <div className="mt-2.5 flex items-center gap-2">
            <span
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium',
                source === 'live'
                  ? 'border-success/30 bg-success/10 text-success'
                  : 'border-border bg-muted text-muted-foreground',
              )}
            >
              <span className={cn('size-1.5 rounded-full', source === 'live' ? 'bg-success' : 'bg-muted-foreground/60')} />
              {source === 'live' ? 'Live' : 'Ledger'}
            </span>
            {note && <span className="max-w-md truncate text-[11px] italic text-muted-foreground">{note}</span>}
          </div>
        )}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  );
}
