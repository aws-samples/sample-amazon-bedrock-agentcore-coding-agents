#!/usr/bin/env python3
"""Where am I in the workshop, and what do I run next? One read-only answer.

    python3 orchestrator/progress.py            # every checkpoint, then NEXT
    python3 orchestrator/progress.py --json     # the same, as JSON

Why this exists. The September 25 survey's lowest scores all described the same
gap: an attendee who fell behind could not tell where they were. Some skipped the
backend deploy the walkthrough went past, some never knew whether their build had
finished, some opened a play URL that could not work, and a handful of individual
problems held up a room that was ready to move on. Every answer was already on the
box, spread across a dozen files and three services, so a facilitator had to ask
for them one at a time while the room waited.

This walks the lab end states IN ORDER against real local state and a few cheap,
read-only AWS reads, prints one line per checkpoint, and ends with ONE next step:
the page and step as the content titles them, and the exact command that page
teaches. An attendee runs it to catch up on their own; a facilitator asks for its
output in chat instead of holding the room.

It is strictly READ-ONLY, which is what makes it safe to run at any moment and to
paste anywhere. It invokes no model, dispatches nothing, opens no Runtime session,
makes no GitHub call (so it does not run ``github.py doctor``, whose last check
writes a disposable branch), and writes no file. It never reads a key, token,
password, or private key: the Kiro key is reported through ``kiro_config.status``,
which returns provider metadata only, and ``github-app.env`` is checked for
existence, never opened. Account ids are masked the way ``diagnose.py`` masks them.
Every check fails soft to ``?`` on its own, and the whole command is bounded to
about fifteen seconds, so a slow or missing service cannot hang it.

It is a reporting tool, never the verdict path: nothing reads it back, and it may
not import ``llm``, ``reviewer``, or the engine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# A laptop has no instance metadata service; do not let the SDK's credential chain
# spend its default retries looking for one. Process-local, and the workshop host
# answers well inside these limits.
os.environ.setdefault("AWS_METADATA_SERVICE_TIMEOUT", "2")
os.environ.setdefault("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")

import run_advice  # noqa: E402
import workshop_urls  # noqa: E402

BUDGET_S = 15.0
REPO_CD = "cd ~/sample-amazon-bedrock-agentcore-coding-agents"

DONE, TODO, UNKNOWN, INFO = "done", "todo", "unknown", "info"
_MARK = {DONE: "✓", TODO: "✗", UNKNOWN: "?", INFO: "·"}

LAB1 = "Lab 1. Build Your Coding Assistant Team"
LAB2 = "Lab 2. Coordinate a Multi-Agent Build"
LAB3 = "Lab 3. Improve and Govern Your Game"
P_WORKSPACE = "Lab 1 > Confirm the Shared Coding Workspace"
P_RUNTIMES = "Lab 1 > Put All Three Agents on Runtime"
P_SHELL = "Lab 1 > Open a Shell Inside a Live Agent"
P_GITHUB = "Lab 2 > Connect GitHub Without Giving Agents Credentials"
P_COORD = "Lab 2 > Deploy the Multi-Agent Coordinator"
P_BUILD = "Lab 2 > Run a Build and Follow It"
P_EVIDENCE = "Lab 2 > Read the Evidence on Each Pull Request"
P_PLAY = "Lab 2 > Play Your Game"
P_STUDIO = "Lab 3 > Improve Your Game in Agent Studio"
AFTER_ALL = ("Lab 3 > Trace the Fix (it reads CloudWatch, which this host check does "
             "not), then the optional pages: Optional: Try team governance, Optional: "
             "Verify in the AWS Console, and Best Practices.")

# Account IDs as they appear in ARNs and ECR hosts; a 12-digit run-ID suffix after
# "_" is not one, so it is left readable.
_ACCOUNT_RE = re.compile(r"(?<![\w])(\d{4})\d{4}(\d{4})(?![\w])")
_RUNTIME_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/([A-Za-z0-9_-]+)$")

# Exactly what the pages teach, copied verbatim so a pasted NEXT block runs as-is.
CMD_STORAGE = [
    "findmnt --mountpoint /mnt/s3files || echo \"NOT MOUNTED: ask a facilitator\"",
    "grep '^INFRA_S3FILES_AP_ARN=' ~/sample-amazon-bedrock-agentcore-coding-agents/coding-agents/infra.config",
]
CMD_NOTE = [
    "printf '# My workspace\\n\\nThis note is shared between the host and Runtime shells.\\n' \\",
    "  > /mnt/s3files/notes.md",
    "cat /mnt/s3files/notes.md",
]
CMD_KIRO_KEY = [
    f"{REPO_CD}/orchestrator",
    "python3 - <<'PY'",
    "import getpass",
    "import json",
    "import kiro_config",
    "",
    "result = kiro_config.save_api_key(getpass.getpass(\"Kiro API key (hidden): \"))",
    "print(json.dumps(result, indent=2))",
    "raise SystemExit(1 if result.get(\"error\") else 0)",
    "PY",
]
CMD_BACKEND = [
    f"{REPO_CD}/coding-agents",
    "WORKSHOP_MODEL=\"${WORKSHOP_MODEL_CLAUDE_CODE:-${WORKSHOP_CLAUDE_MODEL:-}}\" \\",
    "  ./deploy-prebuilt.sh claude-code",
]
CMD_STEERING = [
    "REPO=~/sample-amazon-bedrock-agentcore-coding-agents",
    "cp \"$REPO/orchestrator/harness/codex/AGENTS.md\" /mnt/s3files/AGENTS.md",
    "cp -R \"$REPO/orchestrator/harness/kiro/.kiro\" /mnt/s3files/",
    "sudo mkdir -p /mnt/s3files/skills",
    "sudo cp -R \"$REPO/harness-skills/skills/backend-engineering\" \\",
    "           \"$REPO/harness-skills/skills/frontend-design\" /mnt/s3files/skills/",
    "ls -a /mnt/s3files",
    "grep -m1 'inclusion' /mnt/s3files/.kiro/steering/validator.md",
]
CMD_REPO = ["export GITHUB_REPO=\"owner/repository\"   # <- the repo you just created"]
CMD_APP = [f"{REPO_CD}/coding-agents/gateway_mcp", "python3 create-github-app.py"]
CMD_APP_RESUME = [f"{REPO_CD}/coding-agents/gateway_mcp", "python3 create-github-app.py --resume"]
CMD_GATEWAY = [f"{REPO_CD}/coding-agents/gateway_mcp", "source github-app.env", "./deploy-all.sh"]
CMD_DOCTOR = [REPO_CD, "python3 orchestrator/github.py doctor"]
CMD_COORD = [f"{REPO_CD}/orchestrator-agent", "./deploy-coordinator.sh"]
CMD_SUBMIT = [
    f"{REPO_CD}/CodingAgents",
    "set +H",
    "DIRECTION=$(cat <<'EOF'",
    "<your team's setting, mood, or play idea>",
    "EOF",
    ")",
    "SESSION_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')",
    "printf 'Coordinator session ID: %s\\n' \"$SESSION_ID\"",
    "agentcore invoke --session-id \"$SESSION_ID\" --stream \\",
    "  \"Use preset=game-from-scratch. Creative direction: $DIRECTION\"",
]
CMD_CHECKOUT_GAME = [REPO_CD, "python3 orchestrator/github.py checkout ~/game"]
CMD_CHECKOUT_LAB3 = [REPO_CD, "python3 orchestrator/github.py checkout ~/game-lab3"]
CMD_START_GAME = ["cd ~/game && claude"]
CMD_START_LAB3 = ["cd ~/game-lab3", "claude"]
CMD_STUDIO = ["sudo systemctl start stage2-console", "sudo systemctl is-active stage2-console"]
CMD_GALLERY = [
    REPO_CD,
    "python3 orchestrator/gallery.py publish \\",
    "  --project ~/game \\",
    "  --port 8000 \\",
    "  -- npm start",
]


@dataclass
class Check:
    lab: str
    name: str
    page: str
    state: str = UNKNOWN
    detail: str = ""
    commands: list[str] = field(default_factory=list)
    note: str = ""
    required: bool = True


@dataclass
class Spec:
    lab: str
    name: str
    page: str
    run: Callable[[], dict]
    commands: list[str] = field(default_factory=list)
    note: str = ""
    required: bool = True


# ---------------------------------------------------------------- small helpers
def _mask(text: Any) -> str:
    return _ACCOUNT_RE.sub(r"\1****\2", str(text or ""))


def _repo_root() -> str:
    return os.environ.get("WORKSHOP_REPO_ROOT") or os.path.dirname(_HERE)


def _coding_agents_dir() -> str:
    return os.environ.get("WORKSHOP_CODING_AGENTS_DIR") or os.path.join(_repo_root(), "coding-agents")


def _runs_dir() -> str:
    return os.environ.get("WORKSHOP_RUNS_DIR") or os.path.join(_repo_root(), ".runs")


def _mount() -> str:
    return os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files")


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def _is_mount(path: str) -> bool:
    return os.path.ismount(path)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Every subprocess this tool starts itself (the optional gallery check goes through
# gallery.py's own read-only status path). A NEXT block prints commands for the person
# to run; this tool itself only ever asks questions.
_READONLY_ARGV = (("systemctl", "is-active"),)


def _run_readonly(argv: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    if tuple(argv[:2]) not in _READONLY_ARGV:
        raise PermissionError(f"progress.py runs read-only commands only, not {argv[:2]}")
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _outcome(state: str, detail: str, **overrides: Any) -> dict:
    return {"state": state, "detail": detail, **overrides}


# ---------------------------------------------------------------- memoized reads
_memo: dict[str, Any] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _once(key: str, compute: Callable[[], Any]) -> Any:
    """Compute a shared read once per invocation; a slow key never blocks another."""
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        if key not in _memo:
            try:
                _memo[key] = ("ok", compute())
            except Exception as exc:  # noqa: BLE001 (each caller reports it as "?")
                _memo[key] = ("error", exc)
    status, value = _memo[key]
    if status == "error":
        raise value
    return value


def _base_url() -> str | None:
    return _once("base_url", workshop_urls.public_base_url)


def _recent_runs() -> tuple[list[dict], str, bool]:
    """Newest-first run records from local disk and the deployed coordinator's mirror."""
    def compute():
        import run_store  # noqa: PLC0415
        bucket = run_store.attach_reader_mirror()
        local = os.path.join(_runs_dir(), "state")
        if local.startswith(_repo_root() + os.sep):
            local = os.path.relpath(local, _repo_root())
        where = local + (f" and the s3://{bucket} mirror" if bucket else "")
        return run_store.recent(_runs_dir(), limit=10), where, bool(bucket)
    return _once("runs", compute)


