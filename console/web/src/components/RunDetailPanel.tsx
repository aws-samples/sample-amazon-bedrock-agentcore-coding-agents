import { Badge } from '@foxl/ui';
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Circle,
  FileCheck2,
  GitMerge,
  GitPullRequest,
  Loader2,
  ScrollText,
  ShieldCheck,
} from 'lucide-react';
import { AgentIcon } from './AgentIcon';
import type {
  GateRecord,
  RolePrEntry,
  RunDetail,
  WorkItem,
} from '../api';

function StatusIcon({ status }: { status: string }) {
  if (status === 'passed') {
    return <CheckCircle2 aria-hidden="true" className="size-4 text-emerald-600" />;
  }
  if (status === 'failed' || status === 'needs_human') {
    return <AlertCircle aria-hidden="true" className="size-4 text-destructive" />;
  }
  return (
    <Loader2
      aria-hidden="true"
      className="size-4 animate-spin text-muted-foreground motion-reduce:animate-none"
    />
  );
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

// Five stages, matching the engine: each pull request is checked, reviewed, and
// merged on its own. There is no combined tree and no final pull request above them.
const WORKFLOW = [
  { label: 'Shared Plan', icon: ScrollText },
  { label: 'Role PRs', icon: GitPullRequest },
  { label: 'Per-PR Check', icon: FileCheck2 },
  { label: 'Per-PR Review', icon: ShieldCheck },
  { label: 'Merged', icon: GitMerge },
] as const;

const SETTLED_PR_STATES = ['merged', 'awaiting_review'];

function workflowPosition(run: RunDetail, builders: WorkItem[]): number {
  let position = -1;
  if (run.integration_brief) position = 0;
  if (builders.some((item) => item.pr?.pr_url || item.state !== 'pending')) position = 1;
  if ((run.gate_history?.length ?? 0) > 0) position = 2;
  if (run.review?.state || (run.review?.panels?.length ?? 0) > 0) position = 3;
  if ((run.role_prs ?? []).some((row) => SETTLED_PR_STATES.includes(row.state))) position = 4;
  return position;
}

function failedPosition(run: RunDetail): number {
  if ((run.role_prs ?? []).some((row) => row.state === 'blocked')) return 4;
  const latest = run.gate_history?.at(-1);
  if (latest && !latest.passed && ['failed', 'needs_human'].includes(run.status)) return 2;
  if (run.review?.state === 'changes_requested'
      && ['failed', 'needs_human'].includes(run.status)) return 3;
  return -1;
}

function Workflow({ run, builders }: { run: RunDetail; builders: WorkItem[] }) {
  const current = workflowPosition(run, builders);
  const failed = failedPosition(run);
  const terminalPass = run.status === 'passed';

  return (
    <div className="overflow-x-auto pb-1">
      <ol
        className="grid min-w-[560px] grid-cols-5 sm:min-w-0"
        aria-label="Build workflow"
      >
        {WORKFLOW.map(({ label, icon: Icon }, index) => {
          const isFailed = failed === index;
          const isDone = index < current || (index === current && terminalPass);
          const isActive = index === current && !isDone && !isFailed;
          return (
            <li key={label} className="relative min-w-0 px-1 text-center">
              {index > 0 && (
                <span
                  aria-hidden="true"
                  className={`absolute left-0 right-1/2 top-3 h-px ${
                    isDone || isActive || isFailed ? 'bg-foreground/40' : 'bg-border'
                  }`}
                />
              )}
              {index < WORKFLOW.length - 1 && (
                <span
                  aria-hidden="true"
                  className={`absolute left-1/2 right-0 top-3 h-px ${
                    index < current ? 'bg-foreground/40' : 'bg-border'
                  }`}
                />
              )}
              <span
                className={`relative mx-auto flex size-6 items-center justify-center rounded-full border bg-background ${
                  isFailed
                    ? 'border-destructive text-destructive'
                    : isDone
                      ? 'border-foreground bg-foreground text-background'
                      : isActive
                        ? 'border-foreground text-foreground'
                        : 'border-border text-muted-foreground'
                }`}
              >
                {isDone
                  ? <Check aria-hidden="true" className="size-3.5" />
                  : isFailed
                    ? <AlertCircle aria-hidden="true" className="size-3.5" />
                    : isActive
                      ? <Loader2 aria-hidden="true" className="size-3.5 animate-spin motion-reduce:animate-none" />
                      : <Circle aria-hidden="true" className="size-2.5" />}
              </span>
              <span className="mt-1.5 flex min-h-8 items-start justify-center text-[11px] font-medium leading-4">
                {label}
              </span>
              <Icon aria-hidden="true" className="mx-auto mt-1 size-3 text-muted-foreground" />
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function WorkItemsTable({ builders }: { builders: WorkItem[] }) {
  if (builders.length === 0) return null;
  return (
    <section className="border-t border-border pt-3">
      <h3 className="text-xs font-semibold">Role Pull Requests</h3>
      <div className="mt-2 overflow-x-auto">
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
                  <Badge variant={statusVariant(item.merge_state ?? item.state)} className="text-[10px]">
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
    <section className="min-w-0 border-t border-border pt-3">
      <h3 className="text-xs font-semibold">Checks Run</h3>
      {gates.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">Waiting for the validator.</p>
      ) : (
        <ol className="mt-2 space-y-1.5">
          {gates.map((gate) => (
            <li key={`${gate.sequence}-${gate.stage}`} className="flex min-w-0 items-start gap-2 text-xs">
              {gate.passed
                ? <CheckCircle2 aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-emerald-600" />
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
    <section className="min-w-0 border-t border-border pt-3">
      <h3 className="text-xs font-semibold">Integrated Read-only Review</h3>
      {panels.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          {review?.state
            ? 'No integrated review was recorded for this run.'
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
                <Badge variant={statusVariant(panel.state)} className="text-[10px]">
                  {statusLabel(panel.state)}
                </Badge>
              </div>
              {(panel.reasons?.[0] || panel.note) && (
                <p className="mt-1 break-words pl-5.5 text-muted-foreground">
                  {panel.reasons?.[0] ?? panel.note}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function RolePullRequests({ rows }: { rows: RolePrEntry[] }) {
  return (
    <section className="min-w-0 border-t border-border pt-3">
      <h3 className="text-xs font-semibold">Pull Requests</h3>
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
                <Badge variant={statusVariant(row.state)} className="text-[10px]">
                  {row.state === 'awaiting_review' ? 'Ready to Merge' : statusLabel(row.state)}
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
  const done = run.status === 'passed';
  const failed = run.status === 'failed' || run.status === 'needs_human';

  return (
    <div className="space-y-4 py-1">
      <div className="flex min-w-0 items-center gap-2" role="status" aria-live="polite">
        <StatusIcon status={run.status} />
        <span className="text-sm font-medium">
          {done ? 'Passed' : failed ? 'Needs a Human' : 'Running'}
        </span>
        <code className="ml-auto truncate font-mono text-xs text-muted-foreground" translate="no">
          {run.run_id}
        </code>
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

      <Workflow run={run} builders={builders} />

      {brief && (
        <section className="border-t border-border pt-3">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="text-xs font-semibold">Shared Plan</h3>
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
          <section className="min-w-0 border-t border-border pt-3">
            <h3 className="text-xs font-semibold">Validator</h3>
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

      {run.next_action && failed && (
        <p role="status" className="border-t border-border pt-3 text-xs text-muted-foreground">
          {run.next_action}
        </p>
      )}
    </div>
  );
}
