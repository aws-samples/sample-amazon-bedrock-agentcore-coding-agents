/**
 * Forge types - tasks, sessions, and live events for the-console.
 */

import type { ComputeUsage, ConsoleModelId, LlmUsage, StorageUsage } from './billing.ts';

export type TaskSource =
  | 'issue'         // started from a GitHub Issue
  | 'pr'            // started from a PR (e.g. fix CI)
  | 'chat'          // started from web UI paste
  | 'cron'          // scheduled maintenance
  | 'self_spawn';   // child task filed by another agent

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'review'        // PR opened, awaiting human review
  | 'merged'
  | 'failed'
  | 'cancelled';

/**
 * CLI backend that drives the coding agent inside the AgentCore microVM.
 * All six run inside an AgentCore microVM and open a pull request the same
 * way; they differ in the binary launched and how it reaches a model:
 *
 *   - `claudecode` Anthropic Claude Code CLI. Bedrock-native; Pro/Ultra
 *     route LLM through the relay so AgentCore credits meter (historical default).
 *   - `kiro`       Kiro CLI (https://kiro.dev). BYOK - runs on the user's own
 *     Kiro/Q key; AgentCore meters compute only.
 *   - `codex`      OpenAI Codex CLI against GPT-5.5/5.4 on Amazon Bedrock
 *     (Mantle). Relay-metered like claudecode.
 *   - `cursor`     Cursor Agent CLI (cursor.com). BYOK - runs on the user's
 *     own Cursor key (CURSOR_API_KEY); AgentCore meters compute only.
 *   - `hermes`     Nous Research Hermes CLI. Bedrock-native (Claude frontier
 *     models via the microVM role); relay-metered.
 *   - `opencode`   OpenCode CLI (opencode.ai). Bedrock-native; relay-metered.
 *
 * The last three (cursor/hermes/opencode) run on the unified shell runtime
 * (agent-shell), which launches the CLI inside an AgentCore GA
 * interactive shell rather than a hand-rolled node-pty PTY. New tasks default
 * to `claudecode`; the orchestrator picks another backend on explicit user
 * intent or the user's per-family coding-assistant preference.
 */
export type AgentBackend =
  | 'claudecode' | 'kiro' | 'codex'
  | 'cursor' | 'hermes' | 'opencode';

/**
 * Coding-assistant family the user can configure (per-family harness +
 * default model the orchestrator reads when picking a backend). One
 * family maps 1:1 to an AgentBackend today, but is kept distinct so a
 * family can later expose multiple harness profiles.
 */
export type CodingAssistantFamily = AgentBackend;

/**
 * Backends whose LLM usage is metered through the AgentCore relay (AgentCore
 * credits). The others (kiro, cursor) are BYOK - the user supplies their
 * own provider key and AgentCore meters only AgentCore compute.
 */
export const RELAY_METERED_BACKENDS: readonly AgentBackend[] = [
  'claudecode', 'codex', 'hermes', 'opencode',
];

/** Backends that authenticate with a per-user BYOK provider key. */
export const BYOK_BACKENDS: readonly AgentBackend[] = ['kiro', 'cursor'];

export interface Task {
  id: string;
  user_id: string;
  installation_id: number;
  repo_full_name: string;
  source: TaskSource;
  parent_task_id?: string;          // self_spawn / pr / cron lineage
  github_issue_url?: string;
  github_pr_url?: string;
  runtime_session_id?: string;      // AgentCore session id
  agent_runtime_arn?: string;       // which endpoint (Pro shared / Enterprise dedicated)
  /**
   * Which CLI backend ran this task. Persisted at start so the UI can
   * render the right icon + post-mortem context even after the user
   * flips their default in Settings.
   */
  agent_backend?: AgentBackend;
  status: TaskStatus;
  model_id: ConsoleModelId | string;
  /** Estimated total credits at task start (LLM + compute + storage in USD) */
  budget_credits_usd: number;
  used_credits_usd: number;
  /** Hard cap fraction at which the agent receives a SIGTERM (default 1.0) */
  hard_cap_fraction: number;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  /**
   * Owning task_doc id. The orchestrator's `spawn_agent` tool passes
   * the doc id here so TaskDetailPage can render the plan markdown
   * exactly for this task without a temporal heuristic.
   */
  task_doc_id?: string;
  /** Position of the owning subtask within its Task Doc (tasks.plan_ordinal). */
  plan_ordinal?: number;
  /** Goal the orchestrator handed the coding agent. Plain text or markdown. */
  prompt?: string;
  /** Short human label. Falls back to repo_full_name when absent. */
  title?: string;
}

