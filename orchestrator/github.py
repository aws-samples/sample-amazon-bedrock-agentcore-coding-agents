"""GitHub finalization through the GitHub MCP Gateway (no PAT anywhere).

The workshop's final goal is a pull request on the attendee's own GitHub. In this
model the ONLY GitHub credential is a **GitHub App installation**, held inside the
GitHub MCP Runtime that fronts an IAM-authenticated AgentCore Gateway. The
orchestrator is still the finalization actor (compose -> pytest floor -> reviewer
LGTM -> one PR -> optional auto-merge, all in engine.py), but instead of pushing a
run branch with a fine-grained token it calls the Gateway's GitHub MCP tools over
SigV4:

  * ``create_branch``       open the run branch off the base
  * ``put_file``            write each composed deliverable file onto that branch
  * ``create_pull_request`` open the PR with the critique report as the body
  * ``comment_on_issue``    post the reviewer verdict as a PR comment (a PR is an
                            issue for the comments endpoint, so this works even
                            when the App installation authored the PR -- unlike an
                            APPROVE review, which GitHub forbids on your own PR)
  * ``merge_pull_request``  squash-merge the run branch (auto policy only, and
                            never into the default branch, by construction)

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

import json
import os
import re
import subprocess
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
# merge_policy is persisted in its OWN tiny file (it holds no secret), kept
# separate from the gateway config so the auto-merge decision resolves the same
# way no matter where the gateway URL / repo came from.
_MERGE_POLICY_FILE = os.path.join(_RUNS_DIR, "merge_policy.local.json")
_COMPOSED = os.path.join(_RUNS_DIR, "composed")

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# The Gateway target name the deploy script creates (deploy-gateway.sh:
# TARGET_NAME="GitHubMCP"). Gateway namespaces every tool as ``<target>___<tool>``.
_DEFAULT_TARGET = "GitHubMCP"
# A fresh "Use this template" repo defaults to ``main``; overridable in config.
_DEFAULT_BASE = "main"

# The canonical workshop TEMPLATE repository. Attendees click "Use this template"
# on it to get an ISOLATED per-attendee working repo (no fork, no shared
# credential). Override with WORKSHOP_REPO. Switch to the aws-samples template
# once that repo is published.
WORKSHOP_REPO = os.environ.get(
    "WORKSHOP_REPO",
    "didhd/agentcore-coding-agents-starter")

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


# --- merge_policy (unchanged across the migration; carries no secret) ---------
# After the SEPARATE reviewer emits LGTM, what may the orchestrator do with the
# approved run branch?
#   "human_review" (DEFAULT, fail-closed): open the PR and STOP. A human merges.
#   "auto"        : after LGTM, the orchestrator MAY squash-merge the run branch
#                  into a stable INTEGRATION branch (never the default branch).
MERGE_POLICIES = ("human_review", "auto")
_DEFAULT_MERGE_POLICY = "human_review"
INTEGRATION_BRANCH = "workshop/integration"


# --- Gateway config resolution ------------------------------------------------

def _region_from_url(url: str) -> str:
    """Best-effort region from the gateway host (…bedrock-agentcore.<region>.…),
    else the ambient AWS region, else us-west-2."""
    m = re.search(r"\.([a-z]{2}-[a-z]+-\d)\.", url or "")
    if m:
        return m.group(1)
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")


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

    Returns ``{gateway_url, repo, target, region, default_branch, source}`` when a
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
        "default_branch": (file.get("default_branch") or _DEFAULT_BASE),
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


def _gateway_rpc(cfg: dict, method: str, params: dict, timeout: float = 30.0) -> Any:
    """POST one SigV4-signed JSON-RPC call to the gateway. Returns ``result`` or
    raises GatewayError on a JSON-RPC error / transport failure."""
    global _RPC_ID
    _RPC_ID += 1
    body = json.dumps({"jsonrpc": "2.0", "method": method,
                       "id": _RPC_ID, "params": params}).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    try:
        headers.update(_sigv4_headers(cfg["gateway_url"], body, cfg["region"]))
    except Exception as exc:  # noqa: BLE001
        raise GatewayError(f"cannot SigV4-sign the gateway call: {exc}") from exc
    req = urllib.request.Request(cfg["gateway_url"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise GatewayError(f"gateway HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GatewayError(f"gateway call failed: {exc}") from exc
    if "error" in payload:
        err = payload["error"]
        raise GatewayError(f"{err.get('code')}: {err.get('message')}")
    return payload.get("result", {})


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
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    return text
    return result


def _tools_list(cfg: dict, timeout: float = 15.0) -> list[dict]:
    result = _gateway_rpc(cfg, "tools/list", {}, timeout)
    return result.get("tools", result) if isinstance(result, dict) else result


# --- merge_policy -------------------------------------------------------------

def _coerce_merge_policy(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in MERGE_POLICIES else _DEFAULT_MERGE_POLICY


def _save_policy_file(policy: str) -> None:
    os.makedirs(_RUNS_DIR, exist_ok=True)
    with open(_MERGE_POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump({"merge_policy": policy}, f)


def merge_policy() -> str:
    """The active merge_policy: ``WORKSHOP_MERGE_POLICY`` env -> the Settings-pane
    policy file -> the fail-closed default ``human_review``."""
    env = os.environ.get("WORKSHOP_MERGE_POLICY")
    if env is not None:
        return _coerce_merge_policy(env)
    try:
        with open(_MERGE_POLICY_FILE, encoding="utf-8") as f:
            return _coerce_merge_policy(json.load(f).get("merge_policy"))
    except (OSError, ValueError):
        return _DEFAULT_MERGE_POLICY


def set_merge_policy(value: str | None) -> dict[str, Any]:
    """Flip ONLY the merge_policy, leaving the gateway config untouched."""
    _save_policy_file(_coerce_merge_policy(value))
    return status()


# --- Settings surface (the console + terminal write here) ---------------------

def save_settings(repo: str, gateway_url: str | None = None,
                  merge_policy: str | None = None) -> dict[str, Any]:
    """Persist the Settings-pane gateway connection (ladder rung 2). NO token.

    ``repo`` is the attendee's template-derived repository ``owner/name`` (where
    the PR lands). ``gateway_url`` is normally wired by the workshop (env), so the
    console may omit it; when present it is saved too. ``merge_policy`` rides on
    the same surface: omitted/unknown -> the fail-closed ``human_review``.
    """
    repo = (repo or "").strip()
    if not _REPO_RE.match(repo):
        return {"error": "repo must be owner/name"}
    file = _load_config_file()
    file["repo"] = repo
    gateway_url = (gateway_url or "").strip()
    if gateway_url:
        file["gateway_url"] = gateway_url
    policy = _coerce_merge_policy(merge_policy)
    os.makedirs(_RUNS_DIR, exist_ok=True)
    try:
        os.chmod(_RUNS_DIR, 0o700)
    except OSError:
        pass
    with open(_SETTINGS, "w", encoding="utf-8") as f:
        json.dump(file, f)
    os.chmod(_SETTINGS, 0o600)
    _save_policy_file(policy)
    return status()


def clear_settings() -> dict[str, Any]:
    """Disconnect: remove the gateway config file (merge_policy stays; it is an
    independent, secret-free preference the attendee can toggle any time)."""
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
                "connection_method": "gateway", "merge_policy": merge_policy(),
                "hint": f"Use the '{WORKSHOP_REPO}' template to create your own repo, "
                        "then set GITHUB_GATEWAY_URL + GITHUB_REPO (or paste your "
                        "owner/repo in Settings once the workshop wires the gateway). "
                        "Until then the PR step fails loud: a run composes the branch "
                        "locally but opens no PR (pr_url stays null)."}
    try:
        tools = _tools_list(cfg)
        names = [t.get("name", "") for t in tools] if isinstance(tools, list) else []
    except GatewayError as exc:
        return {"connected": False, "mode": "gateway", "connection_method": "gateway",
                "gateway_url": cfg["gateway_url"], "target": cfg["target"],
                "repo": cfg["repo"], "workshop_repo": WORKSHOP_REPO,
                "merge_policy": merge_policy(),
                "error": f"gateway health check failed: {exc}"}
    return {"connected": True, "mode": "gateway", "connection_method": "gateway",
            "gateway_url": cfg["gateway_url"], "target": cfg["target"],
            "repo": cfg["repo"], "workshop_repo": WORKSHOP_REPO,
            "region": cfg["region"], "merge_policy": merge_policy(),
            "source": cfg["source"], "tool_count": len(names)}


# --- Compose base -------------------------------------------------------------

def ensure_compose_base() -> dict[str, Any]:
    """Compose into a LOCAL scratch repo (no external clone).

    In the gateway model there is no token to authenticate a clone of the
    attendee's private repo, and none is needed: the PR is the set of deliverable
    files ADDED on a new branch, written straight to the attendee's repo via the
    gateway's put_file at open_pr time. So compose stays entirely local and
    offline here; open_pr publishes it. Never raises.
    """
    cfg = _gateway_config()
    if not cfg:
        return {"mode": "local", "reason": "no gateway wired: composing into a local scratch repo"}
    return {"mode": "gateway", "repo": cfg["repo"], "source": cfg["source"],
            "reason": "gateway wired: composing locally, publishing via put_file at PR time"}


# --- The PR path (create_branch + put_file + create_pull_request) --------------

def _composed_files(branch: str) -> list[str]:
    """The repo-relative paths the compose commit introduced on ``branch``.

    The compose base is an empty scratch repo, so the branch's commit contains
    exactly the deliverable files (deliverable/mcp_server.py, deliverable/
    critique.md, deliverable/gate_report.json, and deliverable/chatbot.html when a
    frontend role ran)."""
    r = subprocess.run(
        ["git", "-C", _COMPOSED, "show", "--pretty=format:", "--name-only", branch],
        capture_output=True, text=True, timeout=20, env=_git_env())
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


def _read_composed(branch: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", _COMPOSED, "show", f"{branch}:{path}"],
                       capture_output=True, text=True, timeout=20, env=_git_env())
    return r.stdout if r.returncode == 0 else None


def _ensure_integration_branch(cfg: dict, default_branch: str) -> str | None:
    """Return a stable integration branch auto-merge may squash-close into,
    creating it off the default branch tip if absent. Auto-merge targets THIS
    branch, never the default branch, so the invariant holds structurally."""
    try:
        _tool(cfg, "create_branch",
              {"owner": cfg["repo"].split("/")[0], "repo": cfg["repo"].split("/")[1],
               "branch": INTEGRATION_BRANCH, "from_branch": default_branch})
    except GatewayError as exc:
        # Already existing is success; anything else is a real failure.
        if "already exists" not in str(exc).lower() and "reference already exists" not in str(exc).lower():
            return None
    return INTEGRATION_BRANCH


def open_pr(run: Any, report_md: str) -> dict[str, Any]:
    """Publish the composed run branch to the attendee repo via the gateway and
    open the PR. Returns {pr_url} or {error}. Fails LOUD (real-only): no gateway
    -> PR_NO_GATEWAY, pr_url stays null, never a fake URL."""
    cfg = _gateway_config()
    if not cfg:
        return {"error": "PR_NO_GATEWAY: no GitHub MCP Gateway wired. Deploy the "
                "gateway (coding-agents/gateway_mcp) and set GITHUB_GATEWAY_URL + "
                "GITHUB_REPO (or paste your owner/repo in Settings), then re-run to "
                "open a real PR. The composed run branch is local-only until the "
                "gateway is wired; it is never published as a fake PR."}
    branch = run.composed_branch
    if not branch or not os.path.isdir(os.path.join(_COMPOSED, ".git")):
        return {"error": "no composed branch to publish"}
    owner, _, repo_name = cfg["repo"].partition("/")
    default_branch = cfg["default_branch"]

    # Base-branch policy (the "never auto-merge to main" guarantee, by construction):
    #   human_review -> PR targets the repo's default branch; a human merges to it.
    #   auto         -> PR targets a stable INTEGRATION branch the orchestrator may
    #                   squash-merge into; the default branch is never touched.
    if merge_policy() == "auto":
        base = _ensure_integration_branch(cfg, default_branch)
        if base is None:
            return {"error": "could not prepare the integration branch for auto-merge"}
    else:
        base = default_branch

    files = _composed_files(branch)
    if not files:
        return {"error": "composed branch has no files to publish"}

    # 1. Create the run branch off the base.
    try:
        _tool(cfg, "create_branch", {"owner": owner, "repo": repo_name,
                                     "branch": branch, "from_branch": base})
    except GatewayError as exc:
        if ("already exists" not in str(exc).lower()
                and "reference already exists" not in str(exc).lower()):
            return {"error": f"gateway create_branch failed: {exc}"}

    # 2. Write each deliverable file onto the run branch.
    for path in files:
        content = _read_composed(branch, path)
        if content is None:
            continue
        try:
            _tool(cfg, "put_file", {"owner": owner, "repo": repo_name, "branch": branch,
                                    "path": path, "content": content,
                                    "message": f"{run.run_id}: {path}"})
        except GatewayError as exc:
            return {"error": f"gateway put_file failed for {path}: {exc}"}

    # 3. Open the PR with the critique report as the body.
    title = f"{run.run_id}: {run.task[:80]}"
    try:
        pr = _tool(cfg, "create_pull_request",
                   {"owner": owner, "repo": repo_name, "title": title,
                    "head": branch, "base": base, "body": report_md})
    except GatewayError as exc:
        # A PR for this head may already exist from a prior attempt; surface it.
        return {"error": f"gateway create_pull_request failed: {exc}"}
    if not isinstance(pr, dict) or "url" not in pr:
        return {"error": f"gateway create_pull_request returned no url: {pr!r}"}
    return {"pr_url": pr["url"], "number": pr.get("number"), "base": base,
            "default_branch": default_branch, "source": cfg["source"]}


def post_review(run: Any, body_md: str) -> dict[str, Any]:
    """Post the reviewer verdict as a PR COMMENT via the gateway (the bot
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


def merge_pr(run: Any) -> dict[str, Any]:
    """Squash-merge the run's PR into its integration branch via the gateway.
    Returns {merged, sha} | {skipped} | {error}; never fakes success.

    Defense in depth: refuses to merge a PR whose base is the repo's default
    branch, so auto-merge can never close into the default branch even if the base
    was somehow set wrong."""
    cfg = _gateway_config()
    if not cfg:
        return {"skipped": "local mode (no gateway wired)"}
    pr = getattr(run, "pr", None) or {}
    number = pr.get("number")
    if not number:
        return {"skipped": "no PR number to merge"}
    base, default_branch = pr.get("base"), pr.get("default_branch")
    if base and default_branch and base == default_branch:
        return {"skipped": f"auto-merge never targets the default branch ({default_branch})"}
    owner, _, repo_name = cfg["repo"].partition("/")
    try:
        resp = _tool(cfg, "merge_pull_request",
                     {"owner": owner, "repo": repo_name,
                      "number": number, "merge_method": "squash"})
    except GatewayError as exc:
        return {"error": f"gateway merge failed: {exc}"}
    if isinstance(resp, dict) and resp.get("merged"):
        return {"merged": True, "sha": resp.get("sha", "")}
    return {"error": f"merge did not complete: {resp!r}"}
