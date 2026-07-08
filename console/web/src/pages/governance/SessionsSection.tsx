import { useCallback, useEffect, useMemo, useState } from 'react';
import { cn, Input } from '@foxl/ui';
import { listSessions, stopSession, getDashboard, type SessionRow, type Dashboard } from '../../api';
import {
  StatCard, SortableTh, useSortable, LoadingState, ErrorState, EmptyState, fmtNum, fmtSeconds, fmtTime, maskHandle,
} from '../../shared';
import { PulseDot } from '../../components/Motion';
import { SessionDetailPanel } from './SessionDetailPanel';

type K = 'session' | 'agent' | 'user' | 'started' | 'state';

// Lookback presets mapped to the API's real `window` filter (in MINUTES).
// `all` omits the param. This is the analogue of a date-range control, scoped
// to what the sessions endpoint actually supports.
const WINDOWS: { key: string; label: string; minutes: number | null }[] = [
  { key: '1h', label: '1h', minutes: 60 },
  { key: '24h', label: '24h', minutes: 60 * 24 },
  { key: '7d', label: '7d', minutes: 60 * 24 * 7 },
  { key: 'all', label: 'All', minutes: null },
];

const PAGE = 12; // rows per page; the ledger can hold thousands of sessions

/**
 * The governance session inventory: every Runtime session a run produced, the
 * human it ran as, its liveness, and a kill switch. Click a row to drill into
 * its recorded identity + cost (the slide-in panel). The table sorts on any column
 * and filters by a free-text query over session / agent / user.
 *
 * The kill switch calls the REAL stop endpoint (StopRuntimeSession on AgentCore,
 * a process signal locally) and reflects the result optimistically, then a
 * refresh confirms from the ledger.
 */
