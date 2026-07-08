import { cn } from '@foxl/ui';
import type { SortDir } from './useSortable';

/**
 * A sortable table header cell. Clicking it asks the parent to sort by `k`
 * (toggling direction when it is already the active column). The active column
 * shows a ▲/▼ glyph and a faint highlight; inactive columns show a dim ↕ to
 * advertise that they are sortable. Pair with `useSortable`.
 */
export function SortableTh<K extends string>({
  label,
  k,
  sortKey,
  sortDir,
  onClick,
  align = 'right',
  className,
}: {
  label: string;
  k: K;
  sortKey: string;
  sortDir: SortDir;
  onClick: (k: K) => void;
  align?: 'left' | 'right';
  className?: string;
}) {
  const active = sortKey === k;
  return (
    <th
      onClick={() => onClick(k)}
      title={`Sort by ${label}`}
      className={cn(
        'cursor-pointer select-none whitespace-nowrap px-3 py-2 font-mono text-[11px] font-medium uppercase tracking-wider',
        active ? 'bg-muted/50 text-foreground' : 'text-muted-foreground hover:text-foreground',
        align === 'left' ? 'text-left' : 'text-right',
        className,
      )}
    >
      {label}
      <span className={cn('ml-1 inline-block w-2 text-[10px]', active ? 'opacity-100' : 'opacity-30')}>
        {active ? (sortDir === 'asc' ? '▲' : '▼') : '↕'}
      </span>
    </th>
  );
}
