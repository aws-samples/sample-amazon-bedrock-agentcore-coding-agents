"""GitHub role-PR queue through the GitHub MCP Gateway (no PAT anywhere).

The workshop's final goal is a pull request on the attendee's own GitHub. In this
model the ONLY GitHub credential is a **GitHub App installation**, held inside the
GitHub MCP Runtime that fronts an IAM-authenticated AgentCore Gateway. The
The orchestrator gives every builder a branch and PR against a run-private
integration branch. It publishes each patch atomically, comments executable
evidence on the role PRs, and merges reviewed heads through a queue. After every
role merge the validator authors and runs a fresh executable check. Only the
fully validated integration branch opens a human-review PR to the repository's
current default branch. The Gateway reads that branch; this module never changes
repository settings.

The credential LADDER is now a Gateway config, not a token:

  1. env: ``GITHUB_GATEWAY_URL`` (+ ``GITHUB_REPO`` target, optional
     ``GITHUB_GATEWAY_TARGET``)                      (CI / CFN-provisioned event)
  2. the console Settings pane: the attendee pastes their template-derived repo
     ``owner/name`` (NO token); the gateway URL is wired by the workshop.
     Persisted to a gitignored ``.runs/github_gateway.local.json``.
  3. Neither: the PR step fails LOUD with ``PR_NO_GATEWAY`` and ``pr_url`` stays
     null (never a fake URL, never a silent local-commit substitute).

Nothing here is a secret (a gateway URL and an ``owner/repo`` are not credentials;
the App private key lives only in the MCP Runtime's Secrets Manager). We keep the
0600 file discipline anyway so the config surface mirrors the old one and there is
one place to look. All Gateway calls are SigV4-signed stdlib ``urllib`` against the
gateway URL, service ``bedrock-agentcore``; boto3/botocore are imported lazily and
only on the signing path.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_RUNS_DIR = os.environ.get("WORKSHOP_RUNS_DIR", os.path.join(_REPO_ROOT, ".runs"))
# The gateway config file is independently wirable (WORKSHOP_GITHUB_SETTINGS) so
# tests point it at an empty tmp file (they must NEVER read a developer's real
# wired gateway and open real PRs) WITHOUT relocating the shared compose repo under
# _RUNS_DIR. Defaults next to the other run state. (The env var name is kept for
# back-compat with the e2e GitHub-leak isolation fixture.)
_SETTINGS = os.environ.get("WORKSHOP_GITHUB_SETTINGS",
                           os.path.join(_RUNS_DIR, "github_gateway.local.json"))
_FINAL_MERGE_POLICY_FILE = os.path.join(
    _RUNS_DIR, "final_merge_policy.local.json")
_COMPOSED = os.path.join(_RUNS_DIR, "composed")

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# The Gateway target name the deploy script creates (deploy-gateway.sh:
# TARGET_NAME="GitHubMCP"). Gateway namespaces every tool as ``<target>___<tool>``.
_DEFAULT_TARGET = "GitHubMCP"
# The canonical workshop TEMPLATE repository: the public code repo itself is a
# GitHub template. Attendees click "Use this template" on it to get an ISOLATED
# per-attendee working repo (no fork, no shared credential). Override with
# WORKSHOP_REPO.
WORKSHOP_REPO = os.environ.get(
    "WORKSHOP_REPO",
    "aws-samples/sample-amazon-bedrock-agentcore-coding-agents")

# git subprocess hardening: pin config to /dev/null so a planted ~/.gitconfig
# (e.g. a malicious credential helper) is never read, and never prompt.
_GIT_TRACE_VARS = ("GIT_TRACE", "GIT_TRACE_PACKET", "GIT_TRACE_PERFORMANCE",
                   "GIT_TRACE_SETUP", "GIT_CURL_VERBOSE", "GIT_TRACE_CURL")


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_TRACE_VARS}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# Idempotent branch used only to prove the GitHub App can write before a build.
# Actual work always uses ``workshop/runs/<run_id>/integration``.
DOCTOR_BRANCH = "workshop/doctor"
FINAL_MERGE_POLICIES = ("human_review", "auto")
_DEFAULT_FINAL_MERGE_POLICY = "human_review"


# --- Gateway config resolution ------------------------------------------------

def _ambient_region() -> str:
    """The session's real region from boto3 (config file, then IMDS), or "".

    Used only when neither the gateway URL nor the environment names one, so the
    workshop host still signs for its own region instead of a hardcoded guess."""
    try:
        import boto3  # noqa: PLC0415 (lazy, mirrors the other AWS seams)
        return boto3.session.Session().region_name or ""
    except Exception:  # noqa: BLE001 (no SDK: caller surfaces the empty region)
        return ""


def _region_from_url(url: str) -> str:
    """The region to SigV4-sign for: the gateway's own
    (…bedrock-agentcore.<region>.…), else the ambient AWS region.

    The URL wins because the signature must match the endpoint being called. No
    literal fallback: signing for a region the gateway is not in fails the
    request, and guessing one region for every attendee is how that happens."""
    m = re.search(r"\.([a-z]{2}-[a-z]+-\d)\.", url or "")
    if m:
        return m.group(1)
    return (os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or _ambient_region())


def _load_config_file() -> dict:
    try:
        with open(_SETTINGS, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


# Where the GitHub MCP Gateway deploy writes its state (gateway_mcp/config.sh:
# STATE_FILE=.deployed-state.json next to the deploy scripts). The attendee runs
# gateway_mcp/deploy-all.sh in Stage 2 ON THIS BOX, so github.py can AUTO-DISCOVER
# the gateway URL from that file: no URL to paste, no CFN param. Overridable with
# WORKSHOP_GATEWAY_STATE for local dev / tests.
def _gateway_state_path() -> str:
    return os.environ.get(
        "WORKSHOP_GATEWAY_STATE",
        os.path.join(_REPO_ROOT, "coding-agents", "gateway_mcp", ".deployed-state.json"))


def _discover_gateway_url() -> str:
    """Read gateway_url from the gateway deploy's state file, or '' if not deployed."""
    try:
        with open(_gateway_state_path(), encoding="utf-8") as f:
            return (json.load(f).get("gateway_url") or "").strip()
    except (OSError, ValueError):
        return ""


