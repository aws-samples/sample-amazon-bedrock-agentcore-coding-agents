import { useMemo, useState } from 'react';
import {
  ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator,
} from '@foxl/ui';
import {
  ChevronRight, Folder, FolderOpen, FileText, FileCode, FileJson, FileCog, File,
} from 'lucide-react';
import type { FileNode } from '../hooks/useSession';

// The MIME-ish key we stash the dragged node's path under, so a drop reads the
// source path back. (A plain string drag payload, no external drops.)
const DRAG_TYPE = 'application/x-ws-path';

// One node in the nested tree the explorer renders. The backend returns a FLAT
// list of {path, name, is_dir}; we fold it into this nested shape so folders
// expand and children indent under their parent, the way VS Code paints it.
interface TreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  children: TreeNode[];
}

// The default virtual root (the clone-first ~/src checkout); the live root is the
// session's, passed in as `vroot` so an opened folder (~, /mnt/s3files, or any
// path) builds a correct tree.
const DEFAULT_VROOT = '~/src';

// Workspace-relative path (drop the vroot prefix) for the "Copy relative path"
// menu action; the full virtual path is what "Copy path" yields.
const relPath = (p: string, vroot: string) => p.replace(vroot, '').replace(/^\/+/, '') || p;

function buildTree(nodes: FileNode[], vroot: string): TreeNode[] {
  const root: TreeNode = { name: '', path: vroot, is_dir: true, children: [] };
  const byPath = new Map<string, TreeNode>([[vroot, root]]);

  // Sort by path so parents are created before children.
  const sorted = [...nodes].sort((a, b) => a.path.localeCompare(b.path));
  for (const n of sorted) {
    const rel = n.path.startsWith(vroot) ? n.path.slice(vroot.length) : n.path;
    const parts = rel.split('/').filter(Boolean);
    let parentPath = vroot;
    // Ensure every ancestor directory exists as a node (covers gaps).
    for (let i = 0; i < parts.length; i++) {
      const isLast = i === parts.length - 1;
      const path = `${vroot}/${parts.slice(0, i + 1).join('/')}`;
      if (!byPath.has(path)) {
        const node: TreeNode = {
          name: parts[i]!,
          path,
          is_dir: isLast ? !!n.is_dir : true,
          children: [],
        };
        byPath.set(path, node);
        byPath.get(parentPath)!.children.push(node);
      }
      parentPath = path;
    }
  }
  // Sort each level: directories first, then files, both alphabetical.
  const sortLevel = (node: TreeNode) => {
    node.children.sort((a, b) =>
      a.is_dir !== b.is_dir ? (a.is_dir ? -1 : 1) : a.name.localeCompare(b.name));
    node.children.forEach(sortLevel);
  };
  sortLevel(root);
  return root.children;
}

// All folder paths in the tree (for Collapse all + the parent open-state map).
export function folderPaths(nodes: FileNode[], vroot: string = DEFAULT_VROOT): string[] {
  const out: string[] = [];
  const walk = (ns: TreeNode[]) => ns.forEach((n) => {
    if (n.is_dir) { out.push(n.path); walk(n.children); }
  });
  walk(buildTree(nodes, vroot));
  return out;
}

// File-type icon by extension: a small visual cue, like an editor's file tree.
function iconFor(name: string) {
  const ext = name.slice(name.lastIndexOf('.') + 1).toLowerCase();
  if (['py', 'js', 'ts', 'tsx', 'jsx', 'sh', 'html', 'css'].includes(ext)) return FileCode;
  if (['json', 'jsonl'].includes(ext)) return FileJson;
  if (['md', 'txt', 'rst'].includes(ext)) return FileText;
  if (['toml', 'yaml', 'yml', 'ini', 'cfg', 'conf'].includes(ext)) return FileCog;
  return File;
}

export interface FileTreeActions {
  onOpen: (path: string) => void;
  onRename: (path: string) => void;
  onDelete: (path: string) => void;
  onNewFile: (dirPath?: string) => void;
  onNewFolder: (dirPath?: string) => void;
  onCollapseAll: () => void;
  onCopyPath: (path: string) => void;
  /** Drag-and-drop move: relocate `src` INTO directory `destDir` (VROOT = root). */
  onMove: (src: string, destDir: string) => void;
}

