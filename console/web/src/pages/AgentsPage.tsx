import { useCallback, useEffect, useRef, useState } from 'react';
import { Navigate, useNavigate, useHref, useSearchParams } from 'react-router-dom';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import ContentLayout from '@cloudscape-design/components/content-layout';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Modal from '@cloudscape-design/components/modal';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Tabs from '@cloudscape-design/components/tabs';
import { AgentIcon } from '../components/AgentIcon';
import { Terminal, type TerminalHandle } from '../components/Terminal';
import { getRuntimes, wireRuntime, type RuntimeStatus } from '../api';
import { agentRoles, loadAgentRoles, type AgentRole } from './agents/environments';
import {
  getSessions, openSession, subscribeOutput, sendInput, resizeTerminal, closeSession,
  syncServerSessions, type SessionEntry,
} from '../hooks/useSessionStore';

export function AgentsPage() {
  const navigate = useNavigate();
  const base = useHref('/').replace(/\/$/, '');
  const [params, setParams] = useSearchParams();
  const [roles, setRoles] = useState<AgentRole[]>(agentRoles);
  const [rosterLoading, setRosterLoading] = useState(!roles.length);
  const [runtimes, setRuntimes] = useState<RuntimeStatus | null>(null);
  const [runtimeError, setRuntimeError] = useState('');
  const runtimeRequest = useRef(0);
  const showSessions = params.get('view') === 'sessions';
  const requestedAgent = params.get('agent');
  const selectedRole = roles.find(role => role.id === requestedAgent || role.instances.some(instance => instance.id === requestedAgent)) ?? roles[0];
  useEffect(() => {
    let live = true;
    void loadAgentRoles().then(next => { if (live) { setRoles(next); setRosterLoading(false); } });
    return () => { live = false; };
  }, []);
  const refreshRuntimes = useCallback(async () => {
    const id = ++runtimeRequest.current;
    try {
      const next = await getRuntimes();
      if (id === runtimeRequest.current) { setRuntimes(next); setRuntimeError(''); }
    } catch (e) {
      if (id === runtimeRequest.current) setRuntimeError(e instanceof Error ? e.message : 'Could not read runtime configuration.');
    }
  }, []);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!document.hidden) await refreshRuntimes();
      if (live) timer = setTimeout(poll, 5000);
    };
    void poll();
    return () => { live = false; runtimeRequest.current++; clearTimeout(timer); };
  }, [refreshRuntimes]);
  if (showSessions) return <Navigate to="/governance/activity?tab=sessions" replace />;
  return <ContentLayout header={<Header variant="h1"
    description="Work with your coding agents and manage their Runtime sessions."
    actions={<SpaceBetween direction="horizontal" size="xs">
      <Button onClick={() => navigate('/governance/activity?tab=sessions')}>Manage sessions</Button>
      <Button href={`${base}/settings?tab=connections`}
        onFollow={event => { event.preventDefault(); navigate('/settings?tab=connections'); }}>Manage connections</Button>
    </SpaceBetween>}>Agents</Header>}>
    <SpaceBetween size="l">
        {runtimeError && <Alert type="error" header="Runtime configuration unavailable"
          action={<Button onClick={() => void refreshRuntimes()}>Retry</Button>}>{runtimeError}</Alert>}
        {rosterLoading ? <StatusIndicator type="loading">Loading agent roles</StatusIndicator>
          : !selectedRole ? <Container><Box textAlign="center" padding="l"><SpaceBetween size="m">
            <Box variant="h3">No agents returned</Box><Box color="text-body-secondary">Check the host connection and the served roster.</Box>
            <Button onClick={() => { setRosterLoading(true); void loadAgentRoles().then(next => { setRoles(next); setRosterLoading(false); }); }}>Retry</Button>
          </SpaceBetween></Box></Container>
          : <Tabs activeTabId={selectedRole.id} ariaLabel="Coding agents"
            onChange={({ detail }) => setParams({ agent: detail.activeTabId })}
            tabs={roles.map(role => ({
              id: role.id, href: `${base}/agents?agent=${encodeURIComponent(role.id)}`,
              label: <span className="agent-tab-label"><span aria-hidden="true"><AgentIcon agentId={role.id} size={20} /></span>{role.label}</span>,
              content: <AgentWorkspace key={role.id} role={role} requestedInstance={requestedAgent}
                runtimes={runtimes} onRuntimesChange={setRuntimes} />,
            }))} />}
    </SpaceBetween>
  </ContentLayout>;
}

