"""Keep the deployed coordinator's own session alive while a build is in flight.

WHY THIS EXISTS, measured. A build is fire-and-forget by design: `agentcore invoke`
returns in about eight seconds and the engine keeps working in a background thread inside
the coordinator's microVM. AgentCore reclaims a runtime SESSION that receives no request
for roughly fifteen minutes, and the reclaim takes that thread with it. On a live event
account (397691659327, 2026-09-03) two game builds died 15m06s and 15m08s after their last
inbound request, both mid-validation, with no coordinator log line after the freeze:

    run_180637_d74eda9fc539  last request 18:09:41Z  ->  snapshot frozen 18:24:47Z
    run_183649_8361d5d9bc3c  last request 18:36:49Z  ->  snapshot frozen 18:51:57Z

That is fatal for the workshop as written, not a curiosity. The page tells the attendee to
follow the build with `watch_run.py`, which reads persisted state and never invokes, and to
go do Lab 3 while it runs. So nothing pings the session, and any build longer than about
fifteen minutes dies partway through. A game build takes twenty to forty-five minutes.

WHAT IT DOES. While at least one run is non-terminal, the coordinator invokes its OWN
runtime and session with a sentinel prompt that the entrypoint answers WITHOUT a model
turn, so a ping costs no tokens and does no work. That is enough inbound traffic to keep
the microVM the build is running in.

WHAT IT DELIBERATELY DOES NOT DO. It never pings when nothing is in flight, so an idle
coordinator is still reclaimed (which is correct, and free). It stops after
`WORKSHOP_KEEPALIVE_MAX_S`, so a run wedged non-terminal cannot hold a microVM forever;
past that the platform reclaims as it does today and the watcher reports
COORDINATOR_SESSION_INTERRUPTED, which is already honest. A failed ping is swallowed: the
worst outcome of a broken keepalive must be the reclaim that happens without it.

This is platform glue, NOT the verdict path. It submits nothing, decides nothing, reads no
gate, and writes no run state.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from typing import Callable

# The prompt the entrypoint short-circuits. Deliberately not natural language: it must be
# impossible to confuse with something a person would type, because answering it skips the
# model entirely.
KEEPALIVE_PROMPT = "__workshop_session_keepalive__"

# Ping interval. The observed reclaim is ~15 minutes of silence, so 4 minutes leaves a
# margin of three-plus missed pings before a build is at risk.
INTERVAL_S = float(os.environ.get("WORKSHOP_KEEPALIVE_S", "240"))
# Total time a single in-flight window may be kept warm.
MAX_S = float(os.environ.get("WORKSHOP_KEEPALIVE_MAX_S", "5400"))
# How often the loop wakes to look at the world. Cheap: it only reads a counter.
TICK_S = float(os.environ.get("WORKSHOP_KEEPALIVE_TICK_S", "15"))


def self_runtime_arn(environ: dict[str, str] | None = None) -> str | None:
    """This runtime's own ARN, read from the environment AgentCore already sets.

    `AGENTCORE_RUNTIME_URL` carries the ARN percent-encoded in its path, so there is
    nothing to configure and nothing to keep in sync with the deploy:

        https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<urlencoded-arn>/invocations
    """
    env = os.environ if environ is None else environ
    url = env.get("AGENTCORE_RUNTIME_URL") or ""
    marker = "/runtimes/"
    if marker not in url:
        return None
    tail = url.split(marker, 1)[1]
    encoded = tail.split("/invocations", 1)[0]
    arn = urllib.parse.unquote(encoded)
    return arn if arn.startswith("arn:") else None


def region_of(arn: str) -> str:
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 else ""


def keepalive_loop(
    in_flight: Callable[[], int],
    ping: Callable[[], None],
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    interval_s: float = INTERVAL_S,
    max_s: float = MAX_S,
    tick_s: float = TICK_S,
    stop: Callable[[], bool] = lambda: False,
    on_event: Callable[[str], None] | None = None,
) -> None:
    """Ping while work is in flight, never otherwise, and never past the cap.

    Injected clock and callables so the behaviour is testable without sleeping and
    without AWS: this loop is the whole contract.
    """
    window_start: float | None = None
    last_ping: float | None = None
    exhausted = False
    while not stop():
        if in_flight() <= 0:
            # Nothing to protect. Forget the window so the next build starts fresh, and
            # let the platform reclaim an idle coordinator as it should.
            window_start = last_ping = None
            exhausted = False
            sleep(tick_s)
            continue
        moment = now()
        if window_start is None:
            window_start = moment
        if not exhausted and moment - window_start > max_s:
            exhausted = True
            if on_event:
                on_event(f"keepalive cap reached after {max_s:.0f}s with work still in "
                         "flight; letting the platform reclaim this session")
        if not exhausted and (last_ping is None or moment - last_ping >= interval_s):
            try:
                ping()
                if on_event:
                    on_event("keepalive ping sent")
            except Exception as exc:  # noqa: BLE001 - a ping may never break a build
                if on_event:
                    on_event(f"keepalive ping failed ({exc}); the build continues")
            last_ping = now()
        sleep(tick_s)


_thread: threading.Thread | None = None
_lock = threading.Lock()


def _default_ping(arn: str, session_id: str) -> Callable[[], None]:
    def ping() -> None:
        import boto3  # imported lazily so this module stays importable in tests

        client = boto3.client("bedrock-agentcore", region_name=region_of(arn))
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": KEEPALIVE_PROMPT}).encode("utf-8"),
        )
        body = resp.get("response")
        if body is not None:  # drain so the request completes, then let it go
            try:
                body.read()
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()

    return ping


def ensure_started(session_id: str, in_flight: Callable[[], int],
                   log: Callable[[str], None] | None = None) -> bool:
    """Start the keepalive thread once, for the session this runtime is serving.

    Returns True when a thread is running after the call. A no-op (False) when this is
    not a deployed AgentCore Runtime, because then there is no session to reclaim: the
    console hosts the same engine in an ordinary process on the box.
    """
    global _thread
    arn = self_runtime_arn()
    if not arn or not session_id:
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        ping = _default_ping(arn, session_id)
        _thread = threading.Thread(
            target=keepalive_loop,
            args=(in_flight, ping),
            kwargs={"on_event": log},
            name="workshop-session-keepalive",
            daemon=True,
        )
        _thread.start()
    if log:
        log(f"session keepalive armed for {session_id} every {INTERVAL_S:.0f}s "
            f"while a run is in flight (cap {MAX_S:.0f}s)")
    return True
