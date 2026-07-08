import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Module 1 session + PTY client (the dev workspace mount).
 *   POST /api/dev/sessions {agent_id}              -> { session_id }
 *   POST /api/dev/sessions/{id}/pty {open,resize}   -> opens the bash PTY
 *   POST .../pty {input}                            -> write keystrokes
 *   GET  .../pty/stream  (SSE)                      -> live output frames
 *   POST .../file {path}            -> { path, content, binary }
 *   POST .../file {path, content}   -> { path, bytes }
 *
 * Output is streamed over Server-Sent Events (real-time, byte-for-byte) rather
 * than polled, so the terminal paints as the shell writes instead of in 150 ms
 * chunks. Input and resize stay as small POSTs.
 */

// The interactive dev-workspace mount. Routes are /api/dev/<resource> (the
// orchestrator is /api/orchestrator/*, metrics /api/metrics/*).
const S1 = '/api/dev';

export interface FileNode { path: string; name?: string; size?: number; is_dir?: boolean; }

async function pty(sessionId: string, body: unknown) {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/pty`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  });
  return r.json();
}

// The active session id is remembered per agent in sessionStorage (survives a
// reload / tab-switch within the browser tab, cleared when the tab closes), so
// the terminal RE-ATTACHES to its running shell and shows its history instead of
// a fresh blank one. sessionStorage (not localStorage) on purpose: a brand-new
// browser tab should get a fresh session, not adopt another tab's shell.
const _sidKey = (agentId: string) => `ws:sid:${agentId}`;
function saveSessionId(agentId: string, sid: string) {
  try { sessionStorage.setItem(_sidKey(agentId), sid); } catch { /* private mode / quota */ }
}
function loadSessionId(agentId: string): string | null {
  try { return sessionStorage.getItem(_sidKey(agentId)); } catch { return null; }
}
function clearSessionId(agentId: string) {
  try { sessionStorage.removeItem(_sidKey(agentId)); } catch { /* ignore */ }
}

export function useSession() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [alive, setAlive] = useState(false);
  // The workspace root the session opened at (its virtual label: the clone, ~,
  // /mnt/s3files once the attendee opens it, or whatever folder is open), and
  // whether a folder is open at all (false = the VS Code "no folder" welcome
  // state). The Workspace reads these to root its tree. Clone-first default is
  // ~/sample-amazon-bedrock-agentcore-coding-agents (the cloned repo); the backend
  // reports the real label on connect.
  const [workspace, setWorkspace] = useState<string>('~/sample-amazon-bedrock-agentcore-coding-agents');
  const [hasFolder, setHasFolder] = useState<boolean>(true);
  const stream = useRef<EventSource | null>(null);

  // Bind an SSE stream to a session's PTY. offset=0 replays the server's retained
  // scrollback buffer (up to 200 KB), which is what makes a RE-ATTACH show the
  // prior history; a fresh open streams from the top too (empty buffer). One ref
  // holds the live stream so a rebind always closes the previous one.
  const bindStream = useCallback((sid: string, onOutput: (s: string) => void) => {
    if (stream.current) stream.current.close();
    const es = new EventSource(`${S1}/sessions/${encodeURIComponent(sid)}/pty/stream`);
    es.onmessage = (e) => {
      try { const j = JSON.parse(e.data); if (j.output) onOutput(j.output); }
      catch { /* ignore malformed frame */ }
    };
    es.addEventListener('end', () => { es.close(); setAlive(false); });
    es.onerror = () => { /* browser auto-reconnects with Last-Event-ID semantics */ };
    stream.current = es;
  }, []);

  const open = useCallback(async (agentId: string, size: { rows: number; cols: number }, onOutput: (s: string) => void) => {
    const r = await fetch(`${S1}/sessions`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ agent_id: agentId }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `session create ${r.status}`);
    const created = await r.json();
    const { session_id } = created;
    if (created.workspace) setWorkspace(created.workspace);
    if (created.has_folder !== undefined) setHasFolder(!!created.has_folder);
    setSessionId(session_id);
    saveSessionId(agentId, session_id);
    await pty(session_id, { open: true, resize: size });
    setAlive(true);
    bindStream(session_id, onOutput);
    return session_id as string;
  }, [bindStream]);

  // Re-attach to a PERSISTED session after a reload / tab-switch, so the terminal
  // shows its prior history instead of a blank fresh shell. Checks the stored id
  // is still a live session with a live PTY; if so, binds the SSE stream at
  // offset 0 (the server replays its retained buffer) WITHOUT calling pty {open}
  // (which would kill the shell and respawn, wiping the history). Returns the
  // session id on success, or null when there is nothing live to reattach to
  // (the caller then opens a fresh session).
  const reattach = useCallback(async (agentId: string, onOutput: (s: string) => void) => {
    const sid = loadSessionId(agentId);
    if (!sid) return null;
    try {
      const r = await fetch(`${S1}/sessions/${encodeURIComponent(sid)}`);
      if (!r.ok) { clearSessionId(agentId); return null; }
      const s = await r.json();
      if (s.status !== 'open' || !s.pty_alive) { clearSessionId(agentId); return null; }
      if (s.workspace) setWorkspace(s.workspace);
      if (s.has_folder !== undefined) setHasFolder(!!s.has_folder);
      setSessionId(sid);
      setAlive(true);
      bindStream(sid, onOutput);   // offset 0 -> the server replays the scrollback
      return sid as string;
    } catch {
      return null;   // network error: fall back to a fresh session
    }
  }, [bindStream]);

  const send = useCallback((sid: string, input: string) => {
    pty(sid, { input }).catch(() => {});
  }, []);

  const resize = useCallback((sid: string, size: { rows: number; cols: number }) => {
    pty(sid, { resize: size }).catch(() => {});
  }, []);

  const close = useCallback(() => {
    if (stream.current) { stream.current.close(); stream.current = null; }
    setAlive(false);
  }, []);

  // VS Code "Open Folder": re-root the session at `path` (~ expands server-side),
  // or close to a no-folder state when `path` is null. The backend closes the PTY,
  // so the caller re-opens it (with the measured size) at the new cwd. Returns the
  // new {workspace, has_folder} or throws on a bad path.
  const openFolder = useCallback(async (
    sid: string, path: string | null,
    size: { rows: number; cols: number }, onOutput: (s: string) => void,
  ) => {
    const r = await fetch(`${S1}/sessions/${encodeURIComponent(sid)}/open-folder`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    setWorkspace(j.workspace || '');
    setHasFolder(!!j.has_folder);
    // Re-bind the live PTY/SSE to the re-rooted shell when a folder is open. The
    // backend closed+respawned the PTY at the new cwd, so this open is correct
    // (a fresh shell at the new root); reattach only applies to a reload, not an
    // explicit Open Folder.
    if (stream.current) { stream.current.close(); stream.current = null; }
    if (j.has_folder) {
      await pty(sid, { open: true, resize: size });
      bindStream(sid, onOutput);
      setAlive(true);
    } else {
      setAlive(false);
    }
    return j as { workspace: string; has_folder: boolean };
  }, [bindStream]);

  // Close the PTY EventSource when the hook unmounts. The Workspace re-mounts on
  // every environment-tab switch (key={selected}); without this, each old SSE
  // stream stayed open and they piled up to the browser's ~6-per-host limit,
  // freezing the page. One ref, closed once on unmount.
  useEffect(() => () => {
    if (stream.current) { stream.current.close(); stream.current = null; }
  }, []);

  return { sessionId, alive, workspace, hasFolder, open, reattach, send, resize, close, openFolder };
}

// The backend returns nodes as {path, type: 'dir'|'file', size}. Normalize to
// FileNode so the explorer can tell folders from files and show their names.
// The old code read `is_dir`/`name` the backend never sent, so EVERY node
// rendered as a nameless file. This is the single source of that mapping.
function normalizeNode(n: { path: string; type?: string; is_dir?: boolean; name?: string; size?: number }): FileNode {
  const name = n.name ?? (n.path.split('/').filter(Boolean).pop() ?? n.path);
  return { path: n.path, name, size: n.size, is_dir: n.is_dir ?? n.type === 'dir' };
}

export async function listTree(sessionId: string): Promise<FileNode[]> {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/files`, {
    headers: { accept: 'application/json' },
  });
  if (!r.ok) return [];
  const j = await r.json();
  return ((j.tree || j.files || []) as Array<Parameters<typeof normalizeNode>[0]>).map(normalizeNode);
}