def _service_status(arn: str) -> tuple[str | None, str]:
    """(status, reason) from GetAgentRuntime, or (None, why it was not checked)."""
    match = _RUNTIME_ARN_RE.match(arn or "")
    if not match:
        return None, "not a Runtime ARN"
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415
    except ImportError:
        return None, "boto3 is not installed"
    region, runtime_id = match.groups()
    try:
        client = boto3.client("bedrock-agentcore-control", region_name=region, config=Config(
            connect_timeout=3, read_timeout=6, retries={"max_attempts": 1}))
        return client.get_agent_runtime(agentRuntimeId=runtime_id).get("status") or None, ""
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            return "NOT_FOUND", ""
        return None, code or type(exc).__name__


def _has_app(directory: str) -> tuple[bool | None, int]:
    """(True, n) when a checkout holds more than the repository's starter files."""
    return run_advice.holds_app(directory)


def _pr_urls(rec: dict) -> list[str]:
    return run_advice.pr_urls(rec)


def _merged(rec: dict) -> bool:
    return any(row.get("state") == "merged" for row in rec.get("role_prs") or [])


def _stale(rec: dict) -> bool:
    import run_store  # noqa: PLC0415
    return run_store.active_snapshot_is_stale(rec)


def _one_line(text: Any, limit: int = 240) -> str:
    return " ".join(str(text or "").split())[:limit]


