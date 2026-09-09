import { cn } from '@foxl/ui';
import { Inbox, CircleAlert } from 'lucide-react';

/**
 * Skeleton placeholder while a section's data loads: a heading bar, a row of
 * stat tiles, then a few chart-height blocks. Uses the shared `animate-shimmer`
 * utility so the loading shimmer matches the rest of the console.
 */
export function LoadingState({ rows = 2, className }: { rows?: number; className?: string }) {
  const block = 'animate-shimmer rounded-lg bg-gradient-to-r from-muted via-muted/40 to-muted bg-[length:200%_100%]';
  return (
    <div className={cn('space-y-4', className)} role="status" aria-label="Loading data">
      <div className={cn(block, 'h-6 w-48 rounded')} />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className={cn(block, 'h-24')} />
        ))}
      </div>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className={cn(block, 'h-56')} />
      ))}
    </div>
  );
}

/** A bordered error panel: the destructive-toned counterpart to LoadingState. */
export function ErrorState({ error, className }: { error: string; className?: string }) {
  return (
    <div role="alert" className={cn('flex gap-3 rounded-xl border border-destructive/25 bg-destructive/5 p-4 text-sm text-destructive', className)}>
      <CircleAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0" />
      <div className="min-w-0 break-words leading-6">
        <p className="font-medium">Could not load the evidence</p>
        <p>{error}</p>
      </div>
    </div>
  );
}

/** A calm dashed-border empty slot, used when a table or chart has no rows yet. */
export function EmptyState({ title, hint, className }: { title: string; hint?: string; className?: string }) {
  return (
    <div className={cn('rounded-xl border border-border bg-card px-6 py-12 text-center', className)}>
      <span className="mx-auto mb-4 flex size-11 items-center justify-center rounded-xl border border-border bg-background">
        <Inbox aria-hidden="true" className="size-5 text-muted-foreground" />
      </span>
      <div className="text-sm font-medium text-foreground">{title}</div>
      {hint && <div className="mx-auto mt-2 max-w-lg text-sm leading-6 text-muted-foreground">{hint}</div>}
    </div>
  );
}
