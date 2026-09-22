"""Gallery client boundaries, using real urllib redirect/signing code without sockets.

The host helper and HTTPS transport are replaced at their external boundaries.
These tests do not execute application commands, invoke sudo, discover real AWS
credentials, or claim to prove the host helper's application isolation.
"""
from __future__ import annotations

import argparse
from email.message import Message
import io
import json
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
GAME = "https://games123.cloudfront.net/"
HTML = b"<!doctype html><html><head><title> Moon &amp;\n Lanterns </title></head></html>"


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
        self.state = {"active": True, "public_url": GAME}
        self.stop_state = {"active": False}
        self.stop_error = None

    def __call__(self, operation, *arguments):
        self.calls.append((operation, arguments))
        if operation == "unpublish":
            if self.stop_error:
                raise self.stop_error
            return self.stop_state
        assert operation in ("publish", "status")
        return self.state


@pytest.fixture
def host(monkeypatch):
    helper = _Host()
    monkeypatch.setattr(gallery, "host_command", helper)
    return helper


def test_public_page_metadata_is_read_within_origin_without_aws_headers(https, monkeypatch):
    monkeypatch.setattr(botocore.session, "Session",
                        lambda: pytest.fail("Public-page inspection must not request AWS credentials"))
    https.respond(GAME, code=302, headers={"Location": "/play/"}, body=b"")
    https.respond(GAME + "play/")
    assert gallery.read_public_game(GAME) == "Moon & Lanterns"
    assert [r["url"] for r in https.requests] == [GAME, GAME + "play/"]
    assert all(r["method"] == "GET" and r["timeout"] == 10 for r in https.requests)
    for request in https.requests:
        assert not any(name in request["headers"] for name in
                       ("authorization", "cookie", "x-amz-security-token", "x-amz-date"))


@pytest.mark.parametrize("location", [
    "https://outside.example/",
    "http://games123.cloudfront.net/play/",
    "https://games123.cloudfront.net.evil.example/",
    "https://user:password@games123.cloudfront.net/",
    "https://games123.cloudfront.net:443/play/",
    "/play/#fragment",
])
def test_public_page_refuses_redirects_outside_its_exact_https_origin(https, location):
    https.respond(GAME, code=302, body=b"", headers={"Location": location})
    with pytest.raises(gallery.GalleryError, match="outside the game origin"):
        gallery.read_public_game(GAME)
    assert len(https.requests) == 1


def test_public_page_redirect_loop_has_a_finite_request_and_timeout_bound(https):
    for _ in range(4):
        https.respond(GAME, code=302, body=b"", headers={"Location": "/"})
    with pytest.raises(gallery.GalleryError, match="redirects repeatedly"):
        gallery.read_public_game(GAME)
    assert len(https.requests) == 4
    assert all(request["timeout"] == 10 for request in https.requests)


@pytest.mark.parametrize("body,headers,reason", [
    (b"x" * (gallery.MAX_RESPONSE_BYTES + 1), {"Content-Type": "text/html"}, "too large"),
    (b'{"status":"ok"}', {"Content-Type": "application/json"}, "browser page"),
])
def test_public_inspection_requires_bounded_html(https, body, headers, reason):
    https.respond(GAME, body=body, headers=headers)
    with pytest.raises(gallery.GalleryError, match=reason):
        gallery.read_public_game(GAME)
    assert https.bodies[0].read_sizes == [gallery.MAX_RESPONSE_BYTES + 1]


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


