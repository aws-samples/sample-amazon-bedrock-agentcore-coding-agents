/**
 * Console API client: same-origin calls to server.py's stage mounts:
 *   Module 1 (Agents)     -> /api/dev
 *   Module 2 (Fleets)     -> /api/orchestrator
 *   Module 3 (Governance) -> /api/metrics
 *
 * server.py dispatches each mount to its engine in-process. Everything here is
 * the real run/agent/metrics surface; there is no mock data path.
 */

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { accept: 'application/json' } });
  if (!r.ok) throw new ApiError(r.status, `GET ${path} ${r.status}`);
  return (await r.json()) as T;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: body === undefined ? '{}' : JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text().catch(() => '');
    throw new ApiError(r.status, `POST ${path} ${r.status}${text ? `: ${text}` : ''}`);
  }
  return (await r.json()) as T;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/* ---------------- Module 1: Agents ---------------- */

export interface Agent {
  agent_id: string;
  label: string;
  /** Attendee-editable display name (defaults to label). */
  name?: string;
  /** Attendee-editable role this subagent plays for the orchestrator. */
  purpose?: string;
  model: string;
  credential: string;
  status: string;
  runtime_arn: string | null;
  endpoint: string | null;
  deployed_at?: string | null;
}

export const listAgents = () =>
  get<{ agents: Agent[] }>('/api/dev/agents').then((r) => r.agents);

export const prepareAgent = (agentId: string) =>
  post<Agent>('/api/dev/agents/deploy', { agent_id: agentId });

export const getAgent = (agentId: string) =>
  get<Agent>(`/api/dev/agents/${encodeURIComponent(agentId)}`);

/** Rename a deployed subagent and set its purpose (right-click Edit on the shelf). */
export const editAgent = (agentId: string, fields: { name?: string; purpose?: string }) =>
  post<Agent>(`/api/dev/agents/${encodeURIComponent(agentId)}/edit`, fields);

/* ---------------- Module 2: Fleets / runs ---------------- */

export interface Workflow {
  workflow_ref: string;
  version: string;
  agents: string[];
  usecase: string;
  read_only: boolean;
  description: string;
}

export interface RunRoute {
  workflow_ref: string;
  rule: string;
  agents: string[];
  usecase: string;
}

export interface RunSummary {
  run_id: string;
  task: string;
  status: string;
  phase: string;
  created_at?: string;
  route?: RunRoute | null;
  pr_url?: string | null;
  merge_state?: string | null;
}

export const listWorkflows = () =>
  get<{ workflows: Workflow[] }>('/api/orchestrator/workflows').then((r) => r.workflows);

export const listRuns = () =>
  get<{ runs: RunSummary[] }>('/api/orchestrator/runs').then((r) => r.runs ?? []);

// Paged variant for the sidebar's infinite-scroll history: newest first, with a
// total so the list knows when it has reached the end.
export const listRunsPaged = (limit: number, offset: number) =>
  get<{ runs: RunSummary[]; total: number; offset: number }>(
    `/api/orchestrator/runs?limit=${limit}&offset=${offset}`,
  ).then((r) => ({ runs: r.runs ?? [], total: r.total ?? 0, offset: r.offset ?? offset }));

export const getRun = (runId: string) =>
  get<RunSummary & Record<string, unknown>>(`/api/orchestrator/runs/${encodeURIComponent(runId)}`);

// The terminal verdict the orchestrator reports back: the real pytest gate result,
// the review state, iterations, and the PR url (or null). Only valid once the run
// is terminal (the endpoint 409s while it is still running), so callers fetch it
// when the run settles. Every field is the run's own recorded outcome.
export interface RunResult {
  run_id: string;
  status: string;
  gate?: { passed: boolean; checks?: Array<{ check?: string; passed?: boolean; detail?: string }> };
  review?: { state?: string; lgtm?: boolean; round?: number; gate?: unknown; critique?: unknown } | null;
  pr_url?: string | null;
  merge_state?: string | null;
  iterations?: number;
  fail_reason?: string | null;
  route?: RunRoute | null;
}

