"""GitHub MCP Server.

Exposes the actions the DevOps agent needs against a target GitHub repo.

Auth: retrieves a GitHub installation access token by signing a JWT with
the GitHub App's private key (stored in AWS Secrets Manager) and exchanging
it via the GitHub API.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from typing import Any, List, Optional

import boto3
import httpx
import jwt
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import BaseModel

GITHUB_API = "https://api.github.com"

AWS_REGION = (
    os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
)
GITHUB_APP_SECRET_ARN = os.environ.get("GITHUB_APP_SECRET_ARN", "")

_secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)

GITHUB_TOKEN: Optional[str] = None
GITHUB_TOKEN_EXP: int = 0


def _load_app_creds() -> dict:
    """Read the GitHub App credentials from Secrets Manager.

    The secret JSON contains: app_id, private_key, installation_id.
    """
    resp = _secrets_client.get_secret_value(SecretId=GITHUB_APP_SECRET_ARN)
    return json.loads(resp["SecretString"])


def _mint_installation_token() -> tuple[str, int]:
    """Sign a JWT and exchange it for a GitHub installation access token."""
    creds = _load_app_creds()
    app_id = creds["app_id"]
    private_key = creds["private_key"]
    installation_id = creds["installation_id"]

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": app_id,
    }
    encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")

    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {encoded_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "DevOpsAgent-GitHubMCP",
            },
        )
        r.raise_for_status()
        data = r.json()

    token = data["token"]
    # GitHub installation tokens expire in 1 hour
    expires_at = now + 3600
    return token, expires_at


def _headers() -> dict:
    if not GITHUB_TOKEN:
        raise RuntimeError("GitHub token not initialized; middleware did not run.")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "DevOpsAgent-GitHubMCP",
    }


class TokenMiddleware(Middleware):
    """Mint a GitHub installation token on first request, refresh near expiry."""

    async def on_request(self, context: MiddlewareContext, call_next):
        global GITHUB_TOKEN, GITHUB_TOKEN_EXP
        now = int(time.time())
        if GITHUB_TOKEN is None or now >= GITHUB_TOKEN_EXP - 300:
            GITHUB_TOKEN, GITHUB_TOKEN_EXP = _mint_installation_token()
            print(
                f"[TokenMiddleware] minted token prefix={GITHUB_TOKEN[:8]}... "
                f"expires_in={GITHUB_TOKEN_EXP - now}s",
                flush=True,
            )
        return await call_next(context)


mcp = FastMCP("GitHubMCP")
mcp.add_middleware(TokenMiddleware())


class Issue(BaseModel):
    number: int
    title: str
    body: Optional[str] = None
    state: str
    labels: List[str] = []
    url: str


class Comment(BaseModel):
    id: int
    author: str
    body: str
    created_at: str


# ---------------- Issues ----------------


@mcp.tool()
def get_issue(owner: str, repo: str, issue_number: int) -> Issue:
    """Fetch an issue's title, body, state, labels, and URL."""
    with httpx.Client(timeout=30) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}",
            headers=_headers(),
        )
        r.raise_for_status()
        d = r.json()
    return Issue(
        number=d["number"],
        title=d["title"],
        body=d.get("body") or "",
        state=d["state"],
        labels=[lbl["name"] for lbl in d.get("labels", [])],
        url=d["html_url"],
    )


@mcp.tool()
def list_issue_comments(owner: str, repo: str, issue_number: int) -> List[Comment]:
    """List all comments on an issue in chronological order."""
    with httpx.Client(timeout=30) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/comments",
            headers=_headers(),
            params={"per_page": 100},
        )
        r.raise_for_status()
        return [
            Comment(
                id=x["id"],
                author=x["user"]["login"],
                body=x.get("body") or "",
                created_at=x["created_at"],
            )
            for x in r.json()
        ]


@mcp.tool()
def comment_on_issue(owner: str, repo: str, issue_number: int, body: str) -> dict:
    """Post a comment on an issue. Returns {id, url}."""
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/comments",
            headers=_headers(),
            json={"body": body},
        )
        r.raise_for_status()
        d = r.json()
        return {"id": d["id"], "url": d["html_url"]}


@mcp.tool()
def update_comment(owner: str, repo: str, comment_id: int, body: str) -> str:
    """Edit an existing issue comment by id. Returns the comment URL."""
    with httpx.Client(timeout=30) as c:
        r = c.patch(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}",
            headers=_headers(),
            json={"body": body},
        )
        r.raise_for_status()
        return r.json()["html_url"]


# ---------------- Assignees ----------------


