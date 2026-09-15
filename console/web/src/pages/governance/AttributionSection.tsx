import { useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { getAttributionConfiguration, queryAttribution, type AttributionConfiguration, type AttributionEvidence } from '../../api';
import { fmtNum } from '../../shared';
import { ResourceTable } from '../../shared/ResourceTable';
import { downloadJson } from '../../shared/download';

const WINDOWS = [1, 3, 24].map(hours => ({ value: String(hours), label: `Last ${hours} ${hours === 1 ? 'hour' : 'hours'}` }));
const date = (seconds: number) => new Date(seconds * 1000).toLocaleString();
const tokens = (value: number | null) => value == null ? 'Not reported' : fmtNum(value);
export function AttributionSection() {
  const [config, setConfig] = useState<AttributionConfiguration | null>(null);
  const [data, setData] = useState<AttributionEvidence | null>(null);
  const [hours, setHours] = useState('3');
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
  return <Container header={<Header variant="h2" description="Claude Code request events, grouped by the user.id resource attribute."
    actions={<SpaceBetween direction="horizontal" size="xs">
      <Select ariaLabel="Telemetry time window" selectedOption={WINDOWS.find(window => window.value === hours)!} options={WINDOWS} disabled={loading}
        onChange={({ detail }) => { setHours(detail.selectedOption.value!); setData(null); setError(''); }} />
      <Button variant="primary" loading={loading} disabled={!config?.region || loading} onClick={() => void query()}>Run query</Button>
    </SpaceBetween>}>CloudWatch usage</Header>}>
    <SpaceBetween size="l">
      {config ? <div className="governance-source"><span><strong>Region</strong> {config.region || 'Not configured'}</span>
        <span><strong>Log group</strong> <code className="console-code">{config.log_group}</code></span></div>
        : !error && <StatusIndicator type="loading">Reading telemetry configuration</StatusIndicator>}
      {!!error && <Alert type="error" header="Telemetry query unavailable">{error}</Alert>}
      <ColumnLayout columns={3} borders="vertical">
        {[
          { label: 'Request events', value: data ? fmtNum(data.total_requests) : pendingValue, hint: 'In the selected window' },
          { label: 'Tagged requests', value: data ? fmtNum(data.tagged_requests) : pendingValue, hint: data?.coverage_percent != null ? `${data.coverage_percent}% carry a user label` : 'A named user.id resource attribute' },
          { label: 'Untagged requests', value: data ? fmtNum(data.untagged_requests) : pendingValue, hint: 'Received without a user label' },
        ].map(metric => <SpaceBetween key={metric.label} size="xs"><Box color="text-label" fontWeight="bold">{metric.label}</Box>
          <div className={data ? 'console-metric-value' : 'governance-metric-empty'}>{metric.value}</div>
          <Box color="text-body-secondary" fontSize="body-s">{metric.hint}</Box></SpaceBetween>)}
      </ColumnLayout>
      <ResourceTable title="Usage by user label" variant="embedded" items={data?.rows || []} loading={loading} showCounter={!!data}
        trackBy={row => row.user == null ? 'missing' : `user:${row.user}`} searchText={row => row.user || 'Untagged'} sortingField="requests" descending
        filterPlaceholder="Find a user label"
        actions={<Button iconName="download" disabled={!data} onClick={() => downloadJson('agent-studio-cloudwatch-usage.json', data)}>Export results</Button>}
        empty={<SpaceBetween size="s"><Box variant="strong">{error ? 'Query results unavailable' : data ? 'No matching request events' : 'Run a query to inspect usage'}</Box>
          <Box color="text-body-secondary">{data ? 'Check the time window and exporter. Allow time for new events to arrive.' : 'The query reads CloudWatch. It does not start an agent or a build.'}</Box></SpaceBetween>}
        columns={[
          { id: 'user', header: 'User label', sortingField: 'user', cell: row => row.user ?? <StatusIndicator type="warning">Untagged</StatusIndicator> },
          { id: 'requests', header: 'Requests', sortingField: 'requests', cell: row => fmtNum(row.requests) },
          { id: 'input', header: 'Input tokens', sortingField: 'input_tokens', cell: row => tokens(row.input_tokens) },
          { id: 'output', header: 'Output tokens', sortingField: 'output_tokens', cell: row => tokens(row.output_tokens) },
        ]} />
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
            { label: 'Start', value: date(data.start_time) }, { label: 'End', value: date(data.end_time) },
            { label: 'Query ID', value: <code className="console-code">{data.query_id}</code> },
          ]} />}
          {config && <pre className="console-record">{config.query}</pre>}
          {config?.region && <Link external href={`https://${encodeURIComponent(config.region)}.console.aws.amazon.com/cloudwatch/home?region=${encodeURIComponent(config.region)}#logsV2:logs-insights`}>Open CloudWatch Logs Insights</Link>}
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  </Container>;
}
