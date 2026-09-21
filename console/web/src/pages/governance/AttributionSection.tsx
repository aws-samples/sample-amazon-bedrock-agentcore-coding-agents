import { useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import CopyToClipboard from '@cloudscape-design/components/copy-to-clipboard';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { getAttributionConfiguration, queryAttribution, type AttributionConfiguration, type AttributionEvidence, type AttributionTokenField, type AttributionTokens } from '../../api';
import { fmtNum } from '../../shared';
import { ResourceTable } from '../../shared/ResourceTable';
import { downloadJson } from '../../shared/download';

const WINDOWS = [1, 3, 24].map(hours => ({ value: String(hours), label: `Last ${hours} ${hours === 1 ? 'hour' : 'hours'}` }));
const date = (seconds: number) => new Date(seconds * 1000).toISOString();
const tokens = (value: number | null) => value == null ? 'Unavailable' : fmtNum(value);
const tokenCell = (row: AttributionTokens, field: AttributionTokenField) => (
  <SpaceBetween size="xxs">
    <span className="console-token-value">{tokens(row[field])}</span>
    {row[field] != null && row.reported_requests[field] < row.requests &&
      <Box color="text-status-warning" fontSize="body-s">
        Partial: {fmtNum(row.reported_requests[field])} of {fmtNum(row.requests)} requests
      </Box>}
  </SpaceBetween>
);
export function AttributionSection() {
  const [config, setConfig] = useState<AttributionConfiguration | null>(null);
  const [data, setData] = useState<AttributionEvidence | null>(null);
  const [hours, setHours] = useState('3');
  const [agent, setAgent] = useState('all');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const pendingValue = loading ? 'Querying' : error ? 'Unavailable' : 'Not queried';
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    let live = true;
    getAttributionConfiguration().then(value => { if (live) setConfig(value); })
      .catch(() => { if (live) setError('Could not read telemetry configuration. Reload the page to try again.'); });
    return () => { live = false; request.current?.abort(); };
  }, []);
  async function query() {
    if (loading) return;
    const controller = new AbortController(); request.current = controller;
    setLoading(true); setError(''); setData(null);
    try {
      const result = await queryAttribution(Number(hours), controller.signal);
      if (result.status !== 'Complete') throw new Error('CloudWatch did not return a complete query result.');
      if (!controller.signal.aborted) setData(result);
    } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'The query failed.'); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  }
  const sources = config?.agents || [];
  const options = [{ value: 'all', label: 'All agents' }, ...sources.map(source => ({ value: source.id, label: source.label }))];
  const rows = (data?.rows || []).filter(row => agent === 'all' || row.agent === agent);
  const label = (id: string) => sources.find(source => source.id === id)?.label || id;
  const rowKey = (row: AttributionEvidence['rows'][number]) => JSON.stringify([row.agent, row.user]);
  return <Container header={<Header variant="h2" description="Compare Claude Code and Codex token usage from CloudWatch."
    actions={<SpaceBetween direction="horizontal" size="xs">
      <Select ariaLabel="Telemetry time window" selectedOption={WINDOWS.find(window => window.value === hours)!} options={WINDOWS} disabled={loading}
        onChange={({ detail }) => { setHours(detail.selectedOption.value!); setData(null); setError(''); }} />
      <Button variant="primary" loading={loading} disabled={!config?.region || loading} onClick={() => void query()}>Run query</Button>
    </SpaceBetween>}>CloudWatch usage</Header>}>
    <SpaceBetween size="l">
      <div role="status" aria-live="polite" className="sr-only">
        {loading ? 'Querying CloudWatch…' : data
          ? `Query complete. ${fmtNum(data.total_requests)} usage events across both agents.`
          : error ? 'The usage query is unavailable.' : ''}
      </div>
      {config ? <div className="governance-source"><span><strong>Region</strong> {config.region || 'Not configured'}</span>
        <span><strong>Log group</strong> <code className="console-code">{config.log_group}</code></span></div>
        : !error && <StatusIndicator type="loading">Reading telemetry configuration</StatusIndicator>}
      {!!error && <Alert type="error" header="Telemetry query unavailable">{error}</Alert>}
      <ColumnLayout columns={2} borders="vertical">
        {sources.map(source => {
          const summary = data?.agents.find(item => item.id === source.id);
          return <SpaceBetween key={source.id} size="m">
            <Header variant="h3" description={source.event_description}>{source.label}</Header>
            <KeyValuePairs columns={2} items={[
              { label: 'Usage events', value: summary ? fmtNum(summary.requests) : pendingValue },
              { label: 'User attribution', value: summary
                ? summary.coverage_percent == null ? 'No events' : `${summary.coverage_percent}% tagged`
                : pendingValue },
              { label: 'Total input tokens', value: summary ? tokenCell(summary, 'total_input_tokens') : pendingValue },
              { label: 'Output tokens', value: summary ? tokenCell(summary, 'output_tokens') : pendingValue },
            ]} />
            {summary && <Box color="text-body-secondary" fontSize="body-s">
              {summary.requests ? `${fmtNum(summary.tagged_requests)} tagged · ${fmtNum(summary.untagged_requests)} untagged events`
                : 'No usage events in this window. Check the time range and exporter after running this agent.'}
            </Box>}
          </SpaceBetween>;
        })}
      </ColumnLayout>
      {data && <Box color="text-body-secondary" fontSize="body-s">
        {date(data.start_time)} to {date(data.end_time)} (UTC). Both agents use this same query window.
      </Box>}
      <ResourceTable title="Usage by agent and user" variant="embedded" items={rows} loading={loading} showCounter={!!data}
        description="One row per agent and user label. Token counts include work from unsuccessful builds."
        trackBy={rowKey} searchText={row => `${label(row.agent)} ${row.user || 'Untagged'}`} sortingField="requests" descending
        filterPlaceholder="Find a user label"
        actions={<SpaceBetween direction="horizontal" size="xs">
          <Select ariaLabel="Filter usage by agent" selectedOption={options.find(option => option.value === agent)!} options={options}
            onChange={({ detail }) => setAgent(detail.selectedOption.value!)} />
          <Button iconName="download" disabled={!data} onClick={() => downloadJson('agent-studio-cloudwatch-usage.json', data)}>Export JSON</Button>
        </SpaceBetween>}
        empty={<SpaceBetween size="s"><Box variant="strong">{error ? 'Query results unavailable' : data ? 'No matching request events' : 'Run a query to inspect usage'}</Box>
          <Box color="text-body-secondary">{data ? 'Check the time window and exporter. Allow time for new events to arrive.' : 'The query reads CloudWatch. It does not start an agent or a build.'}</Box></SpaceBetween>}
        columns={[
          { id: 'agent', header: 'Agent', sortingField: 'agent', cell: row => label(row.agent) },
          { id: 'user', header: 'User label', sortingField: 'user', cell: row => row.user ?? <StatusIndicator type="warning">Untagged</StatusIndicator> },
          { id: 'requests', header: 'Usage events', sortingField: 'requests', cell: row => fmtNum(row.requests) },
          { id: 'total-input', header: 'Total input tokens', sortingField: 'total_input_tokens', cell: row => tokenCell(row, 'total_input_tokens') },
          { id: 'output', header: 'Output tokens', sortingField: 'output_tokens', cell: row => tokenCell(row, 'output_tokens') },
        ]} />
      <Box color="text-body-secondary" fontSize="body-s">Export JSON includes both agents and every user, even when the table is filtered. Usage events count exported model request or response records, not prompts, sessions, or builds. This is token usage, not a bill.</Box>
      <ExpandableSection headerText="Cache and reasoning details">
        <SpaceBetween size="m">
          <Box>Claude Code reports uncached input, cache creation, and cache reads separately; total input adds those components. Codex already includes cache tokens in its reported input total. Reasoning tokens are part of Codex output and are not added again.</Box>
          <ResourceTable title="Token breakdown" variant="embedded" items={rows} loading={loading} showCounter={!!data}
            trackBy={rowKey} searchText={row => `${label(row.agent)} ${row.user || 'Untagged'}`}
            filterPlaceholder="Find a user label" empty={<Box>No matching usage events.</Box>}
            columns={[
              { id: 'agent', header: 'Agent', cell: row => label(row.agent) },
              { id: 'user', header: 'User label', cell: row => row.user ?? 'Untagged' },
              { id: 'input', header: 'Uncached input', cell: row => tokenCell(row, 'input_tokens') },
              { id: 'cache-creation', header: 'Cache creation', cell: row => tokenCell(row, 'cache_creation_tokens') },
              { id: 'cache-read', header: 'Cache read', cell: row => tokenCell(row, 'cache_read_tokens') },
              { id: 'reasoning', header: 'Reasoning output', cell: row => tokenCell(row, 'reasoning_output_tokens') },
            ]} />
          <Box color="text-body-secondary" fontSize="body-s">Partial values sum only events that reported that field. Unavailable means it was not reported or could not be calculated completely; it does not mean zero. Claude Code does not report a separate reasoning-token count here.</Box>
        </SpaceBetween>
      </ExpandableSection>
      <Box color="text-body-secondary" fontSize="body-s">Signed-in Chat builds and Agents sessions receive a user label automatically. A manually supplied label does not establish a Cognito sign-in. Kiro credits, coordinator and review calls, and infrastructure charges are outside this query.</Box>
      {!!data?.untagged_requests && <ExpandableSection headerText="Why are some requests untagged?">
        <SpaceBetween size="s">
          <Box>These events reached CloudWatch without a user label. CLI requests without console sign-in and requests made before identity mapping was enabled can appear here.</Box>
          <Box>Sign in before starting a new Chat build or opening an Agents session. A shared terminal keeps the identity of the person who opened it. Earlier events keep their original labels, even after you sign in or update the mapping.</Box>
        </SpaceBetween>
      </ExpandableSection>}
      <ExpandableSection headerText="Query and evidence details">
        <SpaceBetween size="m">
          {data && <KeyValuePairs columns={3} items={[
            { label: 'Start (UTC)', value: date(data.start_time) }, { label: 'End (UTC)', value: date(data.end_time) },
            { label: 'Query ID', value: <code className="console-code">{data.query_id}</code> },
          ]} />}
          {config && <>
            <CopyToClipboard textToCopy={config.query} copyButtonText="Copy query"
              copySuccessText="Query copied" copyErrorText="Could not copy. Select the query text below." />
            <pre className="console-record">{config.query}</pre>
          </>}
          {config?.region && <Link external href={`https://${encodeURIComponent(config.region)}.console.aws.amazon.com/cloudwatch/home?region=${encodeURIComponent(config.region)}#logsV2:logs-insights`}>Open CloudWatch Logs Insights</Link>}
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  </Container>;
}
