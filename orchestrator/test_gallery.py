"""Gallery client boundaries, using real urllib redirect/signing code without sockets.

The host helper and HTTPS transport are replaced at their external boundaries.
These tests do not execute application commands, invoke sudo, discover real AWS
credentials, or claim to prove the host helper's application isolation.
"""
from __future__ import annotations

import argparse
from email.message import Message
from html.parser import HTMLParser
import io
import json
import shlex
import socket
import subprocess
from types import SimpleNamespace
import urllib.error
import urllib.request
from urllib.response import addinfourl

from botocore.credentials import Credentials
from botocore.exceptions import CredentialRetrievalError
import botocore.session
import pytest

import gallery
from event_config import EventConfigError

GALLERY = "https://event123.execute-api.eu-west-1.amazonaws.com/live/"
GAME = "https://d123game.cloudfront.net/play/"
APP = GAME + "app/"
HOSTING_MODE = "isolated-path-v1"
HTML = b"<!doctype html><html><head><title> Moon &amp;\n Lanterns </title></head></html>"
WRAPPER_HTML = (
    b'<!doctype html><title>Workshop game wrapper</title>'
    b'<iframe src="/play/app/" sandbox="allow-scripts allow-forms allow-pointer-lock"></iframe>'
)
WRAPPER_HEADERS = {
    "Content-Type": "text/html",
    "Content-Security-Policy": (
        f"default-src 'none'; script-src 'none'; frame-src {APP}; "
        "base-uri 'none'; form-action 'none'"
    ),
}
APP_HEADERS = {
    "Content-Type": "text/html",
    "Access-Control-Allow-Origin": "*",
    "Content-Security-Policy": (
        "sandbox allow-scripts allow-forms allow-pointer-lock; default-src 'none'; "
        f"script-src 'unsafe-inline' {APP}; style-src 'unsafe-inline' {APP}; "
        f"img-src data: {APP}; font-src {APP}; connect-src {APP}; "
        "base-uri 'none'; form-action 'none'; frame-src 'none'; "
        "object-src 'none'; worker-src 'none'"
    ),
}


