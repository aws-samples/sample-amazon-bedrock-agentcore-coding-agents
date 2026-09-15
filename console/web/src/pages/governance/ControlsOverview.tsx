import { useCallback, useEffect, useState } from 'react';
import { useHref, useNavigate } from 'react-router-dom';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import { getGovernanceControls, type GovernanceControls } from '../../api';

export function ControlsOverview() {
  const navigate = useNavigate();
  const base = useHref('/').replace(/\/$/, '');
  const [data, setData] = useState<GovernanceControls | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    setLoading(true);
    try { setData(await getGovernanceControls()); setError(''); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not read host configuration.'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  return <Container header={<Header variant="h2" description="The identity, approval boundary, and limits used by this host."
    actions={<Button iconName="refresh" loading={loading} onClick={() => void refresh()}>Refresh</Button>}>Identity and approvals</Header>}>
    <SpaceBetween size="l">
      {!!error && <Alert type="error" header="Configuration unavailable">{error}</Alert>}
      {!data ? !error && <StatusIndicator type="loading">Reading host configuration</StatusIndicator> : <SpaceBetween size="m">
        <ColumnLayout columns={3} borders="vertical">
          <KeyValuePairs columns={1} items={[
            { label: 'Signed-in submitter', value: data.identity.known ? data.identity.email || data.identity.user_id
              : <StatusIndicator type="pending">No Cognito identity</StatusIndicator> },
            { label: 'Telemetry mapping', value: <StatusIndicator type={data.identity.mapping_state === 'present' ? 'info' : 'pending'}>
              {data.identity.mapping_state === 'present' ? 'Resource attributes returned'
                : data.identity.mapping_state === 'empty' ? 'No resource attributes returned' : 'Sign in to inspect your mapping'}
            </StatusIndicator> },
          ]} />
          <KeyValuePairs columns={1} items={[
            { label: 'Pull request merging', value: data.merge_policy === 'human_review' ? 'Human review' : 'Automatic after approval' },
            { label: 'Who checks the work', value: data.roles.filter(role => role.kind === 'checker').map(role => role.label).join(', ') || 'No checker in the served roster' },
            { label: 'Approval settings', value: <Link href={`${base}/settings?tab=repository`}
              onFollow={event => { event.preventDefault(); navigate('/settings?tab=repository'); }}>Manage merge policy</Link> },
          ]} />
          <KeyValuePairs columns={1} items={[
            { label: 'Repair limit', value: `${data.limits.repairs_per_pr} per pull request` },
            { label: 'Executable check budget', value: `${data.limits.gate_timeout_seconds} seconds` },
            { label: 'Agent turn budget', value: `${data.limits.role_timeout_seconds / 60} minutes` },
          ]} />
        </ColumnLayout>
        <ExpandableSection headerText="Inspect identity propagation and role separation">
          <SpaceBetween size="m">
            <Box color="text-body-secondary">The submitter is carried to the agent process as telemetry metadata. Pull request authorship still comes from the GitHub App.</Box>
            <KeyValuePairs columns={2} items={[
              { label: 'Submitter ID', value: data.identity.user_id || 'Not available in this session' },
              { label: 'OTEL_RESOURCE_ATTRIBUTES', value: data.identity.telemetry_attributes
                ? <code className="console-code">{data.identity.telemetry_attributes}</code> : 'No resource attributes returned' },
            ]} />
            <Table variant="embedded" trackBy="id" items={data.roles} ariaLabels={{ tableLabel: 'Role separation' }}
              columnDefinitions={[
                { id: 'agent', header: 'Agent', cell: role => role.label },
                { id: 'responsibility', header: 'Responsibility', cell: role => role.role_name.replaceAll('-', ' ') },
                { id: 'boundary', header: 'Role boundary', cell: role => role.kind === 'builder' ? 'Produces the change' : 'Authors the executable check' },
              ]} />
            <Box color="text-body-secondary" fontSize="body-s">A returned label shows the mapping output. Open <Link href={`${base}/governance/usage`}
              onFollow={event => { event.preventDefault(); navigate('/governance/usage'); }}>Usage</Link> to verify that exported events carry it.</Box>
          </SpaceBetween>
        </ExpandableSection>
      </SpaceBetween>}
    </SpaceBetween>
  </Container>;
}
