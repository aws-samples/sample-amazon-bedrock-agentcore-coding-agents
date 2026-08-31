#!/usr/bin/env python3
"""Create this workshop's GitHub App from a manifest, with nothing typed by hand.

WHY THIS EXISTS. The App itself is unavoidable: the Gateway's MCP server mints a
per-run installation token from an App private key in Secrets Manager, which is what
keeps a reusable GitHub credential out of every coding agent. What IS avoidable is the
way that App used to be registered: four permission dropdowns, one webhook checkbox to
clear, a .pem downloaded to a laptop and dragged into the browser IDE, then two
different numbers (App ID and installation ID) transcribed off two different GitHub
pages. Every one of those is a silent typo that surfaces ten minutes later as a 401
from inside the MCP server, after a build has already spent real model tokens.

GitHub's App-manifest flow removes all of it. This script serves the manifest, receives
the redirect, exchanges the temporary code for the App id and private key, waits for
you to install the App, discovers the installation id from the App itself, and writes
the three values into ./github-app.env for deploy-all.sh to read.

    python3 create-github-app.py        # then: source github-app.env && ./deploy-all.sh

HOW THE BROWSER REACHES THIS SCRIPT. GitHub can only deliver the code to a URL your
browser can open, so a bare localhost port on the workshop host is no good: localhost
is your laptop. The workshop host already publishes code-server through CloudFront, and
code-server proxies http://127.0.0.1:<port>/<path> at /proxy/<port>/<path>, so
<CloudFront domain>/proxy/8765/callback lands here. That path is also already
authenticated: only a browser holding the code-server session can reach it.

MANUAL FALLBACK. Everything here is a convenience over a documented manual path. If
this script cannot run (no cryptography module, no egress, a proxy path that does not
resolve), register the App by hand in GitHub settings and export GITHUB_APP_ID,
GITHUB_APP_PRIVATE_KEY_FILE, and GITHUB_APP_INSTALLATION_ID yourself; deploy-credential.sh
verifies the three against GitHub either way.
"""
from __future__ import annotations

import argparse
import base64
import functools
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY_PATH = REPO_ROOT / "agentcore-github-mcp.private-key.pem"
ENV_FILE = Path(__file__).resolve().parent / "github-app.env"
GITHUB_API = "https://api.github.com"
# Unbuffered by construction. This script's whole job is to print ONE url and then
# block, so a buffered stdout is not a cosmetic problem: piped or wrapped, the url
# never appears and the attendee stares at a silent terminal waiting for the thing
# they are supposed to open. A tty happens to line-buffer, which makes the bug
# invisible in the exact place it was authored.
print = functools.partial(print, flush=True)  # noqa: A001 (deliberate shadow)
# GitHub voids the temporary code one hour after the manifest is submitted, and an
# attendee who wandered off is better served by a clear timeout than by a hung script.
INSTALL_WAIT_S = 600


def fail(message: str) -> "None":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_base_url(explicit: str | None) -> str:
    """The public https origin this box is reachable at, from the browser's point of view.

    Order: an explicit flag, then WORKSHOP_PUBLIC_BASE_URL, then the SSM parameter the
    workshop stack publishes. Never guessed from the instance's own hostname: the box
    sits behind CloudFront, so its local view of itself is not what GitHub can reach.
    """
    for candidate in (explicit, os.environ.get("WORKSHOP_PUBLIC_BASE_URL")):
        if candidate:
            return candidate.rstrip("/")
    try:
        out = subprocess.run(
            ["aws", "ssm", "get-parameter", "--name", "/workshop/public-base-url",
             "--query", "Parameter.Value", "--output", "text"],
            capture_output=True, text=True, timeout=20, check=True)
        value = out.stdout.strip()
        if value and value != "None":
            return value.rstrip("/")
    except (subprocess.SubprocessError, OSError):
        pass
    fail("could not resolve the workshop's public URL. Pass --base-url "
         "https://<your CloudFront domain> (the host part of WorkshopUrl), or set "
         "WORKSHOP_PUBLIC_BASE_URL.")
    raise AssertionError("unreachable")


