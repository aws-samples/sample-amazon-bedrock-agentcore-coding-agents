#!/usr/bin/env python3
"""Share an isolated copy of your game with the workshop.

Use the relative foreground start command documented by your game's README:

    python3 orchestrator/gallery.py publish --project ~/game --port 8000 -- npm start
    python3 orchestrator/gallery.py status
    python3 orchestrator/gallery.py unpublish

The event URL comes from the team's SSM configuration. --gallery is an explicit
override for a configured own-account event. The root-owned host helper copies
the application and prepared dependencies, then runs that copy without the
workshop user's files, credentials, or network. The original game stays intact.
The existing workshop distribution serves /play/ through a browser sandbox;
the game does not receive the IDE's cookies or browser storage.
"""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from event_config import EventConfigError, endpoint_region, resolve_gallery_url, validate_endpoint_url

HOST_HELPER = "/usr/local/bin/workshop-game-host"
MAX_RESPONSE_BYTES = 256 * 1024
_GAME_URL = re.compile(r"https://d[a-z0-9]+[.]cloudfront[.]net/play/")
HOSTING_MODE = "isolated-path-v1"


class GalleryError(ValueError):
    """A sharing step did not complete; its message gives the next action."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        # Never forward a signed request to a different URL.
        return None


class _Title(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self.inside = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def game_url(value: str) -> str:
    if not isinstance(value, str) or not _GAME_URL.fullmatch(value):
        raise GalleryError("The host has no valid /play/ GameUrl. Ask the facilitator to check game hosting.")
    return value


def hosted_application(state: dict[str, Any]) -> tuple[str, str]:
    public = game_url(state.get("public_url"))
    application = public + "app/"
    if (state.get("hosting_mode") != HOSTING_MODE
            or state.get("application_url") != application):
        raise GalleryError("The host needs the isolated /play/ game-host update. Ask the facilitator to update its installation.")
    return public, application


def _metadata(title: str, description: str) -> dict[str, str]:
    title, description = title.strip(), description.strip()
    if (not title or len(title) > 100 or len(description) > 280
            or any(ord(c) < 32 or ord(c) == 127 for c in title + description)):
        raise GalleryError("Use a title of 1 to 100 characters and a description of up to 280 characters.")
    return {"title": title, "description": description}


def host_command(operation: str, *arguments: str) -> dict[str, Any]:
    if not Path(HOST_HELPER).is_file():
        raise GalleryError("Game hosting is not installed on this host. Ask the facilitator to update the event stack.")
    try:
        result = subprocess.run(
            ["sudo", "-n", HOST_HELPER, operation, *arguments],
            capture_output=True, text=True, timeout=180, check=False,
        )
    except subprocess.TimeoutExpired:
        raise GalleryError("The host operation timed out. Run gallery.py status before retrying.") from None
    except OSError:
        raise GalleryError("Could not start the game-host helper. Check its installation and sudo access.") from None
    if result.returncode:
        try:
            failure = json.loads(result.stdout)
            error = failure.get("error", {}) if isinstance(failure, dict) else {}
            if not isinstance(error, dict):
                error = {}
            message = error.get("message")
        except (ValueError, TypeError):
            message = None
        if isinstance(message, str) and message.strip():
            detail = message.strip()[:1500]
            rollback = error.get("rollback", {})
            if isinstance(rollback, dict) and rollback.get("succeeded") is True:
                detail += " The previous sharing state was restored."
            raise GalleryError(detail)
        detail = result.stderr.strip()[-1500:] or "The host helper did not complete."
        raise GalleryError(detail)
    try:
        state = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise GalleryError("The host helper returned no usable result. Run gallery.py status before retrying.") from None
    if not isinstance(state, dict):
        raise GalleryError("The host helper returned an invalid result.")
    return state


def _csp_policies(headers) -> list[dict[str, set[str]]]:
    """Multiple enforced policies intersect; report-only headers do not count."""
    get_all = getattr(headers, "get_all", None)
    values = get_all("Content-Security-Policy", []) if get_all else [
        headers.get("Content-Security-Policy", "")
    ]
    policies = []
    for value in values:
        for serialized in value.split(","):
            directives: dict[str, set[str]] = {}
            for item in serialized.split(";"):
                tokens = item.strip().split()
                if tokens:
                    # Browsers use the first occurrence of a directive.
                    directives.setdefault(tokens[0].lower(), set(tokens[1:]))
            policies.append(directives)
    return policies


def _public_boundary(headers, application: str, *, wrapper: bool = False) -> None:
    """Refuse to advertise an old, unconfined server on the IDE's hostname."""
    policies = _csp_policies(headers)
    if wrapper:
        safe = any(
            p.get("default-src") == {"'none'"}
            and p.get("script-src", p.get("default-src")) == {"'none'"}
            and all(p.get(key, p.get("script-src", p.get("default-src"))) == {"'none'"}
                    for key in ("script-src-elem", "script-src-attr"))
            and all(p.get(key, {"'none'"}) == {"'none'"} for key in
                    ("connect-src", "img-src", "font-src", "media-src", "object-src",
                     "worker-src", "manifest-src"))
            and all(p.get(key, {"'none'"}) <= {"'none'", "'unsafe-inline'"} for key in
                    ("style-src", "style-src-elem", "style-src-attr"))
            and p.get("frame-src") == {application}
            and p.get("base-uri") == {"'none'"}
            and p.get("form-action") == {"'none'"}
            for p in policies
        )
    else:
        allowed_connections = {application, application.replace("https://", "wss://", 1)}
        allowed_resources = {
            "script-src": {application, "'unsafe-inline'", "'unsafe-eval'"},
            "script-src-elem": {application, "'unsafe-inline'"},
            "script-src-attr": {"'unsafe-inline'"},
            "style-src": {application, "'unsafe-inline'"},
            "style-src-elem": {application, "'unsafe-inline'"},
            "style-src-attr": {"'unsafe-inline'"},
            "img-src": {application, "data:", "blob:"},
            "font-src": {application, "data:"},
            "media-src": {application, "data:", "blob:"},
        }
        safe = any(
            "sandbox" in p
            and "allow-scripts" in p["sandbox"]
            and p["sandbox"] <= {"allow-scripts", "allow-forms", "allow-pointer-lock"}
            and p.get("default-src") == {"'none'"}
            and bool(p.get("connect-src"))
            and p["connect-src"] <= allowed_connections
            and all(p.get(key) == {"'none'"} for key in
                    ("base-uri", "form-action", "frame-src", "object-src", "worker-src"))
            and all(p.get(key, {"'none'"}) <= values | {"'none'"}
                    for key, values in allowed_resources.items())
            for p in policies
        )
        safe = (safe and headers.get("Access-Control-Allow-Origin") == "*"
                and not headers.get("Access-Control-Allow-Credentials"))
    if not safe or headers.get("Set-Cookie"):
        raise GalleryError("GameUrl is missing the required browser isolation headers. Sharing was not registered; ask the facilitator to check the game-host update.")


