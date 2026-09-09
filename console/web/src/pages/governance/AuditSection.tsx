import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { getAudit, type AuditRow, type AuditTrail } from '../../api';
import { fmtTime } from '../../shared';
import { ResourceTable } from '../../shared/ResourceTable';
import { downloadJson } from '../../shared/download';
import { useMediaQuery } from '../../hooks/useMediaQuery';

export function AuditSection({ refreshKey = 0 }: { refreshKey?: number }) {
  const narrow = useMediaQuery('(max-width: 600px)');
  const [search, setSearch] = useSearchParams();
  const requestedEvent = search.get('event');
  const [data, setData] = useState<AuditTrail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<AuditRow | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    try { setData(await getAudit(200)); setError(''); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not read the audit trail.'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh, refreshKey]);
  useEffect(() => {
    if (requestedEvent && data) setSelected(data.audit.find(row => row.event_id === requestedEvent) || null);
  }, [requestedEvent, data]);
  function closeEvent() {
    setSelected(null);
    if (requestedEvent) { const next = new URLSearchParams(search); next.delete('event'); setSearch(next); }
  }
  const eventNames: Record<string, string> = { policy_evaluation: 'Policy evaluation', session_stop: 'Session stop', stage1_session: 'Development session' };
  const eventName = (kind: string) => eventNames[kind]
    || kind.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase());
  const actionNames: Record<string, string> = { run_command: 'Shell command', write_file: 'File write', read_file: 'File read' };
  const outcomes: Record<string, string> = { allow: 'Allowed by the checker', hold: 'Held for human handling', deny: 'Blocked by policy' };
  const detail = (row: AuditRow, key: string) => typeof row.details?.[key] === 'string' ? String(row.details[key]) : '';
  const eventSummary = (row: AuditRow) => row.kind === 'policy_evaluation'
    ? `Preview: ${outcomes[detail(row, 'outcome')] || 'Decision not recorded'} · ${actionNames[detail(row, 'action')] || 'Action not recorded'}`
    : row.kind === 'stage1_session' ? 'Host terminal activity' : row.line;
  function openEvent(row: AuditRow & { rowKey: string }) {
    const { rowKey: _, ...event } = row;
    setSelected(event);
    if (row.event_id) { const next = new URLSearchParams(search); next.set('event', row.event_id); setSearch(next); }
  }
  const rows = [...(data?.audit || [])].reverse().map((row, index) => ({ ...row, rowKey: row.event_id || `${row.at}:${index}` }));
  return <SpaceBetween size="m">
    {!!error && <Alert type="error" header="Audit trail unavailable">{error}</Alert>}
    {requestedEvent && data && !data.audit.some(row => row.event_id === requestedEvent) &&
      <Alert type="warning">The requested event is not in this host’s latest 200 records. Check the event ID and host, or refresh the trail.</Alert>}
    <ResourceTable<AuditRow & { rowKey: string }> title="Audit trail" description="The most recent 200 operations recorded on this host. Policy previews are identified as previews."
      items={rows} loading={loading} trackBy="rowKey" pageSize={10}
      searchText={row => `${row.kind} ${row.user_id} ${row.line} ${row.event_id || ''}`} filterPlaceholder="Find an event, user, or rule"
      actions={<SpaceBetween direction="horizontal" size="xs">
        <Button iconName="refresh" ariaLabel="Refresh audit trail" loading={loading} onClick={() => void refresh()} />
        <Button iconName="download" disabled={!data?.audit.length} onClick={() => downloadJson('agent-studio-audit.json', data)}>Export</Button>
      </SpaceBetween>}
      empty={<SpaceBetween size="s"><Box variant="strong">{error ? 'Audit records unavailable' : 'No events recorded yet'}</Box>
        <Box color="text-body-secondary">Evaluate an action on Controls to record a policy preview.</Box></SpaceBetween>}
      columns={[
        { id: 'time', header: 'Time', sortingField: 'at', cell: row => fmtTime(row.at), minWidth: narrow ? 86 : 140, width: narrow ? 90 : 180 },
        { id: 'event', header: 'Event', cell: row => <SpaceBetween size="xxs">
          <Link onFollow={() => openEvent(row)}>{eventName(row.kind)}</Link>
          <Box color="text-body-secondary">{eventSummary(row)}</Box>
          {narrow && <Box color="text-body-secondary" fontSize="body-s">{row.user_id}</Box>}
        </SpaceBetween> },
        ...(!narrow ? [{ id: 'user', header: 'User', sortingField: 'user_id', cell: (row: AuditRow) => row.user_id }] : []),
      ]} />
    <Modal visible={selected !== null} header="Audit event" size="large" closeAriaLabel="Close audit event" onDismiss={closeEvent}
      footer={<Box float="right"><Button onClick={closeEvent}>Close</Button></Box>}>
      {selected && <SpaceBetween size="l">
        {selected.kind === 'policy_evaluation' && <Alert type={detail(selected, 'outcome') === 'allow' ? 'info' : 'warning'} header={outcomes[detail(selected, 'outcome')] || 'Policy evaluation'}>
          This was a policy preview. {selected.details?.executed === false ? 'No action was executed.' : 'Execution was not recorded.'}
        </Alert>}
        <KeyValuePairs columns={narrow ? 1 : 2} items={[
          { label: 'Event type', value: eventName(selected.kind) },
          { label: 'Time', value: selected.at ? fmtTime(selected.at) : 'Not recorded' },
          { label: 'Recorded user', value: selected.user_id },
          { label: 'Record source', value: selected.actor_source === 'local-session' ? 'Local session'
            : selected.actor_source === 'console-session' ? 'Console session' : 'Host run ledger' },
          ...(selected.kind === 'policy_evaluation' ? [
            { label: 'Action', value: actionNames[detail(selected, 'action')] || 'Not recorded' },
            { label: 'Matched rule', value: detail(selected, 'rule_id') ? <code className="console-code">{detail(selected, 'rule_id')}</code> : 'None' },
          ] : []),
          ...(selected.kind === 'session_stop' ? [
            { label: 'Session', value: <code className="console-code">{detail(selected, 'session_id') || 'Not recorded'}</code> },
            { label: 'Stop result', value: selected.details?.stopped === true ? 'Stopped' : selected.details?.stopped === false ? 'Failed' : 'Not recorded' },
          ] : []),
          ...(selected.event_id ? [{ label: 'Event ID', value: <code className="console-code">{selected.event_id}</code> }] : []),
        ]} />
        <ExpandableSection headerText="Raw event">
          <SpaceBetween size="m">
            <Button iconName="download" onClick={() => downloadJson(`agent-studio-event-${selected.event_id || 'record'}.json`, selected)}>Export event</Button>
            <pre className="console-record">{JSON.stringify(selected, null, 2)}</pre>
          </SpaceBetween>
        </ExpandableSection>
        <Box color="text-body-secondary">This is the host's recorded event. CLI builds submitted to the deployed coordinator keep their own run history.</Box>
      </SpaceBetween>}
    </Modal>
  </SpaceBetween>;
}
