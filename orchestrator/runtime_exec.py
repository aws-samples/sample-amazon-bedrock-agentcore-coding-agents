"""Dispatch a role to its coding-agent deployed on AgentCore Runtime.

This is the shipped, real-only producer: it runs a role's coding-agent CLI INSIDE
the role's deployed AgentCore Runtime container, over the AgentCore command shell
(``AgentCoreRuntimeClient.open_shell`` → ``ShellSession``). There is no in-process
CLI runner on the shipped path; a role's CLI only ever runs in its deployed
Runtime, never on the orchestrator box:

  1. open a WebSocket shell on the role's runtime (SigV4, the server-side path);
  2. download the exact tracked checkout archive and expand it on Runtime local
     disk;
  3. run the role's native headless CLI with the Bedrock env set inline;
  4. exclude dependency/cache directories and upload one result archive;
  5. capture STDOUT between sentinels so prompt echo and ANSI noise never pollute
     the transcript.

The role prompts are identical to the in-process path; only WHERE the CLI runs
changes. A missing runtime, a nonzero exit, or a missing/empty artifact raises
``RoleExecutionError``; the run fails loud, it never degrades to a local build.

Why a sync wrapper around an async SDK: the engine drives roles on worker
threads. ``run_in_runtime`` owns its own event loop per call (``asyncio.run`` in
a private thread is unsafe to nest, so we run the coroutine on a fresh loop),
keeping the engine's threading model unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
import uuid
from typing import Any, Callable

import roles as _roles

# Strip the VT/ANSI noise the PTY interleaves with output so sentinel lines
# compare cleanly. In order: OSC (ESC ] … BEL/ST, set-title etc.); CSI (ESC [,
# including private-mode markers ``<=>?`` before the params and intermediate
# bytes, e.g. ``ESC[>4m`` / ``ESC[?2004l``); the single-char Fe escapes
# (ESC 7, ESC 8, ESC = , ESC ( B …); BEL; and the remaining lone control bytes
# (keeping TAB and newline).
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*?(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FE_RE = re.compile(r"\x1b[\x20-\x2f]*[0-9@-_a-z=>]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # keep \t (\x09) and \n (\x0a)


def _clean(text: str) -> str:
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    text = _FE_RE.sub("", text)
    return _CTRL_RE.sub("", text)

# Sentinels delimit the CLI output and the artifact read-back inside the one
# shell transcript, so capture is exact regardless of prompt echo / ANSI control.
_RUN_BEGIN = "__ROLE_RUN_BEGIN__"
_RUN_END = "__ROLE_RUN_END__"

# Directories a build creates that are not source. They are reproducible from the
# manifest the agent wrote and never cross the Runtime exchange boundary.
#
# `dist` and `build` are deliberately absent. Either can be an agent-authored
# deliverable, so the engine cannot classify those names as caches.
_TREE_EXCLUDES = ("node_modules", "__pycache__", ".git", ".venv", "venv",
                  ".pytest_cache", ".ruff_cache", ".mypy_cache", ".next",
                  ".cache")

# Per-role execution facts (the Bedrock env each CLI needs, the telemetry env that
# makes it emit, and the headless invocation itself) all come from the role
# REGISTRY (``roles.py``), which declares them once. They used to be three parallel
# dicts keyed by agent id here, which is three places to forget a role.
#
# Telemetry note (Lab 3): every agent image runs an OTel collector sidecar on
# 127.0.0.1:4318 (started at boot by entrypoint.sh); a role's telemetry_env makes
# its CLI emit to it. Enabling emission is only half the story: WHO ran it comes
# from identity.to_otel_env() (the Lab 3 seam) merged in _build_command.


def _role(agent_id: str) -> "_roles.Role":
    """The registry entry for a dispatch target, or a loud failure. A role that is
    registered but NOT on the served roster still resolves here: the roster decides
    what is offered, while this is the executor being asked to run a specific id."""
    try:
        return _roles.get(agent_id)
    except _roles.UnknownRole:
        raise RoleExecutionError(f"unknown agent: {agent_id}") from None


def _cli_invocation(agent_id: str, prompt_var: str, model: str, workdir: str) -> str:
    """The headless CLI command for one agent, run DIRECTLY (not via /app/run.sh,
    which ``cd``s to ``$HOME`` and would move the artifact off the run workspace).

    ``prompt_var`` is a shell variable name holding the (already-safely-assigned)
    prompt, so the prompt text never needs re-quoting here. The command template and
    the default model are the role's own (registry), so each CLI's standard headless
    one-shot form lives with the role that needs it. ``workdir`` is the run workspace
    the caller has already cd'd into; some CLIs need it PASSED EXPLICITLY because
    they anchor their project at the nearest git root rather than process cwd.
    """
    return _role(agent_id).command(prompt_var, model, workdir)


class RoleExecutionError(RuntimeError):
    """A role's runtime dispatch failed (no runtime, nonzero exit, missing artifact)."""


