"""Read explicit build-output declarations without executing project commands.

A declaration explains why a bundled file can be left out of source review; it
does not prove that the committed bytes were reproduced by that build. Unsupported
or ambiguous declarations return no exemption from the normal source limits.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex


_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_SCRIPT_CHARS = 8192
_SKIP_DIRS = frozenset({"node_modules", "venv", "__pycache__"})
_SHELL_EXPANSION = re.compile(r"[;&|<>`$(){}\r\n\\]")
_LOADER = re.compile(
    r"--loader:\.[A-Za-z0-9._-]+="
    r"(?:base64|binary|copy|css|dataurl|empty|file|global-css|js|json|jsx|local-css|text|ts|tsx)\Z"
)


@dataclass(frozen=True)
class DeclaredOutput:
    source: str
    reason: str


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _visible_file(root: Path, directory: Path, name: str) -> str | None:
    """Resolve literal package-relative paths inside the reviewed source tree."""
    if not name or any(char in name for char in "*?[]~") or Path(name).is_absolute():
        return None
    candidate = directory / name
    try:
        relative = candidate.resolve(strict=True).relative_to(root)
        # Follow neither external nor internal symlinks to infer provenance.
        cursor = candidate
        while cursor != root:
            if cursor.is_symlink():
                return None
            if cursor == cursor.parent:
                return None
            cursor = cursor.parent
        if any(part.startswith(".") or part in _SKIP_DIRS for part in relative.parts):
            return None
        if not candidate.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return relative.as_posix()


def _esbuild_paths(script: str) -> tuple[str, str] | None:
    """Only a single literal CLI call with one bundled, minified outfile.

    Do not interpret shell programs, npm aliases, config files, globs, output
    directories or unknown flags. Keeping such files in review is the safe answer.
    """
    if len(script) > _MAX_SCRIPT_CHARS or _SHELL_EXPANSION.search(script):
        return None
    try:
        tokens = shlex.split(script)
    except ValueError:
        return None
    if not tokens or tokens[0] not in {"esbuild", "./node_modules/.bin/esbuild"}:
        return None
    entries = []
    outputs = []
    flags = set()
    for token in tokens[1:]:
        if token in {"--bundle", "--minify"}:
            if token in flags:
                return None
            flags.add(token)
        elif token.startswith("--outfile="):
            outputs.append(token.removeprefix("--outfile="))
        elif _LOADER.fullmatch(token):
            continue
        elif token.startswith("-"):
            return None
        else:
            entries.append(token)
    if flags != {"--bundle", "--minify"} or len(entries) != 1 or len(outputs) != 1:
        return None
    return entries[0], outputs[0]


def declared_outputs(directory: str) -> dict[str, DeclaredOutput]:
    """Return conservative, inspectable exemptions for named generated outputs."""
    root = Path(directory).resolve()
    claims = defaultdict(list)
    inputs = set()
    for folder, directories, names in os.walk(root):
        directories[:] = sorted(
            name for name in directories
            if not name.startswith(".") and name not in _SKIP_DIRS
            and not (Path(folder) / name).is_symlink()
        )
        if "package.json" not in names:
            continue
        manifest = Path(folder) / "package.json"
        try:
            if manifest.is_symlink() or manifest.stat().st_size > _MAX_MANIFEST_BYTES:
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_object)
            scripts = data.get("scripts") if isinstance(data, dict) else None
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(scripts, dict):
            continue
        for name, script in scripts.items():
            if not isinstance(script, str):
                continue
            paths = _esbuild_paths(script)
            if paths is None:
                continue
            source = _visible_file(root, manifest.parent, paths[0])
            output = _visible_file(root, manifest.parent, paths[1])
            if source is None or output is None:
                continue
            inputs.add(source)
            if source == output or Path(output).suffix.lower() not in {".js", ".mjs", ".cjs", ".css"}:
                continue
            reason = (
                "declared generated build output: "
                f"{manifest.relative_to(root).as_posix()} scripts[{json.dumps(name)}] "
                f"declares esbuild --bundle --minify from {source} to {output}; "
                "committed output was not reproduced for this review"
            )
            claims[output].append(DeclaredOutput(source, reason))
    # A source input must never disappear as another script's output. Multiple
    # declarations for one path are ambiguous, even if their commands look alike.
    return {
        output: rows[0] for output, rows in claims.items()
        if len(rows) == 1 and output not in inputs
    }
