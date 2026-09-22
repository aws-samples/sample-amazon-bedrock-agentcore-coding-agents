/**
 * Global session store: connects to REAL AgentCore Runtimes via the backend proxy.
 *
 * No local bash, no mocks. Each session connects to its agent's wired runtime ARN
 * through /api/dev/runtime-sessions, which uses AgentCoreRuntimeClient.open_shell().
 *
 * Multiple sessions per agent: sessions are keyed by their backend session id (a
 * unique tab id). getSessions(agentId) lists the open tabs for one agent so the
 * UI can render a tab bar with a + (open) and an x (close) per session. Sessions
 * persist across navigation; their buffers are replayed on return.
 */

import { createTerminalInputQueue, type TerminalInputQueue } from '../lib/terminalInput';
import { createTerminalResizeQueue, type TerminalResizeQueue } from '../lib/terminalResize';
import type { TerminalOutput } from '../lib/terminalOutput';
import { toast } from '../components/ConsoleNotifications';
import { apiFetch } from '../lib/authSession';

const API = '/api/dev/runtime-sessions';
const _inputQueues = new Map<string, TerminalInputQueue>();
const _resizeQueues = new Map<string, TerminalResizeQueue>();

function stopTerminalIO(id: string) {
  _inputQueues.get(id)?.stop();
  _inputQueues.delete(id);
  _resizeQueues.get(id)?.stop();
  _resizeQueues.delete(id);
}

export interface SessionEntry {
  /** Unique per session/tab (the backend runtime session id). */
  id: string;
  agentId: string;
  runtimeArn: string;
  alive: boolean;
  /** Same PTY either way; this only marks run-created tabs in the UI. */
  openedBy?: 'user' | 'orchestrator';
  /** Stable display number, assigned once at open. Never renumbered when an
   *  earlier tab closes, so "Session 2" stays "Session 2". */
  label: number;
}

// Keyed by session id (the tab id), NOT by agentId, so one agent can hold many.
const _sessions: Map<string, SessionEntry> = new Map();
// Monotonic per-agent counter for stable tab labels (Session 1, 2, 3, ...).
const _seq: Map<string, number> = new Map();

// --- Persistence (R15): keep open tabs across refresh + navigation. ---------
// The store is module-global, so it survives navigation, but a full page reload
// drops it. We mirror the open sessions to sessionStorage so a reload restores
// the same tabs and re-subscribes (the backend keeps the runtime shell alive).
const _PERSIST_KEY = 'agentcore.console.sessions';

function _persist(): void {
  try {
    const rows = [..._sessions.values()].map((s) => ({
      id: s.id, agentId: s.agentId, runtimeArn: s.runtimeArn, label: s.label,
    }));
    const seq = [..._seq.entries()];
    sessionStorage.setItem(_PERSIST_KEY, JSON.stringify({ rows, seq }));
  } catch { /* private mode / disabled storage: in-memory only */ }
}

let _hydrated = false;
function _hydrate(): void {
  if (_hydrated) return;
  _hydrated = true;
  try {
    const raw = sessionStorage.getItem(_PERSIST_KEY);
    if (!raw) return;
    const { rows, seq } = JSON.parse(raw) as {
      rows: { id: string; agentId: string; runtimeArn: string; label: number }[];
      seq: [string, number][];
    };
    for (const r of rows ?? []) {
      // The server owns history and sends it once when this tab re-subscribes.
      _sessions.set(r.id, { ...r, alive: true });
    }
    for (const [k, v] of seq ?? []) _seq.set(k, v);
  } catch { /* corrupt payload: start fresh */ }
}

/** All open sessions for one agent, in insertion order. */
export function getSessions(agentId: string): SessionEntry[] {
  _hydrate();
  return [..._sessions.values()].filter((s) => s.agentId === agentId);
}

export function getSession(id: string): SessionEntry | null {
  return _sessions.get(id) ?? null;
}

/** Open a NEW session (tab) for an agent. Each call creates a fresh runtime shell.
 *  Pass instanceArn to target a SPECIFIC wired instance when the role is a fleet
 *  of N; omit it to let the backend use the role's first instance. */
export async function openSession(
  agentId: string,
  size: { rows: number; cols: number },
  instanceArn?: string,
): Promise<SessionEntry> {
  _hydrate();
  const r = await apiFetch(API, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      agent_id: agentId, cols: size.cols, rows: size.rows,
      ...(instanceArn ? { instance_arn: instanceArn } : {}),
    }),
  });
  const data = await r.json();
  if (!r.ok || data.error) {
    throw new Error(data.error || `Failed to connect: ${r.status}`);
  }

  const nextLabel = (_seq.get(agentId) ?? 0) + 1;
  _seq.set(agentId, nextLabel);
  const entry: SessionEntry = {
    id: data.session_id,
    agentId,
    runtimeArn: data.runtime_arn,
    alive: true,
    label: nextLabel,
    openedBy: data.opened_by === 'orchestrator' ? 'orchestrator' : 'user',
  };
  _sessions.set(entry.id, entry);
  _persist();
  return entry;
}

