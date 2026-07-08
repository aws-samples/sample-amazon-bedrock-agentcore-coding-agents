import { Badge, Card, CardContent } from '@foxl/ui';
import { CheckCircle2, AlertCircle, Loader2 } from 'lucide-react';
import { AgentIcon } from './AgentIcon';
import type { AgentEvent } from '../api';

// A run's status as an ICON (the design system reserves color dots; we use a
// glyph instead): a check when passed, an alert when it needs a human, a
// spinner while in flight.
function StatusIcon({ status }: { status: string }) {
  if (status === 'passed') return <CheckCircle2 className="size-4 text-muted-foreground" />;
  if (status === 'failed' || status === 'needs_human')
    return <AlertCircle className="size-4 text-destructive" />;
  return <Loader2 className="size-4 animate-spin text-muted-foreground" />;
}

const PHASES = ['admission', 'context_hydration', 'pre_flight', 'agent_execution', 'finalization'];

interface ProgressEntry {
  agent: string;
  role: string;
  state: string;
  latency_ms: number;
  tokens: number;
  cost_usd: number;
  note: string;
  engine: string;
}

interface RouteInfo {
  workflow_ref: string;
  version?: string;
  rule: string;
  agents: string[];
  usecase?: string;
  read_only?: boolean;
}

interface RunDetail {
  run_id: string;
  task: string;
  status: string;
  phase: string;
  created_at?: string;
  route?: RouteInfo | null;
  pr_url?: string | null;
  merge_state?: string | null;
  progress?: ProgressEntry[];
  events?: Array<{ ts: string; phase: string; msg: string; agent?: string }>;
  terminals?: Record<string, Array<{ cmd?: string; output?: string; text?: string }>>;
  // Per-role STRUCTURED agent events (the real tool_use/thinking/text stream).
  roleEvents?: Record<string, AgentEvent[]>;
  review?: { verdict?: string; notes?: string[] };
}

function statusVariant(status: string): 'default' | 'secondary' | 'outline' | 'destructive' {
  if (status === 'passed' || status === 'done' || status === 'completed') return 'secondary';
  if (status === 'failed') return 'destructive';
  if (status === 'running') return 'default';
  return 'outline';
}

export function RunDetailPanel({ run }: { run: RunDetail }) {
  const route = run.route;
  const phaseIdx = PHASES.indexOf(run.phase);
  const done = run.status === 'passed';
  const failed = run.status === 'failed' || run.status === 'needs_human';
  const progress: ProgressEntry[] = run.progress ?? [];
  const review = run.review;

  return (
    <Card>
      <CardContent className="space-y-4 py-4">
        {/* Status header: an icon, not a colored dot. */}
        <div className="flex items-center gap-2">
          <StatusIcon status={run.status} />
          <span className="text-sm font-medium">
            {done ? 'Passed' : failed ? 'Needs a human' : 'Running'}
          </span>
          <code className="ml-auto font-mono text-xs text-muted-foreground">{run.run_id}</code>
        </div>

        {/* Route */}
        {route && (
          <div className="space-y-1.5">
            <div className="text-xs text-muted-foreground">{route.rule}</div>
            <div className="flex flex-wrap items-center gap-1.5">
              <code className="font-mono text-xs">{route.workflow_ref}</code>
              {route.agents.map((a) => (
                <Badge key={a} variant="secondary" className="flex items-center gap-1 px-1.5 py-0.5">
                  <AgentIcon agentId={a} size={12} />
                  <span className="text-xs">{a}</span>
                </Badge>
              ))}
            </div>
          </div>
        )}

        {/* Phase chips */}
        <div className="flex flex-wrap gap-1.5">
          {PHASES.map((p, i) => (
            <Badge
              key={p}
              variant={
                i < phaseIdx || done
                  ? 'secondary'
                  : i === phaseIdx
                  ? 'default'
                  : 'outline'
              }
            >
              {p.replace(/_/g, ' ')}
            </Badge>
          ))}
        </div>

        {/* Agent progress */}
        {progress.length > 0 && (
          <div className="space-y-1.5">
            <div className="text-xs font-medium text-muted-foreground">Agents</div>
            <div className="space-y-1">
              {progress.map((p) => (
                <div key={p.agent} className="flex items-center gap-2 rounded-md bg-muted/40 px-2.5 py-1.5">
                  <AgentIcon agentId={p.agent} size={14} />
                  <span className="text-xs font-medium">{p.agent}</span>
                  <span className="text-xs text-muted-foreground">{p.role}</span>
                  <Badge variant={statusVariant(p.state)} className="ml-auto text-xs">
                    {p.state}
                  </Badge>
                  {p.tokens > 0 && (
                    <span className="text-xs text-muted-foreground">{p.tokens.toLocaleString()} tok</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Review gate */}
        {review && (
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted-foreground">Review gate</div>
            <div className="flex items-center gap-2">
              <Badge variant={review.verdict === 'LGTM: no changes needed' ? 'secondary' : 'outline'}>
                {review.verdict ?? 'pending'}
              </Badge>
            </div>
            {review.notes && review.notes.length > 0 && (
              <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                {review.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* PR link + merge outcome */}
        {(run.pr_url || run.merge_state) && (
          <div className="flex items-center gap-2 text-sm">
            {run.pr_url && (
              <a
                href={run.pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="underline"
              >
                View pull request
              </a>
            )}
            {run.merge_state && (
              <Badge
                variant={
                  run.merge_state === 'merged'
                    ? 'secondary'
                    : run.merge_state === 'human_review'
                      ? 'outline'
                      : 'destructive'
                }
                className="text-xs"
              >
                {run.merge_state === 'merged'
                  ? 'auto-merged'
                  : run.merge_state === 'human_review'
                    ? 'awaiting human merge'
                    : run.merge_state}
              </Badge>
            )}
          </div>
        )}
        {/* The per-role tool-call / reasoning stream lives in the transcript's
            compact activity rows (RunActivityRows): one home, no duplicate. */}
      </CardContent>
    </Card>
  );
}
