"""The Development page's workspace files: a tree/read/write surface over a session's
jailed directory, and the path translation between its real root and the virtual
root the UI shows (``/mnt/s3files``, ``~``, or the folder the attendee opened).

Split out of interactive_api.py, which re-exports every name, so ``dispatch`` and
the harness scaffolder use them unchanged. It depends only on the session dict
those functions receive; nothing here opens a PTY, runs a command, or deploys.
"""
from __future__ import annotations

import os
import shutil


def _to_virtual(session: dict, text: str) -> str:
    # Map the session's real workspace root to the /mnt/s3files virtual root for
    # BOTH role and Development sessions. The frontend's file tree strips
    # /mnt/s3files to render the workspace's own top-level entries; without this a
    # dev session (rooted at the real clone abs path) rendered the entire absolute
    # path as phantom folders (home > ubuntu > <clone> > coding-agents > ...) and the
    # open/read/write round-trip broke (_safe_join expects /mnt/s3files-relative).
    # The interactive PTY is a separate raw stream, so real paths still show in the
    # live terminal; only the file tree + scripted /input output are normalized.
    # The virtual root is per-session (_vroot): /mnt/s3files on the workshop box, the
    # login HOME (~) on a plain local box, or whatever folder the attendee opened.
    return text.replace(session["_root"], session.get("_vroot", "/mnt/s3files"))


def _to_real(session: dict, text: str) -> str:
    return text.replace(session.get("_vroot", "/mnt/s3files"), session["_root"])


_TEXT_EXT = {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".html",
             ".css", ".js", ".sh", ".cfg", ".ini", ".mdc", ""}
_MAX_FILE = 200_000          # bytes; refuse to read/write larger blobs in the UI
# Junk skipped at EVERY depth (never attendee work). Agent steering dirs created on
# the mount (AGENTS.md, .config/opencode/opencode.json, .kiro/steering/*.md) remain visible because
# they are workshop files. Root CLI cache names are filtered separately below.
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules",
              ".local", ".cache", ".config", ".npm", ".aws",
              "Library", ".semantic_search"}
# Agent-CLI config artifacts hidden ONLY at the workspace ROOT. CLAUDE_CONFIG_DIR
# is the workspace root, so claude drops these bare names there (cache/, plugins/,
# settings.json, …). An attendee's OWN nested skills/logs/ or a settings.json they
# create inside a project dir must stay visible; root-only keeps both true.
_SKIP_DIRS_ROOT = {"marketplaces", "plugins", "backups", "statsig",
                   "shell-snapshots", "cache", "projects", "todos", "logs",
                   "ide", "history"}
_SKIP_FILES_ROOT = {".claude.json", ".claude.json.backup", "settings.json",
                    "known_marketplaces.json", ".last-update-result.json",
                    "changelog.md", ".bash_history", ".viminfo"}


def _safe_join(session: dict, rel: str) -> str | None:
    """Resolve a vroot-relative path to a real path INSIDE the jail, or None."""
    rel = (rel or "").replace(session.get("_vroot", "/mnt/s3files"), "").lstrip("/")
    full = os.path.normpath(os.path.join(session["_root"], rel))
    root = os.path.realpath(session["_root"])
    if os.path.realpath(full) == root or os.path.realpath(full).startswith(root + os.sep):
        return full
    return None


# Dot dirs that ARE attendee work and stay visible (steering); every OTHER dotdir
# at depth is skipped so opening HOME doesn't descend ~/.vscode, ~/.git, ~/Library…
_KEEP_DOTDIRS = {".config", ".kiro", ".claude"}


def _file_tree(session: dict) -> list[dict]:
    """Return the workspace as a flat, sorted list of {path,type,size} (dirs first).

    Bounded by depth + node count so opening a huge folder (e.g. a real HOME with
    100k+ files) returns a usable tree fast instead of walking everything and hanging
    the "Mounting workspace…" spinner. VS Code lazily loads on expand; we keep the
    flat-list contract but cap DEPTH + total NODES and skip un-authored dot-dirs.
    The caps are read at CALL time (a real env seam tests can set), not import."""
    root = session.get("_root") or ""
    if not root or not os.path.isdir(root):
        return []          # no-folder (VS Code welcome) state, or a stale root
    max_depth = int(os.environ.get("WORKSHOP_TREE_MAX_DEPTH", "8"))
    max_nodes = int(os.environ.get("WORKSHOP_TREE_MAX_NODES", "4000"))
    out: list[dict] = []
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(out) >= max_nodes:
            break
        at_root = os.path.samefile(dirpath, root) if os.path.exists(dirpath) else False
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        skip_dirs = _SKIP_DIRS | (_SKIP_DIRS_ROOT if at_root else set())
        # Prune: junk dirs always; past the depth cap stop descending; and below the
        # root, skip dot-dirs that are not the agent-steering ones we want to show.
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in skip_dirs
            and depth < max_depth
            and not (d.startswith(".") and d not in _KEEP_DOTDIRS))
        for d in dirnames:
            full = os.path.join(dirpath, d)
            out.append({"path": _to_virtual(session, full), "type": "dir", "size": 0})
        for fn in sorted(filenames):
            if len(out) >= max_nodes:
                break
            if at_root and fn in _SKIP_FILES_ROOT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({"path": _to_virtual(session, full), "type": "file", "size": size})
    # Hierarchical (DFS) order, directories before files among siblings: the
    # order a VS Code explorer paints, so a child row always sits directly
    # under its parent. (The old depth-first-by-LEVEL sort scattered children
    # to the bottom of the list.)
    vroot = session.get("_vroot", "/mnt/s3files")
    def _sort_key(e: dict) -> list:
        parts = e["path"].replace(vroot, "").strip("/").split("/")
        return ([(0, c) for c in parts[:-1]]
                + [(0 if e["type"] == "dir" else 1, parts[-1])])
    out.sort(key=_sort_key)
    return out


