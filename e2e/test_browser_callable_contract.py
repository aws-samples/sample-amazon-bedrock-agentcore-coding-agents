"""Regressions from a live traverse: a browser page could not reach its own backend.

On the workshop box, viewed through the VS Code / CloudFront proxy, the UI one agent
built could not talk to the service another agent built. Three stacked breaks caused
it, and each of these tests pins one shut. They survive the agentic-only redesign
because they are BROWSER facts, not assumptions about what the agents build:

  1. The page baked one URL as its only endpoint. Any baked URL is dead the moment the
     page moves (into a repository, onto a reviewer's laptop, behind a proxy).
  2. `localhost` / `127.0.0.1` in a browser means the machine running the BROWSER.
     Behind the workshop proxy that is the attendee's laptop, so a loopback URL in the
     page reaches nothing on the box.
  3. Cross-origin JSON POSTs are preflighted. A service that only handles GET and POST
     looks fine from curl and is unreachable from a page.

What is asserted here is the CONTRACT (resolve at runtime, answer the preflight),
never the content of a deliverable: nothing in this repository knows what the agents
are supposed to produce. The page under test is a hand-written fixture under
`e2e/fixtures/`, and the service under test is a hand-written fixture, not
anything the workshop asks an agent to build.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_FIXTURE_PAGE = os.path.join(_HERE, "fixtures", "deliverable", "ui", "index.html")
# A hand-written fixture service, not a sample: see e2e/fixtures/cors_service.py.
_CORS_SERVICE = os.path.join(_HERE, "fixtures", "cors_service.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError as exc:
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"{url} never became ready: {last}")


# ------------------------------------------------------------------ breaks 1 and 2
def test_page_resolves_its_endpoint_and_never_hardcodes_one():
    """A page must resolve its backend address at runtime: explicit override, then an
    injected global, then its own origin. A literal URL inside fetch() is the bug."""
    html = open(_FIXTURE_PAGE, encoding="utf-8").read()
    assert "URLSearchParams" in html, "no explicit ?endpoint= override"
    assert "window.MCP_ENDPOINT" in html, "no injected-global hook for a host page"
    assert "location.origin" in html, (
        "no same-origin default: the page breaks wherever it is served from")

    targets = re.findall(r"fetch\(\s*([^,\)]+)", html)
    assert targets, "the page performs no fetch at all"
    for target in targets:
        assert "http://" not in target and "https://" not in target, (
            f"fetch() targets a hardcoded URL ({target.strip()}); a browser resolves "
            "loopback to the VIEWER's machine, so this reaches nothing behind a proxy")


# ---------------------------------------------------------------------- break 3
def test_a_service_must_answer_the_cors_preflight_to_be_page_callable():
    """A JSON POST from a page on another origin is PREFLIGHTED: the browser sends
    OPTIONS first and blocks the real call unless the service allows it, and repeats
    the allowance on the real response. A service handling only GET and POST looks
    fine from curl and is unreachable from a page, which is the bug a live traverse
    hit. Pinned against a real process, because this is a wire fact."""
    port = _free_port()
    proc = subprocess.Popen([sys.executable, _CORS_SERVICE, "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_http(base)

        req = urllib.request.Request(
            base, method="OPTIONS",
            headers={"Origin": "http://example.com",
                     "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status in (200, 204), f"preflight returned {resp.status}"
            assert resp.headers.get("Access-Control-Allow-Origin"), (
                "preflight carries no Access-Control-Allow-Origin, so the browser "
                "blocks the page's call")
            allowed = (resp.headers.get("Access-Control-Allow-Headers") or "").lower()
            assert "content-type" in allowed, (
                "Content-Type is not allowed, so a JSON POST from a page is blocked")

        # the allowance must be on the REAL response too, not only the preflight
        req = urllib.request.Request(
            base,
            data=json.dumps({"hello": "world"}).encode(),
            headers={"Content-Type": "application/json", "Origin": "http://example.com"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin"), (
                "the POST response carries no allow-origin, so the browser discards it")
            body = json.loads(resp.read())
        assert body["echo"], "the service did not answer the POST"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
