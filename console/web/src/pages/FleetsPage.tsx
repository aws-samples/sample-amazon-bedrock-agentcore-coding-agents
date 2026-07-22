import { useEffect, useRef, useState, useCallback, useMemo, memo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { newConversationId, touchChat, loadTranscript, saveTranscript } from '../hooks/useChats';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
// rehype-highlight registers lowlight's "common" set (about 37 languages: python,
// ts/js, json, bash, yaml, and more), which covers everything the orchestrator and the
// coding agents emit. It rides in the lazy `markdown` chunk that only loads when
// you open the chat, so it never weighs on the landing route.
import {
  PromptInput, PromptInputForm, PromptInputTextarea, PromptInputActions,
  PromptInputLeftActions, PromptInputRightActions, PromptInputSubmit,
  PromptInputAttachButton,
} from '@foxl/code/components/chat/prompt-input';
import {
  Popover, PopoverTrigger, PopoverContent, cn,
  Collapsible, CollapsibleTrigger, CollapsibleContent,
  Shimmer,
  Tabs, TabsList, TabsTrigger, TabsContent,
  Input, Button,
} from '@foxl/ui';
import {
  CheckCircle2, AlertCircle, GitPullRequest, ChevronDown, Check, X, FileText,
  ChevronRight, Loader2,
  Route, Server, Play, ListChecks, Wrench, Activity,
  Brain, GitBranch, MessageSquarePlus,
} from 'lucide-react';
import {
  streamChat, getRun, getRunTerminals, getRunResult, getRunDiff, listModels, getGithubStatus,
  listSuggestions, getRuntimes, wireRuntime,
  type ChatEvent, type RunSummary, type RunResult, type RunDiff, type AgentEvent, type ModelOption,
  type RuntimeStatus,
} from '../api';
import { RunDetailPanel } from '../components/RunDetailPanel';
import { RunActivityRows } from '../components/RunActivityRows';
import { WorkingDots, PulseDot } from '../components/Motion';
import { useAutoScroll } from '../hooks/useAutoScroll';
import { Terminal, type TerminalHandle } from '../components/Terminal';
import { subscribeOutput, getBuffer } from '../hooks/useSessionStore';

// Human-readable label for a run's CURRENT phase: a straight rename of the real
// engine phase id (run.phase), not invented narration.
const PHASE_LABEL: Record<string, string> = {
  admission: 'routing the task',
  context_hydration: 'reading the module and steering files',
  pre_flight: 'running readiness checks',
  agent_execution: 'dispatching the agents',
  finalization: 'running the pytest gate and review',
};

// Fallback openers shown until the live registry-derived suggestions load. Short
// and capped at 3 to match the backend (chips must not clip).
const PRESETS = [
  'Convert the cost analyzer module to MCP + UI',
  'Build the Critter Lab full-stack app',
  'Patch the backend MCP server',
];

const TERMINAL_STATUSES = ['passed', 'failed', 'needs_human'];

// Where the chosen orchestrator model is remembered across reloads.
const MODEL_STORAGE_KEY = 'agentcore.console.orchestrator-model';

// The ORCHESTRATOR'S model. Fetched live from /api/orchestrator/models; this is the
// first-paint placeholder until that fetch resolves.
const FALLBACK_MODEL: ModelOption = {
  id: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6',
  hint: 'fast and balanced, the default brain',
};

// newConversationId + the chat-list store live in useChats (shared with the
// sidebar ChatList). Imported below.

// Friendly labels for the orchestrator's own tool calls (chat-level tools, not
// per-role coding-agent tools). Every name is a real tool name the orchestrator
// emits, never invented.
const TOOL_LABEL: Record<string, string> = {
  route_task:         'Routing the task',
  dispatch_backend:   'Dispatching backend (Claude Code)',
  dispatch_frontend:  'Dispatching frontend (opencode)',
  dispatch_validator: 'Dispatching validator (Claude Code)',
  run_build:          'Running the full build',
  run_status:         'Checking run status',
};

const TOOL_ICON: Record<string, React.ElementType> = {
  route_task:         Route,
  dispatch_backend:   Server,
  dispatch_frontend:  Server,
  dispatch_validator: ListChecks,
  run_build:          Play,
  run_status:         Activity,
};

// One item in the chat transcript. Tool/reasoning items are injected inline as
// stream events arrive; each is its own list entry so they render in arrival
// order between prose bubbles. The `stepLast` flag is computed at render time
// (whether the next sibling in the list is also a stepper item) so the foxl
// vertical connector line is drawn only between consecutive steps.
type ChatItem =
  | { kind: 'user';      text: string }
  | { kind: 'assistant'; text: string }
  | { kind: 'tool';      name: string; status: 'running' | 'done' }
  | { kind: 'reasoning'; text: string }
  | { kind: 'run';       runId: string; runKind: string };

// A stepper item is a tool call or a reasoning block; consecutive ones are
// connected by the vertical line.
function isStepperItem(it: ChatItem): it is Extract<ChatItem, { kind: 'tool' | 'reasoning' }> {
  return it.kind === 'tool' || it.kind === 'reasoning';
}

export function FleetsPage() {
  // /fleets/:runId deep-links a run; /fleets/c/:chatId selects a sub-chat.
  const { runId: deepLinkRunId, chatId } = useParams<{ runId?: string; chatId?: string }>();
  const nav = useNavigate();

  // The active conversation is the URL's chatId when present, else a fresh id.
  // Keep it in state so a brand-new chat (no chatId yet) has a stable id until
  // the first message routes to /fleets/c/<id>.
  const [conversationId, setConversationId] = useState(() => chatId || newConversationId());
  const [items, setItems] = useState<ChatItem[]>([]);
  const [draft, setDraft] = useState('');
  const [models, setModels] = useState<ModelOption[]>([FALLBACK_MODEL]);
  // Persist the chosen orchestrator model across reloads. Seed from localStorage
  // so a refresh keeps the selection; the server default only applies when the
  // user has never picked one (see the fetch effect below).
  const [model, setModelState] = useState(
    () => localStorage.getItem(MODEL_STORAGE_KEY) || FALLBACK_MODEL.id);
  const setModel = useCallback((id: string) => {
    setModelState(id);
    try { localStorage.setItem(MODEL_STORAGE_KEY, id); } catch { /* private mode */ }
  }, []);
  const [attachments, setAttachments] = useState<{ name: string; text: string }[]>([]);
  const [streaming, setStreaming] = useState(false);
  // Orchestrator wiring state: fetched once on mount, then polled.
  const [runtimes, setRuntimes] = useState<RuntimeStatus | null>(null);
  const [orchWireDraft, setOrchWireDraft] = useState('');
  const [orchWiring, setOrchWiring] = useState(false);
  const [orchWireError, setOrchWireError] = useState('');

  const refreshRuntimes = useCallback(() => {
    getRuntimes().then(setRuntimes).catch(() => {});
  }, []);

  useEffect(() => {
    refreshRuntimes();
    const t = setInterval(refreshRuntimes, 5000);
    return () => clearInterval(t);
  }, [refreshRuntimes]);

  const orchRole = runtimes?.roles.find((r) => r.role === 'orchestrator');
  const orchWired = orchRole?.wired ?? false;

  // GitHub repo chip, fetched once; null = not connected or not yet loaded.
  const [githubRepo, setGithubRepo] = useState<string | null>(null);
  // Prompt chips on the empty state, fetched from the real workflow registry;
  // falls back to PRESETS if the endpoint is not yet available.
  const [suggestions, setSuggestions] = useState<string[]>(PRESETS);
  const abortRef = useRef<AbortController | null>(null);
  // Length of the last item's streamed text: a cheap signal that changes on every
  // token, so auto-scroll follows the stream (not just whole new messages) while
  // the reader is pinned to the bottom.
  const lastLen = items.length ? ((items[items.length - 1] as { text?: string }).text?.length ?? 0) : 0;
  const { scrollRef, isAtBottom, scrollToBottom, onScroll } = useAutoScroll([items.length, lastLen]);

  // Fetch the orchestrator's real model list once on mount. Apply the server
  // default ONLY when the user has no saved choice; otherwise keep their pick (if
  // it is still an offered model), so a refresh never silently reverts it.
  useEffect(() => {
    listModels()
      .then((r) => {
        if (r.models?.length) setModels(r.models);
        const saved = localStorage.getItem(MODEL_STORAGE_KEY);
        const savedStillOffered = saved && (r.models ?? []).some((m) => m.id === saved);
        if (savedStillOffered) {
          setModelState(saved);
        } else if (r.default) {
          setModelState(r.default);
        }
      })
      .catch(() => { /* keep placeholder */ });
  }, []);

  // Fetch prompt suggestions from the real workflow registry.
  useEffect(() => {
    listSuggestions()
      .then((r) => { if (r.suggestions?.length) setSuggestions(r.suggestions); })
      .catch(() => { /* keep PRESETS */ });
  }, []);

  // Fetch GitHub connection status for the repo chip in the message bar.
  useEffect(() => {
    getGithubStatus()
      .then((s) => { if (s.connected && s.repo) setGithubRepo(s.repo); })
      .catch(() => { /* no chip */ });
  }, []);

  // Title for the header: the first user message (truncated), else default.
  const title = useMemo(() => {
    const first = items.find((it) => it.kind === 'user') as Extract<ChatItem, { kind: 'user' }> | undefined;
    if (!first) return 'Orchestrator';
    return first.text.length > 48 ? first.text.slice(0, 46) + '…' : first.text;
  }, [items]);

  const empty = items.length === 0;

  // When the URL's chatId changes (sidebar click, reload on /fleets/c/:id),
  // switch to that conversation and RESTORE its persisted transcript so the
  // messages survive a full page reload (Cmd+R), not just navigation.
  useEffect(() => {
    if (!chatId) return;
    setConversationId(chatId);
    setItems(loadTranscript(chatId) as ChatItem[]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId]);

  // Persist this conversation's transcript on every change, so a reload restores
  // it. Skip the empty state (don't write an empty transcript for a fresh chat).
  useEffect(() => {
    if (items.length) saveTranscript(conversationId, items);
  }, [items, conversationId]);

  // Auto-scroll is handled by useAutoScroll (follows new content only while the
  // reader is at the bottom; scrolling up pauses it). No unconditional scroll
  // here, so re-reading earlier messages is never interrupted.

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const picked = Array.from(files).slice(0, 5);
    const staged = await Promise.all(picked.map(async (f) => {
      if (f.type.startsWith('image/')) {
        const dataUrl: string = await new Promise((res) => {
          const fr = new FileReader();
          fr.onload = () => res(String(fr.result || ''));
          fr.readAsDataURL(f);
        });
        return { name: f.name, text: dataUrl };
      }
      try { return { name: f.name, text: (await f.text()).slice(0, 20_000) }; }
      catch { return { name: f.name, text: '' }; }
    }));
    setAttachments((prev) => [...prev, ...staged].slice(0, 5));
  }, []);

  const onPaste = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length) { e.preventDefault(); addFiles(files); }
  }, [addFiles]);

  // `overrideText` lets a one-click action (a suggestion chip) fill AND send in
  // one go: passing the text directly avoids the stale-`draft` closure that a
  // setDraft()+send() pair would hit (state updates are async).
  const send = useCallback(async (overrideText?: string) => {
    // Only honor a STRING override (a suggestion chip). Form/button submit may
    // invoke this with no arg (or an event), in which case we use the draft.
    const source = typeof overrideText === 'string' ? overrideText : draft;
    const text = source.trim();
    if ((!text && attachments.length === 0) || streaming) return;

    const chatAttachments = attachments.map((a) =>
      a.text.startsWith('data:image/')
        ? { name: a.name, data: a.text }
        : { name: a.name, text: a.text });
    const prompt = text;
    const attachNote = attachments.length
      ? `  ·  ${attachments.length} attachment${attachments.length > 1 ? 's' : ''}`
      : '';

    // Register/update this conversation in the chat list (its first message
    // becomes the title) and ensure the URL points at it, so a reload restores
    // exactly this chat. Do this BEFORE appending so the title is the user text.
    touchChat(conversationId, text || '(attachment)');
    if (chatId !== conversationId) nav(`/fleets/c/${conversationId}`, { replace: true });

    setItems((prev) => [...prev, { kind: 'user', text: (text || '(attachment)') + attachNote }]);
    setDraft('');
    setAttachments([]);
    setStreaming(true);

    let assistantIdx = -1;
    setItems((prev) => {
      assistantIdx = prev.length;
      return [...prev, { kind: 'assistant', text: '' }];
    });

    const ac = new AbortController();
    abortRef.current = ac;

    const onEvent = (ev: ChatEvent) => {
      if (ev.type === 'text') {
        setItems((prev) => {
          const next = [...prev];
          const cur = next[assistantIdx];
          if (cur && cur.kind === 'assistant') {
            next[assistantIdx] = { kind: 'assistant', text: cur.text + ev.text };
          }
          return next;
        });

      } else if (ev.type === 'reasoning') {
        setItems((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.kind === 'reasoning') {
            const next = [...prev];
            next[next.length - 1] = { kind: 'reasoning', text: last.text + ev.text };
            return next;
          }
          return [...prev, { kind: 'reasoning', text: ev.text }];
        });

      } else if (ev.type === 'tool') {
        setItems((prev) => {
          if (ev.status === 'done') {
            for (let i = prev.length - 1; i >= 0; i--) {
              const it = prev[i];
              if (it && it.kind === 'tool' && it.name === ev.name && it.status === 'running') {
                const next = [...prev];
                next[i] = { kind: 'tool', name: ev.name, status: 'done' };
                return next;
              }
            }
            return [...prev, { kind: 'tool', name: ev.name, status: 'done' }];
          }
          return [...prev, { kind: 'tool', name: ev.name, status: 'running' }];
        });

      } else if (ev.type === 'run_started') {
        setItems((prev) => {
          const withRun = [...prev, { kind: 'run' as const, runId: ev.run_id, runKind: ev.kind }];
          assistantIdx = withRun.length;
          return [...withRun, { kind: 'assistant' as const, text: '' }];
        });

      } else if (ev.type === 'error') {
        setItems((prev) => {
          const next = [...prev];
          const cur = next[assistantIdx];
          const msg = `\n\nError: ${ev.error}`;
          if (cur && cur.kind === 'assistant') {
            next[assistantIdx] = { kind: 'assistant', text: cur.text + msg };
          }
          return next;
        });
      }
    };

    try {
      await streamChat({ prompt, conversationId, model, attachments: chatAttachments }, onEvent, ac.signal);
    } catch (e) {
      if (!ac.signal.aborted) {
        setItems((prev) => {
          const next = [...prev];
          const cur = next[assistantIdx];
          const msg = `\n\nError: ${e instanceof Error ? e.message : 'chat failed'}`;
          if (cur && cur.kind === 'assistant') {
            next[assistantIdx] = { kind: 'assistant', text: (cur.text + msg).trim() };
          }
          return next;
        });
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [draft, attachments, streaming, conversationId, chatId, model, nav]);

  const stop = useCallback(() => { abortRef.current?.abort(); setStreaming(false); }, []);

  // "New chat": start a fresh conversation thread. Navigate to the bare /fleets
  // (no chatId) so the empty state shows; the new id is created and the URL moves
  // to /fleets/c/<id> on the first message. The chatId effect clears the items.
  const newChat = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
    setItems([]);
    setDraft('');
    setAttachments([]);
    setConversationId(newConversationId());
    nav('/fleets');
  }, [nav]);

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      {/* ── Conversation column ───────────────────────────────────────────── */}
      <div className="flex min-w-0 min-h-0 flex-1 flex-col overflow-hidden">

        {/* Header: title + a clean New chat button. No status dot (the run card
            carries live state); the header stays calm like the Codex empty state. */}
        <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border px-3">
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground/80">
            {empty ? 'Orchestrator' : title}
          </span>
          <button
            type="button"
            onClick={newChat}
            title="New chat"
            className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-xs font-medium text-foreground shadow-sm hover:bg-accent"
          >
            <MessageSquarePlus className="size-3.5" />
            New chat
          </button>
        </div>

        {/* Transcript: the ONLY scrolling region. min-h-0 lets this flex child
            shrink below its content height so the header above and the message
            bar below stay pinned (a flex-col child without min-h-0 grows to its
            content and pushes the siblings off-screen instead of scrolling). */}
        <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          {/* Deep-link view: /fleets/:runId, render the run directly when the
              route carries a run id that is not already in the transcript. This
              is what the sidebar's SidebarRunList navigates to. */}
          {deepLinkRunId && !items.some((it) => it.kind === 'run' && it.runId === deepLinkRunId) ? (
            <div className="mx-auto max-w-3xl px-4 py-8">
              <RunCard runId={deepLinkRunId} runKind="build" />
            </div>
          ) : empty ? (
            // Codex-style empty state: one large prompt, then a few concise chips.
            // No eyebrow, no title, no status dot.
            <div className="mx-auto flex h-full max-w-2xl flex-col items-center justify-center px-4 text-center">
              <h1 className="animate-enter-up text-3xl font-semibold tracking-[-0.02em] text-foreground">
                What should we build?
              </h1>
              <p className="animate-enter-up mt-3 text-sm text-muted-foreground" style={{ animationDelay: '40ms' }}>
                Ask a question or describe a task. Agents run only when a build is needed.
              </p>
              <div className="mt-7 flex w-full flex-col items-stretch gap-2">
                {suggestions.slice(0, 3).map((p, i) => (
                  <button
                    key={p}
                    onClick={() => { setDraft(p); void send(p); }}
                    className="animate-enter-up rounded-lg border border-border bg-card px-4 py-2.5 text-left text-sm text-foreground shadow-sm transition-colors hover:bg-accent"
                    style={{ animationDelay: `${80 + i * 45}ms` }}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto max-w-3xl px-4 py-8">
              {items.map((it, i) => {
                const isLastItem = i === items.length - 1;
                // For stepper items, the connector line extends DOWN to the next
                // stepper item; omit it after the last consecutive step.
                const nextIt = items[i + 1];
                const stepLast = !nextIt || !isStepperItem(nextIt);

                let node: React.ReactNode;
                if (it.kind === 'user') {
                  node = <UserBubble text={it.text} />;
                } else if (it.kind === 'assistant') {
                  node = <AssistantBubble text={it.text} streaming={streaming && isLastItem} />;
                } else if (it.kind === 'tool') {
                  node = <StepperToolRow name={it.name} status={it.status} isLast={stepLast} />;
                } else if (it.kind === 'reasoning') {
                  node = <StepperReasoningBlock text={it.text} live={streaming && isLastItem} isLast={stepLast} />;
                } else {
                  node = <RunCard runId={it.runId} runKind={it.runKind} />;
                }
                // Wrap each item so it rises in on first mount. The key is stable
                // (index), so the wrapper mounts once and the animation plays
                // once; streaming text mutating the inner bubble does NOT
                // re-trigger it.
                return <div key={i} className="animate-enter-up">{node}</div>;
              })}
            </div>
          )}
        </div>

        {/* Message bar: shrink-0 so it is ALWAYS pinned to the bottom and never
            squished by a long transcript. A "jump to latest" affordance floats
            just above it whenever the reader has scrolled up. */}
        <div className="relative shrink-0">
          {!isAtBottom && !empty && orchWired && (
            <button
              type="button"
              onClick={() => scrollToBottom()}
              className="absolute -top-11 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground shadow-md transition hover:bg-accent"
              title="Jump to latest"
            >
              <ChevronDown className="size-3.5" />
              Latest
            </button>
          )}
        {!orchWired ? (
          <div className="border-t border-border bg-background px-4 py-4">
            <div className="mx-auto max-w-2xl">
              <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border bg-muted/30 px-6 py-8">
                <p className="text-center text-sm text-muted-foreground">
                  The orchestrator is not wired. Deploy the coordinator in Lab 2, then wire its runtime ARN or dev URL below.
                </p>
                <div className="flex w-full max-w-lg gap-2">
                  <Input
                    value={orchWireDraft}
                    onChange={(e) => setOrchWireDraft(e.target.value)}
                    placeholder="https:// or arn:aws:bedrock-agentcore:..."
                    className="text-sm"
                    onKeyDown={async (e) => {
                      if (e.key === 'Enter') {
                        const url = orchWireDraft.trim();
                        if (!url || orchWiring) return;
                        setOrchWiring(true);
                        setOrchWireError('');
                        try {
                          const next = await wireRuntime('orchestrator', url);
                          if (next.error) setOrchWireError(next.error);
                          else { setRuntimes(next); setOrchWireDraft(''); }
                        } catch (err) {
                          setOrchWireError(err instanceof Error ? err.message : 'Failed to wire runtime.');
                        } finally {
                          setOrchWiring(false);
                        }
                      }
                    }}
                  />
                  <Button
                    onClick={async () => {
                      const url = orchWireDraft.trim();
                      if (!url || orchWiring) return;
                      setOrchWiring(true);
                      setOrchWireError('');
                      try {
                        const next = await wireRuntime('orchestrator', url);
                        if (next.error) setOrchWireError(next.error);
                        else { setRuntimes(next); setOrchWireDraft(''); }
                      } catch (err) {
                        setOrchWireError(err instanceof Error ? err.message : 'Failed to wire runtime.');
                      } finally {
                        setOrchWiring(false);
                      }
                    }}
                    disabled={!orchWireDraft.trim() || orchWiring}
                    size="sm"
                  >
                    {orchWiring ? <Loader2 className="size-4 animate-spin" /> : 'Connect'}
                  </Button>
                </div>
                {orchWireError && <p className="text-xs text-destructive">{orchWireError}</p>}
              </div>
            </div>
          </div>
        ) : (
        <PromptInput>
          <PromptInputForm onSubmit={send}>
            {attachments.length > 0 && (
              <div className="flex flex-wrap gap-1.5 px-3 pt-3">
                {attachments.map((a, i) => (
                  <span key={`${a.name}-${i}`} className="flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-xs">
                    <FileText className="size-3 shrink-0 text-muted-foreground" />
                    <span className="max-w-[160px] truncate">{a.name}</span>
                    <button
                      type="button"
                      onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                      className="rounded p-0.5 text-muted-foreground hover:bg-background hover:text-foreground"
                      aria-label={`Remove ${a.name}`}
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <PromptInputTextarea
              value={draft}
              onChange={setDraft}
              onSubmit={send}
              onPaste={onPaste}
              hasAttachments={attachments.length > 0}
              placeholder="Message your orchestrator…"
            />
            <PromptInputActions>
              <PromptInputLeftActions>
                <input
                  id="fleet-attach"
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ''; }}
                />
                <PromptInputAttachButton htmlFor="fleet-attach" disabled={streaming} />
                {/* GitHub repo chip, only shown when a real repo is connected.
                    Shows the full owner/repo (no clip): a repo like
                    aws-samples/amazon-bedrock-agentcore-coding-agents must be fully readable. */}
                {githubRepo && (
                  <span className="flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground">
                    <GitBranch className="size-3 shrink-0" />
                    {githubRepo}
                  </span>
                )}
              </PromptInputLeftActions>
              <PromptInputRightActions>
                {/* The orchestrator's own model selector sits on the right, next to
                    Send (R20), like the Codex composer. */}
                <ModelSelector value={model} onChange={setModel} disabled={streaming} models={models} />
                <PromptInputSubmit
                  disabled={!draft.trim() && attachments.length === 0}
                  isStreaming={streaming}
                  onStop={stop}
                  onSubmit={send}
                />
              </PromptInputRightActions>
            </PromptInputActions>
          </PromptInputForm>
          <p className="mx-auto mt-2 max-w-3xl px-4 text-center text-xs text-muted-foreground">
            The orchestrator runs on AgentCore Runtime. It answers, and dispatches agents only when a task needs them.
          </p>
        </PromptInput>
        )}
        </div>
      </div>
    </div>
  );
}

// ── Run card ──────────────────────────────────────────────────────────────────

// memo: RunCard owns its own polling loop, so it must not be torn down/
// re-rendered every time the parent transcript updates from a streaming token.
// Its only props are the stable runId/runKind.
const RunCard = memo(function RunCard({ runId, runKind }: { runId: string; runKind: string }) {
  const [run, setRun] = useState<(RunSummary & Record<string, unknown>) | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  // After repeated 404s with no successful load, the run id is unknown (a stale
  // deep link). Show a clear not-found state instead of a forever-empty card.
  const [notFound, setNotFound] = useState(false);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  const misses = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const detail = await getRun(runId);
        let merged: RunSummary & Record<string, unknown> = detail as RunSummary & Record<string, unknown>;
        try {
          const { terminals, events } = await getRunTerminals(runId);
          merged = { ...merged, terminals, roleEvents: events };
        } catch { /* terminals optional */ }
        if (cancelled) return;
        misses.current = 0;
        setRun(merged);
        if (TERMINAL_STATUSES.includes(detail.status)) {
          if (poll.current) { clearInterval(poll.current); poll.current = null; }
          try { setResult(await getRunResult(runId)); } catch { /* 409 until terminal */ }
        }
      } catch {
        // Unknown run id: never loaded after a few tries -> stop and mark not-found.
        if (!run) {
          misses.current += 1;
          if (misses.current >= 3 && !cancelled) {
            setNotFound(true);
            if (poll.current) { clearInterval(poll.current); poll.current = null; }
          }
        }
      }
    };
    tick();
    poll.current = setInterval(tick, 1000);
    return () => { cancelled = true; if (poll.current) clearInterval(poll.current); };
  }, [runId]);

  if (notFound) {
    return (
      <div id={`run-${runId}`} className="my-4 rounded-lg border border-border bg-muted/20 p-6 text-center">
        <p className="text-sm font-medium text-foreground">Run not found</p>
        <p className="mt-1 text-xs text-muted-foreground">
          No run with id <span className="font-mono">{runId}</span>. It may have been cleared on a server restart.
        </p>
      </div>
    );
  }

  const live = run != null && !TERMINAL_STATUSES.includes(run.status as string);
  const failReason = run?.fail_reason as string | undefined;
  const route = run?.route as Parameters<typeof RunActivityRows>[0]['route'];
  const progress = run?.progress as Parameters<typeof RunActivityRows>[0]['progress'];
  const roleEvents = (run?.roleEvents as Record<string, AgentEvent[]> | undefined) ?? undefined;
  const terminals = run?.terminals as Record<string, Array<{ cmd?: string; output?: string; text?: string }>> | undefined;

  return (
    <div
      id={`run-${runId}`}
      className={cn(
        'my-4 rounded-lg border bg-muted/20 p-3 transition-[border-color,box-shadow] duration-(--motion-base) ease-soft',
        // A live run gets a faint success (brand blue) ring + soft glow so the
        // eye lands on the active card; it settles to the neutral border when
        // the run ends.
        live ? 'border-success/30 shadow-[0_0_0_1px_hsl(var(--success)/0.06),0_1px_12px_-4px_hsl(var(--success)/0.25)]' : 'border-border',
      )}
    >
      <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
        <span className="rounded bg-background px-1.5 py-0.5 font-mono">{runId}</span>
        <span>·</span>
        <span>{runKind} build</span>
        {live && (
          <span className="ml-auto flex items-center gap-2 text-foreground/70">
            {/* The phase label is the real engine phase (meaningful info); when
                it's unknown we show only the dots, never the bare word "working". */}
            {PHASE_LABEL[(run?.phase as string)] && (
              <Shimmer className="text-xs" duration={1.4}>{PHASE_LABEL[(run?.phase as string)]!}</Shimmer>
            )}
            <WorkingDots className="text-success" />
          </span>
        )}
      </div>
      {run && (
        <div className="mb-2">
          <RunActivityRows route={route} progress={progress} roleEvents={roleEvents} live={live} />
        </div>
      )}
      {run && <RunDetailPanel run={run as Parameters<typeof RunDetailPanel>[0]['run']} />}
      {terminals && Object.keys(terminals).length > 0 && (
        <div className="mt-2">
          <RunTerminalPane terminals={terminals} live={live} />
        </div>
      )}
      {!live && failReason && !result && (
        <div className="mt-2 flex items-center gap-2 text-sm text-destructive">
          <AlertCircle className="size-4" />
          Stopped: {failReason}
        </div>
      )}
      {/* Changes tab: the real composed diff, once the run has settled (the
          commit lands with the gate). Renders nothing until then. */}
      {run && !live && (result || TERMINAL_STATUSES.includes(run.status as string)) && (
        <RunChangesPane runId={runId} />
      )}
      {result && <OrchestratorVerdict result={result} />}
    </div>
  );
});

