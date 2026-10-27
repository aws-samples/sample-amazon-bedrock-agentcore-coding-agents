"""Wake an agent on AgentCore Runtime when a pull request or an issue asks for one.

GitHub calls this function's HTTP endpoint for every event of the workshop App. The
function checks GitHub's signature, acts on each signed body once and only while it is
fresh (the signature carries no time), answers within a second, and hands the work to an
asynchronous copy of itself, which is never retried. Three events start work:

* A pull request is opened, reopened, updated, or marked ready: a **reviewer** session.
* A trusted person comments ``/fix`` on a pull request: a **fixer** session, which opens
  a follow-up pull request against that branch. That pull request is an event too, so a
  different reviewer session reviews the fix: the maker is never the checker.
* The repository owner comments ``/develop <idea>`` on an issue: the **coordinator's
  team**. The copy starts one NEW session of the deployed coordinator Runtime
  (``CodingAgents_orchestrator``) with the idea. The coordinator answers with a run ID at
  once and builds in that session's microVM after this function returns, so the copy
  never stops that session. Each builder's pull request is judged by its own executed
  check and review, so this endpoint does not review it a second time. Only the
  repository owner can start a team, never a bot: the taught coordinator merges green
  work on its own, so a member's or collaborator's ``/develop`` is answered and starts
  nothing.

For a review or a fix the copy:

1. finds the attendee's Claude Code Runtime (``claude_code``) and the GitHub Gateway,
2. posts "an agent woke up" on the pull request,
3. reads the change through the Gateway: the diff, the changed files at the head commit,
   and the repository's review rules (``REVIEW.md`` on the default branch, so a pull request
   cannot rewrite the rules it is judged by),
4. starts a NEW Runtime session (its own microVM) with that bundle; the agent may run the
   code there. That is containment, not a sandbox: the session keeps the Runtime's role,
   the shared mount, and the internet, which is why only a branch of this repository or a
   pull request from someone GitHub associates with it is reviewed,
5. posts the result back through the Gateway.

The GitHub credential never enters the Runtime: the Gateway's GitHub tools hold the App
key, this function reads only the webhook secret, and the session receives files, not a
token. A ``/develop`` session receives the idea and where it was asked; the coordinator
reaches GitHub through the same Gateway.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import itertools
import json
import os
import re
import shlex
import tarfile
import time
import urllib.error
import urllib.request
import uuid

# The webhook secret alone (deploy-credential.sh writes it beside the App's key, which
# this function never reads: only the Gateway's GitHub tools hold the key).
SECRET_ID = "agentcore/github-mcp/webhook"
# GitHub's signature has no timestamp, so a captured delivery stays validly signed. The
# endpoint acts on each signed body once, and only while it is fresh.
REPLAY_WINDOW_S = 3600
GATEWAY_NAME = "github-mcp-gateway"
RUNTIME_NAME = "claude_code"
TARGET = "GitHubMCP"
RULES_FILE = "REVIEW.md"
ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
# The branch prefix the coordinator's engine gives every team pull request
# (orchestrator/work_items.py). Those are judged by their own gate and review.
TEAM_BUILD_BRANCH = "workshop/runs/"
# Who may ask for a fix, by GitHub's author_association: the owner, the organization's
# members, and collaborators; never a bot, so an agent's own comment cannot start another
# agent. (A member or collaborator is not necessarily someone with write access, which is
# why a fix only opens a pull request that a person merges.)
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
# Who may start a team: only the repository owner, never a bot. The taught coordinator
# merges green, approved work with no person involved, and the idea becomes both the
# builders' task and what the checker checks, so read access must not be enough. (The
# lab's repository is a personal private one, so its owner is the attendee.)
TEAM_STARTERS = {"OWNER"}
# The deployed coordinator: project CodingAgents, agent orchestrator (deploy-coordinator.sh).
COORDINATOR_NAME = "CodingAgents_orchestrator"
# A new coordinator session first starts its microVM, then answers with a run ID or a
# refusal before any agent works. This bounds that answer, not the build.
START_READ_TIMEOUT_S = 300
MAX_IDEA_CHARS = 1000
DEVELOP = re.compile(r"^/develop(?=[\s:]|$):?", re.I)
# Quotes people put around the idea, including the curly ones a phone keyboard types.
QUOTES = {'"': '"', "'": "'", "`": "`", "\u201c": "\u201d", "\u2018": "\u2019"}
WATCH = "`python3 orchestrator/watch_run.py --plain`"
# Only a NEW comment is read: an edited one arrives as `edited` and starts nothing.
NEW_COMMENT = "Post a new comment; an edited comment is not read."
USAGE = ("Post a new comment with `/develop` followed by your idea in one sentence, for "
         "example `/develop a lighthouse keeper guides ships through a storm`. An edited "
         "comment is not read.")
NOT_OWNER = ("Only the repository owner can start a team, never a bot, so this comment "
             "started nothing. Ask the owner to post the `/develop` comment. On a repository "
             "an organization owns, GitHub reports everyone as a member, so start the team "
             "from the terminal with `./ask.sh` instead.")
IDEA_REPLIES = {
    "IDEA_MISSING": f"There is no idea after `/develop`, so no team was started. {USAGE}",
    "IDEA_PLACEHOLDER": ("That is the placeholder from the workshop page, so no team was "
                         "started. Write your own idea where the angle brackets and their "
                         f"text are. {USAGE}"),
    "IDEA_TOO_LONG": (f"The idea is longer than {MAX_IDEA_CHARS} characters, so no team was "
                      f"started. Keep it to one sentence; the builders choose the rest. "
                      f"{NEW_COMMENT}"),
}
# Where the full error goes: GitHub gets a code and a fixed hint, never an AWS message
# (those carry ARNs and the account ID). Lab 3, Connect GitHub, step 2 tails this log.
SEE_LOG = "Nothing was retried; the endpoint's log line `DEVELOP_REFUSED` has the details."
# What a person can do about each answer that definitely started nothing: a refusal the
# coordinator answered with, or AgentCore turning the invocation away before it reached
# the coordinator. Any other code gets SEE_LOG.
COORDINATOR_HINTS = {
    **IDEA_REPLIES,
    "BUILD_ALREADY_RUNNING": ("A `/develop` comment starts nothing while another build is "
                              f"running. Watch it with {WATCH}. When it ends, read its "
                              "summary on its issue; post a new `/develop` comment only for "
                              "a different idea, never to retry a failed pull request."),
    "RUN_STATE_UNAVAILABLE": ("The coordinator could not read its saved runs to check that no "
                              "other build is running, so it started nothing. Check with "
                              f"{WATCH}; the coordinator's log has the cause."),
    "REPOSITORY_MISMATCH": ("The coordinator builds only the repository it was deployed "
                            "with. Comment on an issue in that repository."),
    "AccessDeniedException": ("The endpoint is not allowed to start the coordinator yet: "
                              "redeploy the endpoint stack (Lab 3, Deploy the Endpoint, step "
                              "1), then post a new `/develop` comment."),
    "ResourceNotFoundException": ("AgentCore found no coordinator to start. If "
                                  "`./deploy-coordinator.sh` is still running, wait for it to "
                                  "finish (Lab 3, Deploy the Coordinator, step 3), then post a "
                                  "new `/develop` comment."),
    **dict.fromkeys(("ThrottlingException", "ServiceQuotaExceededException",
                     "RetryableConflictException"),
                    "AgentCore turned the request away for now. Wait a minute, then post a "
                    "new `/develop` comment."),
}
# The answer was lost after the request may have reached the coordinator, which submits
# the build before it answers. Commenting again could start a second build.
MAYBE_STARTED = (f"A build may still have started: check with {WATCH} before you comment "
                 f"`/develop` again. {SEE_LOG}")
# Failures that happen before a request leaves this function: nothing reached AgentCore.
NOT_SENT = {"ConnectTimeoutError", "EndpointConnectionError", "ProxyConnectionError",
            "NoCredentialsError", "PartialCredentialsError", "NoRegionError",
            "ParamValidationError"}
RUN_ID = re.compile(r"run_[0-9]{6}_[0-9a-f]{12}")
# One InvokeAgentRuntimeCommand command allows 65,536 characters. The bundle is a
# base64 tar.gz of the diff, the changed files and the rules; the prompt takes the rest.
MAX_BUNDLE_CHARS = 52_000
MAX_DIFF_BYTES = 40_000
MAX_SOURCE_BYTES = 120_000
BEGIN, END = "===REVIEW-BEGIN===", "===REVIEW-END==="
FILES_BEGIN, FILES_END = "===FILES-BEGIN===", "===FILES-END==="
REVIEW_HEADING = "### Review from Claude Code on Amazon Bedrock AgentCore Runtime"

REVIEW_PROMPT = (
    "You are reviewing pull request #{number} \"{title}\" by {author} in {repo}, alone in a "
    "microVM that is discarded when you finish. In this folder: pr.diff (each added or "
    "unchanged line starts with its line number in the new file), src/ (the changed files as "
    "of the pull request's head commit), and REVIEW.md (the repository's review rules from "
    "its default branch; it may be empty). Apply those rules, including any about the "
    "review's tone. You may run the code in src/ to prove a problem. "
    "Write the review in GitHub Markdown: a one-sentence summary; then at most five findings, "
    "each a definite problem naming the file by its repository path (without src/) and the "
    "line, why it matters, and a concrete fix, in "
    "one or two sentences; when you ran something to prove a finding, add the command and its "
    "real output as one short inline code span; when a finding applies a REVIEW.md rule, say "
    "which. End with a line that is exactly 'Verdict: looks good' or 'Verdict: needs changes'. "
    "Skip anything you are unsure of and do not restate the diff. Print only the review."
)
FIX_PROMPT = (
    "You are fixing pull request #{number} \"{title}\" in {repo}, alone in a microVM that is "
    "discarded when you finish. In this folder: pr.diff (the change), src/ (the changed files "
    "as of its head commit), LATEST-REVIEW.md (the latest review of it), REVIEW.md (the "
    "repository's review rules; it may be empty), and REQUEST.md (what the person asked for, "
    "if anything). Fix the "
    "problems the review lists by editing files under src/ only, keep everything else the "
    "same, and run the code to check your fix. Then print a short Markdown list with one line "
    "per file you changed and what you changed. Print only that list."
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_session = None


def _boto():
    global _session
    if _session is None:
        import boto3
        _session = boto3.session.Session()
    return _session


def region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


def log(kind: str, **fields) -> None:
    """One line per step, so `aws logs tail` reads like a live feed of the agent waking."""
    print(f"{kind} " + json.dumps(fields, default=str), flush=True)


def response(status: int, message: str) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps({"message": message})}


def webhook_secret() -> str:
    """The secret GitHub signs deliveries with, stored by deploy-credential.sh."""
    sm = _boto().client("secretsmanager", region_name=region())
    try:
        value = json.loads(sm.get_secret_value(SecretId=SECRET_ID)["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        return ""
    return value.get("webhook_secret") or ""


def event_time(kind: str, payload: dict) -> float | None:
    """When GitHub says the thing happened: the pull request's update, or the comment."""
    from datetime import datetime
    item = payload.get("pull_request") if kind == "pull_request" else payload.get("comment")
    stamp = (item or {}).get("updated_at" if kind == "pull_request" else "created_at") or ""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class DedupeUnavailable(Exception):
    """The table of handled events could not be read or written."""


