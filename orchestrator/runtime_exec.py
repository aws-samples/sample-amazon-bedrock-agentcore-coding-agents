"""Dispatch a role to its coding-agent deployed on AgentCore Runtime.

This is the shipped, real-only producer: it runs a role's coding-agent CLI INSIDE
the role's deployed AgentCore Runtime container, over the AgentCore command shell
(``AgentCoreRuntimeClient.open_shell`` → ``ShellSession``). There is no in-process
CLI runner on the shipped path; a role's CLI only ever runs in its deployed
Runtime, never on the orchestrator box:

  1. open a WebSocket shell on the role's runtime (SigV4, the server-side path);
  2. ``cd`` into the run's workspace on the shared ``/mnt/s3files`` mount and run
     the agent's launcher (``/app/run.sh --print '<prompt>'``) headless, with the
     Bedrock env set inline;
  3. capture only STDOUT, delimited by sentinels so the prompt echo and ANSI
     noise never pollute the transcript or the artifact;
  4. read the artifact the CLI wrote (``cat`` over the same shell; S3Files is a
     managed filesystem, not a transparent ``s3://`` prefix, so the file is read
     back through the runtime, never via ``s3:GetObject``).

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
_ART_BEGIN = "__ARTIFACT_BEGIN__"
_ART_END = "__ARTIFACT_END__"

# The Bedrock env each CLI needs, set inline in the dispatched command because a
# fresh login shell does not inherit the container's PID-1 / Dockerfile ENV.
# All roles use the runtime's own region: opencode (the frontend) talks to plain
# Bedrock, so there is no mantle/us-east-2 special case anymore.
_ROLE_ENV: dict[str, dict[str, str]] = {
    "claude-code": {"CLAUDE_CODE_USE_BEDROCK": "1", "DISABLE_AUTOUPDATER": "1"},
    # The validator is a second Claude Code, so it uses the same Bedrock env.
    "claude-code-validator": {"CLAUDE_CODE_USE_BEDROCK": "1", "DISABLE_AUTOUPDATER": "1"},
    "opencode": {},
    "kiro": {},
}

# Telemetry-enable env per role (Lab 3). Every agent image runs an OTel
# collector sidecar on 127.0.0.1:4318 (started at boot by entrypoint.sh);
# these vars make the agent CLI emit to it. Claude Code exports metrics and
# log events over OTLP; opencode needs OTEL_BSP_SCHEDULE_DELAY=1 because a
# short-lived CLI exits before the default 5s batch flush and its spans would
# silently drop. Enabling emission is only half the story: WHO ran it comes
# from identity.to_otel_env() (the Lab 3 seam) merged in _build_command.
_ROLE_TELEMETRY_ENV: dict[str, dict[str, str]] = {
    "claude-code": {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_METRIC_EXPORT_INTERVAL": "5000",
        "OTEL_LOGS_EXPORT_INTERVAL": "2000",
    },
    "claude-code-validator": {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_METRIC_EXPORT_INTERVAL": "5000",
        "OTEL_LOGS_EXPORT_INTERVAL": "2000",
    },
    # opencode's exporter is switched on in its config file
    # (experimental.openTelemetry, written by configure_opencode.py); the env
    # here is the endpoint + the short-lived-process flush fix.
    "opencode": {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_BSP_SCHEDULE_DELAY": "1",
    },
    "kiro": {},
}


def _cli_invocation(agent_id: str, prompt_var: str, model: str, workdir: str) -> str:
    """The headless CLI command for one agent, run DIRECTLY (not via /app/run.sh,
    which ``cd``s to ``$HOME`` and would move the artifact off the run workspace).

    ``prompt_var`` is a shell variable name holding the (already-safely-assigned)
    prompt, so the prompt text never needs re-quoting here. The flags are each
    CLI's standard headless one-shot form (``--print`` / ``run`` / ``--no-interactive``).
    ``workdir`` is the run workspace the caller has already cd'd into; opencode needs
    it PASSED EXPLICITLY because it anchors its project at the nearest git root, not
    the process cwd, and ``/mnt/s3files/<run>`` is not a git repo.
    """
    if agent_id in ("claude-code", "claude-code-validator"):
        # The validator is a second Claude Code, so it runs the same headless CLI.
        m = model or "us.anthropic.claude-opus-4-6-v1"
        return (f'claude --dangerously-skip-permissions --print --max-turns 50 '
                f'--model {shlex.quote(m)} "${prompt_var}"')
    if agent_id == "opencode":
        m = model or "amazon-bedrock/us.anthropic.claude-sonnet-4-6"
        # opencode 1.17.x has NO --dangerously-skip-permissions flag (passing it
        # hangs/errors). --dir pins the project to the run workspace (it otherwise
        # walks up to the nearest git root and writes chatbot.html off-workspace, so
        # the artifact read-back finds nothing); --auto auto-approves permissions
        # (else it auto-REJECTS reading the staged module under <run>-skill and aborts).
        return (f'opencode run --dir {shlex.quote(workdir)} --auto '
                f'-m {shlex.quote(m)} "${prompt_var}"')
    if agent_id == "kiro":
        return f'kiro-cli chat --no-interactive --trust-all-tools "${prompt_var}"'
    raise RoleExecutionError(f"unknown agent: {agent_id}")


class RoleExecutionError(RuntimeError):
    """A role's runtime dispatch failed (no runtime, nonzero exit, missing artifact)."""