// The terminal surface for a run. An AGENT lane (claude-code / kiro / opencode)
// is that agent's REAL AgentCore Runtime shell session. When the dispatch drove a
// LIVE interactive session (the muxed path), the lane carries its
// live_session_id and the pane renders the SAME live PTY stream the Agents page
// shows -- one session, two surfaces -- via LiveSessionPane (xterm over the
// session's SSE). A lane with no live session (headless dispatch, or the run
// predates the console) falls back to the recorded transcript. The
// ``orchestrator`` lane is the engine's own host-side plumbing (harness staging,
// module probes, the acceptance gate) -- separate work on the orchestrator box,
// NOT the agent's session, so it is its own clearly-labeled tab, sorted last,
// never the default.
type TerminalEntry = {
  cmd?: string; output?: string; text?: string; live_session_id?: string;
};

const ORCHESTRATOR_LANE = 'orchestrator';
const LANE_LABEL: Record<string, string> = {
  [ORCHESTRATOR_LANE]: 'orchestrator (host)',
};

// The live PTY stream for one runtime session, embedded in the run view. Same
// data path as the Agents page terminal (subscribeOutput SSE + buffer replay),
// read-only here: the run view is for WATCHING the agent work; typing belongs
// to the Agents page tab, which is the same underlying session.
function LiveSessionPane({ sessionId }: { sessionId: string }) {
  const termRef = useRef<TerminalHandle>(null);
  const [gone, setGone] = useState(false);
  useEffect(() => {
    const replay = getBuffer(sessionId);
    if (replay) termRef.current?.write(replay);
    const unsub = subscribeOutput(
      sessionId,
      (s) => termRef.current?.write(s),
      () => setGone(true),
    );
    // Size the hidden-tab-safe fit once the pane is visible.
    requestAnimationFrame(() => termRef.current?.fit());
    return unsub;
  }, [sessionId]);
  if (gone) return null;
  return (
    <div className="h-72 overflow-hidden rounded-lg">
      <Terminal ref={termRef} connected />
    </div>
  );
}

