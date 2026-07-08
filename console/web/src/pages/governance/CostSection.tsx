import { useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Cell,
} from 'recharts';
import { cn } from '@foxl/ui';
import { getCostBreakdown, type CostBreakdown } from '../../api';
import {
  ChartCard, SortableTh, useSortable, StatCard, LoadingState, ErrorState, EmptyState,
  useChartTheme, fmtUsd, fmtPct, maskHandle,
} from '../../shared';

type By = 'agent' | 'user';
type Row = { key: string; usd: number; share: number };
type K = 'key' | 'usd' | 'share';

/**
 * The per-user cost surface, the P0 of Module 3. A segmented toggle switches the
 * projection between by-agent (which role spent) and by-user (the OBO chargeback
 * view). A bar chart up top, a sortable share table below. The numbers are the
 * exact `/cost-breakdown` payload; the share column is computed locally from the
 * total so it always sums to 100%.
 */
export function CostSection() {
  const [by, setBy] = useState<By>('agent');
  const [data, setData] = useState<CostBreakdown | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const theme = useChartTheme();

  useEffect(() => {
    let live = true;
    setLoading(true);
    setErr(null);
    getCostBreakdown(by)
      .then((d) => live && setData(d))
      .catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [by]);

  const total = useMemo(
    () => (data ? Object.values(data.breakdown).reduce((s, n) => s + n, 0) : 0),
    [data],
  );

  const rows = useMemo<Row[]>(() => {
    if (!data) return [];
    return Object.entries(data.breakdown).map(([key, usd]) => ({
      key,
      usd,
      share: total > 0 ? usd / total : 0,
    }));
  }, [data, total]);

  const accessors: Record<K, (r: Row) => string | number> = {
    key: (r) => r.key,
    usd: (r) => r.usd,
    share: (r) => r.share,
  };
  const { rows: sorted, sortKey, sortDir, toggle } = useSortable<Row, K>(rows, accessors, {
    initialKey: 'usd',
    initialDir: 'desc',
  });

  const chart = useMemo(() => [...rows].sort((a, b) => b.usd - a.usd), [rows]);
  const label = by === 'agent' ? 'Agent' : 'User';

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {by === 'agent'
            ? 'Spend attributed per agent role. No race, no winner.'
            : 'Spend grouped by the authenticated user recorded in the run ledger.'}
        </p>
        {/* Segmented by-dimension toggle */}
        <div className="flex items-center rounded-lg border border-border bg-card p-0.5 text-xs font-medium">
          {(['agent', 'user'] as By[]).map((opt) => (
            <button
              key={opt}
              onClick={() => setBy(opt)}
              className={cn(
                'rounded-md px-3 py-1 capitalize transition-colors',
                by === opt ? 'bg-primary text-primary-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              By {opt}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <LoadingState rows={1} />
      ) : err ? (
        <ErrorState error={err} />
      ) : rows.length === 0 ? (
        <EmptyState title="No spend recorded yet" hint="A run that invoked a model attributes its cost here." />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            <StatCard accent label="Total spend" value={fmtUsd(total)} hint={`${data?.currency ?? 'USD'} · ${rows.length} ${by}s`} />
            <StatCard label="Top spender" value={chart[0]?.key ?? '-'} hint={chart[0] ? fmtUsd(chart[0].usd) : ''} />
            <StatCard label="Projection" value={`By ${by}`} hint="forgiving dimension param" />
          </div>

          <ChartCard title={`Cost by ${by}`} subtitle="Real token counts priced at published Bedrock rates.">
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={chart} margin={{ top: 8, right: 16, left: -12, bottom: 8 }}>
                <CartesianGrid strokeDasharray="2 4" stroke={theme.grid} />
                <XAxis dataKey="key" stroke={theme.axis} fontSize={11} tickLine={false} />
                <YAxis stroke={theme.axis} fontSize={11} tickLine={false} tickFormatter={(v) => `$${v}`} />
                <Tooltip
                  cursor={{ fill: theme.faint }}
                  contentStyle={{ background: theme.tooltipBg, border: `1px solid ${theme.tooltipBorder}`, borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: theme.tooltipText }}
                  formatter={(v) => [fmtUsd(Number(v)), 'cost']}
                />
                <Bar dataKey="usd" radius={[4, 4, 0, 0]} maxBarSize={72}>
                  {chart.map((_, i) => (
                    <Cell key={i} fill={theme.series[i % theme.series.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </ChartCard>

          <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
            <table className="w-full text-sm">
              <thead className="bg-muted/40">
                <tr>
                  <SortableTh<K> label={label} k="key" sortKey={sortKey} sortDir={sortDir} onClick={toggle} align="left" />
                  <SortableTh<K> label="Cost (USD)" k="usd" sortKey={sortKey} sortDir={sortDir} onClick={toggle} />
                  <SortableTh<K> label="Share" k="share" sortKey={sortKey} sortDir={sortDir} onClick={toggle} />
                </tr>
              </thead>
              <tbody>
                {sorted.map((r) => (
                  <tr key={r.key} className="border-t border-border hover:bg-muted/40">
                    <td className="px-3 py-2.5 font-medium text-foreground">{by === 'user' ? maskHandle(r.key) : r.key}</td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-muted-foreground">{fmtUsd(r.usd)}</td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-muted-foreground">{fmtPct(r.share)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