export const getRunResult = (runId: string) =>
  get<RunResult>(`/api/orchestrator/runs/${encodeURIComponent(runId)}/result`);

// One structured event from a role's real CLI session, the way the agent emitted
// it: assistant prose, extended reasoning, a tool call, or that call's result.
// `name` === 'Task' marks a subagent spawn.
export interface AgentEvent {
  kind: 'text' | 'thinking' | 'tool_use' | 'tool_result';
  text?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  is_error?: boolean;
}

// Per-role shell transcripts AND the structured event stream are served on one
// endpoint (the run-detail payload stays small). `terminals` = raw shell lines;
// `events` = the real tool_use/thinking/text stream the console renders.
export const getRunTerminals = (runId: string) =>
  get<{
    run_id: string;
    terminals: Record<string, Array<{ cmd?: string; output?: string; text?: string }>>;
    events?: Record<string, AgentEvent[]>;
  }>(`/api/orchestrator/runs/${encodeURIComponent(runId)}/terminals`).then((r) => ({
    terminals: r.terminals ?? {},
    events: r.events ?? {},
  }));

// The composed change as a per-file unified diff: the session Changes tab's data,
// read from this run's real commit in the composed repo (`git show`). `files` is
// empty (with `reason`) until the gate is green and the commit lands.
export interface RunDiffFile {
  path: string;
  added: number | null;
  removed: number | null;
  patch: string;
}
export interface RunDiff {
  run_id: string;
  commit: string | null;
  branch: string | null;
  files: RunDiffFile[];
  reason?: string;
}
export const getRunDiff = (runId: string) =>
  get<RunDiff>(`/api/orchestrator/runs/${encodeURIComponent(runId)}/diff`);

export interface SubmitRunInput {
  task: string;
  workflow_ref?: string;
  /** Optional model override. A single id applies to every dispatched role;
   *  the per-role map targets one role (engine `_role_model`: options.models). */
  model?: string;
  models?: Record<string, string>;
}

// The engine reads model overrides off `options` (options.model / options.models),
// so nest them there rather than at the top level (where they were silently
// dropped). task + workflow_ref stay top-level, as the handler expects.
export const submitRun = (input: SubmitRunInput) => {
  const { task, workflow_ref, model, models } = input;
  const options: Record<string, unknown> = {};
  if (model) options.model = model;
  if (models) options.models = models;
  return post<RunSummary & { route?: RunRoute }>('/api/orchestrator/runs', {
    task,
    ...(workflow_ref ? { workflow_ref } : {}),
    ...(Object.keys(options).length ? { options } : {}),
  });
};

/* ---------------- Module 2: the orchestrator's selectable models ---------------- */

export interface ModelOption { id: string; label: string; hint?: string }

// The orchestrator's brain models, resolved from the real Bedrock catalog by the
// backend; the picker is dynamic, not a hardcoded list.
export const listModels = () =>
  get<{ models: ModelOption[]; default: string }>('/api/orchestrator/models');

/* ---------------- Module 2: chat with the orchestrator (SSE) ---------------- */

// One event off the chat stream. A plain turn yields only `text` then `done`;
// `run_started` arrives ONLY when the orchestrator dispatches an agent; that is
// when the UI reveals the run panel and "Running", never before.
export type ChatEvent =
  | { type: 'text'; text: string }
  | { type: 'reasoning'; text: string }
  | { type: 'tool'; name: string; status: 'running' | 'done' }
  | { type: 'run_started'; run_id: string; kind: string }
  | { type: 'error'; error: string }
  // Emitted while the model is silent so the transport chain (CloudFront/nginx
  // idle timeouts) never cuts the stream mid-think. Renders nothing.
  | { type: 'keepalive' }
  | { type: 'done' };

/**
 * Talk to the REAL orchestrator agent and stream its turn. POSTs the prompt and
 * reads the SSE body frame by frame, invoking `onEvent` for each parsed event.
 * `model` sets the ORCHESTRATOR'S own model for the conversation (the chatbot's
 * brain), not a per-role model.
 */
// An attachment carried to the orchestrator: a name plus EITHER an image data URL
// (`data`, decoded server-side into a real image content block) or plain `text`.
export interface ChatAttachment { name: string; data?: string; text?: string }

