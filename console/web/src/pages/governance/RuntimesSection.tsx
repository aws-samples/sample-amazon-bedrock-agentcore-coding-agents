import { useEffect, useMemo, useState } from 'react';
import { cn } from '@foxl/ui';
import { Cpu, Play, CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import { getGovRuntimes, probeRuntime, type GovRuntimeStatus, type ProbeResult } from '../../api';
import { StatCard, LoadingState, ErrorState, EmptyState } from '../../shared';

/**
 * The deployed-runtime fleet: Governance's window onto REAL AgentCore execution,
 * not just a read of the ledger. Each role shows whether a runtime is wired, from
 * where, its ARN, and the fleet size, all from the same runtime_config the
 * orchestrator dispatches against. The "Probe" button runs a tiny REAL job inside
 * the role's runtime (StopRuntimeSession's live sibling) and reports whether the
 * runtime actually executed, proof the fleet is alive, never a mock.
 */
export function RuntimesSection() {
  const [status, setStatus] = useState<GovRuntimeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [probing, setProbing] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, ProbeResult>>({});

  useEffect(() => {
    let live = true;
    getGovRuntimes()
      .then((s) => live && setStatus(s))
      .catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => { live = false; };
  }, []);

  const wiredCount = useMemo(
    () => status?.roles.filter((r) => r.wired).length ?? 0,
    [status],
  );
  const totalInstances = useMemo(
    () => status?.roles.reduce((n, r) => n + (r.count ?? 0), 0) ?? 0,
    [status],
  );

  async function probe(role: string) {
    setProbing((m) => ({ ...m, [role]: true }));
    setResults((m) => { const next = { ...m }; delete next[role]; return next; });
    try {
      const res = await probeRuntime(role);
      setResults((m) => ({ ...m, [role]: res }));
    } catch (e) {
      setResults((m) => ({ ...m, [role]: { role, ok: false, error: String(e) } }));
    } finally {
      setProbing((m) => ({ ...m, [role]: false }));
    }
  }

  if (loading) return <LoadingState rows={1} />;
  if (err) return <ErrorState error={err} />;
  if (!status) return <EmptyState title="No runtime status" />;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatCard accent label="Executor" value={status.executor} hint="real-only dispatch" delay={0} />
        <StatCard label="Roles wired" value={`${wiredCount} / ${status.roles.length}`} hint="have a deployed runtime" delay={60} />
        <StatCard label="Fleet instances" value={String(totalInstances)} hint="across all roles" delay={120} />
      </div>

      <p className="text-sm text-muted-foreground">
        Each role dispatches to its own deployed AgentCore Runtime. Wiring is read from the same
        config the orchestrator uses; <span className="font-medium text-foreground">Probe</span> runs a
        real one-line job inside the runtime to confirm it executes. A live, billable check.
      </p>

      <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-muted/40">
            <tr>
              <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-wide text-muted-foreground">Role</th>
              <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-wide text-muted-foreground">Status</th>
              <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-wide text-muted-foreground">Runtime ARN</th>
              <th className="px-3 py-2 text-left font-mono text-[11px] uppercase tracking-wide text-muted-foreground">Source</th>
              <th className="px-3 py-2 text-right font-mono text-[11px] uppercase tracking-wide text-muted-foreground">Probe</th>
            </tr>
          </thead>
          <tbody>
            {status.roles.map((r) => {
              const busy = probing[r.role];
              const res = results[r.role];
              return (
                <tr key={r.role} className="border-t border-border align-top">
                  <td className="px-3 py-3">
                    <div className="flex items-center gap-2">
                      <Cpu className="size-4 text-muted-foreground" />
                      <span className="font-medium text-foreground">{r.role}</span>
                      {(r.count ?? 0) > 1 && (
                        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">×{r.count}</span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-3">
                    <span
                      className={cn(
                        'inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium',
                        r.wired
                          ? 'bg-success/10 text-success'
                          : 'bg-muted text-muted-foreground',
                      )}
                    >
                      <span className={cn('size-1.5 rounded-full', r.wired ? 'bg-success' : 'bg-muted-foreground/50')} />
                      {r.wired ? 'wired' : 'not wired'}
                    </span>
                  </td>
                  <td className="px-3 py-3">
                    <span className="font-mono text-xs text-muted-foreground">
                      {r.arn ? `…${r.arn.slice(-28)}` : '-'}
                    </span>
                    {res && (
                      <div className="mt-1.5 flex items-start gap-1.5 text-xs">
                        {res.ok ? (
                          <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-success" />
                        ) : (
                          <XCircle className="mt-0.5 size-3.5 shrink-0 text-destructive" />
                        )}
                        <span className={cn('min-w-0', res.ok ? 'text-foreground' : 'text-destructive')}>
                          {res.ok
                            ? `runtime executed, marker echoed${res.session_id ? ` (session ${res.session_id})` : ''}`
                            : res.error || (res.wired === false ? 'no runtime wired' : 'probe did not echo the marker')}
                        </span>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-3 text-muted-foreground">{r.source ?? '-'}</td>
                  <td className="px-3 py-3 text-right">
                    <button
                      onClick={() => probe(r.role)}
                      disabled={!r.wired || busy}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
                    >
                      {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
                      {busy ? 'Probing…' : 'Probe'}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {status.note && <p className="text-xs italic text-muted-foreground">{status.note}</p>}
    </div>
  );
}