function RunTerminalPane({
  terminals, live,
}: {
  terminals: Record<string, TerminalEntry[]>;
  live: boolean;
}) {
  // Agent lanes first (the real runtime sessions), the orchestrator host lane last.
  const lanes = Object.keys(terminals).sort((a, b) => {
    const ao = a === ORCHESTRATOR_LANE ? 1 : 0;
    const bo = b === ORCHESTRATOR_LANE ? 1 : 0;
    return ao - bo;
  });
  if (lanes.length === 0) return null;
  // Default to the first AGENT lane so the runtime session is what opens, never
  // the host plumbing.
  const defaultLane = lanes.find((l) => l !== ORCHESTRATOR_LANE) ?? lanes[0]!;

  // A lane whose newest entry names a live session renders the LIVE PTY (the
  // same session the Agents page streams); otherwise the recorded transcript.
  const liveSessionId = (entries: TerminalEntry[]): string | null => {
    for (let i = entries.length - 1; i >= 0; i--) {
      const sid = entries[i]?.live_session_id;
      if (sid) return sid;
    }
    return null;
  };

  const renderLines = (entries: TerminalEntry[]): string =>
    entries.flatMap((e) => {
      const lines: string[] = [];
      if (e.cmd) lines.push(`$ ${e.cmd}`);
      const body = e.output ?? e.text ?? '';
      if (body) lines.push(body);
      return lines;
    }).join('\n');

  const paneClass =
    'h-56 overflow-y-auto rounded-lg bg-[#1e1e1e] px-3 py-2 font-mono text-[11.5px] leading-relaxed text-[#d4d4d4] whitespace-pre-wrap break-all [scrollbar-width:thin] [scrollbar-color:#555_transparent]';

  const TermPane = ({ entries }: { entries: TerminalEntry[] }) => {
    const sid = liveSessionId(entries);
    if (sid) return <LiveSessionPane sessionId={sid} />;
    const text = renderLines(entries);
    return (
      <pre className={paneClass}>
        {text
          ? text
          : <span className="text-muted-foreground">Waiting for output...</span>
        }
      </pre>
    );
  };

  if (lanes.length === 1) {
    const lane = lanes[0]!;
    return (
      <div className="space-y-1">
        <div className="flex items-center gap-1.5">
          <span className="eyebrow text-muted-foreground/70">{LANE_LABEL[lane] ?? lane}</span>
          {live && <PulseDot live tone="info" size={6} />}
        </div>
        <TermPane entries={terminals[lane]!} />
      </div>
    );
  }

  return (
    <Tabs defaultValue={defaultLane} className="space-y-1">
      <div className="flex items-center gap-2">
        <TabsList className="h-7">
          {lanes.map((l) => (
            <TabsTrigger key={l} value={l} className="px-2 py-0.5 text-xs">{LANE_LABEL[l] ?? l}</TabsTrigger>
          ))}
        </TabsList>
        {live && <PulseDot live tone="info" size={6} />}
      </div>
      {lanes.map((l) => (
        <TabsContent key={l} value={l}>
          <TermPane entries={terminals[l]!} />
        </TabsContent>
      ))}
    </Tabs>
  );
}

