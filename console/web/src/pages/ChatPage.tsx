import { useEffect, useRef, useState, useCallback, useMemo, memo } from 'react';
import { useParams, useNavigate, useHref, useSearchParams } from 'react-router-dom';
import { newConversationId, touchChat, loadTranscript, saveTranscript, useChats, removeChat } from '../hooks/useChats';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Tabs from '@cloudscape-design/components/tabs';
import TextContent from '@cloudscape-design/components/text-content';
import { cn } from '@foxl/ui';
import { ArrowDown, ArrowLeft, ArrowUpRight, Code2, History, MessageSquarePlus, PanelTop, Settings2, TerminalSquare, Trash2 } from 'lucide-react';
import { ChatComposer } from '../components/chat/ChatComposer';
import {
  streamChat, getRun, getRunTerminals, getRunResult, getRunDiff, listModels, getGithubStatus,
  listSuggestions, getRuntimes, listRuns, ApiError,
  type ChatEvent, type RunDetail, type RunResult, type RunDiff, type ModelOption,
  type RuntimeStatus, type GithubStatus,
} from '../api';
import { RunDetailPanel } from '../components/RunDetailPanel';
import { ResourceTable } from '../shared/ResourceTable';
import { LoadingState, ErrorState } from '../shared/States';
import { presentRunDecision, recordedPullRequests, gateResultLabel } from '../lib/runPresentation';
import { AgentIcon } from '../components/AgentIcon';
import { onAgentRoles, agentInstanceLabel, type AgentRole } from './agents/environments';
import { RunActivityRows } from '../components/RunActivityRows';
import { useAutoScroll } from '../hooks/useAutoScroll';
import { Terminal, type TerminalHandle } from '../components/Terminal';
import { subscribeOutput } from '../hooks/useSessionStore';

// Human-readable label for a run's CURRENT phase: a straight rename of the real
// engine phase id (run.phase), not invented narration.
const PHASE_LABEL: Record<string, string> = {
  admission: 'routing the task',
  context_hydration: 'preparing the shared context',
  pre_flight: 'running readiness checks',
  agent_execution: 'dispatching the agents',
  finalization: 'checking and reviewing the role pull requests',
};

// How many opener chips the empty state shows (the backend caps its own list at
// the same number, so the chips never clip). There is deliberately NO hardcoded
// list of openers here: the chips come from the presets API, which is the one
// source the router reads, so the console cannot offer a request it cannot route.
const MAX_SUGGESTIONS = 3;

const TERMINAL_STATUSES = ['passed', 'failed', 'needs_human'];

// Where the chosen orchestrator model is remembered across reloads.
const MODEL_STORAGE_KEY = 'agentcore.console.orchestrator-model';

function savedModel(): string {
  try { return localStorage.getItem(MODEL_STORAGE_KEY) || ''; } catch { return ''; }
}