def _client(region: str):
    # Lazy import: the SDK is only needed when actually dispatching to a runtime,
    # mirroring llm.py / executor.py. Keeps unit tests import-light.
    from bedrock_agentcore.runtime import AgentCoreRuntimeClient  # noqa: PLC0415
    return AgentCoreRuntimeClient(region=region)


def _build_command(agent_id: str, prompt: str, run_subdir: str, artifact_rel: str,
                   model: str, region: str, nonce: str) -> str:
    """The one shell line dispatched into the runtime.

    Sets the Bedrock env inline, cd's into the run's workspace on the shared
    mount, runs the agent launcher headless on the prompt, then prints the
    artifact between sentinels.

    The PTY echoes the whole command line back before running it, so the literal
    sentinel strings would appear in the echo as well as in the real output. To
    capture exactly, the sentinels are assembled at run time from a per-call
    ``nonce`` held in shell variables: the command ECHO shows ``$B1``/``$E1``
    (the variable names), while only the EXECUTED ``echo "$B1"`` emits the
    expanded nonce value, so a search for the value matches real output only.
    ``set -o pipefail`` is intentionally NOT used: the artifact read-back must
    run regardless, and the captured exit code reflects the CLI itself.
    """
    workdir = f"/mnt/s3files/{run_subdir}"
    # Every role uses the runtime's own region: opencode/claude/kiro all call
    # plain Bedrock there (no mantle/us-east-2 special case).
    cli_region = region
    env = {"AWS_REGION": cli_region, "AWS_DEFAULT_REGION": cli_region,
           **_ROLE_ENV.get(agent_id, {}),
           **_ROLE_TELEMETRY_ENV.get(agent_id, {})}
    if agent_id in ("claude-code", "claude-code-validator") and model:
        env["ANTHROPIC_MODEL"] = model
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
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    cli = _cli_invocation(agent_id, "P", model, workdir)

    # opencode's Bedrock provider (Vercel AI SDK, Node) signs with SigV4 but does
    # NOT walk the AWS credential chain the way boto3 does: on a runtime it only has
    # the container role via AWS_CONTAINER_CREDENTIALS_FULL_URI / IMDS, which the SDK
    # leaves unresolved, so it errors "SigV4 authentication requires AWS credentials".
    # claude-code (CLAUDE_CODE_USE_BEDROCK) and kiro resolve the chain fine. So for
    # opencode ONLY, materialize the role's temporary keys into the static env vars
    # the SDK reads, using the awscli that ships in the image. Fail-soft: if the
    # export cannot run the CLI still tries the chain (unchanged behaviour).
    cred_prelude = ""
    if agent_id == "opencode":
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
    # The prompt is held in shell var $P (assigned once, safely quoted) so the CLI
    # line stays clean and the command echo never collides with our sentinels.
    # Sentinels are emitted via $B1..$E1 vars for the same reason. The artifact is
    # read back in a SEPARATE shell session (see _read_artifact_from_runtime): the
    # S3Files mount has brief write-back latency, so a `cat` in this same pipeline
    # right after the CLI exits can miss a file that is in fact written.
    return (
        f"P={shlex.quote(prompt)}; "
        f"B1={_RUN_BEGIN}-{nonce}; E1={_RUN_END}-{nonce}; "
        f'echo "$B1"; '
        f"{peruser_prefix}"
        f"mkdir -p {shlex.quote(workdir)} 2>/dev/null; "
        f"cd {shlex.quote(workdir)} 2>/dev/null || cd /tmp; "
        f"{cred_prelude}"
        f"{env_prefix} {cli}; "
        f"__rc=$?; sync 2>/dev/null; "
        f'echo "$E1"; '
        f"exit $__rc\n"
    )