function AgentWorkspace({ role, requestedInstance, runtimes, onRuntimesChange }: {
  role: AgentRole; requestedInstance: string | null;
  runtimes: RuntimeStatus | null; onRuntimesChange: (value: RuntimeStatus) => void;
}) {
  const [instanceId, setInstanceId] = useState(() => role.instances.find(instance => instance.id === requestedInstance)?.id || role.instances[0]!.id);
  const selected = role.instances.find(instance => instance.id === instanceId) ?? role.instances[0]!;
  const agentId = selected.id;
  const activeAgent = useRef(agentId);
  activeAgent.current = agentId;
  useEffect(() => { activeAgent.current = agentId; return () => { activeAgent.current = ''; }; }, [agentId]);
  const [draft, setDraft] = useState('');
  const [wiring, setWiring] = useState(false);
  const [wireError, setWireError] = useState('');
  const [connectOpen, setConnectOpen] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setFullscreen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fullscreen]);
  const [tabs, setTabs] = useState<SessionEntry[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState('');
  const openingRef = useRef(false);
  const connection = runtimes?.roles.find(item => item.role === agentId);
  const isWired = connection?.wired ?? false;
  const instances = connection?.instances ?? (connection?.arn ? [{ arn: connection.arn, description: connection.description }] : []);
  const [targetArn, setTargetArn] = useState('');
  const terminalAvailable = isWired && (targetArn || connection?.arn || '').startsWith('arn:');
  const instanceArns = instances.map(instance => instance.arn).join(',');
  useEffect(() => { setTargetArn(previous => instances.some(instance => instance.arn === previous) ? previous : instances[0]?.arn || ''); }, [agentId, instanceArns]);

  // Opening a page only reads the registry. Starting a Runtime requires Open session.
  const openTab = useCallback(async () => {
    if (openingRef.current || !terminalAvailable) return;
    openingRef.current = true; setOpening(true); setOpenError('');
    try {
      const entry = await openSession(agentId, { rows: 24, cols: 80 }, targetArn || undefined);
      if (activeAgent.current === agentId) { setTabs(getSessions(agentId)); setActiveTab(entry.id); }
    } catch (e) { if (activeAgent.current === agentId) setOpenError(e instanceof Error ? e.message : 'Could not open the session.'); }
    finally { openingRef.current = false; if (activeAgent.current === agentId) setOpening(false); }
  }, [agentId, terminalAvailable, targetArn]);
  useEffect(() => {
    setOpenError(''); setFullscreen(false);
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const existing = getSessions(agentId);
    setTabs(existing); setActiveTab(existing.at(-1)?.id || null);
    const tick = async () => {
      await syncServerSessions(agentId);
      if (cancelled) return;
      const next = getSessions(agentId);
      setTabs(next); setActiveTab(current => next.some(session => session.id === current) ? current : next.at(-1)?.id || null);
      timer = setTimeout(tick, 3000);
    };
    void tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [agentId]);
  const pruneTab = useCallback((id: string) => {
    const next = getSessions(agentId); setTabs(next);
    setActiveTab(current => current === id ? next.at(-1)?.id || null : current);
  }, [agentId]);
  const closeTab = useCallback(async (id: string) => {
    setOpenError('');
    try { await closeSession(id); pruneTab(id); }
    catch (e) { setOpenError(e instanceof Error ? e.message : 'Could not close the terminal.'); }
  }, [pruneTab]);
  async function connect() {
    if (!draft.trim() || wiring) return;
    setWiring(true); setWireError('');
    try {
      const next = await wireRuntime(agentId, draft.trim());
      if (next.error) throw new Error(next.error);
      onRuntimesChange(next); setDraft(''); setConnectOpen(false);
    } catch (e) { setWireError(e instanceof Error ? e.message : 'Could not connect the runtime.'); }
    finally { setWiring(false); }
  }
  return <SpaceBetween size="l">
    <Container header={<Header variant="h2" description={selected.blurb}
      actions={!isWired && runtimes ? <Button onClick={() => { setDraft(''); setWireError(''); setConnectOpen(true); }}>Connect runtime</Button> : undefined}>Runtime connection</Header>}>
      <SpaceBetween size="l">
        {role.instances.length > 1 && <FormField label="Role"><Select disabled={opening} selectedOption={{ value: selected.id, label: selected.label }}
          options={role.instances.map(instance => ({ value: instance.id, label: instance.label, description: instance.blurb }))}
          onChange={({ detail }) => setInstanceId(detail.selectedOption.value!)} /></FormField>}
        {instances.length > 1 && <FormField label="Runtime for new sessions"><Select disabled={opening} selectedOption={targetArn ? { value: targetArn, label: instances.find(instance => instance.arn === targetArn)?.description || targetArn } : null}
          options={instances.map(instance => ({ value: instance.arn, label: instance.description || instance.arn }))}
          onChange={({ detail }) => setTargetArn(detail.selectedOption.value!)} /></FormField>}
        <KeyValuePairs columns={3} items={[
          { label: 'Role', value: selected.label },
          { label: 'Connection', value: <StatusIndicator type={!runtimes ? 'loading' : isWired ? 'success' : 'pending'}>{!runtimes ? 'Loading' : isWired ? 'Configured' : 'Not configured'}</StatusIndicator> },
          { label: 'Runtime ARN or URL', value: <code className="console-code">{targetArn || connection?.arn || 'Not configured'}</code> },
        ]} />
        {isWired && !terminalAvailable && <Alert type="info">This development URL supports dispatch. Interactive terminals require a deployed AgentCore Runtime ARN.</Alert>}
      </SpaceBetween>
    </Container>
    <div className={fullscreen ? 'agent-terminal-expanded' : 'agent-terminal-frame'}>
      <Container fitHeight={fullscreen} disableContentPaddings header={<Header variant="h2" counter={`(${tabs.length})`}
        description="Sessions opened here and by Chat share the same live terminal."
        actions={<SpaceBetween direction="horizontal" size="xs">
          {!!tabs.length && <Button iconName={fullscreen ? 'shrink' : 'expand'} ariaLabel={fullscreen ? 'Exit expanded terminal' : 'Expand terminal'} onClick={() => setFullscreen(value => !value)} />}
          <Button variant="primary" iconName="add-plus" loading={opening} disabled={!terminalAvailable || opening} onClick={() => void openTab()}>Open session</Button>
        </SpaceBetween>}>Live sessions</Header>}>
        {openError && <Box padding="m"><Alert type="error" header="Session request failed">{openError}</Alert></Box>}
        {tabs.length ? <Tabs fitHeight={fullscreen} activeTabId={activeTab || tabs[0]!.id} onChange={({ detail }) => setActiveTab(detail.activeTabId)}
          disableContentPaddings ariaLabel="Agent terminal sessions" tabs={tabs.map(session => ({
            id: session.id, label: `Session ${session.label}${session.openedBy === 'orchestrator' ? ' · Chat build' : ''}`,
            dismissible: true, dismissLabel: `Close terminal ${session.label}`, onDismiss: () => void closeTab(session.id),
            contentRenderStrategy: 'eager', content: <div className="agent-terminal-pane"><AgentTerminal sessionId={session.id} fullHeight
              active={activeTab === session.id} onGone={() => pruneTab(session.id)} /></div>,
          }))} /> : <Box padding="xxl" textAlign="center"><SpaceBetween size="m">
            <Box variant="h3">{opening ? 'Opening session' : isWired ? 'No open sessions' : 'Connect a runtime to start'}</Box>
            <Box color="text-body-secondary">{opening ? 'Waiting for the Runtime shell.' : terminalAvailable ? 'Open a session when you are ready to work with this agent.' : 'Use the deployed Runtime ARN from Lab 1 to open an interactive terminal.'}</Box>
            {opening && <StatusIndicator type="loading">Connecting</StatusIndicator>}
          </SpaceBetween></Box>}
      </Container>
    </div>
    <Modal visible={connectOpen} header={`Connect ${role.label}`} onDismiss={() => { if (!wiring) setConnectOpen(false); }} closeAriaLabel="Close runtime connection"
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs"><Button variant="link" disabled={wiring} onClick={() => setConnectOpen(false)}>Cancel</Button>
        <Button variant="primary" loading={wiring} disabled={!draft.trim()} onClick={() => void connect()}>Connect</Button></SpaceBetween></Box>}>
      <FormField label="Runtime ARN or development URL" errorText={wireError} description="Use the runtime deployed for this role in Lab 1.">
        <Input value={draft} disabled={wiring} autoComplete={false} onChange={({ detail }) => setDraft(detail.value)}
          onKeyDown={event => { if (event.detail.key === 'Enter') void connect(); }} />
      </FormField>
    </Modal>
  </SpaceBetween>;
}
// One terminal bound to a specific session (tab) id. The session already exists
// in the store (the page opens it before mounting this); this component just
// attaches an xterm, replays the buffer, and subscribes to the live SSE stream.
function AgentTerminal({ sessionId, fullHeight = false, active = true,
  onGone }: { sessionId: string; fullHeight?: boolean;
    active?: boolean; onGone?: () => void }) {
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
    unsubRef.current = subscribeOutput(
      sessionId, (s, replay) => termRef.current?.write(s, replay), () => onGone?.());
    if (active) termRef.current?.focus();
    return () => { unsubRef.current?.(); unsubRef.current = null; mounted.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  return (
    <div className={fullHeight ? 'h-full' : 'h-full overflow-hidden rounded-lg border border-border'}>
      <Terminal
        ref={termRef}
        connected
        onData={(d) => sendInput(sessionId, d)}
        onResize={(s) => resizeTerminal(sessionId, s)}
      />
    </div>
  );
}
