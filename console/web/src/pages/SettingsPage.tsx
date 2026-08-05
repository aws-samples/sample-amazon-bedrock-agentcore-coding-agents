import { useEffect, useState } from 'react';
import {
  Badge,
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
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
import { agentInstanceLabel, onAgentRoles } from './agents/environments';

// Friendly display name for a runtime instance id (Claude Code - Backend builder,
// Claude Code - Validator (gate), OpenCode). The orchestrator role has no agent
// card, so it keeps its id. agentInstanceLabel resolves BOTH claude-code and
// claude-code-validator to distinct labels (the merged sidebar entry hosts both).
function roleName(role: string): string {
  return role === 'orchestrator' ? 'Orchestrator' : agentInstanceLabel(role);
}

export function SettingsPage() {
  const [status, setStatus] = useState<GithubStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [repo, setRepo] = useState('');
  const [formError, setFormError] = useState('');
  const [policySaving, setPolicySaving] = useState(false);
  const [policyError, setPolicyError] = useState('');
  const [confirmAuto, setConfirmAuto] = useState(false);

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
    } catch (err: unknown) {
      setFormError(
        err instanceof Error
          ? err.message
          : 'Could not disconnect the repository. Check the Gateway and try again.',
      );
    } finally {
      setClearing(false);
    }
  }

  async function handleMergePolicy(next: MergePolicy) {
    if (policySaving || status?.merge_policy === next) return;
    setPolicyError('');
    setPolicySaving(true);
    try {
      setStatus(await setMergePolicy(next));
    } catch (err: unknown) {
      setPolicyError(
        err instanceof Error
          ? err.message
          : 'Could not update the merge policy. Check the console service and try again.',
      );
    } finally {
      setPolicySaving(false);
    }
  }

  return (
    <div className="animate-enter-up mx-auto w-full max-w-3xl px-6 py-10 space-y-6">
      <div className="space-y-1">
        <div className="eyebrow">Configuration</div>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Point runs at your repository. The GitHub MCP Gateway opens one pull
          request per role; no personal access token is ever stored here.
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
            The Gateway opens one pull request per builder against your default
            branch. Each one runs its own executable check and its own independent
            review, then merges on its own: left open for you, or auto-merged.
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-5">
          {loading && (
            <p className="text-sm text-muted-foreground" aria-live="polite">Loading status…</p>
          )}

          {!loading && status?.connected && (
            <div className="space-y-1 rounded-md border bg-muted/40 px-4 py-3 text-sm">
              <div className="flex min-w-0 items-start gap-2">
                <span className="text-muted-foreground w-24 shrink-0">Repository</span>
                <span className="min-w-0 break-all font-mono font-medium" translate="no">
                  {status.repo}
                </span>
              </div>
              {status.default_branch && (
                <div className="flex min-w-0 items-start gap-2">
                  <span className="text-muted-foreground w-24 shrink-0">Base branch</span>
                  <span className="min-w-0 break-all font-mono text-xs" translate="no">
                    {status.default_branch}
                  </span>
                </div>
              )}
              {status.gateway_url && (
                <div className="flex min-w-0 items-start gap-2">
                  <span className="text-muted-foreground w-24 shrink-0">Gateway</span>
                  <span
                    className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground"
                    translate="no"
                    title={status.gateway_url}
                  >
                    {status.gateway_url}
                  </span>
                </div>
              )}
              {status.target && (
                <div className="flex min-w-0 items-start gap-2">
                  <span className="text-muted-foreground w-24 shrink-0">MCP target</span>
                  <span className="min-w-0 break-all font-mono text-xs" translate="no">
                    {status.target}
                  </span>
                </div>
              )}
            </div>
          )}

          {!loading && status?.error && (
            <p className="text-sm text-destructive" role="alert">{status.error}</p>
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
                  name="github-repo"
                  type="text"
                  placeholder="owner/repository…"
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
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
                <p className="text-sm text-destructive" role="alert">{formError}</p>
              )}

              {status?.source !== 'environment' && (
                <div className="flex items-center gap-3">
                  <Button type="submit" disabled={saving}>
                    {saving ? 'Saving…' : status?.connected ? 'Update Repository' : 'Connect Repository'}
                  </Button>
                  {status?.connected && (
                    <Button
                      type="button"
                      variant="outline"
                      disabled={clearing}
                      onClick={handleClear}
                    >
                      {clearing ? 'Disconnecting…' : 'Disconnect'}
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
          <div className="eyebrow">Merge policy</div>
          <div className="flex items-center gap-2">
            <CardTitle>Default Branch Merge</CardTitle>
            {status && (
              <Badge
                variant={status.merge_policy === 'auto' ? 'default' : 'secondary'}
                className="text-xs"
              >
                {status.merge_policy === 'auto' ? 'Auto-merge' : 'Human review'}
              </Badge>
            )}
          </div>
          <CardDescription>
            Every pull request is checked and reviewed before it can merge. This
            setting decides who performs that merge into your default branch.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div
            role="group"
            aria-label="Merge policy"
            className="inline-grid w-full grid-cols-2 rounded-md border border-border p-1 sm:w-auto"
          >
            <button
              type="button"
              disabled={policySaving}
              aria-pressed={(status?.merge_policy ?? 'human_review') === 'human_review'}
              onClick={() => void handleMergePolicy('human_review')}
              className={`min-h-10 px-4 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                (status?.merge_policy ?? 'human_review') === 'human_review'
                  ? 'bg-foreground text-background'
                  : 'hover:bg-accent'
              }`}
            >
              Human Review
            </button>
            <button
              type="button"
              disabled={policySaving}
              aria-pressed={status?.merge_policy === 'auto'}
              onClick={() => setConfirmAuto(true)}
              className={`min-h-10 px-4 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                status?.merge_policy === 'auto'
                  ? 'bg-foreground text-background'
                  : 'hover:bg-accent'
              }`}
            >
              Auto-Merge
            </button>
          </div>
          <p className="mt-3 max-w-xl text-xs text-muted-foreground">
            Auto-merge uses the exact version that passed its check and review, and
            still follows branch protection. A red pull request is never merged, and
            a rejected merge leaves that pull request open for a person.
          </p>
          {policyError && (
            <p className="mt-3 text-sm text-destructive" role="alert">
              {policyError}
            </p>
          )}
        </CardContent>
      </Card>

      <AlertDialog open={confirmAuto} onOpenChange={setConfirmAuto}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Auto-Merge Approved Pull Requests?</AlertDialogTitle>
            <AlertDialogDescription>
              Future runs will merge each pull request that passes its own check and
              review into the default branch without waiting for a person.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={policySaving}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={policySaving}
              onClick={() => void handleMergePolicy('auto')}
            >
              Enable Auto-Merge
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

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
  // Kiro's API key lives in the Token Vault, NOT on the runtime ARN, so it is
  // attached to an ALREADY-wired Kiro runtime separately (the event pre-creates the
  // Kiro runtime; the attendee only adds the ksk_ key). We track its presence to
  // show "key set / no key" on the wired Kiro instance and to drive the inline
  // "+ Add API key" editor below (mirrors the "+ Add description" affordance).
  const [kiro, setKiro] = useState<KiroStatus | null>(null);
  // Keyed by ARN, not a bare boolean: this editor renders INSIDE the per-instance
  // insts.map(), so one shared flag opened every Kiro instance's field at once and
  // they all shared a single draft. Mirrors the descOpen pattern below.
  const [kiroKeyOpen, setKiroKeyOpen] = useState<string | null>(null);
  const [kiroKeyDraft, setKiroKeyDraft] = useState('');
  // Which role's "add another instance" input is expanded. Collapsed by default
  // (R11): a wired role just shows its ARN(s); the add-instance field appears
  // only when you click "+ Add instance".
  const [addOpen, setAddOpen] = useState<string | null>(null);
  // Which role's description editor is open. Collapsed by default (R24): a saved
  // description shows as one line; clicking it (or "+ Add description") opens the
  // input. So no empty "What this agent does" field clutters the card.
  const [descOpen, setDescOpen] = useState<string | null>(null);
  // Bumped when the served roster arrives, so the role cards re-render with their
  // friendly labels (roleName reads the roster synchronously once it is cached).
  const [, setRosterVersion] = useState(0);

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
    // The roster gives each wired role its friendly label. Subscribing (rather than
    // just fetching) re-renders the cards once it lands, so a role shows its name
    // instead of its raw id.
    return onAgentRoles(() => setRosterVersion((n) => n + 1));
  }, []);

  // Attach (or replace) the Kiro API key on the already-wired Kiro runtime. This
  // does NOT touch the ARN. It stores the ksk_ key in the Token Vault so the
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
        setKiroKeyOpen(null);
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
          The coding-role runtimes are discovered from the Lab 1 runtime configs.
          Paste the coordinator ARN after its CLI deployment; use these controls only
          when replacing a runtime or adding another instance.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading && (
          <p className="text-sm text-muted-foreground" aria-live="polite">
            Loading runtimes…
          </p>
        )}
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
                <span className="font-medium">{roleName(r.role)}</span>
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
                              aria-label={`Remove ${roleName(r.role)} runtime`}
                              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            >
                              <X aria-hidden="true" className="size-3.5" />
                            </button>
                          )}
                        </div>
                        {/* Per-instance description, collapsed until clicked (R24/R25). */}
                        {r.role !== 'orchestrator' && !isEnv && (
                          descOpen === inst.arn ? (
                            <div className="flex items-center gap-2">
                              <Input
                                name={`description-${r.role}`}
                                aria-label={`${roleName(r.role)} runtime description`}
                                placeholder="Describe what this instance handles…"
                                value={descDrafts[inst.arn] ?? ''}
                                onChange={(e) => setDescDrafts((d) => ({ ...d, [inst.arn]: e.target.value }))}
                                disabled={busy === inst.arn}
                                className="text-xs"
                                autoComplete="off"
                              />
                              <Button type="button" variant="outline" size="sm"
                                disabled={busy === inst.arn}
                                onClick={async () => { await saveDescription(r.role, inst.arn); setDescOpen(null); }}>
                                {busy === inst.arn ? 'Saving…' : 'Save'}
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
                              <Plus aria-hidden="true" className="size-3" /> Add description
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
                          kiroKeyOpen === inst.arn ? (
                            <div className="flex items-center gap-2">
                              <Input
                                type="password"
                                name="kiro-api-key"
                                aria-label="Kiro API key"
                                placeholder="ksk_…"
                                value={kiroKeyDraft}
                                onChange={(e) => setKiroKeyDraft(e.target.value)}
                                disabled={busy === inst.arn}
                                className="font-mono text-xs"
                                autoComplete="off"
                                spellCheck={false}
                              />
                              <Button type="button" variant="outline" size="sm"
                                disabled={busy === inst.arn || !kiroKeyDraft.trim()}
                                onClick={() => saveKiro(inst.arn)}>
                                {busy === inst.arn ? 'Saving…' : 'Save'}
                              </Button>
                              <Button type="button" variant="ghost" size="sm"
                                onClick={() => { setKiroKeyOpen(null); setKiroKeyDraft(''); }}>
                                Cancel
                              </Button>
                            </div>
                          ) : kiro?.connected ? (
                            <div className="flex items-center gap-2 text-xs text-muted-foreground">
                              <Badge variant="secondary" className="text-xs">API key set</Badge>
                              <span className="font-mono">{kiro.key_tail ? `ksk_••••${kiro.key_tail}` : 'ksk_••••'}</span>
                              <button
                                type="button"
                                onClick={() => { setKiroKeyDraft(''); setKiroKeyOpen(inst.arn); }}
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
                              onClick={() => { setKiroKeyDraft(''); setKiroKeyOpen(inst.arn); }}
                              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                            >
                              <Plus aria-hidden="true" className="size-3" /> Add API key <span className="text-muted-foreground/70">(stored in Token Vault)</span>
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
                      <Plus aria-hidden="true" className="size-3" /> Add agent
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
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
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
  const arnId = `runtime-${role}-arn`;
  const descriptionId = `runtime-${role}-description`;
  const apiKeyId = `runtime-${role}-api-key`;
  return (
    <div className="space-y-2 rounded-md border border-border/60 bg-muted/10 p-2.5">
      <div className="space-y-1">
        <Label htmlFor={arnId} className="text-xs">Runtime ARN or dev URL</Label>
        <Input
          id={arnId}
          name={arnId}
          placeholder="arn:aws:bedrock-agentcore:…"
          value={arn}
          onChange={(e) => onArn(e.target.value)}
          disabled={busy}
          className="text-sm"
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      {role !== 'orchestrator' && (
        <div className="space-y-1">
          <Label htmlFor={descriptionId} className="text-xs">Description <span className="text-muted-foreground">(optional, used to route tasks)</span></Label>
          <Input
            id={descriptionId}
            name={descriptionId}
            placeholder="Describe what this agent handles…"
            value={desc}
            onChange={(e) => onDesc(e.target.value)}
            disabled={busy}
            className="text-xs"
            autoComplete="off"
          />
        </div>
      )}
      {isKiro && (
        <div className="space-y-1">
          <Label htmlFor={apiKeyId} className="text-xs">Kiro API key <span className="text-muted-foreground">(stored in Token Vault)</span></Label>
          <Input
            id={apiKeyId}
            name={apiKeyId}
            type="password"
            placeholder="ksk_…"
            value={apiKey}
            onChange={(e) => onApiKey(e.target.value)}
            disabled={busy}
            className="font-mono text-xs"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
      )}
      <div className="flex items-center gap-2 pt-0.5">
        <Button type="button" size="sm" disabled={busy || !arn.trim()} onClick={onSubmit}>
          {busy ? 'Working…' : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
        )}
      </div>
    </div>
  );
}