@pytest.fixture(autouse=True)
def isolated_boundaries(tmp_path, monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "WORKSHOP_GALLERY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    credentials = Credentials("testing-access-id", "testing-secret", "testing-session-token")
    monkeypatch.setattr(
        botocore.session, "Session", lambda: SimpleNamespace(get_credentials=lambda: credentials),
    )
    monkeypatch.setattr(
        socket.socket, "connect", lambda *_a, **_k: pytest.fail("No real network in gallery tests"),
    )
    monkeypatch.setattr(
        gallery.subprocess, "run", lambda *_a, **_k: pytest.fail("No host processes in gallery tests"),
    )


class _Body(io.BytesIO):
    def __init__(self, raw):
        super().__init__(raw)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class _HTTPS(urllib.request.BaseHandler):
    """Replace HTTPS I/O; keep urllib's actual redirect and HTTP-error handlers."""

    handler_order = 100

    def __init__(self):
        self.pending = []
        self.requests = []
        self.bodies = []

    def respond(self, url, *, code=200, body=HTML, headers=None):
        self.pending.append((url, code, body, headers or {"Content-Type": "text/html"}))

    def json(self, url, result, *, code=200):
        self.respond(url, code=code, body=json.dumps(result).encode(),
                     headers={"Content-Type": "application/json"})

    def game(self, *, body=HTML):
        self.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
        self.respond(APP, body=body, headers=APP_HEADERS)

    def fail(self, url, exception):
        self.pending.append((url, None, exception, {}))

    def https_open(self, request):
        self.requests.append({
            "url": request.full_url, "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": request.data, "timeout": request.timeout,
        })
        assert self.pending, "The client made an unexpected additional request"
        url, code, raw, supplied_headers = self.pending.pop(0)
        assert request.full_url == url
        if code is None:
            raise raw
        headers = Message()
        for key, value in supplied_headers.items():
            headers[key] = value
        body = _Body(raw)
        self.bodies.append(body)
        response = addinfourl(body, headers, url, code)
        response.msg = "test response"
        return response


@pytest.fixture
def https(monkeypatch):
    transport = _HTTPS()
    real_build_opener = urllib.request.build_opener
    monkeypatch.setattr(
        urllib.request, "build_opener",
        lambda *handlers: real_build_opener(transport, *handlers),
    )
    yield transport
    assert not transport.pending, "An expected HTTP operation did not occur"


@pytest.fixture
def args(tmp_path):
    project = tmp_path / "moon-game"
    project.mkdir()
    return argparse.Namespace(
        project=project, gallery=GALLERY, port=8000, command=["--", "npm", "start"],
        title="", description="A short rooftop round.",
    )


class _Host:
    def __init__(self):
        self.calls = []
        self.state = {
            "active": True, "public_url": GAME,
            "application_url": APP, "hosting_mode": HOSTING_MODE,
        }
        self.status_state = None
        self.stop_state = {"active": False}
        self.stop_error = None

    def __call__(self, operation, *arguments):
        self.calls.append((operation, arguments))
        if operation == "unpublish":
            if self.stop_error:
                raise self.stop_error
            return self.stop_state
        assert operation in ("publish", "status")
        if operation == "status" and self.status_state is not None:
            return self.status_state
        return self.state


@pytest.fixture
def host(monkeypatch):
    helper = _Host()
    monkeypatch.setattr(gallery, "host_command", helper)
    return helper


def test_public_page_title_comes_from_confined_app_without_aws_headers(https, monkeypatch):
    monkeypatch.setattr(botocore.session, "Session",
                        lambda: pytest.fail("Public-page inspection must not request AWS credentials"))
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, code=302, headers={"Location": "index.html"}, body=b"")
    https.respond(APP + "index.html", headers=APP_HEADERS)
    assert gallery.read_public_game(GAME) == "Moon & Lanterns"
    assert [r["url"] for r in https.requests] == [GAME, APP, APP + "index.html"]
    assert all(r["method"] == "GET" and r["timeout"] == 10 for r in https.requests)
    for request in https.requests:
        assert not any(name in request["headers"] for name in
                       ("authorization", "cookie", "x-amz-security-token", "x-amz-date"))


@pytest.mark.parametrize("location", [
    "https://outside.example/",
    "http://d123game.cloudfront.net/play/app/",
    "https://d123game.cloudfront.net.evil.example/play/app/",
    "https://user:password@d123game.cloudfront.net/play/app/",
    "https://d123game.cloudfront.net:443/play/app/",
    "/play/app/#fragment",
    "/",
    "/console/",
    "/proxy/8000/",
    "/play/",
    "/play/app-neighbor/",
    "/play/app/%2e%2e/console/",
])
def test_public_page_refuses_redirects_outside_exact_app_prefix(https, location):
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, code=302, body=b"", headers={"Location": location})
    with pytest.raises(gallery.GalleryError, match="outside the isolated game path"):
        gallery.read_public_game(GAME)
    assert len(https.requests) == 2


def test_wrapper_redirect_is_rejected_even_when_destination_is_the_app(https):
    https.respond(GAME, code=302, body=b"", headers={"Location": APP})
    with pytest.raises(gallery.GalleryError, match="outside the isolated game path"):
        gallery.read_public_game(GAME)
    assert len(https.requests) == 1


def test_public_page_redirect_loop_has_a_finite_request_and_timeout_bound(https):
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    for _ in range(4):
        https.respond(APP, code=302, body=b"", headers={"Location": APP})
    with pytest.raises(gallery.GalleryError, match="redirects repeatedly"):
        gallery.read_public_game(GAME)
    assert len(https.requests) == 5
    assert all(request["timeout"] == 10 for request in https.requests)


