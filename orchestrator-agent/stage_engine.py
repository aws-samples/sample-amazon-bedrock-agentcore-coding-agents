"""Stage the build engine into this directory so the container is self-contained.

`main.py` dispatches to the engine in `orchestrator/` (presets, engine, reviewer,
…). In the repo that code is one level up (`../orchestrator`); the container build
uses THIS directory as its context, so before `agentcore dev` or `agentcore deploy` we copy those modules in as
a co-located `orchestrator/` package. `main.py` already resolves either layout, so
this is the only step that makes the artifact shippable.

    python3 stage_engine.py     # copies ../orchestrator/*.py -> ./orchestrator/

Idempotent. The staged copy is gitignored (it is a build input, not source).
"""

from __future__ import annotations

import ast
import os
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "orchestrator"))
_DST = os.path.join(_HERE, "orchestrator")

# The entrypoints main.py actually reaches. Everything else that ships is DERIVED
# from these by following real imports (see _closure), because a hand-maintained
# module list is a silent deploy break: roles.py and role_graph.py were added to
# the engine and imported at module scope but never added here, so the staged
# bundle raised ModuleNotFoundError inside the CONTAINER while every local test
# stayed green (the repo layout has ../orchestrator importable; the image does
# not). Derive the list instead of remembering it.
_ROOTS = ("engine.py", "chat.py", "presets.py", "reviewer.py")

# Test modules are box-only and never ship.
_SKIP_PREFIXES = ("test_",)


def _local_modules() -> set[str]:
    """Every module name available in the engine source dir."""
    return {f[:-3] for f in os.listdir(_SRC)
            if f.endswith(".py") and not f.startswith("__")}


def _imports_of(path: str, local: set[str]) -> set[str]:
    """The LOCAL sibling modules `path` imports, anywhere in the file.

    Parsed with ast rather than grepped, so `import x`, `from x import y`, and an
    import nested inside a function or try/except are all found. A missed lazy
    import is exactly the kind of gap that only surfaces in the container.
    """
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in local:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import: already inside the package
                continue
            if node.module:
                root = node.module.split(".")[0]
                if root in local:
                    found.add(root)
    return found


def _closure() -> list[str]:
    """Transitive closure of _ROOTS over local imports: the modules to ship."""
    local = _local_modules()
    seen: set[str] = set()
    queue = [r[:-3] for r in _ROOTS]
    while queue:
        mod = queue.pop()
        if mod in seen or mod.startswith(_SKIP_PREFIXES) or mod not in local:
            continue
        seen.add(mod)
        queue.extend(_imports_of(os.path.join(_SRC, mod + ".py"), local))
    return sorted(f"{m}.py" for m in seen)


def stage() -> list[str]:
    if not os.path.isdir(_SRC):
        raise SystemExit(f"engine source not found: {_SRC}")
    os.makedirs(_DST, exist_ok=True)
    open(os.path.join(_DST, "__init__.py"), "w").close()
    modules = _closure()
    for root in _ROOTS:
        if root not in modules:
            raise SystemExit(
                f"stage_engine: {root} not found in {_SRC}; the coordinator "
                "cannot be packaged without it")
    copied = []
    for name in modules:
        src = os.path.join(_SRC, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(_DST, name))
            copied.append(name)
    # The harness steering files (CLAUDE.md / AGENTS.md / .kiro) the engine reads
    # at context-hydration must ship too, or a run fails HARNESS_MISSING in the
    # bundle even though the engine code is present.
    harness_src = os.path.join(_SRC, "harness")
    if os.path.isdir(harness_src):
        harness_dst = os.path.join(_DST, "harness")
        shutil.rmtree(harness_dst, ignore_errors=True)
        shutil.copytree(harness_src, harness_dst)
        copied.append("harness/")
    # Nothing else ships. There is no sample module and no grading contract to bundle:
    # the request is whatever the attendee types, and the acceptance check is authored
    # per run by the validator role inside its own container.
    return copied


if __name__ == "__main__":
    names = stage()
    print(f"staged {len(names)} engine inputs into {_DST}/")
    for n in names:
        print(f"  + {n}")