@pytest.mark.parametrize("title,page,expected", [
    ("", HTML, "Moon & Lanterns"),
    ("  Chosen title  ", HTML, "Chosen title"),
    ("", b"<html><body>A real titleless game.</body></html>", "moon-game"),
])
def test_publish_success_uses_real_page_metadata_and_exact_registered_origin(
        args, host, https, title, page, expected):
    args.title = title
    args.command = ["--", "npm", "run", "start", "--", "argument with spaces"]
    https.respond(GAME, body=page)
    https.json(GALLERY + "games", {"published": True, "url": GAME, "team": "Team 1"})
    output = io.StringIO()
    assert gallery.publish(args, out=output) == 0
    assert host.calls == [(
        "publish", ("--project", str(args.project.resolve()), "--port", "8000",
                    "--", "npm", "run", "start", "--", "argument with spaces"),
    )]
    assert json.loads(https.requests[1]["body"]) == {
        "title": expected, "description": args.description,
    }
    assert "authorization" not in https.requests[0]["headers"]
    assert "authorization" in https.requests[1]["headers"]
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
    https.respond(GAME)
    https.json(GALLERY + "games", result)
    output = io.StringIO()
    with pytest.raises(gallery.GalleryError, match="different game origin|did not confirm"):
        gallery.publish(args, out=output)
    assert [operation for operation, _ in host.calls] == ["publish", "unpublish"]
    assert "Shared Moon" not in output.getvalue()
    assert "Play:" not in output.getvalue()


def test_publish_http_registration_failure_cleans_up_without_a_second_write(args, host, https):
    https.respond(GAME)
    https.json(GALLERY + "games", {"error": "permission denied"}, code=403)
    with pytest.raises(gallery.GalleryError, match="HTTP 403"):
        gallery.publish(args, out=io.StringIO())
    assert [operation for operation, _ in host.calls] == ["publish", "unpublish"]
    assert [r["method"] for r in https.requests] == ["GET", "POST"]


def test_publish_bad_public_page_cleans_up_before_any_registration(args, host, https):
    https.respond(GAME, body=b"service warming up", headers={"Content-Type": "text/plain"})
    with pytest.raises(gallery.GalleryError, match="browser page"):
        gallery.publish(args, out=io.StringIO())
    assert [operation for operation, _ in host.calls] == ["publish", "unpublish"]
    assert len(https.requests) == 1


@pytest.mark.parametrize("phase", ["public-page", "signed-registration"])
def test_publish_malformed_redirect_is_reported_and_stops_the_copy(args, host, https, phase):
    if phase == "signed-registration":
        https.respond(GAME)
    destination = GAME if phase == "public-page" else GALLERY + "games"
    https.respond(destination, code=302, body=b"", headers={"Location": "https://[broken"})
    try:
        with pytest.raises(gallery.GalleryError):
            gallery.publish(args, out=io.StringIO())
    finally:
        assert [operation for operation, _ in host.calls] == ["publish", "unpublish"], \
            "A malformed redirect after starting the copy must not bypass cleanup"
    assert len(https.requests) == (1 if phase == "public-page" else 2)


@pytest.mark.parametrize("phase", ["public-page", "signed-registration"])
def test_publish_transport_timeout_stops_the_copy_without_retrying(args, host, https, phase):
    if phase == "signed-registration":
        https.respond(GAME)
    destination = GAME if phase == "public-page" else GALLERY + "games"
    https.fail(destination, TimeoutError("private-transport-detail"))
    with pytest.raises(gallery.GalleryError) as error:
        gallery.publish(args, out=io.StringIO())
    assert "private-transport-detail" not in str(error.value)
    assert [operation for operation, _ in host.calls] == ["publish", "unpublish"]
    assert len(https.requests) == (1 if phase == "public-page" else 2)


def test_failed_cleanup_is_disclosed_without_replacing_the_registration_failure(args, host, https):
    host.stop_error = gallery.GalleryError("host stop failed")
    https.respond(GAME)
    https.json(GALLERY + "games", {"error": "permission denied"}, code=403)
    output = io.StringIO()
    with pytest.raises(gallery.GalleryError, match="HTTP 403"):
        gallery.publish(args, out=output)
    assert "Could not stop the shared copy" in output.getvalue()
    assert "gallery.py unpublish" in output.getvalue()
    assert "Shared Moon" not in output.getvalue()
    assert [operation for operation, _ in host.calls] == ["publish", "unpublish"]


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