export async function streamChat(
  input: { prompt: string; conversationId: string; model?: string; attachments?: ChatAttachment[] },
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const r = await fetch('/api/orchestrator/chat', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      prompt: input.prompt,
      conversation_id: input.conversationId,
      ...(input.model ? { model: input.model } : {}),
      ...(input.attachments?.length ? { attachments: input.attachments } : {}),
    }),
    signal,
  });
  if (!r.ok || !r.body) throw new ApiError(r.status, `chat ${r.status}`);
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line; each `data:` line is one JSON event.
    const frames = buf.split('\n\n');
    buf = frames.pop() ?? '';
    for (const frame of frames) {
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          onEvent(JSON.parse(payload) as ChatEvent);
        } catch { /* ignore a malformed frame */ }
      }
    }
  }
}

// Prompt suggestions from the real workflow registry. The backend derives these
// from registered workflow descriptions so they reflect what the orchestrator can
// actually do. Callers fall back to a local constant if this fetch fails.
export const listSuggestions = () =>
  get<{ suggestions: string[] }>('/api/orchestrator/suggestions');

/* ---------------- Module 2: GitHub connection ---------------- */

export type MergePolicy = 'human_review' | 'auto';

// The GitHub connection is a GitHub App installation held inside the GitHub MCP
// Gateway (never a PAT). Status reports the GATEWAY health, not a token: the
// orchestrator opens PRs by calling the gateway's MCP tools over SigV4.
export interface GithubStatus {
  connected: boolean;
  mode: 'gateway' | 'local';
  connection_method?: 'gateway';
  source?: 'environment' | 'settings' | 'discovered';
  gateway_url?: string;
  target?: string;
  region?: string;
  repo?: string;
  tool_count?: number;
  workshop_repo?: string;
  merge_policy?: MergePolicy;
  hint?: string;
  error?: string;
}

export const getGithubStatus = () =>
  get<GithubStatus>('/api/orchestrator/github');

// Connect the PR destination: the attendee's template-derived repo (owner/name).
// NO token. The gateway URL is normally wired by the workshop (env); the console
// may also pass it explicitly. Sending only merge_policy (no repo) flips the
// policy alone.
export const saveGithubCredential = (params: {
  repo: string;
  gateway_url?: string;
  merge_policy?: MergePolicy;
}) => post<GithubStatus>('/api/orchestrator/github', params);

export const clearGithubCredential = () =>
  post<GithubStatus>('/api/orchestrator/github', { clear: true });

// Flip ONLY the merge policy (auto vs human_review) without re-entering the repo.
export const setMergePolicy = (merge_policy: MergePolicy) =>
  post<GithubStatus>('/api/orchestrator/github', { merge_policy });

/* ---------------- Kiro API key (AgentCore Identity Token Vault) ---------------- */

export interface KiroStatus {
  connected: boolean;
  source?: 'settings' | null;
  provider?: string;
  region?: string;
  key_tail?: string;
  error?: string;
}

export const getKiroStatus = () => get<KiroStatus>('/api/orchestrator/kiro');

/* ---------------- Module 2: wirable AgentCore runtimes ---------------- */

// Per-role AgentCore runtime wiring. The orchestrator (real-only) dispatches each
// role to its deployed runtime; the ARNs are SET here (or via env / the
// runtime_config surface), never hardcoded. A missing ARN fails loud.
export interface RuntimeInstance {
  arn: string;
  source: 'environment' | 'settings';
  description?: string;   // per-instance: what this specific runtime does
}

export interface RuntimeRole {
  role: string;
  wired: boolean;
  source?: 'environment' | 'settings' | null;
  arn?: string | null;          // the first instance's ARN (back-compat)
  count?: number;               // fleet size: a role may have N deployed runtimes
  instances?: RuntimeInstance[];
  description?: string;         // what this agent does; the orchestrator reads it
}

export interface RuntimeStatus {
  executor: string;       // 'agentcore' (the real-only shipped executor)
  remote_dispatch: boolean;
  roles: RuntimeRole[];
}