export function FileTree({
  nodes, activePath, actions, isOpen, onToggle, vroot = DEFAULT_VROOT,
}: {
  nodes: FileNode[];
  activePath: string | null;
  actions: FileTreeActions;
  /** Controlled open-state: the Workspace owns it so Collapse-all + reload persistence work. */
  isOpen: (path: string, depth: number) => boolean;
  onToggle: (path: string, next: boolean) => void;
  /** The live virtual root the session reports (/mnt/s3files, ~, or an opened folder). */
  vroot?: string;
}) {
  const tree = useMemo(() => buildTree(nodes, vroot), [nodes, vroot]);
  // The folder path currently hovered as a drop target (highlight ring), or
  // vroot while hovering the blank area to drop at the workspace root.
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  if (tree.length === 0) {
    // Empty workspace: the Workspace wraps the WHOLE explorer pane in a ContextMenu,
    // so right-clicking anywhere here (including the long blank area below) opens
    // New file / New folder. This is just the hint text.
    return (
      <div className="px-2 py-2 text-xs text-muted-foreground">
        Empty workspace. Right-click anywhere to create a file or folder.
      </div>
    );
  }
  // Dropping onto the blank list area (not over any row) moves to the workspace root.
  const rootDragOver = (e: React.DragEvent) => {
    if (!e.dataTransfer.types.includes(DRAG_TYPE)) return;
    e.preventDefault();
    if (e.target === e.currentTarget) setDropTarget(vroot);
  };
  const rootDrop = (e: React.DragEvent) => {
    const src = e.dataTransfer.getData(DRAG_TYPE);
    setDropTarget(null);
    if (src && e.target === e.currentTarget) { e.preventDefault(); actions.onMove(src, vroot); }
  };
  return (
    <ul
      onDragOver={rootDragOver}
      onDrop={rootDrop}
      onDragLeave={(e) => { if (e.target === e.currentTarget && dropTarget === vroot) setDropTarget(null); }}
      className={`min-h-full ${dropTarget === vroot ? 'rounded bg-accent/40 ring-1 ring-primary/40' : ''}`}
    >
      {tree.map((n) => (
        <TreeRow key={n.path} node={n} depth={0} activePath={activePath}
          actions={actions} isOpen={isOpen} onToggle={onToggle}
          dropTarget={dropTarget} setDropTarget={setDropTarget} vroot={vroot} />
      ))}
    </ul>
  );
}

// The destination DIRECTORY a node would drop INTO: a folder accepts into itself;
// a file accepts into its parent (VS Code drops a file beside the hovered file).
const dirTargetOf = (node: TreeNode, vroot: string) =>
  node.is_dir ? node.path : node.path.slice(0, node.path.lastIndexOf('/')) || vroot;

