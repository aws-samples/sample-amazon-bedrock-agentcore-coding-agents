"""Exercise the actual manifest receiver/restart path without GitHub or host secrets.

Only GitHub responses and installation discovery are simulated. The callback
server, process entry point, private journal, file publication, and locks are real.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import html
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import socket
import stat
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

SCRIPT = (Path(__file__).resolve().parents[1] / "coding-agents" / "gateway_mcp"
          / "create-github-app.py")
BASE = "https://example.cloudfront.net"
REPO = "example/workshop"
# Synthetic local API response, never a usable key or live App.
CREATED = {"id": 42, "slug": "fixture-app", "pem": "-----BEGIN TEST FIXTURE KEY-----\nlocal-only\n"}


def _load():
    spec = importlib.util.spec_from_file_location("create_github_app", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _isolate(module, root, monkeypatch):
    monkeypatch.setattr(module, "REPO_ROOT", root)
    monkeypatch.setattr(module, "STATE_FILE", root / ".github-app-setup.json")
    monkeypatch.setattr(module, "DEFAULT_KEY_PATH", root / "key.pem")
    monkeypatch.setattr(module, "ENV_FILE", root / "github-app.env")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Unexpected GitHub request in an offline test")

    monkeypatch.setattr(module, "github", forbidden)
    return module


@pytest.fixture()
def module(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("MANIFEST_PORT", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    return _isolate(_load(), tmp_path, monkeypatch)


@pytest.fixture()
def session(module):
    return module.SetupSession.create(BASE, 8765, REPO, module.DEFAULT_KEY_PATH)


@contextmanager
def _receiver(module, session, *, resuming=False):
    handler = type("TestHandler", (module.Handler,), {
        "setup_session": session, "resuming": resuming,
        "result": {}, "done": threading.Event(),
    })
    server = module.CallbackServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}",
                              handler=handler, session=session)
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture()
def server(module, session):
    with _receiver(module, session) as receiver:
        yield receiver


def _get(base: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, response.read().decode()


def _callback(session, code="fixture-one-time-code"):
    return "/callback?" + urlencode({"code": code, "state": session.data["state"]})


def _conversions(module, monkeypatch):
    calls = []

    def convert(method, path, token=None, *, timeout=30):
        # The uncertainty boundary must be durable BEFORE the one-time POST.
        assert json.loads(module.STATE_FILE.read_bytes())["phase"] == "exchanging"
        calls.append((method, path, timeout))
        return CREATED.copy()

    monkeypatch.setattr(module, "github", convert)
    return calls


def _finish_installation(module, monkeypatch):
    calls = []

    def installed(app_id, pem, owner):
        calls.append((app_id, pem, owner))
        return "99"

    monkeypatch.setattr(module, "wait_for_installation", installed)
    return calls


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_manifest_requests_only_workshop_permissions(module):
    manifest = module.build_manifest(BASE, 8765, "Test App")
    assert manifest["default_permissions"] == {
        "contents": "write", "issues": "write", "pull_requests": "write", "metadata": "read",
    }
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["default_events"] == []
    assert manifest["public"] is False
    assert manifest["redirect_url"] == BASE + "/proxy/8765/callback"


@pytest.mark.parametrize("path", ["/", "/proxy/8765/"])
def test_initial_page_posts_saved_manifest_and_state(server, path):
    status, page = _get(server.url, path)
    assert status == 200
    assert 'name="manifest"' in page
    assert f'new?state={server.session.data["state"]}' in page
    assert json.dumps(server.session.data["manifest"]) in html.unescape(page)


@pytest.mark.parametrize("query", [
    {"code": "abc", "state": "wrong-state"},
    {"state": "original"},
    {"code": "abc"},
    {"code": "", "state": "original"},
    {"code": "abc", "state": ""},
    {"code": ["abc", "other"], "state": "original"},
    {"code": "abc", "state": ["original", "wrong"]},
    {"code": "abc", "state": "original", "extra": "not-expected"},
    {"code": "abc", "state": "non-ascii-\N{SNOWMAN}"},
])
def test_invalid_callbacks_never_exchange_or_end_wait(module, server, query):
    query = dict(query)
    if query.get("state") == "original":
        query["state"] = server.session.data["state"]
    before = module.STATE_FILE.read_bytes()
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(server.url, "/callback?" + urlencode(query, doseq=True))
    assert error.value.code == 400
    assert not server.handler.done.is_set()
    assert module.STATE_FILE.read_bytes() == before


def test_callback_path_is_bound_to_saved_proxy_port(server):
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(server.url, "/proxy/9999" + _callback(server.session))
    assert error.value.code == 404
    assert not server.handler.done.is_set()


def test_result_and_private_key_are_durable_before_browser_ack(module, server, monkeypatch):
    calls = _conversions(module, monkeypatch)
    send = server.handler._send
    acknowledged = []

    def inspect_ack(handler, status, payload):
        if status == 200:
            journal = json.loads(module.STATE_FILE.read_bytes())
            assert journal["phase"] == "converted"
            assert journal["app"] == {"id": "42", "slug": CREATED["slug"]}
            assert module.DEFAULT_KEY_PATH.read_text() == CREATED["pem"]
            assert not server.handler.done.is_set()
            assert server.handler.result["id"] == "42"
            acknowledged.append(True)
        return send(handler, status, payload)

    monkeypatch.setattr(server.handler, "_send", inspect_ack)
    status, page = _get(server.url, _callback(server.session))
    assert status == 200 and "App created" in page and CREATED["pem"] not in page
    assert server.handler.done.wait(2)
    assert acknowledged == [True]
    assert len(calls) == 1


def test_duplicate_callback_reuses_result_and_different_code_is_rejected(module, server, monkeypatch):
    calls = _conversions(module, monkeypatch)
    callback = _callback(server.session)
    assert _get(server.url, callback)[0] == 200
    before = module.STATE_FILE.read_bytes()
    assert _get(server.url, callback)[0] == 200
    assert len(calls) == 1 and module.STATE_FILE.read_bytes() == before
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(server.url, _callback(server.session, "another-code"))
    assert error.value.code == 400 and len(calls) == 1
    assert module.STATE_FILE.read_bytes() == before


def test_concurrent_callback_cannot_start_a_second_exchange(module, server, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def convert(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return CREATED.copy()

    monkeypatch.setattr(module, "github", convert)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(_get, server.url, _callback(server.session))
        try:
            assert entered.wait(2)
            with pytest.raises(urllib.error.HTTPError) as error:
                _get(server.url, _callback(server.session))
            assert error.value.code == 409
            assert not server.handler.done.is_set() and len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=3)[0] == 200


def test_disconnect_after_conversion_still_releases_main_and_survives_restart(module, session, monkeypatch):
    calls = _conversions(module, monkeypatch)
    with _receiver(module, session) as server:
        def disconnect(_self, status, _payload):
            assert status == 200
            raise BrokenPipeError("fixture browser closed")

        monkeypatch.setattr(server.handler, "_send", disconnect)
        with pytest.raises(http.client.RemoteDisconnected):
            _get(server.url, _callback(session))
        assert server.handler.done.wait(2)
        assert server.handler.result["id"] == "42"
    restarted = _isolate(_load(), module.REPO_ROOT, monkeypatch)
    installation = _finish_installation(restarted, monkeypatch)
    assert restarted.main(["--resume", "--repo", REPO]) == 0
    assert installation == [("42", CREATED["pem"], "example")]
    assert len(calls) == 1
    saved = json.loads(module.STATE_FILE.read_bytes())
    assert saved["phase"] == "complete" and "pem" not in saved and "state" not in saved


def test_resume_recovers_key_when_process_stopped_between_journal_and_key(module, session, monkeypatch):
    calls = _conversions(module, monkeypatch)
    write = module.atomic_private_write

    def interrupted(path, data, **kwargs):
        if path == module.DEFAULT_KEY_PATH:
            raise module.SetupError("fixture interruption")
        return write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_private_write", interrupted)
        with pytest.raises(module.SetupError, match="interruption"):
            session.receive("fixture-code", session.data["state"])
    assert json.loads(module.STATE_FILE.read_bytes())["phase"] == "converted"
    assert not module.DEFAULT_KEY_PATH.exists()
    _finish_installation(module, monkeypatch)
    assert module.main(["--resume"]) == 0
    assert module.DEFAULT_KEY_PATH.read_text() == CREATED["pem"]
    assert len(calls) == 1


def test_real_main_timeout_then_new_process_resume_preserves_original_setup(module, monkeypatch, capsys):
    port = _free_port()
    monkeypatch.setattr(module, "CALLBACK_WAIT_S", 0.05)
    original_print = module.print
    published = []

    def inspect_url(*args, **kwargs):
        if any(f"{BASE}/proxy/" in str(arg) for arg in args):
            # This is before a user can possibly see/submit the manifest URL.
            published.append(json.loads(module.STATE_FILE.read_bytes()))
            assert stat.S_IMODE(module.STATE_FILE.stat().st_mode) == 0o600
        return original_print(*args, **kwargs)

    monkeypatch.setattr(module, "print", inspect_url)
    with pytest.raises(SystemExit) as stopped:
        module.main(["--repo", REPO, "--port", str(port)])
    assert stopped.value.code == 1 and len(published) == 1
    original = module.STATE_FILE.read_bytes()
    checkpoint = json.loads(original)
    assert checkpoint["phase"] == "awaiting_callback"
    assert "--resume" in capsys.readouterr().err

    restarted = _isolate(_load(), module.REPO_ROOT, monkeypatch)
    monkeypatch.setattr(restarted, "CALLBACK_WAIT_S", 2)
    conversions = _conversions(restarted, monkeypatch)
    _finish_installation(restarted, monkeypatch)
    listening = threading.Event()
    real_server = restarted.CallbackServer

    def capture_server(*args, **kwargs):
        server = real_server(*args, **kwargs)
        listening.set()
        return server

    monkeypatch.setattr(restarted, "CallbackServer", capture_server)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(restarted.main, ["--resume", "--repo", REPO])
        assert listening.wait(1)
        base = f"http://127.0.0.1:{port}"
        status, page = _get(base, "/")
        assert status == 200 and "<form" not in page and "settings/apps/new" not in page
        assert module.STATE_FILE.read_bytes() == original
        assert _get(base, "/callback?" + urlencode(
            {"code": "original-callback", "state": checkpoint["state"]}))[0] == 200
        assert result.result(timeout=3) == 0
    assert len(conversions) == 1
    completed = json.loads(module.STATE_FILE.read_bytes())
    for key in ("name", "repo", "base_url", "port", "created_at", "expires_at", "manifest_sha256"):
        assert completed[key] == checkpoint[key]


def test_normal_rerun_cannot_replace_pending_setup(module, session, monkeypatch):
    before = module.STATE_FILE.read_bytes()
    monkeypatch.setattr(module.secrets, "token_hex", lambda *_: pytest.fail("New App name generated"))
    with pytest.raises(SystemExit):
        module.main(["--repo", REPO])
    assert module.STATE_FILE.read_bytes() == before


@pytest.mark.parametrize("override", [
    ["--base-url", "https://different.cloudfront.net"],
    ["--repo", "another/repository"],
    ["--port", "9999"],
    ["--key-file", "/tmp/different-fixture-key.pem"],
])
def test_resume_refuses_changed_host_repo_port_or_key(module, session, override, capsys):
    before = module.STATE_FILE.read_bytes()
    with pytest.raises(SystemExit):
        module.main(["--resume", *override])
    assert "different host, repository, port, or key" in capsys.readouterr().err
    assert module.STATE_FILE.read_bytes() == before


def test_resume_refuses_checkpoint_copied_to_another_checkout(module, session, tmp_path, monkeypatch):
    before = module.STATE_FILE.read_bytes()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path / "other-checkout")
    with pytest.raises(module.SetupError, match="foreign"):
        module.SetupSession.load()
    assert module.STATE_FILE.read_bytes() == before


def test_expired_callback_cannot_exchange_and_resume_cannot_extend_expiry(module, session, monkeypatch):
    session.data["created_at"] -= 3601
    session.data["expires_at"] -= 3601
    session.save()
    before = module.STATE_FILE.read_bytes()
    with _receiver(module, session) as server:
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(server.url, _callback(session))
        assert error.value.code == 400 and not server.handler.done.is_set()
    with pytest.raises(SystemExit):
        module.main(["--resume"])
    assert module.STATE_FILE.read_bytes() == before


@pytest.mark.parametrize("failure", ["http", "timeout", "connection", "invalid-response"])
def test_ambiguous_conversion_never_retries_or_exposes_callback(module, session, monkeypatch, failure, capsys):
    calls = []
    secret = "fixture-one-time-code-not-for-errors"

    def failed_exchange(*_args, **_kwargs):
        calls.append(True)
        if failure == "http":
            raise urllib.error.HTTPError(
                "https://api.github.com/app-manifests/" + secret + "/conversions",
                422, secret, {}, io.BytesIO(secret.encode()))
        if failure == "timeout":
            raise TimeoutError(secret)
        if failure == "connection":
            raise urllib.error.URLError(secret)
        return {"id": 42, "slug": "fixture-app"}  # The code might already be consumed.

    monkeypatch.setattr(module, "github", failed_exchange)
    with _receiver(module, session) as server:
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(server.url, _callback(session, secret))
        assert error.value.code == 502
        assert secret not in error.value.read().decode()
        assert server.handler.done.wait(2)
    before = module.STATE_FILE.read_bytes()
    assert json.loads(before)["phase"] == "exchange_failed"
    assert secret.encode() not in before
    with pytest.raises(SystemExit):
        module.main(["--resume"])
    assert "will not be retried" in capsys.readouterr().err
    assert len(calls) == 1 and module.STATE_FILE.read_bytes() == before


def test_crash_after_beginning_exchange_is_not_treated_as_unused_code(module, session, monkeypatch):
    class ProcessStopped(BaseException):
        pass

    def interrupted(*_args, **_kwargs):
        raise ProcessStopped()

    monkeypatch.setattr(module, "github", interrupted)
    with pytest.raises(ProcessStopped):
        session.receive("fixture-code", session.data["state"])
    assert json.loads(module.STATE_FILE.read_bytes())["phase"] == "exchanging"
    with pytest.raises(SystemExit):
        module.main(["--resume"])


def test_wait_deadline_fences_a_late_exchange_response(module, monkeypatch):
    monkeypatch.setattr(module, "CALLBACK_WAIT_S", 0.2)
    port = _free_port()
    listening, entered, release = threading.Event(), threading.Event(), threading.Event()
    real_server = module.CallbackServer
    calls = []

    def capture_server(*args, **kwargs):
        server = real_server(*args, **kwargs)
        listening.set()
        return server

    def slow_exchange(*_args, **kwargs):
        calls.append(kwargs["timeout"])
        entered.set()
        assert release.wait(5)
        return CREATED.copy()

    monkeypatch.setattr(module, "CallbackServer", capture_server)
    monkeypatch.setattr(module, "github", slow_exchange)
    with ThreadPoolExecutor(max_workers=2) as executor:
        main = executor.submit(module.main, ["--repo", REPO, "--port", str(port)])
        assert listening.wait(1)
        pending = module.SetupSession.load()
        request = executor.submit(_get, f"http://127.0.0.1:{port}", _callback(pending))
        try:
            assert entered.wait(1)
            with pytest.raises(SystemExit):
                main.result(timeout=1)
            before = module.STATE_FILE.read_bytes()
            assert json.loads(before)["phase"] == "exchanging"
            with pytest.raises(SystemExit):
                module.main(["--resume"])
        finally:
            release.set()
        with pytest.raises(urllib.error.HTTPError):
            request.result(timeout=2)
    assert len(calls) == 1 and 0 < calls[0] <= 0.2
    assert module.STATE_FILE.read_bytes() == before


def test_no_post_starts_if_checkpoint_write_exhausted_wait(module, session, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(
        time=time.time, monotonic=lambda: clock[0]))
    session.deadline = 1
    write = module.atomic_private_write

    def slow_write(path, data, **kwargs):
        write(path, data, **kwargs)
        clock[0] = 2

    monkeypatch.setattr(module, "atomic_private_write", slow_write)
    with pytest.raises(module.CallbackError, match="before conversion"):
        session.receive("fixture-code", session.data["state"])
    restored = module.SetupSession.load()
    assert restored.data["phase"] == "awaiting_callback"
    assert "code_sha256" not in restored.data


def test_github_wait_is_bounded_even_if_socket_operation_does_not_return(monkeypatch):
    # Exercise the real API adapter with only its network seam replaced.
    module = _load()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def stuck_urlopen(_request, *, timeout):
        calls.append(timeout)
        started.set()
        try:
            assert release.wait(3)
            raise TimeoutError("fixture socket operation")
        finally:
            finished.set()

    monkeypatch.setattr(module.urllib.request, "urlopen", stuck_urlopen)
    began = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="outcome is uncertain"):
            module.github("POST", "/app-manifests/fixture-code/conversions", timeout=0.05)
        assert started.is_set() and time.monotonic() - began < 1
    finally:
        release.set()
        assert finished.wait(2)
    assert calls == [0.05]


def test_no_conversion_if_uncertainty_checkpoint_cannot_be_published(module, session, monkeypatch):
    before = module.STATE_FILE.read_bytes()

    def disk_failure(*_args, **_kwargs):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(module.os, "replace", disk_failure)
    with pytest.raises(module.SetupError, match="durably"):
        session.receive("fixture-code", session.data["state"])
    assert module.STATE_FILE.read_bytes() == before
    assert list(module.REPO_ROOT.glob(".github-app-setup-*.tmp")) == []


def test_lost_successful_response_during_disk_publication_stays_ambiguous(module, session, monkeypatch):
    calls = _conversions(module, monkeypatch)
    write = module.atomic_private_write

    def disk_failure(path, data, **kwargs):
        if path == module.STATE_FILE and json.loads(data)["phase"] == "converted":
            raise module.SetupError("fixture disk failure after exchange")
        return write(path, data, **kwargs)

    monkeypatch.setattr(module, "atomic_private_write", disk_failure)
    with _receiver(module, session) as server:
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(server.url, _callback(session))
        assert error.value.code == 500
        assert "App created" not in error.value.read().decode()
        assert server.handler.done.wait(2)
        # There may be another browser request before main shuts the receiver.
        # An unsaved in-memory response must not turn that request into a success.
        with pytest.raises(urllib.error.HTTPError) as duplicate:
            _get(server.url, _callback(session))
        assert duplicate.value.code == 409
    assert json.loads(module.STATE_FILE.read_bytes())["phase"] == "exchanging"
    assert not module.DEFAULT_KEY_PATH.exists()
    with pytest.raises(SystemExit):
        module.main(["--resume"])
    assert len(calls) == 1


def test_private_files_are_0600_from_creation_not_after_writing(module, monkeypatch):
    real_mkstemp = module.tempfile.mkstemp
    initial_modes = []

    def inspect_creation(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        initial_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd, name

    monkeypatch.setattr(module.tempfile, "mkstemp", inspect_creation)
    previous_umask = os.umask(0o022)
    try:
        session = module.SetupSession.create(BASE, 8765, REPO, module.DEFAULT_KEY_PATH)
        _conversions(module, monkeypatch)
        session.receive("fixture-code", session.data["state"])
        module.write_env("42", module.DEFAULT_KEY_PATH, "99")
    finally:
        os.umask(previous_umask)
    assert initial_modes and set(initial_modes) == {0o600}
    for path in (module.STATE_FILE, module.DEFAULT_KEY_PATH, module.ENV_FILE):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(module.REPO_ROOT.glob(".github-app-setup-*.tmp")) == []


@pytest.mark.parametrize("unsafe", ["symlink", "shared"])
def test_unsafe_state_file_is_rejected_without_following_or_rewriting(module, session, unsafe, tmp_path):
    original = module.STATE_FILE.read_bytes()
    if unsafe == "symlink":
        target = tmp_path / "other-private-state"
        module.STATE_FILE.rename(target)
        module.STATE_FILE.symlink_to(target)
    else:
        module.STATE_FILE.chmod(0o644)
    with pytest.raises(module.SetupError):
        module.SetupSession.load()
    assert module.STATE_FILE.read_bytes() == original


def test_saved_conversion_cannot_overwrite_an_existing_different_key(module, session, monkeypatch):
    module.atomic_private_write(module.DEFAULT_KEY_PATH, b"existing unrelated fixture")
    _conversions(module, monkeypatch)
    with pytest.raises(module.SetupError, match="different key"):
        session.receive("fixture-code", session.data["state"])
    assert module.DEFAULT_KEY_PATH.read_bytes() == b"existing unrelated fixture"
    assert json.loads(module.STATE_FILE.read_bytes())["phase"] == "converted"


def test_saved_key_cannot_be_published_through_a_symlink(module, session, monkeypatch, tmp_path):
    target = tmp_path / "unrelated-fixture-key"
    module.atomic_private_write(target, b"original unrelated fixture")
    module.DEFAULT_KEY_PATH.symlink_to(target)
    _conversions(module, monkeypatch)
    with pytest.raises(module.SetupError):
        session.receive("fixture-code", session.data["state"])
    assert module.DEFAULT_KEY_PATH.is_symlink()
    assert target.read_bytes() == b"original unrelated fixture"
    assert json.loads(module.STATE_FILE.read_bytes())["phase"] == "converted"


def test_overlapping_setup_process_is_refused(module, session, capsys):
    before = module.STATE_FILE.read_bytes()
    with module.setup_lock():
        with pytest.raises(SystemExit):
            module.main(["--resume"])
    assert "Another App setup" in capsys.readouterr().err
    assert module.STATE_FILE.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"version": True},
    {"phase": "fresh-again"},
    {"state": "too-short"},
    {"expires_at": float("inf")},
    {"port": True},
    {"repo": ""},
    {"key_file": "relative/key.pem"},
    {"manifest_sha256": "changed"},
])
def test_malformed_checkpoint_fails_closed(module, session, change):
    session.data.update(change)
    session.save()
    with pytest.raises(module.SetupError):
        module.SetupSession.load()


def test_installation_timeout_keeps_conversion_and_resume_does_not_reconvert(module, session, monkeypatch):
    calls = _conversions(module, monkeypatch)
    session.receive("fixture-code", session.data["state"])
    monkeypatch.setattr(module, "INSTALL_WAIT_S", 0)
    with pytest.raises(SystemExit):
        module.main(["--resume"])
    assert json.loads(module.STATE_FILE.read_bytes())["phase"] == "converted"
    _finish_installation(module, monkeypatch)
    assert module.main(["--resume"]) == 0
    assert len(calls) == 1


def test_installation_response_after_deadline_is_not_accepted(module, monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module, "app_jwt", lambda *_: "fixture-local-token")

    def delayed_installation(*_args, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] = module.INSTALL_WAIT_S + 1
        return [{"id": 99, "account": {"login": "example"}}]

    monkeypatch.setattr(module, "github", delayed_installation)
    with pytest.raises(SystemExit):
        module.wait_for_installation("42", CREATED["pem"], "example")
    assert calls == [30]


def test_app_id_mode_does_not_create_manifest_or_rewrite_key(module, monkeypatch):
    module.atomic_private_write(module.DEFAULT_KEY_PATH, CREATED["pem"].encode())
    key_stat = module.DEFAULT_KEY_PATH.stat()
    installed = _finish_installation(module, monkeypatch)
    assert module.main(["--app-id", "42", "--repo", REPO]) == 0
    assert installed == [("42", CREATED["pem"], "example")]
    assert not module.STATE_FILE.exists()
    assert module.DEFAULT_KEY_PATH.stat().st_ino == key_stat.st_ino


def test_new_setup_requires_an_expected_repository(module):
    with pytest.raises(SystemExit):
        module.main([])
    assert not module.STATE_FILE.exists()


def test_env_preserves_literal_key_path_with_shell_metacharacters(module):
    key_path = module.REPO_ROOT / "fixture $HOME `literal`'s key.pem"
    module.write_env("42", key_path, "99")
    lines = module.ENV_FILE.read_text().splitlines()
    assert "export GITHUB_APP_ID=42" in lines
    assert "export GITHUB_APP_INSTALLATION_ID=99" in lines
    assert shlex.split(lines[2]) == ["export", f"GITHUB_APP_PRIVATE_KEY_FILE={key_path}"]
    assert stat.S_IMODE(module.ENV_FILE.stat().st_mode) == 0o600