/**
 * Lifecycle of a Task Doc's plan from draft through execution. Mirrors
 * task_docs.execution_status in D1 (migration 0025). Distinct from the
 * coarse `TaskDoc.status` ('open'|'in_progress'|'done'|'archived'): a
 * plan can be 'awaiting_confirmation' while its doc status is still
 * 'open'. The user confirms a plan before any coding agent runs.
 */
export type PlanExecutionStatus =
  | 'draft'
  | 'awaiting_confirmation'
  | 'confirmed'
  | 'in_progress'
  | 'completed'
  | 'archived';

/**
 * A user-owned markdown plan. One Task Doc may spawn 0..N GitHub Issues
 * across 0..N repos via its subtasks. Mirrors the task_docs row (with the
 * confirmation-lifecycle columns added in migration 0025).
 */
export interface TaskDoc {
  id: string;
  user_id?: string;
  conversation_id?: string | null;  // chat thread that produced the plan
  title: string;
  body_md?: string;
  /** Coarse doc state, independent of the execution lifecycle below. */
  status: 'open' | 'in_progress' | 'done' | 'archived';
  /** Plan confirmation lifecycle (task_docs.execution_status). */
  execution_status: PlanExecutionStatus;
  /** Set when the user confirms the plan; null until then. */
  confirmed_at?: string | null;
  /** Estimated total budget for the whole plan in USD, if computed. */
  plan_budget_estimate_usd?: number | null;
  /**
   * Repo + installation the plan targets (migration 0026). Stamped when
   * the plan is published so the autonomous heartbeat loop can fan out a
   * confirmed plan WITHOUT a live chat turn carrying the context. Null on
   * pre-0026 docs and on plans not tied to a single repo.
   */
  repo_full_name?: string | null;
  installation_id?: number | null;
  created_at: string;
  updated_at: string;
}

/**
 * A single checkbox item under a Task Doc. The GET /api/task-docs/:id
 * endpoint LEFT JOINs tasks, so `agent_status` (the joined tasks.status)
 * and `used_credits_usd` are populated when the subtask has spawned a
 * coding agent.
 */
export interface TaskSubtask {
  id: string;
  task_doc_id: string;
  ordinal: number;
  title: string;
  kind: 'issue' | 'inline' | 'manual' | string;
  github_issue_url?: string | null;
  agent_task_id?: string | null;
  status: 'pending' | 'running' | 'review' | 'done' | 'failed' | 'cancelled' | string;
  agent_status?: string | null;     // joined tasks.status
  used_credits_usd?: number | null; // joined tasks.used_credits_usd
  created_at: string;
  updated_at: string;
}

export type TaskEventKind =
  | 'queued'
  | 'started'
  | 'shell'           // shell command (cmd, exit_code, duration)
  | 'tool_call'
  | 'model_message'
  | 'pty_output'      // raw PTY bytes streamed from the coding agent
  | 'pty_snapshot'    // last ~32 KB PTY screen buffer, persisted for replay
  | 'github_event'    // PR opened, CI passed/failed, comment, etc.
  | 'budget_warn'     // 50% / 80% / 95% threshold
  | 'sigterm'         // hard cap reached
  | 'finished';

export interface TaskEvent {
  task_id: string;
  ts: string;
  kind: TaskEventKind;
  payload: unknown;
}

/** Final usage report attached when a task completes. */
export interface TaskUsage {
  task_id: string;
  llm: LlmUsage[];
  compute: ComputeUsage;
  storage: StorageUsage;
  /** Wall-clock seconds and active vCPU-seconds; useful for I/O wait pct calc */
  wall_clock_seconds: number;
  io_wait_pct: number;              // 0..1
}

export interface AgentEndpoint {
  user_id?: string;                 // null for shared endpoints
  org?: string;                     // null for shared endpoints
  tier_scope: 'pro_shared' | 'ultra_shared' | 'enterprise_dedicated';
  runtime_arn: string;
  region: string;
  vpc_id?: string;
  status: 'active' | 'creating' | 'deleting';
}
