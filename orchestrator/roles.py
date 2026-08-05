"""The role registry: the ONE declarative description of the agent roster.

Every per-role fact the harness needs lives here, exactly once: what a role is
called, whether it MAKES or CHECKS, which steering file steers it, which CLI runs
it headless, which env that CLI needs, and how it is telemetered. Before this
module the same roster was restated in eight places (``presets``, ``chat``,
``runtime_config``, ``runtime_exec``, ``engine.AGENTS``, ``harness_config``,
``configure_deploy``, and the console's ``environments.ts``), so adding a role
meant eight edits and any missed one was a silent bug.

Two properties follow, and both are the point:

  * **Adding or swapping a role is ONE entry.** Nothing downstream counts roles,
    names them in a literal, or assumes how many there are. There is no "three"
    anywhere: the roster's size is whatever the registry says.
  * **The roster is configurable at runtime.** ``WORKSHOP_ROLES`` selects which
    registered roles are served (see ``roster()``), so an operator can run the
    workshop with a different or smaller team without touching code. Kept-but-
    hidden roles (Codex, the Claude Code validator) stay REGISTERED and off the
    default roster, which is exactly what makes them a restore path rather than
    dead code.

What this module deliberately does NOT know is the WORK. It carries no protocol,
no filename a role must produce, no expected value, and no shape of a correct
answer. Validation is agentic: the checker writes the acceptance check and its
real exit code decides. The registry only says who is at the table.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

# --------------------------------------------------------------------- kinds
# A role either MAKES the work or CHECKS it. This is the structural half of
# maker-is-never-checker: routing reads these, so it can guarantee a build has
# both without knowing any role id. `capability` is the finer-grained "what kind
# of maker" (a backend vs a frontend), which is what a preset selects on.
BUILDER = "builder"
CHECKER = "checker"

# The code repo root (this module lives in orchestrator/), used only to resolve the
# real on-disk steering layout for `Role.steering_path`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Role:
    """One agent role: who it is, how it is steered, and how it is run."""

    id: str                      # the dispatch key, everywhere (AGENTCORE_RUNTIME_<ID>)
    label: str                   # the human name of the CLI behind it
    kind: str                    # BUILDER (makes) or CHECKER (decides)
    capability: str              # "backend" | "frontend" | "validator": what a preset asks for
    role_name: str               # the job it plays, shown in the run view
    description: str             # default "what this agent does" (an operator may override)
    steering_file: str           # the filename the CLI reads from its cwd
    harness_dir: str             # coding-agents/<harness_dir> and harness/<harness_dir>
    cli: str                     # headless one-shot template: {model} {workdir} {prompt}
    default_model: str           # the model the CLI uses when nothing overrides it
    skills: tuple[str, ...] = () # harness skills installed into its container
    env: dict[str, str] = field(default_factory=dict)            # env its CLI needs
    telemetry_env: dict[str, str] = field(default_factory=dict)  # Lab 3: emit signals
    model_env: str = ""          # env var carrying the model id, if the CLI reads one
    credential: str = "bedrock-native"
    # opencode's Bedrock provider (Vercel AI SDK) signs with SigV4 but does not walk
    # the AWS credential chain, so its keys must be materialized before the CLI runs.
    needs_static_credentials: bool = False
    # --- vault-brokered credential (credential == "api-key") ------------------
    # A role whose CLI authenticates with a VENDOR key, not with AWS credentials,
    # cannot get it from the AWS chain: it is fetched at dispatch time from the
    # AgentCore Identity Token Vault and exported into the CLI's own env var, in
    # memory only. These three declare WHERE it lives and WHAT reads it, so the
    # dispatch does not have to know which CLI it is running.
    api_key_env: str = ""        # the env var this CLI reads its key from
    vault_workload: str = ""     # workload identity that owns the credential provider
    vault_provider: str = ""     # api-key credential provider holding the key
    # The provisioning side (``kiro_config``, from the console Settings pane) and the
    # dispatch side must never name two different providers, so both resolve the
    # names through the SAME optional operator overrides, at call time.
    vault_workload_env: str = ""
    vault_provider_env: str = ""
    # Off the served roster by default: registered so it stays a restore path.
    hidden: bool = False

    @property
    def dispatch_tool(self) -> str:
        """The orchestrator's tool name for dispatching this role."""
        return f"dispatch_{self.capability}"

    @property
    def brokers_api_key(self) -> bool:
        """True when this role's CLI needs a VAULT-BROKERED vendor key to run.

        The dispatch path runs a role's CLI directly, never ``/app/run.sh``, so a
        role that authenticates with a vendor key gets NOTHING from the AWS
        credential chain and must have its key materialized explicitly. Declared
        rather than inferred from ``credential`` alone, because a role can be
        ``api-key`` and still have no vault wiring yet (Codex), and silently
        exporting an empty variable for it would look like success.
        """
        return bool(self.credential == "api-key" and self.api_key_env
                    and self.vault_workload and self.vault_provider)

    def vault_names(self) -> tuple[str, str]:
        """(workload identity, credential provider) for this role's brokered key.

        Resolved at CALL time through the role's declared operator-override
        variables, so the provisioning side and the dispatch side cannot drift onto
        two different providers when an operator renames one.
        """
        workload = self.vault_workload
        provider = self.vault_provider
        if self.vault_workload_env:
            workload = os.environ.get(self.vault_workload_env, "").strip() or workload
        if self.vault_provider_env:
            provider = os.environ.get(self.vault_provider_env, "").strip() or provider
        return workload, provider

    @property
    def steering_path(self) -> str:
        """Where this role's steering lives in its harness dir, as it is on disk.

        Published to attendees through ``GET /api/agents``, so it must be a path they
        can actually open. A Docker build context cannot COPY from outside itself, so a
        role whose steering lives at a NESTED path upstream is staged FLAT in its
        harness dir and the Dockerfile puts it back under the nested location inside
        the image (Kiro's ``.kiro/steering/validator.md`` is staged as
        ``steering/agent.md``). Resolve what is really there rather than assuming the
        upstream layout, otherwise this names a file that does not exist.
        """
        role_dir = f"coding-agents/{self.harness_dir}"
        direct = os.path.join(role_dir, self.steering_file)
        if os.path.isfile(os.path.join(_REPO_ROOT, direct)):
            return direct
        flat = os.path.join(role_dir, "steering", "agent.md")
        if os.path.isfile(os.path.join(_REPO_ROOT, flat)):
            return flat
        return direct   # nothing staged yet: name the canonical location

    @property
    def local_steering_path(self) -> str:
        """Where the engine reads this role's steering from (relative to orchestrator/)."""
        return f"harness/{self.harness_dir}/{self.steering_file}"

    @property
    def install(self) -> str:
        """The command an attendee runs to build and deploy this role by hand."""
        return f"cd coding-agents/{self.harness_dir} && ./setup.sh && python deploy.py"

    def command(self, prompt_var: str, model: str, workdir: str) -> str:
        """The headless CLI invocation for this role.

        ``prompt_var`` is a shell variable name already holding the prompt, so the
        text is never re-quoted here. ``model`` falls back to this role's default.
        """
        return self.cli.format(model=shlex.quote(model or self.default_model),
                               workdir=shlex.quote(workdir),
                               prompt=f'"${prompt_var}"')