# ---------------------------------------------------------------- Lab 1
def check_storage() -> dict:
    mount = _mount()
    infra = os.path.join(_coding_agents_dir(), "infra.config")
    try:
        with open(infra, encoding="utf-8") as handle:
            has_arn = any(re.match(r"^INFRA_S3FILES_AP_ARN=.+", line) for line in handle)
    except OSError:
        has_arn = False
    if not _is_mount(mount):
        return _outcome(TODO, f"{mount} is not mounted; the stack creates it, so ask a facilitator")
    if not has_arn:
        return _outcome(TODO, "coding-agents/infra.config has no INFRA_S3FILES_AP_ARN; ask a facilitator")
    return _outcome(DONE, f"{mount} is mounted and infra.config has its access point")


def check_note() -> dict:
    path = os.path.join(_mount(), "notes.md")
    if os.path.isfile(path):
        return _outcome(DONE, f"{path} exists; the Runtime shells read it back")
    return _outcome(TODO, f"{path} not written yet")


def _check_runtime(harness_dir: str, stack_prepared: bool) -> dict:
    path = os.path.join(_coding_agents_dir(), harness_dir, "runtime_config.json")
    config = _read_json(path)
    arn = (config.get("runtime_arn") or "").strip()
    if not arn:
        why = ("the stack should have created it" if stack_prepared
               else "this is the Runtime you create in Lab 1")
        return _outcome(TODO, f"no Runtime ARN in coding-agents/{harness_dir}/runtime_config.json ({why})")
    if not config.get("s3files_access_point_arn"):
        return _outcome(TODO, "deployed without the shared /mnt/s3files mount; run the deploy again")
    status, reason = _service_status(arn)
    platform = config.get("platform_version") or "?"
    shown = _mask(arn)
    if status == "READY":
        return _outcome(DONE, f"READY on {platform}: {shown}")
    if status == "NOT_FOUND":
        return _outcome(TODO, f"the saved ARN no longer exists in AgentCore: {shown}")
    if status:
        return _outcome(TODO, f"AgentCore reports {status}; wait for READY or deploy again: {shown}")
    return _outcome(DONE, f"saved on {platform} (live status not checked: {reason}): {shown}")