@pytest.mark.parametrize("body,headers,reason", [
    (b"x" * (gallery.MAX_RESPONSE_BYTES + 1), {"Content-Type": "text/html"}, "too large"),
    (b'{"status":"ok"}', {"Content-Type": "application/json"}, "browser page"),
])
def test_public_inspection_requires_bounded_html(https, body, headers, reason):
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, body=body, headers={**APP_HEADERS, **headers})
    with pytest.raises(gallery.GalleryError, match=reason):
        gallery.read_public_game(GAME)
    assert https.bodies[1].read_sizes == [gallery.MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("url", [
    "https://d123game.cloudfront.net/",
    "https://d123game.cloudfront.net/proxy/8000/",
    "https://d123game.cloudfront.net/play",
    APP,
    GAME + "?workspace=/home/ubuntu",
    GAME + "#game",
])
def test_old_root_proxy_and_nonexact_game_urls_are_rejected_before_http(https, url):
    with pytest.raises(gallery.GalleryError):
        gallery.read_public_game(url)
    assert https.requests == []


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", [GALLERY + "another", "https://outside.example/private"])
def test_signed_registration_never_follows_even_a_same_origin_redirect(https, code, target):
    https.respond(GALLERY + "games", code=code, body=b"",
                  headers={"Location": target + "?private=signed-location"})
    with pytest.raises(gallery.GalleryError, match=f"HTTP {code}") as error:
        gallery.signed_request(GALLERY, "POST", {"title": "Moon", "description": ""})
    assert len(https.requests) == 1
    assert https.requests[0]["timeout"] == 15
    assert "authorization" in https.requests[0]["headers"]
    assert target not in str(error.value)
    assert "signed-location" not in str(error.value)
    assert "testing-session-token" not in str(error.value)


@pytest.mark.parametrize("endpoint,method,error_type", [
    ("https://event123.execute-api.eu-west-1.amazonaws.com.evil.example/live/", "POST", EventConfigError),
    (GALLERY + "?secret=do-not-echo", "DELETE", EventConfigError),
    (GALLERY, "GET", gallery.GalleryError),
])
def test_invalid_signed_operation_fails_before_credentials_or_http(
        monkeypatch, https, endpoint, method, error_type):
    monkeypatch.setattr(botocore.session, "Session",
                        lambda: pytest.fail("Reject the operation before credential discovery"))
    with pytest.raises(error_type):
        gallery.signed_request(endpoint, method)
    assert https.requests == []


def test_signed_post_has_exact_endpoint_region_and_only_the_supplied_metadata(https, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    metadata = {"title": "Moon & Lanterns", "description": "A short rooftop round."}
    https.json(GALLERY + "games", {"published": True, "url": GAME, "team": "Team 1"})
    result = gallery.signed_request(GALLERY.rstrip("/"), "POST", metadata)
    request = https.requests[0]
    assert result["url"] == GAME
    assert request["url"] == GALLERY + "games"
    assert request["method"] == "POST"
    assert json.loads(request["body"]) == metadata
    assert "/eu-west-1/execute-api/aws4_request" in request["headers"]["authorization"]
    assert request["headers"]["x-amz-security-token"] == "testing-session-token"
    assert request["timeout"] == 15
    assert https.bodies[0].read_sizes == [gallery.MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("body,reason", [
    (b"x" * (gallery.MAX_RESPONSE_BYTES + 1), "oversized"),
    (b"{not-json", "unreadable"),
    (b"[]", "invalid result"),
])
def test_signed_response_is_bounded_and_must_be_a_json_object(https, body, reason):
    https.respond(GALLERY + "games", body=body, headers={"Content-Type": "application/json"})
    with pytest.raises(gallery.GalleryError, match=reason):
        gallery.signed_request(GALLERY, "DELETE")
    assert len(https.requests) == 1
    assert https.bodies[0].read_sizes == [gallery.MAX_RESPONSE_BYTES + 1]


def test_credential_provider_failure_does_not_print_private_error_details(monkeypatch, https):
    def unavailable():
        raise CredentialRetrievalError(provider="unit-test", error_msg="private-credential-detail")

    monkeypatch.setattr(
        botocore.session, "Session", lambda: SimpleNamespace(get_credentials=unavailable),
    )
    with pytest.raises(gallery.GalleryError) as error:
        gallery.signed_request(GALLERY, "POST", {"title": "Moon"})
    assert "private-credential-detail" not in str(error.value)
    assert "AWS session" in str(error.value)
    assert https.requests == []


@pytest.mark.parametrize("case", ["no-command", "missing-project", "bad-endpoint", "bad-title", "bad-description"])
def test_publish_preflight_failure_makes_no_host_mutation(args, host, https, case):
    if case == "no-command":
        args.command = ["--"]
    elif case == "missing-project":
        args.project = args.project / "absent"
    elif case == "bad-endpoint":
        args.gallery = "https://untrusted.example/"
    elif case == "bad-title":
        args.title = "x" * 101
    else:
        args.description = "invalid\x00description"
    with pytest.raises((gallery.GalleryError, EventConfigError)):
        gallery.publish(args, out=io.StringIO())
    assert host.calls == []
    assert https.requests == []


def test_missing_discovered_configuration_is_rejected_before_host_mutation(args, host, https, monkeypatch):
    args.gallery = None

    def missing(_explicit):
        raise EventConfigError("EVENT_CONFIG_MISSING", "The team's parameter is missing.")

    monkeypatch.setattr(gallery, "resolve_gallery_url", missing)
    with pytest.raises(EventConfigError, match="EVENT_CONFIG_MISSING"):
        gallery.publish(args, out=io.StringIO())
    assert host.calls == []
    assert https.requests == []


@pytest.mark.parametrize("changes", [
    {"hosting_mode": None},
    {"hosting_mode": "isolated-origin-v1"},
    {"application_url": None},
    {"application_url": "https://d123game.cloudfront.net/"},
    {"application_url": "https://d123game.cloudfront.net/proxy/8000/"},
    {"application_url": "https://d456other.cloudfront.net/play/app/"},
    {"public_url": "https://d123game.cloudfront.net/"},
])
def test_host_contract_is_verified_before_publish_mutation(args, host, https, changes):
    host.state.update(changes)
    # Missing fields, not just empty values, are representative of an old helper.
    host.state = {key: value for key, value in host.state.items() if value is not None}
    with pytest.raises(gallery.GalleryError):
        gallery.publish(args, out=io.StringIO())
    assert host.calls == [("status", ())]
    assert https.requests == []


def test_host_contract_is_rechecked_after_publish_and_stopped_if_it_changed(args, host, https):
    host.status_state = dict(host.state)
    host.state.pop("hosting_mode")
    with pytest.raises(gallery.GalleryError):
        gallery.publish(args, out=io.StringIO())
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert https.requests == []


@pytest.mark.parametrize("case", [
    "missing-csp", "report-only", "allow-same-origin",
    "allow-popups", "allow-top-navigation", "missing-scripts",
    "broad-connect", "broad-frame", "form-navigation", "missing-cors", "specific-cors",
    "credentialed-cors", "cookie",
])
def test_unsafe_app_headers_stop_copy_before_central_registration(args, host, https, case):
    headers = dict(APP_HEADERS)
    if case == "missing-csp":
        headers.pop("Content-Security-Policy")
    elif case == "report-only":
        headers["Content-Security-Policy-Report-Only"] = headers.pop("Content-Security-Policy")
    elif case.startswith("allow-"):
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "sandbox allow-scripts", "sandbox " + case + " allow-scripts",
        )
    elif case == "missing-scripts":
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "sandbox allow-scripts", "sandbox",
        )
    elif case == "broad-connect":
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "connect-src " + APP, "connect-src https://d123game.cloudfront.net/",
        )
    elif case == "broad-frame":
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "frame-src 'none'", "frame-src https://d123game.cloudfront.net/",
        )
    elif case == "form-navigation":
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "form-action 'none'", "form-action " + APP,
        )
    elif case == "missing-cors":
        headers.pop("Access-Control-Allow-Origin")
    elif case == "specific-cors":
        headers["Access-Control-Allow-Origin"] = "https://d123game.cloudfront.net"
    elif case == "credentialed-cors":
        headers["Access-Control-Allow-Credentials"] = "true"
    else:
        headers["Set-Cookie"] = "game=not-an-auth-cookie"
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, headers=headers)
    output = io.StringIO()
    with pytest.raises(gallery.GalleryError, match="isolation headers"):
        gallery.publish(args, out=output)
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert [request["method"] for request in https.requests] == ["GET", "GET"]
    assert all("authorization" not in request["headers"] for request in https.requests)
    assert "Shared " not in output.getvalue() and "Play:" not in output.getvalue()


