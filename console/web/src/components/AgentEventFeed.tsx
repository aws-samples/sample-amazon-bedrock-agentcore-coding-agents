import { useState } from 'react';
import {
  Collapsible, CollapsibleTrigger, CollapsibleContent, Shimmer,
} from '@foxl/ui';
import { Brain, ChevronRight, Wrench, Bot, Check, X } from 'lucide-react';
import { PulseDot } from './Motion';
import type { AgentEvent } from '../api';

/**
 * Renders one role's REAL agent event stream: the text / extended-reasoning /
 * tool-call / tool-result blocks the CLI actually emitted, in arrival order, so
 * you watch the genuine interleave (think -> call a tool -> read the result ->
 * think -> ...). Nothing here is synthesized; every block came from the agent.
 *
 * Original, workshop-simplified. The visual language (a collapsible reasoning
 * block with a shimmer while live, a wrench-headed tool chip with collapsible
 * input/output) follows a common chat-UI pattern, reimplemented from scratch on
 * the vendored shadcn primitives.
 */
export function AgentEventFeed({ events, live }: { events: AgentEvent[]; live: boolean }) {
  if (events.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border py-6 text-center text-xs text-muted-foreground">
        {live ? "Waiting for the agent's first step…" : 'No events recorded for this role.'}
      </div>
    );
  }
  // The last event is still "in flight" only while the run is live.
  const lastIdx = events.length - 1;
  return (
    <div className="space-y-1.5">
      {events.map((e, i) => {
        const key = `${e.kind}-${i}`;
        if (e.kind === 'thinking') {
          return <ThinkingBlock key={key} text={e.text ?? ''} live={live && i === lastIdx} />;
        }
        if (e.kind === 'tool_use') {
          // Pair this call with the matching result (by id) that follows it.
          const result = events
            .slice(i + 1)
            .find((r) => r.kind === 'tool_result' && r.id && r.id === e.id);
          return <ToolBlock key={key} call={e} result={result} live={live && i === lastIdx} />;
        }
        if (e.kind === 'tool_result') {
          // Rendered inline with its tool_use above; skip the standalone copy.
          return null;
        }
        // assistant prose
        return (
          <p key={key} className="whitespace-pre-wrap text-sm leading-relaxed text-foreground">
            {e.text}
          </p>
        );
      })}
    </div>
  );
}

// Extended reasoning: a collapsible block that SHIMMERS its label while the
// thought is still streaming, then settles to a static "Thought it through" once
// the next event arrives. Closed by default unless live.
function ThinkingBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(live);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="group flex w-full items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">
        <ChevronRight className={`size-3 transition-transform ${open ? 'rotate-90' : ''}`} />
        <Brain className="size-3.5" />
        {live ? <Shimmer className="text-xs" duration={1}>Thinking</Shimmer> : <span>Thought it through</span>}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="ml-4 mt-1 whitespace-pre-wrap border-l border-border pl-3 text-xs leading-relaxed text-muted-foreground">
          {text}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

// A tool call: a chip with the tool name + status, collapsible to show the real
// input and the result the agent got back. The `Task` tool is a subagent spawn,
// so it gets a distinct bot icon and label.
function ToolBlock({ call, result, live }: { call: AgentEvent; result?: AgentEvent; live: boolean }) {
  const isSubagent = call.name === 'Task';
  const [open, setOpen] = useState(false);
  const pending = !result && live;
  const errored = result?.is_error;
  const Icon = isSubagent ? Bot : Wrench;
  const inputStr = call.input ? compactInput(call.input) : '';

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="group flex w-full items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1 text-left text-xs transition-colors hover:bg-muted/70">
        <ChevronRight className={`size-3 shrink-0 text-muted-foreground transition-transform ${open ? 'rotate-90' : ''}`} />
        <Icon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-mono font-medium">{isSubagent ? 'subagent' : call.name}</span>
        {inputStr && (
          <span className="truncate font-mono text-[11px] text-muted-foreground">{inputStr}</span>
        )}
        <StatusPill pending={pending} errored={errored} className="ml-auto" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="ml-4 mt-1 space-y-1 border-l border-border pl-3">
          {call.input && Object.keys(call.input).length > 0 && (
            <pre className="overflow-x-auto rounded bg-muted/60 p-1.5 font-mono text-[11px] text-muted-foreground">
              {JSON.stringify(call.input, null, 2)}
            </pre>
          )}
          {result?.text && (
            <pre className={`max-h-40 overflow-auto whitespace-pre-wrap rounded p-1.5 font-mono text-[11px] ${errored ? 'bg-destructive/10 text-destructive' : 'bg-muted/60 text-muted-foreground'}`}>
              {result.text}
            </pre>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function StatusPill({ pending, errored, className = '' }: { pending: boolean; errored?: boolean; className?: string }) {
  if (pending) {
    return (
      <span className={`flex items-center gap-1.5 text-[10px] text-warning ${className}`}>
        <PulseDot live tone="info" size={6} className="[&_*]:!bg-current" /> running
      </span>
    );
  }
  if (errored) {
    return (
      <span className={`flex items-center gap-1 text-[10px] text-destructive ${className}`}>
        <X className="size-3" /> error
      </span>
    );
  }
  return (
    <span className={`flex items-center gap-1 text-[10px] text-muted-foreground ${className}`}>
      <Check className="size-3" /> done
    </span>
  );
}

// A one-line summary of a tool input for the chip (full JSON is in the body).
function compactInput(input: Record<string, unknown>): string {
  const v = input.file_path ?? input.path ?? input.command ?? input.description ?? input.pattern;
  if (typeof v === 'string') return v.length > 64 ? v.slice(0, 62) + '…' : v;
  return '';
}
