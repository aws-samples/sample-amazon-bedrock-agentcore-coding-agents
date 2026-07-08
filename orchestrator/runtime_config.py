"""Wirable AgentCore runtime config: the deployed ARNs are SET, never hardcoded.

When the orchestrator runs with ``WORKSHOP_EXECUTOR=agentcore`` it dispatches each
role to a coding agent deployed on its own AgentCore Runtime. Those runtime ARNs
are not known until the attendee deploys, so they must be wirable, not baked into
code. This module is that surface, mirroring ``github.py``'s credential ladder:

  1. environment: ``AGENTCORE_RUNTIME_<ROLE>`` (CI / pre-provisioned), or the
     orchestrator's own ``AGENTCORE_RUNTIME_ID`` / ``BEDROCK_AGENTCORE_RUNTIME_ARN``.
  2. the Settings pane: written to a gitignored ``.runs/runtime.local.json``
     (0600), the same place GitHub creds land. Attendees paste the ARN the
     ``agentcore deploy`` output printed, or set it from the terminal.

FLEET, not singletons: a role may have MORE THAN ONE deployed runtime ("3 types"
does not mean exactly 3 instances; the fleet may be 2 Claude Code + 5 Codex + 1
Kiro). So each role wires to a LIST of ARNs: a single string is one instance, a
JSON list (or a comma-separated env value) is a fleet. ``pick(role)`` round-robins
across a role's instances so concurrent runs spread their dispatch over the fleet;
``resolve(role)`` returns the first instance (the single-instance answer the
governance/cost views and presence checks use).

``resolve(role)`` / ``pick(role)`` return None for an unwired role (the executor
fails loud rather than silently running it locally). Nothing here is a secret
(an ARN is not a credential), but we keep the 0600 file discipline anyway so the
config surface is identical to GitHub's and there is one place to look.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)


def _settings_path() -> str:
    """Where the wired ARNs are stored. Resolved at call time from
    ``WORKSHOP_RUNTIME_CONFIG`` (so the location itself is wirable, and tests point
    it at a temp file with a real env var instead of patching internals), else the
    default under the repo's ``.runs``."""
    return os.environ.get(
        "WORKSHOP_RUNTIME_CONFIG",
        os.path.join(_REPO, ".runs", "runtime.local.json"))

# The roles the orchestrator can dispatch (the harness roster) plus the
# orchestrator itself, each independently wirable. The frontend role is
# ``opencode`` (Bedrock Claude Sonnet 4.6): it replaced ``codex`` because the
# GPT-5.x the Codex path needs is allowlist-gated. The codex harness stays in the
# repo (coding-agents/codex/) but is NOT in this roster, so it never appears as a
# wireable agent in the console or as a dispatch target.
ROLES = ("orchestrator", "claude-code", "kiro", "opencode")

# A default one-line "what this agent does" per role, so a freshly wired (or
# auto-discovered) instance carries a meaningful description instead of an empty
# field. An explicit per-instance description the attendee sets always wins; this
# only fills the blank. Matches the role each harness plays in the workshop.
_DEFAULT_DESCRIPTION = {
    "orchestrator": "Coordinates the build: routes a request to the right roles, "
                    "composes their work, runs the acceptance gate, and opens the PR.",
    "claude-code": "Backend builder (Claude Code): wraps the module as a remote MCP "
                   "server that serves the tools over JSON-RPC.",
    "kiro": "Validator (Kiro CLI): owns the acceptance contract that defines when a "
            "build is done.",
    "opencode": "Frontend builder (opencode): builds the thin chatbot UI on top of "
                "the MCP server, on Amazon Bedrock.",
}

# A loose AgentCore runtime ARN / id check: an arn:aws:bedrock-agentcore:... , a
# bare runtime id, OR a local dev endpoint URI (http(s)://…, what `agentcore dev`
# serves) so the orchestrator can be wired against a locally-running role for
# testing WITHOUT a deployed runtime. We validate shape (fail loud on junk), not
# existence. A URI target is dispatched over HTTP instead of the command shell
# (see executor.py / runtime_exec); it is a TESTING seam, documented as such.
_ARN_RE = re.compile(
    r"^(arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[\w.-]+"
    r"|https?://[\w.\-:/]{3,300}"
    r"|[\w.-]{3,200})$")


def is_local_uri(target: str) -> bool:
    """True if a wired runtime target is a local dev endpoint (``http(s)://…``)
    rather than a deployed AgentCore runtime ARN: the testing seam."""
    return target.startswith("http://") or target.startswith("https://")