@pytest.mark.parametrize("case", [
    "missing-csp", "script", "script-element-override", "script-attribute-override",
    "broad-frame", "cookie",
])
def test_wrapper_headers_fail_before_loading_app_or_registering(args, host, https, case):
    headers = dict(WRAPPER_HEADERS)
    if case == "missing-csp":
        headers.pop("Content-Security-Policy")
    elif case == "script":
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "script-src 'none'", "script-src 'unsafe-inline'",
        )
    elif case in ("script-element-override", "script-attribute-override"):
        directive = "script-src-elem" if case == "script-element-override" else "script-src-attr"
        headers["Content-Security-Policy"] += f"; {directive} 'unsafe-inline'"
    elif case == "broad-frame":
        headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
            "frame-src " + APP, "frame-src https://d123game.cloudfront.net/",
        )
    else:
        headers["Set-Cookie"] = "wrapper=fixture"
    https.respond(GAME, body=WRAPPER_HTML, headers=headers)
    with pytest.raises(gallery.GalleryError, match="isolation headers"):
        gallery.publish(args, out=io.StringIO())
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert [request["url"] for request in https.requests] == [GAME]


@pytest.mark.parametrize("directive,value", [
    ("script-src", "*"),
    ("script-src-elem", "'self'"),
    ("script-src-attr", "'unsafe-eval'"),
    ("style-src", "https:"),
    ("style-src-elem", "https://d123game.cloudfront.net/"),
    ("img-src", "https://d123game.cloudfront.net/console/"),
    ("font-src", "https://fonts.example/"),
    ("media-src", "https:"),
])
def test_resource_policies_cannot_reach_ide_paths_or_arbitrary_origins(
        args, host, https, directive, value):
    headers = dict(APP_HEADERS)
    parts = [part.strip() for part in headers["Content-Security-Policy"].split(";")]
    parts = [part for part in parts if part.split() and part.split()[0] != directive]
    headers["Content-Security-Policy"] = "; ".join(parts + [f"{directive} {value}"])
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, headers=headers)
    with pytest.raises(gallery.GalleryError, match="isolation headers"):
        gallery.publish(args, out=io.StringIO())
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert all(request["method"] == "GET" for request in https.requests)