// Friendly labels for the orchestrator's own tool calls (chat-level tools, not
// per-role coding-agent tools). Every name is a real tool name the orchestrator
// emits, never invented.
const TOOL_LABEL: Record<string, string> = {
  list_presets:       'Reading build starting points',
  // No vendor names here: WORKSHOP_ROLES decides which agent fills each capability,
  // so a literal goes stale the moment the roster changes (it said "validator (Claude
  // Code)" while Kiro was the served checker). The roster view names the agents.
  dispatch_backend:   'Dispatching backend',
  dispatch_frontend:  'Dispatching frontend',
  dispatch_validator: 'Dispatching validator',
  run_build:          'Running the full build',
  run_status:         'Checking run status',
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

export function ChatPage() {
  // /chat/:runId deep-links a run; /chat/c/:chatId selects a sub-chat.
  const { runId: deepLinkRunId, chatId } = useParams<{ runId?: string; chatId?: string }>();
  const nav = useNavigate();
  const base = useHref('/').replace(/\/$/, '');
  const href = (path: string) => `${base}${path}`;
  const [searchParams] = useSearchParams();
  const view = searchParams.get('view') === 'history' ? 'history' : 'workspace';
  const { chats } = useChats();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [teamOpen, setTeamOpen] = useState(false);
  const [historyTab, setHistoryTab] = useState('conversations');
  const [deleteChatId, setDeleteChatId] = useState<string | null>(null);

  // The active conversation is the URL's chatId when present, else a fresh id.
  // Keep it in state so a brand-new chat (no chatId yet) has a stable id until
  // the first message routes to /chat/c/<id>.
  const [conversationId, setConversationId] = useState(() => chatId || newConversationId());
  const [items, setItems] = useState<ChatItem[]>(() => chatId ? loadTranscript(chatId) as ChatItem[] : []);
  const [draft, setDraft] = useState('');
  const [models, setModels] = useState<ModelOption[]>([]);
  // Persist the chosen orchestrator model across reloads. Seed from localStorage
  // so a refresh keeps the selection; the server default only applies when the
  // user has never picked one (see the fetch effect below).
  const [model, setModelState] = useState(savedModel);
  const setModel = useCallback((id: string) => {
    setModelState(id);
    try { localStorage.setItem(MODEL_STORAGE_KEY, id); } catch { /* private mode */ }
  }, []);
  const [attachments, setAttachments] = useState<{ name: string; text: string }[]>([]);
  const [streaming, setStreaming] = useState(false);
  // Deployment wiring is discovered by the host. Chat uses its in-process coordinator.
  const [runtimes, setRuntimes] = useState<RuntimeStatus | null>(null);
  const [runtimeLoadError, setRuntimeLoadError] = useState('');
  const [modelError, setModelError] = useState('');
  const [attachmentError, setAttachmentError] = useState('');

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await getRuntimes();
        if (live) { setRuntimes(next); setRuntimeLoadError(''); }
      } catch { if (live) setRuntimeLoadError('Could not reach the workshop host. Reconnecting…'); }
      if (live) timer = setTimeout(refresh, 5000);
    };
    void refresh();
    return () => { live = false; clearTimeout(timer); };
  }, []);
  // A separate coordinator Runtime belongs to the CLI path. Requiring its ARN
  // here would disable Chat on an otherwise fully pre-wired workshop host.
  const coordinatorAvailable = runtimes !== null && !runtimeLoadError;
  const [roles, setRoles] = useState<AgentRole[]>([]);
  useEffect(() => onAgentRoles(setRoles), []);

  // GitHub repo chip, fetched once; null = not connected or not yet loaded.
  const [github, setGithub] = useState<GithubStatus | null>(null);
  const [githubError, setGithubError] = useState('');
  // Prompt chips on the empty state, fetched from the presets API. Empty until
  // that fetch lands: an unreachable orchestrator must show NO chips rather than
  // stale ones this file invented, because a chip the router cannot resolve is a
  // request the attendee cannot actually run.
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const activeConversation = useRef(conversationId);
  activeConversation.current = conversationId;
  useEffect(() => () => abortRef.current?.abort(), []);
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
        const saved = savedModel();
        const savedStillOffered = saved && (r.models ?? []).some((m) => m.id === saved);
        if (savedStillOffered) {
          setModelState(saved);
        } else if (r.default) {
          setModelState(r.default);
        }
      })
      .catch(() => setModelError('Could not load the coordinator models. Reload the page to try again.'));
  }, []);

  // Fetch the opener chips from the presets API (the router's own source).
  useEffect(() => {
    listSuggestions()
      .then((r) => { if (r.suggestions?.length) setSuggestions(r.suggestions); })
      .catch(() => { /* no chips: the attendee types their own request */ });
  }, []);

  // Fetch GitHub connection status for the repo chip in the message bar.
  useEffect(() => {
    getGithubStatus()
      .then(setGithub)
      .catch(() => setGithubError('Could not read the GitHub connection.'));
  }, []);

  // Title for the header: the first user message (truncated), else default.
  const title = useMemo(() => {
    const first = items.find((it) => it.kind === 'user') as Extract<ChatItem, { kind: 'user' }> | undefined;
    if (!first) return 'New build';
    return first.text.length > 48 ? first.text.slice(0, 46) + '…' : first.text;
  }, [items]);

  const empty = items.length === 0;

  // When the URL's chatId changes (sidebar click, reload on /chat/c/:id),
  // switch to that conversation and RESTORE its persisted transcript so the
  // messages survive a full page reload (Cmd+R), not just navigation.
  useEffect(() => {
    if (!chatId || chatId === conversationId) return;
    abortRef.current?.abort();
    setStreaming(false);
    setDraft('');
    setAttachments([]);
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
    setAttachmentError('');
    const picked = Array.from(files).slice(0, 5);
    const staged = await Promise.all(picked.map(async (f) => {
      if (f.type.startsWith('image/')) {
        const dataUrl: string = await new Promise((res) => {
          const fr = new FileReader();
          fr.onload = () => res(String(fr.result || ''));
          fr.onerror = () => { setAttachmentError(`Could not read ${f.name}. Remove it and try again.`); res(''); };
          fr.readAsDataURL(f);
        });
        return { name: f.name, text: dataUrl };
      }
      try { return { name: f.name, text: (await f.text()).slice(0, 20_000) }; }
      catch { setAttachmentError(`Could not read ${f.name}. Try attaching it again.`); return { name: f.name, text: '' }; }
    }));
    setAttachments((prev) => [...prev, ...staged.filter(f => f.text)].slice(0, 5));
  }, []);

  // `overrideText` lets a one-click action (a suggestion chip) fill AND send in
  // one go: passing the text directly avoids the stale-`draft` closure that a
  // setDraft()+send() pair would hit (state updates are async).
  const send = useCallback(async (overrideText?: string) => {
    // Only honor a STRING override (a suggestion chip). Form/button submit may
    // invoke this with no arg (or an event), in which case we use the draft.
    const source = typeof overrideText === 'string' ? overrideText : draft;
    const text = source.trim();
    if ((!text && attachments.length === 0) || streaming || !coordinatorAvailable || !model) return;

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
    if (chatId !== conversationId) nav(`/chat/c/${conversationId}`, { replace: true });

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
      if (ac.signal.aborted || activeConversation.current !== conversationId) return;
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
      if (abortRef.current === ac) { setStreaming(false); abortRef.current = null; }
    }
  }, [draft, attachments, streaming, conversationId, chatId, model, nav, coordinatorAvailable]);

  const stop = useCallback(() => { abortRef.current?.abort(); setStreaming(false); }, []);

  // "New chat": start a fresh conversation thread. Navigate to the bare /chat
  // (no chatId) so the empty state shows; the new id is created and the URL moves
  // to /chat/c/<id> on the first message. The chatId effect clears the items.
  const newChat = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
    setItems([]);
    setDraft('');
    setAttachments([]);
    setConversationId(newConversationId());
    nav('/chat');
  }, [nav]);

  const composer = <ChatComposer draft={draft} onDraft={setDraft} onSend={() => void send()} onStop={stop}
    streaming={streaming} connected={coordinatorAvailable}
    connectionError={runtimeLoadError} loadingConnection={!runtimes && !runtimeLoadError}
    models={models} model={model} onModel={setModel} modelError={modelError}
    repo={github?.connected ? github.repo : undefined} onSettings={() => setTeamOpen(true)}
    attachments={attachments} onFiles={files => { void addFiles(files); }}
    onRemoveFile={index => setAttachments(prev => prev.filter((_, i) => i !== index))} attachmentError={attachmentError} />;
  const openHistory = () => { setHistoryOpen(true); setHistoryTab('conversations'); };
  const teamDetails = <SpaceBetween size="l">
    <KeyValuePairs columns={2} items={[
      { label: 'Coordinator', value: <StatusIndicator type={runtimeLoadError ? 'error' : !runtimes ? 'loading' : 'info'}>
        {runtimeLoadError ? 'Host unavailable' : !runtimes ? 'Loading' : 'Runs on this host'}</StatusIndicator> },
      { label: 'Repository', value: githubError ? 'Unavailable' : github?.repo || 'Not configured' },
      { label: 'Merge policy', value: github?.merge_policy === 'auto' ? 'Automatic after checks and review'
        : github?.merge_policy === 'human_review' ? 'Human review' : 'Not available' },
      { label: 'Execution context', value: 'Workshop host coordinator' },
    ]} />
    <Header variant="h3">Coding team</Header>
    {roles.map(role => <div key={role.id} className="chat-team-detail">
      <AgentIcon agentId={role.id} size={26} /><div>
        <Link href={href(`/agents?agent=${encodeURIComponent(role.id)}`)} onFollow={e => { e.preventDefault(); nav(`/agents?agent=${encodeURIComponent(role.id)}`); }}>{role.label}</Link>
        <Box color="text-body-secondary">{role.instances.map(i => i.label).join(', ')}</Box>
        <StatusIndicator type={!runtimes ? 'loading' : role.instances.every(i => runtimes.roles.some(r => r.role === i.id && r.wired)) ? 'success' : 'pending'}>
          {!runtimes ? 'Loading' : role.instances.every(i => runtimes.roles.some(r => r.role === i.id && r.wired)) ? 'Configured' : 'Not configured'}
        </StatusIndicator>
      </div>
    </div>)}
    <ExpandableSection headerText="How a build is checked">
      <TextContent><p>Each builder opens its own pull request. The validator writes an executable check, then an independent reviewer inspects the result and integration.</p>
        <p>A failed check allows one repair on the same pull request, then hands the decision back to a person.</p></TextContent>
    </ExpandableSection>
    <Box color="text-body-secondary">Chats here use the host coordinator. Follow a build submitted to the deployed coordinator with its CLI watcher.</Box>
  </SpaceBetween>;

  return <section className="chat-surface" aria-label="Chat with your coding team">
    <div className="chat-page-toolbar">
      <div className="chat-page-title"><span>Chat</span>{!empty && <span className="chat-conversation-title">{title}</span>}</div>
      <div className="chat-page-actions">
        <button type="button" className="chat-toolbar-button" onClick={() => setTeamOpen(true)} title="Team and connections" aria-label="Team and connections"><Settings2 size={17} aria-hidden="true" /><span>Team</span></button>
        <button type="button" className="chat-toolbar-button" onClick={openHistory} title="History"><History size={17} aria-hidden="true" /><span>History</span></button>
        <button type="button" className="chat-toolbar-button" disabled={streaming} onClick={newChat} title="New chat"><MessageSquarePlus size={17} aria-hidden="true" /><span>New chat</span></button>
      </div>
    </div>
    {deepLinkRunId || view === 'history' ? <div className="chat-evidence-page">
      <button type="button" className="chat-toolbar-button" onClick={() => nav(chatId ? `/chat/c/${chatId}` : '/chat')}><ArrowLeft size={16} aria-hidden="true" />Back to chat</button>
      {deepLinkRunId ? <RunCard runId={deepLinkRunId} runKind="build" /> : <BuildHistory />}
    </div> : empty ? <div className="chat-welcome-scroll"><div className="chat-welcome">
      <button type="button" className="chat-team-pill" onClick={() => setTeamOpen(true)} aria-label="View your coding team">
        <span className="chat-team-avatars">{roles.map(role => <span key={role.id}><AgentIcon agentId={role.id} size={23} /></span>)}</span>
        <span>Your coding team</span><ArrowUpRight size={14} aria-hidden="true" />
      </button>
      <h1>What should we build?</h1>
      <p className="chat-welcome-subtitle">Ask a question, explore an idea, or give your team a goal.</p>
      <div className="chat-welcome-composer">{composer}</div>
      {suggestions.length > 0 && <div className="chat-suggestions" aria-label="Example goals">
        {suggestions.slice(0, MAX_SUGGESTIONS).map((suggestion, index) => {
          const Icon = [PanelTop, TerminalSquare, Code2][index % 3]!;
          return <button type="button" key={suggestion} className="chat-suggestion" onClick={() => {
            setDraft(suggestion); document.getElementById('orchestrator-prompt')?.focus();
          }}><Icon size={20} aria-hidden="true" /><span>{suggestion}</span><ArrowUpRight size={15} aria-hidden="true" className="chat-suggestion-arrow" /></button>;
        })}
      </div>}
    </div></div> : <>
      <div className="chat-messages-scroll" ref={scrollRef} onScroll={onScroll} aria-label="Conversation">
        <div className="chat-messages"><SpaceBetween size="l">{items.map((it, i) => {
          if (it.kind === 'user') return <UserBubble key={i} text={it.text} />;
          if (it.kind === 'assistant') return <AssistantBubble key={i} text={it.text} streaming={streaming && i === items.length - 1} />;
          if (it.kind === 'tool') return <StepperToolRow key={i} name={it.name} status={it.status} />;
          if (it.kind === 'reasoning') return <StepperReasoningBlock key={i} text={it.text} live={streaming && i === items.length - 1} />;
          return <RunCard key={i} runId={it.runId} runKind={it.runKind} compact />;
        })}</SpaceBetween></div>
      </div>
      <div className="chat-bottom-composer">
        {!isAtBottom && <button type="button" className="chat-latest" onClick={() => scrollToBottom()} aria-label="Jump to latest activity"><ArrowDown size={18} aria-hidden="true" /></button>}
        {composer}
      </div>
    </>}
    <Modal visible={teamOpen} onDismiss={() => setTeamOpen(false)} header="Team and connections" closeAriaLabel="Close team details"
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
        <Button variant="primary" onClick={() => nav('/settings?tab=connections')}>Manage connections</Button>
      </SpaceBetween></Box>}>{teamDetails}</Modal>
    <Modal visible={historyOpen} onDismiss={() => setHistoryOpen(false)} header="History" size="large" closeAriaLabel="Close history">
      <Tabs activeTabId={historyTab} onChange={({ detail }) => setHistoryTab(detail.activeTabId)} tabs={[
        { id: 'conversations', label: 'Conversations', content: chats.length ? <div className="chat-history-list">
          {chats.map(chat => <div key={chat.id} className="chat-history-row">
            <button type="button" className="chat-history-open" disabled={streaming} onClick={() => { nav(`/chat/c/${chat.id}`); setHistoryOpen(false); }}>
              <span>{chat.title}</span><time>{new Date(chat.updatedAt).toLocaleString()}</time>
            </button>
            <button type="button" className="chat-icon-button" disabled={streaming} aria-label={`Remove conversation: ${chat.title}`}
              onClick={() => { setHistoryOpen(false); setDeleteChatId(chat.id); }}><Trash2 size={16} aria-hidden="true" /></button>
          </div>)}
        </div> : <div className="chat-history-empty"><History size={28} aria-hidden="true" /><strong>No conversations yet</strong><p>Your chats will appear here after your first message.</p></div> },
        { id: 'builds', label: 'Builds', content: <BuildHistory onOpen={() => setHistoryOpen(false)} /> },
      ]} />
    </Modal>
    <Modal visible={deleteChatId !== null} onDismiss={() => setDeleteChatId(null)} header="Remove conversation?" closeAriaLabel="Cancel removal"
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" onClick={() => setDeleteChatId(null)}>Cancel</Button>
        <Button variant="primary" onClick={() => { if (deleteChatId) { removeChat(deleteChatId); if (deleteChatId === conversationId) newChat(); } setDeleteChatId(null); }}>Remove</Button>
      </SpaceBetween></Box>}>This removes the conversation from this browser. Recorded builds and pull requests are retained.</Modal>
  </section>;
}

