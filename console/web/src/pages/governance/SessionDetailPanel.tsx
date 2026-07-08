import { useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Cell,
} from 'recharts';
import { cn } from '@foxl/ui';
import { ShieldCheck, KeyRound, GitPullRequest, X } from 'lucide-react';
import {
  getIdentity, getUserMetrics, type Identity, type UserMetrics, type SessionRow,
} from '../../api';
import { fmtUsd, fmtNum, fmtSeconds, maskHandle, useChartTheme } from '../../shared';

/**
 * A right-side slide-in drill-down for one governance session. It fetches the
 * recorded user attribution and the owning user's roll-up (runs, cost, tokens,
 * p95). It lays them out as evidence tiles, a per-agent
 * cost chart, and a cost split. Open by passing a `session`; close clears it.
 */
export function SessionDetailPanel({ session, onClose }: { session: SessionRow | null; onClose: () => void }) {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [metrics, setMetrics] = useState<UserMetrics | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const theme = useChartTheme();

  const sid = session?.session_id ?? null;
  const user = session?.user_id ?? session?.user ?? null;

  useEffect(() => {
    if (!sid) return;
    let live = true;
    setLoading(true);
    setErr(null);
    setIdentity(null);
    setMetrics(null);
    Promise.all([
      getIdentity(sid),
      user ? getUserMetrics(user, 'all') : Promise.resolve(null),
    ])
      .then(([id, m]) => {
        if (!live) return;
        setIdentity(id);
        setMetrics(m);
      })
      .catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [sid, user]);

  const open = !!session;
  const costByAgent = metrics?.cost_by_agent ?? metrics?.by_agent ?? {};
  const costBars = useMemo(
    () => Object.entries(costByAgent).map(([agent, usd]) => ({ agent, usd: usd as number })),
    [costByAgent],
  );

  return (
    <>
      {/* Backdrop: click to dismiss. Fades with the panel. */}
      <div
        onClick={onClose}
        className={cn(
          'fixed inset-0 z-30 bg-background/40 backdrop-blur-[2px] transition-opacity',
          open ? 'opacity-100' : 'pointer-events-none opacity-0',
        )}
      />

      {/* The panel itself slides in from the right. */}
      <aside
        className={cn(
          'fixed bottom-0 right-0 top-0 z-40 w-[460px] max-w-[92vw] overflow-y-auto border-l border-border bg-card shadow-xl transition-transform duration-200 ease-soft',
          open ? 'translate-x-0' : 'translate-x-full',
        )}
      >
        {session && (
          <div className="space-y-5 p-6">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Session identity
                </div>
                <h2 className="mt-0.5 break-all font-mono text-sm font-semibold text-foreground">{session.session_id}</h2>
                <div className="mt-1 text-xs text-muted-foreground">
                  {session.assistant_type ?? session.agent ?? 'agent'} · {maskHandle(user) || 'unknown user'}
                </div>
              </div>
              <button
                onClick={onClose}
                aria-label="Close"
                className="flex size-7 items-center justify-center rounded-full border border-border text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <X className="size-3.5" />
              </button>
            </div>

            {loading && <div className="animate-shimmer h-28 rounded-xl bg-gradient-to-r from-muted via-muted/40 to-muted bg-[length:200%_100%]" />}
            {err && (
              <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{err}</div>
            )}

            {!loading && identity && (
              <>
                {/* Session attribution: facts read from the run ledger. GitHub
                    authorship is credential-dependent and is not inferred here. */}
                <div className="grid grid-cols-2 gap-2">
                  <AttestTile
                    icon={<ShieldCheck className="size-3.5" />}
                    label="Recorded user"
                    value={maskHandle(identity.recorded_user)}
                    good={Boolean(identity.recorded_user)}
                    note="from the run ledger"
                  />
                  <AttestTile
                    icon={<KeyRound className="size-3.5" />}
                    label="Static creds on agent"
                    value={identity.static_credentials_on_agent ? 'present' : 'none'}
                    good={!identity.static_credentials_on_agent}
                    note="brokered, never stored"
                  />
                  <AttestTile
                    icon={<GitPullRequest className="size-3.5" />}
                    label="GitHub actor"
                    value={identity.github_actor}
                    good
                    note="resolved at PR time"
                  />
                  <AttestTile
                    label="Authentication"
                    value={identity.auth_provider}
                    good
                    note={identity.attribution_source}
                  />
                </div>

                {/* The owning user's roll-up: the per-user cost surface, scoped. */}
                {metrics && (
                  <div className="rounded-xl border border-border bg-background p-4">
                    <div className="mb-3 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                      This user's totals
                    </div>
                    <div className="grid grid-cols-4 gap-2 text-sm">
                      <MiniTile label="Runs" value={fmtNum(metrics.runs ?? 0)} />
                      <MiniTile label="Cost" value={fmtUsd(metrics.total_cost_usd ?? 0)} accent />
                      <MiniTile label="Tokens" value={fmtNum(metrics.total_tokens ?? 0)} />
                      <MiniTile label="p95" value={fmtSeconds(metrics.p95_latency_ms ?? 0)} />
                    </div>

                    {costBars.length > 0 && (
                      <div className="mt-4 space-y-2">
                        <div className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                          Cost by agent
                        </div>
                        <ResponsiveContainer width="100%" height={150}>
                          <BarChart data={costBars} margin={{ top: 4, right: 8, left: -18, bottom: 0 }}>
                            <CartesianGrid strokeDasharray="2 4" stroke={theme.grid} />
                            <XAxis dataKey="agent" stroke={theme.axis} fontSize={10} tickLine={false} />
                            <YAxis stroke={theme.axis} fontSize={10} tickLine={false} tickFormatter={(v) => `$${v}`} />
                            <Tooltip
                              cursor={{ fill: theme.faint }}
                              contentStyle={{ background: theme.tooltipBg, border: `1px solid ${theme.tooltipBorder}`, borderRadius: 8, fontSize: 12 }}
                              labelStyle={{ color: theme.tooltipText }}
                              formatter={(v) => [fmtUsd(Number(v)), 'cost']}
                            />
                            <Bar dataKey="usd" radius={[3, 3, 0, 0]} maxBarSize={48}>
                              {costBars.map((_, i) => (
                                <Cell key={i} fill={theme.series[i % theme.series.length]} />
                              ))}
                            </Bar>
                          </BarChart>
                        </ResponsiveContainer>
                        {costBars.map(({ agent, usd }) => (
                          <div key={agent} className="flex items-center justify-between border-b border-border/60 py-1.5 text-sm last:border-0">
                            <span className="text-foreground">{agent}</span>
                            <span className="font-mono text-muted-foreground">{fmtUsd(usd)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                <p className="text-[11px] leading-relaxed text-muted-foreground">
                  This is the user recorded on the run for audit and cost attribution.
                  GitHub authorship depends on the credential used for the GitHub call; this panel does not attest OAuth OBO delegation.
                </p>
              </>
            )}
          </div>
        )}
      </aside>
    </>
  );
}

function AttestTile({
  icon, label, value, note, good,
}: {
  icon?: React.ReactNode;
  label: string;
  value: string;
  note?: string;
  good?: boolean;
}) {
  return (
    <div className={cn('rounded-lg border px-3 py-2', good ? 'border-success/30 bg-success/5' : 'border-border bg-background')}>
      <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {icon}
        <span className="truncate">{label}</span>
      </div>
      <div className="mt-1 truncate text-[13px] font-semibold text-foreground" title={value}>{value}</div>
      {note && <div className="mt-0.5 truncate text-[10px] text-muted-foreground">{note}</div>}
    </div>
  );
}

function MiniTile({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className={cn('rounded-lg border px-2 py-1.5', accent ? 'border-primary/30 bg-primary/5' : 'border-border bg-card')}>
      <div className="truncate text-[10px] font-medium uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-1 text-[15px] font-semibold leading-none tabular-nums text-foreground">{value}</div>
    </div>
  );
}