def _search_files(session: dict, query: str, *, max_files: int = 200,
                  max_hits: int = 300) -> dict:
    """Content-based workspace search (the editor's Cmd+F across files). Walks the
    same jail/skip rules as the tree, reads each text file once, and returns the
    matching lines grouped by file: [{path, hits:[{line, text}]}]. Case-insensitive
    substring match (not a regex, so a stray bracket can't error). Bounded by
    max_files / max_hits so a huge workspace never blocks the loop."""
    q = (query or "").strip()
    if not q:
        return {"query": query, "results": [], "truncated": False}
    needle = q.lower()
    root = session["_root"]
    results: list[dict] = []
    hits_total = 0
    files_scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        at_root = os.path.samefile(dirpath, root) if os.path.exists(dirpath) else False
        skip_dirs = _SKIP_DIRS | (_SKIP_DIRS_ROOT if at_root else set())
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for fn in sorted(filenames):
            if at_root and fn in _SKIP_FILES_ROOT:
                continue
            if os.path.splitext(fn)[1].lower() not in _TEXT_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > _MAX_FILE:
                    continue
                with open(full, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            files_scanned += 1
            if files_scanned > max_files:
                truncated = True
                break
            file_hits: list[dict] = []
            for i, line in enumerate(lines, 1):
                if needle in line.lower():
                    file_hits.append({"line": i, "text": line[:400]})
                    hits_total += 1
                    if hits_total >= max_hits:
                        truncated = True
                        break
            if file_hits:
                results.append({"path": _to_virtual(session, full), "hits": file_hits})
            if truncated:
                break
        if truncated:
            break
    return {"query": q, "results": results, "truncated": truncated}


def _read_file(session: dict, rel: str) -> dict:
    full = _safe_join(session, rel)
    if not full or not os.path.isfile(full):
        return {"error": "file not found", "path": rel}
    if os.path.getsize(full) > _MAX_FILE:
        return {"error": "file too large to open", "path": rel}
    ext = os.path.splitext(full)[1].lower()
    try:
        with open(full, encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return {"path": _to_virtual(session, full), "binary": True, "content": ""}
    return {"path": _to_virtual(session, full), "binary": False,
            "language": _lang_for(ext), "content": content}


def _write_file(session: dict, rel: str, content: str) -> dict:
    full = _safe_join(session, rel)
    if full is None:
        return {"error": "path escapes workspace", "path": rel}
    if len(content or "") > _MAX_FILE:
        return {"error": "content too large", "path": rel}
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": _to_virtual(session, full), "bytes": len(content.encode("utf-8")),
            "tree": _file_tree(session)}


def _delete_file(session: dict, rel: str) -> dict:
    """Remove one workspace file OR directory (the explorer's right-click Delete).

    Stays inside the jail via _safe_join; a directory is removed recursively (the
    explorer can delete a folder, like VS Code). The workspace root itself cannot
    be deleted. Missing file / bad path return {"error": ...}, never raise.
    """
    full = _safe_join(session, rel)
    if full is None:
        return {"error": "invalid path", "path": rel}
    if not os.path.exists(full):
        return {"error": "not found", "path": rel}
    if os.path.realpath(full) == os.path.realpath(session["_root"]):
        return {"error": "cannot delete the workspace root", "path": rel}
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    except OSError as exc:
        return {"error": str(exc), "path": rel}
    return {"ok": True, "path": rel, "tree": _file_tree(session)}


def _make_dir(session: dict, rel: str) -> dict:
    """Create a new directory in the workspace (the explorer's New Folder).

    Stays inside the jail via _safe_join; an existing path is reported, never
    silently merged into. Returns the fresh tree so the explorer re-renders.
    """
    full = _safe_join(session, rel)
    if full is None:
        return {"error": "path escapes workspace", "path": rel}
    if os.path.exists(full):
        return {"error": "already exists", "path": rel}
    try:
        os.makedirs(full, exist_ok=False)
    except OSError as exc:
        return {"error": str(exc), "path": rel}
    return {"ok": True, "path": _to_virtual(session, full), "tree": _file_tree(session)}


def _rename_file(session: dict, rel: str, to: str) -> dict:
    """Move/rename one workspace file within the jail (explorer's Rename).

    BOTH the source and the destination must resolve inside the session root
    via _safe_join; either escaping rejects with {"error": "invalid path"}.
    Returns the fresh tree on success.
    """
    src = _safe_join(session, rel)
    dst = _safe_join(session, to)
    if src is None or dst is None:
        return {"error": "invalid path", "path": rel, "to": to}
    if not os.path.exists(src):
        return {"error": "not found", "path": rel}
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
    except OSError as exc:
        return {"error": str(exc), "path": rel, "to": to}
    return {"ok": True, "path": _to_virtual(session, dst), "tree": _file_tree(session)}


def _lang_for(ext: str) -> str:
    return {".py": "python", ".md": "markdown", ".json": "json", ".toml": "toml",
            ".yaml": "yaml", ".yml": "yaml", ".html": "html", ".css": "css",
            ".js": "javascript", ".sh": "bash", ".mdc": "markdown"}.get(ext, "text")