# Claude Code reads Bedrock through its own switch, and exports metrics + log
# events over OTLP to the collector sidecar every agent image runs on
# 127.0.0.1:4318. Both Claude Code roles share this, so it is written once.
_CLAUDE_ENV = {"CLAUDE_CODE_USE_BEDROCK": "1", "DISABLE_AUTOUPDATER": "1"}
_CLAUDE_TELEMETRY = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
    "OTEL_METRIC_EXPORT_INTERVAL": "5000",
    "OTEL_LOGS_EXPORT_INTERVAL": "2000",
}
# Reasoning EFFORT, high by default. These agents are given real projects (several
# features, real persistence, real input rejection) and their work is graded by an
# executable another agent wrote, so the cost of thinking less is a red gate and a
# wasted re-implement round, not a cheaper run. Verified against the installed CLIs
# rather than assumed: `claude --effort` accepts low|medium|high|xhigh|max and warns
# and IGNORES anything else, so a bad value degrades to the default instead of failing
# the run. Wirable for an operator who wants to trade quality for latency or spend.
_CLAUDE_EFFORT = os.environ.get("WORKSHOP_CLAUDE_EFFORT", "xhigh").strip()
# opencode calls the same idea a model VARIANT and its accepted values are
# provider-specific (its help names high, max, minimal), so it gets its own variable.
_OPENCODE_VARIANT = os.environ.get("WORKSHOP_OPENCODE_VARIANT", "high").strip()

# The headless one-shot form of each CLI. Placeholders are filled by Role.command.
_CLAUDE_CLI = ("claude --dangerously-skip-permissions --print --max-turns 50 "
               + (f"--effort {_CLAUDE_EFFORT} " if _CLAUDE_EFFORT else "")
               + "--model {model} {prompt}")