def first_time(digest: str) -> bool:
    """Record this body's digest; False when it was handled before (a redelivery or a replay)."""
    table = os.environ.get("DELIVERIES_TABLE", "")
    if not table:
        return True
    ddb = _boto().client("dynamodb", region_name=region())
    try:
        ddb.put_item(TableName=table, ConditionExpression="attribute_not_exists(id)",
                     Item={"id": {"S": digest},
                           "expires": {"N": str(int(time.time()) + 2 * REPLAY_WINDOW_S)}})
    except ddb.exceptions.ConditionalCheckFailedException:
        return False
    except Exception as exc:  # noqa: BLE001 (botocore ClientError or a transport failure)
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        raise DedupeUnavailable(code) from exc
    return True


def forget(digest: str) -> None:
    """Undo first_time when the hand-off failed, so GitHub's Redeliver can try again."""
    table = os.environ.get("DELIVERIES_TABLE", "")
    if table:
        try:
            _boto().client("dynamodb", region_name=region()).delete_item(
                TableName=table, Key={"id": {"S": digest}})
        except Exception as exc:  # noqa: BLE001 (best effort; the event is already failing)
            log("FORGET_FAILED", error=type(exc).__name__)


def signature_ok(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def job_from_event(kind: str, payload: dict) -> dict | None:
    """The job an event asks for, or None when it needs no agent."""
    repo = payload.get("repository") or {}
    where = {"owner": (repo.get("owner") or {}).get("login", ""), "repo": repo.get("name", ""),
             "default_branch": repo.get("default_branch", "")}
    if kind == "pull_request":
        if payload.get("action") not in ACTIONS:
            return None
        pr = payload.get("pull_request") or {}
        if pr.get("draft") or pr.get("state") != "open":
            return None
        # A team build's pull request (Lab 3's coordinator, branch workshop/runs/...)
        # already carries its own executed check and independent review. A second,
        # REVIEW.md-based verdict beside them would blur which one decides.
        if str((pr.get("head") or {}).get("ref", "")).startswith(TEAM_BUILD_BRANCH):
            return None
        # The reviewer runs the change with the Runtime's role, the shared mount, and the
        # internet, so only code the repository trusts reaches a session: a branch of this
        # repository (whoever pushed it had write access, including the App that opens the
        # fixer's pull request) or a fork by someone GitHub associates with it.
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or repo.get("full_name")
        same_repo = bool(head_repo) and head_repo == base_repo  # a deleted fork has no head repo
        if not same_repo and pr.get("author_association") not in TRUSTED:
            return None
        return {"mode": "review", **where, "number": pr["number"], "title": pr.get("title", ""),
                "author": (pr.get("user") or {}).get("login", ""), "sha": pr["head"]["sha"]}
    if kind == "issue_comment":
        issue, comment = payload.get("issue") or {}, payload.get("comment") or {}
        body = (comment.get("body") or "").strip()
        user = comment.get("user") or {}
        association = comment.get("author_association")
        trusted = user.get("type") != "Bot" and association in TRUSTED
        if payload.get("action") == "created" and DEVELOP.match(body):
            # The team's own comments (the App is a Bot) can never start another team, and
            # neither a bot nor a stranger is answered. A member or collaborator is told on
            # GitHub that only the owner can start a team; a /develop on a pull request is
            # answered there. Neither job starts a team.
            if not trusted:
                return None
            return {"mode": "develop", **where, "number": issue["number"],
                    "title": issue.get("title", ""), "author": user.get("login", ""),
                    "idea": idea_from(body), "comment_url": comment.get("html_url", ""),
                    "on_pull_request": "pull_request" in issue,
                    "by_owner": association in TEAM_STARTERS}
        if (payload.get("action") != "created" or "pull_request" not in issue
                or not body.lower().startswith("/fix")):
            return None
        if not trusted:
            return None
        return {"mode": "fix", **where, "number": issue["number"], "title": issue.get("title", ""),
                "author": user.get("login", ""), "request": body[4:].strip()}
    return None


def idea_from(body: str) -> str:
    """What follows ``/develop``: stripped, without one pair of quotes around all of it."""
    idea = DEVELOP.sub("", body, count=1).strip()
    if len(idea) >= 2 and QUOTES.get(idea[0]) == idea[-1] and idea[0] not in idea[1:-1]:
        idea = idea[1:-1].strip()
    return idea


def idea_problem(idea: str) -> str | None:
    """Why this idea cannot start a team, or None. The codes match the coordinator's."""
    if not idea:
        return "IDEA_MISSING"
    if re.fullmatch(r"<[^<>]*>", idea) or "<your idea" in idea.lower():
        return "IDEA_PLACEHOLDER"
    if len(idea) > MAX_IDEA_CHARS:
        return "IDEA_TOO_LONG"
    return None


def handler(event, context):
    if "job" in event or "review" in event:
        job = event.get("job") or {"mode": "review", **event["review"]}
        if job.get("mode") == "develop":
            return develop(job)
        return fix(job) if job.get("mode") == "fix" else review(job)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    secret = webhook_secret()
    if not secret:
        return response(503, "No webhook secret yet: connect the repository first.")
    if not signature_ok(secret, raw, headers.get("x-hub-signature-256", "")):
        log("REJECTED", reason="signature")
        return response(401, "Signature does not match this App's webhook secret.")
    kind = headers.get("x-github-event", "")
    if kind == "ping":
        return response(200, "pong")
    payload = json.loads(raw or b"{}")
    job = job_from_event(kind, payload)
    if job is None:
        return response(202, f"Ignored {kind} event.")
    happened = event_time(kind, payload)
    if happened is None or time.time() - happened > REPLAY_WINDOW_S:
        log("REJECTED", reason="stale", event=kind)
        return response(202, "Ignored: older than the replay window.")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        fresh = first_time(digest)
    except DedupeUnavailable as exc:
        log("REJECTED", reason="dedupe unavailable", error=str(exc), event=kind)
        return response(503, "Could not record the event; ask GitHub to redeliver it.")
    if not fresh:
        log("REJECTED", reason="already handled", event=kind)
        return response(202, "Ignored: this event was already handled.")
    job["received_at"] = time.time()
    log("EVENT", event=kind, action=payload.get("action"), repo=f"{job['owner']}/{job['repo']}",
        number=job["number"], mode=job["mode"])
    try:
        _boto().client("lambda", region_name=region()).invoke(
            FunctionName=context.invoked_function_arn, InvocationType="Event",
            Payload=json.dumps({"job": job}).encode())
    except Exception:
        forget(digest)
        raise
    if job["mode"] == "develop":
        if not job["by_owner"]:
            return response(202, "Answering the comment: only the repository owner can start a team.")
        return response(202, "Answering on the pull request: /develop starts a team from an issue."
                        if job["on_pull_request"] else "An agent team is waking up for this issue.")
    verb = "fix" if job["mode"] == "fix" else "review"
    return response(202, f"An agent is waking up to {verb} this pull request.")


# ---------------- Gateway (SigV4-signed MCP JSON-RPC) ----------------

def gateway_url() -> str:
    control = _boto().client("bedrock-agentcore-control", region_name=region())
    token = None
    while True:
        page = control.list_gateways(**({"nextToken": token} if token else {}))
        for item in page.get("items", []):
            if item.get("name") == GATEWAY_NAME:
                return control.get_gateway(gatewayIdentifier=item["gatewayId"])["gatewayUrl"]
        token = page.get("nextToken")
        if not token:
            raise RuntimeError(f"Gateway {GATEWAY_NAME} not found; run the connect step first")


def tool(url: str, name: str, arguments: dict, *, raw: bool = False):
    """Call one GitHub tool through the Gateway. ``raw`` keeps a text result as text,
    so a file that happens to be JSON is not parsed."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": f"{TARGET}___{name}", "arguments": arguments}}).encode()
    creds = _boto().get_credentials().get_frozen_credentials()
    request = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json, text/event-stream"})
    SigV4Auth(creds, "bedrock-agentcore", region()).add_auth(request)
    req = urllib.request.Request(url, data=body, headers=dict(request.headers), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.loads(r.read().decode() or "{}")
    if "error" in payload:
        raise RuntimeError(f"{name}: {payload['error']}")
    result = payload.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"{name}: {json.dumps(result.get('content'))[:300]}")
    for block in result.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            if text.lower().startswith(("error calling tool", "error executing tool")):
                raise RuntimeError(f"{name}: {text[:300]}")
            if raw:
                return text
            try:
                return json.loads(text)
            except ValueError:
                return text
    return result


# ---------------- What the session receives ----------------

class RuntimeMissing(RuntimeError):
    """No Runtime of that name in this account and Region yet."""


def runtime_arn(name: str = RUNTIME_NAME) -> str:
    control = _boto().client("bedrock-agentcore-control", region_name=region())
    token = None
    while True:
        page = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for item in page.get("agentRuntimes", []):
            if item.get("agentRuntimeName") == name:
                return item["agentRuntimeArn"]
        token = page.get("nextToken")
        if not token:
            first = ("deploy Claude Code first" if name == RUNTIME_NAME
                     else "run ./deploy-coordinator.sh first")
            raise RuntimeMissing(f"Runtime {name} not found; {first}")


def number_patch(patch: str) -> str:
    """Prefix each added or unchanged line with its line number in the new file."""
    out, line_no = [], None
    for line in patch.splitlines():
        hunk = HUNK.match(line)
        if hunk:
            line_no = int(hunk.group(1))
            out.append(line)
        elif line_no is None or line.startswith("\\"):
            out.append(line)
        elif line.startswith("-"):
            out.append(f"     {line}")
        else:
            out.append(f"{line_no:4d} {line}")
            line_no += 1
    return "\n".join(out)


def build_diff(pr: dict) -> str:
    parts = []
    for f in pr.get("files", []):
        header = f"diff --git a/{f['filename']} b/{f['filename']}  ({f['status']}, +{f['additions']} -{f['deletions']})"
        patch = f.get("patch")
        parts.append(header + "\n" + (number_patch(patch) if patch else "(no patch: binary or too large)"))
    diff = "\n\n".join(parts)
    data = diff.encode()
    if len(data) > MAX_DIFF_BYTES:
        diff = data[:MAX_DIFF_BYTES].decode(errors="ignore") + "\n\n[diff truncated: only the first 40 KB was reviewed]"
    return diff


def gather(url: str, job: dict, pr: dict) -> tuple[dict[str, str], str]:
    """The changed files at the head commit (bounded) and the repository's rules.

    Rules come from the DEFAULT branch: a pull request cannot rewrite the rules it is
    judged by until it merges, and a fix pull request stacked on a feature branch (the
    one /fix opens) is judged by the same rules as the pull request it fixes."""
    where = {"owner": job["owner"], "repo": job["repo"]}
    files, total = {}, 0
    for f in pr.get("files", []):
        if f.get("status") == "removed":
            continue
        try:
            text = tool(url, "get_file", {**where, "path": f["filename"], "ref": pr["head_sha"]}, raw=True)
        except Exception:  # noqa: BLE001 (binary, too large, or unreadable: the diff still has it)
            continue
        if not isinstance(text, str) or total + len(text.encode()) > MAX_SOURCE_BYTES:
            continue
        size = len(text.encode())
        files[f["filename"]] = text
        total += size
    try:
        ref = job.get("default_branch") or pr["base"]
        rules = tool(url, "get_file", {**where, "path": RULES_FILE, "ref": ref}, raw=True)
    except Exception:  # noqa: BLE001 (no rules file is the common case)
        rules = ""
    return files, rules if isinstance(rules, str) else ""


def bundle(entries: dict[str, str]) -> str:
    """A base64 tar.gz of {path: text}, the one argument a session needs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in entries.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def fit_bundle(diff: str, files: dict[str, str], extras: dict[str, str]) -> tuple[str, list[str]]:
    """Bundle everything, dropping the largest source files until it fits one command."""
    kept = dict(files)
    dropped = []
    while True:
        entries = {"pr.diff": diff, **extras, **{f"src/{p}": t for p, t in kept.items()}}
        encoded = bundle(entries)
        if len(encoded) <= MAX_BUNDLE_CHARS or not kept:
            return encoded, dropped
        largest = max(kept, key=lambda p: len(kept[p]))
        dropped.append(largest)
        del kept[largest]


def build_command(job: dict, encoded: str, mode: str = "review") -> str:
    template = FIX_PROMPT if mode == "fix" else REVIEW_PROMPT
    prompt = template.format(number=job["number"], title=job["title"].replace('"', "'"),
                             author=job.get("author", ""), repo=f"{job['owner']}/{job['repo']}")
    script = (
        "mkdir -p /tmp/work/src && cd /tmp/work && "
        f"echo {encoded} | base64 -d | tar xzf - && "
        f"echo {BEGIN} && WORKSHOP_AGENT_WORKDIR=/tmp/work /app/run.sh --print {shlex.quote(prompt)}; "
        f"echo {END}"
    )
    if mode == "fix":
        script += f"; echo {FILES_BEGIN}; tar czf - src | base64 -w0; echo; echo {FILES_END}"
    return "sh -c " + shlex.quote(script)


# A session's printed AWS errors carry role ARNs and the account ID; neither belongs on
# GitHub, so an excerpt posted there is redacted first (the log keeps the exit code).
_ARN = re.compile(r"arn:aws[a-z-]*:[^\s'\"`]*")
_ACCOUNT = re.compile(r"(?<![\w])\d{12}(?![\w])")


def redact(text: str) -> str:
    return _ACCOUNT.sub("<account>", _ARN.sub("<arn>", text or ""))


def extract_review(stdout: str) -> str:
    if BEGIN not in stdout:
        return ""
    return stdout.split(BEGIN, 1)[1].split(END, 1)[0].strip()


def extract_files(stdout: str, limit: int = 4_000_000) -> dict[str, str]:
    """The text files the fixer left under src/, keyed by repository path."""
    if FILES_BEGIN not in stdout:
        return {}
    encoded = stdout.split(FILES_BEGIN, 1)[1].split(FILES_END, 1)[0].strip()
    if not encoded or len(encoded) > limit:
        return {}
    out = {}
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith("src/") or ".." in member.name:
                continue
            data = tar.extractfile(member).read()
            try:
                out[member.name[len("src/"):]] = data.decode()
            except UnicodeDecodeError:
                continue
    return out


def run_session(arn: str, session: str, command: str) -> tuple[str, int | None]:
    from botocore.config import Config
    client = _boto().client("bedrock-agentcore", region_name=region(),
                            config=Config(read_timeout=840, retries={"max_attempts": 1}))
    resp = client.invoke_agent_runtime_command(
        agentRuntimeArn=arn, runtimeSessionId=session,
        body={"command": command, "timeout": 600})
    out, code = [], None
    for event in resp["stream"]:
        chunk = event.get("chunk") or {}
        if "contentDelta" in chunk:
            out.append(chunk["contentDelta"].get("stdout") or "")
        if "contentStop" in chunk:
            code = chunk["contentStop"].get("exitCode")
    try:
        client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session)
    except Exception:  # noqa: BLE001 (the session may already have ended)
        pass
    return "".join(out), code


