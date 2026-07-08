import { useEffect, useState } from 'react';
import { cn } from '@foxl/ui';
import { ShieldX, ShieldAlert } from 'lucide-react';
import { getPolicies, type Policy } from '../../api';
import { LoadingState, ErrorState, EmptyState, StatCard } from '../../shared';

/**
 * The guardrail view: the SAME Cedar-style rule set the harness enforces at its
 * command boundary, decided OUTSIDE the model. Hard rules forbid outright; soft
 * rules gate for human approval. They are read-only here, decided in code rather
 * than toggled from a dashboard, which is exactly why no prompt can argue past
 * one. If the enforcing module isn't reachable, the API says so and we surface it.
 */
export function PoliciesSection() {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [enforced, setEnforced] = useState<boolean>(false);
  const [note, setNote] = useState<string | undefined>();
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    getPolicies()
      .then((d) => {
        if (!live) return;
        setPolicies(d.policies ?? []);
        setEnforced(!!d.enforced);
        setNote(d.note);
      })
      .catch((e) => live && setErr(String(e)))
      .finally(() => live && setLoading(false));
    return () => { live = false; };
  }, []);

  if (loading) return <LoadingState rows={1} />;
  if (err) return <ErrorState error={err} />;

  const hard = policies.filter((p) => p.tier === 'hard').length;
  const soft = policies.filter((p) => p.tier === 'soft').length;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatCard accent label="Enforced" value={enforced ? 'Yes' : 'No'} hint="checked before every tool call" />
        <StatCard label="Hard denies" value={String(hard)} hint="absolute" />
        <StatCard label="Soft gates" value={String(soft)} hint="human-in-the-loop" />
      </div>

      <p className="text-sm text-muted-foreground">
        The harness screens every file write and shell command against these rules before it runs, so a
        blocked action is refused with the matched rule id. These are decided in code, not editable here.
      </p>
      {note && <p className="text-xs italic text-muted-foreground">{note}</p>}

      {policies.length === 0 ? (
        <EmptyState title="No guardrails reported" hint="The enforcing policy module isn't reachable from this deploy." />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <ul className="divide-y divide-border">
            {policies.map((p) => {
              const isHard = p.tier === 'hard';
              return (
                <li key={p.rule_id} className="flex items-start justify-between gap-4 px-4 py-3">
                  <div className="flex min-w-0 items-start gap-3">
                    <span
                      className={cn(
                        'mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md',
                        isHard ? 'bg-destructive/10 text-destructive' : 'bg-warning/15 text-warning',
                      )}
                    >
                      {isHard ? <ShieldX className="size-4" /> : <ShieldAlert className="size-4" />}
                    </span>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <code className="font-mono text-xs text-foreground">{p.rule_id}</code>
                        <span
                          className={cn(
                            'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider',
                            isHard ? 'bg-destructive/10 text-destructive' : 'bg-muted text-muted-foreground',
                          )}
                        >
                          {p.tier}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">{p.summary}</p>
                    </div>
                  </div>
                  <span className="shrink-0 rounded border border-border px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                    {p.effect}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
