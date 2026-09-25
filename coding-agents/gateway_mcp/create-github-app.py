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

    export GITHUB_REPO=owner/repository
    python3 create-github-app.py        # then: source github-app.env && ./deploy-all.sh

RECOVERY. The callback receiver waits ten minutes. If it times out, run
`python3 create-github-app.py --resume` in the same checkout and refresh the original
failed callback tab. Resume keeps the original state, App name, repository and host;
it never reposts a registration form. The saved callback window expires one hour
after setup began. Once the App id and key are saved, --resume skips conversion and
waits for installation instead (also at most ten minutes).

If you never reached GitHub's 'Create GitHub App' button (the tab was closed, or
GITHUB_REPO changed before creation), `--restart` sets that unfinished setup aside and
prints a new setup URL. It refuses once an App was created and its key saved. If GitHub
rejects the manifest with "Public cannot be private", add `--public`.

GITHUB_REPO accepts owner/repository; a pasted https://github.com/ URL, a trailing
.git, or a trailing slash is normalized, and an email or a missing owner is explained.

If an exchange's outcome is uncertain, its code is not retried. Recover the EXISTING
App and its private key in GitHub settings, then use --app-id ID --key-file PATH.
This is also the path for an older helper that left no saved checkpoint.

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
from contextlib import contextmanager
import fcntl
import functools
import hashlib
import html
import json
import math
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY_PATH = REPO_ROOT / "agentcore-github-mcp.private-key.pem"
ENV_FILE = Path(__file__).resolve().parent / "github-app.env"
STATE_FILE = Path(__file__).resolve().parent / ".github-app-setup.json"
GITHUB_API = "https://api.github.com"
# Unbuffered by construction. This script's whole job is to print ONE url and then
# block, so a buffered stdout is not a cosmetic problem: piped or wrapped, the url
# never appears and the attendee stares at a silent terminal waiting for the thing
# they are supposed to open. A tty happens to line-buffer, which makes the bug
# invisible in the exact place it was authored.
print = functools.partial(print, flush=True)  # noqa: A001 (deliberate shadow)
# Bound each receiver/installation wait. The saved callback expiry is conservative:
# it starts before we publish the form, and restarting never renews it.
INSTALL_WAIT_S = 600
CALLBACK_WAIT_S = 600
MANIFEST_LIFETIME_S = 3600


class SetupError(RuntimeError):
    """A message safe to display without a callback URL, code, state, or PEM."""


class CallbackError(SetupError):
    def __init__(self, message: str, status: int = 400, terminal: bool = False):
        super().__init__(message)
        self.status, self.terminal = status, terminal


