import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Cell,
} from 'recharts';
import {
  getDashboard, getCostBreakdown,
  type Dashboard, type CostBreakdown,
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
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const theme = useChartTheme();

  useEffect(() => {
    let live = true;
    Promise.all([getDashboard(), getCostBreakdown('agent')])
      .then(([d, c]) => {
        if (!live) return;
        setDash(d);
        setCost(c);
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
  if (!dash) return <EmptyState title="No metrics yet" hint="This console has no recorded session data yet." />;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard accent label="Active sessions" value={fmtNum(dash.active_sessions)} hint="Observed by this console" delay={0} />
        <StatCard label="Recorded sessions" value={fmtNum(dash.runs_total)} hint="In the host ledger" delay={60} />
        <StatCard label="p95 latency" value={dash.runs_total ? fmtSeconds(dash.p95_latency_ms) : 'No samples'} hint="Recorded session duration" delay={120} />
        <StatCard label="Recorded estimate" value={costBars.length ? fmtUsd(totalCost) : 'No data'} hint="Incomplete billing coverage" delay={180} />
      </div>

      <ChartCard title="Recorded estimates by agent" subtitle="Source: this host's run ledger. Missing usage is not a zero-cost build.">
        {costBars.length === 0 ? (
          <div className="px-3">
            <EmptyState title="No usage estimates recorded" hint="For the Lab 3 request events, open Governance > Attribution. CLI coordinator runs have their own history." />
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

      <div className="evidence-surface text-sm leading-6 text-muted-foreground">
        <p>These estimates cover usage present in the host ledger. Kiro usage and
          infrastructure billing require separate accounting. A missing or zero
          estimate does not establish the complete cost of a run.</p>
        <Link to="/governance/attribution" className="mt-3 inline-block font-medium text-signal">
          Open Lab 3 Attribution
        </Link>
      </div>
    </div>
  );
}