# Each role's default model is WIRABLE, because an account without Opus access must
# be able to run the workshop by exporting one variable rather than editing code.
# These are the same names the Stage 1 harness configurators read, so the shelf, the
# deployed image, and the dispatch all name one model.
_CLAUDE_MODEL = os.environ.get("WORKSHOP_CLAUDE_MODEL", "us.anthropic.claude-opus-4-6-v1")
_OPENCODE_MODEL = os.environ.get(
    "WORKSHOP_OPENCODE_MODEL", "amazon-bedrock/us.anthropic.claude-sonnet-4-6")
_CODEX_MODEL = os.environ.get("WORKSHOP_CODEX_MODEL", "openai.gpt-5.5")
# Kiro names models in its OWN vendor namespace, not as Bedrock inference profiles, so
# this cannot share _CLAUDE_MODEL. Verified against a live Runtime with
# `kiro-cli chat --list-models`: claude-opus-5 / claude-sonnet-5 / claude-opus-4.8 /
# auto / ... . Kept wirable like every other role's model, and read by BOTH halves of
# the flow (this registry for the Lab 2 dispatch, run.sh for the Lab 1 session).
_KIRO_MODEL = os.environ.get("WORKSHOP_KIRO_MODEL", "claude-opus-5")


# --------------------------------------------------------------------- registry
# The roster, declared once. `hidden=True` keeps a role registered but off the
# served team: it is reachable by WORKSHOP_ROLES (the restore path) and never
# appears as a wireable agent or a dispatch target by default.
REGISTRY: tuple[Role, ...] = (
    Role(
        id="claude-code",
        label="Claude Code",
        kind=BUILDER,
        capability="backend",
        role_name="backend-builder",
        description="Backend builder (Claude Code): writes the service side of "
                    "whatever was asked, in whatever language the task calls for.",
        steering_file="CLAUDE.md",
        harness_dir="claude-code",
        cli=_CLAUDE_CLI,
        default_model=_CLAUDE_MODEL,
        skills=("configure-claude-code-backend", "harness-setup"),
        env=_CLAUDE_ENV,
        telemetry_env=_CLAUDE_TELEMETRY,
        model_env="ANTHROPIC_MODEL",
    ),
    Role(
        id="opencode",
        label="opencode",
        kind=BUILDER,
        capability="frontend",
        role_name="frontend-builder",
        description="Frontend builder (opencode): builds the part a person "
                    "interacts with, on Amazon Bedrock.",
        steering_file="AGENTS.md",
        harness_dir="opencode",
        # --dir pins the project to the run workspace (opencode otherwise walks up
        # to the nearest git root and writes off-workspace, so the artifact read-back
        # finds nothing). --auto auto-approves permissions; it auto-REJECTS otherwise
        # and aborts. There is NO --dangerously-skip-permissions flag in 1.17.x.
        cli=("opencode run --dir {workdir} --auto "
             + (f"--variant {_OPENCODE_VARIANT} " if _OPENCODE_VARIANT else "")
             + "-m {model} {prompt}"),
        default_model=_OPENCODE_MODEL,
        skills=("configure-opencode-frontend", "vercel-deploy"),
        # opencode's exporter is switched on in its config file
        # (experimental.openTelemetry); the env here is the endpoint plus the
        # short-lived-process flush fix (a one-shot CLI exits before the default
        # 5s batch flush, and its spans would silently drop).
        telemetry_env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                       "OTEL_BSP_SCHEDULE_DELAY": "1"},
        credential="runtime-iam",
        needs_static_credentials=True,
    ),
    Role(
        id="kiro",
        label="Kiro",
        kind=CHECKER,
        capability="validator",
        role_name="validator",
        description="Validator (Kiro): decides what acceptable means for this "
                    "task, writes the check, and never edits the work. "
                    "Authenticates with your own ksk_ key through Token Vault.",
        steering_file=os.path.join(".kiro", "steering", "validator.md"),
        harness_dir="kiro",
        # kiro-cli DOES take `--model`, and the checker is pinned to the strongest
        # model on the roster rather than left on the `auto` router: `auto` picks by
        # task for "optimal usage", so a gate decision could be routed to a cheap
        # model, and the checker is the one role whose judgement the whole workshop
        # rests on. The Lab 1 interactive session gets the same value from run.sh
        # writing chat.defaultModel; this is the Lab 2 dispatch half.
        #
        # The id is kiro-cli's OWN vendor name, NOT a Bedrock inference profile.
        # Verified with `kiro-cli chat --list-models` in a live Runtime:
        # `claude-opus-5` (2.20x credits, 1M context). `--model`/`--effort` were
        # verified in `kiro-cli chat --help` there too; an earlier comment here
        # claimed this CLI had no model flag, which was wrong.
        cli="kiro-cli chat --no-interactive --trust-all-tools --model {model} {prompt}",
        default_model=_KIRO_MODEL,
        skills=("configure-kiro-validator",),
        credential="api-key",
        # Kiro is the one served role whose CLI authenticates with a VENDOR key
        # rather than with AWS credentials. Lab 1's interactive session gets it from
        # ``/app/run.sh``, but the Lab 2 dispatch runs ``kiro-cli`` directly (run.sh
        # cd's to $HOME and would move the artifact off the run workspace), so the
        # key has to be brokered by the dispatch itself. These three names are the
        # SAME pair ``coding-agents/kiro/setup.sh``, ``orchestrator/kiro_config.py``
        # and ``run.sh`` use, declared here once so no caller hardcodes them.
        api_key_env="KIRO_API_KEY",
        vault_workload="kiro-coding-agent",
        vault_provider="kiro-api-key",
        vault_workload_env="WORKSHOP_KIRO_WORKLOAD",
        vault_provider_env="WORKSHOP_KIRO_PROVIDER",
    ),
    # ---- registered, off the served roster: the restore paths ----------------
    # Both stay REGISTERED (not deleted) so each remains a one-variable restore
    # path, reachable with WORKSHOP_ROLES alone:
    #
    #   * The Claude Code validator is the BEDROCK-NATIVE, NO-KEY checker. It runs
    #     the same container as the backend builder, steered by its own
    #     acceptance-contract ``CLAUDE.md``, and needs no API key and no Token
    #     Vault. Restore it (``WORKSHOP_ROLES=claude-code,opencode,claude-code-
    #     validator``) for any account without a Kiro subscription, which is the
    #     only thing the served Kiro checker needs that Bedrock alone cannot give.
    #     Kiro is servable BECAUSE the event now provisions that subscription:
    #     ``static/aws/central-account.yaml`` stands up IAM Identity Center in the
    #     one Workshop Studio central account and, off the lifecycle-notification
    #     bus, gives each team an Identity Center user with a Kiro / Amazon Q
    #     Developer Pro subscription. The only remaining attendee step is minting
    #     their own ``ksk_`` key at app.kiro.dev (the Prerequisites page "Get Your
    #     Kiro API Key") and pasting it; the harness fetches it at session start
    #     through AgentCore Identity / Token Vault, in memory only, never as a
    #     runtime env var.
    #   * Codex is DISABLED at events because the GPT-5.x models it needs are not
    #     available on a Workshop Studio account (the entitlement is allowlist-gated
    #     and returns 401), so opencode on native Bedrock plays the frontend. Do not
    #     put it back on the roster until GPT is actually entitled there: naming it
    #     in WORKSHOP_ROLES on an event account produces a role that 401s on every
    #     dispatch.
    Role(
        id="claude-code-validator",
        label="Claude Code",
        kind=CHECKER,
        capability="validator",
        role_name="validator",
        description="Validator (Claude Code): decides what acceptable means for "
                    "this task, writes the check, and never edits the work.",
        steering_file="CLAUDE.md",
        harness_dir="claude-code-validator",
        cli=_CLAUDE_CLI,
        default_model=_CLAUDE_MODEL,
        skills=("configure-claude-code-validator",),
        env=_CLAUDE_ENV,
        telemetry_env=_CLAUDE_TELEMETRY,
        model_env="ANTHROPIC_MODEL",
        hidden=True,
    ),
    Role(
        id="codex",
        label="Codex",
        kind=BUILDER,
        capability="frontend",
        role_name="frontend-builder",
        description="Frontend builder (Codex): DISABLED at events, because the "
                    "GPT-5.x models it needs are not available on a Workshop "
                    "Studio account. Off the served roster.",
        steering_file="AGENTS.md",
        harness_dir="codex",
        cli="codex exec --dangerously-bypass-approvals-and-sandbox -m {model} {prompt}",
        default_model=_CODEX_MODEL,
        skills=(),
        credential="api-key",
        hidden=True,
    ),
)