def check_kiro_key() -> dict:
    import kiro_config  # noqa: PLC0415
    result = kiro_config.status()   # provider metadata only; never the key
    if result.get("error"):
        return _outcome(UNKNOWN, _mask(_one_line(result["error"], 160)))
    if result.get("connected"):
        verdict = result.get("key_check")
        how = ("found in the Token Vault" if result.get("source") == "token-vault"
               else "saved to the Token Vault from this host")
        extra = f"; key check: {verdict}" if verdict else ""
        if verdict == "rejected":
            return _outcome(TODO, f"Kiro rejected the saved key{extra}; paste it again")
        return _outcome(DONE, how + extra)
    return _outcome(TODO, "no Kiro key in the Token Vault yet (use your own ksk_ or the KiroApiKey event output)")


def check_steering(roles_needing: list[tuple[str, str]], skills: list[str]) -> dict:
    mount = _mount()
    missing = []
    for label, relative in roles_needing:
        path = os.path.join(mount, relative)
        if not os.path.isfile(path):
            missing.append(f"{label} {relative}")
        elif relative.endswith("validator.md"):
            with open(path, encoding="utf-8", errors="replace") as handle:
                if "inclusion: always" not in handle.read(4096):
                    missing.append(f"{label} {relative} (no 'inclusion: always')")
    for skill in skills:
        if not os.path.isdir(os.path.join(mount, "skills", skill)):
            missing.append(f"skills/{skill}")
    if missing:
        return _outcome(TODO, "missing on the mount: " + ", ".join(missing))
    staged = [relative for _, relative in roles_needing] + [f"skills/{s}" for s in skills]
    return _outcome(DONE, "staged: " + ", ".join(staged))


# ---------------------------------------------------------------- Lab 2
def _github():
    import github  # noqa: PLC0415 (imports no credential; the Gateway path is lazy)
    return github


def check_repo() -> dict:
    github = _github()
    repo = github.configured_repository()
    if not repo:
        return _outcome(TODO, "GITHUB_REPO is not set in this terminal and no repository is saved")
    if not github._REPO_RE.match(repo):
        # Never echo the value: a token pasted into GITHUB_REPO would be printed into
        # a report people paste into chat. Say what is wrong with its shape instead.
        shape = ("contains '@', so it looks like an email" if "@" in repo
                 else "has no '/'" if "/" not in repo else "is not owner/name")
        return _outcome(TODO, f"GITHUB_REPO {shape}; set it to the owner/repository part "
                              "of your repository's URL")
    source = "this terminal" if os.environ.get("GITHUB_REPO") else "saved settings"
    return _outcome(DONE, f"{repo} (from {source})")


def check_app() -> dict:
    folder = os.path.join(_coding_agents_dir(), "gateway_mcp")
    if os.path.isfile(os.path.join(folder, "github-app.env")):   # exists only; never opened
        return _outcome(DONE, "github-app.env written by the App setup")
    if os.path.exists(os.path.join(folder, ".github-app-setup.json")):
        return _outcome(TODO, "the App setup started but did not finish; resume it",
                        commands=CMD_APP_RESUME,
                        note="Keep it running until it prints Wrote .../github-app.env.")
    return _outcome(TODO, "no GitHub App registered from this host yet")


def check_gateway() -> dict:
    github = _github()
    url = _read_json(github._gateway_state_path()).get("gateway_url") or ""
    if url.startswith("https://"):
        return _outcome(DONE, f"deployed: {url}")
    return _outcome(TODO, "no gateway_url in coding-agents/gateway_mcp/.deployed-state.json")


def check_settings() -> dict:
    saved = _github()._load_config_file()
    if saved.get("repo") and str(saved.get("gateway_url") or "").startswith("https://"):
        return _outcome(DONE, f"saved for Agent Studio: {saved['repo']}")
    return _outcome(TODO, "the repository and Gateway URL are not saved for Agent Studio yet")


def check_doctor() -> dict:
    return _outcome(INFO, "not run here, because its last check writes a disposable branch; "
                          "run it yourself and require six PASS lines")


def check_coordinator() -> dict:
    cli = os.path.join(_repo_root(), "CodingAgents", "agentcore", ".cli")
    state = _read_json(os.path.join(cli, "deployed-state.json"))
    entry = ((((state.get("targets") or {}).get("default") or {}).get("resources") or {})
             .get("runtimes") or {}).get("orchestrator") or {}
    arn = entry.get("runtimeArn") or ""
    if not arn:
        return _outcome(TODO, "not deployed yet")
    receipt = (_read_json(os.path.join(cli, "runtime-platforms.json")).get("default") or {}).get("orchestrator") or {}
    if receipt.get("runtime_status") != "READY":
        return _outcome(TODO, "deployed, but the script's READY check has not finished; if "
                              "./deploy-coordinator.sh is still running, let it finish, otherwise "
                              "run it again (it reuses the project)")
    status, _reason = _service_status(arn)
    if status == "NOT_FOUND":
        return _outcome(TODO, f"the saved coordinator no longer exists: {_mask(arn)}")
    return _outcome(DONE, f"READY on {receipt.get('platform_version', '?')}: {_mask(arn)}")