@pytest.mark.parametrize("serialized_together", [False, True])
def test_extra_policy_cannot_weaken_one_complete_enforced_boundary(https, serialized_together):
    headers = Message()
    for name, value in APP_HEADERS.items():
        if name != "Content-Security-Policy":
            headers[name] = value
    restrictive = APP_HEADERS["Content-Security-Policy"]
    permissive = "default-src *; sandbox allow-scripts allow-same-origin"
    if serialized_together:
        headers["Content-Security-Policy"] = permissive + ", " + restrictive
    else:
        headers["Content-Security-Policy"] = permissive
        headers["Content-Security-Policy"] = restrictive
    headers["Content-Security-Policy-Report-Only"] = "default-src *"
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, headers=headers)
    assert gallery.read_public_game(GAME) == "Moon & Lanterns"


def test_later_duplicate_safe_directive_cannot_replace_first_unsafe_directive(https):
    headers = dict(APP_HEADERS)
    headers["Content-Security-Policy"] = (
        "connect-src https://d123game.cloudfront.net/; " + headers["Content-Security-Policy"]
    )
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, headers=headers)
    with pytest.raises(gallery.GalleryError, match="isolation headers"):
        gallery.read_public_game(GAME)
    assert len(https.requests) == 2


def test_js_form_save_permissions_and_optional_pointer_lock_are_accepted(https):
    # This verifies policy admission, not DOM submit events or an actual save.
    # The native canary must additionally execute preventDefault() + fetch().
    headers = dict(APP_HEADERS)
    headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
        " allow-pointer-lock", "",
    ).replace("connect-src " + APP, "connect-src " + APP + " " + APP.replace("https:", "wss:", 1))
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, headers=headers)
    assert gallery.read_public_game(GAME) == "Moon & Lanterns"


