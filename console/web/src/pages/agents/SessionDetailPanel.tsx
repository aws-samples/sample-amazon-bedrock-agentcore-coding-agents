import { useEffect, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { getIdentity, type Identity, type SessionRow } from '../../api';
import { fmtTime, maskHandle } from '../../shared';
import { agentInstanceLabel } from './environments';

export function SessionDetailPanel({ session, onClose }: { session: SessionRow | null; onClose: () => void }) {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const id = session?.session_id;
  useEffect(() => {
    if (!id) return;
    let live = true;
    setLoading(true); setError(''); setIdentity(null);
    void getIdentity(id).then(value => { if (live) setIdentity(value); })
      .catch(e => { if (live) setError(e instanceof Error ? e.message : 'Could not read this session’s identity record.'); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [id]);
  return <Modal visible={!!session} header="Session details" onDismiss={onClose} closeAriaLabel="Close session details" size="large"
    footer={<Box float="right"><Button onClick={onClose}>Close</Button></Box>}>
    <SpaceBetween size="l">
      <KeyValuePairs columns={2} items={[
        { label: 'Session ID', value: <code className="console-code">{id}</code> },
        { label: 'Agent', value: agentInstanceLabel(session?.assistant_type || session?.agent) || 'Not recorded' },
        { label: 'Recorded user', value: maskHandle(session?.user_id || session?.user) || 'Not recorded' },
        { label: 'Started', value: fmtTime(session?.started_at) },
        { label: 'Runtime', value: <code className="console-code">{session?.runtime_arn || 'Not recorded'}</code> },
        { label: 'Record source', value: session?.source === 'runtime-registry' ? 'This host’s terminal registry' : 'This host’s run ledger' },
      ]} />
      {loading && <StatusIndicator type="loading">Loading identity record</StatusIndicator>}
      {!!error && <Alert type="error">{error}</Alert>}
      {identity && <Container header={<Header variant="h2">Identity record</Header>}>
        <SpaceBetween size="m">
          <KeyValuePairs columns={2} items={[
            { label: 'Authentication provider', value: identity.auth_provider === 'not-recorded' ? 'Not recorded' : identity.auth_provider },
            { label: 'Attribution source', value: identity.attribution_source === 'runtime-registry' ? 'Terminal registry' : 'Run ledger' },
            { label: 'GitHub actor', value: 'Determined by the GitHub credential' },
            { label: 'Static credentials in this record', value: identity.static_credentials_on_agent === true ? 'Reported present'
              : identity.static_credentials_on_agent === false ? 'None reported' : 'Not reported' },
          ]} />
          <Box color="text-body-secondary">A recorded user label does not prove delegated access to GitHub. For exported request and token usage, run the CloudWatch query on Governance.</Box>
        </SpaceBetween>
      </Container>}
    </SpaceBetween>
  </Modal>;
}
