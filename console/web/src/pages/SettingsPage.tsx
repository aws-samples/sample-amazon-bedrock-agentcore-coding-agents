import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import ContentLayout from '@cloudscape-design/components/content-layout';
import Form from '@cloudscape-design/components/form';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import Modal from '@cloudscape-design/components/modal';
import RadioGroup from '@cloudscape-design/components/radio-group';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Tabs from '@cloudscape-design/components/tabs';
import {
  type GithubStatus, type MergePolicy, type RuntimeStatus, type RuntimeSource, type KiroStatus,
  clearGithubCredential, getGithubStatus, saveGithubCredential, setMergePolicy,
  getKiroStatus, saveKiroKey, clearKiroKey, getRuntimes, wireRuntime, addRuntime, removeRuntime, describeRuntime,
} from '../api';
import { ResourceTable } from '../shared/ResourceTable';
import { AgentIcon } from '../components/AgentIcon';
import { toast } from '../components/ConsoleNotifications';
import { agentInstanceLabel, onAgentRoles } from './agents/environments';

const roleName = (role: string) => role === 'orchestrator' ? 'Coordinator' : agentInstanceLabel(role);
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'The request failed. Try again.';
type Connection = { role: string; arn: string; source: RuntimeSource; description?: string };
const sourceName = (source: RuntimeSource) => ({ settings: 'Console settings', environment: 'Environment', deployed: 'Deployment' })[source] || source;

