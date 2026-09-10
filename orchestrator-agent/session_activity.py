"""Register background builds with AgentCore's native async-task lifecycle.

A dispatch returns before its worker thread finishes. Without async-task
registration, Runtime sees an idle session and may reclaim it after 15 minutes.
Two live builds previously stopped at 15m06s and 15m08s after their last request.

The SDK reports HealthyBusy while our registration exists. No self-invocation,
model call, forced health status, or open client connection is required. The
entrypoint observes activity before closing its response; a small observer then
releases the registration when every build has stopped.

The engine supplies the lifetime bound. An interrupted or wedged build cannot
keep the session busy forever, and a later request cannot reset that bound.
This module reads only an activity count: it never changes a run or its verdict.
"""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Any, Callable


class ActivityTracker:
    def __init__(
        self,
        app: Any,
        in_flight: Callable[[], int],
        *,
        max_s: float,
        tick_s: float = 5.0,
        now: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        # Keep the existing operator override compatible with older deployments.
        override = os.environ.get("WORKSHOP_KEEPALIVE_MAX_S")
        self.max_s = float(override) if override else float(max_s)
        if not math.isfinite(self.max_s) or self.max_s <= 0:
            raise ValueError("The background activity lifetime must be finite and positive")
        if not math.isfinite(tick_s) or tick_s <= 0:
            raise ValueError("The background activity interval must be finite and positive")
        self.app, self.in_flight = app, in_flight
        self.tick_s, self.now = tick_s, now
        self.log = log or (lambda message: None)
        self._task_id: int | None = None
        self._started_at: float | None = None
        self._exhausted = False
        self._lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _complete(self) -> None:
        if self._task_id is not None:
            self.app.complete_async_task(self._task_id)
            self._task_id = None

    def observe(self) -> None:
        """Synchronize SDK health with the engine's actual queued/running count.

        Called synchronously before an invocation response closes as well as by
        the observer. A counter failure preserves busy status within the SAME
        bounded window; it cannot silently abandon an existing build.
        """
        with self._lock:
            if self._stop.is_set():
                return
            try:
                work = self.in_flight()
            except Exception as exc:
                self.log(f"Could not read background activity ({exc}); preserving the bounded activity window")
                work = 1
            if work <= 0:
                self._complete()
                self._started_at = None
                self._exhausted = False
                return

            moment = self.now()
            if self._started_at is None:
                self._started_at = moment
            if moment - self._started_at >= self.max_s:
                self._complete()
                if not self._exhausted:
                    self.log(f"Background activity cap reached after {self.max_s:.0f}s; releasing the session")
                self._exhausted = True
                return
            if not self._exhausted and self._task_id is None:
                self._task_id = self.app.add_async_task("coding-builds")
                self.log("Background builds registered with AgentCore async-task tracking")

    def _watch(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self.observe()
            except Exception as exc:
                # SDK tracking is local bookkeeping. If it fails, retain the
                # original deadline and retry without touching a build result.
                self.log(f"Background activity tracking failed ({exc}); retrying")

    def ensure_started(self) -> None:
        self.observe()
        with self._thread_lock:
            if self._stop.is_set():
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._watch, name="workshop-background-activity", daemon=True,
                )
                self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._complete()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