def _build_outcome(runs: list[dict], where: str, mirror_ok: bool, *, checkout_dir: str,
                   page: str, none_found: dict, chat: bool) -> dict:
    """Shared by the Lab 2 CLI build and the Lab 3 Chat build.

    The advice follows ``run_advice``, the same reading the watcher uses, so the two
    never disagree: a PR recorded only under ``work_items`` still counts, the generic
    red-gate choice is offered only when the check or review stayed red after its one
    repair, and a Lab 3 Chat build is never told to rerun Lab 2's build.
    """
    has_app, _count = _has_app(_home(checkout_dir))
    newest = runs[0] if runs else None
    newest_line = ""
    if newest:
        newest_line = f"{newest.get('run_id', '?')} {newest.get('status', '?')}"
    if has_app:
        extra = f"; newest build {newest_line}" if newest_line else ""
        return _outcome(DONE, f"merged code is in ~/{checkout_dir}{extra}")
    if not runs:
        if not mirror_ok and not chat:
            # The deployed coordinator's runs live in its S3 mirror. Unreadable is not
            # the same as empty, so say both cases instead of a bare "submit".
            return {**none_found, "detail": "no local build found, and the deployed "
                                            "coordinator's run mirror could not be read",
                    "note": "If you already submitted, do NOT submit again: follow it with "
                            f"python3 orchestrator/watch_run.py --plain <run_id>. "
                            f"Otherwise: {none_found.get('note', '')}".rstrip()}
        note = none_found.get("note", "")
        if not chat:
            note = ("If you submitted in the last few minutes, wait instead: a build "
                    "appears here at its first heartbeat, and the coordinator may be "
                    "waiting for your answer in the first terminal. " + note)
        return {**none_found, "detail": f"no build found in {where}", "note": note}
    passed = next((r for r in runs if r.get("status") == "passed" and _pr_urls(r)), None)
    if passed:
        urls = _pr_urls(passed)
        state = "merged" if _merged(passed) else "waiting for your review and merge"
        return _outcome(DONE, f"{passed.get('run_id')} passed; pull request(s) "
                              f"{', '.join(urls)} {'is' if len(urls) == 1 else 'are'} {state}")
    run_id = newest.get("run_id", "?")
    urls = _pr_urls(newest)
    # Name the pull request(s) that need a person, not merely the first one: in a
    # two-PR run the first may already be merged while the second is red.
    blocked = [row.get("pr_url") for row in newest.get("role_prs") or []
               if row.get("state") == "blocked" and row.get("pr_url")]
    focus = blocked or urls
    pr = f"; pull request(s) {', '.join(focus)}" if focus else ""
    follow = f"{page} > 2. Watch the agents work" if page == P_BUILD else f"{page} > 4. Follow the check and review"
    if newest.get("status") in ("queued", "running") and not _stale(newest):
        return _outcome(
            TODO, f"{run_id} is {newest.get('status')} (phase {newest.get('phase') or '-'}){pr}",
            page=follow, commands=[REPO_CD, f"python3 orchestrator/watch_run.py --plain {run_id}"],
            note="Do not resubmit while it runs. Ctrl+C stops watching, not the build.")
    stale = _stale(newest)
    reason = "COORDINATOR_SESSION_INTERRUPTED" if stale else newest.get("fail_reason")
    status = "needs_human" if stale else newest.get("status")
    detail = f"{run_id} {status}" + (f": {reason}" if reason else "") + pr
    action = "" if stale else _one_line(newest.get("next_action"), 900)
    advice = f"The run's own advice: {action}\n" if action else ""
    result_page = f"{page} > 3. Read the recorded result" if page == P_BUILD else follow
    if urls:
        if stale:
            note = (f"The coordinator was recycled, but {', '.join(focus)} "
                    "is unaffected: open it, read its "
                    "check and Assessment, and continue as a person. Do not resubmit the whole build.")
        elif run_advice.red_after_repair(newest):
            other = ("leave it open, read Next action in Agent Studio, and ask your facilitator if "
                     "the check itself looks wrong" if chat else
                     "rerun Run a Build step 1 with a simpler direction (a new session ID); "
                     "the first run stays recorded")
            note = (f"{advice}The check or review stayed red after its one repair. Open "
                    f"{', '.join(focus)}, "
                    "read the latest check comment and Assessment, then either merge it yourself "
                    f"if you judge it good enough, or {other}.")
        else:
            note = f"{advice}Open {', '.join(focus)} and read the latest comments before deciding anything."
        return _outcome(TODO, detail, page=result_page, note=note, commands=[])
    if stale:
        note = "The coordinator was recycled before this run opened a pull request; submit it again."
    elif action:
        note = advice.rstrip()
    else:
        note = "No pull request was opened. Submit again with a simpler direction."
    # Print the submit commands only when submitting again is the advice: for a turn
    # limit, an exhausted quota, or a GitHub refusal, the run itself says not to.
    resubmit = stale or not action or bool(newest.get("resubmission_allowed"))
    return {**none_found, "state": TODO, "detail": detail, "note": note,
            **({} if resubmit else {"commands": [], "page": result_page})}