def _read_game_page(start: str, application: str, *, wrapper: bool = False) -> str:
    opener = urllib.request.build_opener(_NoRedirect)
    url = start
    for _ in range(4):
        request = urllib.request.Request(url, headers={"Accept": "text/html"})
        try:
            with opener.open(request, timeout=10) as response:
                _public_boundary(response.headers, application, wrapper=wrapper)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise GalleryError("The game's opening page is too large to inspect. Check its root page.")
                if "text/html" not in response.headers.get("Content-Type", "").lower():
                    raise GalleryError("GameUrl did not return a browser page. Check the game's README start command.")
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                try:
                    target = urllib.parse.urljoin(url, exc.headers.get("Location", ""))
                    parsed = urllib.parse.urlsplit(target)
                    original = urllib.parse.urlsplit(application)
                    path = urllib.parse.unquote(parsed.path)
                except ValueError:
                    raise GalleryError("GameUrl returned an invalid redirect. Check the game's root page.") from None
                if (wrapper or parsed.scheme != "https" or parsed.netloc != original.netloc
                        or parsed.username or parsed.password or parsed.fragment
                        or not path.startswith(original.path)
                        or any(part in (".", "..") for part in path.split("/"))
                        or "\\" in path or "%" in path):
                    raise GalleryError("GameUrl redirected outside the isolated game path. Sharing was not registered.") from None
                url = target
                continue
            raise GalleryError(f"GameUrl returned HTTP {exc.code}. Check the shared game with gallery.py status.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GalleryError("Could not reach GameUrl. Check game hosting and CloudFront deployment status.") from None
        except GalleryError:
            raise
        except ValueError:
            raise GalleryError("GameUrl returned an invalid redirect or page. Check the game's root page.") from None
    raise GalleryError("GameUrl redirects repeatedly. Check the game's root page before sharing.")


def read_public_game(base: str) -> str:
    """Verify the wrapper and read metadata only from its isolated application."""
    public = game_url(base)
    application = public + "app/"
    _read_game_page(public, application, wrapper=True)
    parser = _Title()
    parser.feed(_read_game_page(application, application))
    return " ".join(" ".join(parser.parts).split())[:100]


def signed_request(base: str, method: str, body: dict[str, str] | None = None) -> dict[str, Any]:
    base = validate_endpoint_url(base)
    if method not in ("POST", "DELETE"):
        raise GalleryError("Unsupported gallery write operation.")
    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415
    from botocore.exceptions import BotoCoreError  # noqa: PLC0415
    from botocore.session import Session  # noqa: PLC0415

    try:
        credentials = Session().get_credentials()
        if credentials is None:
            raise GalleryError("Run this command on the workshop host using its instance role.")
        data = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
        request = AWSRequest(
            method=method, url=base + "games", data=data,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", endpoint_region(base)).add_auth(request)
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(
            urllib.request.Request(
                request.url, data=data, method=method, headers=dict(request.headers.items()),
            ), timeout=15,
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GalleryError("The gallery returned an oversized response.")
            result = json.loads(raw)
        if not isinstance(result, dict):
            raise GalleryError("The gallery returned an invalid result.")
        return result
    except urllib.error.HTTPError as exc:
        message = ""
        try:
            error = json.loads(exc.read(4096))
            message = str(error.get("error", ""))[:500] if isinstance(error, dict) else ""
        except (ValueError, OSError):
            pass
        if exc.code == 403 and not message:
            message = ("The event did not authorize this host. Ask the facilitator "
                       "to check the team and central gallery permissions.")
        raise GalleryError(f"Gallery registration returned HTTP {exc.code}. {message}".strip()) from None
    except (BotoCoreError, urllib.error.URLError, TimeoutError, OSError):
        raise GalleryError("Could not contact the event gallery. Check the host's AWS session and connection.") from None
    except (json.JSONDecodeError, UnicodeError):
        raise GalleryError("The gallery returned an unreadable result.") from None
    except GalleryError:
        raise
    except ValueError:
        raise GalleryError("The gallery returned an invalid redirect or response.") from None


def publish(args: argparse.Namespace, out=sys.stdout) -> int:
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        raise GalleryError("After --, supply the relative foreground start command from your game's README.")
    if not args.project.is_dir():
        raise GalleryError("The game project directory does not exist.")
    # Resolve before changing any hosting state. Missing event setup fails promptly.
    endpoint = resolve_gallery_url(args.gallery)
    if args.title:
        _metadata(args.title, args.description)
    elif len(args.description.strip()) > 280 or any(ord(c) < 32 or ord(c) == 127 for c in args.description):
        raise GalleryError("Use a description of up to 280 characters without control characters.")
    # An old helper must not expose a game without the browser boundary.
    hosted_application(host_command("status"))
    print("Preparing an isolated copy of your game...", file=out, flush=True)
    state = host_command(
        "publish", "--project", str(args.project.resolve()), "--port", str(args.port), "--", *command,
    )
    if state.get("active") is not True:
        raise GalleryError("The shared game did not become ready. Run gallery.py status.")
    try:
        public_url, _application = hosted_application(state)
        detected_title = read_public_game(public_url)
        metadata = _metadata(args.title or detected_title or args.project.name, args.description)
        result = signed_request(endpoint, "POST", metadata)
        if result.get("published") is not True or result.get("url") != public_url:
            raise GalleryError("The event returned a different game URL or did not confirm registration.")
    except (GalleryError, EventConfigError):
        # A failed registration must not be advertised as a completed share.
        try:
            host_command("unpublish")
            print("Stopped the new shared copy because gallery registration did not complete.", file=out)
        except GalleryError:
            print("Could not stop the shared copy after registration failed. Run gallery.py unpublish.", file=out)
        raise
    print(f"Shared {metadata['title']} for {result.get('team', 'your team')}.", file=out)
    print(f"Play: {public_url}", file=out)
    print(f"Gallery: {endpoint}", file=out)
    print("Open Play in a private browser window to check access without an IDE sign-in.", file=out)
    print("Stop sharing with: python3 orchestrator/gallery.py unpublish", file=out)
    return 0


def unpublish(args: argparse.Namespace, out=sys.stdout) -> int:
    state = host_command("unpublish")
    if state.get("active") is not False:
        raise GalleryError("The host did not confirm that sharing stopped. Run gallery.py status.")
    print("Stopped the shared game copy.", file=out)
    try:
        endpoint = resolve_gallery_url(args.gallery)
        result = signed_request(endpoint, "DELETE")
        if result.get("published") is not False:
            raise GalleryError("The gallery did not confirm removal.")
    except (GalleryError, EventConfigError) as exc:
        raise GalleryError(f"The public game is stopped, but its gallery card could not be removed. "
                           f"Run this unpublish command again after fixing event access. {exc}") from None
    print("Removed your game from the gallery. Your original project and saved scores are unchanged.", file=out)
    return 0


def status(args: argparse.Namespace, out=sys.stdout) -> int:
    state = host_command("status")
    active = state.get("active") is True
    print("Sharing: " + ("running" if active else "stopped"), file=out)
    if state.get("public_url"):
        print("Play: " + game_url(state["public_url"]), file=out)
    try:
        print("Gallery: " + resolve_gallery_url(args.gallery), file=out)
    except EventConfigError as exc:
        print(str(exc), file=out)
        return 1
    if active:
        print("Use a private browser window to test the Play link without an IDE sign-in.", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    share = subparsers.add_parser("publish", help="share an isolated copy of your game")
    share.add_argument("--gallery", default=None, help="explicit event gallery URL; otherwise discover it from team SSM")
    share.add_argument("--project", type=lambda value: Path(os.path.expanduser(value)), required=True)
    share.add_argument("--port", type=int, choices=(8000, 8001), default=8000)
    share.add_argument("--title", default="", help="game title; otherwise read its actual opening page")
    share.add_argument("--description", default="", help="short optional description for other teams")
    share.add_argument("command", nargs=argparse.REMAINDER, help="relative foreground README command after --")
    for name, help_text in (("status", "show the shared game's status"), ("unpublish", "stop sharing and remove the gallery card")):
        child = subparsers.add_parser(name, help=help_text)
        child.add_argument("--gallery", default=None, help="explicit event gallery URL")
    args = parser.parse_args(argv)
    try:
        return {"publish": publish, "unpublish": unpublish, "status": status}[args.operation](args)
    except (GalleryError, EventConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
