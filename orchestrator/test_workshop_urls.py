"""The play URL is resolved from the stack, never assembled by hand.

September 25: the most common "my game does not work" report was a hand-typed URL.
`workshop_urls` reads the origin the stack publishes and checks the local port.
"""
import http.server
import os
import socket
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import workshop_urls  # noqa: E402


class _Done:
    def __init__(self, stdout):
        self.stdout = stdout


def test_the_env_override_wins_and_loses_its_trailing_slash(monkeypatch):
    monkeypatch.setenv("WORKSHOP_PUBLIC_BASE_URL", "https://dexample.cloudfront.net/")

    def no_aws(*_a, **_k):
        raise AssertionError("an explicit origin must not call SSM")

    monkeypatch.setattr(subprocess, "run", no_aws)
    assert workshop_urls.public_base_url() == "https://dexample.cloudfront.net"
    assert workshop_urls.proxy_url(8000) == "https://dexample.cloudfront.net/proxy/8000/"


def test_the_origin_comes_from_the_stack_parameter(monkeypatch):
    monkeypatch.delenv("WORKSHOP_PUBLIC_BASE_URL", raising=False)
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        assert kwargs.get("timeout"), "an SSM read must be bounded"
        return _Done("https://d1234abcd.cloudfront.net\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert workshop_urls.proxy_url(8001) == "https://d1234abcd.cloudfront.net/proxy/8001/"
    assert seen and "/workshop/public-base-url" in seen[0]


def test_no_parameter_means_no_invented_url(monkeypatch):
    monkeypatch.delenv("WORKSHOP_PUBLIC_BASE_URL", raising=False)

    def failing(*_a, **_k):
        raise subprocess.CalledProcessError(254, "aws")

    monkeypatch.setattr(subprocess, "run", failing)
    assert workshop_urls.public_base_url() is None
    assert workshop_urls.proxy_url(8000) is None
    hint = " ".join(workshop_urls.play_hint(8000))
    assert "/proxy/8000/" in hint and "public-base-url" in hint

    # A value that is not an https origin ("None", an error string) is not a URL.
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _Done("None\n"))
    assert workshop_urls.public_base_url() is None


def test_the_hint_names_the_browser_and_the_slash():
    lines = workshop_urls.play_hint(8000, base="https://d1234abcd.cloudfront.net")
    assert "https://d1234abcd.cloudfront.net/proxy/8000/" in lines[0]
    assert "signed in" in lines[0] and "trailing slash" in lines[1]


def test_local_answer_sees_a_real_server_and_a_closed_port():
    class Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        answered, detail = workshop_urls.local_answer(port)
        assert answered and detail == "HTTP 404", \
            "any status means a server is up; correctness is the gate's job"
    finally:
        server.shutdown()
        server.server_close()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    answered, detail = workshop_urls.local_answer(closed)
    assert not answered and f"nothing is listening on port {closed}" == detail