def check_build() -> dict:
    runs, where, mirror_ok = _recent_runs()
    # A Lab 2 build comes from the CLI through the deployed coordinator, which records
    # no signed-in user. An Agent Studio Chat build records the Cognito submitter, so
    # the two labs' builds are told apart by that recorded fact, not by guessing.
    cli_runs = [r for r in runs if not r.get("submitted_by")]
    return _build_outcome(cli_runs, where, mirror_ok, checkout_dir="game", page=P_BUILD,
                          chat=False, none_found=_outcome(
        TODO, "", page=f"{P_BUILD} > 1. Submit the request", commands=CMD_SUBMIT,
        note="Replace the angle-bracket text with your own direction. Save the session ID "
             "and run ID, then follow it: python3 orchestrator/watch_run.py --plain"))


def check_chat_build() -> dict:
    runs, where, mirror_ok = _recent_runs()
    chat_runs = [r for r in runs if r.get("submitted_by")]
    return _build_outcome(chat_runs, where, mirror_ok, checkout_dir="game-lab3", page=P_STUDIO,
                          chat=True, none_found=_outcome(
        TODO, "", page=f"{P_STUDIO} > 3. Describe one problem you actually saw", commands=[],
        note="In Agent Studio, open Workspace > Chat and describe one problem from your play "
             "notes: how to reproduce it, what happened, and what should happen."))


def _check_checkout(directory: str, commands: list[str], merge_page: str) -> dict:
    has_app, count = _has_app(_home(directory))
    if has_app is None:
        return _outcome(TODO, f"~/{directory} does not exist yet", commands=commands,
                        note=f"Merge the pull request on GitHub first ({merge_page}).")
    if not has_app:
        return _outcome(TODO, f"~/{directory} holds only the repository's starter files: the pull "
                              "request was not merged when checkout ran", commands=commands,
                        note="Merge the pull request on GitHub, then run checkout again; "
                             "it replaces that README-only folder.")
    return _outcome(DONE, f"~/{directory} holds {count} files or folders besides the starter files")


def check_game_checkout() -> dict:
    return _check_checkout("game", CMD_CHECKOUT_GAME,
                           f"{P_EVIDENCE} > 4. Choose who merges")


def check_lab3_checkout() -> dict:
    return _check_checkout("game-lab3", CMD_CHECKOUT_LAB3,
                           f"{P_STUDIO} > 5. Play the updated version")


def _check_port(port: int, directory: str, commands: list[str], moved_on: bool = False) -> dict:
    try:
        base = _base_url()
    except Exception:  # noqa: BLE001
        base = None
    url = workshop_urls.proxy_url(port, base) if base else None
    open_line = (f"open {url} in the browser where VS Code is signed in" if url else
                 f"open your VS Code tab's https origin followed by /proxy/{port}/")
    answered, detail = workshop_urls.local_answer(port)
    if answered:
        return _outcome(DONE, f"answering on port {port} ({detail}); {open_line}")
    if moved_on:
        return _outcome(INFO, f"{detail}; fine now that you have moved on to Lab 3 "
                              "(restart it only to replay the original)")
    return _outcome(TODO, f"{detail}", commands=commands,
                    note=(f"Ask Claude Code to read the README and start the game in the foreground "
                          f"with PORT={port}, using the request on that page, and keep that terminal "
                          f"running. Then {open_line}. The trailing slash matters."))


def check_game_8000() -> dict:
    moved_on = bool(_has_app(_home("game-lab3"))[0])
    return _check_port(8000, "game", CMD_START_GAME, moved_on=moved_on)


def check_game_8001() -> dict:
    return _check_port(8001, "game-lab3", CMD_START_LAB3)


def _gallery_status() -> dict | None:
    import gallery  # noqa: PLC0415
    if not os.path.isfile(gallery.HOST_HELPER):
        return None
    return gallery.host_command("status")   # the helper's read-only status operation


def check_gallery() -> dict:
    state = _gallery_status()
    if state is None:
        return _outcome(INFO, "the event's game-hosting helper is not on this host (own account: skip)")
    if state.get("active") is True:
        url = state.get("public_url") or ""
        return _outcome(DONE, "a published copy is running" + (f": {url}" if url else ""))
    return _outcome(TODO, "no published copy is running")