BY_ID: dict[str, Role] = {r.id: r for r in REGISTRY}

# The orchestrator is wirable like a role but is NOT a dispatch target: it is the
# thing that dispatches. It has no harness directory and no steering file.
ORCHESTRATOR = "orchestrator"
ORCHESTRATOR_DESCRIPTION = (
    "Coordinates the build: routes a request, opens one pull request per role, and "
    "runs the executable gate and review on each one before it merges.")


class UnknownRole(KeyError):
    """A role id that is not registered. Raised rather than guessed at."""


def get(role_id: str) -> Role:
    """The registered role, or raise. Never a nearest match."""
    try:
        return BY_ID[role_id]
    except KeyError:
        raise UnknownRole(role_id) from None


def roster() -> tuple[Role, ...]:
    """The roles this deployment SERVES, in dispatch order (makers, then checker).

    ``WORKSHOP_ROLES`` overrides the default roster with a comma-separated list of
    registered role ids, which is how an operator runs a different or smaller team
    without a code change (and how a hidden role is restored). An unknown id there
    fails loud: a typo must not silently shrink the team.
    """
    override = os.environ.get("WORKSHOP_ROLES", "").strip()
    if override:
        ids = [part.strip() for part in override.split(",") if part.strip()]
        unknown = [i for i in ids if i not in BY_ID]
        if unknown:
            raise UnknownRole(
                f"WORKSHOP_ROLES names unregistered role(s): {', '.join(unknown)}. "
                f"Registered: {', '.join(BY_ID)}")
        return order([BY_ID[i] for i in ids])
    return order([r for r in REGISTRY if not r.hidden])