def reply_object(text: str) -> dict | None:
    """The coordinator's answer in one event's data, or None when the data is not it.

    The coordinator yields one JSON string and the Runtime SDK JSON-encodes what it
    yields, so the data is that object either as it is or encoded once more."""
    value = text
    for _ in range(2):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
        if isinstance(value, dict):
            return value if ("run_id" in value or "error" in value) else None
        if not isinstance(value, str):
            return None
    return None


def first_reply(lines) -> dict:
    """The first object with ``run_id`` or ``error`` in a server-sent event stream."""
    data: list[str] = []
    for raw in itertools.chain(lines, [""]):
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).rstrip("\r")
        if line.startswith("data:"):
            data.append(line[6:] if line.startswith("data: ") else line[5:])
        elif not line and data:
            found = reply_object("\n".join(data))
            data = []
            if found is not None:
                return found
    return {}


def start_team(arn: str, session: str, event: dict) -> dict:
    """Ask the coordinator for one build and return its first answer.

    One invocation of a NEW coordinator session, with one attempt: a resent invocation
    could start a second build. The build runs in that session's microVM as an
    asynchronous task after this call returns, so the session is never stopped here;
    stopping it would end the build."""
    from botocore.config import Config
    client = _boto().client("bedrock-agentcore", region_name=region(),
                            config=Config(read_timeout=START_READ_TIMEOUT_S,
                                          retries={"total_max_attempts": 1}))
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn, runtimeSessionId=session,
        payload=json.dumps({"github_event": event}).encode(),
        contentType="application/json", accept="text/event-stream")
    body = resp["response"]
    try:
        if "event-stream" in (resp.get("contentType") or ""):
            reply = first_reply(body.iter_lines() if hasattr(body, "iter_lines")
                                else body.read().splitlines())
        else:
            reply = reply_object(body.read().decode("utf-8", "replace")) or {}
    finally:
        try:
            body.close()
        except Exception:  # noqa: BLE001 (closing early only ends this read; the build goes on)
            pass
    # The request reached the coordinator, which submits a build before it answers.
    return reply or {"error": "NO_REPLY", "detail": "the coordinator's answer had no run ID",
                     "may_have_started": True}


