"""One network blip must not cost an attendee their pull request.

`open_pr` makes one gateway call per deliverable FILE, so a build that wrote a dozen
files rolled a dozen dice on a single-attempt transport. Any reset or 503 anywhere in
that loop left the run `passed` with `pr_url` null -- the build succeeded, the work was
composed, and there was nothing for the attendee to re-run.

The other half matters just as much: a 4xx must NOT be retried. A wrong repo, a missing
`Pull requests: write` permission or a protected branch is GitHub giving us an answer,
and retrying it three times only turns a clear failure into a slower, vaguer one.
"""

from __future__ import annotations

import os
import sys
import tempfile
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("WORKSHOP_GITHUB_SETTINGS",
                      os.path.join(tempfile.mkdtemp(), "gh.json"))

import github  # noqa: E402

_CFG = {"gateway_url": "https://gw.example/mcp", "repo": "me/repo",
        "target": "GitHubMCP", "region": "us-west-2",
        "default_branch": "main", "source": "env"}


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int):
    return urllib.error.HTTPError("https://gw.example/mcp", code, "boom", {},
                                  __import__("io").BytesIO(b"detail"))


def _patch(monkeypatch, side_effects):
    """Feed urlopen a list of outcomes; an Exception instance is raised."""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        out = side_effects[min(i, len(side_effects) - 1)]
        if isinstance(out, Exception):
            raise out
        return _Resp(out)

    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(github, "_sigv4_headers", lambda *a, **k: {"X": "signed"})
    monkeypatch.setattr(github.time, "sleep", lambda *_a: None)  # no real backoff
    return calls


_OK = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'


def test_a_transient_reset_is_retried_and_the_call_succeeds(monkeypatch):
    calls = _patch(monkeypatch, [OSError("connection reset"), _OK])
    assert github._gateway_rpc(_CFG, "tools/call", {}) == {"ok": True}
    assert calls["n"] == 2, "did not retry the transient failure"


def test_a_503_is_retried(monkeypatch):
    calls = _patch(monkeypatch, [_http_error(503), _OK])
    assert github._gateway_rpc(_CFG, "tools/call", {}) == {"ok": True}
    assert calls["n"] == 2


def test_a_throttle_is_retried(monkeypatch):
    calls = _patch(monkeypatch, [_http_error(429), _OK])
    assert github._gateway_rpc(_CFG, "tools/call", {}) == {"ok": True}
    assert calls["n"] == 2


def test_a_404_is_NOT_retried(monkeypatch):
    """The App cannot see the repo. That is an answer; retrying hides it."""
    calls = _patch(monkeypatch, [_http_error(404)])
    try:
        github._gateway_rpc(_CFG, "tools/call", {})
    except github.GatewayError as exc:
        assert "404" in str(exc)
    else:
        raise AssertionError("a 404 should raise")
    assert calls["n"] == 1, f"a 404 was retried {calls['n']} times"


def test_a_403_is_NOT_retried(monkeypatch):
    """A missing 'Pull requests: write' permission must fail fast and clearly."""
    calls = _patch(monkeypatch, [_http_error(403)])
    try:
        github._gateway_rpc(_CFG, "tools/call", {})
    except github.GatewayError:
        pass
    assert calls["n"] == 1


def test_a_jsonrpc_error_is_NOT_retried(monkeypatch):
    """The gateway answered; the tool rejected the request."""
    body = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"bad params"}}'
    calls = _patch(monkeypatch, [body])
    try:
        github._gateway_rpc(_CFG, "tools/call", {})
    except github.GatewayError as exc:
        assert "bad params" in str(exc)
    assert calls["n"] == 1


def test_retries_are_bounded_and_the_last_error_is_reported(monkeypatch):
    """It must give up, and say what actually went wrong."""
    calls = _patch(monkeypatch, [OSError("connection reset")])
    try:
        github._gateway_rpc(_CFG, "tools/call", {})
    except github.GatewayError as exc:
        assert "connection reset" in str(exc)
    else:
        raise AssertionError("should raise after exhausting attempts")
    assert calls["n"] == github._RPC_ATTEMPTS


def test_an_unsignable_call_fails_immediately(monkeypatch):
    """Absent credentials are not transient; retrying wastes the attendee's time."""
    n = {"c": 0}

    def boom(*_a, **_k):
        n["c"] += 1
        raise RuntimeError("no credentials")

    monkeypatch.setattr(github, "_sigv4_headers", boom)
    try:
        github._gateway_rpc(_CFG, "tools/call", {})
    except github.GatewayError as exc:
        assert "SigV4" in str(exc)
    assert n["c"] == 1


def test_each_attempt_is_signed_afresh(monkeypatch):
    """A SigV4 signature carries a timestamp; a retry after backoff can otherwise
    fall outside the accepted clock skew and fail for a second, wrong reason."""
    signed = {"n": 0}

    def counting_sign(*_a, **_k):
        signed["n"] += 1
        return {"X": "signed"}

    _patch(monkeypatch, [OSError("reset"), _OK])
    monkeypatch.setattr(github, "_sigv4_headers", counting_sign)
    github._gateway_rpc(_CFG, "tools/call", {})
    assert signed["n"] == 2, "the retry reused the first attempt's signature"


def test_concurrent_calls_never_share_a_request_id(monkeypatch):
    """Read-then-serialize was not atomic, so two threads could stamp one id."""
    import threading  # noqa: PLC0415

    seen: list[int] = []
    lock = threading.Lock()

    def fake_urlopen(req, timeout=None):
        body = req.data.decode()
        with lock:
            seen.append(__import__("json").loads(body)["id"])
        return _Resp(_OK)

    monkeypatch.setattr(github.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(github, "_sigv4_headers", lambda *a, **k: {"X": "s"})
    threads = [threading.Thread(target=github._gateway_rpc,
                                args=(_CFG, "tools/call", {})) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == len(set(seen)), f"duplicate JSON-RPC ids: {sorted(seen)}"