// ── Changes tab ─────────────────────────────────────────────────────────────
//
// The composed change as a per-file unified diff, the local twin of the PR's
// "Files changed" (Copilot-app pattern). Loaded from the run's REAL commit in
// the composed repo (GET /runs/:id/diff -> `git show`), so the files and hunks
// are exactly what the PR carries. Shown only once a run is terminal (the
// commit lands when the gate goes green); a run with no commit renders nothing.
const RunChangesPane = memo(function RunChangesPane({ runId }: { runId: string }) {
  const [diff, setDiff] = useState<RunDiff | null>(null);
  useEffect(() => {
    let cancelled = false;
    getRunDiff(runId).then((d) => { if (!cancelled) setDiff(d); }).catch(() => { /* no diff */ });
    return () => { cancelled = true; };
  }, [runId]);

  if (!diff || diff.files.length === 0) return null;
  const totalAdded = diff.files.reduce((n, f) => n + (f.added ?? 0), 0);
  const totalRemoved = diff.files.reduce((n, f) => n + (f.removed ?? 0), 0);

  return (
    <div className="mt-2 rounded-lg border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
        <FileText className="size-3.5" />
        <span className="font-medium text-foreground">
          {diff.files.length} file{diff.files.length === 1 ? '' : 's'} changed
        </span>
        {totalAdded > 0 && <span className="text-emerald-500">+{totalAdded}</span>}
        {totalRemoved > 0 && <span className="text-destructive">-{totalRemoved}</span>}
        {diff.branch && <span className="ml-auto font-mono opacity-70">{diff.branch}</span>}
      </div>
      <div className="divide-y divide-border">
        {diff.files.map((f) => (
          <Collapsible key={f.path} defaultOpen={diff.files.length <= 2}>
            <CollapsibleTrigger className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-muted/40">
              <ChevronRight className="size-3 shrink-0 transition-transform data-[state=open]:rotate-90" />
              <span className="min-w-0 flex-1 truncate font-mono">{f.path}</span>
              {typeof f.added === 'number' && f.added > 0 && <span className="text-emerald-500">+{f.added}</span>}
              {typeof f.removed === 'number' && f.removed > 0 && <span className="text-destructive">-{f.removed}</span>}
            </CollapsibleTrigger>
            <CollapsibleContent>
              <DiffBody patch={f.patch} />
            </CollapsibleContent>
          </Collapsible>
        ))}
      </div>
    </div>
  );
});