def start_failure(exc: Exception) -> dict:
    """The coordinator invocation itself failed: say which way, never retry it.

    ``may_have_started`` is False only when the request cannot have reached the
    coordinator: no connection was made, or AgentCore refused the request itself (a 4xx,
    except RuntimeClientError, which reports the Runtime's own failure). A read timeout,
    a broken stream, a 5xx, or anything unknown may have come after the build started.
    ``detail`` is the full message, for the log only: it can carry ARNs and the account."""
    name = type(exc).__name__
    if name == "ReadTimeoutError":
        return {"error": "COORDINATOR_NO_REPLY", "detail": f"no answer within {START_READ_TIMEOUT_S} s",
                "may_have_started": True}
    response = getattr(exc, "response", None)
    response = response if isinstance(response, dict) else {}
    error = response.get("Error") or {}
    code = str(error.get("Code") or name)
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    turned_away = (isinstance(status, int) and 400 <= status < 500
                   and code != "RuntimeClientError" and name != "EventStreamError")
    return {"error": code, "detail": str(error.get("Message") or exc)[:2000],
            "may_have_started": not (name in NOT_SENT or turned_away)}


# ---------------- The three jobs ----------------

def review(job: dict) -> dict:
    started = time.time()
    url = gateway_url()
    where = {"owner": job["owner"], "repo": job["repo"], "issue_number": job["number"]}
    session = f"pr-review-{job['number']}-{uuid.uuid4()}"
    tool(url, "comment_on_issue", {**where, "body": (
        "**An agent just woke up** to review this pull request, in its own microVM "
        f"(session `{session[-12:]}`). Nobody started it by hand: the pull request did. "
        "Its review will appear below in a minute or two.")})
    pr = tool(url, "get_pull_request_files",
              {"owner": job["owner"], "repo": job["repo"], "number": job["number"]})
    files, rules = gather(url, job, pr)
    encoded, dropped = fit_bundle(build_diff(pr), files, {RULES_FILE: rules})
    log("SESSION", mode="review", number=job["number"], session=session, files=len(files) - len(dropped),
        rules=bool(rules))
    stdout, code = run_session(runtime_arn(), session, build_command(job, encoded))
    text = extract_review(stdout)
    seconds = time.time() - job.get("received_at", started)
    rules_note = (f" It applied the rules in `{RULES_FILE}` on `{job.get('default_branch') or pr['base']}`."
                  if rules else "")
    if code == 0 and text:
        body = (f"{REVIEW_HEADING}\n\n{text}\n\n---\n"
                f"Reviewed commit `{job['sha'][:7]}` in a new session of its own (`{session[-12:]}`), "
                f"{seconds:.0f} s after the pull request event. The agent received the diff and "
                f"{len(files) - len(dropped)} changed file(s) and could run them; it never held a "
                f"GitHub token.{rules_note} Comment `/fix` to have another agent propose a fix.")
    else:
        body = (f"The review session ended without a review (exit {code}). The agent's last "
                f"output:\n\n```\n{redact(stdout[-1500:])}\n```")
    tool(url, "comment_on_issue", {**where, "body": body})
    result = {"repo": f"{job['owner']}/{job['repo']}", "number": job["number"], "exit": code,
              "reviewed": bool(code == 0 and text), "seconds": round(seconds, 1), "session": session,
              "rules": bool(rules)}
    # One log line per finished review; progress.py reads it to report the review step.
    log("REVIEW_POSTED", **result)
    return result