# ---------------------------------------------------------------- Lab 3
def check_studio() -> dict:
    try:
        result = _run_readonly(["systemctl", "is-active", "stage2-console"])
    except FileNotFoundError:
        return _outcome(UNKNOWN, "systemctl is not available here (not the workshop host)")
    state = (result.stdout or "").strip() or "unknown"
    if state == "active":
        return _outcome(DONE, "stage2-console is active; open ConsoleUrl from Event Outputs")
    return _outcome(TODO, f"stage2-console is {state}")


# ---------------------------------------------------------------- the checklist
def specs() -> list[Spec]:
    """Every checkpoint, in the order the labs teach them. Roles come from the registry."""
    import roles  # noqa: PLC0415
    served = roles.roster()
    out = [
        Spec(LAB1, "Shared storage", f"{P_WORKSPACE} > 2. Confirm the mount and the ARN every deploy reads",
             check_storage, CMD_STORAGE,
             note="If either check fails, ask your facilitator: the stack creates the storage."),
        Spec(LAB1, "Shared note", f"{P_WORKSPACE} > Verify", check_note, CMD_NOTE, required=False),
    ]
    for role in served:
        if role.capability == "backend":
            continue
        out.append(Spec(
            LAB1, f"{role.label} Runtime", f"{P_RUNTIMES} > Verify",
            (lambda d=role.harness_dir: _check_runtime(d, True)),
            [f"{REPO_CD}/coding-agents", f"./deploy-prebuilt.sh {role.harness_dir}"],
            note="The stack prepares this one. If it is missing, that page's Verify step "
                 "deploys it from the prebuilt image with this command."))
    if "kiro" in [role.id for role in served]:
        out.append(Spec(LAB1, "Kiro API key", f"{P_RUNTIMES} > 2. Give Kiro your API key",
                        check_kiro_key, CMD_KIRO_KEY,
                        note="Paste the key at the hidden prompt. Look for \"key_check\": \"verified\"."))
    for role in served:
        if role.capability != "backend":
            continue
        commands = (CMD_BACKEND if role.id == "claude-code"
                    else [f"{REPO_CD}/coding-agents", f"./deploy-prebuilt.sh {role.harness_dir}"])
        out.append(Spec(
            LAB1, f"{role.label} Runtime (yours)", f"{P_RUNTIMES} > 3. Create the backend Runtime",
            (lambda d=role.harness_dir: _check_runtime(d, False)), commands,
            note="If it is already running in another terminal, wait for it instead. Keep it "
                 "running until it prints Runtime ARN: and then Done. It usually takes about a minute."))
    # Claude Code roles read the CLAUDE.md baked into their image, so only the other
    # roles' steering belongs on the shared mount (a copy nobody reads would only
    # reach the checker). Skills follow the builder capabilities actually served.
    needing = [(role.label, role.steering_file) for role in served if role.steering_file != "CLAUDE.md"]
    skill_for = {"backend": "backend-engineering", "frontend": "frontend-design"}
    skills = [skill_for[c] for c in dict.fromkeys(r.capability for r in served) if c in skill_for]
    out.append(Spec(LAB1, "Steering on the mount", f"{P_SHELL} > 1. Stage the steering and skills",
                    (lambda: check_steering(needing, skills)), CMD_STEERING,
                    note="The grep must print inclusion: always."))
    out += [
        Spec(LAB2, "App repository", f"{P_GITHUB} > 1. Create your working repository", check_repo,
             CMD_REPO, note="Create it on GitHub with No template, Private, and Add README first. "
                            "Use your GitHub username, not your email."),
        Spec(LAB2, "GitHub App", f"{P_GITHUB} > 2. Register the GitHub App", check_app, CMD_APP,
             note="Open the printed setup URL in the same browser as VS Code."),
        Spec(LAB2, "GitHub Gateway", f"{P_GITHUB} > 3. Deploy the credential, the MCP server, and the Gateway",
             check_gateway, CMD_GATEWAY,
             note="Wait for Gateway verification passed: GitHubMCP tools are discoverable."),
        Spec(LAB2, "Saved GitHub settings", f"{P_GITHUB} > 4. Export the coordinator's configuration",
             check_settings, [],
             note="Run that step's code block; its last line reads Saved for Agent Studio: owner/repository."),
        Spec(LAB2, "GitHub doctor", f"{P_GITHUB} > 5. Prove the boundary, then prove the App can write",
             check_doctor, CMD_DOCTOR, required=False),
        Spec(LAB2, "Coordinator", f"{P_COORD} > 1. Run the deployment script", check_coordinator,
             CMD_COORD, note="It usually takes 5 to 10 minutes, and up to 15. Do not press Ctrl+C "
                             "while it waits; it ends with Coordinator deployed."),
        Spec(LAB2, "Build", f"{P_BUILD} > 1. Submit the request", check_build, CMD_SUBMIT),
        Spec(LAB2, "Merged game on the host", f"{P_PLAY} > 1. Get the merged code onto the box",
             check_game_checkout),
        Spec(LAB2, "Game on port 8000", f"{P_PLAY} > 2. Ask Claude Code to run it", check_game_8000),
        Spec(LAB2, "Published copy", f"{P_PLAY} > 4. Share a playable copy in the event gallery",
             check_gallery, CMD_GALLERY, required=False,
             note="Pass the README's own foreground command after --; npm start is only an example."),
        Spec(LAB3, "Agent Studio", f"{P_STUDIO} > 1. Open Agent Studio", check_studio, CMD_STUDIO,
             note="Continue when it prints active, then open ConsoleUrl from Event Outputs."),
        Spec(LAB3, "Chat build", f"{P_STUDIO} > 3. Describe one problem you actually saw", check_chat_build),
        Spec(LAB3, "Updated game on the host", f"{P_STUDIO} > 5. Play the updated version",
             check_lab3_checkout),
        Spec(LAB3, "Updated game on port 8001", f"{P_STUDIO} > 5. Play the updated version", check_game_8001),
    ]
    return out