@mcp.tool()
def assign_issue(
    owner: str, repo: str, issue_number: int, assignees: List[str]
) -> List[str]:
    """Add assignees to an issue. Returns the final list of assignee logins."""
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/assignees",
            headers=_headers(),
            json={"assignees": assignees},
        )
        r.raise_for_status()
        return [u["login"] for u in r.json().get("assignees", [])]


# ---------------- Labels ----------------


@mcp.tool()
def set_labels(
    owner: str, repo: str, issue_number: int, labels: List[str]
) -> List[str]:
    """Replace an issue's labels with the given set. Returns the final labels."""
    with httpx.Client(timeout=30) as c:
        r = c.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/labels",
            headers=_headers(),
            json={"labels": labels},
        )
        r.raise_for_status()
        return [lbl["name"] for lbl in r.json()]


@mcp.tool()
def add_labels(
    owner: str, repo: str, issue_number: int, labels: List[str]
) -> List[str]:
    """Add labels to an issue without removing existing ones."""
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/labels",
            headers=_headers(),
            json={"labels": labels},
        )
        r.raise_for_status()
        return [lbl["name"] for lbl in r.json()]


@mcp.tool()
def ensure_labels(
    owner: str, repo: str, issue_number: int, labels: List[dict[str, str]]
) -> List[str]:
    """Create missing labels, then add them to an issue or pull request.

    ``labels`` is ``[{"name", "color", "description"}]``. Pull requests are
    issues for the label API, so role/run/work ids can travel with every PR even
    though the GitHub App installation is the account that authored it.
    """
    names: list[str] = []
    with httpx.Client(timeout=30) as c:
        for item in labels:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            names.append(name)
            encoded = urllib.parse.quote(name, safe="")
            hit = c.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/labels/{encoded}",
                headers=_headers(),
            )
            if hit.status_code == 404:
                created = c.post(
                    f"{GITHUB_API}/repos/{owner}/{repo}/labels",
                    headers=_headers(),
                    json={
                        "name": name,
                        "color": str(item.get("color") or "6f7781").lstrip("#"),
                        "description": str(item.get("description") or "")[:100],
                    },
                )
                created.raise_for_status()
            else:
                hit.raise_for_status()
        if not names:
            return []
        r = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/labels",
            headers=_headers(),
            json={"labels": names},
        )
        r.raise_for_status()
        return [lbl["name"] for lbl in r.json()]


@mcp.tool()
def remove_label(owner: str, repo: str, issue_number: int, label: str) -> bool:
    """Remove a single label from an issue. Returns True on success."""
    with httpx.Client(timeout=30) as c:
        r = c.delete(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{issue_number}/labels/{label}",
            headers=_headers(),
        )
        return r.status_code in (200, 204)


# ---------------- File reads ----------------


@mcp.tool()
def get_repository(owner: str, repo: str) -> dict:
    """Read repository metadata needed by the PR workflow.

    The orchestrator reads ``default_branch`` because every role pull request is
    based on it and merges into it. This tool never changes repository settings.
    """
    with httpx.Client(timeout=30) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}",
            headers=_headers(),
        )
        r.raise_for_status()
        data = r.json()
        return {
            "default_branch": data["default_branch"],
            "private": bool(data.get("private")),
            "url": data["html_url"],
        }


@mcp.tool()
def get_file(owner: str, repo: str, path: str, ref: str = "main") -> str:
    """Read a file's content from the repository. Returns the decoded text content."""
    with httpx.Client(timeout=30) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(),
            params={"ref": ref},
        )
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode()
        return content


@mcp.tool()
def list_files(owner: str, repo: str, path: str = "", ref: str = "main") -> List[str]:
    """List files and directories at a given path in the repository."""
    with httpx.Client(timeout=30) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(),
            params={"ref": ref},
        )
        r.raise_for_status()
        items = r.json()
        if isinstance(items, list):
            return [f"{item['name']}{'/' if item['type'] == 'dir' else ''}" for item in items]
        return [items["name"]]


@mcp.tool()
def get_repository_archive(owner: str, repo: str, ref: str = "main") -> dict:
    """Return a credential-brokered tar.gz snapshot of one repository ref.

    The installation token stays inside this Runtime. The coordinator receives
    only repository bytes, stages them as an immutable base, and gives every
    builder a separate writable checkout.
    """
    with httpx.Client(timeout=90, follow_redirects=True) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/tarball/{ref}",
            headers=_headers(),
        )
        r.raise_for_status()
        body = r.content
        if len(body) > 32 * 1024 * 1024:
            raise RuntimeError(
                "repository archive exceeds the 32 MiB workshop checkout limit")
        return {
            "format": "tar.gz",
            "archive_base64": base64.b64encode(body).decode("ascii"),
        }


# ---------------- Git refs + commits ----------------


