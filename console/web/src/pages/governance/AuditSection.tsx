import { useEffect, useMemo, useState } from 'react';
import { cn, Input } from '@foxl/ui';
import { getAudit, type AuditRow } from '../../api';
import { LoadingState, ErrorState, EmptyState, StatCard, fmtTime, maskHandle } from '../../shared';

// A stable muted tone per event kind, so the eye can scan the feed by type
// without a brand color. Unknown kinds fall through to the neutral chip.
const KIND_TONE: Record<string, string> = {
  orchestrator_run: 'bg-success/10 text-success',
  stage1_conversion: 'bg-primary/10 text-foreground',
  stage1_session: 'bg-primary/10 text-foreground',
};

/**
 * The append-only governance audit trail: every real ledger event (orchestrator
 * runs, Stage-1 sessions, deploys, verifies) rendered as one auditable line. A
 * free-text filter narrows the feed; a kind chip colors each row. Nothing here
 * is synthesized; the source path is shown in the header.
 */
export function AuditSection() {
  const [trail, setTrail] = useState<AuditRow[]>([]);
  const [source, setSource] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState('');

  useEffect(() => {
    let live = true;
    const load = () =>
      getAudit(200)
        .then((d) => {
          if (!live) return;
          // Newest first; the feed reads top-down like a tail.
          setTrail([...d.audit].reverse());
          setSource(d.source);
          setErr(null);
        })
        .catch((e) => live && setErr(String(e)))
        .finally(() => live && setLoading(false));
    load();
    // Refresh the trail every 10s, and only while the tab is visible.
    const t = setInterval(() => {
      if (live && document.visibilityState === 'visible') load();
    }, 10000);
    return () => { live = false; clearInterval(t); };
  }, []);

  const kinds = useMemo(() => Array.from(new Set(trail.map((r) => r.kind))), [trail]);

  const filtered = useMemo(() => {
    const f = q.trim().toLowerCase();
    if (!f) return trail;
    return trail.filter((r) => `${r.kind} ${r.user_id} ${r.line}`.toLowerCase().includes(f));
  }, [trail, q]);

  if (loading) return <LoadingState rows={1} />;
  if (err) return <ErrorState error={err} />;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatCard accent label="Events" value={String(trail.length)} hint="append-only" />
        <StatCard label="Event kinds" value={String(kinds.length)} hint={kinds.slice(0, 3).join(', ')} />
        <StatCard label="Source" value="ledger" hint={source.split('/').pop() ?? 'telemetry.jsonl'} />
      </div>

      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">Real ledger feed · {source}</p>
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter the trail…" className="h-8 w-56" />
      </div>

      {filtered.length === 0 ? (
        <EmptyState title="No audit events match" hint="Submit a task on the Tasks page and the run lands here." />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <ul className="divide-y divide-border">
            {filtered.map((r, i) => (
              <li key={`${r.at}-${i}`} className="flex items-start gap-3 px-4 py-2.5">
                <span className={cn('mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium', KIND_TONE[r.kind] ?? 'bg-muted text-muted-foreground')}>
                  {r.kind}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="break-words font-mono text-xs leading-relaxed text-foreground/90">{r.line}</div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {fmtTime(r.at)} · {maskHandle(r.user_id) || 'unknown'}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
