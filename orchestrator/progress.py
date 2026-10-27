#!/usr/bin/env python3
"""Where am I in the workshop, and what do I run next? One read-only answer.

    python3 orchestrator/progress.py            # every checkpoint, then NEXT
    python3 orchestrator/progress.py --json     # the same, as JSON

The workshop has three labs: close the lid, twenty at once, and a team of agents that
GitHub wakes up. This walks their end states IN ORDER against real local state and a
few cheap, read-only AWS reads, prints one line per checkpoint, and ends with ONE next
step: the page as the content titles it and the exact command that page teaches. An
attendee who fell behind runs it to catch up; a facilitator asks for its output instead
of holding the room.

It is strictly READ-ONLY, which is what makes it safe to run at any moment and to
paste anywhere. It invokes no model, opens no Runtime session, makes no GitHub call,
and writes no file. It never reads a key, token, password, or private key: the
GitHub App credential is checked with Secrets Manager ``DescribeSecret`` (metadata
only), and ``github-app.env`` is never opened. Account ids are masked. Every check
fails soft to ``?`` on its own, and the whole command is bounded to about fifteen
seconds, so a slow or missing service cannot hang it.

It is a reporting tool, never the verdict path: nothing reads it back, and it may
not import ``llm``, ``reviewer``, or the engine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "coding-agents"))
import team_exchange_policy  # noqa: E402

# A laptop has no instance metadata service; do not let the SDK spend its default
# retries looking for one. The workshop host answers well inside these limits.
os.environ.setdefault("AWS_METADATA_SERVICE_TIMEOUT", "2")
os.environ.setdefault("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")

BUDGET_S = 15.0
DONE, TODO, UNKNOWN = "done", "todo", "unknown"
_MARK = {DONE: "✓", TODO: "✗", UNKNOWN: "?"}

GET_IN = "Getting Started"
PART1 = "Lab 1. Run Claude Code on AgentCore Runtime"
PART2 = "Lab 2. Run Many Agents in Parallel"
PART3 = "Lab 3. Build with a Team of Agents from GitHub"

SECRET_ID = "agentcore/github-mcp/github-app"
GATEWAY_NAME = "github-mcp-gateway"
WEBHOOK_PARAM = "/workshop/webhook-url"
KIRO_PROVIDER = "kiro-api-key"
# At an event the team stack's key import records its outcome here (names and the
# status only, never the key): READY, or UNAVAILABLE with the reason and the fallback.
KIRO_KEY_STATUS_PARAM = "/workshop/kiro/key-status"
NO_KEY_CHECKER = "claude-code-validator"
LID = "lid"
SHARED_REPO = "lid.git"   # Lab 2: the bare repository on the mount
GAME = "lid-game"         # Lab 2: the game, a Git repository in your home
ARCADE = "arcade"         # Lab 2: one worktree per agent branch, and the run record

# Exactly what the pages teach, copied verbatim so a pasted NEXT block runs as-is.
# validate_content.py fails if any line here stops appearing in a page's code block.
CMD_CD = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents/coding-agents"]
CMD_REGISTRY = ["export REGISTRY=$(aws sts get-caller-identity --query Account --output text)"
                ".dkr.ecr.$AWS_REGION.amazonaws.com"]
CMD_BUILD = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents"] + CMD_REGISTRY + [
    "docker buildx bake claude-code --push"]
CMD_CONNECT = [
    'ARN=$(aws bedrock-agentcore-control list-agent-runtimes --query '
    '"agentRuntimes[?agentRuntimeName==\'claude_code\'].agentRuntimeArn" --output text)',
    "cat /proc/sys/kernel/random/uuid > ~/lid-session",
    'agentcore exec --it --runtime "$ARN" --session-id "$(cat ~/lid-session)"']
CMD_START = ["cd /mnt/s3files/lid && /app/run.sh"]
CMD_SHARE = CMD_CD + ["python3 fanout.py --init"]
CMD_FANOUT = CMD_CD + ["python3 fanout.py --agents 20"]
CMD_GITHUB = CMD_CD + ["./connect-github.sh"]
CMD_ROOT = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents"]
CMD_INFRA = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents && coding-agents/infra/setup.sh"]
CMD_ENDPOINT = [
    "cd ~/sample-amazon-bedrock-agentcore-coding-agents/coding-agents/pr_review && . ../infra.config",
    "aws cloudformation package --template-file template.yaml --output-template-file "
    '/tmp/pr-review.yaml --s3-bucket "$INFRA_BUCKET"',
    "aws cloudformation deploy --template-file /tmp/pr-review.yaml --stack-name "
    "coding-agents-pr-review --capabilities CAPABILITY_NAMED_IAM"]
# The stack creates codex and kiro; this is the fallback for a missing one. Without a
# pre-built image in this clone, deploy-prebuilt.sh builds it first, then updates (or
# creates) the Runtime of that name with its own deploy.py role.
# One block per missing team Runtime, as the page's expand has them: pasting both would
# rebuild and update a Runtime that is already READY.
CMD_TEAM_CODEX = CMD_CD + ["./deploy-prebuilt.sh codex"]
CMD_TEAM_KIRO = CMD_CD + ["./deploy-prebuilt.sh kiro"]
CMD_TEAM_CHECKER = CMD_CD + ["./deploy-prebuilt.sh claude-code-validator"]
_TEAM_RUNTIME_CMDS = {"codex": CMD_TEAM_CODEX, "kiro": CMD_TEAM_KIRO,
                      "claude_code_validator": CMD_TEAM_CHECKER}
# Only claude_code needs it: Lab 1's console role. The roles deploy.py made for codex
# and kiro already have their own archive grant and, for kiro, the Token Vault.
CMD_GRANTS = [
    "cd ~/sample-amazon-bedrock-agentcore-coding-agents/coding-agents && . infra.config",
    'role() { basename "$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id '
    '"$(aws bedrock-agentcore-control list-agent-runtimes --query '
    '"agentRuntimes[?agentRuntimeName==\'$1\'].agentRuntimeId" --output text)" '
    '--query roleArn --output text)"; }',
    'aws iam put-role-policy --role-name "$(role claude_code)" '
    '--policy-name TeamRunExchange --policy-document '
    '"$(python3 team_exchange_policy.py --role claude-code --bucket "$INFRA_BUCKET")"']
CONSOLE_ROLE = "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-"
CMD_KIRO_KEY = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents && python3 orchestrator/kiro_config.py save"]
CMD_COORDINATOR = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents/orchestrator-agent",
                   "WORKSHOP_MERGE_POLICY=auto ./deploy-coordinator.sh"]
CMD_ASK = ["cd ~/sample-amazon-bedrock-agentcore-coding-agents/orchestrator-agent",
           './ask.sh "Use preset=team-game. Creative direction: <your idea, in one sentence>"']
CMD_WATCH = CMD_ROOT + ["python3 orchestrator/watch_run.py --plain"]

_ACCOUNT_RE = re.compile(r"(?<![\w])(\d{4})\d{4}(\d{4})(?![\w])")
_RUNTIME_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/([A-Za-z0-9_-]+)$")


@dataclass
class Check:
    part: str
    name: str
    state: str = UNKNOWN
    detail: str = ""
    commands: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Spec:
    part: str
    name: str
    run: Callable[[], dict]
    commands: list[str] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------- small helpers
def _mask(text) -> str:
    return _ACCOUNT_RE.sub(r"\1****\2", str(text or ""))


def _repo_root() -> str:
    return os.environ.get("WORKSHOP_REPO_ROOT") or os.path.dirname(_HERE)


def _agents_dir() -> str:
    return os.environ.get("WORKSHOP_CODING_AGENTS_DIR") or os.path.join(_repo_root(), "coding-agents")


def _mount() -> str:
    return os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files")


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _outcome(state: str, detail: str, **extra) -> dict:
    return {"state": state, "detail": detail, **extra}


def _region() -> str | None:
    arn = _read_json(os.path.join(_agents_dir(), "claude-code", "runtime_config.json")).get("runtime_arn", "")
    match = _RUNTIME_ARN_RE.match(arn or "")
    return match.group(1) if match else (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def _client(service: str):
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415
    return boto3.client(service, region_name=_region(), config=Config(
        connect_timeout=3, read_timeout=6, retries={"max_attempts": 1}))


def _error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "") or type(exc).__name__


def _visible_files(path: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(path) if not n.startswith("."))
    except OSError:
        return []


# ---------------------------------------------------------------- Get in
def check_host() -> dict:
    if not os.path.ismount(_mount()):
        return _outcome(TODO, f"{_mount()} is not mounted yet; the host is still setting up")
    return _outcome(DONE, f"shared folder mounted at {_mount()}")


def check_infra() -> dict:
    """The stack copies its verified infra.config into the home clone when bootstrap ends.

    Until then this is TODO, which usually means the stack is still finishing. The same
    file can also be recorded by hand with coding-agents/infra/setup.sh, which reads the
    stack's resource IDs and creates nothing.
    """
    if not os.path.isfile(os.path.join(_agents_dir(), "infra.config")):
        return _outcome(TODO, "this clone has no coding-agents/infra.config yet")
    return _outcome(DONE, "coding-agents/infra.config records the stack's network and storage")


def check_image(repository: str = "coding-agents-claude-code") -> dict:
    """The image the attendee built and pushed: its ``latest`` tag in their own ECR."""
    try:
        images = _client("ecr").describe_images(
            repositoryName=repository, imageIds=[{"imageTag": "latest"}]).get("imageDetails", [])
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) in ("RepositoryNotFoundException", "ImageNotFoundException"):
            return _outcome(TODO, f"no {repository}:latest in your ECR yet")
        return _outcome(UNKNOWN, f"ECR unreadable ({_error_code(exc)})")
    if not images:
        return _outcome(TODO, f"no {repository}:latest in your ECR yet")
    return _outcome(DONE, f"{repository}:latest " + str(images[0].get("imageDigest", ""))[:19])


# ---------------------------------------------------------------- 1. Close the lid
def _named_runtime_arn(name: str) -> str:
    """A Runtime built in the console is known by its name, not by a local file."""
    pages = _client("bedrock-agentcore-control").get_paginator("list_agent_runtimes")
    for page in pages.paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return runtime.get("agentRuntimeArn", "")
    return ""


def check_runtime() -> dict:
    arn = _read_json(os.path.join(_agents_dir(), "claude-code", "runtime_config.json")).get("runtime_arn", "")
    if not _RUNTIME_ARN_RE.match(arn or ""):
        try:
            arn = _named_runtime_arn("claude_code")
        except ImportError:
            return _outcome(UNKNOWN, "boto3 is not installed")
        except Exception as exc:  # noqa: BLE001
            return _outcome(UNKNOWN, f"Runtime list unreadable ({_error_code(exc)})")
    match = _RUNTIME_ARN_RE.match(arn or "")
    if not match:
        return _outcome(TODO, "no Runtime named claude_code yet")
    try:
        status = _client("bedrock-agentcore-control").get_agent_runtime(
            agentRuntimeId=match.group(2)).get("status", "")
    except ImportError:
        return _outcome(UNKNOWN, f"{match.group(2)} recorded; boto3 is not installed to check it")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ResourceNotFoundException":
            return _outcome(TODO, f"{match.group(2)} was recorded but no longer exists; deploy again")
        return _outcome(UNKNOWN, f"{match.group(2)} recorded; status unreadable ({_error_code(exc)})")
    if status == "READY":
        return _outcome(DONE, f"{match.group(2)} is READY")
    return _outcome(TODO if status.endswith("FAILED") else UNKNOWN, f"{match.group(2)} is {status}")


def check_lid_work() -> dict:
    # Lab 1's house rules (Build the Container Image, step 3) are the attendee's own
    # file, not work the agent finished.
    files = [f for f in _visible_files(os.path.join(_mount(), LID)) if f != "CLAUDE.md"]
    if not files:
        return _outcome(TODO, f"nothing built in {_mount()}/{LID} yet")
    shown = ", ".join(files[:4]) + (", ..." if len(files) > 4 else "")
    return _outcome(DONE, f"{len(files)} file(s) in {_mount()}/{LID}: {shown}")


# ---------------------------------------------------------------- 2. Twenty at once
def check_shared_repo() -> dict:
    """fanout.py --init: ~/lid-game is a Git repository, and its bare copy is on the mount."""
    bare = os.path.join(_mount(), SHARED_REPO)
    if not os.path.isdir(_home(GAME, ".git")):
        return _outcome(TODO, f"~/{GAME} is not a Git repository yet")
    if not os.path.isfile(os.path.join(bare, "HEAD")):
        return _outcome(TODO, f"no bare repository at {bare} yet")
    return _outcome(DONE, f"~/{GAME} pushes to {bare}")


def check_fanout() -> dict:
    summary = _read_json(_home(ARCADE, "fanout-summary.json"))
    if not summary:
        return _outcome(TODO, "no agent run on the shared repository yet")
    agents, pushed = summary.get("agents", 0), summary.get("pushed", 0)
    detail = (f"{agents} agents on {summary.get('microvms', 0)} microVMs, {pushed} branches pushed, "
              f"{summary.get('wall_s', '?')}s wall clock")
    return _outcome(DONE if agents and pushed == agents else TODO, detail)


# ---------------------------------------------------------------- 3. A team from GitHub
def check_github() -> dict:
    try:
        _client("secretsmanager").describe_secret(SecretId=SECRET_ID)
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ResourceNotFoundException":
            return _outcome(TODO, "no GitHub App connected yet")
        return _outcome(UNKNOWN, f"credential unreadable ({_error_code(exc)})")
    try:
        control = _client("bedrock-agentcore-control")
        token, found = None, False
        while not found:
            page = control.list_gateways(**({"nextToken": token} if token else {}))
            found = any(item.get("name") == GATEWAY_NAME for item in page.get("items", []))
            token = page.get("nextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001
        return _outcome(UNKNOWN, f"App stored; gateway unreadable ({_error_code(exc)})")
    if not found:
        return _outcome(TODO, "App stored, but its GitHub tools are not deployed yet")
    return _outcome(DONE, "App connected and its GitHub tools deployed")


def check_endpoint() -> dict:
    """The endpoint GitHub calls, which the attendee deployed from pr_review/template.yaml."""
    try:
        url = _client("ssm").get_parameter(Name=WEBHOOK_PARAM)["Parameter"]["Value"]
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ParameterNotFound":
            return _outcome(TODO, "the endpoint is not deployed yet")
        return _outcome(UNKNOWN, f"webhook endpoint unreadable ({_error_code(exc)})")
    host = url.split("/")[2] if url.count("/") >= 2 else url
    return _outcome(DONE, f"deployed ({host.split('.')[0]}...)")


def check_review() -> dict:
    try:
        url = _client("ssm").get_parameter(Name=WEBHOOK_PARAM)["Parameter"]["Value"]
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ParameterNotFound":
            return _outcome(TODO, "no endpoint yet, so nothing can wake up")
        return _outcome(UNKNOWN, f"webhook endpoint unreadable ({_error_code(exc)})")
    try:
        logs = _client("logs")
        groups = logs.describe_log_groups(logGroupNamePattern="PrReviewWebhook").get("logGroups", [])
        since = int((time.time() - 2 * 86400) * 1000)
        for group in groups:
            events = logs.filter_log_events(logGroupName=group["logGroupName"], startTime=since,
                                            filterPattern='"REVIEW_POSTED"', limit=20).get("events", [])
            if events:
                last = events[-1]["message"]
                record = json.loads(last[last.index("{"):]) if "{" in last else {}
                return _outcome(DONE, f"an agent reviewed {record.get('repo', '?')}#{record.get('number', '?')} "
                                      f"in {record.get('seconds', '?')}s")
    except Exception as exc:  # noqa: BLE001
        return _outcome(UNKNOWN, f"endpoint ready; reviews unreadable ({_error_code(exc)})")
    host = url.split("/")[2] if url.count("/") >= 2 else url
    return _outcome(TODO, f"endpoint ready ({host.split('.')[0]}...); no review posted yet")


def _project_json() -> dict:
    return _read_json(os.path.join(_repo_root(), "CodingAgents", "agentcore", "agentcore.json"))


def _coordinator_roles() -> str:
    """WORKSHOP_ROLES as the coordinator was deployed with it, or "" for the default team."""
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("name") == "WORKSHOP_ROLES" and isinstance(node.get("value"), str):
                found.append(node["value"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(_project_json())
    return found[0] if found else os.environ.get("WORKSHOP_ROLES", "")


def _event_key_status() -> str | None:
    """The event's key import outcome (KiroAccess central), or None outside an event."""
    try:
        value = _client("ssm").get_parameter(Name=KIRO_KEY_STATUS_PARAM)["Parameter"]["Value"]
        status = json.loads(value).get("key_status")
    except Exception:  # noqa: BLE001 (absent, unreadable or not JSON: no event record)
        return None
    return status if isinstance(status, str) and status else None