def test_current_host_generated_policies_and_wrapper_match_client_contract(https):
    import game_host

    def response_headers(wrapper=False):
        headers = {"Content-Type": "text/html"}
        for line in game_host.browser_headers(GAME, wrapper=wrapper).splitlines():
            command, name, value, mode = shlex.split(line)
            assert (command, mode) == ("add_header", "always;")
            headers[name] = value
        return headers

    class Frames(HTMLParser):
        def __init__(self):
            super().__init__()
            self.frames = []

        def handle_starttag(self, tag, attrs):
            if tag == "iframe":
                self.frames.append(dict(attrs))

    wrapper = game_host.render_wrapper()
    parsed = Frames()
    parsed.feed(wrapper)
    assert len(parsed.frames) == 1
    assert parsed.frames[0]["src"] == "app/"
    assert set(parsed.frames[0]["sandbox"].split()) == {
        "allow-scripts", "allow-forms", "allow-pointer-lock",
    }
    https.respond(GAME, body=wrapper.encode(), headers=response_headers(wrapper=True))
    https.respond(APP, headers=response_headers())
    assert gallery.read_public_game(GAME) == "Moon & Lanterns"


@pytest.mark.parametrize("title,page,expected", [
    ("", HTML, "Moon & Lanterns"),
    ("  Chosen title  ", HTML, "Chosen title"),
    ("", b"<html><body>A real titleless game.</body></html>", "moon-game"),
])
def test_publish_success_uses_app_metadata_and_exact_registered_wrapper(
        args, host, https, title, page, expected):
    args.title = title
    args.command = ["--", "npm", "run", "start", "--", "argument with spaces"]
    https.game(body=page)
    https.json(GALLERY + "games", {"published": True, "url": GAME, "team": "Team 1"})
    output = io.StringIO()
    assert gallery.publish(args, out=output) == 0
    assert host.calls == [("status", ()), (
        "publish", ("--project", str(args.project.resolve()), "--port", "8000",
                    "--", "npm", "run", "start", "--", "argument with spaces"),
    )]
    assert json.loads(https.requests[2]["body"]) == {
        "title": expected, "description": args.description,
    }
    assert "authorization" not in https.requests[0]["headers"]
    assert "authorization" not in https.requests[1]["headers"]
    assert "authorization" in https.requests[2]["headers"]
    assert f"Play: {GAME}" in output.getvalue()
    assert f"Gallery: {GALLERY}" in output.getvalue()
    assert f"Shared {expected} for Team 1." in output.getvalue()
    assert "testing-secret" not in output.getvalue()
    assert "testing-session-token" not in output.getvalue()


@pytest.mark.parametrize("result", [
    {"published": False, "url": GAME},
    {"published": 1, "url": GAME},
    {"published": True, "url": "https://another.cloudfront.net/"},
    {"published": True, "url": GAME + "different/"},
    {"published": True},
])
def test_unconfirmed_or_different_registration_stops_the_shared_copy(args, host, https, result):
    https.game()
    https.json(GALLERY + "games", result)
    output = io.StringIO()
    with pytest.raises(gallery.GalleryError, match="different game URL|did not confirm"):
        gallery.publish(args, out=output)
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert "Shared Moon" not in output.getvalue()
    assert "Play:" not in output.getvalue()


def test_publish_http_registration_failure_cleans_up_without_a_second_write(args, host, https):
    https.game()
    https.json(GALLERY + "games", {"error": "permission denied"}, code=403)
    with pytest.raises(gallery.GalleryError, match="HTTP 403"):
        gallery.publish(args, out=io.StringIO())
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert [r["method"] for r in https.requests] == ["GET", "GET", "POST"]


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("body", [b"", b"{}", b'{"message":"Forbidden"}', b'{"error":""}', b"not-json"])
def test_api_gateway_403_without_error_has_actionable_permissions_hint(https, method, body):
    https.respond(GALLERY + "games", code=403, body=body,
                  headers={"Content-Type": "application/json"})
    with pytest.raises(gallery.GalleryError) as error:
        gallery.signed_request(GALLERY, method, {"title": "Moon"})
    message = str(error.value).lower()
    assert "http 403" in message
    assert "facilitator" in message and "team" in message and "central" in message
    assert "permission" in message
    assert len(https.requests) == 1, "A permission failure must not trigger an automatic retry"
    assert https.bodies[0].read_sizes == [4096]