export const getRuntimes = () => get<RuntimeStatus>('/api/orchestrator/runtimes');

// One agent to wire: its ARN or a local http(s):// dev URL and an optional
// description (used to route tasks). Used by both wire (first) and add (grow the fleet).
export interface AgentWireInput {
  arn: string;
  description?: string;
}

// Wire a role to a SINGLE runtime (replaces any prior fleet for that role).
export const wireRuntime = (role: string, input: string | AgentWireInput) => {
  const i: AgentWireInput = typeof input === 'string' ? { arn: input } : input;
  return post<RuntimeStatus & { error?: string }>('/api/orchestrator/runtimes', {
    role, arn: i.arn,
    ...(i.description ? { description: i.description } : {}),
  });
};

// Grow a role's FLEET: add another deployed instance of the same type
// (2 Claude Code, 5 opencode, and so on). Dispatch round-robins across the fleet.
export const addRuntime = (role: string, input: string | AgentWireInput) => {
  const i: AgentWireInput = typeof input === 'string' ? { arn: input } : input;
  return post<RuntimeStatus & { error?: string }>('/api/orchestrator/runtimes', {
    role, arn: i.arn, add: true,
    ...(i.description ? { description: i.description } : {}),
  });
};

export const clearRuntime = (role?: string) =>
  post<RuntimeStatus>('/api/orchestrator/runtimes', { clear: true, role });

// Remove ONE instance from a role's fleet (the per-instance x button).
export const removeRuntime = (role: string, arn: string) =>
  post<RuntimeStatus & { error?: string }>('/api/orchestrator/runtimes', { remove: true, role, arn });

// Set ONE instance's description (keyed by its ARN). The orchestrator reads these
// to describe its dispatch targets dynamically (no hardcoded blurb).
export const describeRuntime = (role: string, arn: string, description: string) =>
  post<RuntimeStatus & { error?: string }>('/api/orchestrator/runtimes', { describe: true, role, arn, description });

/* ---------------- Module 3: Governance / metrics ---------------- */

export interface Dashboard {
  active_sessions: number;
  runs_total: number;
  p95_latency_ms: number;
  cost_by_agent: Record<string, number>;
}

export const getDashboard = () => get<Dashboard>('/api/metrics/dashboard');

export interface UserMetrics {
  user_id?: string;
  range?: string;
  runs?: number;
  total_cost_usd?: number;
  total_tokens?: number;
  p95_latency_ms?: number;
  cost_by_agent?: Record<string, number>;
  [k: string]: unknown;
}

export const getUserMetrics = (user: string, range = '24h') =>
  get<UserMetrics>(
    `/api/metrics/users/${encodeURIComponent(user)}/metrics?range=${encodeURIComponent(range)}`,
  );

export interface CostBreakdown {
  by: string;
  breakdown: Record<string, number>;
  currency: string;
}

export const getCostBreakdown = (by: 'agent' | 'user' = 'agent') =>
  get<CostBreakdown>(`/api/metrics/cost-breakdown?by=${by}`);

export interface SessionRow {
  session_id: string;
  assistant_type?: string;
  agent?: string;
  user_id?: string;
  user?: string;
  state?: string;
  claude_running?: boolean;
  runtime_arn?: string | null;
  started_at?: string;
  [k: string]: unknown;
}

// List governance sessions, optionally filtered. `window` is a lookback in
// MINUTES (the API's documented filter); `assistant_type`/`user_id` scope by
// agent or human. All filters are real query params the metrics API honors.
export const listSessions = (filters?: { window?: number; assistant_type?: string; user_id?: string }) => {
  const qs = new URLSearchParams();
  if (filters?.window != null) qs.set('window', String(filters.window));
  if (filters?.assistant_type) qs.set('assistant_type', filters.assistant_type);
  if (filters?.user_id) qs.set('user_id', filters.user_id);
  const q = qs.toString();
  return get<{ sessions: SessionRow[] }>(`/api/metrics/sessions${q ? `?${q}` : ''}`).then((r) => r.sessions ?? []);
};