def check_checker() -> dict:
    roles = [r.strip() for r in _coordinator_roles().split(",") if r.strip()]
    if NO_KEY_CHECKER in roles and "kiro" not in roles:
        return _outcome(DONE, f"this team checks with {NO_KEY_CHECKER}, which needs no Kiro key")
    try:
        _client("bedrock-agentcore-control").get_api_key_credential_provider(name=KIRO_PROVIDER)
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) != "ResourceNotFoundException":
            return _outcome(UNKNOWN, f"Token Vault unreadable ({_error_code(exc)})")
        event = _event_key_status()
        if event and event != "READY":
            # The event delivered no key: say why, and the fallback the import named.
            return _outcome(TODO, f"the event's Kiro key did not arrive: {event}")
        return _outcome(TODO, "Kiro has no key in the Token Vault yet")
    # Provider metadata only: GetApiKeyCredentialProvider never returns the key.
    origin = " from the event's central account" if _event_key_status() == "READY" else ""
    return _outcome(DONE, f"Kiro's key is in the Token Vault ({KIRO_PROVIDER}){origin}")


def _runtime_arns(node) -> list[str]:
    if isinstance(node, str):
        return [node] if _RUNTIME_ARN_RE.match(node) else []
    items = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    return [arn for value in items for arn in _runtime_arns(value)]