class ModelQuotaError(RoleExecutionError):
    """The CLI reached its model but the account's daily token allowance is spent."""


_DAILY_MODEL_QUOTA_RE = re.compile(
    r"too many tokens per day|daily (?:token )?(?:allowance|limit|quota).*(?:exceed|spent)",
    re.IGNORECASE,
)


def model_quota_exhausted(output: str) -> bool:
    """Recognize the daily-limit error Claude Code can print while exiting zero."""
    return bool(_DAILY_MODEL_QUOTA_RE.search(output or ""))


def region_for(runtime_arn: str, fallback: str | None = None) -> str:
    """The region a runtime call MUST use: the one in the runtime's own ARN.

    An AgentCore ARN carries its region (``arn:aws:bedrock-agentcore:<region>:...``)
    and ``open_shell`` REJECTS a client whose region differs, so the ARN is the only
    authoritative source. A caller-supplied default is a guess, and a hardcoded
    default is a guess that is silently wrong in every other region: this file used
    to default to ``us-west-2``, and on a us-east-1 event box every workspace
    read-back raised "ARN region does not match client region" and was reported to
    the attendee as their agent having written nothing.

    Falls back (only for a non-ARN target, e.g. the local ``agentcore dev`` URI) to
    the caller's value, then the ambient region, and never to a literal region."""
    parts = (runtime_arn or "").split(":")
    if len(parts) > 3 and parts[3]:
        return parts[3]
    return (fallback or os.environ.get("WORKSHOP_BEDROCK_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION") or "")


def _client(region: str):
    # Lazy import: the SDK is only needed when actually dispatching to a runtime,
    # mirroring llm.py / executor.py. Keeps unit tests import-light.
    from bedrock_agentcore.runtime import AgentCoreRuntimeClient  # noqa: PLC0415
    return AgentCoreRuntimeClient(region=region)


