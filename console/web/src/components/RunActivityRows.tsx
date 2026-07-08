import { useState } from 'react';
import {
  Collapsible, CollapsibleTrigger, CollapsibleContent, cn,
} from '@foxl/ui';
import {
  ChevronRight, Route, Server, CheckCircle2, AlertCircle,
} from 'lucide-react';
import { AgentIcon } from './AgentIcon';
import { AgentEventFeed } from './AgentEventFeed';
import { WorkingDots } from './Motion';
import type { AgentEvent } from '../api';

// What the orchestrator routed the task to: the REAL workflow ref + the rule
// that picked it + the agents it dispatches. Mirrors the engine's RunRoute.
interface RouteInfo {
  workflow_ref: string;
  rule: string;
  agents: string[];
  usecase?: string;
}

// One role's REAL execution record off the run (engine `public_progress`): which
// agent, the role it plays, and its live state.
interface ProgressEntry {
  agent: string;
  role: string;
  state: string;
  tokens?: number;
  note?: string;
}

const TERMINAL_STATES = ['passed', 'done', 'completed', 'failed', 'error', 'needs_human'];

/**
 * The orchestrator's WORK for one run, rendered as the reference's compact
 * activity rows: a "route_task" row (the real routing decision) and one
 * "dispatching <role>" row per dispatched agent. Each role row expands to the
 * agent's REAL tool-call / reasoning stream (AgentEventFeed). Every row is driven
 * by a real run poll (route + progress + per-role events); nothing is invented,
 * and when there is no data yet a role simply shows "waiting".
 *
 * Original, workshop-simplified: the row visual language follows a common
 * coding-agent console pattern, reimplemented on the vendored shadcn primitives.
 */
export function RunActivityRows({
  route, progress, roleEvents, live,
}: {
  route?: RouteInfo | null;
  progress?: ProgressEntry[];
  roleEvents?: Record<string, AgentEvent[]>;
  live: boolean;
}) {
  const events = roleEvents ?? {};
  const entries = progress ?? [];
  // Drive the dispatch rows off the real progress; if progress hasn't populated
  // yet, fall back to the routed agent list so a row appears as soon as routing
  // is known (state "waiting"), never an invented agent.
  const dispatched: ProgressEntry[] = entries.length
    ? entries
    : (route?.agents ?? []).map((a) => ({ agent: a, role: '', state: 'waiting' }));

  if (!route && dispatched.length === 0) return null;

  return (
    <div className="space-y-1">
      {route && (
        <div className="animate-enter-up">
          <RouteRow route={route} />
        </div>
      )}
      {dispatched.map((p, i) => (
        <div key={p.agent} className="animate-enter-up" style={{ animationDelay: `${(i + 1) * 50}ms` }}>
          <DispatchRow
            entry={p}
            events={events[p.agent] ?? []}
            live={live}
          />
        </div>
      ))}
    </div>
  );
}

// The "route_task" row: the orchestrator's real routing decision. Expands to the
// rule that fired and the workflow it selected.
function RouteRow({ route }: { route: RouteInfo }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="group flex w-full items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1 text-left text-xs transition-colors hover:bg-muted/70">
        <ChevronRight className={cn('size-3 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')} />
        <Route className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-mono font-medium">route_task</span>
        <span className="truncate font-mono text-[11px] text-muted-foreground">{route.workflow_ref}</span>
        <span className="ml-auto flex items-center gap-1 text-[10px] text-muted-foreground">
          <CheckCircle2 className="size-3" /> routed
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="ml-4 mt-1 space-y-1 border-l border-border pl-3 text-xs text-muted-foreground">
          <div>{route.rule}</div>
          <div className="flex flex-wrap items-center gap-1.5">
            {route.agents.map((a) => (
              <span key={a} className="flex items-center gap-1 rounded bg-muted/60 px-1.5 py-0.5">
                <AgentIcon agentId={a} size={12} />
                <span className="font-mono text-[11px]">{a}</span>
              </span>
            ))}
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

// A "dispatching <agent>" row: one dispatched role. Expands to the agent's REAL
// tool-call / reasoning stream. Closed by default; opens to inspect the work.
function DispatchRow({
  entry, events, live,
}: {
  entry: ProgressEntry;
  events: AgentEvent[];
  live: boolean;
}) {
  const [open, setOpen] = useState(false);
  const roleLive = live && !TERMINAL_STATES.includes(entry.state);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="group flex w-full items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1 text-left text-xs transition-colors hover:bg-muted/70">
        <ChevronRight className={cn('size-3 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')} />
        <Server className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-medium">dispatching</span>
        <span className="flex items-center gap-1">
          <AgentIcon agentId={entry.agent} size={12} />
          <span className="font-mono text-[11px]">{entry.agent}</span>
        </span>
        {entry.role && <span className="truncate text-[11px] text-muted-foreground">{entry.role}</span>}
        <RoleStatus state={entry.state} live={roleLive} className="ml-auto" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="ml-4 mt-1 max-h-72 overflow-y-auto border-l border-border pl-3">
          <AgentEventFeed events={events} live={roleLive} />
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function RoleStatus({ state, live, className = '' }: { state: string; live: boolean; className?: string }) {
  if (live) {
    return (
      <span className={cn('flex items-center gap-1.5 text-[10px] text-muted-foreground', className)}>
        {state && <span>{state}</span>}
        <WorkingDots size={3} />
      </span>
    );
  }
  const failed = state === 'failed' || state === 'error';
  return (
    <span className={cn('flex items-center gap-1 text-[10px]', failed ? 'text-destructive' : 'text-muted-foreground', className)}>
      {failed ? <AlertCircle className="size-3" /> : <CheckCircle2 className="size-3" />}
      {state || 'done'}
    </span>
  );
}