// One file's unified-diff patch, colored per hunk line (Copilot's green/red
// gutter). We render the raw `git show` patch; the +/- prefix drives the color.
function DiffBody({ patch }: { patch: string }) {
  // Drop the file header lines (diff --git / index / +++ / ---) so the pane
  // shows the hunks, matching the reference's per-file body.
  const lines = patch.split('\n').filter(
    (l) => !/^(diff --git |index |--- |\+\+\+ |new file|deleted file|similarity |rename )/.test(l));
  return (
    <pre className="max-h-72 overflow-auto bg-[#1e1e1e] px-3 py-2 font-mono text-[11.5px] leading-relaxed [scrollbar-width:thin] [scrollbar-color:#555_transparent]">
      {lines.map((l, i) => {
        const tone = l.startsWith('@@')
          ? 'text-sky-400'
          : l.startsWith('+')
            ? 'bg-emerald-500/10 text-emerald-300'
            : l.startsWith('-')
              ? 'bg-destructive/10 text-red-300'
              : 'text-[#d4d4d4]';
        return <div key={i} className={cn('whitespace-pre-wrap break-all px-1', tone)}>{l || ' '}</div>;
      })}
    </pre>
  );
}

function OrchestratorVerdict({ result }: { result: RunResult }) {
  const passed = result.status === 'passed';
  const gatePassed = result.gate?.passed;
  const reviewState = result.review?.state;
  return (
    <div className="mt-2 space-y-2 rounded-xl bg-background px-3 py-2.5 text-sm">
      <div className="flex items-center gap-2 font-medium">
        {passed ? <CheckCircle2 className="size-4 text-muted-foreground" /> : <AlertCircle className="size-4 text-destructive" />}
        {passed
          ? 'Done. The acceptance gate passed.'
          : result.status === 'needs_human'
            ? 'Stopped for a human. The gate did not pass.'
            : 'The run failed.'}
      </div>
      <ul className="space-y-0.5 text-xs text-muted-foreground">
        <li>pytest gate: {gatePassed ? 'passed' : 'did not pass'}
          {result.gate?.checks?.length ? ` (${result.gate.checks.length} checks)` : ''}</li>
        {reviewState && <li>review: {reviewState}</li>}
        {typeof result.iterations === 'number' && <li>iterations: {result.iterations}</li>}
        {result.fail_reason && <li>reason: {result.fail_reason}</li>}
      </ul>
      {result.pr_url ? (
        <a href={result.pr_url} target="_blank" rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-sm underline">
          <GitPullRequest className="size-3.5" />
          View the pull request
        </a>
      ) : null}
    </div>
  );
}