@mcp.tool()
def create_branch(owner: str, repo: str, branch: str, from_branch: str = "main") -> str:
    """Create a new branch from `from_branch`. Returns the new ref name."""
    with httpx.Client(timeout=30) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{from_branch}",
            headers=_headers(),
        )
        r.raise_for_status()
        sha = r.json()["object"]["sha"]
        r = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
            headers=_headers(),
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        r.raise_for_status()
        return r.json()["ref"]


@mcp.tool()
def get_branch_head(owner: str, repo: str, branch: str) -> str:
    """Return the commit SHA currently at a branch head."""
    with httpx.Client(timeout=30) as c:
        r = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{branch}",
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()["object"]["sha"]


@mcp.tool()
def reset_branch(owner: str, repo: str, branch: str,
                 from_branch: str) -> dict:
    """Reset an engine-owned branch to a known source head.

    Per-run work branches use this to refresh a stale PR without exposing the
    GitHub App credential to the role Runtime. The stable ``workshop/doctor``
    branch also uses it as an idempotent write-permission probe.
    """
    if branch == from_branch:
        raise ValueError("refusing to reset a branch onto itself")
    with httpx.Client(timeout=30) as c:
        source = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{from_branch}",
            headers=_headers(),
        )
        source.raise_for_status()
        sha = source.json()["object"]["sha"]
        target = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{branch}",
            headers=_headers(),
        )
        if target.status_code == 404:
            made = c.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
                headers=_headers(),
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            made.raise_for_status()
        else:
            target.raise_for_status()
            moved = c.patch(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                headers=_headers(),
                json={"sha": sha, "force": True},
            )
            moved.raise_for_status()
        return {"branch": branch, "base": from_branch, "sha": sha}


@mcp.tool()
def commit_changes(
    owner: str,
    repo: str,
    branch: str,
    files: List[dict[str, str]],
    deletions: List[str],
    message: str,
    expected_parent: str = "",
    from_branch: str = "",
) -> dict:
    """Commit additions/edits/deletions and move one role branch atomically.

    ``files`` carries base64 bytes as ``{"path", "content_base64"}``. The
    expected-parent compare prevents a stale writer from silently replacing a
    branch that changed after its owner refreshed it. When ``from_branch`` is
    supplied, build directly on its current head and move ``branch`` only after
    the new commit exists. This avoids briefly making an open PR identical to its
    base, which GitHub may interpret as a closed PR.
    """
    if from_branch and branch == from_branch:
        raise ValueError("refusing to commit a branch onto itself")
    with httpx.Client(timeout=90) as c:
        target = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{branch}",
            headers=_headers(),
        )
        target_exists = target.status_code != 404
        if target_exists:
            target.raise_for_status()
            current_head = target.json()["object"]["sha"]
        else:
            current_head = ""
        if expected_parent and current_head != expected_parent:
            raise RuntimeError(
                f"STALE_BRANCH:{branch} expected {expected_parent}, "
                f"found {current_head or 'missing'}")

        if from_branch:
            source = c.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{from_branch}",
                headers=_headers(),
            )
            source.raise_for_status()
            parent_sha = source.json()["object"]["sha"]
        else:
            if not target_exists:
                raise RuntimeError(f"BRANCH_NOT_FOUND:{branch}")
            parent_sha = current_head
        parent = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/commits/{parent_sha}",
            headers=_headers(),
        )
        parent.raise_for_status()
        base_tree = parent.json()["tree"]["sha"]

        entries: list[dict[str, Any]] = []
        for item in files:
            raw = base64.b64decode(str(item["content_base64"]), validate=True)
            blob = c.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/blobs",
                headers=_headers(),
                json={
                    "content": base64.b64encode(raw).decode("ascii"),
                    "encoding": "base64",
                },
            )
            blob.raise_for_status()
            entries.append({
                "path": item["path"],
                "mode": "100644",
                "type": "blob",
                "sha": blob.json()["sha"],
            })
        entries.extend({
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": None,
        } for path in deletions)
        if not entries:
            return {
                "sha": parent_sha,
                "base_sha": parent_sha,
                "changed": False,
            }

        tree = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees",
            headers=_headers(),
            json={"base_tree": base_tree, "tree": entries},
        )
        tree.raise_for_status()
        commit = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/commits",
            headers=_headers(),
            json={
                "message": message,
                "tree": tree.json()["sha"],
                "parents": [parent_sha],
            },
        )
        commit.raise_for_status()
        commit_sha = commit.json()["sha"]

        if target_exists:
            latest = c.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{branch}",
                headers=_headers(),
            )
            latest.raise_for_status()
            latest_head = latest.json()["object"]["sha"]
            if latest_head != current_head:
                raise RuntimeError(
                    f"STALE_BRANCH:{branch} expected {current_head}, "
                    f"found {latest_head}")
            moved = c.patch(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                headers=_headers(),
                json={"sha": commit_sha, "force": bool(from_branch)},
            )
        else:
            moved = c.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
                headers=_headers(),
                json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
        moved.raise_for_status()
        return {
            "sha": commit_sha,
            "base_sha": parent_sha,
            "changed": True,
        }