def latest_review(comments: list) -> str:
    for c in reversed(comments or []):
        if (c.get("body") or "").startswith(REVIEW_HEADING):
            return c["body"]
    return ""


def fix(job: dict) -> dict:
    started = time.time()
    url = gateway_url()
    where = {"owner": job["owner"], "repo": job["repo"], "issue_number": job["number"]}
    pr = tool(url, "get_pull_request_files",
              {"owner": job["owner"], "repo": job["repo"], "number": job["number"]})
    # A team build's pull request belongs to the coordinator's engine: its own executed
    # check decides it, and the engine's one bounded repair commits onto this same branch,
    # which a fixer's pushed branch would conflict with.
    if str(pr.get("head_ref") or "").startswith(TEAM_BUILD_BRANCH):
        tool(url, "comment_on_issue", {**where, "body": (
            "This pull request belongs to a team build, so `/fix` does not run here: the "
            "team's own executed check decides it, and the coordinator makes at most one "
            "bounded repair on this branch. Read the check evidence in the comments, then "
            "merge or close it yourself.")})
        log("FIX_REFUSED", number=job["number"], reason="team build")
        return {"number": job["number"], "fixed": False, "reason": "team_build"}
    if not pr.get("same_repo"):
        tool(url, "comment_on_issue", {**where, "body": (
            "This pull request comes from a fork, so the workshop App cannot push a fix branch "
            "for it. Push the fix from the fork instead.")})
        return {"number": job["number"], "fixed": False, "reason": "fork"}
    review_text = latest_review(tool(url, "list_issue_comments", where))
    session = f"pr-fix-{job['number']}-{uuid.uuid4()}"
    tool(url, "comment_on_issue", {**where, "body": (
        f"**An agent just woke up** to fix this pull request, in its own microVM (session "
        f"`{session[-12:]}`), because @{job['author']} asked. It works from the latest review "
        "and opens a separate pull request against this branch; a different agent then reviews "
        "that fix.")})
    files, rules = gather(url, job, pr)
    extras = {RULES_FILE: rules, "LATEST-REVIEW.md": review_text, "REQUEST.md": job.get("request", "")}
    encoded, dropped = fit_bundle(build_diff(pr), files, extras)
    sent = {p: t for p, t in files.items() if p not in dropped}
    log("SESSION", mode="fix", number=job["number"], session=session, files=len(sent))
    stdout, code = run_session(runtime_arn(), session, build_command(job, encoded, mode="fix"))
    summary = extract_review(stdout)
    changed = [{"path": p, "content": t} for p, t in extract_files(stdout).items() if sent.get(p) != t]
    seconds = time.time() - job.get("received_at", started)
    if code != 0 or not changed:
        reason = "it changed no file" if code == 0 else f"exit {code}"
        tool(url, "comment_on_issue", {**where, "body": (
            f"The fix session ended without a fix ({reason}). Its last output:\n\n"
            f"```\n{redact(stdout[-1500:] if code else summary[-1500:])}\n```")})
        log("FIX_FAILED", number=job["number"], exit=code, session=session)
        return {"number": job["number"], "fixed": False, "exit": code}
    branch = f"agent-fix-{job['number']}-{session[-6:]}"
    tool(url, "create_branch", {"owner": job["owner"], "repo": job["repo"], "branch": branch,
                                "from_branch": pr["head_ref"]})
    tool(url, "put_files", {"owner": job["owner"], "repo": job["repo"], "branch": branch,
                            "files": changed, "message": f"Fix the review findings on #{job['number']}"})
    opened = tool(url, "create_pull_request", {
        "owner": job["owner"], "repo": job["repo"], "head": branch, "base": pr["head_ref"],
        "title": f"Fix the review findings on #{job['number']}",
        "body": (f"{summary}\n\n---\nWritten by a fixer session (`{session[-12:]}`) on Amazon "
                 f"Bedrock AgentCore Runtime, {seconds:.0f} s after @{job['author']} asked on "
                 f"#{job['number']}. A different session reviews this pull request; merge it into "
                 f"`{pr['head_ref']}` when you agree.")})
    tool(url, "comment_on_issue", {**where, "body": (
        f"Opened #{opened['number']} with the proposed fix ({len(changed)} file(s)). A different "
        "agent is reviewing it now.")})
    result = {"repo": f"{job['owner']}/{job['repo']}", "number": job["number"], "fix_pr": opened["number"],
              "files": len(changed), "seconds": round(seconds, 1), "session": session}
    log("FIX_OPENED", **result)
    return result


