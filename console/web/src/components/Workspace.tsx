import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  Button, ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator,
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
  DropdownMenuLabel,
} from '@foxl/ui';
import {
  FileCode, RefreshCw, X, FilePlus, FolderPlus, TerminalSquare, ChevronsDownUp, Search,
  ChevronRight, ChevronDown, FolderOpen, FolderInput, House,
} from 'lucide-react';
import { Terminal, type TerminalHandle } from './Terminal';
import { FileTree, folderPaths } from './FileTree';
import { CodeEditor } from './CodeEditor';
import {
  PromptDialog, ConfirmDeleteDialog, type PromptRequest, type ConfirmRequest,
} from './WorkspaceDialogs';
import {
  useSession, listTree, readFile, writeFile, deleteFile, renameFile, makeDir,
  searchFiles, type FileNode, type SearchFileResult,
} from '../hooks/useSession';

// The default virtual root before the session reports its own (the clone-first
// ~/sample-amazon-bedrock-agentcore-coding-agents checkout). The live root is the
// session's `workspace` (the clone, ~, /mnt/s3files once opened, or any opened folder).
const DEFAULT_VROOT = '~/sample-amazon-bedrock-agentcore-coding-agents';
// Join a directory path with a new child name into a workspace-relative path.
const joinDir = (vroot: string, dir: string | undefined, name: string) => {
  const baseDir = (dir ?? vroot).replace(vroot, '').replace(/^\/+/, '');
  return baseDir ? `${baseDir}/${name}` : name;
};

interface OpenTab {
  path: string;
  name: string;
  body: string;
  dirty: boolean;
  binary: boolean;
  language?: string;   // from the read API; drives comment-toggle + the status bar
}

const base = (p: string) => p.split('/').filter(Boolean).pop() ?? p;

// localStorage keys are namespaced per agentId so the dev workspace and each
// role card remember their own layout independently (and never collide).
const foldKey = (agentId: string) => `ws:fold:${agentId}`;
const tabsKey = (agentId: string) => `ws:tabs:${agentId}`;
const widthKey = (agentId: string) => `ws:sidebar:${agentId}`;
// Recently-opened folders (VS Code-style), shared across the dev workspace so the
// folder switcher offers them instead of hardcoding any one path. Seeded with the
// clone-first checkout; /mnt/s3files is added here automatically once the attendee
// opens it (after creating S3 Files in Stage 1), never hardcoded as always-present.
const RECENT_KEY = 'ws:recent-folders';
const RECENT_SEED = ['~/sample-amazon-bedrock-agentcore-coding-agents'];
const RECENT_MAX = 6;
function loadRecent(): string[] {
  const saved = loadJSON<string[]>(RECENT_KEY, []);
  const merged = [...saved, ...RECENT_SEED.filter((s) => !saved.includes(s))];
  return merged.slice(0, RECENT_MAX);
}
function pushRecent(path: string): string[] {
  const next = [path, ...loadRecent().filter((p) => p !== path)].slice(0, RECENT_MAX);
  saveJSON(RECENT_KEY, next);
  return next;
}

const SIDEBAR_MIN = 180;
const SIDEBAR_MAX = 480;
const SIDEBAR_DEFAULT = 248;

function loadJSON<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch { return fallback; }
}
function saveJSON(key: string, value: unknown) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* quota/SSR */ }
}

// Small icon button for the explorer header: VS Code style, invisible until you
// hover the header (the parent adds `group`), with a real tooltip via `title`.
function HeaderBtn({
  onClick, disabled, title, children,
}: {
  onClick: () => void; disabled?: boolean; title: string; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className="rounded p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100 disabled:opacity-0"
    >
      {children}
    </button>
  );
}

/**
 * VS Code-style workspace over a live session: a resizable file-tree sidebar with
 * a right-click menu, a multi-tab editor (line-number gutter), a content search
 * panel (Cmd/Ctrl+F across files), and one terminal pane. Opens a shell on mount.
 *
 * Sidebar width, folder fold-state, open tabs, and the active tab persist per
 * agent across reloads (localStorage). The sidebar, the editor body, and the
 * terminal each own their own scroll region (the sidebar never scrolls the editor
 * and vice versa).
 *
 * `fullHeight` drops the fixed 520px box and fills the parent instead, for the
 * IDE-style Development page; the default keeps the card-sized box used inside
 * the Agents role cards.
 */
