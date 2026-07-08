import { useCallback, useMemo, useState } from 'react';

/**
 * Column-sort state for the console's per-row statistics tables.
 *
 * Clicking a column flips its direction when it is already active, otherwise it
 * activates that column at the supplied initial direction. `null`/`undefined`
 * values are always parked at the bottom regardless of direction, so a missing
 * metric never floats to the top of a leaderboard. Strings compare via
 * `localeCompare` (so non-ASCII labels order naturally); numbers compare by value.
 *
 *   const accessors = {
 *     agent: (r) => r.agent,        // string
 *     cost:  (r) => r.cost_usd,     // number
 *     p95:   (r) => r.p95 ?? null,  // nullable
 *   };
 *   const { rows, sortKey, sortDir, toggle } = useSortable(data, accessors, {
 *     initialKey: 'cost', initialDir: 'desc',
 *   });
 */
export type SortDir = 'asc' | 'desc';
export type SortValue = string | number | null | undefined;

export interface SortableOptions<K extends string> {
  initialKey: K;
  initialDir?: SortDir;
}

export function useSortable<T, K extends string>(
  items: T[],
  accessors: Record<K, (item: T) => SortValue>,
  opts: SortableOptions<K>,
) {
  const [sortKey, setSortKey] = useState<K>(opts.initialKey);
  const [sortDir, setSortDir] = useState<SortDir>(opts.initialDir ?? 'desc');

  const toggle = useCallback(
    (k: K) => {
      if (k === sortKey) {
        setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
      } else {
        setSortKey(k);
        setSortDir(opts.initialDir ?? 'desc');
      }
    },
    [sortKey, opts.initialDir],
  );

  const rows = useMemo(() => {
    const read = accessors[sortKey];
    if (!read) return items;
    const mul = sortDir === 'asc' ? 1 : -1;
    return [...items].sort((a, b) => {
      const av = read(a);
      const bv = read(b);
      const aNull = av == null;
      const bNull = bv == null;
      if (aNull && bNull) return 0;
      if (aNull) return 1; // nulls last, always
      if (bNull) return -1;
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * mul;
      return String(av).localeCompare(String(bv)) * mul;
    });
  }, [items, accessors, sortKey, sortDir]);

  return { rows, sortKey, sortDir, toggle };
}