export function subscribeOutput(
  id: string,
  onOutput: TerminalOutput,
  onGone?: () => void,
): () => void {
  const es = new EventSource(`${API}/${encodeURIComponent(id)}/stream`);
  es.onmessage = (e) => {
    try {
      const j = JSON.parse(e.data);
      if (typeof j.output === 'string') {
        onOutput(j.output, j.replay === true);
      } else if (j.error) {
        // The backend no longer has this session (e.g. dropped on a server
        // restart). Prune the dead tab so a reload doesn't show a stale one.
        es.close();
        stopTerminalIO(id);
        _sessions.delete(id);
        _persist();
        onGone?.();
      }
    } catch { /* ignore */ }
  };
  es.addEventListener('end', () => {
    es.close();
    stopTerminalIO(id);
    const entry = _sessions.get(id);
    if (entry) entry.alive = false;
  });
  return () => es.close();
}

export function sendInput(id: string, input: string) {
  let queue = _inputQueues.get(id);
  if (!queue) {
    queue = createTerminalInputQueue(async text => {
      const response = await apiFetch(`${API}/${encodeURIComponent(id)}/input`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ input: text }),
      });
      const result = await response.json();
      if (!response.ok || result.error || !result.ok) {
        throw new Error(result.error || `Terminal input failed (${response.status}).`);
      }
    }, error => {
      toast.error(`Terminal input stopped: ${error.message} Reload to reconnect before typing again.`);
    });
    _inputQueues.set(id, queue);
  }
  queue.send(input);
}

export function resizeTerminal(id: string, size: { rows: number; cols: number }) {
  let queue = _resizeQueues.get(id);
  if (!queue) {
    queue = createTerminalResizeQueue(async measured => {
      const response = await apiFetch(`${API}/${encodeURIComponent(id)}/resize`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(measured),
      });
      const result = await response.json();
      if (!response.ok || result.error || !result.ok) {
        throw new Error(result.error || `Terminal resize failed (${response.status}).`);
      }
    }, error => console.warn('Could not resize Runtime terminal:', error.message));
    _resizeQueues.set(id, queue);
  }
  queue.resize(size);
}

/** Only remove the tab after the server confirms the terminal closed. */
export async function closeSession(id: string): Promise<void> {
  const response = await apiFetch(`${API}/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (response.status !== 404) {
    const result = await response.json();
    if (!response.ok || result.error || !result.ok) {
      throw new Error(result.error || 'The host did not confirm that the terminal closed.');
    }
  }
  stopTerminalIO(id);
  _sessions.delete(id);
  _persist();
}

/**
 * Merge the server's terminal registry into the local tab store. This includes
 * manually opened terminals and orchestrator-created run PTYs, so a Chat build
 * appears automatically and remains the SAME writable shell on the Agents page.
 * Returns true when anything changed (so callers re-render only on change).
 */
export async function syncServerSessions(agentId: string): Promise<boolean> {
  _hydrate();
  let rows: { session_id: string; agent_id: string; runtime_arn: string;
              alive: boolean; opened_by?: 'user' | 'orchestrator' }[];
  try {
    const r = await apiFetch(`${API}?agent_id=${encodeURIComponent(agentId)}`);
    const data = await r.json();
    if (!r.ok || !Array.isArray(data.sessions)) return false;
    rows = data.sessions;
  } catch {
    return false;
  }
  let changed = false;
  const seen = new Set<string>();
  for (const s of rows) {
    seen.add(s.session_id);
    if (!s.alive) continue;
    const current = _sessions.get(s.session_id);
    if (!current) {
      const nextLabel = (_seq.get(agentId) ?? 0) + 1;
      _seq.set(agentId, nextLabel);
      _sessions.set(s.session_id, {
        id: s.session_id, agentId, runtimeArn: s.runtime_arn,
        alive: true, label: nextLabel,
        openedBy: s.opened_by === 'orchestrator' ? 'orchestrator' : 'user',
      });
      changed = true;
    }
  }
  // Prune local tabs the server no longer knows (restart) or that died.
  for (const s of [..._sessions.values()]) {
    if (s.agentId === agentId && !seen.has(s.id)) {
      stopTerminalIO(s.id);
      _sessions.delete(s.id);
      changed = true;
    }
  }
  if (changed) _persist();
  return changed;
}
