import { Badge } from '@foxl/ui';
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  FileCheck2,
  GitMerge,
  GitPullRequest,
  Loader2,
  ScrollText,
  ShieldCheck,
} from 'lucide-react';
import { AgentIcon } from './AgentIcon';
import { presentRunDecision, recordedPullRequests, type DecisionTone } from '../lib/runPresentation';
import type {
  GateRecord,
  RolePrEntry,
  RunDetail,
  WorkItem,
} from '../api';

function StatusIcon({ tone }: { tone: DecisionTone }) {
  if (tone === 'success') return <CheckCircle2 aria-hidden="true" className="size-5 text-success" />;
  if (tone === 'waiting') return <Clock3 aria-hidden="true" className="size-5 text-warning" />;
  if (tone === 'danger') return <AlertCircle aria-hidden="true" className="size-5 text-destructive" />;
  return <Loader2 aria-hidden="true" className="size-5 animate-spin text-signal motion-reduce:animate-none" />;
}

function statusVariant(status: string): 'default' | 'secondary' | 'outline' | 'destructive' {
  if (['passed', 'done', 'completed', 'merged', 'approved'].includes(status)) return 'secondary';
  if (['failed', 'error', 'blocked', 'needs_human', 'changes_requested'].includes(status)) {
    return 'destructive';
  }
  if (['running', 'working', 'merging'].includes(status)) return 'default';
  return 'outline';
}

