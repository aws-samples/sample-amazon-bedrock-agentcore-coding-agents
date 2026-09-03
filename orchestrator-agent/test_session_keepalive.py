"""The coordinator must outlive the platform's idle-session reclaim, and only just.

A fire-and-forget build runs in a background thread inside the coordinator's microVM, and
AgentCore reclaims a session that receives no request for about fifteen minutes. Two live
builds died exactly that way (see session_keepalive.py). These tests pin the contract of
the fix: ping while work is in flight, never when idle, stop at the cap, never let a ping
failure touch the build, and answer the sentinel without a model turn.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import session_keepalive as ka  # noqa: E402


class _Clock:
    """A clock the loop drives: sleep() is the only thing that advances time."""

    def __init__(self) -> None:
        self.t = 0.0

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def now(self) -> float:
        return self.t


def _run(in_flight, *, ticks: int, ping, interval_s=240.0, max_s=5400.0, tick_s=15.0,
         events=None):
    clock = _Clock()
    budget = {"n": ticks}

    def stop() -> bool:
        if budget["n"] <= 0:
            return True
        budget["n"] -= 1
        return False

    ka.keepalive_loop(in_flight, ping, sleep=clock.sleep, now=clock.now,
                      interval_s=interval_s, max_s=max_s, tick_s=tick_s, stop=stop,
                      on_event=(events.append if events is not None else None))
    return clock


def test_it_pings_immediately_and_then_on_the_interval_while_a_run_is_in_flight():
    sent: list[float] = []
    clock_holder = {}

    def in_flight() -> int:
        return 1

    def ping() -> None:
        sent.append(clock_holder["clock"].t)

    clock = _Clock()
    clock_holder["clock"] = clock
    budget = {"n": 80}  # 80 ticks x 15s = 1200s of simulated build

    def stop() -> bool:
        if budget["n"] <= 0:
            return True
        budget["n"] -= 1
        return False

    ka.keepalive_loop(in_flight, ping, sleep=clock.sleep, now=clock.now,
                      interval_s=240.0, max_s=5400.0, tick_s=15.0, stop=stop)
    assert sent[0] == 0.0, "the first ping goes out as soon as work is seen"
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert gaps and all(240.0 <= g <= 255.0 for g in gaps), gaps
    # 1200s of build at a 240s interval: the reclaim window is ~900s, so this is the
    # property that matters -- no gap is ever long enough to lose the microVM.
    assert max(gaps) < 900.0


def test_an_idle_coordinator_is_never_pinged():
    """No work, no pings: an idle session SHOULD be reclaimed, and pings are not free."""
    sent = []
    _run(lambda: 0, ticks=100, ping=lambda: sent.append(1))
    assert sent == []


def test_the_cap_stops_the_pinging_and_says_so():
    sent = []
    events: list[str] = []
    _run(lambda: 1, ticks=60, ping=lambda: sent.append(1), interval_s=60.0, max_s=300.0,
         events=events)
    # 60 ticks = 900s simulated, but pinging stops once the 300s cap passes.
    assert len(sent) <= 6, sent
    assert any("cap reached" in e for e in events), events


def test_the_window_resets_when_the_build_finishes_so_the_next_one_is_protected():
    state = {"in_flight": 1}
    sent: list[int] = []
    clock = _Clock()
    budget = {"n": 60}

    def stop() -> bool:
        if budget["n"] <= 0:
            return True
        budget["n"] -= 1
        # first build runs 10 ticks, then idle 10, then a second build
        n = 60 - budget["n"]
        state["in_flight"] = 1 if (n <= 10 or n > 20) else 0
        return False

    ka.keepalive_loop(lambda: state["in_flight"], lambda: sent.append(1),
                      sleep=clock.sleep, now=clock.now, interval_s=240.0, max_s=5400.0,
                      tick_s=15.0, stop=stop)
    assert len(sent) >= 2, "the second build gets its own immediate ping"


def test_a_failing_ping_never_escapes():
    """The worst a broken keepalive may do is the reclaim that happens without it."""
    def ping() -> None:
        raise RuntimeError("throttled")

    events: list[str] = []
    _run(lambda: 1, ticks=40, ping=ping, events=events)  # must not raise
    assert any("ping failed" in e for e in events), events


def test_the_runtime_arn_comes_from_the_environment_agentcore_already_sets():
    arn = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/"
           "CodingAgents_orchestrator-ABC123")
    env = {"AGENTCORE_RUNTIME_URL":
           "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
           + arn.replace(":", "%3A").replace("/", "%2F") + "/invocations"}
    assert ka.self_runtime_arn(env) == arn
    assert ka.region_of(arn) == "us-west-2"
    assert ka.self_runtime_arn({}) is None, "not a deployed runtime: no keepalive"
    assert ka.self_runtime_arn({"AGENTCORE_RUNTIME_URL": "https://x/runtimes/nope/invocations"}) is None


def test_it_does_not_arm_outside_a_deployed_runtime(monkeypatch):
    monkeypatch.delenv("AGENTCORE_RUNTIME_URL", raising=False)
    assert ka.ensure_started("session-123", lambda: 1) is False


def test_the_entrypoint_answers_the_sentinel_without_building_an_agent():
    """The ping must cost no model turn: main.py has to return before _get_or_create_agent."""
    src = (_HERE / "main.py").read_text()
    body = src.split("async def invoke(", 1)[1]
    sentinel_at = body.index("KEEPALIVE_PROMPT")
    agent_at = body.index("_get_or_create_agent()")
    assert sentinel_at < agent_at, "the keepalive branch must short-circuit before the agent"
    branch = body[sentinel_at:agent_at]
    assert "return" in branch


def test_the_keepalive_is_not_on_the_verdict_path():
    """It may not import the engine, the reviewer, or an llm: it decides nothing."""
    src = (_HERE / "session_keepalive.py").read_text()
    for forbidden in ("import llm", "import reviewer", "import engine", "run_store"):
        assert forbidden not in src, forbidden
