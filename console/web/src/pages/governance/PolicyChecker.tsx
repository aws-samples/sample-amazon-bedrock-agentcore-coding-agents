import { useEffect, useState } from 'react';
import { useHref, useNavigate } from 'react-router-dom';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table from '@cloudscape-design/components/table';
import Textarea from '@cloudscape-design/components/textarea';
import { evaluatePolicy, getPolicies, type Policies, type PolicyEvaluation } from '../../api';

const EXAMPLES = [
  { value: 'inspect', label: 'Inspect the working tree', action: 'run_command', target: 'git status', read_only: false },
  { value: 'root', label: 'Attempt a destructive command', action: 'run_command', target: 'rm -rf /', read_only: false },
  { value: 'force', label: 'Attempt a force push', action: 'run_command', target: 'git push --force origin main', read_only: false },
  { value: 'credentials', label: 'Attempt a credential file write', action: 'write_file', target: 'config/.env', read_only: false },
  { value: 'readonly', label: 'Attempt a write in a read-only workflow', action: 'write_file', target: 'README.md', read_only: true },
];
const ACTIONS = [
  { value: 'run_command', label: 'Shell command' },
  { value: 'write_file', label: 'File write' },
  { value: 'read_file', label: 'File read' },
];

export function PolicyChecker({ onEvaluation }: { onEvaluation: () => void }) {
  const base = useHref('/').replace(/\/$/, '');
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<Policies | null>(null);
  const [catalogError, setCatalogError] = useState('');
  const [example, setExample] = useState<string | null>('inspect');
  const [action, setAction] = useState('run_command');
  const [target, setTarget] = useState('git status');
  const [readOnly, setReadOnly] = useState(false);
  const [result, setResult] = useState<PolicyEvaluation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    getPolicies().then(value => { if (live) setCatalog(value); })
      .catch(() => { if (live) setCatalogError('Could not read the policy catalog. Reload this page to retry.'); });
    return () => { live = false; };
  }, []);
  function changed() { setResult(null); setError(''); setExample(null); }
  async function evaluate() {
    if (loading || !target.trim()) return;
    setLoading(true); setResult(null); setError('');
    try { setResult(await evaluatePolicy({ action, target, read_only: readOnly })); onEvaluation(); }
    catch (e) { setError(e instanceof Error ? e.message : 'The policy check failed.'); }
    finally { setLoading(false); }
  }
  return <Container header={<Header variant="h2" description="Evaluate an action with the host's policy checker. This preview does not execute it.">Policy checker</Header>}>
    <SpaceBetween size="m">
      <div className="governance-form-row">
        <FormField label="Example"><Select selectedOption={EXAMPLES.find(item => item.value === example) || null}
          placeholder="Custom action" options={EXAMPLES} disabled={loading}
          onChange={({ detail }) => {
            const selected = EXAMPLES.find(item => item.value === detail.selectedOption.value)!;
            setExample(selected.value); setAction(selected.action); setTarget(selected.target); setReadOnly(selected.read_only); setResult(null); setError('');
          }} /></FormField>
        <FormField label="Action"><Select selectedOption={ACTIONS.find(item => item.value === action)!} options={ACTIONS} disabled={loading}
          onChange={({ detail }) => { changed(); setAction(detail.selectedOption.value!); }} /></FormField>
      </div>
      <FormField stretch label={action === 'run_command' ? 'Command to evaluate' : 'File path to evaluate'}>
        <Textarea value={target} rows={2} disabled={loading} spellcheck={false}
          onChange={({ detail }) => { changed(); setTarget(detail.value); }} />
      </FormField>
      <div className="governance-action-row">
        <Checkbox checked={readOnly} disabled={loading}
          description="The read-only rule checks file writes, not shell side effects."
          onChange={({ detail }) => { changed(); setReadOnly(detail.checked); }}>Read-only workflow</Checkbox>
        <Button variant="primary" loading={loading} disabled={!target.trim()} onClick={() => void evaluate()}>Evaluate action</Button>
      </div>
      {!!error && <Alert type="error" header="Policy evaluation failed">{error}</Alert>}
      {result && <SpaceBetween size="s">
        <Alert type={result.outcome === 'allow' ? 'info' : 'warning'}
          header={result.outcome === 'allow' ? 'Allowed by this checker' : result.outcome === 'hold' ? 'Held for human handling' : 'Blocked by policy'}>
          {result.reason || 'No rule matched this action.'} No action was executed.
        </Alert>
        <KeyValuePairs columns={2} items={[
          { label: 'Matched rule', value: result.rule_id ? <code className="console-code">{result.rule_id}</code> : 'None' },
          { label: 'Audit event', value: result.event_id ? <code className="console-code">{result.event_id}</code> : 'Not recorded' },
        ]} />
        {result.event_id && <Link href={`${base}/governance/activity?event=${encodeURIComponent(result.event_id)}`}
          onFollow={event => { event.preventDefault(); navigate(`/governance/activity?event=${encodeURIComponent(result.event_id!)}`); }}>View audit event</Link>}
        {!result.audit_recorded && <Alert type="warning">{result.audit_error || 'The policy result was not recorded in the audit trail.'}</Alert>}
      </SpaceBetween>}
      {!!catalogError && <Alert type="error">{catalogError}</Alert>}
      <ExpandableSection headerText={`Rules and enforcement scope${catalog ? ` (${catalog.policies.length})` : ''}`}>
        <SpaceBetween size="m">
          <Box>{catalog?.scope || 'Loading policy scope'}</Box>
          {catalog?.note && <Box color="text-body-secondary">{catalog.note}</Box>}
          {catalog && <Table variant="embedded" trackBy="rule_id" items={catalog.policies} wrapLines
            ariaLabels={{ tableLabel: 'Policy rules' }} columnDefinitions={[
              { id: 'rule', header: 'Rule', cell: rule => <code className="console-code">{rule.rule_id}</code> },
              { id: 'decision', header: 'Decision', cell: rule => rule.tier === 'hard' ? 'Deny' : 'Hold for a person' },
              { id: 'description', header: 'Checks', cell: rule => rule.summary },
            ]} />}
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  </Container>;
}