export function Workspace({ agentId = 'claude-code', fullHeight = false }: { agentId?: string; fullHeight?: boolean }) {
  const [tree, setTree] = useState<FileNode[]>([]);
  const [tabs, setTabs] = useState<OpenTab[]>([]);
  const [active, setActive] = useState<string | null>(null);   // active editor tab path
  const [view, setView] = useState<'editor' | 'terminal'>('terminal');
  const [booting, setBooting] = useState(true);
  // Recently-opened folders for the folder switcher (VS Code "Recent").
  const [recent, setRecent] = useState<string[]>(() => loadRecent());
  // Explicit folder open/closed overrides (path -> open?), persisted. A path NOT
  // in the map falls back to the default: every folder starts COLLAPSED (the
  // workspace root row is controlled separately, so the tree stays visible).
  const [folds, setFolds] = useState<Record<string, boolean>>(() => loadJSON(foldKey(agentId), {}));
  // Sidebar width (px), drag-resizable and persisted.
  const [sidebarW, setSidebarW] = useState<number>(() => {
    const w = loadJSON<number>(widthKey(agentId), SIDEBAR_DEFAULT);
    return Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, w || SIDEBAR_DEFAULT));
  });
  const [dragging, setDragging] = useState(false);
  // Content-search panel (Cmd/Ctrl+F).
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchFileResult[]>([]);
  const [searching, setSearching] = useState(false);
  // Quick-open palette (Cmd/Ctrl+P): fuzzy-jump to a file by name.
  const [quickOpen, setQuickOpen] = useState(false);
  // File-op dialogs (replace window.prompt / confirm / alert).
  const [prompt, setPrompt] = useState<PromptRequest | null>(null);
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);

  const termRef = useRef<TerminalHandle>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const session = useSession();
  const opened = useRef(false);
  const restoredTabs = useRef(false);   // rehydrate persisted tabs exactly once
  // Gate tab persistence until the saved tabs have been rehydrated. Without
  // this, the persist effect fires on the first render (tabs=[], active=null)
  // and overwrites the saved state BEFORE the restore effect can read it.
  const [hydrated, setHydrated] = useState(false);

  // The live virtual root the session reports (/mnt/s3files on the workshop box,
  // ~ on a plain local box, or whatever folder the attendee opened). Everything
  // path-related keys off this instead of a hardcoded constant.
  const vroot = session.workspace || DEFAULT_VROOT;
  const hasFolder = session.hasFolder;

  const refreshTree = useCallback(async (sid: string) => {
    setTree(await listTree(sid));
  }, []);

  // Persist layout/state whenever it changes.
  useEffect(() => { saveJSON(foldKey(agentId), folds); }, [folds, agentId]);
  useEffect(() => { saveJSON(widthKey(agentId), sidebarW); }, [sidebarW, agentId]);
  useEffect(() => {
    if (!hydrated) return;
    saveJSON(tabsKey(agentId), { paths: tabs.map((t) => t.path), active });
  }, [tabs, active, agentId, hydrated]);

  // Auto-open the shell on mount. First try to RE-ATTACH to a persisted session
  // (a reload / tab-switch): the server replays its retained scrollback so the
  // terminal shows its history instead of a blank fresh shell. Only when there
  // is no live session to reattach to do we open a new one. The PTY opens at the
  // measured terminal size.
  useEffect(() => {
    if (opened.current) return;
    opened.current = true;
    (async () => {
      // Wait for the terminal's layout + font metrics to settle before measuring.
      // On mount xterm has just been open()'d; fitting synchronously in the same
      // tick computes too-few columns (the glyph cell width is not final yet), so
      // the PTY would spawn narrower than the pane and stay narrow until a manual
      // resize. Two rAFs (a layout + a paint) let the real cell size land, so the
      // first fit matches the pane and the PTY spawns at the correct width.
      await new Promise<void>((r) =>
        requestAnimationFrame(() => requestAnimationFrame(() => r())));
      const size = termRef.current?.fit() ?? { rows: 24, cols: 80 };
      try {
        const write = (s: string) => termRef.current?.write(s);
        const reattached = await session.reattach(agentId, write);
        const sid = reattached ?? (await session.open(agentId, size, write));
        // A reattached shell keeps its old winsize; nudge it to THIS pane's size
        // (a resize POST only sets the winsize, it never respawns the shell, so
        // the scrollback the server just replayed is preserved).
        if (reattached) session.resize(sid, size);
        termRef.current?.focus();
        // Web font (JetBrains Mono) can finish loading AFTER the first fit, which
        // shifts the cell width; re-fit once the font is ready and push the true
        // winsize to the PTY so a late font swap never leaves the shell too narrow.
        const settle = (document as Document & { fonts?: FontFaceSet }).fonts?.ready
          ?? Promise.resolve();
        settle.then(() => {
          const s2 = termRef.current?.fit();
          if (s2 && (s2.cols !== size.cols || s2.rows !== size.rows)) session.resize(sid, s2);
        });
        await refreshTree(sid);
      } catch (e) {
        termRef.current?.write(`\r\n\x1b[33m${String(e)}\x1b[0m`);
      } finally {
        setBooting(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live Explorer: files the attendee (or the agent CLI) creates in the terminal
  // land on disk, not through our editor ops, so poll the tree while the session
  // is alive so a shell-created file shows up within a couple seconds.
  useEffect(() => {
    if (!session.sessionId || !session.alive) return;
    const sid = session.sessionId;
    const t = setInterval(() => { refreshTree(sid); }, 2000);
    return () => clearInterval(t);
  }, [session.sessionId, session.alive, refreshTree]);

  // Reopen the tabs from the previous visit once the session is live.
  useEffect(() => {
    if (restoredTabs.current || !session.sessionId) return;
    restoredTabs.current = true;
    const saved = loadJSON<{ paths: string[]; active: string | null }>(tabsKey(agentId), { paths: [], active: null });
    if (!saved.paths.length) { setHydrated(true); return; }
    const sid = session.sessionId;
    (async () => {
      const loaded: OpenTab[] = [];
      for (const p of saved.paths) {
        const j = await readFile(sid, p);
        if (j.error) continue;
        loaded.push({
          path: j.path, name: base(j.path), body: j.binary ? '' : (j.content || ''),
          dirty: false, binary: !!j.binary, language: j.language,
        });
      }
      if (loaded.length) {
        setTabs(loaded);
        const act = saved.active && loaded.some((t) => t.path === saved.active) ? saved.active : loaded[loaded.length - 1]!.path;
        setActive(act);
        setView('editor');
      }
      setHydrated(true);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.sessionId]);

  // ── Sidebar resize (drag the divider) ─────────────────────────────────────
  useEffect(() => {
    if (!dragging) return;
    function onMove(e: MouseEvent) {
      const left = rootRef.current?.getBoundingClientRect().left ?? 0;
      const w = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, e.clientX - left));
      setSidebarW(w);
    }
    function onUp() { setDragging(false); }
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [dragging]);

  // ── Fold-state helpers (controlled FileTree) ──────────────────────────────
  const isOpen = useCallback((path: string, depth: number) => {
    const o = folds[path];
    // Default every folder to COLLAPSED on first entry; only explicit user
    // toggles (persisted in `folds`) open them. (`depth` is unused now but kept
    // in the signature for the controlled-FileTree contract.)
    void depth;
    return o === undefined ? false : o;
  }, [folds]);
  const toggleFold = useCallback((path: string, next: boolean) => {
    setFolds((f) => ({ ...f, [path]: next }));
  }, []);
  const collapseAll = useCallback(() => {
    const all = folderPaths(tree, vroot);
    // Collapse every child folder but KEEP the mount-root open, so the top-level
    // entries stay visible (Collapse Folders, not "hide the whole tree").
    setFolds((f) => ({ ...Object.fromEntries(all.map((p) => [p, false])), [vroot]: f[vroot] !== false }));
  }, [tree, vroot]);

  async function copyPath(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast.success('Copied to clipboard', { description: text });
    } catch {
      toast.error('Could not copy to clipboard');
    }
  }

  async function openFile(path: string, opts?: { line?: number }) {
    if (!session.sessionId) return;
    setView('editor');
    if (tabs.some((t) => t.path === path)) { setActive(path); return; }
    const j = await readFile(session.sessionId, path);
    if (j.error) return;
    setTabs((ts) => [...ts, {
      path: j.path, name: base(j.path), body: j.binary ? '' : (j.content || ''),
      dirty: false, binary: !!j.binary, language: j.language,
    }]);
    setActive(j.path);
    // (opts.line reserved for future scroll-to-line; the tab opens regardless.)
    void opts;
  }

  function dropTab(path: string) {
    setTabs((ts) => {
      const next = ts.filter((t) => t.path !== path);
      if (active === path) setActive(next.length ? next[next.length - 1]!.path : null);
      return next;
    });
  }
  // VS Code closes a dirty tab behind a confirm so unsaved edits aren't lost.
  function closeTab(path: string) {
    const tab = tabs.find((t) => t.path === path);
    if (tab?.dirty) {
      setConfirm({
        name: tab.name, isDir: false, kind: 'unsaved',
        onConfirm: async () => dropTab(path),
      });
      return;
    }
    dropTab(path);
  }

  // Tab right-click actions (VS Code parity). `keep` is the predicate for tabs to
  // retain; the active tab is re-pointed at a surviving tab (or null) when needed.
  const keepTabs = useCallback((keep: (t: OpenTab) => boolean) => {
    setTabs((ts) => {
      const next = ts.filter(keep);
      setActive((a) => (a && next.some((t) => t.path === a) ? a : (next.length ? next[next.length - 1]!.path : null)));
      return next;
    });
  }, []);
  const closeOthers = (path: string) => keepTabs((t) => t.path === path);
  const closeToRight = (path: string) => {
    const idx = tabs.findIndex((t) => t.path === path);
    if (idx < 0) return;
    const keepSet = new Set(tabs.slice(0, idx + 1).map((t) => t.path));
    keepTabs((t) => keepSet.has(t.path));
  };
  const closeSaved = () => keepTabs((t) => t.dirty);   // keep only the dirty (unsaved) tabs
  const closeAll = () => keepTabs(() => false);

  // Tab drag-reorder (VS Code parity): drag a tab onto another to move it there.
  const dragTab = useRef<string | null>(null);
  const reorderTab = useCallback((src: string, beforePath: string) => {
    if (src === beforePath) return;
    setTabs((ts) => {
      const from = ts.findIndex((t) => t.path === src);
      const to = ts.findIndex((t) => t.path === beforePath);
      if (from < 0 || to < 0) return ts;
      const next = [...ts];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved!);
      return next;
    });
  }, []);

  function edit(path: string, body: string) {
    setTabs((ts) => ts.map((t) => (t.path === path ? { ...t, body, dirty: true } : t)));
  }

  async function save(path: string) {
    const tab = tabs.find((t) => t.path === path);
    if (!session.sessionId || !tab) return;
    await writeFile(session.sessionId, path, tab.body);
    setTabs((ts) => ts.map((t) => (t.path === path ? { ...t, dirty: false } : t)));
    await refreshTree(session.sessionId);
  }

  function newFile(dirPath?: string) {
    if (!session.sessionId) return;
    setPrompt({
      kind: 'new-file', initial: 'untitled.txt',
      onSubmit: async (name) => {
        const rel = joinDir(vroot, dirPath, name);
        const j = await writeFile(session.sessionId!, rel, '');
        if (j.error) return j.error;
        await refreshTree(session.sessionId!);
        openFile(j.path || rel);
        return null;
      },
    });
  }

  function newFolder(dirPath?: string) {
    if (!session.sessionId) return;
    setPrompt({
      kind: 'new-folder', initial: 'new-folder',
      onSubmit: async (name) => {
        const j = await makeDir(session.sessionId!, joinDir(vroot, dirPath, name));
        if (j.error) return j.error;
        await refreshTree(session.sessionId!);
        return null;
      },
    });
  }

  function rename(path: string) {
    if (!session.sessionId) return;
    setPrompt({
      kind: 'rename', initial: base(path),
      onSubmit: async (to) => {
        if (to === base(path)) return null;
        const parentRel = path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1).replace(`${vroot}/`, '') : '';
        const j = await renameFile(session.sessionId!, path, parentRel + to);
        if (j.error) return j.error;
        // Re-point any open tab (file, or files under a renamed folder) to the new path.
        const moved = j.path || `${vroot}/${parentRel}${to}`;
        setTabs((ts) => ts.map((t) =>
          t.path === path ? { ...t, path: moved, name: base(moved) }
            : t.path.startsWith(`${path}/`) ? { ...t, path: moved + t.path.slice(path.length), name: base(moved + t.path.slice(path.length)) }
              : t));
        setActive((a) => (a === path ? moved : (a && a.startsWith(`${path}/`) ? moved + a.slice(path.length) : a)));
        await refreshTree(session.sessionId!);
        return null;
      },
    });
  }

  function remove(path: string) {
    if (!session.sessionId) return;
    const node = tree.find((n) => n.path === path);
    setConfirm({
      name: base(path), isDir: !!node?.is_dir,
      onConfirm: async () => {
        const j = await deleteFile(session.sessionId!, path);
        if (j.error) return;        // surfaced by the next tree refresh; no browser alert
        dropTab(path);              // file is gone: drop the tab, never re-prompt for unsaved
        setTabs((ts) => ts.filter((t) => !t.path.startsWith(`${path}/`)));   // and any under a deleted folder
        await refreshTree(session.sessionId!);
      },
    });
  }

  // Drag-and-drop move: relocate `src` INTO `destDir` (vroot = workspace root),
  // keeping its basename. Reuses the backend rename op (os.rename across dirs).
  const move = useCallback(async (src: string, destDir: string) => {
    if (!session.sessionId) return;
    const name = base(src);
    const dest = (destDir === vroot ? '' : destDir.replace(`${vroot}/`, '') + '/') + name;
    const srcRel = src.replace(`${vroot}/`, '');
    if (dest === srcRel) return;    // already there, no-op
    const j = await renameFile(session.sessionId, src, dest);
    if (j.error) { toast.error('Could not move', { description: j.error }); return; }
    // Re-point any open tab for the moved file (or files under a moved folder).
    const moved = j.path || `${vroot}/${dest}`;
    setTabs((ts) => ts.map((t) => {
      if (t.path === src) return { ...t, path: moved, name: base(moved) };
      if (t.path.startsWith(`${src}/`)) {
        const np = moved + t.path.slice(src.length);
        return { ...t, path: np, name: base(np) };
      }
      return t;
    }));
    setActive((a) => (a === src ? moved : (a && a.startsWith(`${src}/`) ? moved + a.slice(src.length) : a)));
    await refreshTree(session.sessionId);
    toast.success('Moved', { description: `${base(src)} → ${destDir.replace(vroot, '') || '/'}` });
  }, [session.sessionId, refreshTree, vroot]);

  // ── Open Folder (VS Code root switcher) ───────────────────────────────────
  // Re-root the session at `path` (~ expands server-side); null closes the folder
  // to the no-folder welcome state. Clears tabs (they belonged to the old root).
  const doOpenFolder = useCallback(async (path: string | null) => {
    if (!session.sessionId) return;
    const size = termRef.current?.fit() ?? { rows: 24, cols: 80 };
    try {
      await session.openFolder(session.sessionId, path, size, (s) => termRef.current?.write(s));
      setTabs([]); setActive(null); setFolds({});
      if (session.sessionId) await refreshTree(session.sessionId);
      if (path) { setRecent(pushRecent(path)); setView('terminal'); termRef.current?.focus(); }
    } catch (e) {
      toast.error('Could not open folder', { description: String(e) });
    }
  }, [session, refreshTree]);

  function promptOpenFolder() {
    setPrompt({
      kind: 'open-folder', initial: '~',
      onSubmit: async (p) => { await doOpenFolder(p); return null; },
    });
  }

  // ── Content search (Cmd/Ctrl+F) ───────────────────────────────────────────
  const runSearch = useCallback(async (q: string) => {
    if (!session.sessionId || !q.trim()) { setSearchResults([]); return; }
    setSearching(true);
    const r = await searchFiles(session.sessionId, q.trim());
    setSearchResults(r.results || []);
    setSearching(false);
  }, [session.sessionId]);

  function openSearch() {
    setSearchOpen(true);
    setTimeout(() => searchInputRef.current?.focus(), 0);
  }

  // Editor keymap (only while focus is inside this workspace, so the browser's own
  // shortcuts still work elsewhere on the page): Cmd/Ctrl+S save, +F search-in-files,
  // +P quick-open palette, +W close the active tab.
  useEffect(() => {
    const inside = () => !!(rootRef.current && rootRef.current.contains(document.activeElement));
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key === 's' && active) { e.preventDefault(); save(active); return; }
      if (mod && e.key === 'f' && inside()) { e.preventDefault(); openSearch(); return; }
      if (mod && e.key === 'p' && inside()) {
        e.preventDefault();
        setQuickOpen((v) => !v);
        return;
      }
      if (mod && e.key === 'w' && inside() && active && view === 'editor') {
        e.preventDefault();
        closeTab(active);
        return;
      }
      if (e.key === 'Escape') {
        if (quickOpen) setQuickOpen(false);
        else if (searchOpen) setSearchOpen(false);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, tabs, searchOpen, quickOpen, view]);

  const activeTab = tabs.find((t) => t.path === active) ?? null;
  const ready = !!session.sessionId;
  // The mount-root node's open state (VS Code workspace-folder root). Defaults
  // open; persisted in the same `folds` map keyed on VROOT. Collapsing it folds
  // the whole tree.
  const rootOpen = folds[vroot] !== false;

  return (
    <>
    <div
      ref={rootRef}
      className={`flex overflow-hidden border border-border bg-card ${fullHeight ? 'h-full rounded-none border-x-0 border-b-0' : 'rounded-lg'}`}
      style={fullHeight ? undefined : { height: 520 }}
    >
      {/* Explorer sidebar (its own scroll region; width drag-resizable) */}
      <div
        className="group flex min-h-0 shrink-0 flex-col border-r border-border bg-sidebar/40"
        style={{ width: sidebarW }}
      >
        {!hasFolder ? (
          <NoFolder
            onOpen={promptOpenFolder}
            onHome={() => doOpenFolder('~')}
            recent={recent}
            onRecent={(p) => doOpenFolder(p)}
            disabled={!ready}
          />
        ) : searchOpen ? (
          <SearchPanel
            inputRef={searchInputRef}
            query={searchQuery}
            setQuery={setSearchQuery}
            results={searchResults}
            searching={searching}
            vroot={vroot}
            onRun={runSearch}
            onClose={() => setSearchOpen(false)}
            onPick={(path, line) => openFile(path, { line })}
          />
        ) : (
          <ContextMenu>
          <ContextMenuTrigger asChild>
          <div className="min-h-0 flex-1 overflow-auto py-1">
            {/* The mount-root row: a collapsible workspace-folder node (VS Code
                style). Its chevron folds the WHOLE tree (state persisted in the
                `folds` map keyed on VROOT), and the action icons (New File / New
                Folder / Search / Collapse / Refresh) sit on the RIGHT of this same
                row, revealed on hover, exactly like a VS Code workspace folder. */}
            <div className="flex items-center pr-1.5">
              {/* The workspace-folder root row. Its chevron folds the WHOLE tree; the
                  folder name is a DROPDOWN that switches the root (Open Folder / Home /
                  the workshop mount / Close Folder), exactly like VS Code's folder menu. */}
              <button
                onClick={() => toggleFold(vroot, !rootOpen)}
                style={{ paddingLeft: 8 }}
                className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-accent"
                title={rootOpen ? 'Collapse' : 'Expand'}
              >
                <ChevronRight className={`size-3 transition-transform ${rootOpen ? 'rotate-90' : ''}`} />
              </button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    className="flex min-w-0 flex-1 items-center gap-1 rounded-md py-1 pl-0.5 pr-2 text-left text-[13px] font-medium text-foreground/70 hover:bg-accent"
                    title={`Workspace root: ${vroot}  (click to switch)`}
                  >
                    <span className="truncate">{vroot}</span>
                    <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-56">
                  <DropdownMenuItem onSelect={promptOpenFolder}>
                    <FolderInput className="mr-2 size-4" /> Open Folder…
                  </DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => doOpenFolder('~')}>
                    <House className="mr-2 size-4" /> Home (~)
                  </DropdownMenuItem>
                  {recent.filter((p) => p !== vroot).length > 0 && (
                    <>
                      <DropdownMenuSeparator />
                      <DropdownMenuLabel className="text-[11px] font-medium text-muted-foreground">
                        Recent
                      </DropdownMenuLabel>
                      {recent.filter((p) => p !== vroot).map((p) => (
                        <DropdownMenuItem key={p} onSelect={() => doOpenFolder(p)}>
                          <FolderOpen className="mr-2 size-4" />
                          <span className="truncate">{p}</span>
                        </DropdownMenuItem>
                      ))}
                    </>
                  )}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onSelect={() => doOpenFolder(null)} className="text-muted-foreground">
                    <X className="mr-2 size-4" /> Close Folder
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <div className="flex shrink-0 items-center gap-0.5">
                <HeaderBtn onClick={() => newFile()} disabled={!ready} title="New File">
                  <FilePlus className="size-4" />
                </HeaderBtn>
                <HeaderBtn onClick={() => newFolder()} disabled={!ready} title="New Folder">
                  <FolderPlus className="size-4" />
                </HeaderBtn>
                <HeaderBtn onClick={openSearch} disabled={!ready} title="Search in files (⌘F)">
                  <Search className="size-4" />
                </HeaderBtn>
                <HeaderBtn onClick={collapseAll} disabled={!ready} title="Collapse Folders">
                  <ChevronsDownUp className="size-4" />
                </HeaderBtn>
                <HeaderBtn onClick={() => ready && refreshTree(session.sessionId!)} disabled={!ready} title="Refresh Explorer">
                  <RefreshCw className="size-4" />
                </HeaderBtn>
              </div>
            </div>
            {booting ? (
              <p className="px-3 py-2 text-xs text-muted-foreground">Mounting workspace…</p>
            ) : rootOpen ? (
              <FileTree
                nodes={tree}
                activePath={active}
                isOpen={isOpen}
                onToggle={toggleFold}
                vroot={vroot}
                actions={{
                  onOpen: openFile,
                  onRename: rename,
                  onDelete: remove,
                  onNewFile: newFile,
                  onNewFolder: newFolder,
                  onCollapseAll: collapseAll,
                  onCopyPath: copyPath,
                  onMove: move,
                }}
              />
            ) : null}
          </div>
          </ContextMenuTrigger>
          <ContextMenuContent className="w-52">
            <ContextMenuItem onSelect={() => newFile()}>New file</ContextMenuItem>
            <ContextMenuItem onSelect={() => newFolder()}>New folder</ContextMenuItem>
            <ContextMenuSeparator />
            <ContextMenuItem onSelect={collapseAll}>Collapse all folders</ContextMenuItem>
            <ContextMenuItem onSelect={() => ready && refreshTree(session.sessionId!)}>Refresh</ContextMenuItem>
          </ContextMenuContent>
          </ContextMenu>
        )}
      </div>

      {/* Drag handle: a 1px divider with a wider invisible hit area. */}
      <div
        onMouseDown={() => setDragging(true)}
        className={`group/divider relative z-10 w-px shrink-0 cursor-col-resize bg-border ${dragging ? 'bg-primary' : ''}`}
        title="Drag to resize"
      >
        <div className="absolute inset-y-0 -left-1 -right-1" />
      </div>

      {/* Editor tabs + terminal, switched by the tab bar (its own scroll region) */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-stretch border-b border-border bg-muted/20">
          <div className="flex min-w-0 flex-1 items-stretch overflow-x-auto">
            {tabs.map((t) => (
              <ContextMenu key={t.path}>
                <ContextMenuTrigger asChild>
                  <div
                    draggable
                    onDragStart={() => { dragTab.current = t.path; }}
                    onDragOver={(e) => { if (dragTab.current && dragTab.current !== t.path) e.preventDefault(); }}
                    onDrop={(e) => { e.preventDefault(); if (dragTab.current) reorderTab(dragTab.current, t.path); dragTab.current = null; }}
                    onClick={() => { setActive(t.path); setView('editor'); }}
                    title={t.path}
                    className={`group flex cursor-pointer items-center gap-2 border-r border-border px-3 py-2 text-[13px] ${active === t.path && view === 'editor' ? 'bg-background' : 'text-muted-foreground hover:bg-accent/50'}`}
                  >
                    <FileCode className="size-3.5 shrink-0" />
                    <span className="max-w-[140px] truncate">{t.name}</span>
                    {t.dirty && <span className="size-1.5 rounded-full bg-foreground/60" />}
                    <button
                      onClick={(e) => { e.stopPropagation(); closeTab(t.path); }}
                      className="rounded p-0.5 opacity-0 hover:bg-accent group-hover:opacity-100"
                      title="Close"
                    >
                      <X className="size-3" />
                    </button>
                  </div>
                </ContextMenuTrigger>
                <ContextMenuContent className="w-52">
                  <ContextMenuItem onSelect={() => closeTab(t.path)}>Close</ContextMenuItem>
                  <ContextMenuItem onSelect={() => closeOthers(t.path)} disabled={tabs.length <= 1}>Close Others</ContextMenuItem>
                  <ContextMenuItem onSelect={() => closeToRight(t.path)}
                    disabled={tabs.findIndex((x) => x.path === t.path) >= tabs.length - 1}>Close to the Right</ContextMenuItem>
                  <ContextMenuItem onSelect={closeSaved}>Close Saved</ContextMenuItem>
                  <ContextMenuItem onSelect={closeAll}>Close All</ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem onSelect={() => save(t.path)} disabled={!t.dirty}>Save</ContextMenuItem>
                  <ContextMenuItem onSelect={() => copyPath(t.path)}>Copy Path</ContextMenuItem>
                  <ContextMenuItem onSelect={() => copyPath(t.path.replace(`${vroot}/`, ''))}>Copy Relative Path</ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
            ))}
          </div>
          <button
            onClick={() => setView('terminal')}
            className={`flex shrink-0 items-center gap-1.5 border-l border-border px-3 py-2 text-[13px] ${view === 'terminal' ? 'bg-background' : 'text-muted-foreground hover:bg-accent/50'}`}
            title="Terminal"
          >
            <TerminalSquare className="size-3.5" /> Terminal
          </button>
        </div>

        <div className="relative min-h-0 flex-1">
          {/* Terminal stays mounted so the PTY session persists across tab switches. */}
          <div className={view === 'terminal' ? 'absolute inset-0' : 'invisible absolute inset-0'}>
            <Terminal
              ref={termRef}
              connected={session.alive}
              onData={(d) => session.sessionId && session.send(session.sessionId, d)}
              onResize={(s) => session.sessionId && session.resize(session.sessionId, s)}
            />
          </div>
          {view === 'editor' && (
            <div className="absolute inset-0 flex flex-col">
              {!activeTab ? (
                <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                  Open a file from the explorer to edit it.
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
                    <span className="font-mono text-xs text-muted-foreground">{activeTab.path}</span>
                    <Button size="sm" variant="ghost" onClick={() => save(activeTab.path)} disabled={!activeTab.dirty}>Save</Button>
                  </div>
                  {activeTab.binary ? (
                    <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">Binary file.</div>
                  ) : (
                    <CodeEditor
                      value={activeTab.body}
                      onChange={(body) => edit(activeTab.path, body)}
                      onSave={() => save(activeTab.path)}
                      language={activeTab.language}
                    />
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
    <PromptDialog request={prompt} onClose={() => setPrompt(null)} />
    <ConfirmDeleteDialog request={confirm} onClose={() => setConfirm(null)} />
    {quickOpen && (
      <QuickOpen
        files={tree.filter((n) => !n.is_dir).map((n) => n.path)}
        vroot={vroot}
        onPick={(p) => { setQuickOpen(false); openFile(p); }}
        onClose={() => setQuickOpen(false)}
      />
    )}
    </>
  );
}

// Quick-open palette (Cmd/Ctrl+P): a centered overlay that fuzzy-filters the
// workspace's files by name/path and opens the chosen one. Arrow keys + Enter,
// Esc to dismiss; the same jump-to-file affordance VS Code's Cmd+P gives.
function QuickOpen({
  files, vroot, onPick, onClose,
}: {
  files: string[];
  vroot: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);

  // Subsequence fuzzy match (chars in order, gaps allowed), ranked by how tight
  // the match is; empty query shows everything (most-relevant order = as-listed).
  const matches = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rel = (p: string) => p.replace(`${vroot}/`, '');
    if (!needle) return files.map(rel).slice(0, 50);
    const scored: { path: string; score: number }[] = [];
    for (const p of files) {
      const hay = rel(p).toLowerCase();
      let i = 0, score = 0, lastHit = -1;
      for (let h = 0; h < hay.length && i < needle.length; h++) {
        if (hay[h] === needle[i]) {
          score += lastHit === h - 1 ? 3 : 1;      // reward consecutive hits
          if (hay[h] === '/' || h === 0) score += 2;
          lastHit = h; i++;
        }
      }
      if (i === needle.length) scored.push({ path: rel(p), score });
    }
    scored.sort((a, b) => b.score - a.score || a.path.length - b.path.length);
    return scored.slice(0, 50).map((s) => s.path);
  }, [q, files, vroot]);

  useEffect(() => { setSel(0); }, [q]);
  const pick = (relPath: string) => onPick(`${vroot}/${relPath}`);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 pt-[12vh]"
      onClick={onClose}>
      <div className="w-[min(560px,90vw)] overflow-hidden rounded-lg border border-border bg-popover shadow-xl"
        onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(matches.length - 1, s + 1)); }
            if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
            if (e.key === 'Enter' && matches[sel]) { e.preventDefault(); pick(matches[sel]!); }
            if (e.key === 'Escape') { e.preventDefault(); onClose(); }
          }}
          placeholder="Go to file by name…"
          className="w-full border-b border-border bg-transparent px-3 py-2.5 font-mono text-[13px] outline-none"
        />
        <div className="max-h-[50vh] overflow-auto py-1">
          {matches.length === 0 ? (
            <div className="px-3 py-2 text-xs text-muted-foreground">No matching files</div>
          ) : matches.map((m, i) => (
            <button
              key={m}
              onMouseEnter={() => setSel(i)}
              onClick={() => pick(m)}
              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left font-mono text-[12px] ${i === sel ? 'bg-accent text-foreground' : 'text-foreground/80 hover:bg-accent/50'}`}
            >
              <FileCode className="size-3.5 shrink-0 text-muted-foreground" />
              <span className="truncate">{m}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// The content-search panel (Cmd/Ctrl+F): a query box + results grouped by file,
// each hit a clickable line. Lives in the sidebar in place of the tree while open.
function SearchPanel({
  inputRef, query, setQuery, results, searching, vroot, onRun, onClose, onPick,
}: {
  inputRef: React.RefObject<HTMLInputElement | null>;
  query: string;
  setQuery: (q: string) => void;
  results: SearchFileResult[];
  searching: boolean;
  vroot: string;
  onRun: (q: string) => void;
  onClose: () => void;
  onPick: (path: string, line: number) => void;
}) {
  const total = results.reduce((n, r) => n + r.hits.length, 0);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-1 px-2 pb-1.5">
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onRun(query);
            if (e.key === 'Escape') onClose();
          }}
          placeholder="Search in files"
          className="h-7 min-w-0 flex-1 rounded border border-input bg-background px-2 font-mono text-[12px] outline-none focus:border-ring focus:ring-1 focus:ring-ring"
        />
        <button onClick={onClose} title="Close search"
          className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">
          <X className="size-3.5" />
        </button>
      </div>
      <div className="px-2 pb-1 text-[11px] text-muted-foreground">
        {searching ? 'Searching…'
          : query.trim()
            ? (total ? `${total} result${total === 1 ? '' : 's'} in ${results.length} file${results.length === 1 ? '' : 's'}` : 'No results')
            : 'Type a query, then Enter'}
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-1 pb-2">
        {results.map((r) => (
          <div key={r.path} className="mb-1.5">
            <div className="flex items-center gap-1 px-1.5 py-0.5 font-mono text-[11px] font-medium text-foreground/70" title={r.path}>
              <FileCode className="size-3 shrink-0" />
              <span className="truncate">{r.path.replace(`${vroot}/`, '')}</span>
            </div>
            {r.hits.map((h) => (
              <button
                key={`${r.path}:${h.line}`}
                onClick={() => onPick(r.path, h.line)}
                title={`${r.path}:${h.line}  ${h.text.trim()}`}
                className="flex w-full items-start gap-2 rounded px-1.5 py-0.5 text-left hover:bg-accent"
              >
                <span className="w-7 shrink-0 text-right font-mono text-[11px] text-muted-foreground/50">{h.line}</span>
                <span className="truncate font-mono text-[12px] text-foreground/90">{h.text.trim()}</span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

// The VS Code "no folder open" welcome panel: shown when the session has no folder
// (Close Folder, or a plain box with nothing to default to). One primary Open Folder
// action plus quick targets, so the attendee is never stuck without a way back in.
function NoFolder({
  onOpen, onHome, recent, onRecent, disabled,
}: {
  onOpen: () => void;
  onHome: () => void;
  recent: string[];
  onRecent: (path: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-4 text-center">
      <FolderOpen className="size-8 text-muted-foreground/40" />
      <p className="text-[13px] text-muted-foreground">No folder open</p>
      <Button size="sm" onClick={onOpen} disabled={disabled} className="gap-1.5">
        <FolderInput className="size-4" /> Open Folder
      </Button>
      <div className="flex flex-col items-stretch gap-1 pt-1 text-[12px]">
        <button onClick={onHome} disabled={disabled}
          className="flex items-center gap-1.5 rounded px-2 py-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50">
          <House className="size-3.5" /> Home (~)
        </button>
        {recent.length > 0 && (
          <>
            <p className="px-2 pt-1.5 text-left text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">Recent</p>
            {recent.map((p) => (
              <button key={p} onClick={() => onRecent(p)} disabled={disabled}
                className="flex items-center gap-1.5 rounded px-2 py-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50">
                <FolderOpen className="size-3.5 shrink-0" />
                <span className="truncate">{p}</span>
              </button>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