export function SettingsPage() {
  const [search, setSearch] = useSearchParams();
  const tab = ['repository', 'connections', 'kiro'].includes(search.get('tab') || '') ? search.get('tab')! : 'repository';
  const [github, setGithub] = useState<GithubStatus | null>(null);
  const [runtimes, setRuntimes] = useState<RuntimeStatus | null>(null);
  const [kiro, setKiro] = useState<KiroStatus | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [repo, setRepo] = useState('');
  const [policy, setPolicy] = useState<MergePolicy>('human_review');
  const [confirm, setConfirm] = useState<'auto' | 'repo' | 'kiro' | null>(null);
  const [connectOpen, setConnectOpen] = useState(false);
  const [role, setRole] = useState('');
  const [arn, setArn] = useState('');
  const [description, setDescription] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [kiroDraft, setKiroDraft] = useState('');
  const [edit, setEdit] = useState<Connection | null>(null);
  const [remove, setRemove] = useState<Connection | null>(null);
  const [, setRosterVersion] = useState(0);
  const report = (key: string, value = '') => setErrors(prev => ({ ...prev, [key]: value }));
  useEffect(() => onAgentRoles(() => setRosterVersion(n => n + 1)), []);
  useEffect(() => {
    let live = true;
    void Promise.allSettled([
      getGithubStatus().then(next => { if (live) { setGithub(next); setRepo(next.repo || ''); setPolicy(next.merge_policy || 'human_review'); } }).catch(e => { if (live) report('repository', errorMessage(e)); }),
      getRuntimes().then(next => { if (live) { setRuntimes(next); setRole(next.roles[0]?.role || ''); } }).catch(e => { if (live) report('connections', errorMessage(e)); }),
      getKiroStatus().then(next => { if (live) setKiro(next); }).catch(e => { if (live) report('kiro', errorMessage(e)); }),
    ]).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);

  async function saveRepo() {
    const value = repo.trim();
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value)) { report('repository', 'Enter the repository in owner/name format.'); return; }
    setBusy('repository'); report('repository');
    try {
      const next = await saveGithubCredential({ repo: value });
      if (next.error) throw new Error(next.error);
      setGithub(next); setRepo(next.repo || ''); toast.success('Repository saved');
    } catch (e) { report('repository', errorMessage(e)); } finally { setBusy(''); }
  }
  async function savePolicy(next: MergePolicy) {
    setBusy('policy'); report('policy');
    try {
      const result = await setMergePolicy(next);
      if (result.error) throw new Error(result.error);
      setGithub(result); setPolicy(result.merge_policy || next); setConfirm(null); toast.success('Merge policy saved');
    } catch (e) { report('policy', errorMessage(e)); } finally { setBusy(''); }
  }
  async function disconnectRepo() {
    setBusy('repository'); report('repository');
    try {
      const next = await clearGithubCredential();
      if (next.error) throw new Error(next.error);
      setGithub(next); setRepo(next.repo || ''); setConfirm(null); toast.success('Repository disconnected');
    } catch (e) { report('repository', errorMessage(e)); } finally { setBusy(''); }
  }
  async function connect() {
    if (!role || !arn.trim()) return;
    setBusy('connect'); report('connect');
    try {
      const input = { arn: arn.trim(), description: description.trim() || undefined,
        apiKey: role === 'kiro' ? apiKey.trim() || undefined : undefined };
      const next = runtimes?.roles.find(r => r.role === role)?.wired ? await addRuntime(role, input) : await wireRuntime(role, input);
      if (next.error) throw new Error(next.error);
      setRuntimes(next); setConnectOpen(false); setArn(''); setDescription(''); setApiKey('');
      if (role === 'kiro' && input.apiKey) void getKiroStatus().then(setKiro).catch(e => report('kiro', errorMessage(e)));
      toast.success('Runtime connection saved');
    } catch (e) { report('connect', errorMessage(e)); } finally { setBusy(''); }
  }
  async function saveDescription() {
    if (!edit) return;
    setBusy('description'); report('description');
    try {
      const next = await describeRuntime(edit.role, edit.arn, description);
      if (next.error) throw new Error(next.error);
      setRuntimes(next); setEdit(null); toast.success('Description saved');
    } catch (e) { report('description', errorMessage(e)); } finally { setBusy(''); }
  }
  async function removeConnection() {
    if (!remove) return;
    setBusy('remove'); report('remove');
    try {
      const next = await removeRuntime(remove.role, remove.arn);
      if (next.error) throw new Error(next.error);
      setRuntimes(next); setRemove(null); toast.success('Runtime connection removed');
    } catch (e) { report('remove', errorMessage(e)); } finally { setBusy(''); }
  }
  async function saveKey(clear = false) {
    setBusy('kiro'); report('kiro');
    try {
      const next = clear ? await clearKiroKey() : await saveKiroKey(kiroDraft.trim());
      if (next.error) throw new Error(next.error);
      setKiro(next); setKiroDraft(''); setConfirm(null); toast.success(clear ? 'Kiro key removed' : 'Kiro key saved');
    } catch (e) { report('kiro', errorMessage(e)); } finally { setBusy(''); }
  }
  async function refreshKiro() {
    setBusy('kiro-refresh'); report('kiro');
    try { setKiro(await getKiroStatus(true)); }
    catch (e) { report('kiro', errorMessage(e)); }
    finally { setBusy(''); }
  }
  const connections: Connection[] = runtimes?.roles.flatMap(r =>
    (r.instances ?? (r.wired && r.arn ? [{ arn: r.arn, source: r.source!, description: r.description }] : []))
      .map(instance => ({ ...instance, role: r.role }))) ?? [];
  const footer = (cancel: () => void, action: () => void, text: string, disabled = false) => <Box float="right"><SpaceBetween direction="horizontal" size="xs">
    <Button variant="link" disabled={!!busy} onClick={cancel}>Cancel</Button>
    <Button variant="primary" loading={!!busy} disabled={disabled} onClick={action}>{text}</Button>
  </SpaceBetween></Box>;

  return <ContentLayout header={<Header variant="h1" description="Manage the repository, Runtime connections, and credentials used by this host.">Settings</Header>}>
    <Tabs activeTabId={tab} onChange={({ detail }) => setSearch({ tab: detail.activeTabId })} tabs={[
      { id: 'repository', label: 'Repository and merging', content: <SpaceBetween size="l">
        <Container header={<Header variant="h2" actions={<StatusIndicator type={loading ? 'loading' : github?.connected ? 'success' : 'pending'}>
          {loading ? 'Loading' : github?.connected ? 'Configured' : 'Not configured'}</StatusIndicator>}>GitHub repository</Header>}>
          <form onSubmit={e => { e.preventDefault(); void saveRepo(); }}><Form errorText={errors.repository || github?.error}
            actions={github?.source !== 'environment' ? <SpaceBetween direction="horizontal" size="xs">
              {github?.connected && <Button disabled={!!busy} onClick={() => setConfirm('repo')}>Disconnect</Button>}
              <Button variant="primary" formAction="submit" loading={busy === 'repository'} disabled={loading || !!busy}>Save repository</Button>
            </SpaceBetween> : undefined}>
            <SpaceBetween size="l">
              <FormField label="Repository" description="Use your private app repository, initialized with a README." constraintText="Format: owner/repository">
                <Input value={repo} onChange={({ detail }) => setRepo(detail.value)} placeholder="owner/repository"
                  disabled={loading || !!busy || github?.source === 'environment'} autoComplete={false} />
              </FormField>
              {github?.source === 'environment' && <Alert type="info">This repository is managed by the host environment.</Alert>}
              {github?.hint && !github.connected && <Box color="text-body-secondary">{github.hint}</Box>}
              {github?.connected && <KeyValuePairs columns={2} items={[
                { label: 'Default branch', value: github.default_branch || 'Not returned' },
                { label: 'Gateway', value: <code className="console-code">{github.gateway_url || 'Not returned'}</code> },
                { label: 'MCP target', value: github.target || 'Not returned' },
                { label: 'Configuration source', value: github.source || 'Not returned' },
              ]} />}
            </SpaceBetween>
          </Form></form>
        </Container>
        <Container header={<Header variant="h2" description="Each pull request must pass its executable check and independent review before it is eligible to merge.">Merge policy</Header>}>
          <Form errorText={errors.policy} actions={<Button variant="primary" disabled={loading || !github || policy === github.merge_policy || !!busy}
            loading={busy === 'policy'} onClick={() => policy === 'auto' ? setConfirm('auto') : void savePolicy(policy)}>Save policy</Button>}>
            <RadioGroup value={policy} onChange={({ detail }) => setPolicy(detail.value as MergePolicy)}
              items={[
                { value: 'human_review', label: 'Human review', disabled: loading || !!busy, description: 'Leave approved pull requests open for a person to review and merge.' },
                { value: 'auto', label: 'Automatic merge', disabled: loading || !!busy, description: 'Merge approved pull requests into the default branch, subject to branch protection.' },
              ]} />
          </Form>
        </Container>
      </SpaceBetween> },
      { id: 'connections', label: 'Runtime connections', content: <SpaceBetween size="l">
        {!!errors.connections && <Alert type="error" header="Could not load runtime connections">{errors.connections}</Alert>}
        <ResourceTable title="Runtime connections" items={connections} loading={loading} trackBy={row => `${row.role}:${row.arn}`}
          description="Connect the runtimes deployed in Labs 1 and 2. Multiple connections to one role form a fleet."
          searchText={row => `${roleName(row.role)} ${row.role} ${row.arn} ${row.description || ''} ${row.source}`}
          actions={<Button variant="primary" iconName="add-plus" disabled={loading || !runtimes || !!busy}
            onClick={() => { setArn(''); setDescription(''); setApiKey(''); report('connect'); setConnectOpen(true); }}>Connect runtime</Button>}
          empty={<SpaceBetween size="s"><Box variant="strong">{errors.connections ? 'Runtime connections unavailable' : 'No runtimes connected'}</Box>
            <Box color="text-body-secondary">{errors.connections ? 'Reload the page to try again.' : 'Connect a deployed Runtime ARN or a local development URL.'}</Box></SpaceBetween>}
          columns={[
            { id: 'role', header: 'Role', sortingField: 'role', minWidth: 160, cell: row => <SpaceBetween direction="horizontal" size="xs" alignItems="center"><AgentIcon agentId={row.role} size={20} />{roleName(row.role)}</SpaceBetween> },
            { id: 'arn', header: 'Runtime ARN or URL', minWidth: 260, cell: row => <code className="console-code">{row.arn}</code> },
            { id: 'source', header: 'Source', sortingField: 'source', minWidth: 130, cell: row => sourceName(row.source) },
            { id: 'description', header: 'Description', minWidth: 340, cell: row => <SpaceBetween size="xs"><Box>{row.description || 'No description'}</Box>
              <Link variant="secondary" onFollow={() => { setEdit(row); setDescription(row.description || ''); report('description'); }}>Edit description</Link></SpaceBetween> },
            { id: 'actions', header: 'Actions', minWidth: 150, cell: row => row.source === 'settings' ? <Button onClick={() => { setRemove(row); report('remove'); }}>Remove</Button>
              : <Box color="text-body-secondary">Managed by {row.source === 'environment' ? 'environment' : 'deployment'}</Box> },
          ]} />
      </SpaceBetween> },
      { id: 'kiro', label: 'Kiro access', content: <Container header={<Header variant="h2"
        actions={<Button iconName="refresh" loading={busy === 'kiro-refresh'} disabled={loading || !!busy} onClick={() => void refreshKiro()}>Refresh status</Button>}
        description="The console discovers credentials provisioned in the AgentCore Identity Token Vault, including those saved from the workshop terminal. Kiro loads the key at session start.">Kiro credential</Header>}>
        <form onSubmit={e => { e.preventDefault(); void saveKey(); }}><Form errorText={errors.kiro || kiro?.error}
          actions={<SpaceBetween direction="horizontal" size="xs">
            {kiro?.connected && <Button disabled={!!busy} onClick={() => setConfirm('kiro')}>Remove key</Button>}
            <Button variant="primary" formAction="submit" disabled={loading || !kiroDraft.trim() || !!busy} loading={busy === 'kiro'}>Save API key</Button>
          </SpaceBetween>}>
          <SpaceBetween size="l">
            <KeyValuePairs columns={2} items={[
              { label: 'Credential status', value: <StatusIndicator type={loading ? 'loading' : kiro?.connected ? 'success' : kiro?.connected === false ? 'pending' : 'error'}>{loading ? 'Loading' : kiro?.connected ? 'Configured' : kiro?.connected === false ? 'Not configured' : 'Unable to verify'}</StatusIndicator> },
              { label: 'Credential provider', value: kiro?.provider || 'Not returned' },
              { label: 'Region', value: kiro?.region || 'Not returned' },
              { label: 'Configuration source', value: kiro?.source === 'token-vault' ? 'Token Vault discovery' : kiro?.source === 'settings' ? 'Console settings' : 'Not available' },
            ]} />
            <FormField label={kiro?.connected ? 'Replacement API key' : 'API key'} description="Create your key in the Kiro web application, then paste it here.">
              <Input type="password" value={kiroDraft} onChange={({ detail }) => setKiroDraft(detail.value)} disabled={!!busy} autoComplete={false} placeholder="ksk_…" />
            </FormField>
          </SpaceBetween>
        </Form></form>
      </Container> },
    ]} />
    <Modal visible={connectOpen} header="Connect runtime" closeAriaLabel="Close runtime connection" onDismiss={() => { if (!busy) { setConnectOpen(false); setApiKey(''); } }}
      footer={footer(() => { setConnectOpen(false); setApiKey(''); }, () => void connect(), 'Connect', !arn.trim() || !role)}>
      <SpaceBetween size="l">
        {!!errors.connect && <Alert type="error">{errors.connect}</Alert>}
        <FormField label="Role"><Select selectedOption={role ? { value: role, label: roleName(role) } : null} disabled={!!busy}
          options={(runtimes?.roles ?? []).map(r => ({ value: r.role, label: roleName(r.role), disabled: r.source === 'environment' }))}
          onChange={({ detail }) => { setRole(detail.selectedOption.value!); setApiKey(''); }} /></FormField>
        <FormField label="Runtime ARN or development URL"><Input value={arn} disabled={!!busy} autoComplete={false} onChange={({ detail }) => setArn(detail.value)} /></FormField>
        <FormField label="Description" constraintText="Optional"><Input value={description} disabled={!!busy} onChange={({ detail }) => setDescription(detail.value)} /></FormField>
        {role === 'kiro' && <FormField label="Kiro API key" constraintText="Optional when a key is already configured. Stored in the Token Vault.">
          <Input value={apiKey} type="password" autoComplete={false} disabled={!!busy} onChange={({ detail }) => setApiKey(detail.value)} /></FormField>}
      </SpaceBetween>
    </Modal>
    <Modal visible={!!edit} header="Edit description" closeAriaLabel="Close description editor" onDismiss={() => { if (!busy) setEdit(null); }}
      footer={footer(() => setEdit(null), () => void saveDescription(), 'Save')}>
      <FormField label="Description" errorText={errors.description} description={<code className="console-code">{edit?.arn}</code>}>
        <Input value={description} disabled={!!busy} onChange={({ detail }) => setDescription(detail.value)} />
      </FormField>
    </Modal>
    <Modal visible={!!remove} header="Remove runtime connection?" closeAriaLabel="Cancel connection removal" onDismiss={() => { if (!busy) setRemove(null); }}
      footer={footer(() => setRemove(null), () => void removeConnection(), 'Remove')}>
      <SpaceBetween size="m"><Box>Remove this connection from the console configuration. The Runtime resource remains deployed.</Box>
        <code className="console-code">{remove?.arn}</code>{!!errors.remove && <Alert type="error">{errors.remove}</Alert>}</SpaceBetween>
    </Modal>
    <Modal visible={confirm !== null} header={confirm === 'auto' ? 'Enable automatic merge?' : confirm === 'repo' ? 'Disconnect repository?' : 'Remove Kiro key?'}
      closeAriaLabel="Cancel change" onDismiss={() => { if (!busy) setConfirm(null); }}
      footer={footer(() => setConfirm(null), () => { if (confirm === 'auto') void savePolicy('auto'); else if (confirm === 'repo') void disconnectRepo(); else void saveKey(true); },
        confirm === 'auto' ? 'Enable automatic merge' : confirm === 'repo' ? 'Disconnect' : 'Remove key')}>
      <SpaceBetween size="m"><Box>{confirm === 'auto' ? 'Future runs will merge each approved pull request into the default branch without waiting for a person.'
        : confirm === 'repo' ? 'Future builds will need a repository connection to publish pull requests. Existing pull requests remain in GitHub.'
        : 'New Kiro sessions will need a replacement key before they can authenticate.'}</Box>
        {(confirm === 'auto' ? errors.policy : confirm === 'repo' ? errors.repository : errors.kiro) && <Alert type="error">{confirm === 'auto' ? errors.policy : confirm === 'repo' ? errors.repository : errors.kiro}</Alert>}
      </SpaceBetween>
    </Modal>
  </ContentLayout>;
}
