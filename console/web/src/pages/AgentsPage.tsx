import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Card, CardHeader, CardTitle, CardContent,
  Badge, Button, Input,
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '@foxl/ui';
import { ChevronDown, Link2, Loader2, Maximize2, Minimize2, Plus, X } from 'lucide-react';
import { AgentIcon } from '../components/AgentIcon';
import { Terminal, type TerminalHandle } from '../components/Terminal';
import { SectionHeader } from '../shared';
import { getRuntimes, wireRuntime, clearRuntime, type RuntimeStatus } from '../api';
import { agentRole } from './agents/environments';
import {
  getSessions, openSession, subscribeOutput, sendInput, resizeTerminal, getBuffer, closeSession,
  syncServerSessions,
  type SessionEntry,
} from '../hooks/useSessionStore';

export function AgentsPage() {
  const { env } = useParams();
  const selectedRole = agentRole(env);

  // A role can host >1 runtime instance under one sidebar entry (Claude Code =
  // backend builder + validator). Pick WHICH instance this page is showing; the
  // dropdown below switches it. Single-instance roles (OpenCode) just pin their one.
  const [instanceId, setInstanceId] = useState<string>(selectedRole.instances[0]!.id);
  useEffect(() => {
    setInstanceId(selectedRole.instances[0]!.id);
  }, [selectedRole.id]);
  const selectedInstance =
    selectedRole.instances.find((i) => i.id === instanceId) ?? selectedRole.instances[0]!;
  const hasMultipleInstances = selectedRole.instances.length > 1;
  // `selected` is the ACTIVE runtime instance: its id is the backend role key the
  // /api/dev + wiring endpoints use, its label/blurb describe this instance.
  const selected = selectedInstance;

  const [runtimes, setRuntimes] = useState<RuntimeStatus | null>(null);
  const [draft, setDraft] = useState('');
  const [wiring, setWiring] = useState(false);
  const [error, setError] = useState('');
  const [fullscreen, setFullscreen] = useState(false);

  // Open session tabs for the selected agent + which one is active. Sessions live
  // in the global store (persist across navigation); this mirrors the store's
  // list for the current agent so the tab bar re-renders on open/close.
  const [tabs, setTabs] = useState<SessionEntry[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState('');

  const refreshRuntimes = useCallback(() => {
    getRuntimes().then(setRuntimes).catch(() => {});
  }, []);

  useEffect(() => {
    refreshRuntimes();
    const t = setInterval(refreshRuntimes, 5000);
    return () => clearInterval(t);
  }, [refreshRuntimes]);

  const role = runtimes?.roles.find((r) => r.role === selected.id);
  const isWired = role?.wired ?? false;
  const currentArn = role?.arn ?? '';
  const instanceList = role?.instances ?? [];
  const isFleet = instanceList.length > 1;

  // Which wired instance a NEW session opens against (only meaningful for a fleet
  // of N). Defaults to the first; the attendee can switch before clicking +.
  const [targetArn, setTargetArn] = useState<string>('');
  useEffect(() => {
    // Reset the target to the first instance whenever the agent or its fleet changes.
    setTargetArn(instanceList[0]?.arn ?? '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected.id, instanceList.map((i) => i.arn).join(',')]);

  // Guards that stop the double-open race the two effects used to cause:
  //  - openingRef: a SYNCHRONOUS lock. `opening` (state) updates async, so two
  //    effect runs could both read opening=false and each open a session (that
  //    was the "Session 2 + Session 3" bug). A ref flips synchronously.
  //  - userClosedAllRef: remembers that the human closed the LAST tab for this
  //    agent, so auto-open does not immediately reopen one (the "close one and
  //    another appears" bug). Reset when the agent changes.
  const openingRef = useRef(false);
  const userClosedAllRef = useRef(false);

  const openTab = useCallback(async (instanceArn?: string) => {
    if (openingRef.current) return;
    openingRef.current = true;
    setOpening(true);
    setOpenError('');
    try {
      const entry = await openSession(selected.id, { rows: 24, cols: 80 }, instanceArn);
      userClosedAllRef.current = false;
      setTabs(getSessions(selected.id));
      setActiveTab(entry.id);
    } catch (e) {
      setOpenError(e instanceof Error ? e.message : 'Failed to open session.');
    } finally {
      openingRef.current = false;
      setOpening(false);
    }
  }, [selected.id]);

  // One effect owns the tab list for the selected agent: seed from the store,
  // then poll the SERVER registry (so an orchestrator-opened live PTY shows up as
  // a tab), and auto-open exactly ONE session when the agent is wired and none is
  // open -- unless the human just closed them all. openingRef makes the auto-open
  // single-flight, so no second tab races in.
  useEffect(() => {
    let stop = false;
    userClosedAllRef.current = false;
    const existing = getSessions(selected.id);
    setTabs(existing);
    setActiveTab(existing.length ? existing[existing.length - 1]!.id : null);

    const tick = async () => {
      if (stop) return;
      await syncServerSessions(selected.id);
      if (stop) return;
      const next = getSessions(selected.id);
      setTabs(next);
      setActiveTab((cur) =>
        cur && next.some((s) => s.id === cur)
          ? cur
          : (next.length ? next[next.length - 1]!.id : null));
      // Auto-open one session only when wired, none open, the human didn't close
      // them all, and no open is already in flight.
      if (isWired && next.length === 0 && !userClosedAllRef.current && !openingRef.current) {
        void openTab();
      }
    };
    void tick();
    const t = setInterval(tick, 3000);
    return () => { stop = true; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected.id, isWired, openTab]);

  const closeTab = useCallback((id: string) => {
    closeSession(id);
    const remaining = getSessions(selected.id);
    // If that was the last tab, remember the human wants NONE open so the
    // auto-open effect does not immediately reopen one.
    if (remaining.length === 0) userClosedAllRef.current = true;
    setTabs(remaining);
    setActiveTab((cur) => (cur === id ? (remaining.length ? remaining[remaining.length - 1]!.id : null) : cur));
  }, [selected.id]);

  // A restored tab whose backend session is gone (server restart): the store
  // already pruned it, so just resync the tab bar to what survives.
  const pruneTab = useCallback((id: string) => {
    const remaining = getSessions(selected.id);
    setTabs(remaining);
    setActiveTab((cur) => (cur === id ? (remaining.length ? remaining[remaining.length - 1]!.id : null) : cur));
  }, [selected.id]);

  // Map each wired ARN to a 1-based fleet index, so a tab on a fleet of N can
  // show "Session 2 · #1" (which deployed instance it is talking to).
  const arnIndex = (arn: string) => {
    const i = instanceList.findIndex((inst) => inst.arn === arn);
    return i >= 0 ? i + 1 : 0;
  };

  const tabBar = (
    <div className="flex items-center gap-1 overflow-x-auto">
      {tabs.map((t) => (
        <div
          key={t.id}
          className={`flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs ${
            activeTab === t.id ? 'border-border bg-card font-medium text-foreground' : 'border-transparent text-muted-foreground hover:bg-accent'
          }`}
        >
          <button onClick={() => setActiveTab(t.id)} className="flex items-center gap-1.5">
            <AgentIcon agentId={selected.id} size={12} />
            Session {t.label}
            {t.openedBy === 'orchestrator' && (
              <span className="rounded bg-primary/10 px-1 text-[10px] font-medium text-primary">run</span>
            )}
            {isFleet && arnIndex(t.runtimeArn) > 0 && (
              <span className="text-muted-foreground">{`· #${arnIndex(t.runtimeArn)}`}</span>
            )}
          </button>
          <button onClick={() => closeTab(t.id)} className="rounded p-0.5 hover:bg-muted" title="Close session">
            <X className="size-3" />
          </button>
        </div>
      ))}
      {/* Fleet of N: pick WHICH instance the next + opens against. */}
      {isFleet && (
        <select
          value={targetArn}
          onChange={(e) => setTargetArn(e.target.value)}
          className="h-7 shrink-0 rounded-md border border-border bg-card px-1.5 text-xs text-muted-foreground"
          title="Which instance a new session connects to"
        >
          {instanceList.map((inst, i) => (
            <option key={inst.arn} value={inst.arn}>
              {`#${i + 1}${inst.description ? ` ${inst.description}` : ''}`}
            </option>
          ))}
        </select>
      )}
      <Button
        variant="ghost" size="sm" className="h-7 shrink-0 px-2"
        onClick={() => openTab(isFleet ? targetArn : undefined)}
        disabled={opening || !isWired} title="New session"
      >
        {opening ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
      </Button>
    </div>
  );

  async function handleWire() {
    const url = draft.trim();
    if (!url || wiring) return;
    setWiring(true);
    setError('');
    try {
      const next = await wireRuntime(selected.id, url);
      if (next.error) setError(next.error);
      else { setRuntimes(next); setDraft(''); }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to wire runtime.');
    } finally {
      setWiring(false);
    }
  }

  async function handleClear() {
    setWiring(true);
    try {
      // Close every open session for this agent before unwiring.
      getSessions(selected.id).forEach((s) => closeSession(s.id));
      setTabs([]);
      setActiveTab(null);
      setRuntimes(await clearRuntime(selected.id));
      setDraft('');
    } catch { /* keep current state */ } finally {
      setWiring(false);
    }
  }

  if (fullscreen && isWired && activeTab) {
    return (
      <div className="absolute inset-0 z-30 flex flex-col bg-background">
        <div className="flex items-center gap-2 border-b border-border px-4 py-2">
          <AgentIcon agentId={selectedRole.id} size={16} />
          <span className="text-sm font-medium">
            {selectedRole.label}
            {hasMultipleInstances && <span className="text-muted-foreground"> · {selectedInstance.label}</span>}
          </span>
          <div className="ml-4 flex-1">{tabBar}</div>
          <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => setFullscreen(false)}>
            <Minimize2 className="size-4" />
          </Button>
        </div>
        <div className="relative flex-1">
          {tabs.map((t) => (
            <div key={t.id} className={`absolute inset-0 ${activeTab === t.id ? '' : 'hidden'}`}>
              <AgentTerminal sessionId={t.id} fullHeight active={activeTab === t.id} onGone={() => pruneTab(t.id)} />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="animate-enter-up mx-auto w-full max-w-6xl space-y-6 px-6 py-8">
      <SectionHeader
        title="Agents"
        subtitle="Connect to each agent's AgentCore Runtime. Run agentcore dev locally or wire a deployed ARN."
      />

      <Card>
        <CardHeader className="gap-1.5">
          <CardTitle className="flex items-center gap-2">
            <AgentIcon agentId={selectedRole.id} size={18} />
            {selectedRole.label}
            {/* One role, >1 runtime instance (Claude Code = backend + validator):
                switch between them here. The two are DISTINCT runtimes (different
                role ids + ARNs); the dropdown makes that legible instead of two
                identical "Claude Code" sidebar rows. */}
            {hasMultipleInstances && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" size="sm" className="ml-1 h-7 gap-1.5 px-2 text-xs font-normal">
                    {selectedInstance.label}
                    <ChevronDown className="size-3.5 text-muted-foreground" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-72">
                  {selectedRole.instances.map((inst) => {
                    const r = runtimes?.roles.find((x) => x.role === inst.id);
                    return (
                      <DropdownMenuItem
                        key={inst.id}
                        onSelect={() => setInstanceId(inst.id)}
                        className="flex flex-col items-start gap-0.5 py-2"
                      >
                        <span className="flex items-center gap-1.5 text-sm font-medium">
                          {inst.label}
                          {r?.wired
                            ? <span className="rounded bg-primary/10 px-1 text-[10px] font-medium text-primary">connected</span>
                            : <span className="rounded bg-muted px-1 text-[10px] text-muted-foreground">not wired</span>}
                        </span>
                        <span className="text-[11px] text-muted-foreground">{inst.blurb}</span>
                        {r?.wired && r.arn && (
                          <code className="mt-0.5 max-w-full truncate font-mono text-[10px] text-muted-foreground/70">{r.arn}</code>
                        )}
                      </DropdownMenuItem>
                    );
                  })}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            {isWired ? (
              <Badge variant="success" className="ml-2">connected</Badge>
            ) : (
              <Badge variant="outline" className="ml-2 text-muted-foreground">not wired</Badge>
            )}
            {isWired && activeTab && (
              <Button variant="ghost" size="sm" className="ml-auto h-7 px-2" onClick={() => setFullscreen(true)} title="Fullscreen">
                <Maximize2 className="size-4" />
              </Button>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {isWired ? (
            <>
              {tabBar}
              <div className="relative h-[440px]">
                {tabs.length === 0 ? (
                  <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-border bg-muted/30">
                    <p className="text-sm text-muted-foreground">
                      {openError || 'Opening a session...'}
                    </p>
                  </div>
                ) : (
                  // Keep every open tab mounted (hidden) so its terminal + buffer
                  // survive tab switches; only the active one is visible.
                  tabs.map((t) => (
                    <div key={t.id} className={`absolute inset-0 ${activeTab === t.id ? '' : 'hidden'}`}>
                      <AgentTerminal sessionId={t.id} active={activeTab === t.id} onGone={() => pruneTab(t.id)} />
                    </div>
                  ))
                )}
              </div>
            </>
          ) : (
            <>
              <div className="flex h-[400px] items-center justify-center rounded-lg border border-dashed border-border bg-muted/30">
                <p className="text-sm text-muted-foreground">
                  No runtime connected. Run <code className="font-mono">agentcore dev</code> and paste the URL below.
                </p>
              </div>
              <div className="flex gap-2">
                <Input
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder="https:// or arn:aws:bedrock-agentcore:..."
                  className="text-sm"
                  onKeyDown={(e) => { if (e.key === 'Enter') handleWire(); }}
                />
                <Button onClick={handleWire} disabled={!draft.trim() || wiring} size="sm">
                  {wiring ? <Loader2 className="size-4 animate-spin" /> : 'Connect'}
                </Button>
              </div>
              {error && <p className="text-xs text-destructive">{error}</p>}
            </>
          )}

          {isWired && currentArn && (
            <div className="flex items-center gap-2 rounded-md bg-muted/50 px-3 py-1.5">
              <Link2 className="size-3 shrink-0 text-muted-foreground" />
              <code className="flex-1 break-all font-mono text-[11px] text-muted-foreground">{currentArn}</code>
              <Button variant="ghost" size="sm" className="h-6 shrink-0 px-2 text-xs" onClick={handleClear} disabled={wiring}>
                Disconnect
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// One terminal bound to a specific session (tab) id. The session already exists
// in the store (the page opens it before mounting this); this component just
// attaches an xterm, replays the buffer, and subscribes to the live SSE stream.
function AgentTerminal({ sessionId, fullHeight = false, active = true, onGone }: { sessionId: string; fullHeight?: boolean; active?: boolean; onGone?: () => void }) {
  const termRef = useRef<TerminalHandle>(null);
  const mounted = useRef(false);
  // Hold the SSE unsubscribe so the useEffect cleanup closes the stream when this
  // terminal unmounts. The async IIFE's own return is NOT the effect cleanup, so
  // without this ref every unmount leaks an EventSource; ~6 leaked streams hit
  // the per-host connection cap and the whole console appears to hang.
  const unsubRef = useRef<(() => void) | null>(null);

  // Refit whenever this tab becomes active. A tab mounted while hidden
  // (display:none) measures 0x0, so its first fit is wrong; when it is revealed
  // we re-fit and push the real cols/rows to the runtime PTY so the TUI reflows
  // to the pane instead of staying at the tiny initial grid.
  useEffect(() => {
    if (!active || !mounted.current) return;
    const id = requestAnimationFrame(() => {
      const size = termRef.current?.fit();
      if (size) resizeTerminal(sessionId, size);
      termRef.current?.focus();
    });
    return () => cancelAnimationFrame(id);
  }, [active, sessionId]);

  useEffect(() => {
    if (mounted.current) return;
    mounted.current = true;
    // Only push the initial winsize when this tab is actually visible. A tab
    // opened in the background (tab 2+) measures 0x0 while hidden, so fit() now
    // returns the default without resizing; the active-refit effect below pushes
    // the real cols/rows the moment the tab is revealed (R12).
    if (active) {
      const size = termRef.current?.fit() ?? { rows: 24, cols: 80 };
      resizeTerminal(sessionId, size);
    }
    const buf = getBuffer(sessionId);
    if (buf) termRef.current?.write(buf);
    unsubRef.current = subscribeOutput(sessionId, (s) => termRef.current?.write(s), () => onGone?.());
    if (active) termRef.current?.focus();
    return () => { unsubRef.current?.(); unsubRef.current = null; mounted.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  return (
    <div className={fullHeight ? 'h-full' : 'h-full overflow-hidden rounded-lg border border-border'}>
      <Terminal
        ref={termRef}
        onData={(d) => sendInput(sessionId, d)}
        onResize={(s) => resizeTerminal(sessionId, s)}
      />
    </div>
  );
}