def check_team_runtimes() -> dict:
    """codex and the checker in use, by name. The stack creates codex, kiro and the
    no-key checker (the last is optional there: a failed build does not fail the stack).
    A missing one gets its own deploy block, never the others'."""
    roles = [r.strip() for r in _coordinator_roles().split(",") if r.strip()]
    checker = NO_KEY_CHECKER if NO_KEY_CHECKER in roles and "kiro" not in roles else "kiro"
    states = []
    for name in ("codex", checker.replace("-", "_")):
        try:
            arn = _named_runtime_arn(name)
            status = _client("bedrock-agentcore-control").get_agent_runtime(
                agentRuntimeId=arn.rsplit("/", 1)[-1]).get("status", "") if arn else ""
        except ImportError:
            return _outcome(UNKNOWN, "boto3 is not installed")
        except Exception as exc:  # noqa: BLE001
            return _outcome(UNKNOWN, f"Runtime list unreadable ({_error_code(exc)})")
        if status != "READY":
            detail = f"no READY Runtime named {name} yet" + (f" ({status})" if status else "")
            return _outcome(TODO, detail, commands=_TEAM_RUNTIME_CMDS[name])
        states.append(name)
    return _outcome(DONE, " and ".join(states) + " are READY")


def check_team_grants() -> dict:
    """Console roles must have the current producer-specific archive grant.

    Only a console default role needs the TeamRunExchange grant, and in the workshop
    that is claude_code from Lab 1. The stack's codex and kiro (or the no-key checker)
    run on roles deploy.py made, which already carry their own grants, and Kiro's
    also reads its key from the Token Vault, so neither is asked for a grant. A team
    Runtime someone made in the console instead is still checked, by name, for both
    grants it lacks: TeamRunExchange, and for kiro also ReadKiroKey
    (kiro/token-vault-policy.json), without which its check cannot read its key.
    """
    roles = [r.strip() for r in _coordinator_roles().split(",") if r.strip()]
    checker = NO_KEY_CHECKER if NO_KEY_CHECKER in roles and "kiro" not in roles else "kiro"
    missing, stale, key_missing = [], [], False
    try:
        control, iam = _client("bedrock-agentcore-control"), _client("iam")
        for name in ("claude_code", "codex", checker.replace("-", "_")):
            arn = _named_runtime_arn(name)
            if not arn:
                return _outcome(TODO, f"no Runtime named {name} yet")
            role = control.get_agent_runtime(agentRuntimeId=arn.rsplit("/", 1)[-1])["roleArn"]
            role = role.rsplit("/", 1)[-1]
            if not role.startswith(CONSOLE_ROLE):
                continue  # deploy.py gives this producer its own archive grant
            for policy in ("TeamRunExchange",) + (("ReadKiroKey",) if name == "kiro" else ()):
                try:
                    saved = iam.get_role_policy(RoleName=role, PolicyName=policy)
                    if (policy == "TeamRunExchange"
                            and not team_exchange_policy.is_current(
                                saved.get("PolicyDocument", {}), name.replace("_", "-"))):
                        stale.append(name)
                except Exception as exc:  # noqa: BLE001
                    if _error_code(exc) != "NoSuchEntity":
                        raise
                    if policy == "TeamRunExchange":
                        missing.append(name)
                    else:
                        key_missing = True
    except ImportError:
        return _outcome(UNKNOWN, "boto3 is not installed")
    except Exception as exc:  # noqa: BLE001
        return _outcome(UNKNOWN, f"role grants unreadable ({_error_code(exc)})")
    gaps = []
    if missing:
        others = [n for n in missing if n != "claude_code"]
        hint = (f" ({', '.join(others)} also has a console role: set role() to its "
                "Runtime name and --role to its name with hyphens, "
                "not the Claude Code policy)") if others else ""
        gaps.append("no TeamRunExchange grant yet for " + ", ".join(missing) + hint)
    if stale:
        gaps.append("outdated or wrong-producer TeamRunExchange grant for "
                    + ", ".join(stale)
                    + ": replace it using team_exchange_policy.py --role "
                    "<role-id> --bucket \"$INFRA_BUCKET\"; the Runtime name uses "
                    "underscores and --role uses hyphens")
    if key_missing:
        # Its check reads the key at session start; without this the gate dies later.
        gaps.append("no ReadKiroKey grant yet for kiro, whose console role cannot read its key "
                    "from the Token Vault: in the same terminal, run aws iam put-role-policy "
                    '--role-name "$(role kiro)" --policy-name ReadKiroKey --policy-document '
                    "file://kiro/token-vault-policy.json, or give kiro its own role with "
                    "./deploy-prebuilt.sh kiro")
    if gaps:
        return _outcome(TODO, "; ".join(gaps))
    return _outcome(DONE, "Claude Code's role may read coordinator inputs and create "
                          "its own archives outside the shared mount; the team's "
                          "other roles already have their own grants")