def _build_read_command(run_subdir: str, artifact_rel: str, nonce: str) -> str:
    """A separate shell command that reads the artifact back, sentinel-delimited.
    Run after the build (own session) so S3Files write-back has settled."""
    path = f"/mnt/s3files/{run_subdir}/{artifact_rel}"
    # A leading newline before the END marker guarantees it starts on its own line
    # even when the artifact has no trailing newline (else `cat` output and the
    # marker share a line and the slice can't find the boundary). _slice strips the
    # one extra newline. printf '\n' is portable.
    return (
        f"B2={_ART_BEGIN}-{nonce}; E2={_ART_END}-{nonce}; "
        f'echo "$B2"; '
        f"cat {shlex.quote(path)} 2>/dev/null; "
        f"printf '\\n'; "
        f'echo "$E2"; exit 0\n'
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
        await shell.send(command)
        async for frame in shell:
            if time.monotonic() > deadline:
                raise RoleExecutionError(
                    f"ROLE_EXECUTION_ERROR: runtime dispatch exceeded {timeout_s:.0f}s")
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
                      artifact_rel: str, model: str,
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
                line = raw.decode("utf-8", "replace")
                transcript_parts.append(line)
                if on_line:
                    on_line(line.rstrip("\n"))
    except (urllib.error.URLError, OSError) as exc:
        raise RoleExecutionError(
            f"ROLE_EXECUTION_ERROR: {agent_id} local dev dispatch to {url} "
            f"failed: {exc}") from exc
    transcript = "".join(transcript_parts)

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
    return {"exit": 0, "transcript": transcript, "artifact": artifact,
            "session_id": "local-dev"}


def _dispatch_once(runtime_arn: str, agent_id: str, prompt: str, run_subdir: str,
                   artifact_rel: str, model: str, region: str,
                   on_line: Callable[[str], None] | None,
                   timeout_s: float) -> dict[str, Any]:
    """One shell dispatch of the role's CLI; returns ``{exit, transcript, raw}``.
    The artifact read-back is done by the caller after a successful exit."""
    nonce = uuid.uuid4().hex[:12]
    # runtimeSessionId must be >= 33 chars (AgentCore command-shell constraint);
    # a uuid4 hex is 32, so prefix it to clear the floor deterministically.
    session_id = "rex-" + uuid.uuid4().hex + uuid.uuid4().hex[:4]
    command = _build_command(agent_id, prompt, run_subdir, artifact_rel, model, region, nonce)
    # asyncio.run needs no running loop; the engine calls this from a worker
    # thread that has none, so run directly.
    result = asyncio.run(_drive_shell(runtime_arn, command, region, on_line,
                                      timeout_s, session_id))
    transcript = _slice(result["raw"], f"{_RUN_BEGIN}-{nonce}", f"{_RUN_END}-{nonce}")
    return {"exit": result["exit"], "transcript": transcript,
            "session_id": result["session_id"]}


def _run_in_live_pty(session: Any, agent_id: str, prompt: str, run_subdir: str,
                     artifact_rel: str, region: str,
                     on_line: Callable[[str], None] | None,
                     timeout_s: float) -> dict[str, Any]:
    """Drive the agent's LIVE interactive TUI (the SAME PTY the console's Agents
    page streams) for one dispatch turn, then read the artifact back.

    This is the MUXED path: one PTY, many subscribers. The human watches the real
    Claude Code / opencode / kiro TUI work the turn live on the Agents page (and
    the run view mirrors it), the orchestrator types the turn and reads the same
    screen, and the human can keep typing into the same session afterwards.

    The turn is framed exactly like a human: an ``[orchestrator]`` banner, the
    prompt pasted (bracketed paste) into the TUI's input box, Enter as its own
    keystroke. Done = the screen goes quiet (a working TUI repaints its status
    line continuously, so buffer-idle is the turn boundary). The artifact is then
    read back over a separate one-shot command shell on the SAME runtime -- the
    PTY stays clean for the human, and the read is exact (sentinel-delimited),
    never scraped from TUI paint.
    """
    session.busy = True
    try:
        # A just-opened session (the dispatch opened it itself) must be READY --
        # WebSocket up, TUI banner painted -- before keystrokes land; typing into
        # a connecting shell is a silent drop. Fail loud, never dispatch blind.
        if not session.wait_ready(timeout_s=120.0):
            raise RoleExecutionError(
                f"ROLE_EXECUTION_ERROR: {agent_id} live session "
                f"{session.session_id} never became ready (runtime shell did not "
                "connect/paint)")
        t0_len = len(session.buffer)
        session.emit_banner(f"run {run_subdir}: {prompt[:120]}"
                            + ("..." if len(prompt) > 120 else ""))
        # The TUI's cwd is $HOME (run.sh cd's there), not the run workspace, so the
        # prompt itself must pin absolute paths; the engine's prompts already name
        # /mnt/s3files/<run>/ paths explicitly. Tell the agent where to work first.
        session.send_turn(
            f"Work in /mnt/s3files/{run_subdir} (create it if needed; cd there "
            f"first).\n\n{prompt}")
        finished = session.wait_turn_idle(quiet_s=8.0, timeout_s=timeout_s)
        transcript = _clean(session.buffer[t0_len:])
        if on_line:
            for line in transcript.splitlines():
                on_line(line)
        if not finished:
            raise RoleExecutionError(
                f"ROLE_EXECUTION_ERROR: {agent_id} live session still busy after "
                f"{timeout_s:.0f}s; transcript tail:\n{transcript[-600:]}")
    finally:
        session.busy = False

    # Artifact read-back: a separate one-shot command shell on the same runtime
    # (same S3Files mount), retried for write-back lag; identical to the headless
    # path's read so the fail-loud contract is one code path.
    artifact = _read_artifact_from_runtime(session.runtime_arn, run_subdir,
                                           artifact_rel, region)
    if not artifact:
        raise RoleExecutionError(
            f"ROLE_EXECUTION_ERROR: {agent_id} live turn ended but {artifact_rel} "
            f"is missing/empty in the runtime; transcript tail:\n{transcript[-600:]}")
    return {"exit": 0, "transcript": transcript, "artifact": artifact,
            "session_id": session.session_id, "live_session": True}


def _read_artifact_from_runtime(runtime_arn: str, run_subdir: str,
                                artifact_rel: str, region: str) -> str:
    """Read /mnt/s3files/<run>/<artifact> over a fresh one-shot command shell,
    sentinel-delimited, retrying for S3Files write-back lag. Returns "" when the
    file never appears (the caller decides how loud to fail)."""
    artifact = ""
    for attempt in range(6):
        read_nonce = uuid.uuid4().hex[:12]
        read_cmd = _build_read_command(run_subdir, artifact_rel, read_nonce)
        read_sid = "rexrd-" + uuid.uuid4().hex + uuid.uuid4().hex[:4]
        rr = asyncio.run(_drive_shell(runtime_arn, read_cmd, region, None,
                                      60.0, read_sid))
        artifact = _slice(rr["raw"], f"{_ART_BEGIN}-{read_nonce}",
                          f"{_ART_END}-{read_nonce}")
        if artifact:
            break
        time.sleep(2.0 * (attempt + 1))
    return artifact


def _live_session_for(agent_id: str, runtime_arn: str) -> Any | None:
    """The live console PTY this dispatch should drive, if the console is hosting
    one for the SAME runtime this role is wired to. Import is lazy and optional:
    runtime_shell lives in interactive-api (the console); a coordinator deployed
    without the console has no live-PTY surface and uses the headless shell."""
    try:
        import runtime_shell  # noqa: PLC0415 (console-only surface)
    except Exception:
        return None
    try:
        s = runtime_shell.ensure_dispatch_session(agent_id,
                                                  instance_arn=runtime_arn)
    except Exception:
        return None
    # Only drive a session on the SAME runtime the router picked; a session on a
    # different fleet instance would build in another microVM's mount namespace.
    if s is None or s.runtime_arn != runtime_arn:
        return None
    return s


def run_in_runtime(runtime_arn: str, agent_id: str, prompt: str, run_subdir: str,
                   artifact_rel: str, model: str, region: str = "us-west-2",
                   on_line: Callable[[str], None] | None = None,
                   timeout_s: float = 600.0) -> dict[str, Any]:
    """Run ``agent_id``'s CLI inside its deployed runtime and read the artifact
    it wrote. Returns ``{exit, transcript, artifact, session_id}``.

    TWO dispatch surfaces, one contract:

      * LIVE PTY (preferred when the console hosts one): the dispatch drives the
        agent's real interactive TUI -- the same WebSocket shell session the
        Agents page streams -- so the human, the orchestrator, and the run view
        all watch ONE live session (server fan-out), and the human can type into
        it before and after the turn.
      * HEADLESS one-shot (always available): a fresh command shell runs the CLI
        ``--print``-style. This is the path when no console PTY exists (CLI-only
        submit, coordinator deployed without the console) and the safety net if
        the live surface cannot host this dispatch.

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

    _arn_parts0 = runtime_arn.split(":")
    _live_region = _arn_parts0[3] if len(_arn_parts0) > 3 and _arn_parts0[3] else region
    live = _live_session_for(agent_id, runtime_arn)
    if live is not None:
        return _run_in_live_pty(live, agent_id, prompt, run_subdir, artifact_rel,
                                _live_region, on_line, timeout_s)

    # The AgentCore client AND the dispatched command must use the RUNTIME's own
    # region, parsed from its ARN (arn:aws:bedrock-agentcore:<region>:...), never a
    # caller default. Otherwise open_shell raises a region mismatch on any runtime
    # not in the default region and the container never runs.
    _arn_parts = runtime_arn.split(":")
    if len(_arn_parts) > 3 and _arn_parts[3]:
        region = _arn_parts[3]

    import llm  # noqa: PLC0415 (lazy; only the dispatch path needs alias/fallback)

    run = _dispatch_once(runtime_arn, agent_id, prompt, run_subdir, artifact_rel,
                         model, region, on_line, timeout_s)
    transcript = run["transcript"]
    session_id = run["session_id"]
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

    # Read the artifact back in a SEPARATE session, with retries; S3Files
    # write-back can lag a beat behind the CLI's own "file written" return.
    artifact = _read_artifact_from_runtime(runtime_arn, run_subdir,
                                           artifact_rel, region)
    if not artifact:
        raise RoleExecutionError(
            f"ROLE_EXECUTION_ERROR: {agent_id} finished but {artifact_rel} is "
            f"missing/empty in the runtime after retries; transcript tail:\n{transcript[-600:]}")
    return {"exit": run["exit"], "transcript": transcript, "artifact": artifact,
            "session_id": session_id}
