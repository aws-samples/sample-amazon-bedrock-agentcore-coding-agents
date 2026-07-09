import { useEffect, useState } from 'react';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
} from '@foxl/ui';
import {
  GithubStatus,
  MergePolicy,
  RuntimeStatus,
  KiroStatus,
  clearGithubCredential,
  getGithubStatus,
  saveGithubCredential,
  setMergePolicy,
  getKiroStatus,
  saveKiroKey,
  clearKiroKey,
  getRuntimes,
  wireRuntime,
  addRuntime,
  removeRuntime,
  describeRuntime,
} from '../api';
import { Plus, X } from 'lucide-react';
import { AgentIcon } from '../components/AgentIcon';
import { agentRole } from './agents/environments';

// Friendly display name for a role id (Claude Code, Kiro CLI, opencode). The
// orchestrator role has no agent card, so it keeps its id.
function roleName(role: string): string {
  return role === 'orchestrator' ? 'Orchestrator' : agentRole(role).label;
}

export function SettingsPage() {
  const [status, setStatus] = useState<GithubStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [repo, setRepo] = useState('');
  const [formError, setFormError] = useState('');

  const applyStatus = (s: GithubStatus) => {
    setStatus(s);
    setRepo(s.repo ?? '');
  };

  useEffect(() => {
    getGithubStatus()
      .then(applyStatus)
      .finally(() => setLoading(false));
  }, []);

  // Connect the PR destination: only the attendee's template-derived repo
  // (owner/name). NO token -- the GitHub App credential lives inside the GitHub
  // MCP Gateway, and the orchestrator opens the PR by calling the gateway's MCP
  // tools over SigV4. The gateway URL is wired by the workshop (env).
  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setFormError('');
    if (!repo.trim() || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo.trim())) {
      setFormError('Repo must be in owner/name format.');
      return;
    }
    setSaving(true);
    try {
      const next = await saveGithubCredential({ repo: repo.trim() });
      if ('error' in next && next.error) {
        setFormError(String(next.error));
      } else {
        applyStatus(next);
      }
    } catch (err: unknown) {
      setFormError(err instanceof Error ? err.message : 'Save failed.');
    } finally {
      setSaving(false);
    }
  }

  async function handleClear() {
    setClearing(true);
    try {
      const next = await clearGithubCredential();
      setStatus(next);
      setRepo('');
    } catch {
      // status unchanged on error
    } finally {
      setClearing(false);
    }
  }

  const [policySaving, setPolicySaving] = useState(false);
  async function handlePolicy(next: MergePolicy) {
    if (policySaving || status?.merge_policy === next) return;
    setPolicySaving(true);
    try {
      setStatus(await setMergePolicy(next));
    } catch {
      // status unchanged on error
    } finally {
      setPolicySaving(false);
    }
  }

  return (
    <div className="animate-enter-up mx-auto w-full max-w-3xl px-6 py-10 space-y-6">
      <div className="space-y-1">
        <div className="eyebrow">Configuration</div>
        <h1 className="text-2xl font-semibold tracking-[-0.02em]">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Point runs at your repository. The GitHub MCP Gateway opens the pull request;
          no personal access token is ever stored here.
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="eyebrow">Pull request destination</div>
          <div className="flex items-center gap-2">
            <CardTitle>GitHub MCP Gateway</CardTitle>
            {status?.connected && (
              <Badge variant="secondary" className="text-xs">
                {status.tool_count ? `${status.tool_count} tools` : 'connected'}
              </Badge>
            )}
            {status && !status.connected && (
              <Badge variant="outline" className="text-xs text-muted-foreground">
                not connected
              </Badge>
            )}
          </div>
          <CardDescription>
            A run opens its pull request through an IAM-authenticated AgentCore Gateway
            backed by a GitHub App, so no token lives in the console. Set the repository
            the PR lands in. Until the gateway answers, runs compose locally and the PR
            url stays empty.
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-5">
          {loading && (
            <p className="text-sm text-muted-foreground">Loading status...</p>
          )}

          {!loading && status?.connected && (
            <div className="rounded-md border bg-muted/40 px-4 py-3 space-y-1 text-sm">
              <div className="flex items-center gap-2">
                <span className="text-muted-foreground w-24 shrink-0">Repository</span>
                <span className="font-mono font-medium">{status.repo}</span>
              </div>
              {status.gateway_url && (
                <div className="flex items-center gap-2">
                  <span className="text-muted-foreground w-24 shrink-0">Gateway</span>
                  <span className="font-mono text-xs text-muted-foreground truncate">
                    {status.gateway_url}
                  </span>
                </div>
              )}
              {status.target && (
                <div className="flex items-center gap-2">
                  <span className="text-muted-foreground w-24 shrink-0">MCP target</span>
                  <span className="font-mono text-xs">{status.target}</span>
                </div>
              )}
            </div>
          )}

          {!loading && status?.error && (
            <p className="text-sm text-destructive">{status.error}</p>
          )}

          {!loading && !status?.connected && status?.hint && (
            <p className="text-sm text-muted-foreground">{status.hint}</p>
          )}

          {!loading && (
            <form onSubmit={handleSave} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="github-repo">Repository</Label>
                <Input
                  id="github-repo"
                  type="text"
                  placeholder="owner/repo"
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  disabled={saving || status?.source === 'environment'}
                />
                <p className="text-xs text-muted-foreground">
                  Your repo created from the{' '}
                  <code className="font-mono text-xs">{status?.workshop_repo ?? 'workshop'}</code>{' '}
                  template (<span className="font-medium">Use this template</span> on GitHub).
                </p>
              </div>

              {status?.source === 'environment' && (
                <p className="text-sm text-muted-foreground">
                  The repository and gateway are set via{' '}
                  <code className="font-mono text-xs">GITHUB_REPO</code> and{' '}
                  <code className="font-mono text-xs">GITHUB_GATEWAY_URL</code>{' '}
                  environment variables.
                </p>
              )}

              {formError && (
                <p className="text-sm text-destructive">{formError}</p>
              )}

              {status?.source !== 'environment' && (
                <div className="flex items-center gap-3">
                  <Button type="submit" disabled={saving}>
                    {saving ? 'Saving...' : status?.connected ? 'Update' : 'Connect'}
                  </Button>
                  {status?.connected && (
                    <Button
                      type="button"
                      variant="outline"
                      disabled={clearing}
                      onClick={handleClear}
                    >
                      {clearing ? 'Disconnecting...' : 'Disconnect'}
                    </Button>
                  )}
                </div>
              )}
            </form>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="eyebrow">Workflow</div>
          <div className="flex items-center gap-2">
            <CardTitle>Merge policy</CardTitle>
            {status && (
              <Badge variant={status.merge_policy === 'auto' ? 'default' : 'secondary'} className="text-xs">
                {status.merge_policy === 'auto' ? 'auto-merge on' : 'human review'}
              </Badge>
            )}
          </div>
          <CardDescription>
            How a run finishes after the reviewer approves. Auto-merge posts a bot approval and
            squash-merges into the <code className="font-mono">workshop/integration</code> branch,
            never your default branch. Off by default.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {(['human_review', 'auto'] as const).map((policy) => {
              const active = (status?.merge_policy ?? 'human_review') === policy;
              return (
                <button
                  key={policy}
                  type="button"
                  disabled={policySaving}
                  onClick={() => handlePolicy(policy)}
                  className={`rounded-lg border p-3 text-left text-sm transition ${
                    active
                      ? 'border-primary bg-primary/5 ring-1 ring-primary'
                      : 'border-border hover:bg-accent'
                  } disabled:opacity-60`}
                >
                  <div className="font-medium">
                    {policy === 'auto' ? 'Auto-merge' : 'Human review'}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {policy === 'auto'
                      ? 'Bot approves, squash-merges to integration. Fully autonomous.'
                      : 'Open the PR and stop. A human merges. (Default.)'}
                  </div>
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <RuntimesCard />
    </div>
  );
}

// Wire each role's deployed AgentCore runtime ARN. Nothing is hardcoded: the ARN
// is whatever deploy.py wrote to runtime_config.json. The orchestrator dispatches a role to
// its runtime when WORKSHOP_EXECUTOR=agentcore; a missing ARN fails loud (no local
// fallback). Same config surface the orchestrator reads from runtime_config
// writes to. Follows the GitHub card's pattern.
function RuntimesCard() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [descDrafts, setDescDrafts] = useState<Record<string, string>>({});
  // Per-role drafts for the "Add agent" form: a description and (kiro only) the
  // API key entered alongside the ARN/URL.
  const [addDescDrafts, setAddDescDrafts] = useState<Record<string, string>>({});
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  // Kiro's API key lives in the Token Vault, NOT on the runtime ARN — so it is
  // attached to an ALREADY-wired Kiro runtime separately (the event pre-creates the
  // Kiro runtime; the attendee only adds the ksk_ key). We track its presence to
  // show "key set / no key" on the wired Kiro instance and to drive the inline
  // "+ Add API key" editor below (mirrors the "+ Add description" affordance).
  const [kiro, setKiro] = useState<KiroStatus | null>(null);
  const [kiroKeyOpen, setKiroKeyOpen] = useState(false);
  const [kiroKeyDraft, setKiroKeyDraft] = useState('');
  // Which role's "add another instance" input is expanded. Collapsed by default
  // (R11): a wired role just shows its ARN(s); the add-instance field appears
  // only when you click "+ Add instance".
  const [addOpen, setAddOpen] = useState<string | null>(null);
  // Which role's description editor is open. Collapsed by default (R24): a saved
  // description shows as one line; clicking it (or "+ Add description") opens the
  // input. So no empty "What this agent does" field clutters the card.
  const [descOpen, setDescOpen] = useState<string | null>(null);

  const applyStatus = (s: RuntimeStatus) => {
    setStatus(s);
    // Seed the per-instance description drafts (keyed by ARN) from the saved
    // values so the editor opens pre-filled.
    setDescDrafts((prev) => {
      const next = { ...prev };
      for (const r of s.roles)
        for (const inst of r.instances ?? [])
          if (next[inst.arn] === undefined) next[inst.arn] = inst.description ?? '';
      return next;
    });
  };

  useEffect(() => {
    getRuntimes().then(applyStatus).catch(() => {}).finally(() => setLoading(false));
    getKiroStatus().then(setKiro).catch(() => {});
  }, []);

  // Attach (or replace) the Kiro API key on the already-wired Kiro runtime. This
  // does NOT touch the ARN — it stores the ksk_ key in the Token Vault so the
  // pre-created runtime can authenticate with no redeploy.
  async function saveKiro(arn: string) {
    const key = kiroKeyDraft.trim();
    if (!key || busy) return;
    setBusy(arn);
    setError('');
    try {
      const next = await saveKiroKey(key);
      if (next.error) setError(next.error);
      else {
        setKiro(next);
        setKiroKeyOpen(false);
        setKiroKeyDraft('');
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Saving the Kiro key failed.');
    } finally {
      setBusy(null);
    }
  }

  async function removeKiro(arn: string) {
    setBusy(arn);
    try {
      setKiro(await clearKiroKey());
    } catch { /* unchanged on error */ } finally {
      setBusy(null);
    }
  }

  async function saveDescription(role: string, arn: string) {
    setBusy(arn);
    setError('');
    try {
      const next = await describeRuntime(role, arn, descDrafts[arn] ?? '');
      if (next.error) setError(next.error);
      else applyStatus(next);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Saving description failed.');
    } finally {
      setBusy(null);
    }
  }

  // `grow` true ADDS another agent to the role's fleet (2nd opencode, 3rd Claude
  // Code, …); false wires a single agent. Dispatch round-robins across a fleet.
  // Carries the optional description and (kiro only) the API key entered in the
  // same form, so one "Add agent" submit wires the ARN/URL, stores the key in the
  // Token Vault, and records the description together.
  async function wire(role: string, grow = false) {
    const arn = (drafts[role] ?? '').trim();
    if (!arn || busy) return;
    setBusy(role);
    setError('');
    try {
      const input = {
        arn,
        description: (addDescDrafts[role] ?? '').trim() || undefined,
        apiKey: role === 'kiro' ? ((keyDrafts[role] ?? '').trim() || undefined) : undefined,
      };
      const next = grow ? await addRuntime(role, input) : await wireRuntime(role, input);
      if (next.error) setError(next.error);
      else {
        applyStatus(next);
        setDrafts((d) => ({ ...d, [role]: '' }));
        setAddDescDrafts((d) => ({ ...d, [role]: '' }));
        setKeyDrafts((d) => ({ ...d, [role]: '' }));
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Wiring failed.');
    } finally {
      setBusy(null);
    }
  }

  async function removeInstance(role: string, arn: string) {
    setBusy(role);
    try {
      applyStatus(await removeRuntime(role, arn));
    } catch { /* unchanged on error */ } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="eyebrow">Runtime</div>
        <div className="flex items-center gap-2">
          <CardTitle>AgentCore runtimes</CardTitle>
          {status && (
            <Badge variant={status.remote_dispatch ? 'default' : 'secondary'} className="text-xs">
              {status.remote_dispatch ? 'dispatching to Runtime' : 'no runtime wired'}
            </Badge>
          )}
        </div>
        <CardDescription>
          Wire each agent's runtime ARN so the orchestrator can dispatch to it. The event
          pre-provisions the opencode and validator runtimes (both Bedrock-native, no key), so
          you only paste their ARN; the backend Claude Code is the one you build and deploy by hand.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading && <p className="text-sm text-muted-foreground">Loading runtimes...</p>}
        {!loading && status?.roles.map((r) => {
          // A role is a FLEET: instances[] is every deployed runtime wired to it
          // (env fleets are comma-separated; settings fleets grow via "add").
          const insts = r.instances ?? (r.wired && r.arn ? [{ arn: r.arn, source: r.source as 'environment' | 'settings' }] : []);
          const isEnv = r.source === 'environment';
          return (
            // Each role is its own bordered section (R11): header row, then its
            // ARN(s), an optional description, and a collapsed "+ Add instance".
            <div key={r.role} className="space-y-2 rounded-lg border border-border p-3">
              <div className="flex items-center gap-2">
                <AgentIcon agentId={r.role} size={16} />
                <Label className="font-medium">{roleName(r.role)}</Label>
                <span className="font-mono text-[11px] text-muted-foreground">{r.role}</span>
                {r.wired ? (
                  <Badge variant="secondary" className="text-xs">{isEnv ? 'env var' : 'console'}</Badge>
                ) : (
                  <Badge variant="outline" className="text-xs text-muted-foreground">not wired</Badge>
                )}
                {(r.count ?? insts.length) > 1 && (
                  <Badge variant="outline" className="text-xs">fleet of {r.count ?? insts.length}</Badge>
                )}
              </div>

              {r.wired ? (
                <div className="space-y-2">
                  {/* Each INSTANCE is its own sub-card: its ARN, a per-instance x,
                      and its own collapsed description (R25). */}
                  {insts.map((inst) => {
                    const desc = inst.description ?? '';
                    return (
                      <div key={inst.arn} className="space-y-1.5 rounded-md border border-border/60 bg-muted/20 p-2">
                        <div className="flex items-center gap-2">
                          <code className="flex-1 break-all font-mono text-xs">{inst.arn}</code>
                          {!isEnv && (
                            <button
                              type="button"
                              disabled={busy === inst.arn}
                              onClick={() => { setAddOpen(null); removeInstance(r.role, inst.arn); }}
                              title="Remove this runtime"
                              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive"
                            >
                              <X className="size-3.5" />
                            </button>
                          )}
                        </div>
                        {/* Per-instance description, collapsed until clicked (R24/R25). */}
                        {r.role !== 'orchestrator' && !isEnv && (
                          descOpen === inst.arn ? (
                            <div className="flex items-center gap-2">
                              <Input
                                placeholder="What this instance does (used to route tasks)"
                                value={descDrafts[inst.arn] ?? ''}
                                onChange={(e) => setDescDrafts((d) => ({ ...d, [inst.arn]: e.target.value }))}
                                disabled={busy === inst.arn}
                                className="text-xs"
                                autoFocus
                              />
                              <Button type="button" variant="outline" size="sm"
                                disabled={busy === inst.arn}
                                onClick={async () => { await saveDescription(r.role, inst.arn); setDescOpen(null); }}>
                                {busy === inst.arn ? '...' : 'Save'}
                              </Button>
                              <Button type="button" variant="ghost" size="sm" onClick={() => setDescOpen(null)}>
                                Cancel
                              </Button>
                            </div>
                          ) : desc ? (
                            <button
                              type="button"
                              onClick={() => setDescOpen(inst.arn)}
                              className="block w-full truncate text-left text-xs text-muted-foreground hover:text-foreground"
                              title="Edit description"
                            >
                              {desc}
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => setDescOpen(inst.arn)}
                              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                            >
                              <Plus className="size-3" /> Add description
                            </button>
                          )
                        )}
                        {/* Kiro's credential is the API key, NOT the ARN: the event pre-creates
                            the Kiro runtime, so the attendee only ATTACHES a ksk_ key to this
                            already-wired instance. Collapsed like the description (R24): shows
                            "key set" once stored, or "+ Add API key" to open the field. The key
                            is stored in the Token Vault via the credential provider, never on the
                            ARN. */}
                        {r.role === 'kiro' && !isEnv && (
                          kiroKeyOpen ? (
                            <div className="flex items-center gap-2">
                              <Input
                                type="password"
                                placeholder="ksk_..."
                                value={kiroKeyDraft}
                                onChange={(e) => setKiroKeyDraft(e.target.value)}
                                disabled={busy === inst.arn}
                                className="font-mono text-xs"
                                autoComplete="off"
                                autoFocus
                              />
                              <Button type="button" variant="outline" size="sm"
                                disabled={busy === inst.arn || !kiroKeyDraft.trim()}
                                onClick={() => saveKiro(inst.arn)}>
                                {busy === inst.arn ? '...' : 'Save'}
                              </Button>
                              <Button type="button" variant="ghost" size="sm"
                                onClick={() => { setKiroKeyOpen(false); setKiroKeyDraft(''); }}>
                                Cancel
                              </Button>
                            </div>
                          ) : kiro?.connected ? (
                            <div className="flex items-center gap-2 text-xs text-muted-foreground">
                              <Badge variant="secondary" className="text-xs">API key set</Badge>
                              <span className="font-mono">{kiro.key_tail ? `ksk_••••${kiro.key_tail}` : 'ksk_••••'}</span>
                              <button
                                type="button"
                                onClick={() => { setKiroKeyDraft(''); setKiroKeyOpen(true); }}
                                className="hover:text-foreground"
                              >
                                Replace
                              </button>
                              <button
                                type="button"
                                disabled={busy === inst.arn}
                                onClick={() => removeKiro(inst.arn)}
                                className="hover:text-destructive"
                              >
                                Remove
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              onClick={() => { setKiroKeyDraft(''); setKiroKeyOpen(true); }}
                              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                            >
                              <Plus className="size-3" /> Add API key <span className="text-muted-foreground/70">(stored in Token Vault)</span>
                            </button>
                          )
                        )}
                      </div>
                    );
                  })}
                  {/* "Add agent" collapsed by default (R11): a labeled form, not a
                      single field, so the ARN/URL, description, and (kiro) API key
                      each have their own row instead of overlapping placeholders. */}
                  {!isEnv && (addOpen === r.role ? (
                    <AgentForm
                      role={r.role}
                      arn={drafts[r.role] ?? ''}
                      desc={addDescDrafts[r.role] ?? ''}
                      apiKey={keyDrafts[r.role] ?? ''}
                      busy={busy === r.role}
                      submitLabel="Add agent"
                      onArn={(v) => setDrafts((d) => ({ ...d, [r.role]: v }))}
                      onDesc={(v) => setAddDescDrafts((d) => ({ ...d, [r.role]: v }))}
                      onApiKey={(v) => setKeyDrafts((d) => ({ ...d, [r.role]: v }))}
                      onSubmit={() => { wire(r.role, true); setAddOpen(null); }}
                      onCancel={() => setAddOpen(null)}
                    />
                  ) : (
                    <button
                      type="button"
                      onClick={() => setAddOpen(r.role)}
                      className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                    >
                      <Plus className="size-3" /> Add agent
                    </button>
                  ))}
                </div>
              ) : (
                <AgentForm
                  role={r.role}
                  arn={drafts[r.role] ?? ''}
                  desc={addDescDrafts[r.role] ?? ''}
                  apiKey={keyDrafts[r.role] ?? ''}
                  busy={busy === r.role}
                  submitLabel="Wire agent"
                  onArn={(v) => setDrafts((d) => ({ ...d, [r.role]: v }))}
                  onDesc={(v) => setAddDescDrafts((d) => ({ ...d, [r.role]: v }))}
                  onApiKey={(v) => setKeyDrafts((d) => ({ ...d, [r.role]: v }))}
                  onSubmit={() => wire(r.role)}
                />
              )}
            </div>
          );
        })}
        {error && <p className="text-sm text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}

// One agent's wire form: an ARN-or-URL field, an optional description, and (only
// for the kiro role) its API key. Each field is its own labeled row, so the
// placeholders never overlap. Submit is disabled until the ARN/URL is present.
function AgentForm({
  role, arn, desc, apiKey, busy, submitLabel,
  onArn, onDesc, onApiKey, onSubmit, onCancel,
}: {
  role: string;
  arn: string;
  desc: string;
  apiKey: string;
  busy: boolean;
  submitLabel: string;
  onArn: (v: string) => void;
  onDesc: (v: string) => void;
  onApiKey: (v: string) => void;
  onSubmit: () => void;
  onCancel?: () => void;
}) {
  const isKiro = role === 'kiro';
  return (
    <div className="space-y-2 rounded-md border border-border/60 bg-muted/10 p-2.5">
      <div className="space-y-1">
        <Label className="text-xs">Runtime ARN or dev URL</Label>
        <Input
          placeholder="https:// or arn:aws:bedrock-agentcore:..."
          value={arn}
          onChange={(e) => onArn(e.target.value)}
          disabled={busy}
          className="text-sm"
          autoFocus
        />
      </div>
      {role !== 'orchestrator' && (
        <div className="space-y-1">
          <Label className="text-xs">Description <span className="text-muted-foreground">(optional, used to route tasks)</span></Label>
          <Input
            placeholder="What this agent does"
            value={desc}
            onChange={(e) => onDesc(e.target.value)}
            disabled={busy}
            className="text-xs"
          />
        </div>
      )}
      {isKiro && (
        <div className="space-y-1">
          <Label className="text-xs">Kiro API key <span className="text-muted-foreground">(stored in Token Vault)</span></Label>
          <Input
            type="password"
            placeholder="ksk_..."
            value={apiKey}
            onChange={(e) => onApiKey(e.target.value)}
            disabled={busy}
            className="font-mono text-xs"
            autoComplete="off"
          />
        </div>
      )}
      <div className="flex items-center gap-2 pt-0.5">
        <Button type="button" size="sm" disabled={busy || !arn.trim()} onClick={onSubmit}>
          {busy ? '...' : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
        )}
      </div>
    </div>
  );
}
