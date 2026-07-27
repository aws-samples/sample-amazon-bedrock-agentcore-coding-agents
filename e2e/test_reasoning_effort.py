"""Reasoning effort is set HIGH by default, on every surface, and stays wirable.

These roles are handed real projects (several features, real persistence, real input
rejection) and their work is graded by an executable another agent wrote. Thinking less
does not buy a cheaper run: it buys a red gate and a wasted re-implement round.

The flag names were verified against the INSTALLED CLIs, not assumed:
  * `claude --effort` accepts low|medium|high|xhigh|max, and WARNS-and-ignores anything
    else ("Unknown --effort value 'bogus' - ignoring it and using the default effort"),
    so a bad value degrades instead of failing a run.
  * opencode calls it a model VARIANT (`--variant`), values provider-specific.
  * opencode has NEVER had `--dangerously-skip-permissions`: it is absent from
    `opencode run --help` in 1.18.x and passing it errors the run. It uses `--auto`.
"""

from __future__ import annotations

import importlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ORCH = os.path.join(_ROOT, "orchestrator")
if _ORCH not in sys.path:
    sys.path.insert(0, _ORCH)

_CLAUDE_EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}
_RUN_SH = {
    "claude-code": os.path.join(_ROOT, "coding-agents", "claude-code", "run.sh"),
    "claude-code-validator": os.path.join(
        _ROOT, "coding-agents", "claude-code-validator", "run.sh"),
    "opencode": os.path.join(_ROOT, "coding-agents", "opencode", "run.sh"),
}


def _roles_with(env: dict):
    for key in ("WORKSHOP_CLAUDE_EFFORT", "WORKSHOP_OPENCODE_VARIANT"):
        os.environ.pop(key, None)
    os.environ.update(env)
    sys.modules.pop("roles", None)
    return importlib.import_module("roles")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_dispatch_defaults_to_high_effort():
    """The ORCHESTRATOR path (roles.py -> the headless CLI line)."""
    try:
        roles = _roles_with({})
        for role in roles.roster():
            cli = role.cli
            if cli.startswith("claude"):
                assert "--effort xhigh" in cli, cli
            elif cli.startswith("opencode"):
                assert "--variant high" in cli, cli
    finally:
        _roles_with({})


def test_effort_is_wirable_both_up_and_off():
    """An operator can raise it, or clear it to fall back to each CLI's own default.
    An empty value must emit NO flag rather than an empty one."""
    try:
        roles = _roles_with({"WORKSHOP_CLAUDE_EFFORT": "max",
                             "WORKSHOP_OPENCODE_VARIANT": "max"})
        clis = [r.cli for r in roles.roster()]
        assert any("--effort max" in c for c in clis), clis
        assert any("--variant max" in c for c in clis), clis

        roles = _roles_with({"WORKSHOP_CLAUDE_EFFORT": "",
                             "WORKSHOP_OPENCODE_VARIANT": ""})
        for c in (r.cli for r in roles.roster()):
            assert "--effort" not in c and "--variant" not in c, c
    finally:
        _roles_with({})


def test_the_default_is_a_value_the_claude_cli_accepts():
    """A value outside the accepted set is silently ignored by the CLI, which would
    make this whole setting a no-op that nothing reports."""
    try:
        roles = _roles_with({})
        for role in roles.roster():
            if not role.cli.startswith("claude"):
                continue
            parts = role.cli.split()
            value = parts[parts.index("--effort") + 1]
            assert value in _CLAUDE_EFFORT_VALUES, (
                f"{role.id} uses --effort {value!r}, which `claude` would warn about "
                f"and IGNORE. Accepted: {sorted(_CLAUDE_EFFORT_VALUES)}")
    finally:
        _roles_with({})


def test_the_lab1_run_sh_path_sets_effort_too():
    """Lab 1 pages 4-6 drive `/app/run.sh` DIRECTLY, never the orchestrator, so the
    registry's flag does not reach them. Both surfaces have to carry it."""
    for role, path in _RUN_SH.items():
        body = _read(path)
        flag = "--variant" if role == "opencode" else "--effort"
        assert flag in body, f"{role}/run.sh never passes {flag}"
        var = ("WORKSHOP_OPENCODE_VARIANT" if role == "opencode"
               else "WORKSHOP_CLAUDE_EFFORT")
        assert var in body, f"{role}/run.sh hardcodes the effort instead of reading {var}"


def test_opencode_never_passes_a_flag_it_does_not_have():
    """`--dangerously-skip-permissions` is absent from `opencode run --help` in 1.18.x
    and passing it ERRORS the run. This shipped in run.sh, so the Lab 1 p5 interactive
    path was broken; the registry already used the correct `--auto`."""
    # Comments are stripped first: the fix's own explanation names the bad flag, and
    # matching prose would make this test fail on wording rather than on behaviour.
    body = _read(_RUN_SH["opencode"])
    code = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    assert "--dangerously-skip-permissions" not in code, (
        "opencode/run.sh passes a flag opencode does not accept; use --auto")
    assert "--auto" in code
    try:
        roles = _roles_with({})
        oc = [r for r in roles.roster() if r.id == "opencode"]
        if oc:
            assert "--dangerously-skip-permissions" not in oc[0].cli
            assert "--auto" in oc[0].cli
    finally:
        _roles_with({})