def develop(job: dict) -> dict:
    """Start the coordinator's team for a /develop comment, and say so on the issue."""
    started = time.time()
    url = gateway_url()
    where = {"owner": job["owner"], "repo": job["repo"], "issue_number": job["number"]}
    result = {"repo": f"{job['owner']}/{job['repo']}", "number": job["number"]}

    def refuse(reason: str, body: str, **extra) -> dict:
        tool(url, "comment_on_issue", {**where, "body": body})
        log("DEVELOP_REFUSED", **result, reason=reason, **extra)
        return {**result, "started": False, "reason": reason, **extra}

    if not job.get("by_owner"):
        return refuse("NOT_OWNER", NOT_OWNER, author=job.get("author", ""))
    if job.get("on_pull_request"):
        return refuse("ON_PULL_REQUEST", "Comment /develop on an issue, not a pull request. "
                      "Nothing was started.")
    idea = job.get("idea") or ""
    problem = idea_problem(idea)
    if problem:
        return refuse(problem, IDEA_REPLIES[problem])
    try:
        arn = runtime_arn(COORDINATOR_NAME)
    except RuntimeMissing:
        return refuse("COORDINATOR_MISSING", (
            "No coordinator yet: run `./deploy-coordinator.sh` (Lab 3, Deploy the Coordinator, "
            "step 3), then post a new `/develop` comment with your idea. Nothing was started."))
    session = f"gh-develop-{job['number']}-{uuid.uuid4()}"
    author = job.get("author", "")
    tool(url, "comment_on_issue", {**where, "body": (
        f"**An agent team woke up.** `{author}` asked for a build on this issue, so the "
        "coordinator on Amazon Bedrock AgentCore Runtime is starting one in a new session of "
        f"its own (`{session[-12:]}`). No one ran a command: the comment did. Its run ID "
        "follows in a moment.")})
    log("SESSION", mode="develop", number=job["number"], session=session)
    event = {"kind": "develop", "owner": job["owner"], "repo": job["repo"],
             "issue_number": job["number"], "author": author, "idea": idea,
             "comment_url": job.get("comment_url", "")}
    try:
        reply = start_team(arn, session, event)
    except Exception as exc:  # noqa: BLE001 (any failed invocation is reported, not retried)
        reply = start_failure(exc)
    seconds = round(time.time() - job.get("received_at", started), 1)
    run_id = str(reply.get("run_id") or "").replace("`", "")
    if run_id and not reply.get("error"):
        agents = ", ".join(f"`{str(a).replace('`', '')}`" for a in reply.get("agents") or [])
        preset = str(reply.get("preset") or "").replace("`", "")
        plan = (f" with {agents}" if agents else "") + (f" (preset `{preset}`)" if preset else "")
        tool(url, "comment_on_issue", {**where, "body": (
            f"The coordinator started build `{run_id}`{plan}. Each builder opens its own pull "
            "request in this repository; a different agent's executed check and an independent "
            "review decide each one, and the coordinator posts a summary on this issue when the "
            f"build ends. Watch it with {WATCH} in `~/sample-amazon-bedrock-agentcore-coding-agents`.")})
        log("DEVELOP_STARTED", **result, run_id=run_id, session=session, seconds=seconds,
            agents=reply.get("agents") or [], preset=reply.get("preset", ""))
        return {**result, "started": True, "run_id": run_id, "session": session, "seconds": seconds}
    error = str(reply.get("error") or "NO_REPLY")
    if "error_type" not in reply and re.fullmatch(r"[A-Z][A-Za-z0-9_]*(?::[\w.-]+)?", error):
        # A coordinator refusal (it starts nothing), or the invocation's own failure,
        # which start_failure and start_team mark when a build may have started.
        code, detail = error, reply.get("detail")
        maybe = bool(reply.get("may_have_started"))
    else:  # the Runtime SDK's own error event: the entrypoint raised, maybe after submitting
        code, detail = str(reply.get("error_type") or "COORDINATOR_ERROR"), error
        maybe = True
    code = re.sub(r"[^\w.:-]", "", code)[:80] or "COORDINATOR_ERROR"
    # The detail stays in this function's log: an AWS message names ARNs and the account.
    detail = " ".join(str(detail or "").split())[:2000]
    if maybe:
        lead = (f"The coordinator did not answer within {START_READ_TIMEOUT_S} s"
                if code == "COORDINATOR_NO_REPLY"
                else "The endpoint could not read the coordinator's answer")
        body = f"{lead}: `{code}`. {MAYBE_STARTED}"
    else:
        running = str(reply.get("running_run_id") or "")
        seen = f"It sees `{running}` still running. " if RUN_ID.fullmatch(running) else ""
        body = (f"The coordinator did not start a build: `{code}`. {seen}"
                + COORDINATOR_HINTS.get(code.split(":", 1)[0], SEE_LOG))
    return refuse(code, body, session=session, detail=detail, may_have_started=maybe)