def test_api_gateway_403_publish_stops_copy_and_does_not_advertise_success(args, host, https):
    https.game()
    https.json(GALLERY + "games", {"message": "Forbidden"}, code=403)
    output = io.StringIO()
    with pytest.raises(gallery.GalleryError, match="facilitator"):
        gallery.publish(args, out=output)
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert [r["method"] for r in https.requests] == ["GET", "GET", "POST"]
    assert "Shared Moon" not in output.getvalue() and "Play:" not in output.getvalue()
    assert "Stopped the new shared copy" in output.getvalue()


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_403_with_specific_error_preserves_server_reason_without_generic_hint(https, method):
    reason = "This team is not registered for the event."
    https.json(GALLERY + "games", {"error": reason}, code=403)
    with pytest.raises(gallery.GalleryError) as error:
        gallery.signed_request(GALLERY, method)
    assert str(error.value) == "Gallery registration returned HTTP 403. " + reason
    assert len(https.requests) == 1


@pytest.mark.parametrize("code", [401, 500])
def test_non_403_without_error_does_not_invent_gallery_permission_diagnosis(https, code):
    https.json(GALLERY + "games", {"message": "Request failed"}, code=code)
    with pytest.raises(gallery.GalleryError) as error:
        gallery.signed_request(GALLERY, "POST")
    assert str(error.value) == f"Gallery registration returned HTTP {code}."
    assert len(https.requests) == 1


def test_publish_bad_public_page_cleans_up_before_any_registration(args, host, https):
    https.respond(GAME, body=WRAPPER_HTML, headers=WRAPPER_HEADERS)
    https.respond(APP, body=b"service warming up",
                  headers={**APP_HEADERS, "Content-Type": "text/plain"})
    with pytest.raises(gallery.GalleryError, match="browser page"):
        gallery.publish(args, out=io.StringIO())
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert len(https.requests) == 2


@pytest.mark.parametrize("phase", ["public-page", "signed-registration"])
def test_publish_malformed_redirect_is_reported_and_stops_the_copy(args, host, https, phase):
    if phase == "signed-registration":
        https.game()
    destination = GAME if phase == "public-page" else GALLERY + "games"
    https.respond(destination, code=302, body=b"", headers={"Location": "https://[broken"})
    try:
        with pytest.raises(gallery.GalleryError):
            gallery.publish(args, out=io.StringIO())
    finally:
        assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"], \
            "A malformed redirect after starting the copy must not bypass cleanup"
    assert len(https.requests) == (1 if phase == "public-page" else 3)


@pytest.mark.parametrize("phase", ["public-page", "signed-registration"])
def test_publish_transport_timeout_stops_the_copy_without_retrying(args, host, https, phase):
    if phase == "signed-registration":
        https.game()
    destination = GAME if phase == "public-page" else GALLERY + "games"
    https.fail(destination, TimeoutError("private-transport-detail"))
    with pytest.raises(gallery.GalleryError) as error:
        gallery.publish(args, out=io.StringIO())
    assert "private-transport-detail" not in str(error.value)
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]
    assert len(https.requests) == (1 if phase == "public-page" else 3)


def test_failed_cleanup_is_disclosed_without_replacing_the_registration_failure(args, host, https):
    host.stop_error = gallery.GalleryError("host stop failed")
    https.game()
    https.json(GALLERY + "games", {"error": "permission denied"}, code=403)
    output = io.StringIO()
    with pytest.raises(gallery.GalleryError, match="HTTP 403"):
        gallery.publish(args, out=output)
    assert "Could not stop the shared copy" in output.getvalue()
    assert "gallery.py unpublish" in output.getvalue()
    assert "Shared Moon" not in output.getvalue()
    assert [operation for operation, _ in host.calls] == ["status", "publish", "unpublish"]