def check_coordinator() -> dict:
    if not _project_json():
        return _outcome(TODO, "no coordinator project yet")
    state = _read_json(os.path.join(_repo_root(), "CodingAgents", "agentcore", ".cli",
                                    "deployed-state.json"))
    arns = [a for a in _runtime_arns(state) if "orchestrator" in a.rsplit("/", 1)[-1]]
    if not arns:
        return _outcome(TODO, "the coordinator project exists but is not deployed yet")
    runtime_id = arns[0].rsplit("/", 1)[-1]
    try:
        status = _client("bedrock-agentcore-control").get_agent_runtime(
            agentRuntimeId=runtime_id).get("status", "")
    except ImportError:
        return _outcome(UNKNOWN, f"{runtime_id} recorded; boto3 is not installed to check it")
    except Exception as exc:  # noqa: BLE001
        return _outcome(UNKNOWN, f"{runtime_id} recorded; status unreadable ({_error_code(exc)})")
    if status == "READY":
        return _outcome(DONE, f"{runtime_id} is READY")
    return _outcome(TODO if status.endswith("FAILED") else UNKNOWN, f"{runtime_id} is {status}")


def check_team_build() -> dict:
    import run_store  # noqa: PLC0415 (read-only; never the engine)
    run_store.attach_reader_mirror()
    runs = run_store.recent(os.environ.get("WORKSHOP_RUNS_DIR")
                            or os.path.join(_repo_root(), ".runs"), limit=5)
    for run in runs:
        prs = [r for r in (run.get("role_prs") or []) if isinstance(r, dict) and r.get("pr_url")]
        if not prs:
            continue
        status = run.get("status") or "?"
        detail = f"{run.get('run_id', '?')}: {len(prs)} pull request(s), {status}"
        if status in ("passed", "needs_human", "failed"):
            return _outcome(DONE, detail)
        return _outcome(UNKNOWN, detail + " (still running)")
    return _outcome(TODO, "no team build with a pull request yet")