// The kill switch (StopRuntimeSession). Locally this REALLY signals the recorded
// session process; on AgentCore it calls StopRuntimeSession. Returns the stop
// result, or null if there is no such session.
export const stopSession = (sessionId: string) =>
  post<{ session_id: string; stopped: boolean } | null>(
    `/api/metrics/sessions/${encodeURIComponent(sessionId)}/stop`,
  );

// p95 latency, optionally scoped to one agent or one user. The response echoes
// back the scope it applied so the caller can confirm what it measured.
export interface LatencyP95 {
  p95_latency_ms: number;
  scope: { assistant_type?: string; user_id?: string };
}

export const getLatencyP95 = (scope?: { assistant_type?: string; user_id?: string }) => {
  const qs = new URLSearchParams();
  if (scope?.assistant_type) qs.set('assistant_type', scope.assistant_type);
  if (scope?.user_id) qs.set('user_id', scope.user_id);
  const q = qs.toString();
  return get<LatencyP95>(`/api/metrics/latency/p95${q ? `?${q}` : ''}`);
};

// User attribution recorded for one session. GitHub authorship is deliberately
// separate because it depends on the credential selected for finalization.
export interface Identity {
  session_id: string;
  recorded_user: string;
  user_email: string;
  user_name: string;
  auth_provider: 'cognito' | 'os-user';
  environment: 'local' | 'agentcore';
  attribution_source: 'run-ledger';
  github_actor: 'credential-dependent';
  static_credentials_on_agent: boolean;
}

export const getIdentity = (sessionId: string) =>
  get<Identity>(`/api/metrics/sessions/${encodeURIComponent(sessionId)}/identity`);

// One Cedar-style guardrail the harness enforces before any tool call runs.
export interface Policy {
  tier: 'hard' | 'soft';
  rule_id: string;
  effect: 'forbid' | 'gate';
  summary: string;
}

export interface Policies {
  policies: Policy[];
  enforced: boolean;
  note?: string;
}

export const getPolicies = () => get<Policies>('/api/metrics/policies');

// One row of the append-only governance audit trail, derived from the real
// telemetry ledger (orchestrator runs, Stage-1 sessions, deploys, verifies).
export interface AuditRow {
  at: string;
  kind: string;
  user_id: string;
  line: string;
}

export interface AuditTrail {
  audit: AuditRow[];
  total: number;
  source: string;
}

export const getAudit = (limit = 200) =>
  get<AuditTrail>(`/api/metrics/audit?limit=${limit}`);

/* ---------------- Module 3: real AgentCore runtime status + dispatch ---------------- */

// The deployed-runtime wiring, as Governance sees it (the metrics mount reads the
// SAME runtime_config the orchestrator dispatches against). Distinct from the
// Stage-2 /api/orchestrator/runtimes surface used by Settings; this one is the
// read view under Governance.
export interface GovRuntimeRole {
  role: string;
  wired: boolean;
  source?: 'environment' | 'settings' | null;
  arn?: string | null;
  count?: number;
  instances?: { arn: string; source: string }[];
}

export interface GovRuntimeStatus {
  executor: string;
  remote_dispatch: boolean;
  roles: GovRuntimeRole[];
  note?: string;
}

export const getGovRuntimes = () => get<GovRuntimeStatus>('/api/metrics/runtimes');

// A real, billable health dispatch: runs a tiny job inside the role's deployed
// runtime and reads its echoed marker back. `ok` is true only when the runtime
// genuinely executed and wrote the marker; an unwired role returns wired:false.
export interface ProbeResult {
  role: string;
  ok: boolean;
  wired?: boolean;
  arn?: string;
  source?: string;
  marker_echoed?: boolean;
  artifact_preview?: string;
  session_id?: string;
  error?: string;
}

export const probeRuntime = (role: string) =>
  post<ProbeResult>(`/api/metrics/runtimes/${encodeURIComponent(role)}/probe`);

// Auth: Cognito user identity
export interface AuthUser {
  authenticated: boolean;
  user_id?: string;
  email?: string;
  name?: string;
  groups?: string[];
}

export const getAuthMe = async (): Promise<AuthUser | null> => {
  try {
    return await get<AuthUser>('/api/auth/me');
  } catch {
    return null;
  }
};