@mcp.tool()
def put_files(
    owner: str,
    repo: str,
    branch: str,
    files: List[dict],
    message: str,
) -> str:
    """Write MANY files to a branch as ONE commit. Returns the commit SHA.

    ``files`` is ``[{"path": "a/b.py", "content": "..."}]``.

    Why this exists: the Contents API (``put_file``) creates a commit PER FILE, so a
    38-file deliverable arrived as 38 commits titled "run_x: <path>" -- a commit log
    that describes the transport rather than the change, and a reviewer who cannot see
    the deliverable as one unit. The Git Data API builds a tree and commits it once:
    blobs -> tree -> commit -> move the ref.

    ``put_file`` stays for genuine single-file edits.
    """
    with httpx.Client(timeout=60) as c:
        # 1. The branch tip we are building on.
        r = c.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{branch}",
                  headers=_headers())
        r.raise_for_status()
        parent_sha = r.json()["object"]["sha"]
        r = c.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/commits/{parent_sha}",
                  headers=_headers())
        r.raise_for_status()
        base_tree = r.json()["tree"]["sha"]

        # 2. One blob per file. base64 so any content (binary, CRLF, unicode) survives.
        tree_entries = []
        for item in files:
            path = item["path"]
            r = c.post(
                f"{GITHUB_API}/repos/{owner}/{repo}/git/blobs",
                headers=_headers(),
                json={"content": base64.b64encode(
                          str(item["content"]).encode()).decode(),
                      "encoding": "base64"},
            )
            r.raise_for_status()
            tree_entries.append({"path": path, "mode": "100644", "type": "blob",
                                 "sha": r.json()["sha"]})

        # 3. One tree, one commit, then move the branch ref to it.
        r = c.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees", headers=_headers(),
                   json={"base_tree": base_tree, "tree": tree_entries})
        r.raise_for_status()
        tree_sha = r.json()["sha"]

        r = c.post(f"{GITHUB_API}/repos/{owner}/{repo}/git/commits", headers=_headers(),
                   json={"message": message, "tree": tree_sha, "parents": [parent_sha]})
        r.raise_for_status()
        commit_sha = r.json()["sha"]

        r = c.patch(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                    headers=_headers(), json={"sha": commit_sha})
        r.raise_for_status()
        return commit_sha


@mcp.tool()
def put_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
) -> str:
    """Create or update a file on a branch via the Contents API. Returns the commit SHA."""
    with httpx.Client(timeout=30) as c:
        existing = c.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(),
            params={"ref": branch},
        )
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing.status_code == 200:
            payload["sha"] = existing.json()["sha"]
        r = c.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()["commit"]["sha"]


# ---------------- Pull Requests ----------------


@mcp.tool()
def create_pull_request(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    draft: bool = False,
) -> dict:
    """Open a PR from `head` → `base`. Returns {number, url}."""
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            headers=_headers(),
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            },
        )
        r.raise_for_status()
        d = r.json()
        return {"number": d["number"], "url": d["html_url"]}


@mcp.tool()
def merge_pull_request(
    owner: str,
    repo: str,
    number: int,
    merge_method: str = "squash",
    head_sha: str = "",
) -> dict:
    """Merge a pull request (`merge_method`: merge | squash | rebase).

    Returns {merged: bool, sha}. When configured to merge, the orchestrator uses
    this for each reviewed role pull request into the repository's default branch;
    every pull request merges on its own. The caller verifies base/head policy and
    pins ``head_sha``; this tool merges the supplied PR number.
    """
    with httpx.Client(timeout=30) as c:
        payload = {"merge_method": merge_method}
        if head_sha:
            payload["sha"] = head_sha
        r = c.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/merge",
            headers=_headers(),
            json=payload,
        )
        if r.is_error:
            try:
                detail = r.json().get("message") or r.text
            except (ValueError, AttributeError):
                detail = r.text
            raise RuntimeError(
                f"GitHub merge rejected PR #{number} "
                f"({r.status_code}): {detail[:300]}")
        d = r.json()
        return {"merged": bool(d.get("merged")), "sha": d.get("sha", "")}


if __name__ == "__main__":
    # AgentCore is the authenticated ingress proxy and supplies an internal Host.
    mcp.run(
        transport="http",
        host="0.0.0.0",
        stateless_http=True,
        host_origin_protection=False,
    )