# Round-robin dispatch cursor per role, so a fleet of N instances spreads load
# across concurrent runs. Thread-safe: the engine dispatches roles on worker
# threads, so a bare int would race.
_RR_LOCK = threading.Lock()
_RR_CURSOR: dict[str, int] = {}


def _env_key(role: str) -> str:
    return "AGENTCORE_RUNTIME_" + role.replace("-", "_").upper()


def _as_list(value: Any) -> list[str]:
    """Normalize a stored runtime value to a list of ARN strings. A single string
    is one instance; a list keeps its string members (a fleet); anything else is
    dropped. Order is preserved (it is the round-robin order)."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def _split_env(value: str) -> list[str]:
    """A role's env var may carry a fleet as a comma-separated list of ARNs."""
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_file() -> dict[str, list[str]]:
    """The wired runtimes from the settings file, each role normalized to a list
    of ARNs (a string member is read as a one-instance fleet)."""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        out: dict[str, list[str]] = {}
        for k, v in data.get("runtimes", {}).items():
            arns = _as_list(v)
            if arns:
                out[k] = arns
        return out
    except (OSError, ValueError):
        return {}


# The harness deploy.py writes coding-agents/<role>/runtime_config.json with the
# real deployed ARN. The event pre-provisions Codex and Kiro at box boot (the CFN
# ProvisionPreBuiltAgents step), so those files exist BEFORE the attendee touches
# Settings. Auto-discovering them here is what makes the console show Codex/Kiro
# as already wired (matching what the content says: "the event already wired its
# Runtime ARN"), the same source of truth the Stage 1 Agents shelf reads. It is
# the LOWEST-priority source: an explicit env var or a Settings entry still wins.
def _coding_agents_dir() -> str:
    """The directory holding each harness's ``<role>/runtime_config.json``.
    Wirable (WORKSHOP_CODING_AGENTS_DIR / WORKSHOP_REPO_ROOT) so tests point it at
    a temp tree and the suite never reads a developer's real deploy state; the
    same resolution the Stage 1 interactive API uses."""
    explicit = os.environ.get("WORKSHOP_CODING_AGENTS_DIR")
    if explicit:
        return explicit
    root = os.environ.get("WORKSHOP_REPO_ROOT")
    if root:
        return os.path.join(root, "coding-agents")
    return os.path.join(_REPO, "coding-agents")


# Only a role that maps to a real harness directory can be auto-discovered; the
# orchestrator itself is not a dispatch target and has no coding-agents/ dir.
_HARNESS_DIRS = {"claude-code": "claude-code", "kiro": "kiro", "opencode": "opencode"}


