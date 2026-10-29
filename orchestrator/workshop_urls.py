"""The URLs an attendee actually opens, resolved from the stack instead of typed by hand.

At the September 25 delivery the most common "the game does not work" report was
not a broken game. Attendees were told to take the origin of their VS Code tab and
append ``/proxy/8000/``, and they typed a different host, dropped the trailing
slash, opened it in a browser without the code-server login, or opened it before
anything was listening. The box already knows the right answer: the stack publishes
its public origin as the SSM parameter ``/workshop/public-base-url`` (the host cannot
derive it, because it sits behind CloudFront). So print the finished URL, and say
plainly whether anything answers on the port.

Stdlib only and read-only: it reads one SSM parameter and makes one local HTTP
request. It never starts, stops, or changes anything.
"""
from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request

PUBLIC_BASE_PARAM = "/workshop/public-base-url"


def _ssm(name: str, timeout: float) -> str | None:
    try:
        out = subprocess.run(
            ["aws", "ssm", "get-parameter", "--name", name,
             "--query", "Parameter.Value", "--output", "text"],
            capture_output=True, text=True, timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError):
        return None
    value = out.stdout.strip()
    return value if value.startswith("https://") else None


def public_base_url(timeout: float = 10) -> str | None:
    """The https origin the attendee's browser reaches this box at, or None.

    Same order as the GitHub App setup: WORKSHOP_PUBLIC_BASE_URL, then the stack's
    SSM parameter. Never guessed from the instance's own hostname.
    """
    explicit = os.environ.get("WORKSHOP_PUBLIC_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    value = _ssm(PUBLIC_BASE_PARAM, timeout)
    return value.rstrip("/") if value else None


def proxy_url(port: int, base: str | None = None) -> str | None:
    """``<origin>/proxy/<port>/``: code-server's path to a local port, trailing slash included."""
    base = base if base is not None else public_base_url()
    return f"{base.rstrip('/')}/proxy/{int(port)}/" if base else None


def local_answer(port: int, timeout: float = 3) -> tuple[bool, str]:
    """Whether something answers HTTP on 127.0.0.1:<port>, and what it said.

    Any HTTP status counts as answering: a 404 on ``/`` is still a running server,
    and the question here is "is it up", not "is it correct" (that is the gate's job).
    """
    url = f"http://127.0.0.1:{int(port)}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return True, f"HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
            return False, f"nothing is listening on port {port}"
        return False, f"no answer on port {port} ({reason})"


def answer_line(port: int, answered: bool, detail: str) -> str:
    """The last line the command prints: what ``local_answer`` found, in plain words.

    October 29 dry run: Lab 2 runs this command on the line BEFORE the one that starts
    the web server, so "not answering" read like a failure. Nothing listening yet is
    the normal state at that point; say so, and say what starts it. This only words
    the result; the check itself is ``local_answer``, unchanged.
    """
    if answered:
        return f"answering: {detail}"
    if detail == f"nothing is listening on port {int(port)}":
        return (f"not listening yet: nothing is listening on port {int(port)}. That is "
                "expected before the server starts; the next command starts it, and the "
                "address above works once it is running.")
    return f"not answering: {detail}"


def play_hint(port: int, base: str | None = None) -> list[str]:
    """Lines telling the attendee exactly which URL to open for a local port."""
    url = proxy_url(port, base)
    if not url:
        return [f"Open your VS Code tab's origin followed by /proxy/{port}/ "
                "(this host could not read /workshop/public-base-url)."]
    return [f"Open this in the browser where VS Code is signed in: {url}",
            "(the trailing slash matters; another browser shows a login page)"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Print the URL for a local port and whether it answers.")
    parser.add_argument("port", type=int, nargs="?", default=8000)
    args = parser.parse_args()
    answered, detail = local_answer(args.port)
    for line in play_hint(args.port):
        print(line)
    print(answer_line(args.port, answered, detail))
