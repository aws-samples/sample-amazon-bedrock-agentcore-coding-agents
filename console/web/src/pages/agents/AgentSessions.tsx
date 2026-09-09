import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Link from '@cloudscape-design/components/link';
import Modal from '@cloudscape-design/components/modal';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { listSessions, stopSession, type SessionRow } from '../../api';
import { fmtTime, maskHandle } from '../../shared';
import { ResourceTable } from '../../shared/ResourceTable';
import { toast } from '../../components/ConsoleNotifications';
import { agentInstanceLabel } from './environments';
import { SessionDetailPanel } from './SessionDetailPanel';
import { useMediaQuery } from '../../hooks/useMediaQuery';

const WINDOWS = [
  { value: '1h', label: 'Last hour', minutes: 60 },
  { value: '24h', label: 'Last 24 hours', minutes: 1440 },
  { value: '7d', label: 'Last 7 days', minutes: 10080 },
  { value: 'all', label: 'All recorded sessions', minutes: undefined },
];
export function AgentSessions({ onStopped }: { onStopped?: () => void }) {
  const narrow = useMediaQuery('(max-width: 600px)');
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<SessionRow | null>(null);
  const [pendingStop, setPendingStop] = useState<string | null>(null);
  const [stopError, setStopError] = useState('');
  const [auditWarning, setAuditWarning] = useState('');
  const [stopped, setStopped] = useState<Record<string, boolean>>({});
  const [stopping, setStopping] = useState(false);
  const [windowKey, setWindowKey] = useState('all');
  const requestId = useRef(0);
  const refresh = useCallback(async () => {
    const id = ++requestId.current;
    const window = WINDOWS.find(w => w.value === windowKey)!;
    try {
      const records = await listSessions(window.minutes ? { window: window.minutes } : undefined);
      if (id === requestId.current) { setSessions(records); setError(''); }
    } catch (e) {
      if (id === requestId.current) setError(e instanceof Error ? e.message : 'Could not refresh sessions.');
    } finally { if (id === requestId.current) setLoading(false); }
  }, [windowKey]);
  useEffect(() => {
    setLoading(true); void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 10000);
    return () => { requestId.current++; clearInterval(timer); };
  }, [refresh]);
  async function stop() {
    if (!pendingStop || stopping) return;
    setStopping(true); setStopError(''); setAuditWarning('');
    try {
      const result = await stopSession(pendingStop);
      if (!result?.stopped) throw new Error(result?.error || 'The service did not confirm that the session stopped.');
      setStopped(current => ({ ...current, [pendingStop]: true })); setPendingStop(null);
      if (result.audit_recorded === false) setAuditWarning(result.audit_error || 'The session stopped, but the host could not save its audit record.');
      toast.success('Session stopped'); onStopped?.(); await refresh();
    } catch (e) { setStopError(e instanceof Error ? e.message : 'Could not stop the session.'); }
    finally { setStopping(false); }
  }
  const state = (row: SessionRow) => stopped[row.session_id] || row.state === 'stopped' ? 'Stopped'
    : row.state ? row.state.charAt(0).toUpperCase() + row.state.slice(1)
    : row.claude_running === true ? 'Running' : 'Not reported';
  const canStop = (row: SessionRow) => row.can_stop && state(row) !== 'Stopped';
  const stopButton = (row: SessionRow) => <Button onClick={() => { setPendingStop(row.session_id); setStopError(''); }}>Stop</Button>;
  return <SpaceBetween size="l">
    {!!error && <Alert type="error" header="Could not refresh sessions">{error}{sessions.length > 0 && ' The last retrieved records are shown below.'}</Alert>}
    {!!auditWarning && <Alert type="warning" header="Audit record unavailable">{auditWarning}</Alert>}
    <ResourceTable<SessionRow> title="Sessions" items={sessions} loading={loading} trackBy="session_id" sortingField="started_at" descending
      description="Open sessions are registered terminals on this host. Recorded rows are historical activity, not a live Runtime inventory."
      searchText={row => `${row.session_id} ${row.assistant_type || row.agent} ${row.user_id || row.user} ${state(row)}`}
      actions={<SpaceBetween direction="horizontal" size="xs">
        <Select ariaLabel="Session time window" selectedOption={WINDOWS.find(w => w.value === windowKey)!} options={WINDOWS}
          onChange={({ detail }) => setWindowKey(detail.selectedOption.value!)} />
        <Button iconName="refresh" ariaLabel="Refresh sessions" onClick={() => void refresh()} />
      </SpaceBetween>}
      empty={<SpaceBetween size="s"><Box variant="strong">{error ? 'Sessions unavailable' : 'No sessions recorded in this window'}</Box>
        <Box color="text-body-secondary">{error ? 'Retry when the host connection is available.' : 'Choose another time window or return to the terminals to open a session.'}</Box></SpaceBetween>}
      columns={[
        { id: 'session', header: narrow ? 'Session' : 'Session ID', sortingField: 'session_id', cell: row => <SpaceBetween size="xxs">
          <Link onFollow={() => setSelected(row)}><span className="console-code">{row.session_id}</span></Link>
          {narrow && <><Box color="text-body-secondary">{agentInstanceLabel(row.assistant_type || row.agent)}</Box>
            <Box color="text-body-secondary" fontSize="body-s">{fmtTime(row.started_at)}</Box></>}
        </SpaceBetween> },
        ...(!narrow ? [
          { id: 'agent', header: 'Agent', cell: (row: SessionRow) => agentInstanceLabel(row.assistant_type || row.agent) || 'Not recorded' },
          { id: 'user', header: 'Recorded user', cell: (row: SessionRow) => maskHandle(row.user_id || row.user) || 'Not recorded' },
          { id: 'started', header: 'Started', sortingField: 'started_at', cell: (row: SessionRow) => fmtTime(row.started_at) },
        ] : []),
        { id: 'state', header: 'State', width: narrow ? 125 : undefined, cell: row => <SpaceBetween size="s">
          <StatusIndicator type={['Open', 'Running'].includes(state(row)) ? 'in-progress' : state(row) === 'Stopped' ? 'stopped' : 'pending'}>{state(row)}</StatusIndicator>
          {narrow && canStop(row) && stopButton(row)}
        </SpaceBetween> },
        ...(!narrow ? [{ id: 'actions', header: 'Actions', cell: (row: SessionRow) => canStop(row) ? stopButton(row)
          : <Box color="text-body-secondary">{state(row) === 'Stopped' ? 'Stopped' : 'No stop target recorded'}</Box> }] : []),
      ]} />
    <Modal visible={pendingStop !== null} header="Stop session?" closeAriaLabel="Cancel session stop" onDismiss={() => { if (!stopping) setPendingStop(null); }}
      footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs"><Button variant="link" disabled={stopping} onClick={() => setPendingStop(null)}>Cancel</Button>
        <Button variant="primary" loading={stopping} onClick={() => void stop()}>Stop session</Button></SpaceBetween></Box>}>
      <SpaceBetween size="m"><Box>This ends its running processes and discards files kept only in the session. Files saved to shared storage remain.</Box>
        <code className="console-code">{pendingStop}</code>{!!stopError && <Alert type="error" header="Session stop failed">{stopError}</Alert>}
      </SpaceBetween>
    </Modal>
    <SessionDetailPanel session={selected} onClose={() => setSelected(null)} />
  </SpaceBetween>;
}