def _discover_deployed(role: str) -> list[str]:
    """The deployed ARN from ``coding-agents/<role>/runtime_config.json``, or []
    when the harness has not been deployed (no file / no valid ARN). Never raises:
    a missing or malformed file just means "not deployed yet"."""
    sub = _HARNESS_DIRS.get(role)
    if not sub:
        return []
    path = os.path.join(_coding_agents_dir(), sub, "runtime_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            arn = (json.load(f).get("runtime_arn") or "").strip()
    except (OSError, ValueError):
        return []
    return [arn] if arn and _ARN_RE.match(arn) else []


def _load_descriptions() -> dict[str, str]:
    """Free-text descriptions of what each agent does, keyed by INSTANCE ARN (so a
    fleet can describe each instance independently). For back-compat a key that is
    a ROLE name (older single-description-per-role files) is still read. The
    orchestrator reads these to describe its dispatch targets DYNAMICALLY (no
    hardcoded blurb)."""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        out: dict[str, str] = {}
        for k, v in (data.get("descriptions") or {}).items():
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
        return out
    except (OSError, ValueError):
        return {}


def _write_file(runtimes: dict[str, list[str]], descriptions: dict[str, str] | None = None) -> None:
    path = _settings_path()
    runs_dir = os.path.dirname(path)
    os.makedirs(runs_dir, exist_ok=True)
    try:
        os.chmod(runs_dir, 0o700)
    except OSError:
        pass
    # A one-instance role is written back as a bare string (the back-compatible
    # shape every older reader and test expects); a fleet is written as a list.
    serializable: dict[str, Any] = {}
    for role, arns in runtimes.items():
        if not arns:
            continue
        serializable[role] = arns[0] if len(arns) == 1 else list(arns)
    # Preserve existing descriptions unless the caller passed a fresh map.
    descs = descriptions if descriptions is not None else _load_descriptions()
    payload: dict[str, Any] = {"runtimes": serializable}
    if descs:
        payload["descriptions"] = {k: v for k, v in descs.items() if v}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.chmod(path, 0o600)


def describe_arn(arn: str) -> str:
    """The description set for one instance ARN, or '' if none."""
    return _load_descriptions().get(arn.strip(), "")


def describe(role: str) -> str:
    """A representative description for a ROLE: the first wired instance's
    description, falling back to a legacy role-keyed description. '' if none."""
    descs = _load_descriptions()
    for arn, _src in instances(role):
        if descs.get(arn):
            return descs[arn]
    return descs.get(role, "")


def describe_map() -> dict[str, str]:
    """role -> a representative description (first instance's), for the chatbot's
    dynamic agent section. Only roles with a description appear."""
    out: dict[str, str] = {}
    for role in ROLES:
        d = describe(role)
        if d:
            out[role] = d
    return out


def save_description(role: str, arn: str, description: str) -> dict[str, Any]:
    """Set (or clear, with '') the description for ONE instance ARN of a role.
    Persists alongside the ARNs. The role is validated; the ARN must be wired to
    it (so a description always attaches to a real instance)."""
    role, arn = role.strip(), arn.strip()
    if role not in ROLES:
        return {"error": f"unknown role {role!r} (expected one of {', '.join(ROLES)})"}
    wired_arns = [a for a, _ in instances(role)]
    if arn not in wired_arns:
        return {"error": f"ARN not wired to {role}"}
    descs = _load_descriptions()
    description = (description or "").strip()
    if description:
        descs[arn] = description[:500]  # bound the free text
    else:
        descs.pop(arn, None)
    _write_file(_load_file(), descs)
    return status()


def instances(role: str) -> list[tuple[str, str]]:
    """Every wired (arn, source) for a role, down the resolution ladder.

    Ladder (highest priority first), the same vocabulary github.py uses:
      1. ``environment`` : ``AGENTCORE_RUNTIME_<ROLE>`` (a whole override; when
         set, the lower sources are ignored so an operator override is never
         half-merged).
      2. ``settings``    : the ``.runs/runtime.local.json`` the Settings pane /
         terminal writes (an attendee pasted the ARN, or grew a fleet).
      3. ``deployed``    : auto-discovered from the harness's own
         ``coding-agents/<role>/runtime_config.json`` that ``deploy.py`` wrote.
         This is what surfaces the event-pre-provisioned Codex/Kiro as already
         wired without the attendee pasting anything.
    Returns [] for a role with nothing wired anywhere."""
    env = os.environ.get(_env_key(role))
    if env:
        return [(arn, "environment") for arn in _split_env(env)]
    saved = _load_file().get(role, [])
    if saved:
        return [(arn, "settings") for arn in saved]
    return [(arn, "deployed") for arn in _discover_deployed(role)]


def resolve(role: str) -> tuple[str, str] | None:
    """Resolve the FIRST (arn, source) for a role down the ladder, or None if
    unset. The single-instance answer: presence checks and the governance/cost
    views use this; ``pick`` is what load-balances an actual dispatch."""
    hits = instances(role)
    return hits[0] if hits else None


def pick(role: str) -> tuple[str, str] | None:
    """Pick the next (arn, source) for a role, round-robin across its fleet, or
    None if the role is unwired. With one instance this always returns it; with N
    it cycles, so concurrent runs spread their dispatch across the deployed fleet."""
    hits = instances(role)
    if not hits:
        return None
    with _RR_LOCK:
        idx = _RR_CURSOR.get(role, 0) % len(hits)
        _RR_CURSOR[role] = idx + 1
    return hits[idx]


def resolve_map() -> dict[str, str]:
    """Every role with a wired ARN -> its FIRST ARN (env wins over file). The
    back-compatible single-instance map the AgentCoreExecutor's runtime_arns
    mapping and older callers use; ``fleet_map`` is the full per-role list."""
    out: dict[str, str] = {}
    for role in ROLES:
        hit = resolve(role)
        if hit:
            out[role] = hit[0]
    return out


def fleet_map() -> dict[str, list[str]]:
    """Every role with at least one wired ARN -> the full list of its instances."""
    out: dict[str, list[str]] = {}
    for role in ROLES:
        hits = [arn for arn, _ in instances(role)]
        if hits:
            out[role] = hits
    return out


def save_runtime(role: str, arn: str) -> dict[str, Any]:
    """SET one role's deployed runtime to a single ARN, REPLACING any prior fleet
    (the Settings pane / terminal writes this). Validates shape and persists to the
    gitignored 0600 file. Use ``add_runtime`` to grow a fleet instead of replacing."""
    role, arn = role.strip(), arn.strip()
    if role not in ROLES:
        return {"error": f"unknown role {role!r} (expected one of {', '.join(ROLES)})"}
    if not _ARN_RE.match(arn):
        return {"error": "value must be an AgentCore runtime ARN or id"}
    runtimes = _load_file()
    runtimes[role] = [arn]
    _write_file(runtimes)
    return status()


def add_runtime(role: str, arn: str) -> dict[str, Any]:
    """ADD one runtime instance to a role's fleet (keeps the existing instances).

    This is how "3 types" becomes a real fleet: wire a second Codex, a third
    Claude Code. Validates shape, de-dups (adding the same ARN twice is a no-op),
    and persists. Identical to ``save_runtime`` for the first instance of a role."""
    role, arn = role.strip(), arn.strip()
    if role not in ROLES:
        return {"error": f"unknown role {role!r} (expected one of {', '.join(ROLES)})"}
    if not _ARN_RE.match(arn):
        return {"error": "value must be an AgentCore runtime ARN or id"}
    runtimes = _load_file()
    fleet = runtimes.get(role, [])
    if arn not in fleet:
        fleet.append(arn)
    runtimes[role] = fleet
    _write_file(runtimes)
    return status()


def remove_runtime(role: str, arn: str) -> dict[str, Any]:
    """Remove ONE instance from a role's fleet (the per-instance x button). When
    it was the role's last instance the role becomes unwired. Removing an ARN that
    is not wired is a no-op. The role's description is preserved."""
    role, arn = role.strip(), arn.strip()
    if role not in ROLES:
        return {"error": f"unknown role {role!r} (expected one of {', '.join(ROLES)})"}
    runtimes = _load_file()
    fleet = [a for a in runtimes.get(role, []) if a != arn]
    if fleet:
        runtimes[role] = fleet
    else:
        runtimes.pop(role, None)
    _write_file(runtimes)  # preserves descriptions (reloads them)
    return status()


def clear_runtime(role: str | None = None) -> dict[str, Any]:
    """Unwire one role's whole fleet (or all roles when role is None)."""
    if role is None:
        try:
            os.remove(_settings_path())
        except OSError:
            pass
        return status()
    runtimes = _load_file()
    runtimes.pop(role, None)
    if runtimes:
        _write_file(runtimes)
    else:
        try:
            os.remove(_settings_path())
        except OSError:
            pass
    return status()


def status() -> dict[str, Any]:
    """The wiring view for GET /api/runtimes: per role, whether an ARN is wired,
    from where, and HOW MANY instances (the fleet size). The ARN tail is shown
    (not a secret, but keeps the UI tidy).

    Back-compatible shape: ``arn``/``source`` are the FIRST instance's; ``count``
    and ``instances`` (the full list) are additive for the fleet view.

    The shipped engine is real-only, so the executor defaults to ``agentcore``
    (there is no ``local`` executor anymore). ``remote_dispatch`` stays true on the
    real path; a role with no wired ARN fails loud at dispatch time, not here."""
    executor = os.environ.get("WORKSHOP_EXECUTOR", "agentcore").strip().lower() or "agentcore"
    descs = _load_descriptions()
    roles: list[dict[str, Any]] = []
    for role in ROLES:
        hits = instances(role)
        # Each instance carries its OWN description (keyed by ARN); legacy
        # role-keyed descriptions still surface on the first instance, and a role
        # default fills the blank so the card is never description-less.
        default_desc = _DEFAULT_DESCRIPTION.get(role, "")
        inst_list = [{"arn": arn, "source": src,
                      "description": descs.get(arn) or default_desc} for arn, src in hits]
        if inst_list and inst_list[0]["description"] == default_desc and descs.get(role):
            inst_list[0]["description"] = descs[role]
        roles.append({
            "role": role,
            "wired": bool(hits),
            "source": hits[0][1] if hits else None,
            "arn": hits[0][0] if hits else None,
            "count": len(hits),
            "instances": inst_list,
            # role-level representative (first instance's), for back-compat.
            "description": inst_list[0]["description"] if inst_list else "",
        })
    return {"executor": executor, "roles": roles,
            "remote_dispatch": executor == "agentcore"}