export async function readFile(sessionId: string, path: string) {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ path }),
  });
  return r.json() as Promise<{ path: string; content?: string; binary?: boolean; language?: string; error?: string }>;
}

export async function writeFile(sessionId: string, path: string, content: string) {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path, content }),
  });
  return r.json() as Promise<{ path: string; bytes?: number; tree?: FileNode[]; error?: string }>;
}

export async function deleteFile(sessionId: string, path: string) {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path, op: 'delete' }),
  });
  return r.json() as Promise<{ tree?: FileNode[]; error?: string }>;
}

export async function renameFile(sessionId: string, path: string, to: string) {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path, op: 'rename', to }),
  });
  return r.json() as Promise<{ path?: string; tree?: FileNode[]; error?: string }>;
}

export async function makeDir(sessionId: string, path: string) {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path, op: 'mkdir' }),
  });
  return r.json() as Promise<{ path?: string; tree?: FileNode[]; error?: string }>;
}

export interface SearchHit { line: number; text: string; }
export interface SearchFileResult { path: string; hits: SearchHit[]; }

// Content-based workspace search (the editor's Cmd+F across files). The backend
// greps every text file in the jail and returns matching lines grouped by file.
export async function searchFiles(sessionId: string, query: string) {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/file`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ op: 'search', query }),
  });
  return r.json() as Promise<{ query: string; results: SearchFileResult[]; truncated?: boolean; error?: string }>;
}

export interface DirEntry { name: string; path: string; }
export interface ListDirsResult {
  path: string;
  label: string;
  parent: string | null;
  home: string;
  entries: DirEntry[];
  error?: string;
}

// Directory browser for the Open Folder modal: returns immediate subdirectories
// of `path` (dirs only, sorted), a display label, and the parent path for "Up"
// navigation (null at home — can't navigate above home).
export async function listDirs(sessionId: string, path: string): Promise<ListDirsResult> {
  const r = await fetch(`${S1}/sessions/${encodeURIComponent(sessionId)}/list-dirs`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  return r.json() as Promise<ListDirsResult>;
}
