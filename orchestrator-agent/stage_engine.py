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

import os
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "orchestrator"))
_DST = os.path.join(_HERE, "orchestrator")

# The engine modules the agent's tools call. Tests and __pycache__ are skipped:
# the bundle ships runtime code only. This must list EVERY module engine.py (and
# its imports) pulls in, or the staged bundle fails to import on the runtime.
# The shipped engine produces artifacts by dispatching each role to its deployed
# Runtime (runtime_exec/runtime_stage), so those ship; there is no in-process
# CLI/Strands producer to bundle.
_MODULES = ("engine.py", "presets.py", "reviewer.py", "llm.py", "chat.py",
            "runtime_exec.py", "runtime_stage.py", "runtime_config.py",
            "github.py", "harness_config.py", "executor.py", "policy.py",
            "identity_baggage.py", "peruser.py")


def stage() -> list[str]:
    if not os.path.isdir(_SRC):
        raise SystemExit(f"engine source not found: {_SRC}")
    os.makedirs(_DST, exist_ok=True)
    open(os.path.join(_DST, "__init__.py"), "w").close()
    copied = []
    for name in _MODULES:
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