def app_jwt(app_id: str, pem: str) -> str:
    """A short-lived App JWT, signed exactly the way the MCP server signs its own."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        fail("python3 has no `cryptography` module, so this script cannot sign an App "
             "JWT. Install it (pip3 install cryptography) or use the manual path.")
        raise AssertionError("unreachable")

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    sig = b64(key.sign(head + b"." + body, padding.PKCS1v15(), hashes.SHA256()))
    return (head + b"." + body + b"." + sig).decode()


def github(method: str, path: str, token: str | None = None) -> dict:
    req = urllib.request.Request(f"{GITHUB_API}{path}", method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "agentcore-workshop")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def build_manifest(base_url: str, port: int, name: str) -> dict:
    """The App this workshop needs, and nothing more.

    These four permissions are exactly what the Gateway's tools use: read and write
    file contents to push a branch, issues to comment gate and review evidence, pull
    requests to open and merge them, and metadata because GitHub requires it. The
    webhook is registered inactive because nothing in this workshop listens for one;
    `hook_attributes.url` is required by the manifest schema even so.
    """
    return {
        "name": name,
        "url": base_url,
        "redirect_url": f"{base_url}/proxy/{port}/callback",
        "public": False,
        "default_permissions": {
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
            "metadata": "read",
        },
        "default_events": [],
        "hook_attributes": {"url": f"{base_url}/proxy/{port}/webhook", "active": False},
    }


_PAGE_CSS = (
    "body{font-family:system-ui,-apple-system,sans-serif;max-width:34rem;"
    "margin:12vh auto;padding:0 1.5rem;color:#16191f;line-height:1.6}"
    "h1{font-size:1.3rem}code{background:#f1f3f5;padding:.15rem .35rem;"
    "border-radius:.2rem}a{color:#0972d3}"
)


def start_page(manifest: dict, state: str) -> bytes:
    """A page whose only job is to POST the manifest to GitHub.

    It has to be a POST (the manifest travels in a form field, not a query string), so
    a plain link cannot do it. The form auto-submits, and the button is the fallback for
    a browser that blocks the scripted submit.
    """
    body = json.dumps(manifest)
    return (
        "<!doctype html><meta charset=utf-8><title>Create the workshop GitHub App</title>"
        f"<style>{_PAGE_CSS}</style>"
        "<h1>Creating your GitHub App</h1>"
        "<p>GitHub will show you a confirmation page with the name and permissions "
        "already filled in. Choose <strong>Create GitHub App</strong>.</p>"
        f'<form id="f" method="post" action="https://github.com/settings/apps/new?state={state}">'
        f'<input type="hidden" name="manifest" value=\'{body}\'>'
        '<button type="submit">Continue to GitHub</button></form>'
        "<script>document.getElementById('f').submit()</script>"
    ).encode()


def result_page(title: str, html: str) -> bytes:
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            f"<style>{_PAGE_CSS}</style><h1>{title}</h1>{html}").encode()


class Handler(BaseHTTPRequestHandler):
    manifest: dict = {}
    state: str = ""
    result: dict = {}
    done = threading.Event()

    def log_message(self, *_args) -> None:  # keep the terminal readable
        return

    def _send(self, code: int, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        # code-server strips the /proxy/<port> prefix, so this normally sees / and
        # /callback. Anything that is not the callback serves the start page, because
        # the prefix handling is the proxy's business and a 404 here would look like a
        # broken workshop rather than a path detail.
        if parsed.path.rstrip("/").rsplit("/", 1)[-1] != "callback":
            self._send(200, start_page(self.manifest, self.state))
            return

        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            self._send(400, result_page(
                "No code from GitHub",
                "<p>GitHub redirected here without a <code>code</code>. Start over in "
                "the terminal.</p>"))
            return
        if state != self.state:
            # A mismatched state means this redirect did not come from the form this
            # run served, so refuse it rather than converting somebody else's code.
            self._send(400, result_page(
                "State mismatch",
                "<p>This redirect does not belong to the run waiting in your terminal. "
                "Start over in the terminal.</p>"))
            return
        try:
            created = github("POST", f"/app-manifests/{code}/conversions")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:400]
            self._send(502, result_page(
                "GitHub refused the conversion",
                f"<p>HTTP {exc.code}. The code is valid for one hour and once only.</p>"
                f"<pre>{detail}</pre>"))
            # Assign on the CLASS, not on self: the handler instance is discarded after
            # this request, so an instance attribute would never reach main().
            Handler.result = {"error": f"conversion failed: HTTP {exc.code}"}
            self.done.set()
            return
        Handler.result = created
        slug = created.get("slug", "")
        self._send(200, result_page(
            "App created",
            f"<p>App <code>{slug}</code> (id {created.get('id')}) exists, and its "
            "private key is on the workshop host.</p>"
            "<p><strong>One step left:</strong> install it on the repository you "
            f'created, at <a href="https://github.com/apps/{slug}/installations/new" '
            'target="_blank" rel="noreferrer">this install page</a>. Choose '
            "<strong>Only select repositories</strong> and pick that one repo.</p>"
            "<p>Your terminal is waiting for the installation and will finish on its "
            "own.</p>"))
        self.done.set()


def wait_for_installation(app_id: str, pem: str, expect_owner: str | None) -> str:
    """Poll the App for its own installations, so nobody transcribes an id.

    The installation id is the one number attendees most often paste in the wrong box,
    and the App can simply be asked. Installing is still a human decision, so this
    waits rather than assuming.
    """
    deadline = time.time() + INSTALL_WAIT_S
    reminded = False
    while time.time() < deadline:
        try:
            installs = github("GET", "/app/installations", token=app_jwt(app_id, pem))
        except urllib.error.HTTPError as exc:
            fail(f"GitHub rejected the App JWT (HTTP {exc.code}); the private key and "
                 f"App id {app_id} do not match.")
            raise AssertionError("unreachable")
        if installs:
            if expect_owner:
                for item in installs:
                    if (item.get("account") or {}).get("login", "").lower() == expect_owner.lower():
                        return str(item["id"])
                print(f"  installed, but not under {expect_owner}; still waiting for "
                      f"an installation on that account...")
            else:
                return str(installs[0]["id"])
        if not reminded:
            print("  waiting for you to install the App (the browser tab has the link)...")
            reminded = True
        time.sleep(3)
    fail("timed out waiting for the App to be installed. Install it, then re-run with "
         "--app-id and --key-file to finish without creating a second App.")
    raise AssertionError("unreachable")


def write_env(app_id: str, key_path: Path, installation_id: str) -> None:
    ENV_FILE.write_text(
        "# Written by create-github-app.py. Source it, then run ./deploy-all.sh\n"
        f"export GITHUB_APP_ID={app_id}\n"
        f'export GITHUB_APP_PRIVATE_KEY_FILE="{key_path}"\n'
        f"export GITHUB_APP_INSTALLATION_ID={installation_id}\n")
    ENV_FILE.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="public https origin of this workshop host")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MANIFEST_PORT", "8765")))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPO", ""),
                        help="owner/repository the App will be installed on")
    parser.add_argument("--key-file", default=str(DEFAULT_KEY_PATH))
    parser.add_argument("--app-id",
                        help="finish an App that already exists (skips creation)")
    args = parser.parse_args()

    key_path = Path(args.key_file).expanduser()
    owner = args.repo.split("/")[0] if "/" in args.repo else None

    if args.app_id:
        # Resume path: the App exists and its key is on disk, only the installation is
        # missing. This is what a timeout or a closed browser tab leaves behind, and
        # creating a second App would be the wrong repair.
        if not key_path.is_file():
            fail(f"--app-id given but no private key at {key_path}")
        pem = key_path.read_text()
        app_id = args.app_id
    else:
        base_url = resolve_base_url(args.base_url)
        state = secrets.token_urlsafe(24)
        # The name has to be unique across GitHub, so make it unguessable rather than
        # asking every attendee in the room to invent one.
        name = f"AgentCore GitHub MCP {secrets.token_hex(3)}"
        Handler.manifest = build_manifest(base_url, args.port, name)
        Handler.state = state
        server = HTTPServer(("127.0.0.1", args.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        print("\nOpen this URL in the SAME browser you are using for VS Code:\n")
        print(f"    {base_url}/proxy/{args.port}/\n")
        print("GitHub shows a confirmation page with the name and the four permissions")
        print("already set. Choose 'Create GitHub App', then install it on your repo.")
        print("This terminal finishes on its own.\n")

        if not Handler.done.wait(timeout=INSTALL_WAIT_S):
            fail("timed out waiting for GitHub to redirect back. Check that the URL "
                 "above opened, then run this script again.")
        server.shutdown()
        created = Handler.result
        if "error" in created or not created.get("pem"):
            fail(created.get("error", "GitHub returned no private key"))
        app_id = str(created["id"])
        pem = created["pem"]
        key_path.write_text(pem)
        key_path.chmod(0o600)
        print(f"App id {app_id} created; private key written to {key_path}")

    installation_id = wait_for_installation(app_id, pem, owner)
    write_env(app_id, key_path, installation_id)
    print(f"Installation id {installation_id} discovered from the App itself.")
    print(f"Wrote {ENV_FILE}\n")
    print("Next:")
    print(f"    source {ENV_FILE.name}")
    print("    ./deploy-all.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