export function SessionsSection() {
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState<SessionRow | null>(null);
  const [stopped, setStopped] = useState<Record<string, boolean>>({});
  const [stopping, setStopping] = useState<Record<string, boolean>>({});
  const [windowKey, setWindowKey] = useState('all');
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [page, setPage] = useState(0);

  const refresh = useCallback(() => {
    setErr(null);
    const w = WINDOWS.find((x) => x.key === windowKey);
    // The KPI strip is the fleet-wide rollup (always the dashboard), independent
    // of the table's window filter, so the headline numbers stay stable while
    // the table narrows.
    getDashboard().then(setDash).catch(() => {});
    return listSessions(w?.minutes != null ? { window: w.minutes } : undefined)
      .then(setSessions)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [windowKey]);

  useEffect(() => {
    let live = true;
    refresh();
    // Poll so a freshly-opened Stage-1 session or a new run appears without a
    // manual reload. 10s (not 4s) keeps the governance view current without
    // hammering the sessions endpoint, and we skip ticks while the tab is hidden
    // so a backgrounded console isn't doing needless work.
    const t = setInterval(() => {
      if (live && document.visibilityState === 'visible') refresh();
    }, 10000);
    return () => { live = false; clearInterval(t); };
  }, [refresh]);

  async function kill(e: React.MouseEvent, sessionId: string) {
    e.stopPropagation(); // don't open the drill-down when killing
    setStopping((m) => ({ ...m, [sessionId]: true }));
    try {
      await stopSession(sessionId);
      setStopped((m) => ({ ...m, [sessionId]: true }));
      await refresh();
    } catch {
      /* leave the row; the next refresh reflects the truth */
    } finally {
      setStopping((m) => ({ ...m, [sessionId]: false }));
    }
  }

  const filtered = useMemo(() => {
    const f = q.trim().toLowerCase();
    if (!f) return sessions;
    return sessions.filter((s) =>
      [s.session_id, s.assistant_type ?? s.agent, s.user_id ?? s.user]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(f)),
    );
  }, [sessions, q]);

  const accessors: Record<K, (s: SessionRow) => string | number | null> = {
    session: (s) => s.session_id,
    agent: (s) => s.assistant_type ?? s.agent ?? null,
    user: (s) => s.user_id ?? s.user ?? null,
    started: (s) => s.started_at ?? null,
    state: (s) => (stopped[s.session_id] ? 0 : s.claude_running ? 2 : 1),
  };
  const { rows, sortKey, sortDir, toggle } = useSortable<SessionRow, K>(filtered, accessors, {
    initialKey: 'started',
    initialDir: 'desc',
  });

  // Page the table; the ledger can hold thousands of rows; show a window so the
  // section paints fast and stays readable. Reset to page 0 when the filter or
  // window changes (the row set underneath shifts).
  const pageRows = useMemo(() => rows.slice(page * PAGE, page * PAGE + PAGE), [rows, page]);
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE));
  useEffect(() => { setPage(0); }, [q, windowKey]);

  const Th = (p: { label: string; k: K; align?: 'left' | 'right' }) => (
    <SortableTh<K> label={p.label} k={p.k} sortKey={sortKey} sortDir={sortDir} onClick={toggle} align={p.align ?? 'left'} />
  );

  if (loading) return <LoadingState rows={1} />;
  if (err) return <ErrorState error={err} />;

  return (
    <div className="space-y-4">
      {/* Fleet-wide rollup over the sessions table: active count, total spend,
          and p95, all from the dashboard endpoint. */}
      <div className="grid grid-cols-3 gap-4">
        <StatCard accent label="Active sessions" value={dash ? fmtNum(dash.active_sessions) : '-'} hint="running now" delay={0} />
        <StatCard label="Total cost" value={dash ? `$${Object.values(dash.cost_by_agent).reduce((s, n) => s + n, 0).toFixed(2)}` : '-'} hint="attributed" delay={60} />
        <StatCard label="p95 latency" value={dash ? fmtSeconds(dash.p95_latency_ms) : '-'} hint="fleet-wide" delay={120} />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {rows.length} session{rows.length === 1 ? '' : 's'}
          {windowKey !== 'all' ? ` in the last ${WINDOWS.find((w) => w.key === windowKey)?.label}` : ' on the ledger'}
          {pageCount > 1 ? ` · page ${page + 1} of ${pageCount}` : ''} · click a row for recorded attribution
        </p>
        <div className="flex items-center gap-2">
          {/* Lookback presets: each re-queries the sessions endpoint with a real
              `window` filter (minutes). */}
          <div className="flex items-center rounded-lg border border-border bg-card p-0.5 text-xs font-medium">
            {WINDOWS.map((w) => (
              <button
                key={w.key}
                onClick={() => setWindowKey(w.key)}
                className={cn(
                  'rounded-md px-2.5 py-1 transition-colors',
                  windowKey === w.key ? 'bg-primary text-primary-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {w.label}
              </button>
            ))}
          </div>
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter sessions…"
            className="h-8 w-56"
          />
        </div>
      </div>

      {rows.length === 0 ? (
        <EmptyState title="No sessions match" hint="Run a task on the Tasks page or open a shell in Development to populate the inventory." />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <table className="w-full text-sm">
            <thead className="bg-muted/40">
              <tr>
                <Th label="Session" k="session" />
                <Th label="Agent" k="agent" />
                <Th label="User" k="user" />
                <Th label="Started" k="started" />
                <Th label="State" k="state" />
                <th className="px-3 py-2 text-right font-mono text-[11px] uppercase tracking-wide text-muted-foreground">Kill</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((s) => {
                const isStopped = stopped[s.session_id] || !s.claude_running;
                const busy = stopping[s.session_id];
                return (
                  <tr
                    key={s.session_id}
                    onClick={() => setSelected(s)}
                    className={cn(
                      'cursor-pointer border-t border-border transition-colors',
                      selected?.session_id === s.session_id ? 'bg-muted/60' : 'hover:bg-muted/40',
                    )}
                  >
                    <td className="max-w-[220px] truncate px-3 py-2.5 font-mono text-xs text-muted-foreground" title={s.session_id}>
                      {s.session_id}
                    </td>
                    <td className="px-3 py-2.5 text-foreground">{s.assistant_type ?? s.agent ?? '-'}</td>
                    <td className="px-3 py-2.5 text-muted-foreground">{maskHandle(s.user_id ?? s.user) || '-'}</td>
                    <td className="px-3 py-2.5 tabular-nums text-muted-foreground">{fmtTime(s.started_at)}</td>
                    <td className="px-3 py-2.5">
                      <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                        {!stopped[s.session_id] && s.claude_running && <PulseDot live tone="success" size={6} />}
                        {stopped[s.session_id] ? 'stopped' : s.claude_running ? 'running' : 'idle'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <button
                        onClick={(e) => kill(e, s.session_id)}
                        disabled={isStopped || busy}
                        className="rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:pointer-events-none disabled:opacity-40"
                      >
                        {busy ? 'Stopping…' : 'Stop'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {pageCount > 1 && (
        <div className="flex items-center justify-end gap-2">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
          >
            Prev
          </button>
          <span className="text-xs tabular-nums text-muted-foreground">{page + 1} / {pageCount}</span>
          <button
            onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
            disabled={page >= pageCount - 1}
            className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
          >
            Next
          </button>
        </div>
      )}

      <SessionDetailPanel session={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
