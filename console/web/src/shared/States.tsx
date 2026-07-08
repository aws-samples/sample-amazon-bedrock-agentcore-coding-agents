import { cn } from '@foxl/ui';

/**
 * Skeleton placeholder while a section's data loads: a heading bar, a row of
 * stat tiles, then a few chart-height blocks. Uses the shared `animate-shimmer`
 * utility so the loading shimmer matches the rest of the console.
 */
export function LoadingState({ rows = 2, className }: { rows?: number; className?: string }) {
  const block = 'animate-shimmer rounded-lg bg-gradient-to-r from-muted via-muted/40 to-muted bg-[length:200%_100%]';
  return (
    <div className={cn('space-y-4', className)}>
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
    <div className={cn('rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive', className)}>
      <span className="font-semibold">Couldn't load this section: </span>
      {error}
    </div>
  );
}

/** A calm dashed-border empty slot, used when a table or chart has no rows yet. */
export function EmptyState({ title, hint, className }: { title: string; hint?: string; className?: string }) {
  return (
    <div className={cn('rounded-lg border border-dashed border-border bg-muted/30 px-6 py-12 text-center', className)}>
      <div className="text-sm font-medium text-foreground">{title}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}