function BuildHistory({ onOpen }: { onOpen?: () => void }) {
  const nav = useNavigate();
  const base = useHref('/').replace(/\/$/, '');
  const [runs, setRuns] = useState<Awaited<ReturnType<typeof listRuns>>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try { setRuns(await listRuns()); setError(''); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not load builds.'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); const t = setInterval(() => { if (!document.hidden) void refresh(); }, 10000); return () => clearInterval(t); }, [refresh]);
  return <SpaceBetween size="l">
    {error && <Alert type="error" header="Could not refresh build history">{error}</Alert>}
    <ResourceTable title="Builds" description="Runs recorded by this host. Builds submitted to the deployed coordinator have a separate history."
      items={runs} loading={loading} trackBy="run_id" sortingField="created_at" descending
      searchText={r => `${r.run_id} ${r.task} ${r.status} ${r.phase}`}
      actions={<Button iconName="refresh" ariaLabel="Refresh build history" onClick={() => void refresh()} />}
      empty={<SpaceBetween size="s"><Box variant="strong">{error ? 'Build history unavailable' : 'No builds recorded'}</Box>
        <Box color="text-body-secondary">{error ? 'Retry when the host connection is available.' : 'Send a goal from the build workspace to start a run.'}</Box></SpaceBetween>}
      columns={[
        { id: 'run', header: 'Build ID', sortingField: 'run_id', cell: r => <Link href={`${base}/chat/${r.run_id}`}
          onFollow={e => { e.preventDefault(); onOpen?.(); nav(`/chat/${r.run_id}`); }}>{r.run_id}</Link> },
        { id: 'task', header: 'Goal', sortingField: 'task', cell: r => r.task },
        { id: 'status', header: 'Status', sortingField: 'status', cell: r => <StatusIndicator type={r.status === 'failed' ? 'error' : r.status === 'needs_human' ? 'warning' : r.status === 'passed' ? 'success' : 'in-progress'}>
          {r.status === 'passed' ? 'Checks completed' : r.status.replaceAll('_', ' ')}</StatusIndicator> },
        { id: 'phase', header: 'Phase', cell: r => PHASE_LABEL[r.phase] || r.phase || '—' },
        { id: 'created', header: 'Created', sortingField: 'created_at', cell: r => r.created_at ? new Date(r.created_at).toLocaleString() : 'Not recorded' },
      ]} />
  </SpaceBetween>;
}

// ── Run card ──────────────────────────────────────────────────────────────────

// memo: RunCard owns its own polling loop, so it must not be torn down/
// re-rendered every time the parent transcript updates from a streaming token.
// Its only props are the stable runId/runKind.
const RunCard = memo(function RunCard({ runId, runKind, compact = false }: { runId: string; runKind: string; compact?: boolean }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [pollError, setPollError] = useState('');

  useEffect(() => {
    let cancelled = false;
    let finished = false;
    let misses = 0;
    let hasLoaded = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setRun(null);
    setResult(null);
    setNotFound(false);
    setPollError('');
    const tick = async () => {
      try {
        // Independent reads run together. A missing terminal stream must not
        // discard the authoritative run record.
        const [detailResult, terminalResult] = await Promise.allSettled([
          getRun(runId), getRunTerminals(runId),
        ]);
        if (cancelled) return;
        if (detailResult.status === 'rejected') throw detailResult.reason;
        const detail = detailResult.value;
        const merged = terminalResult.status === 'fulfilled'
          ? { ...detail, terminals: terminalResult.value.terminals, roleEvents: terminalResult.value.events }
          : detail;
        misses = 0;
        hasLoaded = true;
        setPollError('');
        setRun(merged);
        if (TERMINAL_STATUSES.includes(detail.status)) {
          finished = true;
          try {
            const next = await getRunResult(runId);
            if (!cancelled) setResult(next);
          } catch { /* the run record still contains the terminal evidence */ }
        }
      } catch (error) {
        if (cancelled) return;
        if (!hasLoaded && error instanceof ApiError && error.status === 404) {
          misses += 1;
          if (misses >= 3) {
            finished = true;
            setNotFound(true);
          }
        } else {
          misses = 0;
          setPollError(hasLoaded
            ? 'Could not refresh this run. The last recorded evidence remains below; retrying.'
            : 'Could not load this run. Check the console connection; retrying.');
        }
      } finally {
        // Schedule after completion so a slow response cannot overlap the next poll.
        if (!cancelled && !finished) timer = setTimeout(tick, 2000);
      }
    };
    void tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [runId]);

  const nav = useNavigate();
  const base = useHref('/').replace(/\/$/, '');
  if (notFound) return <Alert type="warning" header="Build not found">
    This host has no record of <code>{runId}</code>. A build submitted to the deployed coordinator has a separate history.
  </Alert>;
  if (!run) return pollError ? <ErrorState error={pollError} /> : <LoadingState />;
  const live = !TERMINAL_STATUSES.includes(run.status);
  const decision = presentRunDecision(run);
  const terminals = run.terminals;
  if (compact) return <Container header={<Header variant="h3" actions={<Link href={`${base}/chat/${runId}`}
    onFollow={e => { e.preventDefault(); nav(`/chat/${runId}`); }}>View build</Link>}>{runKind === 'build' ? 'Build' : runKind}</Header>}>
    <SpaceBetween size="s">
      <StatusIndicator type={decision.tone === 'danger' ? 'error' : decision.tone === 'waiting' ? 'warning' : decision.tone === 'success' ? 'success' : 'in-progress'}>{decision.title}</StatusIndicator>
      <Box color="text-body-secondary">{decision.detail}</Box>
      <Box fontSize="body-s"><code>{runId}</code></Box>
      {live && PHASE_LABEL[run.phase] && <Box color="text-body-secondary">{PHASE_LABEL[run.phase]}</Box>}
      {pollError && <Alert type="warning">{pollError}</Alert>}
    </SpaceBetween>
  </Container>;
  return <div id={`run-${runId}`}><SpaceBetween size="l">
    {pollError && <Alert type="warning">{pollError}</Alert>}
    {run.source === 'persisted' && <Alert type="info" header="Recorded build">
      Pull requests and checks were loaded from saved history. Terminal sessions from the previous console process are no longer attached.
    </Alert>}
    <RunDetailPanel run={run} />
    {live && <Container header={<Header variant="h2">Agent activity</Header>}>
      <RunActivityRows route={run.route} progress={run.progress} roleEvents={run.roleEvents} live={live} />
    </Container>}
    {terminals && Object.keys(terminals).length > 0 && <Container header={<Header variant="h2" description="Live Runtime sessions and recorded host output.">Terminal output</Header>}>
      <RunTerminalPane terminals={terminals} live={live} />
    </Container>}
    {!live && <RunChangesPane runId={runId} />}
    {result && <OrchestratorVerdict result={result} />}
  </SpaceBetween></div>;
});

// The terminal surface for a run. An agent lane is the captured output of that
// role's bounded headless AgentCore shell. Old persisted runs may still carry a
// live_session_id from the retired muxed dispatch path; LiveSessionPane keeps
// those records readable. The
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
    const unsub = subscribeOutput(
      sessionId,
      (s, replay) => termRef.current?.write(s, replay),
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
  terminals,
}: {
  terminals: Record<string, TerminalEntry[]>;
  live: boolean;
}) {
  const [activeLane, setActiveLane] = useState('');
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
          : <span className="text-muted-foreground">Waiting for output…</span>
        }
      </pre>
    );
  };

  return <Tabs tabs={lanes.map(lane => ({
    id: lane, label: LANE_LABEL[lane] ?? agentInstanceLabel(lane),
    content: <TermPane entries={terminals[lane]!} />,
  }))} activeTabId={lanes.includes(activeLane) ? activeLane : defaultLane}
    onChange={({ detail }) => setActiveLane(detail.activeTabId)} />;
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

  return <Container header={<Header variant="h2" counter={`(${diff.files.length})`}
    description={`${totalAdded} lines added, ${totalRemoved} removed${diff.branch ? ` · ${diff.branch}` : ''}`}>Changed files</Header>}>
    <SpaceBetween size="s">{diff.files.map(f => <ExpandableSection key={f.path} headerText={f.path}
      defaultExpanded={diff.files.length <= 2}><DiffBody patch={f.patch} /></ExpandableSection>)}</SpaceBetween>
  </Container>;
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
  const prs = recordedPullRequests(result);
  return <ExpandableSection headerText="Terminal result" variant="container">
    <SpaceBetween size="m">
      <KeyValuePairs columns={3} items={[
        { label: 'Latest executable check', value: gateResultLabel(result.gate) },
        { label: 'Check executions', value: result.gate_history?.length ?? 'Not recorded' },
        { label: 'Review', value: result.review?.state?.replaceAll('_', ' ') || 'Not recorded' },
      ]} />
      {result.fail_reason && <Alert type="error" header="Recorded reason">{result.fail_reason}</Alert>}
      <SpaceBetween direction="horizontal" size="m">{prs.filter(row => row.pr_url).map(row =>
        <Link key={row.work_id} external href={row.pr_url!}>Open {row.role || row.agent || 'recorded'} pull request</Link>)}</SpaceBetween>
      {result.next_action && <Box>{result.next_action}</Box>}
    </SpaceBetween>
  </ExpandableSection>;
}