function TreeRow({
  node, depth, activePath, actions, isOpen, onToggle, dropTarget, setDropTarget, vroot,
}: {
  node: TreeNode;
  depth: number;
  activePath: string | null;
  actions: FileTreeActions;
  isOpen: (path: string, depth: number) => boolean;
  onToggle: (path: string, next: boolean) => void;
  dropTarget: string | null;
  setDropTarget: (p: string | null) => void;
  vroot: string;
}) {
  const open = node.is_dir && isOpen(node.path, depth);
  const active = node.path === activePath;
  const Icon = node.is_dir ? (open ? FolderOpen : Folder) : iconFor(node.name);
  const pad = 8 + depth * 12;
  const target = dirTargetOf(node, vroot);
  const isDropHere = dropTarget === target && (node.is_dir || dropTarget !== vroot);

  // Drag-and-drop move (VS Code parity). A row is draggable; dragging over a
  // folder (or a file, targeting its parent) rings the destination; a drop calls
  // onMove(src, destDir). We never let a node drop into itself or its own subtree.
  const onDragStart = (e: React.DragEvent) => {
    e.dataTransfer.setData(DRAG_TYPE, node.path);
    e.dataTransfer.effectAllowed = 'move';
  };
  const wouldRecurse = (src: string) =>
    target === src || target.startsWith(`${src}/`) ||
    target === (src.slice(0, src.lastIndexOf('/')) || vroot);  // already there
  const onDragOver = (e: React.DragEvent) => {
    if (!e.dataTransfer.types.includes(DRAG_TYPE)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    setDropTarget(target);
  };
  const onDrop = (e: React.DragEvent) => {
    const src = e.dataTransfer.getData(DRAG_TYPE);
    e.preventDefault();
    e.stopPropagation();
    setDropTarget(null);
    if (src && src !== node.path && !wouldRecurse(src)) actions.onMove(src, target);
  };

  return (
    <li>
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <button
            draggable
            onDragStart={onDragStart}
            onDragOver={onDragOver}
            onDrop={onDrop}
            onClick={() => (node.is_dir ? onToggle(node.path, !open) : actions.onOpen(node.path))}
            onKeyDown={(e) => {
              // VS Code: F2 renames the focused row, Delete removes it.
              if (e.key === 'F2') { e.preventDefault(); actions.onRename(node.path); }
              if (e.key === 'Delete' || (e.key === 'Backspace' && (e.metaKey || e.ctrlKey))) {
                e.preventDefault(); actions.onDelete(node.path);
              }
            }}
            style={{ paddingLeft: pad }}
            title={node.name}
            className={`flex w-full items-center gap-1 rounded-md py-1 pr-2 text-left text-[13px] hover:bg-accent ${active ? 'bg-accent' : ''} ${isDropHere ? 'bg-accent/60 ring-1 ring-inset ring-primary/50' : ''}`}
          >
            {node.is_dir ? (
              <ChevronRight className={`size-3 shrink-0 text-muted-foreground transition-transform ${open ? 'rotate-90' : ''}`} />
            ) : (
              <span className="inline-block w-3 shrink-0" />
            )}
            <Icon className={`size-3.5 shrink-0 ${node.is_dir ? 'text-primary' : 'text-muted-foreground'}`} />
            <span className="truncate">{node.name}</span>
          </button>
        </ContextMenuTrigger>
        <ContextMenuContent className="w-52">
          {node.is_dir ? (
            <>
              <ContextMenuItem onSelect={() => actions.onNewFile(node.path)}>New file</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onNewFolder(node.path)}>New folder</ContextMenuItem>
              <ContextMenuSeparator />
              <ContextMenuItem onSelect={() => actions.onRename(node.path)}>Rename</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onDelete(node.path)} className="text-destructive">Delete folder</ContextMenuItem>
              <ContextMenuSeparator />
              <ContextMenuItem onSelect={() => actions.onCopyPath(node.path)}>Copy path</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onCopyPath(relPath(node.path, vroot))}>Copy relative path</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onCollapseAll()}>Collapse all folders</ContextMenuItem>
            </>
          ) : (
            <>
              <ContextMenuItem onSelect={() => actions.onOpen(node.path)}>Open</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onRename(node.path)}>Rename</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onDelete(node.path)} className="text-destructive">Delete</ContextMenuItem>
              <ContextMenuSeparator />
              <ContextMenuItem onSelect={() => actions.onCopyPath(node.path)}>Copy path</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onCopyPath(relPath(node.path, vroot))}>Copy relative path</ContextMenuItem>
              <ContextMenuSeparator />
              <ContextMenuItem onSelect={() => actions.onNewFile()}>New file</ContextMenuItem>
              <ContextMenuItem onSelect={() => actions.onNewFolder()}>New folder</ContextMenuItem>
            </>
          )}
        </ContextMenuContent>
      </ContextMenu>
      {node.is_dir && open && node.children.length > 0 && (
        <ul>
          {node.children.map((c) => (
            <TreeRow key={c.path} node={c} depth={depth + 1} activePath={activePath}
              actions={actions} isOpen={isOpen} onToggle={onToggle}
              dropTarget={dropTarget} setDropTarget={setDropTarget} vroot={vroot} />
          ))}
        </ul>
      )}
    </li>
  );
}