def _gateway_config() -> dict | None:
    """Resolve the gateway config down the ladder, or None when nothing is wired.

    Returns ``{gateway_url, repo, target, region, source}`` when a
    gateway URL AND a target repo are both known; otherwise None. The gateway URL
    resolves: ``GITHUB_GATEWAY_URL`` env -> the Settings file -> AUTO-DISCOVERED from
    the gateway deploy's ``.deployed-state.json``. The repo resolves: ``GITHUB_REPO``
    env -> the Settings file. So the common attendee flow is zero-paste: deploy the
    gateway (writes the state file) and set the repo in Settings.
    """
    file = _load_config_file()
    gateway_url = (os.environ.get("GITHUB_GATEWAY_URL")
                   or file.get("gateway_url")
                   or _discover_gateway_url() or "").strip()
    repo = (os.environ.get("GITHUB_REPO") or file.get("repo") or "").strip()
    if not gateway_url or not _REPO_RE.match(repo):
        return None
    target = (os.environ.get("GITHUB_GATEWAY_TARGET")
              or file.get("target") or _DEFAULT_TARGET).strip()
    if os.environ.get("GITHUB_GATEWAY_URL"):
        source = "environment"
    elif file.get("gateway_url"):
        source = "settings"
    else:
        source = "discovered"
    return {
        "gateway_url": gateway_url,
        "repo": repo,
        "target": target,
        "region": (file.get("region") or _region_from_url(gateway_url)),
        "source": source,
    }


# --- Gateway MCP transport (SigV4-signed JSON-RPC) ----------------------------

class GatewayError(RuntimeError):
    """A JSON-RPC error or transport failure calling a Gateway MCP tool."""


def _sigv4_headers(url: str, body: bytes, region: str) -> dict[str, str]:
    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415
    import botocore.session  # noqa: PLC0415

    creds = botocore.session.get_session().get_credentials().get_frozen_credentials()
    aws_req = AWSRequest(method="POST", url=url, data=body,
                         headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "bedrock-agentcore", region).add_auth(aws_req)
    return dict(aws_req.headers)


_RPC_ID = 0
_RPC_ID_LOCK = threading.Lock()