// ── Transcript items ───────────────────────────────────────────────────────

// memo: a user bubble's text never changes after it's placed, so it must not
// re-render on every streaming token tick of a later assistant message.
const UserBubble = memo(function UserBubble({ text }: { text: string }) {
  return (
    <div className="mb-4 flex justify-end">
      <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
        {text}
      </div>
    </div>
  );
});

// The assistant's prose, rendered as markdown. Animated dots while streaming
// with no text yet (only tool/reasoning events so far).
function AssistantBubble({ text, streaming }: { text: string; streaming: boolean }) {
  const isEmpty = text.length === 0;
  return (
    <div className="mb-4 flex justify-start">
      <div className="max-w-[85%] rounded-2xl rounded-bl-sm bg-muted px-4 py-2.5 text-sm">
        {isEmpty && streaming ? (
          <WorkingDots size={5} />
        ) : isEmpty ? (
          <span className="opacity-0">.</span>
        ) : (
          // `md-stream` (when streaming) hangs the blinking caret off the LAST
          // rendered block's end via ::after, so it trails the final character
          // inline instead of dropping to its own newline below the prose.
          <div className={cn('prose-chat', streaming && 'md-stream')}>
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>{text}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Foxl-style stepper ────────────────────────────────────────────────────
//
// Each tool call / reasoning block uses the foxl ToolCallRenderer layout:
//   - Left column: a small circle (icon inside) + a vertical connector line
//     that links to the next step when !isLast.
//   - Right column: a collapsible trigger row + optional expanded content.
// The vertical line is drawn as a 1px div that grows to fill the left column
// height, connected visually to the circle above it.

// A tool call the orchestrator made, shown as a foxl stepper item.
// While running: spinner in the circle + Shimmer label. When done: icon + label.
// memo: re-renders only when its own name/status/isLast change, not on every
// token the streaming assistant message appends below it.
const StepperToolRow = memo(function StepperToolRow({
  name, status, isLast,
}: {
  name: string;
  status: 'running' | 'done';
  isLast: boolean;
}) {
  const [open, setOpen] = useState(false);
  const label = TOOL_LABEL[name] ?? name;
  const Icon = TOOL_ICON[name] ?? Wrench;
  const running = status === 'running';

  return (
    <div className="relative flex gap-3">
      {/* Left column: circle node + vertical connector */}
      <div className="flex flex-col items-center">
        <div className={cn(
          'flex size-6 shrink-0 items-center justify-center rounded-full border bg-muted transition-colors duration-(--motion-base) ease-soft',
          running ? 'border-success/40' : 'border-border',
        )}>
          {running
            ? <Loader2 className="size-3 animate-spin text-success" />
            : <Icon className="size-3 text-muted-foreground" />
          }
        </div>
        {!isLast && <div className="mt-1 w-px flex-1 bg-border" />}
      </div>
      {/* Right column: trigger + optional expanded detail */}
      <div className={cn('min-w-0 flex-1 overflow-hidden', !isLast && 'pb-4')}>
        <Collapsible open={open} onOpenChange={setOpen}>
          <CollapsibleTrigger className="group flex w-full items-center gap-1.5 text-left text-xs text-muted-foreground hover:text-foreground">
            {running ? (
              <Shimmer className="text-xs font-medium" duration={1.2}>{label}</Shimmer>
            ) : (
              <span className="font-medium">{label}</span>
            )}
            <ChevronRight className={cn('ml-auto size-3 shrink-0 opacity-50 transition-transform', open && 'rotate-90')} />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="mt-1 rounded-md border border-border/50 bg-muted/30 px-2.5 py-1.5 text-[11px] text-muted-foreground">
              <span className="font-mono">{name}</span>
              <span className="ml-2 text-muted-foreground/70">{running ? 'running' : 'done'}</span>
            </div>
          </CollapsibleContent>
        </Collapsible>
      </div>
    </div>
  );
});

// Extended reasoning: foxl stepper shape with a brain icon. Collapsed by
// default; Shimmer "Thinking…" while live, "Thought for a moment" when done.
// memo: only its own text/live/isLast drive re-renders.
const StepperReasoningBlock = memo(function StepperReasoningBlock({
  text, live, isLast,
}: {
  text: string;
  live: boolean;
  isLast: boolean;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="relative flex gap-3">
      {/* Left column */}
      <div className="flex flex-col items-center">
        <div className="flex size-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted">
          <Brain className="size-3 text-muted-foreground" />
        </div>
        {!isLast && <div className="mt-1 w-px flex-1 bg-border" />}
      </div>
      {/* Right column */}
      <div className={cn('min-w-0 flex-1 overflow-hidden', !isLast && 'pb-4')}>
        <Collapsible open={open} onOpenChange={setOpen}>
          <CollapsibleTrigger className="group flex w-full items-center gap-1.5 text-left text-xs text-muted-foreground hover:text-foreground">
            {live ? (
              <Shimmer className="text-xs" duration={1.2}>Thinking…</Shimmer>
            ) : (
              <span>Thought for a moment</span>
            )}
            <ChevronRight className={cn('ml-auto size-3 shrink-0 opacity-50 transition-transform', open && 'rotate-90')} />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-border/50 bg-muted/30 px-2.5 py-1.5 text-[11px] leading-relaxed text-muted-foreground">
              {text}
            </div>
          </CollapsibleContent>
        </Collapsible>
      </div>
    </div>
  );
});

// ── Model selector ────────────────────────────────────────────────────────

function ModelSelector({
  value, onChange, disabled, models,
}: {
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  models: ModelOption[];
}) {
  const [open, setOpen] = useState(false);
  const current = models.find((m) => m.id === value) ?? models[0] ?? FALLBACK_MODEL;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="flex h-8 items-center gap-1.5 rounded-lg px-2 text-[13px] text-foreground/80 hover:bg-muted hover:text-foreground disabled:opacity-50"
        >
          <span className="max-w-[150px] truncate">{current.label}</span>
          <ChevronDown className="size-3 shrink-0 opacity-40" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[260px] p-1.5">
        {models.map((m) => {
          const selected = m.id === value;
          return (
            <button
              key={m.id}
              type="button"
              onClick={() => { onChange(m.id); setOpen(false); }}
              className={cn(
                'flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left hover:bg-muted/60',
                selected && 'bg-muted/50',
              )}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-medium">{m.label}</div>
                {m.hint && <div className="truncate text-[11px] text-muted-foreground/80">{m.hint}</div>}
              </div>
              {selected && <Check className="size-3.5 shrink-0 text-primary" />}
            </button>
          );
        })}
      </PopoverContent>
    </Popover>
  );
}