SPECS = [
    Spec(GET_IN, "Workshop host", check_host,
         note="The host is still setting up or the shared folder is missing. Wait a minute and "
              "run progress.py again; if it persists, ask a facilitator."),
    Spec(GET_IN, "Your clone's infra.config", check_infra, CMD_INFRA,
         note="The stack copies it into your clone when it finishes. Run progress.py again "
              "shortly, or record the stack's IDs yourself with the command below (Getting "
              "Started, At an AWS Guided Event, step 3)."),
    Spec(PART1, "Claude Code image", check_image, CMD_BUILD,
         note="First time? Create the repository and sign in to it first (Lab 1, Build the Container Image, steps 1 and 2)."),
    Spec(PART1, "Claude Code Runtime", check_runtime,
         note="Create it in the AgentCore console: Agents > More > Host agent, named claude_code, "
              "with your image and Create default role (Lab 1, Create the Runtime and Open a Session, step 1). "
              "That step's first expand creates the same Runtime from the terminal instead."),
    Spec(PART1, "The work it finished", check_lid_work, CMD_CONNECT + CMD_START,
         note="Connect, start Claude Code in the shared folder, and paste the task on the page. "
              "Press Enter, then press Ctrl+] right away, while it works; if Ctrl+] does "
              "nothing, the page says why and how to get back. The 'Shell connection closed "
              "unexpectedly' line after the reconnect command is expected: the session keeps "
              "running."),
    Spec(PART2, "The shared repository", check_shared_repo, CMD_SHARE,
         note="This makes ~/lid-game a Git repository and pushes it to a bare repository on the "
              "mount, which every agent clones."),
    Spec(PART2, "Twenty agents", check_fanout, CMD_FANOUT,
         note="Add your rule to ~/lid-game/CLAUDE.md, then commit and push it first (step 3)."),
    Spec(PART3, "The endpoint", check_endpoint, CMD_ENDPOINT,
         note="Package and deploy template.yaml from that folder (Lab 3, Deploy the Endpoint, step 1)."),
    Spec(PART3, "GitHub connected", check_github, CMD_GITHUB,
         note="Create the private repository first (Lab 3, Deploy the Endpoint, step 2), then "
              "connect it (Lab 3, Connect GitHub, step 1)."),
    Spec(PART3, "The checker's key", check_checker, CMD_KIRO_KEY,
         note="At an AWS event the key arrives on its own (KiroKeyStatus in Event outputs); if "
              "the line above says it did not, use the fallback it names. In your own account, "
              "create a key at app.kiro.dev, then paste it at the hidden prompt of the command "
              "below (Lab 3, Check Kiro's API Key). No key at all? Step 1 of Deploy the "
              "Coordinator has a checker that needs none."),
    Spec(PART3, "The team's Runtimes", check_team_runtimes, CMD_TEAM_CODEX,
         note="The stack creates codex and kiro, and the no-key checker. Deploy the missing "
              "one with the commands below (Lab 3, Deploy the Coordinator, step 1); it builds "
              "its image first if this clone has none."),
    Spec(PART3, "The coordinator", check_coordinator, CMD_COORDINATOR,
         note="Add your rule to a SKILL.md first (step 2): the coordinator image carries it."),
    Spec(PART3, "Claude Code's grant", check_team_grants, CMD_GRANTS,
         note="Let Claude Code's console role trade its source archive through the bucket "
              "(Lab 3, Deploy the Coordinator, step 4). The stack's codex and kiro roles "
              "already can."),
    Spec(PART3, "An agent woke up", check_review,
         note="Open a pull request in your connected repository on GitHub, then watch its "
              "conversation (Lab 3, Deploy the Coordinator, step 5)."),
    Spec(PART3, "A team build", check_team_build, CMD_WATCH,
         note="Comment /develop <your idea, in one sentence> on an issue in your repository "
              "(Lab 3, Start the Team from GitHub, step 1), then watch it."),
]