def test_unpublish_success_requires_both_host_stop_and_card_removal(args, host, https):
    https.json(GALLERY + "games", {"published": False})
    output = io.StringIO()
    assert gallery.unpublish(args, out=output) == 0
    assert host.calls == [("unpublish", ())]
    request = https.requests[0]
    assert request["method"] == "DELETE"
    assert json.loads(request["body"]) == {}
    assert "Stopped the shared game copy." in output.getvalue()
    assert "Removed your game from the gallery." in output.getvalue()


@pytest.mark.parametrize("failure", ["access-denied", "unconfirmed", "missing-config", "malformed-redirect"])
def test_unpublish_card_failure_honestly_preserves_the_completed_host_stop(
        args, host, https, monkeypatch, failure):
    if failure == "access-denied":
        https.json(GALLERY + "games", {"error": "permission denied"}, code=403)
    elif failure == "unconfirmed":
        https.json(GALLERY + "games", {"published": True})
    elif failure == "malformed-redirect":
        https.respond(GALLERY + "games", code=302, body=b"", headers={"Location": "https://[broken"})
    else:
        def missing(_explicit):
            raise EventConfigError("EVENT_CONFIG_MISSING", "The team's parameter is missing.")
        monkeypatch.setattr(gallery, "resolve_gallery_url", missing)
    output = io.StringIO()
    with pytest.raises(gallery.GalleryError) as error:
        gallery.unpublish(args, out=output)
    assert "public game is stopped" in str(error.value)
    assert "gallery card could not be removed" in str(error.value)
    assert "again after fixing event access" in str(error.value)
    assert host.calls == [("unpublish", ())]
    assert "Stopped the shared game copy." in output.getvalue()
    assert "Removed your game" not in output.getvalue()


def test_unconfirmed_host_stop_does_not_remove_the_card(args, host, https):
    host.stop_state = {"active": True}
    with pytest.raises(gallery.GalleryError, match="did not confirm"):
        gallery.unpublish(args, out=io.StringIO())
    assert https.requests == []


def test_status_only_reads_host_state_and_reports_unavailable_event_config(args, host, monkeypatch, https):
    def missing(_explicit):
        raise EventConfigError("EVENT_CONFIG_MISSING", "The team's parameter is missing.")

    monkeypatch.setattr(gallery, "resolve_gallery_url", missing)
    output = io.StringIO()
    assert gallery.status(args, out=output) == 1
    assert host.calls == [("status", ())]
    assert https.requests == []
    assert "Sharing: running" in output.getvalue()
    assert "EVENT_CONFIG_MISSING" in output.getvalue()


def test_host_helper_receives_argv_without_a_shell_and_with_a_finite_timeout(tmp_path, monkeypatch):
    helper = tmp_path / "workshop-game-host"
    helper.write_text("test placeholder; never executed")
    monkeypatch.setattr(gallery, "HOST_HELPER", str(helper))
    observed = []

    def run(command, **kwargs):
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"active": false}', "")

    monkeypatch.setattr(gallery.subprocess, "run", run)
    assert gallery.host_command("publish", "--project", "/path with spaces", "--", "npm", "start") == \
        {"active": False}
    command, options = observed[0]
    assert command == ["sudo", "-n", str(helper), "publish", "--project", "/path with spaces", "--", "npm", "start"]
    assert 0 < options["timeout"] <= 180
    assert not options.get("shell", False)
    assert "input" not in options


def test_host_helper_timeout_does_not_claim_success_or_repeat_the_operation(tmp_path, monkeypatch):
    helper = tmp_path / "workshop-game-host"
    helper.touch()
    monkeypatch.setattr(gallery, "HOST_HELPER", str(helper))
    calls = []

    def timeout(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(gallery.subprocess, "run", timeout)
    with pytest.raises(gallery.GalleryError, match="status before retrying"):
        gallery.host_command("publish")
    assert len(calls) == 1


def test_cli_invalid_port_fails_before_any_host_operation(args, host, https):
    with pytest.raises(SystemExit) as error:
        gallery.main(["publish", "--project", str(args.project), "--port", "8002", "--", "npm", "start"])
    assert error.value.code == 2
    assert host.calls == []
    assert https.requests == []
