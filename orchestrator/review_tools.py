"""Read-only tools the integrated reviewer uses on ONE pull request's tree.

November 18: the reviewer used to receive every changed file AND the base branch in
one prompt, and refused to review past a size budget (9 of 60 green team builds on
November 17). A reviewer reads like a person does instead, the way Foxl's reviewer
works (`foxl/shared/code-orchestrator/reviewer-prompt.ts`, RV1): it starts from the
pull request's own changes and the check that decided its gate, then reads, lists
and searches whatever else it needs. Nothing here writes, runs, or reaches the
network; every path stays inside the tree the executable gate inspected.
"""
from __future__ import annotations

import os
import re
from typing import Any

_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})
_MAX_READ_LINES = 1500
_MAX_READ_CHARS = 100_000
_MAX_LIST_ENTRIES = 400
_MAX_SEARCH_RESULTS = 80
_MAX_SEARCH_FILE_BYTES = 2_000_000

READ_TOOLS: list[dict[str, Any]] = [
    {"toolSpec": {
        "name": "read_file",
        "description": (
            "Read a text file of the pull request's tree as it would merge (the base "
            "branch plus this pull request), with line numbers. Long files arrive in "
            f"pages of up to {_MAX_READ_LINES} lines: pass start_line to continue."),
        "inputSchema": {"json": {
            "type": "object", "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "path relative to the tree root"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            }}},
    }},
    {"toolSpec": {
        "name": "list_files",
        "description": "List the files under a directory of the tree, with their sizes.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "directory, default the root"}},
        }}},
    },
    {"toolSpec": {
        "name": "search_code",
        "description": (
            "Search the tree's text files for a regular expression (a plain substring "
            "also works) and return matching lines as path:line: text. Use it to find "
            "every place that reads a field, route, or function this pull request changes."),
        "inputSchema": {"json": {
            "type": "object", "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "directory to search, default the root"},
            }}},
    }},
]
TOOL_NAMES = frozenset(spec["toolSpec"]["name"] for spec in READ_TOOLS)


class ReviewTree:
    """The read-only view of one pull request's merged tree."""

    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        self.reads: list[str] = []

    def _resolve(self, rel: Any) -> str:
        rel = str(rel or ".").replace("\\", "/").lstrip("/")
        full = os.path.realpath(os.path.join(self.root, rel))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ValueError(f"{rel} is outside the pull request's tree")
        if any(part in _SKIP_DIRS for part in os.path.relpath(full, self.root).split(os.sep)):
            raise ValueError(f"{rel} is installed or version-control data, not source")
        return full

    def run(self, name: str, args: Any) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            if name == "read_file":
                return self.read_file(args.get("path"), args.get("start_line"), args.get("end_line"))
            if name == "list_files":
                return self.list_files(args.get("path"))
            if name == "search_code":
                return self.search_code(args.get("pattern"), args.get("path"))
        except (OSError, ValueError) as exc:
            return f"ERROR: {exc}"
        return f"ERROR: unknown tool {name}"

    def read_file(self, path: Any, start_line: Any = None, end_line: Any = None) -> str:
        full = self._resolve(path)
        if not os.path.isfile(full):
            return f"ERROR: {path} is not a file in the tree"
        with open(full, "rb") as f:
            raw = f.read()
        if b"\0" in raw[:8192]:
            return f"{path}: binary file, {len(raw):,} bytes (not shown)"
        lines = raw.decode("utf-8", errors="replace").splitlines()
        first = max(1, int(start_line or 1))
        last = min(len(lines), int(end_line or first + _MAX_READ_LINES - 1),
                   first + _MAX_READ_LINES - 1)
        out, used = [], 0
        for number in range(first, last + 1):
            line = lines[number - 1]
            if used + len(line) > _MAX_READ_CHARS:
                last = number - 1
                break
            out.append(f"{number:>6}  {line}")
            used += len(line) + 1
        self.reads.append(str(path))
        tail = (f"\n[{path}: lines {first}-{last} of {len(lines)}; read_file with "
                f"start_line={last + 1} for the rest]" if last < len(lines) else
                f"\n[{path}: end of file, {len(lines)} lines]")
        return "\n".join(out) + tail

    def list_files(self, path: Any = None) -> str:
        top = self._resolve(path)
        if not os.path.isdir(top):
            return f"ERROR: {path} is not a directory in the tree"
        rows = []
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rows.append(f"{os.path.relpath(full, self.root)}  {os.path.getsize(full):,} bytes")
                if len(rows) >= _MAX_LIST_ENTRIES:
                    return "\n".join(rows) + f"\n[first {_MAX_LIST_ENTRIES} entries; list a subdirectory for more]"
        return "\n".join(rows) or "(no files)"

    def search_code(self, pattern: Any, path: Any = None) -> str:
        text = str(pattern or "")
        if not text:
            return "ERROR: give a pattern"
        try:
            rx = re.compile(text)
        except re.error:
            rx = re.compile(re.escape(text))
        top = self._resolve(path)
        hits = []
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) > _MAX_SEARCH_FILE_BYTES:
                        continue
                    with open(full, "rb") as f:
                        raw = f.read()
                except OSError:
                    continue
                if b"\0" in raw[:8192]:
                    continue
                rel = os.path.relpath(full, self.root)
                for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{number}: {line.strip()[:300]}")
                        if len(hits) >= _MAX_SEARCH_RESULTS:
                            return "\n".join(hits) + f"\n[first {_MAX_SEARCH_RESULTS} matches; narrow the pattern or the path]"
        return "\n".join(hits) or "(no matches)"
