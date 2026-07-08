import { useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Cell,
} from 'recharts';
import {
  getDashboard, getCostBreakdown, getLatencyP95,
  type Dashboard, type CostBreakdown, type LatencyP95,
} from '../../api';
import {
  StatCard, ChartCard, LoadingState, ErrorState, EmptyState, useChartTheme,
  fmtNum, fmtUsd, fmtSeconds,
} from '../../shared';

/**
 * The governance Overview: at-a-glance fleet health composed from the same
 * four metric functions the API exposes (dashboard, cost-breakdown, latency).
 * This proves the API-first invariant: the page derives nothing the endpoints
 * don't already give. KPIs up top, a cost-by-agent bar, then a small latency
 * read-out.
 */
export function OverviewSection() {
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [cost, setCost] = useState<CostBreakdown | null>(null);
  const [latency, setLatency] = useState<LatencyP95 | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const theme = useChartTheme();

  useEffect(() => {
    let live = true;
    Promise.all([getDashboard(), getCostBreakdown('agent'), getLatencyP95()])
      .then(([d, c, l]) => {
        if (!live) return;
        setDash(d);
        setCost(c);
        setLatency(l);
      })
      .catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, []);

  const totalCost = useMemo(
    () => (cost ? Object.values(cost.breakdown).reduce((s, n) => s + n, 0) : 0),
    [cost],
  );

  const costBars = useMemo(
    () =>
      cost
        ? Object.entries(cost.breakdown)
            .map(([agent, usd]) => ({ agent, usd }))
            .sort((a, b) => b.usd - a.usd)
        : [],
    [cost],
  );

  if (loading) return <LoadingState />;
  if (err) return <ErrorState error={err} />;
  if (!dash) return <EmptyState title="No metrics yet" hint="Run a task on the Tasks page and the numbers move." />;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard accent label="Active sessions" value={fmtNum(dash.active_sessions)} hint="running right now" delay={0} />
        <StatCard label="Runs total" value={fmtNum(dash.runs_total)} hint="across the ledger" delay={60} />
        <StatCard label="p95 latency" value={fmtSeconds(dash.p95_latency_ms)} hint="fleet-wide" delay={120} />
        <StatCard label="Total spend" value={fmtUsd(totalCost)} hint="attributed, no winner" delay={180} />
      </div>

      <ChartCard title="Cost by agent" subtitle="Spend grouped from the run ledger: attribution, not a ranking.">
        {costBars.length === 0 ? (
          <div className="px-3">
            <EmptyState title="No spend recorded yet" hint="A run with a real model invocation populates this." />
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={costBars} margin={{ top: 8, right: 16, left: -12, bottom: 8 }}>
              <CartesianGrid strokeDasharray="2 4" stroke={theme.grid} />
              <XAxis dataKey="agent" stroke={theme.axis} fontSize={11} tickLine={false} />
              <YAxis stroke={theme.axis} fontSize={11} tickLine={false} tickFormatter={(v) => `$${v}`} />
              <Tooltip
                cursor={{ fill: theme.faint }}
                contentStyle={{ background: theme.tooltipBg, border: `1px solid ${theme.tooltipBorder}`, borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: theme.tooltipText }}
                formatter={(v) => [fmtUsd(Number(v)), 'cost']}
              />
              <Bar dataKey="usd" radius={[4, 4, 0, 0]} maxBarSize={72}>
                {costBars.map((_, i) => (
                  <Cell key={i} fill={theme.series[i % theme.series.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </ChartCard>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard label="p95, fleet-wide" value={fmtSeconds(latency?.p95_latency_ms ?? dash.p95_latency_ms)} hint="nearest-rank, deterministic" />
        <StatCard label="Agents attributed" value={fmtNum(Object.keys(cost?.breakdown ?? {}).length)} hint="distinct roles billed" />
        <StatCard label="Currency" value={cost?.currency ?? 'USD'} hint="published Bedrock rates" />
      </div>
    </div>
  );
}
