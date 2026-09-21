import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator, { type StatusIndicatorProps } from '@cloudscape-design/components/status-indicator';
import Tabs from '@cloudscape-design/components/tabs';
import TextContent from '@cloudscape-design/components/text-content';
import { AgentIcon } from './AgentIcon';
import { presentRunDecision, recordedPullRequests } from '../lib/runPresentation';
import { ResourceTable } from '../shared/ResourceTable';
import { agentInstanceLabel } from '../pages/agents/environments';
import type { GateRecord, RunDetail } from '../api';

function label(value: string): string {
  const text = value.replaceAll('_', ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}
function statusType(status: string): StatusIndicatorProps.Type {
  if (['passed', 'done', 'completed', 'merged', 'approved'].includes(status)) return 'success';
  if (['failed', 'error', 'blocked', 'changes_requested'].includes(status)) return 'error';
  if (['needs_human', 'awaiting_review', 'abstained'].includes(status)) return 'warning';
  if (['running', 'working', 'merging', 'executing'].includes(status)) return 'in-progress';
  return 'pending';
}

function PullRequests({ run }: { run: RunDetail }) {
  const recorded = recordedPullRequests(run);
  const builders = Object.values(run.work_items ?? {}).filter(item => item.kind === 'builder');
  const rows = builders.map(item => {
    const pr = recorded.find(pr => pr.work_id === item.work_id);
    return {
      id: item.work_id, agent: item.agent, role: item.capability,
      url: pr?.pr_url || item.pr?.pr_url,
      state: pr?.state || item.merge_state || item.state,
      branch: item.worktree_branch || item.branch,
      turns: item.attempt, refreshes: item.dependency_refreshes,
      error: pr?.error,
    };
  });
  for (const pr of recorded) {
    if (!rows.some(row => row.id === pr.work_id)) rows.push({
      id: pr.work_id, agent: pr.agent, role: pr.role, url: pr.pr_url || undefined,
      state: pr.state, branch: '', turns: undefined!, refreshes: undefined, error: pr.error,
    });
  }
  return <ResourceTable title="Pull requests" variant="embedded" items={rows} trackBy="id" showCounter={false}
    description="Follow each builder from work in progress to its checked and reviewed pull request."
    searchText={row => `${row.id} ${row.agent} ${row.role} ${row.state}`}
    empty={<SpaceBetween size="s"><Box variant="strong">No pull requests recorded</Box>
      <Box color="text-body-secondary">The coordinator opens a pull request after a builder produces work.</Box></SpaceBetween>}
    columns={[
      { id: 'role', header: 'Role', sortingField: 'role', minWidth: 160, cell: row => <SpaceBetween direction="horizontal" size="xs">
        <AgentIcon agentId={row.agent} size={20} /><div><Box variant="strong">{label(row.role || row.agent)}</Box>
          <Box color="text-body-secondary" fontSize="body-s">{agentInstanceLabel(row.agent)}</Box></div></SpaceBetween> },
      { id: 'pr', header: 'Pull request', minWidth: 180, cell: row => row.url ? <Link external href={row.url}>Open pull request</Link> : 'Not opened' },
      { id: 'state', header: 'Status', sortingField: 'state', minWidth: 200, cell: row => <SpaceBetween size="xs">
        <StatusIndicator type={statusType(row.state)}>{row.state === 'awaiting_review' ? 'Awaiting your review' : label(row.state)}</StatusIndicator>
        {!!row.error && <Box color="text-status-error">{row.error}</Box>}</SpaceBetween> },
      { id: 'turns', header: 'Build turns', sortingField: 'turns', minWidth: 120, cell: row => row.turns ?? 'Not recorded' },
      { id: 'branch', header: 'Worktree branch', minWidth: 260, cell: row => <code className="console-code">{row.branch || 'Not recorded'}</code> },
      { id: 'refreshes', header: 'Base refreshes', minWidth: 140, cell: row => row.refreshes ?? 'Not recorded' },
    ]} />;
}

function Checks({ gates }: { gates: GateRecord[] }) {
  return <ResourceTable title="Executable checks" variant="embedded" items={gates}
    trackBy={gate => `${gate.sequence}-${gate.stage}`} sortingField="sequence"
    description="The engine runs the validator's executable and records its exit result. Failed checks remain in the history after a repair."
    searchText={gate => `${gate.sequence} ${gate.stage} ${gate.summary}`}
    empty={<Box>No executable checks have been recorded.</Box>}
    columns={[
      { id: 'sequence', header: 'Execution', sortingField: 'sequence', minWidth: 120, cell: gate => gate.sequence },
      { id: 'stage', header: 'Stage', sortingField: 'stage', minWidth: 160, cell: gate => gate.stage },
      { id: 'result', header: 'Result', minWidth: 130, cell: gate => <StatusIndicator type={gate.passed ? 'success' : 'error'}>{gate.passed ? 'Passed' : 'Failed'}</StatusIndicator> },
      { id: 'evidence', header: 'Recorded evidence', minWidth: 360, cell: gate => <SpaceBetween size="xs">
        <Box>{gate.summary || 'No summary recorded'}</Box>
        {!!gate.checks?.length && <ExpandableSection headerText={`${gate.checks.length} recorded assertions`}>
          <SpaceBetween size="s">{gate.checks.map((check, i) => <div key={i}>
            <StatusIndicator type={check.passed === true ? 'success' : check.passed === false ? 'error' : 'pending'}>
              {check.check || `Assertion ${i + 1}`}
            </StatusIndicator>
            {check.detail && <Box color="text-body-secondary">{check.detail}</Box>}
          </div>)}</SpaceBetween>
        </ExpandableSection>}
      </SpaceBetween> },
    ]} />;
}

function Reviews({ review }: { review: RunDetail['review'] }) {
  const panels = review?.panels ?? [];
  return <SpaceBetween size="l">
    <Header variant="h2" description="A separate reviewer inspects the pull request using adversarial and design/integration lenses.">Independent review</Header>
    <KeyValuePairs columns={2} items={[
      { label: 'Review status', value: review?.state ? <StatusIndicator type={statusType(review.state)}>{label(review.state)}</StatusIndicator> : 'Not recorded' },
      { label: 'Review round', value: review?.round ?? 'Not recorded' },
    ]} />
    {!!review?.reasons?.length && <Alert type="warning" header="Review findings"><TextContent><ul>
      {review.reasons.map((reason, i) => <li key={i}>{reason}</li>)}
    </ul></TextContent></Alert>}
    {panels.length ? panels.map(panel => <ExpandableSection key={panel.name} variant="container" defaultExpanded
      headerText={panel.label || label(panel.name)} headerActions={<StatusIndicator type={statusType(panel.state)}>{label(panel.state)}</StatusIndicator>}>
      <SpaceBetween size="m">
        {!!panel.reasons?.length && <TextContent><ul>{panel.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></TextContent>}
        {panel.assessment && <Box>{panel.assessment}</Box>}
        {panel.lenses?.adversarial && <KeyValuePairs items={[{ label: 'Adversarial verification', value: panel.lenses.adversarial }]} />}
        {panel.lenses?.design && <KeyValuePairs items={[{ label: 'Design and integration', value: panel.lenses.design }]} />}
        {panel.note && <Box color="text-body-secondary">{panel.note}</Box>}
        {panel.model && <Box fontSize="body-s" color="text-body-secondary">Model: {panel.model}</Box>}
      </SpaceBetween>
    </ExpandableSection>) : <Box color="text-body-secondary">No review panels have been recorded.</Box>}
    {review?.assessment && !panels.length && <Box>{review.assessment}</Box>}
  </SpaceBetween>;
}

function SharedPlan({ run }: { run: RunDetail }) {
  const brief = run.integration_brief;
  if (!brief) return <Box color="text-body-secondary">No shared plan has been recorded.</Box>;
  return <SpaceBetween size="l">
    <Header variant="h2">Shared plan</Header>
    {brief.summary && <Box>{brief.summary}</Box>}
    {!!brief.shared_contract?.length && <TextContent><h3>Shared contract</h3><ul>
      {brief.shared_contract.map((row, i) => <li key={i}>{row}</li>)}
    </ul></TextContent>}
    {Object.entries(brief.role_assignments ?? {}).map(([id, assignment]) => <ExpandableSection key={id} headerText={agentInstanceLabel(id)} defaultExpanded>
      <KeyValuePairs columns={1} items={[
        { label: 'Objective', value: assignment.objective || 'Not recorded' },
        { label: 'Provides', value: assignment.provides?.join(', ') || 'Not recorded' },
        { label: 'Consumes', value: assignment.consumes?.join(', ') || 'Not recorded' },
      ]} />
    </ExpandableSection>)}
    {!!brief.open_questions?.length && <TextContent><h3>Open questions</h3><ul>
      {brief.open_questions.map((question, i) => <li key={i}>{question}</li>)}
    </ul></TextContent>}
  </SpaceBetween>;
}

export function RunDetailPanel({ run }: { run: RunDetail }) {
  const decision = presentRunDecision(run);
  const prs = recordedPullRequests(run);
  const gates = run.gate_history ?? [];
  return <SpaceBetween size="l">
    <Alert type={decision.tone === 'danger' ? 'error' : decision.tone === 'waiting' ? 'warning' : decision.tone === 'success' ? 'success' : 'info'}
      header={decision.title}>
      {decision.detail}{run.next_action && <Box padding={{ top: 's' }}><strong>Next action:</strong> {run.next_action}</Box>}
    </Alert>
    <Container header={<Header variant="h2">Build details</Header>}>
      <SpaceBetween size="m">
        <KeyValuePairs columns={3} items={[
          { label: 'Build ID', value: <code className="console-code">{run.run_id}</code> },
          { label: 'Created', value: run.created_at ? new Date(run.created_at).toLocaleString() : 'Not recorded' },
          { label: 'Submitted by', value: run.submitted_by || 'Not recorded' },
          { label: 'Phase', value: run.phase ? label(run.phase) : 'Not recorded' },
          { label: 'Selected roles', value: run.route?.agents.map(agentInstanceLabel).join(', ') || 'Not recorded' },
          { label: 'Pull requests opened', value: prs.filter(pr => pr.pr_url).length },
        ]} />
        <ExpandableSection headerText="Original request">
          <TextContent><p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{run.task}</p></TextContent>
        </ExpandableSection>
      </SpaceBetween>
    </Container>
    <Tabs variant="container" ariaLabel="Build evidence" tabs={[
      { id: 'pull-requests', label: 'Pull requests', content: <PullRequests run={run} /> },
      { id: 'checks', label: `Executable checks (${gates.length})`, content: <Checks gates={gates} /> },
      { id: 'review', label: 'Independent review', content: <Reviews review={run.review} /> },
      { id: 'plan', label: 'Shared plan', content: <SharedPlan run={run} /> },
    ]} />
  </SpaceBetween>;
}