const UserBubble = memo(function UserBubble({ text }: { text: string }) {
  return <div className="chat-message-user" aria-label="Your message"><div>{text}</div></div>;
});

function AssistantBubble({ text, streaming }: { text: string; streaming: boolean }) {
  if (!text && !streaming) return null;
  return <div className="chat-message-assistant" aria-label="Coordinator message">
    {!text ? <StatusIndicator type="loading">Thinking…</StatusIndicator>
      : <div className={cn('prose-chat', streaming && 'md-stream')}><ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>{text}</ReactMarkdown></div>}
  </div>;
}

const StepperToolRow = memo(function StepperToolRow({ name, status }: { name: string; status: 'running' | 'done' }) {
  return <ExpandableSection headerText={<StatusIndicator type={status === 'running' ? 'loading' : 'info'}>{TOOL_LABEL[name] ?? name}</StatusIndicator>}>
    <KeyValuePairs columns={2} items={[{ label: 'Tool', value: <code>{name}</code> },
      { label: 'Execution', value: status === 'running' ? 'Running' : 'Completed' }]} />
  </ExpandableSection>;
});

const StepperReasoningBlock = memo(function StepperReasoningBlock({ text, live }: { text: string; live: boolean }) {
  return <ExpandableSection headerText={live ? 'Coordinator reasoning…' : 'Coordinator reasoning'}>
    <Box color="text-body-secondary"><div className="whitespace-pre-wrap">{text}</div></Box>
  </ExpandableSection>;
});