def _evaluate(spec: Spec) -> Check:
    check = Check(spec.lab, spec.name, spec.page, commands=list(spec.commands),
                  note=spec.note, required=spec.required)
    try:
        outcome = spec.run()
    except Exception as exc:  # noqa: BLE001 (one broken check never hides the rest)
        check.state, check.detail = UNKNOWN, f"could not check: {type(exc).__name__}: {_one_line(exc, 160)}"
        check.detail = _mask(check.detail)
        return check
    for key, value in outcome.items():
        setattr(check, key, value)
    check.detail = _mask(check.detail)
    check.note = _mask(check.note)
    return check


def collect(budget_s: float = BUDGET_S) -> list[Check]:
    """Run every check concurrently; anything unfinished at the deadline becomes '?'."""
    _memo.clear()
    _locks.clear()
    items = specs()
    results: list[Check | None] = [None] * len(items)

    def work(index: int) -> None:
        results[index] = _evaluate(items[index])

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(len(items))]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + budget_s
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    out = []
    for spec, result in zip(items, list(results)):
        out.append(result or Check(spec.lab, spec.name, spec.page, UNKNOWN,
                                   f"timed out after {budget_s:.0f}s", required=spec.required))
    return out


def next_step(checks: list[Check]) -> Check | None:
    """The first required checkpoint that is known to be not done."""
    return next((c for c in checks if c.required and c.state == TODO), None)


def render(checks: list[Check]) -> str:
    lines = ["Workshop progress for this host (read-only: nothing is created, started, or "
             "changed; no keys or passwords, so it is safe to send to a helper)",
             time.strftime("  checked %Y-%m-%d %H:%M:%S UTC", time.gmtime())]
    width = max(len(c.name) for c in checks) + 2
    lab = None
    for check in checks:
        if check.lab != lab:
            lab = check.lab
            lines += ["", lab]
        optional = "" if check.required or check.state == INFO else " (optional)"
        lines.append(f"  {_MARK[check.state]} {(check.name + optional).ljust(width + 11)} {check.detail}")
    step = next_step(checks)
    lines.append("")
    if step is None:
        unknown = [c.name for c in checks if c.required and c.state == UNKNOWN]
        if unknown:
            lines.append("NEXT  nothing is known to be missing, but these could not be checked: "
                         + ", ".join(unknown) + ". Ask a facilitator to look at them.")
        else:
            lines.append("All the checkpoints this host can verify are done.")
            lines.append(f"NEXT  {AFTER_ALL}")
        return "\n".join(lines)
    lines.append(f"NEXT  {step.page}")
    lines.append(f"      {step.name}: {step.detail}")
    if step.commands:
        # Flush left on purpose: an indented heredoc terminator or Python body would
        # break when pasted, so the block is printed exactly as the page shows it.
        lines.append("  Run in the VS Code terminal:")
        lines += list(step.commands)
    for part in (step.note or "").splitlines():
        lines.append(f"  {part}")
    lines.append("  Then run python3 ~/sample-amazon-bedrock-agentcore-coding-agents/orchestrator/progress.py again.")
    return "\n".join(lines)


def as_json(checks: list[Check]) -> str:
    step = next_step(checks)
    return json.dumps({"checks": [asdict(c) for c in checks],
                       "next": asdict(step) if step else None}, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print the checkpoints as JSON")
    args = parser.parse_args(argv)
    try:
        checks = collect()
    except Exception as exc:  # noqa: BLE001 (for example, WORKSHOP_ROLES names an unknown role)
        print(f"progress.py could not build its checklist: {_mask(exc)}", file=sys.stderr)
        return 2
    print(as_json(checks) if args.json else render(checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