# One transient network failure must not cost an attendee a role or integration PR.
# The queue performs several archive, branch, commit, comment, and merge calls, so a
# single reset or 503 must receive a bounded transport retry.
_RPC_ATTEMPTS = 3
_RPC_BACKOFF_S = 2.0
# Retry ONLY what is genuinely transient. A 4xx is the gateway or GitHub telling us the
# request is wrong (bad repo, missing permission, protected branch) and retrying it
# hides a real answer behind a slower failure.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _gateway_rpc(cfg: dict, method: str, params: dict, timeout: float = 30.0) -> Any:
    """POST one SigV4-signed JSON-RPC call to the gateway. Returns ``result`` or
    raises GatewayError on a JSON-RPC error / transport failure.

    Transient transport failures are retried (see ``_RPC_ATTEMPTS``); a 4xx or a
    JSON-RPC error is returned immediately, because those are answers, not blips.
    """
    global _RPC_ID
    with _RPC_ID_LOCK:
        # Increment and CAPTURE under the lock: read-then-serialize is not atomic, so
        # concurrent runs could otherwise put the same id on two different requests.
        _RPC_ID += 1
        rpc_id = _RPC_ID
    body = json.dumps({"jsonrpc": "2.0", "method": method,
                       "id": rpc_id, "params": params}).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    last: Exception | None = None
    for attempt in range(_RPC_ATTEMPTS):
        call_headers = dict(headers)
        try:
            # Re-sign per attempt: a SigV4 signature carries a timestamp and a retry
            # after a backoff can fall outside the accepted skew window.
            call_headers.update(_sigv4_headers(cfg["gateway_url"], body, cfg["region"]))
        except Exception as exc:  # noqa: BLE001 (never transient: bad/absent creds)
            raise GatewayError(f"cannot SigV4-sign the gateway call: {exc}") from exc
        req = urllib.request.Request(cfg["gateway_url"], data=body,
                                     headers=call_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            err = GatewayError(f"gateway HTTP {exc.code}: {detail}")
            if exc.code not in _RETRY_STATUS:
                raise err from exc          # a real answer; do not paper over it
            last = err
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = GatewayError(f"gateway call failed: {exc}")
        else:
            if "error" in payload:
                e = payload["error"]
                raise GatewayError(f"{e.get('code')}: {e.get('message')}")
            return payload.get("result", {})
        if attempt < _RPC_ATTEMPTS - 1:
            time.sleep(_RPC_BACKOFF_S * (attempt + 1))
    raise last if last else GatewayError("gateway call failed with no error recorded")


def _tool(cfg: dict, tool: str, arguments: dict, timeout: float = 30.0) -> Any:
    """Call a GitHub MCP tool through the gateway target (``<target>___<tool>``).

    Unwraps MCP content blocks: a tool returning structured JSON arrives as a
    ``text`` block holding that JSON; a tool returning a bare string (e.g.
    put_file's commit sha) arrives as text that is not JSON -- return it raw."""
    name = f"{cfg['target']}___{tool}"
    result = _gateway_rpc(cfg, "tools/call", {"name": name, "arguments": arguments}, timeout)
    if isinstance(result, dict) and "content" in result:
        for block in result["content"]:
            if block.get("type") == "text":
                text = block.get("text", "")
                _raise_if_tool_error(tool, text)
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    return text
    return result


# A tool that FAILED inside the MCP server answers with a normal JSON-RPC result whose
# text is the error message ("Error calling tool 'create_branch': ... 404 ..."), not a
# JSON-RPC error. So `_gateway_rpc` sees success and every `except GatewayError` around
# a `_tool` call is dead code for exactly the failures it was written to catch.
#
# Live consequence: `doctor` called create_branch against a repo whose default ref the
# App could not read, got that 404 back as a STRING, and reported
# "PASS app_can_write_repo (prepared workshop/integration)" -- while no branch existed.
# A preflight whose whole job is to fail early instead reported green.
_TOOL_ERROR_PREFIXES = ("error calling tool", "error executing tool", "tool error:")


def _raise_if_tool_error(tool: str, text: str) -> None:
    """Turn an error-shaped tool RESULT into the GatewayError callers already handle."""
    head = (text or "").strip()
    if any(head[:40].lower().startswith(p) for p in _TOOL_ERROR_PREFIXES):
        raise GatewayError(f"{tool}: {head[:400]}")


def _tools_list(cfg: dict, timeout: float = 15.0) -> list[dict]:
    result = _gateway_rpc(cfg, "tools/list", {}, timeout)
    return result.get("tools", result) if isinstance(result, dict) else result


def repository_default_branch(cfg: dict | None = None) -> str:
    """Read the target repository's default branch through the GitHub App.

    There is deliberately no guessed fallback. A wrong target branch can make a
    valid build open or merge the wrong pull request, so an old Gateway that cannot
    read repository metadata must fail pre-flight and be redeployed.
    """
    resolved = cfg or _gateway_config()
    if not resolved:
        raise GatewayError("no GitHub MCP Gateway wired")
    owner, repo_name = _repo_parts(resolved)
    info = _tool(
        resolved,
        "get_repository",
        {"owner": owner, "repo": repo_name},
        timeout=20.0,
    )
    branch = (
        str(info.get("default_branch") or "").strip()
        if isinstance(info, dict) else ""
    )
    if not branch:
        raise GatewayError(
            "get_repository returned no default_branch; redeploy the Gateway")
    return branch


# --- Final PR policy ----------------------------------------------------------

def _coerce_final_merge_policy(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    return (candidate if candidate in FINAL_MERGE_POLICIES
            else _DEFAULT_FINAL_MERGE_POLICY)


def _save_final_merge_policy(policy: str) -> None:
    os.makedirs(_RUNS_DIR, exist_ok=True)
    with open(_FINAL_MERGE_POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump({"final_merge_policy": policy}, f)


def final_merge_policy() -> str:
    """How the already-green final PR finishes: human review or auto-merge."""
    env = (os.environ.get("WORKSHOP_FINAL_MERGE_POLICY")
           or os.environ.get("WORKSHOP_MERGE_POLICY"))
    if env is not None:
        return _coerce_final_merge_policy(env)
    try:
        with open(_FINAL_MERGE_POLICY_FILE, encoding="utf-8") as f:
            return _coerce_final_merge_policy(
                json.load(f).get("final_merge_policy"))
    except (OSError, ValueError):
        return _DEFAULT_FINAL_MERGE_POLICY


def set_final_merge_policy(value: str | None) -> dict[str, Any]:
    """Change only the final PR policy; role PRs always use the merge queue."""
    _save_final_merge_policy(_coerce_final_merge_policy(value))
    return status()


# --- Settings surface (the console + terminal write here) ---------------------

def save_settings(repo: str, gateway_url: str | None = None,
                  final_policy: str | None = None) -> dict[str, Any]:
    """Persist the Settings-pane gateway connection (ladder rung 2). NO token.

    ``repo`` is the attendee's template-derived repository ``owner/name`` (where
    the PR lands). ``gateway_url`` is normally wired by the workshop (env), so the
    console may omit it; when present it is saved too.
    """
    repo = (repo or "").strip()
    if not _REPO_RE.match(repo):
        return {"error": "repo must be owner/name"}
    file = _load_config_file()
    file["repo"] = repo
    gateway_url = (gateway_url or "").strip()
    if gateway_url:
        file["gateway_url"] = gateway_url
    if final_policy is not None:
        _save_final_merge_policy(_coerce_final_merge_policy(final_policy))
    os.makedirs(_RUNS_DIR, exist_ok=True)
    try:
        os.chmod(_RUNS_DIR, 0o700)
    except OSError:
        pass
    with open(_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(file, f)
    os.chmod(_SETTINGS, 0o600)
    return status()


def clear_settings() -> dict[str, Any]:
    """Disconnect by removing the local Gateway configuration."""
    try:
        os.remove(_SETTINGS)
    except OSError:
        pass
    return status()


def status() -> dict[str, Any]:
    """The connection status the console renders. Reports the GATEWAY health
    (tools/list), never a token. ``connected`` is true only when the gateway
    answers a signed tools/list for a wired repo."""
    cfg = _gateway_config()
    if not cfg:
        return {"connected": False, "mode": "local", "workshop_repo": WORKSHOP_REPO,
                "connection_method": "gateway",
                "final_merge_policy": final_merge_policy(),
                "hint": f"Use the '{WORKSHOP_REPO}' template to create your own repo, "
                        "then set GITHUB_GATEWAY_URL + GITHUB_REPO (or paste your "
                        "owner/repo in Settings once the workshop wires the gateway). "
                        "Until then pre-flight fails before any builder runs."}
    try:
        tools = _tools_list(cfg)
        names = [t.get("name", "") for t in tools] if isinstance(tools, list) else []
        default_branch = repository_default_branch(cfg)
    except GatewayError as exc:
        return {"connected": False, "mode": "gateway", "connection_method": "gateway",
                "gateway_url": cfg["gateway_url"], "target": cfg["target"],
                "repo": cfg["repo"], "workshop_repo": WORKSHOP_REPO,
                "final_merge_policy": final_merge_policy(),
                "error": f"gateway health check failed: {exc}"}
    return {"connected": True, "mode": "gateway", "connection_method": "gateway",
            "gateway_url": cfg["gateway_url"], "target": cfg["target"],
            "repo": cfg["repo"], "workshop_repo": WORKSHOP_REPO,
            "region": cfg["region"], "source": cfg["source"],
            "default_branch": default_branch,
            "final_merge_policy": final_merge_policy(),
            "tool_count": len(names)}


def doctor() -> dict[str, Any]:
    """Prove the PR path works BEFORE a build spends ten minutes discovering it does not.

    ``status()`` answers "does the gateway respond", which is not the same question.
    The mistakes Lab 2 actually produces are one step further in: the App installed
    on the wrong repository, ``GITHUB_REPO`` pointing at a repo the installation
    cannot see, or a typo in the owner. Every one of those passes ``tools/list`` and
    then fails at ``create_branch``, after the coordinator is deployed and a build
    has run its agents. That is the most expensive possible moment to find out.

    Idea from awslabs/aidlc-workflows v2, which ships ``/aidlc --doctor``: check the
    setup as its own step, with a named result per check, so a broken install is a
    30-second answer rather than a failed run.

    It resolves config, lists the gateway's tools, reads the target repository, and
    idempotently prepares ``workshop/doctor`` to prove write permission. It writes no
    file and opens no pull request, so it is safe to run repeatedly.

    Returns ``{ok, checks: [{check, passed, detail}], hint}``: ``ok`` only when every
    check passed.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> bool:
        checks.append({"check": name, "passed": passed, "detail": detail})
        return passed

    def done(hint: str = "") -> dict[str, Any]:
        ok = all(c["passed"] for c in checks)
        out: dict[str, Any] = {"ok": ok, "checks": checks}
        if not ok and hint:
            out["hint"] = hint
        return out

    cfg = _gateway_config()
    if not add("config_resolved", cfg is not None,
               "gateway URL + owner/repo resolved"
               if cfg else "no gateway URL and/or repo is wired"):
        return done("Export GITHUB_GATEWAY_URL and GITHUB_REPO (the Broker GitHub "
                    "Tools page, step 6), or paste owner/repo in console Settings. "
                    "Until then pre-flight stops before any builder runs.")
    add("repo_shape", bool(_REPO_RE.match(cfg["repo"])),
        f"repo is {cfg['repo']!r}")

    try:
        tools = _tools_list(cfg)
        names = [t.get("name", "") for t in tools] if isinstance(tools, list) else []
    except GatewayError as exc:
        add("gateway_reachable", False, f"signed tools/list failed: {exc}")
        return done("The Gateway did not answer a signed request. Re-run "
                    "coding-agents/gateway_mcp/verify-gateway.sh; if that fails too, "
                    "the Runtime or its target is not READY yet.")
    add("gateway_reachable", True, f"{len(names)} tool(s) discoverable")

    # Every tool the role-PR queue calls. A stale Gateway may answer tools/list
    # while still lacking the archive, atomic branch update, label, or merge calls.
    need = (
        "create_branch",
        "get_repository",
        "get_repository_archive",
        "get_branch_head",
        "reset_branch",
        "commit_changes",
        "create_pull_request",
        "comment_on_issue",
        "ensure_labels",
        "merge_pull_request",
        "list_files",
    )
    missing = [t for t in need
               if not any(n.endswith(f"___{t}") or n == t for n in names)]
    add("pr_tools_present", not missing,
        "checkout + branch + atomic change + PR + label + comment + merge tools exposed"
        if not missing else f"missing from the gateway: {', '.join(missing)}")

    # The check that catches the real Lab 2 mistake: can the App READ this repo?
    # A wrong owner, a typo, or an App installed on a different repository all
    # reach the gateway fine and only fail when a build tries to write.
    owner, _, repo_name = cfg["repo"].partition("/")
    try:
        default_branch = repository_default_branch(cfg)
        # list_files at the repo root, on the resolved default branch: the
        # cheapest call that requires the installation to actually have this
        # repository, and it is the same tool set a build uses.
        _tool(cfg, "list_files",
              {"owner": owner, "repo": repo_name, "path": "",
               "ref": default_branch}, timeout=20.0)
        add("app_can_reach_repo", True,
            f"the App installation can read {cfg['repo']} "
            f"({default_branch})")
    except GatewayError as exc:
        reason = str(exc)
        lowered = reason.lower()
        if "not found" in lowered or "404" in lowered:
            detail = (f"the App installation cannot see {cfg['repo']} (404). Either "
                      "the App is installed on a different repository, or "
                      "GITHUB_REPO names the wrong owner/repo.")
        elif "401" in lowered or "credential" in lowered or "auth" in lowered:
            detail = ("the App credential was rejected. Re-run deploy-credential.sh "
                      "with the App ID, installation ID, and .pem that belong "
                      "together.")
        else:
            detail = f"read through the gateway failed: {reason}"
        add("app_can_reach_repo", False, detail)
        return done("Fix the App installation or GITHUB_REPO before deploying the "
                    "coordinator: every one of these failures would otherwise "
                    "surface only after a build had already run its agents.")

    # Reading the repo does NOT prove the App may write to it, and a beginner filling in
    # the GitHub App permission form commonly leaves "Pull requests" (or "Contents") on
    # read-only. That combination passes every check above -- the gateway answers, the
    # tools are all listed, the repo is visible -- and then fails at `create_branch`
    # AFTER a ten-minute build. So probe a write here, while it is still cheap.
    #
    # The stable probe branch is deliberately separate from every run-private
    # integration branch. `create_branch` is idempotent and the Gateway exposes no
    # branch delete operation.
    try:
        _tool(cfg, "create_branch",
              {"owner": owner, "repo": repo_name, "branch": DOCTOR_BRANCH,
               "from_branch": default_branch}, timeout=20.0)
        add("app_can_write_repo", True,
            f"the App can create a branch on {cfg['repo']} "
            f"(prepared {DOCTOR_BRANCH})")
    except GatewayError as exc:
        reason = str(exc)
        lowered = reason.lower()
        if _branch_already_exists(exc):
            add("app_can_write_repo", True,
                f"{DOCTOR_BRANCH} already exists, so the App can write")
        elif "403" in lowered or "forbidden" in lowered or "permission" in lowered:
            add("app_can_write_repo", False,
                "the App can READ the repo but not write to it (403). In the App's "
                "settings, set Repository permissions -> Contents, Issues, and Pull "
                "requests to Read and write, then re-install the App so the new "
                "permissions take effect. Metadata remains Read-only.")
            return done("Grant the App write access before deploying the coordinator: "
                        "otherwise the build runs for ten minutes and only then fails "
                        "at the pull-request step.")
        else:
            # Do not fail the whole preflight on something we cannot attribute; say so.
            add("app_can_write_repo", False,
                f"could not confirm write access: {reason}")
            return done("Resolve the write check above before deploying the "
                        "coordinator.")
    return done()


# --- Compose base -------------------------------------------------------------

def ensure_compose_base() -> dict[str, Any]:
    """Describe where the local evidence commit is recorded.

    The queue itself operates on the attendee repository through the Gateway.
    This scratch repository exists only for the console's final diff view and
    never substitutes for a GitHub pull request.
    """
    cfg = _gateway_config()
    if not cfg:
        return {"mode": "local",
                "reason": "no gateway wired: recording only a local evidence diff"}
    return {"mode": "gateway", "repo": cfg["repo"], "source": cfg["source"],
            "reason": "gateway queue active; recording a local evidence diff"}


def _branch_already_exists(exc: Exception) -> bool:
    """True when create_branch failed only because the branch is already there.

    GitHub answers a duplicate ref with a bare ``422 Unprocessable Entity`` and the MCP
    server forwards httpx's message, which does NOT contain the words "already exists".
    Matching only that phrase made a SUCCESS look like a failure: on a rerun, `doctor`
    reported "could not confirm write access: ... 422" and NOT READY on a repo it had
    itself prepared moments earlier. 422 on the refs endpoint has exactly one cause
    here, since the ref name and the base sha are both ours.
    """
    low = str(exc).lower()
    return ("already exists" in low or "reference already exists" in low
            or "422" in low)


_ARCHIVE_COMPRESSED_LIMIT = 32 * 1024 * 1024
_ARCHIVE_EXPANDED_LIMIT = 128 * 1024 * 1024
_ARCHIVE_FILE_LIMIT = 5000


def _repo_parts(cfg: dict) -> tuple[str, str]:
    owner, _, repo_name = cfg["repo"].partition("/")
    return owner, repo_name


def _extract_repository_archive(encoded: str, destination: str) -> int:
    """Safely materialize the Gateway-brokered repository snapshot."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise GatewayError(f"repository archive is not valid base64: {exc}") from exc
    if len(raw) > _ARCHIVE_COMPRESSED_LIMIT:
        raise GatewayError("repository archive exceeds the compressed size limit")

    scratch = destination + f".tmp-{os.getpid()}-{threading.get_ident()}"
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)
    count = total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                # GitHub archives have one generated top-level directory. Strip it
                # and reject anything that could escape the destination.
                name = member.name.replace("\\", "/")
                parts = name.split("/", 1)
                if len(parts) != 2 or not parts[1]:
                    continue
                rel = parts[1]
                rel_parts = rel.split("/")
                if rel.startswith("/") or any(p in ("", ".", "..")
                                              for p in rel_parts):
                    raise GatewayError(
                        f"repository archive contains an unsafe path: {rel!r}")
                count += 1
                total += int(member.size or 0)
                if count > _ARCHIVE_FILE_LIMIT or total > _ARCHIVE_EXPANDED_LIMIT:
                    raise GatewayError(
                        "repository archive exceeds the expanded checkout limit")
                source = archive.extractfile(member)
                if source is None:
                    continue
                dest = os.path.join(scratch, *rel_parts)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    shutil.copyfileobj(source, f)
                if member.mode & 0o111:
                    os.chmod(dest, os.stat(dest).st_mode | 0o111)
        shutil.rmtree(destination, ignore_errors=True)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        os.replace(scratch, destination)
        return count
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def snapshot_branch(branch: str, destination: str) -> dict[str, Any]:
    """Read one private branch through the Gateway into a local checkout."""
    cfg = _gateway_config()
    if not cfg:
        return {"error": "PR_NO_GATEWAY: no GitHub MCP Gateway wired"}
    owner, repo_name = _repo_parts(cfg)
    try:
        head = _tool(
            cfg, "get_branch_head",
            {"owner": owner, "repo": repo_name, "branch": branch},
            timeout=30.0,
        )
        payload = _tool(
            cfg, "get_repository_archive",
            {"owner": owner, "repo": repo_name, "ref": branch},
            timeout=120.0,
        )
        encoded = payload.get("archive_base64", "") if isinstance(
            payload, dict) else ""
        files = _extract_repository_archive(encoded, destination)
    except (GatewayError, OSError, tarfile.TarError) as exc:
        return {"error": f"repository checkout failed: {exc}"}
    return {
        "branch": branch,
        "sha": str(head),
        "files": files,
        "repo": cfg["repo"],
        "source": cfg["source"],
    }


def prepare_run_integration(branch: str, destination: str) -> dict[str, Any]:
    """Create one run-scoped integration branch and clone its private snapshot."""
    cfg = _gateway_config()
    if not cfg:
        return {
            "error": "PR_NO_GATEWAY: this workflow opens role pull requests before "
                     "validation, so the GitHub MCP Gateway must be wired first. "
                     "Run `python3 orchestrator/github.py doctor`.",
        }
    owner, repo_name = _repo_parts(cfg)
    try:
        default_branch = repository_default_branch(cfg)
        _tool(
            cfg, "create_branch",
            {
                "owner": owner,
                "repo": repo_name,
                "branch": branch,
                "from_branch": default_branch,
            },
            timeout=30.0,
        )
    except GatewayError as exc:
        if not _branch_already_exists(exc):
            return {"error": f"integration branch creation failed: {exc}"}
    snapshot = snapshot_branch(branch, destination)
    if not snapshot.get("error"):
        snapshot["default_branch"] = default_branch
    return snapshot


def _item_labels(run: Any, item: Any) -> list[dict[str, str]]:
    return [
        {
            "name": f"run:{run.run_id}",
            "color": "0969da",
            "description": "AgentCore workshop run id",
        },
        {
            "name": f"role:{item.capability}",
            "color": "8250df",
            "description": "Coding-agent role",
        },
        {
            "name": f"work:{item.work_id}",
            "color": "1f883d",
            "description": "Isolated coding-agent work id",
        },
    ]


def publish_work_item(run: Any, item: Any, body_md: str) -> dict[str, Any]:
    """Create or refresh one role-owned branch and its existing pull request."""
    cfg = _gateway_config()
    if not cfg:
        return {"error": "PR_NO_GATEWAY: no GitHub MCP Gateway wired"}
    patch = getattr(item, "_patch", None)
    if patch is None:
        return {"error": f"WORK_PATCH_MISSING:{item.work_id}"}
    if not patch.changes:
        return {"error": f"ROLE_PRODUCED_NO_CHANGE:{item.work_id}"}
    owner, repo_name = _repo_parts(cfg)
    try:
        files = [{
            "path": path,
            "content_base64": base64.b64encode(content).decode("ascii"),
        } for path, content in patch.changes.items() if content is not None]
        deletions = [path for path, content in patch.changes.items()
                     if content is None]
        committed = _tool(
            cfg, "commit_changes",
            {
                "owner": owner,
                "repo": repo_name,
                "branch": item.branch,
                "files": files,
                "deletions": deletions,
                "message": (
                    f"{run.run_id} {item.work_id}: {item.role} "
                    f"round {max(1, item.attempt)}"
                ),
                "expected_parent": item.head_sha or "",
                "from_branch": item.base_branch,
            },
            timeout=120.0,
        )
        item.base_sha = (committed.get("base_sha", "")
                         if isinstance(committed, dict) else "")
        item.head_sha = (committed.get("sha", "")
                         if isinstance(committed, dict) else str(committed))
        replaced_pr = None
        if item.pr:
            current = _tool(
                cfg, "get_issue",
                {
                    "owner": owner,
                    "repo": repo_name,
                    "issue_number": item.pr["number"],
                },
                timeout=30.0,
            )
            state = (current.get("state", "")
                     if isinstance(current, dict) else "")
            if state != "open":
                replaced_pr = dict(item.pr)
                item.pr = None
        if not item.pr:
            opened = _tool(
                cfg, "create_pull_request",
                {
                    "owner": owner,
                    "repo": repo_name,
                    "title": f"[{item.capability}] {run.task.splitlines()[0][:72]}",
                    "head": item.branch,
                    "base": item.base_branch,
                    "body": body_md,
                },
                timeout=30.0,
            )
            if not isinstance(opened, dict) or "url" not in opened:
                return {
                    "error": f"role PR returned no URL: {opened!r}",
                    "head_sha": item.head_sha,
                }
            item.pr = {
                "pr_url": opened["url"],
                "number": opened.get("number"),
                "base": item.base_branch,
                "head": item.branch,
                "source": cfg["source"],
            }
            if replaced_pr:
                item.pr["replaces"] = replaced_pr.get("pr_url")
        labels = _tool(
            cfg, "ensure_labels",
            {
                "owner": owner,
                "repo": repo_name,
                "issue_number": item.pr["number"],
                "labels": _item_labels(run, item),
            },
            timeout=30.0,
        )
        item.pr["labels"] = labels
        item.pr["head_sha"] = item.head_sha
        item.state = "in_review"
        item.stale = False
        return dict(item.pr)
    except GatewayError as exc:
        return {
            "error": f"role PR publish failed for {item.work_id}: {exc}",
            "pr_url": (item.pr or {}).get("pr_url"),
            "number": (item.pr or {}).get("number"),
        }


def comment_on_work_item(item: Any, body_md: str) -> dict[str, Any]:
    """Post gate/review/refresh evidence on a role's existing PR."""
    cfg = _gateway_config()
    number = (getattr(item, "pr", None) or {}).get("number")
    if not cfg:
        return {"skipped": "local mode (no gateway wired)"}
    if not number:
        return {"skipped": f"{item.work_id} has no PR number"}
    owner, repo_name = _repo_parts(cfg)
    try:
        response = _tool(
            cfg, "comment_on_issue",
            {
                "owner": owner,
                "repo": repo_name,
                "issue_number": number,
                "body": body_md,
            },
        )
    except GatewayError as exc:
        return {"error": f"gateway comment failed: {exc}"}
    return {
        "reviewed": True,
        "review_url": response.get("url", "") if isinstance(response, dict) else "",
    }


def merge_work_item(run: Any, item: Any) -> dict[str, Any]:
    """Squash one green role PR into this run's private integration branch."""
    cfg = _gateway_config()
    pr = getattr(item, "pr", None) or {}
    if not cfg:
        return {"error": "PR_NO_GATEWAY: no GitHub MCP Gateway wired"}
    if pr.get("base") != run.integration_branch:
        return {
            "error": f"refusing to merge {item.work_id}: base "
                     f"{pr.get('base')!r} is not {run.integration_branch!r}",
        }
    number = pr.get("number")
    if not number:
        return {"error": f"{item.work_id} has no PR number"}
    owner, repo_name = _repo_parts(cfg)
    try:
        response = _tool(
            cfg, "merge_pull_request",
            {
                "owner": owner,
                "repo": repo_name,
                "number": number,
                "merge_method": "squash",
                "head_sha": item.head_sha or "",
            },
            timeout=60.0,
        )
    except GatewayError as exc:
        return {"error": f"role PR merge failed: {exc}"}
    if not isinstance(response, dict) or not response.get("merged"):
        return {"error": f"role PR merge did not complete: {response!r}"}
    item.merge_state = "merged"
    item.state = "merged"
    return {"merged": True, "sha": response.get("sha", "")}


def open_integration_pr(run: Any, body_md: str) -> dict[str, Any]:
    """Open the final evidence PR from the validated run branch to default."""
    cfg = _gateway_config()
    if not cfg:
        return {"error": "PR_NO_GATEWAY: no GitHub MCP Gateway wired"}
    owner, repo_name = _repo_parts(cfg)
    try:
        default_branch = repository_default_branch(cfg)
        pinned_branch = str(
            getattr(run, "final_base_branch", None) or default_branch)
        if pinned_branch != default_branch:
            return {
                "error": "final integration PR refused: repository default branch "
                         f"changed from {pinned_branch!r} to {default_branch!r} "
                         "during the run",
            }
        head_sha = str(_tool(
            cfg, "get_branch_head",
            {
                "owner": owner,
                "repo": repo_name,
                "branch": run.integration_branch,
            },
            timeout=30.0,
        ))
        opened = _tool(
            cfg, "create_pull_request",
            {
                "owner": owner,
                "repo": repo_name,
                "title": f"[integration] {run.task.splitlines()[0][:72]}",
                "head": run.integration_branch,
                "base": pinned_branch,
                "body": body_md,
            },
            timeout=30.0,
        )
    except GatewayError as exc:
        return {"error": f"final integration PR failed: {exc}"}
    if not isinstance(opened, dict) or "url" not in opened:
        return {"error": f"final integration PR returned no URL: {opened!r}"}
    result = {
        "pr_url": opened["url"],
        "number": opened.get("number"),
        "base": pinned_branch,
        "head": run.integration_branch,
        "head_sha": head_sha,
        "default_branch": pinned_branch,
        "source": cfg["source"],
    }
    try:
        labels = _tool(
            cfg, "ensure_labels",
            {
                "owner": owner,
                "repo": repo_name,
                "issue_number": result["number"],
                "labels": [
                    {
                        "name": f"run:{run.run_id}",
                        "color": "0969da",
                        "description": "AgentCore workshop run id",
                    },
                    {
                        "name": "integration",
                        "color": "cf222e",
                        "description": "Validated multi-role integration",
                    },
                    {
                        "name": "gate:green",
                        "color": "1f883d",
                        "description": "Executable acceptance gate passed",
                    },
                ],
            },
        )
        result["labels"] = labels
    except GatewayError as exc:
        result["error"] = f"final PR opened but labels failed: {exc}"
    return result


def merge_integration_pr(run: Any) -> dict[str, Any]:
    """Auto-merge the already-green final PR to the default branch.

    The reviewed head SHA is mandatory. A stale or protected PR fails and remains
    open for a person; this function never bypasses branch protection.
    """
    cfg = _gateway_config()
    pr = getattr(run, "pr", None) or {}
    if not cfg:
        return {"error": "PR_NO_GATEWAY: no GitHub MCP Gateway wired"}
    try:
        default_branch = repository_default_branch(cfg)
    except GatewayError as exc:
        return {"error": f"cannot read repository default branch: {exc}"}
    if pr.get("base") != default_branch:
        return {
            "error": "refusing final auto-merge: PR does not target the "
                     f"default branch {default_branch!r}",
        }
    if pr.get("head") != getattr(run, "integration_branch", None):
        return {"error": "refusing final auto-merge: unexpected PR head branch"}
    number = pr.get("number")
    head_sha = str(pr.get("head_sha") or "")
    if not number or not head_sha:
        return {"error": "final auto-merge requires a PR number and reviewed head SHA"}
    owner, repo_name = _repo_parts(cfg)
    try:
        response = _tool(
            cfg, "merge_pull_request",
            {
                "owner": owner,
                "repo": repo_name,
                "number": number,
                "merge_method": "squash",
                "head_sha": head_sha,
            },
            timeout=60.0,
        )
    except GatewayError as exc:
        return {"error": f"final PR auto-merge failed: {exc}"}
    if not isinstance(response, dict) or not response.get("merged"):
        return {"error": f"final PR auto-merge did not complete: {response!r}"}
    return {"merged": True, "sha": response.get("sha", "")}


def post_review(run: Any, body_md: str) -> dict[str, Any]:
    """Post the review-panel verdict as a PR COMMENT via the gateway (the bot
    Assessment analog). A PR is an issue for the comments endpoint, so this works
    even when the App installation authored the PR -- unlike an APPROVE review,
    which GitHub rejects on your own PR (HTTP 422). Returns {reviewed} | {skipped}
    | {error}; never fakes success."""
    cfg = _gateway_config()
    if not cfg:
        return {"skipped": "local mode (no gateway wired)"}
    pr = getattr(run, "pr", None) or {}
    number = pr.get("number")
    if not number:
        return {"skipped": "no PR number to review"}
    owner, _, repo_name = cfg["repo"].partition("/")
    try:
        resp = _tool(cfg, "comment_on_issue",
                     {"owner": owner, "repo": repo_name,
                      "issue_number": number, "body": body_md})
    except GatewayError as exc:
        return {"error": f"gateway comment failed: {exc}"}
    url = resp.get("url", "") if isinstance(resp, dict) else ""
    return {"reviewed": True, "review_url": url}


# --- CLI ----------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    """`python3 orchestrator/github.py doctor`: check the PR path from a terminal.

    A module entrypoint rather than a new script: the checks have to run through
    the same config resolution and the same signed calls a build uses, or they
    would be verifying something else.
    """
    if len(argv) != 1 or argv[0] not in ("doctor", "status"):
        print("usage: python3 orchestrator/github.py doctor|status")
        return 2
    if argv[0] == "status":
        print(json.dumps(status(), indent=2))
        return 0
    result = doctor()
    for check in result["checks"]:
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  {mark}  {check['check']}: {check['detail']}")
    if result["ok"]:
        print("\nGitHub PR path ready: the App can reach the repo through the Gateway.")
        return 0
    print(f"\nNOT READY. {result.get('hint', '')}")
    return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_main(_sys.argv[1:]))
