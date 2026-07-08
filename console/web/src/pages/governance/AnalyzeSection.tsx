import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { cn } from '@foxl/ui';
import { Sparkles, Send, Square, RotateCcw, Download, Wrench, Check, Loader2 } from 'lucide-react';
import {
  streamChat, getDashboard, getCostBreakdown, listSessions,
  type ChatEvent, type Dashboard, type CostBreakdown, type SessionRow,
} from '../../api';
import { WorkingDots } from '../../components/Motion';
import { fmtUsd, fmtSeconds } from '../../shared';

type ToolCall = { name: string; status: 'running' | 'done' };
type Msg = { id: string; role: 'user' | 'assistant'; text: string; tools: ToolCall[] };

// Friendly labels for the orchestrator's own tool calls, so a chip reads as a
// real action ("Routing the task") rather than a bare function name.
const TOOL_LABEL: Record<string, string> = {
  route_task: 'Routing the task',
  dispatch_backend: 'Dispatching backend',
  dispatch_frontend: 'Dispatching frontend',
  dispatch_validator: 'Dispatching validator',
  run_build: 'Running the build',
  run_status: 'Checking run status',
};

const newId = () => `m_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;

const SUGGESTED = [
  'Summarize this fleet\'s cost and where the spend concentrates.',
  'Which agent is the most expensive, and is the split reasonable?',
  'Are there any running sessions I should consider stopping?',
  'Explain the governance posture: identity, cost attribution, and guardrails.',
];

/**
 * The AI Analyze surface: ask the orchestrator's own model about the live
 * governance snapshot. Each question is grounded with the real metrics (the
 * dashboard, the per-agent cost split, the session count) so the model reasons
 * over actual numbers, not guesses. A pure analysis turn answers as a chatbot
 * and never dispatches a build (the chat reveals a run only when the model calls
 * a dispatch tool). The transcript exports to markdown for a report.
 */
export function AnalyzeSection() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [snapshot, setSnapshot] = useState<{ dash: Dashboard; cost: CostBreakdown; sessions: SessionRow[] } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const convId = useRef(`gov_${Date.now().toString(36)}`);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Pull a fresh real snapshot to ground the model. Refetched per mount so the
  // analysis always reflects the current ledger.
  useEffect(() => {
    let live = true;
    Promise.all([getDashboard(), getCostBreakdown('agent'), listSessions()])
      .then(([dash, cost, sessions]) => live && setSnapshot({ dash, cost, sessions }))
      .catch(() => {/* the section still works; the model just won't be grounded */});
    return () => { live = false; };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  // A compact, factual context block prepended to the user's question. Kept
  // small and literal; it's the real snapshot, not a narrative.
  const groundingFor = useCallback((question: string) => {
    if (!snapshot) return question;
    const { dash, cost, sessions } = snapshot;
    const costLines = Object.entries(cost.breakdown)
      .map(([a, c]) => `  - ${a}: ${fmtUsd(c)}`)
      .join('\n') || '  - (none recorded)';
    const active = sessions.filter((s) => s.claude_running).length;
    return [
      'You are reviewing a live AgentCore coding-agent fleet. Here is its current governance snapshot (real telemetry):',
      `- Active sessions: ${dash.active_sessions} of ${dash.runs_total} total`,
      `- Fleet p95 latency: ${fmtSeconds(dash.p95_latency_ms)}`,
      `- Running sessions right now: ${active}`,
      'Cost by agent (USD, attribution only, no winner):',
      costLines,
      '',
      `Answer this concisely, using only the numbers above. Do not start a build.\n\nQuestion: ${question}`,
    ].join('\n');
  }, [snapshot]);

  const send = useCallback(async (raw: string) => {
    const question = raw.trim();
    if (!question || streaming) return;
    setDraft('');

    const asstId = newId();
    setMessages((prev) => [
      ...prev,
      { id: newId(), role: 'user', text: question, tools: [] },
      { id: asstId, role: 'assistant', text: '', tools: [] },
    ]);
    setStreaming(true);

    const patch = (fn: (m: Msg) => Msg) =>
      setMessages((prev) => prev.map((m) => (m.id === asstId ? fn(m) : m)));

    const ac = new AbortController();
    abortRef.current = ac;
    // The orchestrator stream carries more than prose: a `tool` event (running →
    // done) when the model calls one of its tools, and a `reasoning` trace. We
    // surface tool calls as chips so the chat shows the real action the model
    // took, exactly the events the SSE emits, nothing invented.
    const onEvent = (ev: ChatEvent) => {
      if (ev.type === 'text') {
        patch((m) => ({ ...m, text: m.text + ev.text }));
      } else if (ev.type === 'tool') {
        patch((m) => {
          const tools = [...m.tools];
          if (ev.status === 'done') {
            // Settle the most recent running call of this name.
            for (let i = tools.length - 1; i >= 0; i--) {
              const tc = tools[i];
              if (tc && tc.name === ev.name && tc.status === 'running') {
                tools[i] = { name: tc.name, status: 'done' };
                return { ...m, tools };
              }
            }
            return { ...m, tools: [...tools, { name: ev.name, status: 'done' }] };
          }
          return { ...m, tools: [...tools, { name: ev.name, status: 'running' }] };
        });
      } else if (ev.type === 'error') {
        patch((m) => ({ ...m, text: `${m.text}\n\nError: ${ev.error}` }));
      }
    };

    try {
      await streamChat({ prompt: groundingFor(question), conversationId: convId.current }, onEvent, ac.signal);
    } catch (e) {
      if (!ac.signal.aborted) {
        patch((m) => ({ ...m, text: `${m.text}\n\nError: ${e instanceof Error ? e.message : 'analysis failed'}`.trim() }));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [streaming, groundingFor]);

  const stop = useCallback(() => { abortRef.current?.abort(); setStreaming(false); }, []);
  const reset = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
    setMessages([]);
    convId.current = `gov_${Date.now().toString(36)}`;
  }, []);

  const exportMarkdown = useCallback(() => {
    if (!messages.length) return;
    const lines: string[] = ['# Governance: AI Analysis', ''];
    for (const m of messages) {
      if (m.role === 'user') lines.push('---', '', `**Q.** ${m.text.split('Question: ').pop() ?? m.text}`, '');
      else if (m.text) lines.push(m.text, '');
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'governance-analysis.md';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [messages]);

  const empty = messages.length === 0;

  return (
    <div className="flex h-[min(70vh,640px)] flex-col rounded-xl border border-border bg-card shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2 text-sm">
          <Sparkles className="size-4 text-muted-foreground" />
          <span className="font-medium text-foreground">Ask about this fleet</span>
          <span className="text-xs text-muted-foreground">· grounded in the live snapshot</span>
        </div>
        <div className="flex items-center gap-1.5">
          {messages.length > 0 && (
            <>
              <button onClick={exportMarkdown} title="Export to markdown" className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground">
                <Download className="size-3.5" /> Export
              </button>
              <button onClick={reset} title="New analysis" className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground">
                <RotateCcw className="size-3.5" /> Reset
              </button>
            </>
          )}
        </div>
      </div>

      {/* Transcript */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4">
        {empty ? (
          <div className="mx-auto flex h-full max-w-xl flex-col items-center justify-center text-center">
            <p className="text-sm text-muted-foreground">
              Ask the orchestrator model to read this fleet's cost, sessions, and posture. It answers
              over the real numbers. A question never starts a build.
            </p>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              {SUGGESTED.map((p, i) => (
                <button
                  key={p}
                  onClick={() => send(p)}
                  className="animate-enter-up rounded-md bg-muted px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
                  style={{ animationDelay: `${i * 50}ms` }}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {messages.map((m, i) => (
              <div key={m.id} className={cn('flex', m.role === 'user' ? 'justify-end' : 'justify-start')}>
                {m.role === 'user' ? (
                  <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
                    {/* Show only the user's question, not the grounding preamble. */}
                    {m.text.includes('Question: ') ? m.text.split('Question: ').pop() : m.text}
                  </div>
                ) : (
                  <div className="max-w-[85%] rounded-2xl rounded-bl-sm bg-muted px-4 py-2.5 text-sm">
                    {m.tools.length > 0 && (
                      <div className="mb-2 flex flex-wrap gap-1.5">
                        {m.tools.map((tc, j) => (
                          <span
                            key={`${tc.name}-${j}`}
                            className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5 text-[10px] font-medium text-muted-foreground"
                          >
                            {tc.status === 'running' ? (
                              <Loader2 className="size-3 animate-spin" />
                            ) : (
                              <Check className="size-3 text-success" />
                            )}
                            {TOOL_LABEL[tc.name] ?? tc.name}
                          </span>
                        ))}
                      </div>
                    )}
                    {m.text.length === 0 && streaming && i === messages.length - 1 ? (
                      m.tools.length === 0 ? <WorkingDots size={5} /> : <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"><Wrench className="size-3" /> working…</span>
                    ) : (
                      <div className={cn('prose-chat', streaming && i === messages.length - 1 && 'md-stream')}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>{m.text}</ReactMarkdown>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Composer */}
      <div className="border-t border-border p-3">
        <div className="flex items-end gap-2 rounded-xl border border-border bg-background p-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(draft); }
            }}
            rows={1}
            placeholder="Ask about cost, sessions, identity, or guardrails…"
            className="max-h-32 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-muted-foreground"
          />
          {streaming ? (
            <button onClick={stop} className="flex size-8 items-center justify-center rounded-lg bg-muted text-muted-foreground hover:text-foreground" title="Stop">
              <Square className="size-3.5" />
            </button>
          ) : (
            <button
              onClick={() => send(draft)}
              disabled={!draft.trim()}
              className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground disabled:opacity-40"
              title="Send"
            >
              <Send className="size-3.5" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