def _run_all(specs: list[Spec], budget_s: float = BUDGET_S) -> list[Check]:
    results: dict[int, dict] = {}

    def work(i: int, spec: Spec) -> None:
        try:
            results[i] = spec.run()
        except Exception as exc:  # noqa: BLE001 (one broken check never hides the rest)
            results[i] = _outcome(UNKNOWN, f"check failed: {type(exc).__name__}")

    threads = [threading.Thread(target=work, args=(i, s), daemon=True) for i, s in enumerate(specs)]
    for thread in threads:
        thread.start()
    deadline = time.time() + budget_s
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    checks = []
    for i, spec in enumerate(specs):
        outcome = results.get(i) or _outcome(UNKNOWN, "no answer within the time limit")
        # A check may name the exact block for what it found missing (a page block too).
        commands = outcome.get("commands") or spec.commands
        checks.append(Check(spec.part, spec.name, outcome["state"], _mask(outcome["detail"]),
                            list(commands), spec.note))
    return checks


def next_step(checks: list[Check]) -> Check | None:
    """The first checkpoint that is not done, in workshop order."""
    return next((c for c in checks if c.state != DONE), None)


def render(checks: list[Check]) -> str:
    lines, part = [], None
    for check in checks:
        if check.part != part:
            part = check.part
            lines.append(f"\n{part}")
        lines.append(f"  {_MARK[check.state]} {check.name}: {check.detail}")
    step = next_step(checks)
    lines.append("")
    if step is None:
        lines.append("All three labs are done. Share your Play link (gallery.py status), then read Summary and Next Steps.")
        return "\n".join(lines)
    lines.append(f"NEXT ({step.part} > {step.name}):")
    if step.note:
        lines.append(f"  {step.note}")
    if step.commands:
        lines.append("")
        lines.extend(f"  {command}" for command in step.commands)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only: where you are, and the next command.")
    parser.add_argument("--json", action="store_true", help="print the checkpoints as JSON")
    args = parser.parse_args(argv)
    checks = _run_all(SPECS)
    if args.json:
        step = next_step(checks)
        print(json.dumps({"checks": [asdict(c) for c in checks],
                          "next": asdict(step) if step else None}, indent=2))
    else:
        print(render(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