function statusLabel(status: string): string {
  return status.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const integerFormat = new Intl.NumberFormat();

/** Record counts, not inferred green stages. Parallel PRs can settle differently. */
function Workflow({ run }: { run: RunDetail }) {
  const prs = recordedPullRequests(run).filter((row) => row.pr_url);
  const checks = run.gate_history?.length ?? 0;
  const decision = presentRunDecision(run);
  const records = [
    { label: 'Shared plan', icon: ScrollText, detail: run.integration_brief ? 'Recorded' : 'Not recorded yet', recorded: !!run.integration_brief },
    { label: 'Pull requests', icon: GitPullRequest, detail: `${prs.length} opened`, recorded: prs.length > 0 },
    { label: 'Executable checks', icon: FileCheck2, detail: `${checks} execution${checks === 1 ? '' : 's'}`, recorded: checks > 0 },
    { label: 'Independent review', icon: ShieldCheck, detail: run.review?.state ? statusLabel(run.review.state) : 'Not recorded yet', recorded: !!run.review?.state },
    { label: 'PR outcome', icon: GitMerge, detail: decision.title, recorded: run.status === 'passed' },
  ];
  return (
    <section aria-label="Build workflow">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Evidence trail</h3>
        <span className="text-xs text-muted-foreground">Each PR is checked independently</span>
      </div>
      <ol className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {records.map(({ label, icon: Icon, detail, recorded }) => (
          <li key={label} className="min-w-0 rounded-lg border border-border bg-background/60 p-3">
            <Icon aria-hidden="true" className={`mb-3 size-[18px] ${recorded ? 'text-signal' : 'text-muted-foreground'}`} />
            <div className="text-xs font-medium">{label}</div>
            <div className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function WorkItemsTable({ builders }: { builders: WorkItem[] }) {
  if (builders.length === 0) return null;
  return (
    <section className="evidence-surface">
      <h3 className="text-sm font-semibold">Role Pull Requests</h3>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[700px] text-left text-xs">
          <thead className="text-muted-foreground">
            <tr>
              <th scope="col" className="pb-1.5 font-medium">Role</th>
              <th scope="col" className="pb-1.5 font-medium">Work ID</th>
              <th scope="col" className="pb-1.5 font-medium">Pull Request</th>
              <th scope="col" className="pb-1.5 font-medium">Activity</th>
              <th scope="col" className="pb-1.5 text-right font-medium">State</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {builders.map((item) => (
              <tr key={item.work_id}>
                <td className="py-2 pr-3">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <AgentIcon agentId={item.agent} size={14} />
                    <span className="truncate font-medium">{item.capability}</span>
                  </span>
                </td>
                <td className="py-2 pr-3 font-mono text-muted-foreground" translate="no">
                  <span className="block">{item.work_id}</span>
                  {item.worktree_branch && (
                    <span className="block text-[10px]">{item.worktree_branch}</span>
                  )}
                </td>
                <td className="py-2 pr-3">
                  {item.pr?.pr_url ? (
                    <a
                      href={item.pr.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-medium underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      Open Role PR
                    </a>
                  ) : (
                    <span className="text-muted-foreground">Not opened</span>
                  )}
                </td>
                <td className="py-2 pr-3">
                  <span className="block font-medium">
                    {item.attempt ?? 0} {(item.attempt ?? 0) === 1 ? 'turn' : 'turns'}
                  </span>
                  {(item.dependency_refreshes ?? 0) > 0 && (
                    <span className="block text-[10px] text-muted-foreground">
                      {item.dependency_refreshes}{' '}
                      {(item.dependency_refreshes ?? 0) === 1 ? 'update' : 'updates'} after an earlier merge
                    </span>
                  )}
                </td>
                <td className="py-2 text-right">
                  <Badge variant={statusVariant(item.merge_state ?? item.state)} className="text-[11px]">
                    {statusLabel(item.merge_state ?? item.state)}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function GateHistory({ gates }: { gates: GateRecord[] }) {
  return (
    <section className="evidence-surface min-w-0">
      <h3 className="text-sm font-semibold">Checks Run</h3>
      {gates.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">Waiting for the validator.</p>
      ) : (
        <ol className="mt-2 space-y-1.5">
          {gates.map((gate) => (
            <li key={`${gate.sequence}-${gate.stage}`} className="flex min-w-0 items-start gap-2 text-xs">
              {gate.passed
                ? <CheckCircle2 aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-success" />
                : <AlertCircle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-destructive" />}
              <span className="min-w-0 flex-1">
                <span className="font-medium">{gate.stage}</span>
                {gate.summary && (
                  <span className="block break-words text-muted-foreground">{gate.summary}</span>
                )}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function IntegratedReview({ review }: { review: RunDetail['review'] }) {
  const panels = review?.panels ?? [];
  return (
    <section className="evidence-surface min-w-0">
      <h3 className="text-sm font-semibold">Independent review</h3>
      {panels.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          {review?.state
            ? 'No review panels were recorded for this run.'
            : 'Waiting for the adversarial and design review lenses.'}
        </p>
      ) : (
        <ol className="mt-2 space-y-2">
          {panels.map((panel) => (
            <li key={panel.name} className="min-w-0 text-xs">
              <div className="flex min-w-0 items-center gap-2">
                <ShieldCheck aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate font-medium">
                  {panel.label ?? statusLabel(panel.name)}
                </span>
                <Badge variant={statusVariant(panel.state)} className="text-[11px]">
                  {statusLabel(panel.state)}
                </Badge>
              </div>
              {(panel.reasons?.length ?? 0) > 0 ? (
                <ul className="mt-2 list-disc space-y-1 pl-5 text-muted-foreground">
                  {panel.reasons!.map((reason, index) => <li key={index} className="break-words leading-5">{reason}</li>)}
                </ul>
              ) : panel.note ? (
                <p className="mt-2 break-words text-muted-foreground">{panel.note}</p>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function RolePullRequests({ rows }: { rows: RolePrEntry[] }) {
  return (
    <section className="evidence-surface min-w-0">
      <h3 className="text-sm font-semibold">Pull Requests</h3>
      {rows.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">Waiting for the checks and reviews.</p>
      ) : (
        <ul className="mt-2 space-y-1.5">
          {rows.map((row) => (
            <li key={row.work_id} className="min-w-0 text-xs">
              <div className="flex min-w-0 items-center gap-2">
                {row.pr_url ? (
                  <a
                    href={row.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="min-w-0 flex-1 truncate font-medium underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {row.role || row.agent}
                  </a>
                ) : (
                  <span className="min-w-0 flex-1 truncate">{row.role || row.agent}</span>
                )}
                <Badge variant={statusVariant(row.state)} className="text-[11px]">
                  {row.state === 'awaiting_review' ? 'Awaiting your review' : statusLabel(row.state)}
                </Badge>
              </div>
              {row.error && (
                <p className="mt-0.5 break-words text-[10px] text-destructive">{row.error}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function RunDetailPanel({ run }: { run: RunDetail }) {
  const route = run.route;
  const progress = run.progress ?? [];
  const items = Object.values(run.work_items ?? {});
  const builders = items.filter((item) => item.kind === 'builder');
  const checker = items.find((item) => item.kind === 'checker');
  const gates = run.gate_history ?? [];
  const rolePrs = run.role_prs ?? [];
  const brief = run.integration_brief;
  const decision = presentRunDecision(run);

  return (
    <div className="space-y-4 py-1">
      <div className="flex flex-wrap items-start gap-3 rounded-xl border border-border bg-card p-4" role="status" aria-live="polite">
        <div className="mt-0.5"><StatusIcon tone={decision.tone} /></div>
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold">{decision.title}</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{decision.detail}</p>
          {run.next_action && <p className="mt-3 text-sm leading-6"><span className="font-medium">Next: </span>{run.next_action}</p>}
        </div>
        <code className="max-w-full break-all font-mono text-[11px] text-muted-foreground" translate="no">{run.run_id}</code>
      </div>

      {route && (
        <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
          <span className="break-words text-muted-foreground">{route.rule}</span>
          {route.agents.map((agent) => (
            <Badge key={agent} variant="secondary" className="flex items-center gap-1">
              <AgentIcon agentId={agent} size={12} />
              <span>{agent}</span>
            </Badge>
          ))}
        </div>
      )}

      <Workflow run={run} />

      {brief && (
        <section className="evidence-surface">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="text-sm font-semibold">Shared Plan</h3>
            {brief.merge_order?.length ? (
              <code className="min-w-0 truncate text-[10px] text-muted-foreground" translate="no">
                {brief.merge_order.join(' -> ')}
              </code>
            ) : null}
          </div>
          {brief.summary && <p className="mt-1.5 break-words text-xs">{brief.summary}</p>}
          {(brief.shared_contract?.length ?? 0) > 0 && (
            <ul className="mt-1.5 list-disc space-y-0.5 break-words pl-4 text-xs text-muted-foreground">
              {brief.shared_contract?.map((row) => <li key={row}>{row}</li>)}
            </ul>
          )}
        </section>
      )}

      <WorkItemsTable builders={builders} />

      <div className="grid gap-4 md:grid-cols-2">
        <GateHistory gates={gates} />
        <IntegratedReview review={run.review} />
        <RolePullRequests rows={rolePrs} />
        {checker && (
          <section className="evidence-surface min-w-0">
            <h3 className="text-sm font-semibold">Validator</h3>
            <p className="mt-2 break-all text-[10px] text-muted-foreground">
              <code translate="no">{checker.work_id}</code> authored one executable
              check per pull request. Its real exit code is that pull request's gate.
            </p>
          </section>
        )}
      </div>

      {progress.some((entry) => entry.tokens > 0) && (
        <p className="border-t border-border pt-3 text-[10px] text-muted-foreground">
          {integerFormat.format(progress.reduce((sum, entry) => sum + entry.tokens, 0))} tokens reported
        </p>
      )}


    </div>
  );
}