def private_bytes(path: Path) -> bytes:
    """Do not follow a symlink or read another user's / shared secret file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise SetupError("Setup/key files must be regular, owner-only files.")
            return stream.read()
    except OSError:
        raise SetupError("Could not read the private setup/key file.") from None


def atomic_private_write(path: Path, data: bytes, *, replace: bool = True) -> None:
    """Publish complete 0600 bytes; a key destination is never overwritten."""
    temporary = None
    try:
        if path.exists() or path.is_symlink():
            previous = private_bytes(path)
            if not replace:
                if previous == data:
                    return
                raise SetupError("A different key already exists; it was left unchanged.")
        fd, name = tempfile.mkstemp(prefix=".github-app-setup-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)  # Atomic publication without replacing a key.
            except FileExistsError:
                if private_bytes(path) != data:
                    raise SetupError("A different key already exists; it was left unchanged.")
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise SetupError("Could not durably save setup data. Preserve the checkpoint and use --resume.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def setup_lock():
    """Only one process may receive or resume this setup."""
    lock_path = STATE_FILE.with_suffix(".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise SetupError("The setup lock must be a regular, owner-only file.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("Another App setup is still running in this checkout.") from None
        yield
    finally:
        os.close(fd)


_REPO_SHAPE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def normalize_repo(value: str | None) -> str:
    """owner/name from what attendees actually paste: a URL, a trailing .git or slash."""
    repo = (value or "").strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        if repo.lower().startswith(prefix):
            repo = repo[len(prefix):]
            break
    repo = repo.rstrip("/")
    return repo[:-4] if repo.lower().endswith(".git") else repo


def repo_problem(value: str) -> str:
    """Say which part of owner/repository is wrong, with the fix."""
    example = "for example: export GITHUB_REPO=\"octocat/my-agent-project\""
    if "@" in value.split("/")[0]:
        return f"GITHUB_REPO must start with your GitHub username, not an email address ({example})."
    if "/" not in value:
        return (f"GITHUB_REPO needs your GitHub username too: '{value}' should be "
                f"'your-username/{value}' ({example}).")
    return f"GITHUB_REPO must look like owner/repository, not '{value}' ({example})."


def normalized_base_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise SetupError("The workshop base URL must be an HTTPS origin.")
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise SetupError("The workshop base URL must be an HTTPS origin, without a path or query.")
    try:
        port = parsed.port
    except ValueError:
        raise SetupError("The workshop base URL has an invalid port.") from None
    return f"https://{parsed.hostname.lower()}" + (f":{port}" if port and port != 443 else "")


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


class SetupSession:
    """A private journal, including the uncertainty boundary before conversion."""

    def __init__(self, path: Path, data: dict):
        self.path, self.data = path, data
        self.lock = threading.Lock()
        self.deadline: float | None = None
        self.closed = False

    def save(self) -> None:
        atomic_private_write(self.path, (json.dumps(self.data, indent=2) + "\n").encode())

    @classmethod
    def create(cls, base_url: str, port: int, repo: str, key_path: Path, public: bool = False):
        if STATE_FILE.exists() or STATE_FILE.is_symlink():
            saved = saved_summary()
            raise SetupError(
                f"A saved App setup exists{saved}. Continue it with --resume; do not create "
                "another App. If you never chose 'Create GitHub App' on GitHub (no such App "
                "under GitHub Settings > Developer settings > GitHub Apps), start over with --restart.")
        if key_path.exists() or key_path.is_symlink():
            raise SetupError("A key already exists. Finish its App with --app-id and --key-file.")
        now = time.time()
        name = f"AgentCore GitHub MCP {secrets.token_hex(3)}"
        manifest = build_manifest(base_url, port, name, public)
        session = cls(STATE_FILE, {
            "version": 1, "phase": "awaiting_callback",
            "state": secrets.token_urlsafe(24), "name": name,
            "repo": repo, "base_url": base_url, "port": port,
            "key_file": str(key_path), "checkout": str(REPO_ROOT.resolve()),
            "created_at": now, "expires_at": now + MANIFEST_LIFETIME_S,
            "manifest": manifest, "manifest_sha256": manifest_digest(manifest),
            "public": public,
        })
        session.save()
        return session

    @classmethod
    def load(cls):
        try:
            data = json.loads(private_bytes(STATE_FILE))
            valid = (
                type(data["version"]) is int and data["version"] == 1
                and data["phase"] in {"awaiting_callback", "exchanging", "exchange_failed",
                                      "converted", "complete"}
                and data["checkout"] == str(REPO_ROOT.resolve())
                and normalized_base_url(data["base_url"]) == data["base_url"]
                and type(data["port"]) is int and 1 <= data["port"] <= 65535
                and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", data["repo"]))
                and Path(data["key_file"]).is_absolute()
                and type(data.get("public", False)) is bool
                and data["manifest"] == build_manifest(data["base_url"], data["port"], data["name"],
                                                       data.get("public", False))
                and manifest_digest(data["manifest"]) == data["manifest_sha256"]
                and all(type(data[key]) in (int, float) and math.isfinite(data[key])
                        for key in ("created_at", "expires_at"))
                and 0 < data["expires_at"] - data["created_at"] <= MANIFEST_LIFETIME_S
            )
            if data["phase"] != "complete":
                valid = valid and isinstance(data["state"], str) and bool(
                    re.fullmatch(r"[A-Za-z0-9_-]{24,}", data["state"]))
            if data["phase"] in {"converted", "complete"}:
                valid = valid and all((
                    re.fullmatch(r"[1-9][0-9]*", data["app"]["id"]),
                    re.fullmatch(r"[A-Za-z0-9-]+", data["app"]["slug"]),
                    re.fullmatch(r"[0-9a-f]{64}", data["key_sha256"]),
                ))
            if data["phase"] == "converted":
                valid = valid and isinstance(data["pem"], str) and bool(
                    re.fullmatch(r"[0-9a-f]{64}", data["code_sha256"]))
            if not valid:
                raise ValueError("Invalid checkpoint")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SetupError("Invalid or foreign setup checkpoint; preserve it and recover the existing App.") from None
        return cls(STATE_FILE, data)

    def check_context(self, base_url: str, repo: str | None, port: int | None,
                      key_path: Path | None) -> None:
        if (base_url != self.data["base_url"]
                or (repo is not None and repo != self.data["repo"])
                or (port is not None and port != self.data["port"])
                or (key_path is not None and str(key_path) != self.data["key_file"])):
            raise SetupError(
                "Saved setup belongs to a different host, repository, port, or key path: it "
                f"was started for {self.data['repo']}. To continue it, export "
                f"GITHUB_REPO={self.data['repo']} and use "
                f"--resume. If App '{self.data['name']}' was never created on GitHub, start "
                "over with --restart.")

    def check_waiting(self) -> None:
        if self.data["phase"] != "awaiting_callback":
            raise SetupError(
                "The previous conversion may have consumed its code. It will not be retried. "
                "Recover the existing App in GitHub settings, then use --app-id and --key-file.")
        if time.time() >= self.data["expires_at"]:
            raise SetupError(
                "The saved callback window expired. Preserve this checkpoint and recover the "
                "existing App in GitHub settings; do not register a replacement.")

    def credentials(self) -> dict:
        app = self.data["app"]
        key_path = Path(self.data["key_file"])
        if self.data["phase"] == "complete":
            pem = private_bytes(key_path).decode()
        else:
            pem = self.data["pem"]
        if hashlib.sha256(pem.encode()).hexdigest() != self.data["key_sha256"]:
            raise SetupError("Saved key evidence changed; no file was overwritten.")
        atomic_private_write(key_path, pem.encode(), replace=False)
        return {**app, "pem": pem}

    def receive(self, code: str, state: str) -> dict:
        with self.lock:
            if self.closed:
                raise CallbackError("The receiver wait ended. Restart it with --resume.", 409)
            if not secrets.compare_digest(state.encode(), self.data.get("state", "").encode()):
                raise CallbackError("State mismatch. Use the original setup; no code was exchanged.")
            if time.time() >= self.data["expires_at"]:
                raise CallbackError("The saved callback window expired; recover the existing App.")
            digest = hashlib.sha256(code.encode()).hexdigest()
            if self.data["phase"] == "converted":
                if digest != self.data["code_sha256"]:
                    raise CallbackError("This setup already converted a different callback.")
                return self.credentials()
            try:
                self.check_waiting()
            except SetupError as exc:
                raise CallbackError(str(exc), 409) from None
            remaining = self.deadline - time.monotonic() if self.deadline is not None else CALLBACK_WAIT_S
            if remaining <= 0:
                raise CallbackError("The receiver wait ended. Restart it with --resume.")
            previous = self.data.copy()
            self.data.update(phase="exchanging", code_sha256=digest)
            self.save()  # A lost response must never look like an unused code.
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    self.data = previous  # No request started; resume is still safe.
                    self.save()
                    raise CallbackError(
                        "The receiver wait ended before conversion. Restart it with --resume.",
                        409, terminal=True)
        # Do not hold the checkpoint lock during network I/O. Closing the bounded
        # receiver fences off late responses before another process can resume it.
        failure = None
        try:
            created = github("POST", f"/app-manifests/{quote(code, safe='')}/conversions",
                             timeout=min(30, remaining))
            app_id, slug, pem = str(created["id"]), created["slug"], created["pem"]
            if (not re.fullmatch(r"[1-9][0-9]*", app_id)
                    or not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9-]+", slug)
                    or not isinstance(pem, str) or not pem.strip()):
                raise ValueError("Invalid conversion result")
        except Exception as exc:
            # No URL, response body, or callback value belongs in an error.
            failure = {"exchange_error": type(exc).__name__}
            if isinstance(exc, urllib.error.HTTPError):
                failure["http_status"] = exc.code
                exc.close()
        with self.lock:
            if self.closed:
                raise CallbackError(
                    "The receiver ended during conversion. Preserve the checkpoint; "
                    "the exchange outcome is uncertain and will not be retried.",
                    502, terminal=True)
            if failure is not None:
                self.data.update(phase="exchange_failed", **failure)
                self.save()
                raise CallbackError(
                    "Conversion did not complete safely and will not be retried. Preserve the "
                    "checkpoint; recover the existing App in GitHub settings.",
                    502, terminal=True) from None
            previous = self.data
            self.data = {
                **previous, "phase": "converted", "app": {"id": app_id, "slug": slug},
                "pem": pem, "key_sha256": hashlib.sha256(pem.encode()).hexdigest(),
            }
            try:
                self.save()  # Save the one-time response before any browser acknowledgement.
            except Exception:
                # A duplicate must not acknowledge an in-memory result whose
                # durable publication failed. Only a new load may recover it.
                self.data = previous
                raise
            return self.credentials()

    def close(self) -> None:
        with self.lock:
            self.closed = True

    def complete(self, installation_id: str) -> None:
        self.data.update(phase="complete", installation_id=installation_id)
        for key in ("state", "pem", "code_sha256"):
            self.data.pop(key, None)
        self.save()


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


def github(method: str, path: str, token: str | None = None, *, timeout: float = 30) -> dict:
    # urllib's timeout alone is per socket operation: DNS or a trickling response
    # can outlast it. Bound the caller's entire wait without retrying the request.
    # A timed-out POST has an uncertain outcome, handled by the durable journal.
    done, outcome = threading.Event(), {}

    def request():
        try:
            req = urllib.request.Request(f"{GITHUB_API}{path}", method=method)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "agentcore-workshop")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
            outcome["result"] = json.loads(raw) if raw else {}
        except Exception as exc:
            outcome["error"] = exc
        finally:
            done.set()

    if timeout <= 0:
        raise TimeoutError("GitHub request budget expired before the request.")
    threading.Thread(target=request, daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError("GitHub response wait expired; the request outcome is uncertain.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def build_manifest(base_url: str, port: int, name: str, public: bool = False) -> dict:
    """The App this workshop needs, and nothing more.

    These four permissions are exactly what the Gateway's tools use: read and write
    file contents to push a branch, issues to comment gate and review evidence, pull
    requests to open and merge them, and metadata because GitHub requires it. The
    webhook is registered inactive because nothing in this workshop listens for one;
    `hook_attributes.url` is required by the manifest schema even so.

    The App is private unless --public is chosen. Some GitHub accounts reject a
    private manifest ("Public cannot be private"); a public App is still installed
    only on the repository its owner selects, and its private key stays on this host.
    """
    return {
        "name": name,
        "url": base_url,
        "redirect_url": f"{base_url}/proxy/{port}/callback",
        "public": public,
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
    body = html.escape(json.dumps(manifest), quote=True)
    return (
        "<!doctype html><meta charset=utf-8><title>Create the workshop GitHub App</title>"
        f"<style>{_PAGE_CSS}</style>"
        "<h1>Creating your GitHub App</h1>"
        "<p>On GitHub, confirm the App name and choose <strong>Create GitHub App</strong>. "
        "Then review its permissions and select your workshop repository when you install it.</p>"
        f'<form id="f" method="post" action="https://github.com/settings/apps/new?state={quote(state, safe="")}">'
        f'<input type="hidden" name="manifest" value=\'{body}\'>'
        '<button type="submit">Continue to GitHub</button></form>'
        "<script>document.getElementById('f').submit()</script>"
    ).encode()


def result_page(title: str, html: str) -> bytes:
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            f"<style>{_PAGE_CSS}</style><h1>{title}</h1>{html}").encode()


class Handler(BaseHTTPRequestHandler):
    setup_session: SetupSession
    resuming = False
    result: dict = {}
    done = threading.Event()

    def log_message(self, *_args) -> None:  # keep the terminal readable
        return

    def _send(self, code: int, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        session = self.setup_session
        prefix = f'/proxy/{session.data["port"]}'
        if parsed.path.rstrip("/") in ("", prefix):
            if self.resuming or session.data["phase"] != "awaiting_callback":
                self._send(200, result_page(
                    "Resume the existing App setup",
                    "<p>Refresh the original GitHub callback tab while this receiver is running. "
                    "Do not create another App. If you no longer have that tab, recover the "
                    "existing App from GitHub settings using <code>--app-id</code> and "
                    "<code>--key-file</code>. If you never chose Create GitHub App, stop the "
                    "terminal helper and run it with <code>--restart</code>.</p>"))
            else:
                self._send(200, start_page(session.data["manifest"], session.data["state"]))
            return
        # Accept code-server's stripped and unstripped paths, not arbitrary paths
        # whose final component happens to be "callback".
        if parsed.path not in ("/callback", prefix + "/callback"):
            self._send(404, result_page("Not found", "<p>Use the original setup tab.</p>"))
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (set(query) != {"code", "state"}
                or any(len(query[key]) != 1 or not query[key][0] for key in query)):
            self._send(400, result_page(
                "Invalid callback",
                "<p>Exactly one nonempty code and state are required. No code was exchanged.</p>"))
            return
        finished = False
        try:
            try:
                created = session.receive(query["code"][0], query["state"][0])
            except CallbackError as exc:
                finished = exc.terminal
                if finished:
                    type(self).result = {"error": str(exc)}
                self._send(exc.status, result_page("Setup not completed", f"<p>{html.escape(str(exc))}</p>"))
                return
            except SetupError as exc:
                finished = True
                type(self).result = {"error": str(exc)}
                self._send(500, result_page("Setup not saved", f"<p>{html.escape(str(exc))}</p>"))
                return
            # Both the journal and key already exist durably. Record the result
            # before replying, and release main after replying, even on disconnect.
            type(self).result = created
            finished = True
            slug = created["slug"]
            self._send(200, result_page(
                "App created",
                f"<p>App <code>{slug}</code> (id {created['id']}) exists, and its "
                "private key is saved on the workshop host.</p>"
                "<p><strong>One step left:</strong> install it on the repository you "
                f'created, at <a href="https://github.com/apps/{slug}/installations/new" '
                'target="_blank" rel="noreferrer">this install page</a>. Choose '
                "<strong>Only select repositories</strong> and pick that one repo.</p>"
                "<p>Your terminal is waiting for the installation. If it stopped, "
                "run this helper with <code>--resume</code>.</p>"))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # The browser may leave; the saved outcome must still reach main.
        finally:
            if finished:
                self.done.set()


class CallbackServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


def wait_for_callback(session: SetupSession, *, resuming: bool) -> dict:
    session.check_waiting()
    handler = type("SetupHandler", (Handler,), {
        "setup_session": session, "resuming": resuming,
        "result": {}, "done": threading.Event(),
    })
    server = CallbackServer(("127.0.0.1", session.data["port"]), handler)
    session.deadline = time.monotonic() + min(
        CALLBACK_WAIT_S, session.data["expires_at"] - time.time())
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True).start()
    try:
        if resuming:
            print("\nReceiver resumed for the SAME App. Refresh the original failed callback tab.")
            print("Do not open a new GitHub App registration.")
            print("If you never chose 'Create GitHub App' on GitHub, press Ctrl+C and run:")
            print("    python3 create-github-app.py --restart\n")
        else:
            print("\nOpen this URL in the SAME browser you are using for VS Code:\n")
            print(f'    {session.data["base_url"]}/proxy/{session.data["port"]}/\n')
            print("If a 'Welcome to code-server' page asks for a password, enter the")
            print("WorkshopPassword from Event outputs; it then continues to GitHub.")
            print("On GitHub, confirm the App name and choose 'Create GitHub App'.")
            print("Then review its permissions and install it on your workshop repository.\n")
        print("This receiver waits at most ten minutes. If it stops, run this helper with --resume.")
        if not handler.done.wait(timeout=max(0, session.deadline - time.monotonic())):
            raise SetupError(
                "Timed out waiting for the callback. The original setup is saved. Run with "
                "--resume, then refresh the original failed callback tab; do not create another App.")
        if "error" in handler.result:
            raise SetupError(handler.result["error"])
        return handler.result
    finally:
        session.close()  # Late network responses cannot rewrite a resumed checkpoint.
        server.shutdown()
        server.server_close()


def wait_for_installation(app_id: str, pem: str, expect_owner: str | None) -> str:
    """Poll the App for its own installations, so nobody transcribes an id.

    The installation id is the one number attendees most often paste in the wrong box,
    and the App can simply be asked. Installing is still a human decision, so this
    waits rather than assuming.
    """
    deadline = time.monotonic() + INSTALL_WAIT_S
    reminded = False
    while time.monotonic() < deadline:
        try:
            token = app_jwt(app_id, pem)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            installs = github("GET", "/app/installations", token=token, timeout=min(30, remaining))
        except urllib.error.HTTPError as exc:
            exc.close()
            fail(f"GitHub rejected the App JWT (HTTP {exc.code}); the private key and "
                 f"App id {app_id} do not match.")
            raise AssertionError("unreachable")
        if time.monotonic() >= deadline:
            break
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
        time.sleep(max(0, min(3, deadline - time.monotonic())))
    fail("timed out waiting for the App to be installed. Install it, then re-run with "
         "--resume for a saved setup, or --app-id and --key-file for an existing key.")
    raise AssertionError("unreachable")


def saved_summary() -> str:
    """A best-effort ' for owner/repo (App 'name')' for a checkpoint that may be foreign."""
    try:
        data = json.loads(private_bytes(STATE_FILE))
        return f" for {data['repo']} (App '{data['name']}')"
    except (SetupError, ValueError, KeyError, TypeError):
        return ""


def abandon_unfinished_setup() -> None:
    """Move aside a setup that never reached GitHub's Create step, keeping it as evidence.

    Only an awaiting_callback checkpoint qualifies: no code was exchanged, so no private
    key exists for it. If its App was created on GitHub anyway, that App has no key and
    no installation; the message names it so the attendee can delete it."""
    if not (STATE_FILE.exists() or STATE_FILE.is_symlink()):
        return
    try:
        data = json.loads(private_bytes(STATE_FILE))
        phase, name = data["phase"], data["name"]
    except (SetupError, ValueError, KeyError, TypeError):
        raise SetupError("The saved setup cannot be read; preserve it and ask a facilitator.") from None
    if phase != "awaiting_callback":
        raise SetupError(
            f"The saved setup already created App '{name}'. Use --resume, or --app-id and "
            "--key-file; --restart would abandon its private key.")
    archived = STATE_FILE.with_name(f"{STATE_FILE.name}.abandoned-{int(time.time())}")
    os.rename(STATE_FILE, archived)
    print(f"Set aside the unfinished setup for '{name}' ({archived.name}).")
    print("If an App with that name appears under GitHub Settings > Developer settings > "
          "GitHub Apps, it has no private key; you can delete it.")


def discover_key_file() -> Path:
    """The one private key on this host, when --key-file was not given."""
    if DEFAULT_KEY_PATH.exists():
        return DEFAULT_KEY_PATH
    candidates = sorted({path.resolve() for folder in (REPO_ROOT, ENV_FILE.parent, Path.home())
                         for path in folder.glob("*.private-key.pem")})
    if len(candidates) == 1:
        print(f"Using private key {candidates[0]}")
        return candidates[0]
    if not candidates:
        raise SetupError(
            "No private key found on this host. In the App's GitHub settings choose "
            "'Generate a private key', drag the .pem into the VS Code Explorer, then pass "
            "--key-file PATH.")
    raise SetupError("Several private keys found; pass --key-file with one of: "
                     + ", ".join(str(path) for path in candidates))


def read_key(path: Path) -> str:
    try:
        return private_bytes(path).decode()
    except SetupError:
        if path.exists() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SetupError(f"The key file is readable by others. Run: chmod 600 {path}") from None
        raise


def write_env(app_id: str, key_path: Path, installation_id: str) -> None:
    atomic_private_write(ENV_FILE, (
        "# Written by create-github-app.py. Source it, then run ./deploy-all.sh\n"
        f"export GITHUB_APP_ID={app_id}\n"
        f"export GITHUB_APP_PRIVATE_KEY_FILE={shlex.quote(str(key_path))}\n"
        f"export GITHUB_APP_INSTALLATION_ID={installation_id}\n").encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="public https origin of this workshop host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--repo",
                        help="owner/repository the App will be installed on")
    parser.add_argument("--key-file", help="private key file (defaults to the workshop checkout)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--app-id", help="finish an App whose private key is already on disk")
    mode.add_argument("--resume", action="store_true",
                      help="resume the saved App setup, without creating another App")
    mode.add_argument("--restart", action="store_true",
                      help="set aside a setup that never reached GitHub's Create step, then start again")
    parser.add_argument("--public", action="store_true",
                        help="register a public App, only if GitHub rejects the private "
                             "manifest with 'Public cannot be private'")
    args = parser.parse_args(argv)
    try:
        raw_repo = args.repo if args.repo is not None else os.environ.get("GITHUB_REPO")
        repo = normalize_repo(raw_repo) if raw_repo is not None else None
        port = args.port if args.port is not None else (
            int(os.environ["MANIFEST_PORT"]) if "MANIFEST_PORT" in os.environ else None)
        key_override = Path(args.key_file).expanduser().absolute() if args.key_file else None
        if port is not None and not 1 <= port <= 65535:
            raise SetupError("The callback port must be between 1 and 65535.")
        if repo and not _REPO_SHAPE.fullmatch(repo):
            raise SetupError(repo_problem(repo))
        if repo and repo != (raw_repo or "").strip():
            print(f"Using repository {repo}")
        if args.public and (args.resume or args.app_id):
            raise SetupError("--public applies only to a new setup; combine it with --restart.")
        with setup_lock():
            session = None
            if args.app_id:
                if not re.fullmatch(r"[1-9][0-9]*", args.app_id):
                    raise SetupError("The App id must be a positive integer.")
                key_path = key_override or discover_key_file()
                pem = read_key(key_path)
                app_id = args.app_id
            else:
                base_url = normalized_base_url(resolve_base_url(args.base_url))
                if args.resume:
                    session = SetupSession.load()
                    session.check_context(base_url, repo, port, key_override)
                else:
                    if not repo:
                        raise SetupError("Set GITHUB_REPO or pass --repo owner/repository before "
                                         "starting App setup (for example: export "
                                         "GITHUB_REPO=\"octocat/my-agent-project\").")
                    if args.restart:
                        abandon_unfinished_setup()
                    session = SetupSession.create(
                        base_url, port or 8765, repo, key_override or DEFAULT_KEY_PATH, args.public)
                key_path, repo = Path(session.data["key_file"]), session.data["repo"]
                if session.data["phase"] in {"converted", "complete"}:
                    created = session.credentials()
                else:
                    created = wait_for_callback(session, resuming=args.resume)
                app_id, pem = created["id"], created["pem"]
                print(f"App id {app_id}; private key saved at {key_path}")
                print(f'Install this App: https://github.com/apps/{created["slug"]}/installations/new')
            owner = repo.split("/")[0] if repo and "/" in repo else None
            installation_id = wait_for_installation(app_id, pem, owner)
            write_env(app_id, key_path, installation_id)
            if session is not None:
                session.complete(installation_id)
    except SetupError as exc:
        fail(str(exc))
    except (OSError, ValueError):
        # urllib errors may include a one-time code or credential-bearing URL.
        fail("Setup could not finish safely. Preserve its checkpoint and key; use --resume "
             "or recover the existing App with --app-id and --key-file.")
    print(f"Installation id {installation_id} discovered from the App itself.")
    print(f"Wrote {ENV_FILE}\n")
    print("Next:")
    print(f"    source {ENV_FILE.name}")
    print("    ./deploy-all.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