def order(roles: list[Role]) -> tuple[Role, ...]:
    """Makers first, checker last: the order the run view reads in, and the order
    the engine's join expects (the checker runs after the work it checks).

    Deduplicated BY ID rather than by object, because a Role carries dicts (its env)
    and so is not hashable; id is the identity that matters anyway."""
    seen: list[Role] = []
    taken: set[str] = set()
    for r in roles:
        if r.id not in taken:
            taken.add(r.id)
            seen.append(r)
    return tuple([r for r in seen if r.kind == BUILDER]
                 + [r for r in seen if r.kind == CHECKER]
                 + [r for r in seen if r.kind not in (BUILDER, CHECKER)])


def roster_ids() -> tuple[str, ...]:
    """The served role ids, in dispatch order."""
    return tuple(r.id for r in roster())


def builders() -> tuple[Role, ...]:
    """The served roles that MAKE. However many there are."""
    return tuple(r for r in roster() if r.kind == BUILDER)


def builder_ids() -> tuple[str, ...]:
    return tuple(r.id for r in builders())


def checkers() -> tuple[Role, ...]:
    """The served roles that DECIDE. Maker is never checker, so these are disjoint
    from ``builders()`` by construction (a role declares one kind)."""
    return tuple(r for r in roster() if r.kind == CHECKER)


def checker_ids() -> tuple[str, ...]:
    return tuple(r.id for r in checkers())


def by_capability(capability: str) -> tuple[str, ...]:
    """The served role ids offering a capability ("backend", "frontend",
    "validator"), in dispatch order. Empty when this roster has none, which is a
    legitimate answer: a preset that needs a capability nobody serves is a
    routing failure the caller reports, not something to substitute for."""
    return tuple(r.id for r in roster() if r.capability == capability)


def maker_capabilities() -> tuple[str, ...]:
    """The MAKER capabilities this roster serves, in dispatch order.

    This is the choice the router hands the model: "which kinds of work does this
    request need?" Derived from the registry, so a roster swap or a smaller team
    changes what can be routed without an edit anywhere else, and nothing counts
    roles or names them in a literal.
    """
    out: list[str] = []
    for r in builders():
        if r.capability not in out:
            out.append(r.capability)
    return tuple(out)


def role_names() -> dict[str, str]:
    """role id -> the job it plays, for the run view."""
    return {r.id: r.role_name for r in roster()}


def descriptions() -> dict[str, str]:
    """role id -> its default description, plus the orchestrator's."""
    out = {ORCHESTRATOR: ORCHESTRATOR_DESCRIPTION}
    out.update({r.id: r.description for r in roster()})
    return out


def wirable_ids() -> tuple[str, ...]:
    """Everything the console can wire a runtime ARN for: the orchestrator plus
    every served role. The orchestrator leads because it is wired first."""
    return (ORCHESTRATOR,) + roster_ids()


def harness_dirs() -> dict[str, str]:
    """role id -> its ``coding-agents/<dir>`` name, for deploy discovery."""
    return {r.id: r.harness_dir for r in roster()}


def public_roster() -> list[dict[str, object]]:
    """The roster for the API, so the console renders the SAME team the engine
    dispatches instead of keeping its own copy. Presentation only: no ARNs, no
    credentials, no env."""
    return [{"role": r.id, "label": r.label, "kind": r.kind,
             "capability": r.capability, "role_name": r.role_name,
             "description": r.description, "steering_file": r.steering_file,
             "model": r.default_model, "credential": r.credential}
            for r in roster()]