def dispatch_env(agent_id: str, run_subdir: str) -> dict[str, str]:
    """The telemetry + identity + correlation env for a dispatched build.

    The bounded headless shell receives it at launch so every build emits
    attributed telemetry through the collector sidecar.
    """
    env = dict(_role(agent_id).telemetry_env)
    try:
        from identity_baggage import get_current_identity
        identity = get_current_identity()
        if identity is not None and not identity.is_anonymous():
            env.update(identity.to_otel_env())
    except Exception:
        pass
    run_id = run_subdir.split("/", 1)[0]
    _corr = f"run.id={run_id},agent.id={agent_id}"
    _existing_res = env.get("OTEL_RESOURCE_ATTRIBUTES", "")
    env["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"{_existing_res},{_corr}" if _existing_res else _corr)
    return env


def _build_command(agent_id: str, prompt: str, run_subdir: str,
                   artifact_rel: str | None, model: str, region: str,
                   nonce: str, archive_uri: str | None = None,
                   skills_uri: str | None = None) -> str:
    """The one shell line dispatched into the runtime.

    Downloads the exact Git checkout archive, expands it on Runtime local disk,
    runs the agent there, then atomically uploads one result archive. No checkout
    is copied file by file through the S3 Files NFS surface.

    The PTY echoes the whole command line back before running it, so the literal
    sentinel strings would appear in the echo as well as in the real output. To
    capture exactly, the sentinels are assembled at run time from a per-call
    ``nonce`` held in shell variables: the command ECHO shows ``$B1``/``$E1``
    (the variable names), while only the EXECUTED ``echo "$B1"`` emits the
    expanded nonce value, so a search for the value matches real output only.
    ``set -o pipefail`` is intentionally NOT used: the artifact read-back must
    run regardless, and the captured exit code reflects the CLI itself.
    """
    # A fresh local path per turn prevents files from a failed/previous shell
    # leaking into this attempt. The stable S3 object is the continuity boundary.
    workdir = f"/tmp/workshop-{nonce}"
    source_archive = f"/tmp/workshop-source-{nonce}.tar.gz"
    result_archive = f"/tmp/workshop-result-{nonce}.tar.gz"
    archive_uri = archive_uri or f"s3://workshop-runtime-exchange/{run_subdir}.tar.gz"
    # Every role uses the runtime's own region: opencode/claude/kiro all call
    # plain Bedrock there (no mantle/us-east-2 special case).
    cli_region = region
    role = _role(agent_id)
    env = {"AWS_REGION": cli_region, "AWS_DEFAULT_REGION": cli_region,
           **role.env, **role.telemetry_env}
    # A CLI that reads its model from the environment says so in the registry
    # (model_env), so an override reaches it without this file knowing which CLI.
    if role.model_env and model:
        env[role.model_env] = model
    # Propagate authenticated run attribution metadata into the runtime.
    identity = None
    try:
        from identity_baggage import get_current_identity
        identity = get_current_identity()
        if identity is not None and not identity.is_anonymous():
            env.update(identity.to_env())
            # Lab 3 seam: stamp the run's telemetry with the submitting user.
            # to_otel_env() ships returning {} (the gap attendees find on
            # page 1 and close on page 2); once implemented, every signal the
            # agent emits carries user.id and the per-user cost view works.
            env.update(identity.to_otel_env())
    except Exception:
        identity = None
    # Task correlation: every role of one run carries the same run.id (and its
    # own agent.id), so one Logs Insights query can group a single task's cost
    # across the fleet even though the CLIs cannot join a shared trace tree.
    # Merged (never overwritten) so the identity stamp above survives intact.
    run_id = run_subdir.split("/", 1)[0]
    _corr = f"run.id={run_id},agent.id={agent_id}"
    _existing_res = env.get("OTEL_RESOURCE_ATTRIBUTES", "")
    env["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"{_existing_res},{_corr}" if _existing_res else _corr)
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    cli = _cli_invocation(agent_id, "P", model, workdir)

    # Some providers sign with SigV4 but do NOT walk the AWS credential chain the
    # way boto3 does (opencode's Vercel AI SDK is the case in the served roster): on
    # a runtime such a CLI only has the container role via
    # AWS_CONTAINER_CREDENTIALS_FULL_URI / IMDS, which it leaves unresolved, so it
    # errors "SigV4 authentication requires AWS credentials". Claude Code
    # (CLAUDE_CODE_USE_BEDROCK) and Kiro resolve the chain fine. A role that needs
    # it declares needs_static_credentials, and we materialize its temporary keys
    # into the static env vars the SDK reads, using the awscli in the image.
    # Fail-soft: if the export cannot run, the CLI still tries the chain.
    cred_prelude = ""
    if role.needs_static_credentials:
        cred_prelude = (
            'eval "$(aws configure export-credentials --format env 2>/dev/null)" '
            '2>/dev/null || true; ')

    # Per-user cost attribution (Stage 3): when a per-user role is wired
    # (PERUSER_ROLE_ARN) and we know the user, run the agent's CLI under a session
    # named for that user, so its Bedrock calls are logged as the user rather than
    # the shared runtime role. The session-name assumption itself lives in
    # peruser.assume_as_user (attendees build it in Stage 3); it returns "" until
    # then, so the default dispatch is unchanged and runs as the runtime role.
    peruser_prefix = ""
    _peruser_role = os.environ.get("PERUSER_ROLE_ARN", "")
    if _peruser_role and identity is not None and not identity.is_anonymous():
        try:
            from peruser import assume_as_user
            peruser_prefix = assume_as_user(identity.user_id, _peruser_role, cli_region)
        except Exception:
            peruser_prefix = ""
    tar_excludes = " ".join(
        f"--exclude={shlex.quote(name)} --exclude={shlex.quote('*/' + name)}"
        for name in _TREE_EXCLUDES
    )
    steering = role.steering_file.replace("\\", "/")
    steering_source = f"$HOME/{steering}"
    steering_target = os.path.join(workdir, *steering.split("/"))
    steering_parent = os.path.dirname(steering_target)
    skill_setup = ""
    if skills_uri:
        skill_archive = f"/tmp/workshop-skills-{nonce}.tar.gz"
        skill_setup = (
            f"if aws s3 cp {shlex.quote(skills_uri)} "
            f"{shlex.quote(skill_archive)} --region {shlex.quote(cli_region)} "
            "--only-show-errors 2>/dev/null; then "
            f"tar --no-same-owner --no-same-permissions --touch -xzf "
            f"{shlex.quote(skill_archive)} -C {shlex.quote(workdir)}; "
            f"rm -f {shlex.quote(skill_archive)}; "
            "fi; "
        )

    # The prompt is held in shell var $P (assigned once, safely quoted) so the CLI
    # line stays clean and the command echo never collides with our sentinels.
    # Sentinels are emitted via $B1..$E1 vars for the same reason.
    return (
        f"P={shlex.quote(prompt)}; "
        f"B1={_RUN_BEGIN}-{nonce}; E1={_RUN_END}-{nonce}; "
        f'echo "$B1"; '
        f"rm -rf {shlex.quote(workdir)} "
        f"{shlex.quote(source_archive)} {shlex.quote(result_archive)}; "
        f"mkdir -p {shlex.quote(workdir)}; "
        f"aws s3 cp {shlex.quote(archive_uri)} "
        f"{shlex.quote(source_archive)} --region {shlex.quote(cli_region)} "
        "--only-show-errors; "
        f"__hydrate_rc=$?; "
        f"if [ $__hydrate_rc -eq 0 ]; then "
        f"tar --no-same-owner --no-same-permissions --touch -xzf "
        f"{shlex.quote(source_archive)} -C {shlex.quote(workdir)}; "
        f"__hydrate_rc=$?; fi; "
        f"rm -f {shlex.quote(source_archive)}; "
        f"if [ $__hydrate_rc -ne 0 ]; then echo \"$E1\"; "
        f"exit $__hydrate_rc; fi; "
        f"{skill_setup}"
        f"mkdir -p {shlex.quote(steering_parent)}; "
        f"if test -f {steering_source}; then "
        f"cp {steering_source} {shlex.quote(steering_target)}; fi; "
        f"cd {shlex.quote(workdir)}; "
        f"("
        f"{peruser_prefix}"
        f"{cred_prelude}"
        f"{env_prefix} {cli}"
        f"); "
        f"__rc=$?; "
        f"tar -C {shlex.quote(workdir)} {tar_excludes} "
        f"-czf {shlex.quote(result_archive)} .; "
        f"__pack_rc=$?; "
        f"if [ $__pack_rc -eq 0 ]; then "
        f"aws s3 cp {shlex.quote(result_archive)} "
        f"{shlex.quote(archive_uri)} --region {shlex.quote(cli_region)} "
        "--only-show-errors; __pack_rc=$?; fi; "
        f"rm -f {shlex.quote(result_archive)}; "
        f'echo "$E1"; '
        f"if [ $__rc -ne 0 ]; then exit $__rc; fi; "
        f"exit $__pack_rc\n"
    )


async def _drive_shell(runtime_arn: str, command: str, region: str,
                       on_line: Callable[[str], None] | None,
                       timeout_s: float, session_id: str) -> dict[str, Any]:
    """Open the shell, send the command, capture STDOUT until CLOSE/STATUS."""
    from bedrock_agentcore.runtime.shell import ShellChannel  # noqa: PLC0415
    client = _client(region)
    shell_id = str(uuid.uuid4())
    out: list[str] = []
    exit_code: int | None = None
    deadline = time.monotonic() + timeout_s

    async with client.open_shell(runtime_arn=runtime_arn, session_id=session_id,
                                 shell_id=shell_id) as shell:
        # asyncio.wait_for on EACH frame, not a check inside the loop body. The
        # deadline used to be tested only after a frame arrived, so a shell that
        # connected and then went silent blocked forever: `async for` simply never
        # yielded, the check never ran, and the timeout was unreachable. Observed
        # live twice on one box (a 600s dispatch sat 9m02s, a 180s probe 4m32s,
        # both with zero output) until killed by hand. A run that hangs reports no
        # verdict at all, which is worse than any red gate, so the wall clock -- not
        # the peer -- has to decide when to give up.
        await asyncio.wait_for(shell.send(command),
                               timeout=max(1.0, deadline - time.monotonic()))
        frames = shell.__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RoleExecutionError(
                    f"ROLE_EXECUTION_ERROR: runtime dispatch exceeded {timeout_s:.0f}s")
            try:
                frame = await asyncio.wait_for(frames.__anext__(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                # Silence past the deadline is the SAME failure as an overlong run,
                # and it must be reported, never waited out.
                raise RoleExecutionError(
                    f"ROLE_EXECUTION_ERROR: runtime dispatch exceeded "
                    f"{timeout_s:.0f}s with no further output from the shell "
                    f"(the session stopped sending frames)") from exc
            except StopAsyncIteration:
                break
            ch = frame.channel
            if ch == ShellChannel.STDOUT:
                text = frame.text
                out.append(text)
                if on_line:
                    for line in text.splitlines():
                        on_line(line)
            elif ch == ShellChannel.STDERR:
                out.append(frame.text)
            elif ch == ShellChannel.STATUS:
                exit_code = _exit_from_status(frame)
                break
            elif ch == ShellChannel.CLOSE:
                break
    return {"raw": "".join(out), "exit": exit_code if exit_code is not None else 0,
            "session_id": session_id}


def _exit_from_status(frame: Any) -> int:
    """Best-effort exit code from a STATUS frame (shape varies by SDK build)."""
    for attr in ("exit_code", "exitCode"):
        v = getattr(frame, attr, None)
        if isinstance(v, int):
            return v
    try:
        payload = frame.payload
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        if isinstance(payload, str) and payload.strip():
            data = json.loads(payload)
            for k in ("exitCode", "exit_code", "status"):
                if isinstance(data.get(k), int):
                    return data[k]
    except Exception:  # noqa: BLE001 (status parsing is best-effort)
        pass
    return 0


def _slice(raw: str, begin: str, end: str) -> str:
    """Text strictly between the ``begin`` and ``end`` sentinels.

    The command echo prints the sentinel values mid-line (inside the assignment
    and the ``echo`` arguments); the EXECUTED ``echo`` prints each sentinel alone
    on its own line. So we match a sentinel only when it stands as its own line
    (optionally CR-terminated); that uniquely selects the real output and never
    the echo. Lines are split on CR or LF (the PTY emits CRLF)."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    bi = ei = -1
    for i, ln in enumerate(lines):
        s = _clean(ln).strip()
        if s == begin and bi == -1:
            bi = i
        elif s == end and bi != -1:
            ei = i
            break
    if bi == -1 or ei == -1:
        return ""
    return "\n".join(_clean(ln) for ln in lines[bi + 1:ei]).strip("\n")


def _run_in_local_dev(dev_url: str, agent_id: str, prompt: str, run_subdir: str,
                      artifact_rel: str | None, model: str,
                      on_line: Callable[[str], None] | None,
                      timeout_s: float) -> dict[str, Any]:
    """TESTING dispatch: POST the prompt to a local ``agentcore dev`` endpoint's
    ``/invocations`` and read the artifact from the shared local workspace.

    ``agentcore dev`` serves the role's agent over HTTP on localhost against the
    same ``/mnt/s3files`` the deployed runtime would use, so the contract matches
    the shell path: drive the agent, then read the artifact file it wrote. Same
    fail-loud rules: a transport error or a missing/empty artifact raises.
    """
    import os
    import urllib.error
    import urllib.request

    url = dev_url.rstrip("/")
    if not url.endswith("/invocations"):
        url = url + "/invocations"
    body = json.dumps({"prompt": prompt, "model": model,
                       "run_subdir": run_subdir, "agent_id": agent_id}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    transcript_parts: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            for raw in resp:  # the dev server streams SSE-ish lines; relay them
                # Clean PER LINE, not once at the end: `on_line` is what the engine
                # collects into the `tail:` of a role-failure message, so cleaning the
                # assembled transcript afterwards leaves the caller's copy raw. A real
                # opencode failure arrived wrapped in colour codes, and the diagnosis
                # ("AWS SigV4 requires credentials") was present but unreadable, with
                # truncation landing mid-escape-sequence.
                line = _clean(raw.decode("utf-8", "replace"))
                transcript_parts.append(line)
                if on_line:
                    on_line(line.rstrip("\n"))
    except (urllib.error.URLError, OSError) as exc:
        raise RoleExecutionError(
            f"ROLE_EXECUTION_ERROR: {agent_id} local dev dispatch to {url} "
            f"failed: {exc}") from exc
    transcript = "".join(transcript_parts)  # already cleaned per line above
    dev_exit = _dev_exit_code(transcript)

    # Read the artifact the dev server wrote to the shared local workspace. The
    # mount root is wirable (WORKSHOP_S3FILES_DIR; defaults to /mnt/s3files), and
    # the workdir also resolves off WORKSHOP_REPO_ROOT, mirroring the bundle.
    mnt = os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files")
    repo_root = os.environ.get("WORKSHOP_REPO_ROOT", os.getcwd())

    # Defense-in-depth: only read a candidate that resolves INSIDE its base dir.
    # run_subdir is a server run id or governance-probe/<role> (role gated by the
    # resolve() allowlist) and artifact_rel is a fixed literal, so no traversal
    # string reaches here today; this keeps the read-back contained even if a
    # future caller feeds run_subdir a less-trusted value (py/path-injection).
    def _contained(base: str, *parts: str) -> str | None:
        full = os.path.realpath(os.path.join(base, *parts))
        base_real = os.path.realpath(base)
        return full if (full == base_real
                        or full.startswith(base_real + os.sep)) else None

    if not artifact_rel:
        # Builder dispatch: no named file to wait for; the caller reads the tree.
        return {"exit": dev_exit, "transcript": transcript, "artifact": "",
                "session_id": "local-dev"}
    candidates = [c for c in (
        _contained(mnt, run_subdir, artifact_rel),
        _contained(repo_root, ".runs", run_subdir, artifact_rel),
        _contained(repo_root, run_subdir, artifact_rel),
    ) if c]
    artifact = ""
    for _ in range(6):
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as f:
                    artifact = f.read()
                if artifact:
                    break
            except OSError:
                continue
        if artifact:
            break
        time.sleep(2.0)
    if not artifact:
        raise RoleExecutionError(
            f"ROLE_EXECUTION_ERROR: {agent_id} local dev run finished but "
            f"{artifact_rel} is missing/empty under {run_subdir}; "
            f"transcript tail:\n{transcript[-600:]}")
    return {"exit": dev_exit, "transcript": transcript, "artifact": artifact,
            "session_id": "local-dev"}


# The trailer local_dev_runtime.py prints as its last line. Prefixed so it cannot be
# confused with the agent's own output.
_DEV_EXIT_MARKER = "__DEV_RUNTIME_EXIT__"


def _dev_exit_code(transcript: str) -> int:
    """The role CLI's REAL exit code from the local dev seam's transcript.

    This path used to return 0 unconditionally, which quietly disabled the engine's
    exit-code guard for the whole local seam. A live run had opencode fail on missing
    SigV4 credentials (exit 1, nothing written) and the engine still marked the role
    ``done`` with a file count borrowed from its teammates, because every role shares
    one workspace and the only remaining check was "the tree is not empty".

    An ABSENT marker returns 0 on purpose: it means the dev server predates the
    trailer, and inventing a failure for an old server would be worse than the status
    quo. A present marker is authoritative.
    """
    for line in reversed((transcript or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith(_DEV_EXIT_MARKER):
            try:
                return int(stripped[len(_DEV_EXIT_MARKER):].strip())
            except ValueError:
                return 0
    return 0


def _dispatch_once(runtime_arn: str, agent_id: str, prompt: str, run_subdir: str,
                   artifact_rel: str | None, model: str, region: str,
                   on_line: Callable[[str], None] | None,
                   timeout_s: float) -> dict[str, Any]:
    """One shell dispatch of the role's CLI; returns ``{exit, transcript, raw}``.
    The artifact read-back is done by the caller after a successful exit."""
    nonce = uuid.uuid4().hex[:12]
    import runtime_stage  # noqa: PLC0415
    archive_uri = runtime_stage.archive_uri(run_subdir, region)
    run_id = run_subdir.replace("\\", "/").split("/", 1)[0]
    skills_uri = runtime_stage.skills_archive_uri(run_id, agent_id, region)
    # runtimeSessionId must be >= 33 chars (AgentCore command-shell constraint);
    # a uuid4 hex is 32, so prefix it to clear the floor deterministically.
    session_id = "rex-" + uuid.uuid4().hex + uuid.uuid4().hex[:4]
    command = _build_command(
        agent_id, prompt, run_subdir, artifact_rel, model, region, nonce,
        archive_uri=archive_uri, skills_uri=skills_uri)
    # asyncio.run needs no running loop; the engine calls this from a worker
    # thread that has none, so run directly.
    result = asyncio.run(_drive_shell(runtime_arn, command, region, on_line,
                                      timeout_s, session_id))
    transcript = _slice(result["raw"], f"{_RUN_BEGIN}-{nonce}", f"{_RUN_END}-{nonce}")
    return {"exit": result["exit"], "transcript": transcript,
            "session_id": result["session_id"]}


def _read_artifact_from_runtime(runtime_arn: str, run_subdir: str,
                                artifact_rel: str | None, region: str) -> str:
    """Read a named file from the role's atomically uploaded result archive."""
    if not artifact_rel:
        return ""
    tree = read_tree_from_runtime(runtime_arn, run_subdir, ".", region)
    data = tree.get(artifact_rel.replace("\\", "/"))
    return data.decode("utf-8", errors="replace") if data is not None else ""


def list_tree_in_runtime(runtime_arn: str, run_subdir: str,
                         region: str | None = None) -> str:
    """List the atomically uploaded result archive without opening a Runtime shell."""
    try:
        import runtime_stage  # noqa: PLC0415
        return runtime_stage.list_archive(
            run_subdir, region_for(runtime_arn, region))
    except Exception:  # noqa: BLE001 (a probe that cannot run proves nothing)
        return ""


def read_tree_from_runtime(runtime_arn: str, run_subdir: str, tree_rel: str,
                           region: str | None = None) -> dict[str, bytes]:
    """Read source bytes from one S3 object uploaded at the end of the role turn."""
    import runtime_stage  # noqa: PLC0415
    tree = runtime_stage.read_archive(
        run_subdir, region_for(runtime_arn, region))
    normalized = (tree_rel or ".").replace("\\", "/").strip("/")
    if normalized in ("", "."):
        return tree
    prefix = normalized + "/"
    return {
        path[len(prefix):]: data
        for path, data in tree.items()
        if path.startswith(prefix)
    }


def clone_runtime_tree(runtime_arn: str, source_subdir: str, dest_subdir: str,
                       region: str | None = None) -> None:
    """Clone a tracked checkout with one S3 server-side object copy."""
    for label, value in (("source", source_subdir), ("destination", dest_subdir)):
        if (not value or os.path.isabs(value)
                or ".." in value.replace("\\", "/").split("/")):
            raise RoleExecutionError(
                f"ROLE_EXECUTION_ERROR: unsafe {label} Runtime subdirectory")
    try:
        import runtime_stage  # noqa: PLC0415
        runtime_stage.clone_archive(
            source_subdir, dest_subdir, region_for(runtime_arn, region))
    except Exception as exc:
        raise RoleExecutionError(
            "ROLE_EXECUTION_ERROR: the coordinator could not clone the tracked "
            f"checkout archive into {dest_subdir}: {exc}") from exc


def run_in_runtime(runtime_arn: str, agent_id: str, prompt: str, run_subdir: str,
                   artifact_rel: str | None, model: str, region: str | None = None,
                   on_line: Callable[[str], None] | None = None,
                   timeout_s: float = 600.0) -> dict[str, Any]:
    """Run ``agent_id``'s CLI inside its deployed runtime and read the artifact
    it wrote. Returns ``{exit, transcript, artifact, session_id}``.

    Every orchestrated role uses a fresh bounded command shell and its native
    headless CLI. The tracked-source archive exchange requires this boundary:
    the turn downloads one immutable checkout archive, works on Runtime-local
    disk, and atomically uploads one result archive before the shell exits.
    Manually opened console terminals remain a separate interactive surface.

    Raises ``RoleExecutionError`` on a nonzero exit or a missing/empty artifact:
    the same fail-loud contract the engine's ``_read_artifact`` enforced locally.

    SAME-PROVIDER RESILIENCE (legacy, now dormant): an OpenAI-on-Bedrock model
    could be de-registered or have a transient outage, surfaced as a nonzero exit
    with a model-down signature. ``llm.openai_sibling`` returns a healthy sibling
    ONLY for an ``openai.*`` model id, so this retry fires only for that provider.
    The frontend role now runs opencode on a Bedrock Claude model, so
    ``openai_sibling`` returns None and this block is a no-op for it; it stays in
    place for any future ``openai.*`` dispatch and is harmless otherwise.

    TESTING SEAM: when ``runtime_arn`` is a local dev URI (``http(s)://…``, what
    ``agentcore dev`` serves), dispatch over HTTP to its ``/invocations`` instead
    of the command shell, so the orchestrator can be exercised end to end against
    a locally-running role WITHOUT a deployed runtime. This is the ONLY non-shell
    producer and it is gated strictly on the URI shape (never reachable for a real
    ARN), so it cannot become a silent local fallback.
    """
    if runtime_arn.startswith("http://") or runtime_arn.startswith("https://"):
        return _run_in_local_dev(runtime_arn, agent_id, prompt, run_subdir,
                                 artifact_rel, model, on_line, timeout_s)

    # The AgentCore client AND the dispatched command must use the RUNTIME's own
    # region (region_for: parsed from the ARN), never a caller default. Otherwise
    # open_shell raises a region mismatch on any runtime not in the default region
    # and the container never runs.
    region = region_for(runtime_arn, region)

    import llm  # noqa: PLC0415 (lazy; only the dispatch path needs alias/fallback)

    run = _dispatch_once(runtime_arn, agent_id, prompt, run_subdir, artifact_rel,
                         model, region, on_line, timeout_s)
    transcript = run["transcript"]
    session_id = run["session_id"]
    # Claude Code currently exits 0 for this API rejection. Treat the transcript as
    # authoritative for the one known false-zero case, before an empty checkout is
    # mislabeled as work the builder chose not to do.
    if model_quota_exhausted(transcript):
        raise ModelQuotaError(
            f"MODEL_QUOTA_EXHAUSTED: {agent_id} could not start because the "
            f"account's daily token allowance for {model} is exhausted; "
            f"transcript tail:\n{transcript[-600:]}")
    if run["exit"] != 0:
        sibling = llm.openai_sibling(model)
        if sibling and llm.cli_model_is_down(transcript):
            if on_line:
                on_line(f"[{agent_id}] model {model} is down "
                        f"(de-registered or backend outage); retrying once on {sibling}")
            model = sibling
            run = _dispatch_once(runtime_arn, agent_id, prompt, run_subdir,
                                 artifact_rel, model, region, on_line, timeout_s)
            transcript = run["transcript"]
            session_id = run["session_id"]
    if run["exit"] != 0:
        raise RoleExecutionError(
            f"ROLE_EXECUTION_ERROR: {agent_id} CLI exited {run['exit']} in its "
            f"runtime; transcript tail:\n{transcript[-600:]}")

    # A NAMED artifact is read back in a SEPARATE session, with retries; S3Files
    # write-back can lag a beat behind the CLI's own "file written" return. Only the
    # validator's authored check is named: builders decide their own files, so their
    # output is read as a whole tree by the caller, and "wrote nothing at all" is the
    # failure signal there (engine._require_work) rather than a missing filename.
    artifact = ""
    if artifact_rel:
        artifact = _read_artifact_from_runtime(runtime_arn, run_subdir,
                                               artifact_rel, region)
        if not artifact:
            raise RoleExecutionError(
                f"ROLE_EXECUTION_ERROR: {agent_id} finished but {artifact_rel} is "
                f"missing/empty in the runtime after retries; transcript tail:\n{transcript[-600:]}")
    return {"exit": run["exit"], "transcript": transcript, "artifact": artifact,
            "session_id": session_id}
