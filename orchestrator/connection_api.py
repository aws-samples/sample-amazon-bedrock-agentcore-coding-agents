"""Connection API: the HTTP shell over the embedded orchestration engine.

Serves the frozen contract in API_CONTRACT.md. The shapes have not changed since
the stub days, so the Console built against the stub keeps working now that the
engine (``engine.py``) sits underneath.

    python3 orchestrator/connection_api.py        # serves http://localhost:8090

What happens on POST /api/runs:
  - the engine drives the five-phase blueprint (admission -> context hydration ->
    pre-flight -> agent execution -> finalization) on a worker thread,
  - the backend role boots the reference MCP server as a live subprocess,
  - the validator role proves the grading contract in-process,
  - the frontend role does a live tools/list round-trip,
  - finalization runs the pytest acceptance gate over the wire against the
    booted endpoint, with bounded iteration (2) then needs_human.

There is no race and no winner: the three agents are roles composed into one
deliverable; the result carries ``composed_from``, never a winner.

Extra (additive, contract-safe) endpoints the engine makes possible:
  GET /api/runs/{id}/events   : the append-only phase journal (the audit trail)

On AgentCore, the same engine swaps its local executor for runtime dispatch via
the reference harness's deploy/connect surface; this HTTP layer does not change.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat as _chat  # noqa: E402  the orchestrator brain (chatbot agent + streaming)
import github  # noqa: E402
import kiro_config  # noqa: E402  Kiro API key -> Token Vault (Settings pane)
import router  # noqa: E402
import runtime_config  # noqa: E402
from engine import (  # noqa: E402
    AGENTS,
    TERMINAL,
    Engine,
    public_diff,
    public_events,
    public_progress,
    public_result,
    public_run,
    public_terminals,
)

HOST = "0.0.0.0"
PORT = 8090

ENGINE = Engine()
# The chatbot agent dispatches through THIS engine, so the runs it kicks are the
# same runs the /api/runs endpoints poll. Without this, chat.py's module-default
# engine would create runs the console could never see.
_chat.use_engine(ENGINE)

# Per-conversation Strands message history, so the chatbot has multi-turn memory
# (question → answer → question) across stateless HTTP calls. Keyed by the
# conversation id the console sends; trimmed to a sane cap per conversation.
_CONVERSATIONS: dict[str, list] = {}
_CONV_LOCK = __import__("threading").Lock()
_MAX_TURNS = 40  # messages retained per conversation (user+assistant entries)


def chat_stream(conversation_id: str, prompt: str, model_id: str | None = None,
                attachments: list | None = None, user_identity: dict | None = None):
    """Drive one chat turn of the orchestrator agent and yield JSON-able
    events for the console to stream as SSE:

      {"type":"text","text":...}                 (assistant text delta)
      {"type":"run_started","run_id","kind"}      (a dispatch/build tool fired)
      {"type":"done"}                             (turn finished)

    A plain conversational turn yields only text + done (NO run_started), so the
    console shows a normal chatbot answer with no run panel. A run is born only
    when the agent calls a dispatch_*/run_build tool; the engine then fails loud
    at pre-flight if the role's runtime is not wired (never a local fake)."""
    # Propagate user identity into the engine context for audit attribution.
    if user_identity:
        from identity_baggage import UserIdentity, set_current_identity
        set_current_identity(UserIdentity.from_dict(user_identity))

    with _CONV_LOCK:
        history = list(_CONVERSATIONS.get(conversation_id, []))
    last_messages = None
    for ev in _chat.stream_chat(prompt, model_id=model_id, messages=history,
                                attachments=attachments):
        if ev.get("type") == "done":
            last_messages = ev.get("messages")
            continue
        yield ev
    if last_messages is not None:
        with _CONV_LOCK:
            _CONVERSATIONS[conversation_id] = last_messages[-_MAX_TURNS:]
    yield {"type": "done"}


def _reconcile_loop() -> None:
    """Periodic stranded-run sweep. The engine's reconcile() only matters when a
    worker thread dies outright (its own try/finally never ran); without this
    caller that run would sit non-terminal forever. 60s cadence; STRANDED_AFTER_S
    inside reconcile() decides what is actually stranded."""
    import threading as _threading
    import time as _time

    def _loop() -> None:
        while True:
            _time.sleep(60)
            try:
                ENGINE.reconcile()
            except Exception:  # noqa: BLE001 (the sweeper must never die)
                pass

    _threading.Thread(target=_loop, daemon=True, name="engine-reconcile").start()


_reconcile_loop()

DEFAULT_TASK = ("Convert /mnt/s3files/sample/cost_analyzer.py to a remote MCP server "
                "with tests + a chatbot UI")


def dispatch(method: str, path: str, body: dict | None,
             query: str = "") -> tuple[int, dict]:
    """Pure router for the Stage 2 API: (status, json-able dict).

    Shared by the standalone server (below) and the unified console server
    (`console/server.py`) so both run the SAME embedded engine. ``query`` is
    the raw query string (e.g. ``limit=20&offset=40``) for paged endpoints; it is
    optional so existing 3-arg callers keep working.
    """
    if method == "GET":
        if path == "/api/health":
            # executor: which execution seam produces artifacts. Shipped is
            # "agentcore" (dispatch to deployed role runtimes, fail loud on a
            # missing wired ARN); deterministic offline tests inject "fixture".
            return 200, {"status": "ok", "mode": "engine",
                         "executor": ENGINE.executor.name}
        if path == "/api/agents":
            return 200, {"agents": AGENTS}
        if path == "/api/workflows":
            return 200, {"workflows": router.public_workflows()}
        if path == "/api/models":
            # The orchestrator's selectable brain models, resolved from the real
            # Bedrock catalog at runtime; the message-bar picker fetches this
            # instead of a hardcoded list.
            return 200, _chat.available_models()
        if path == "/api/suggestions":
            # Opening prompts for the empty chat, derived from the real workflow
            # registry; the chips are dynamic, not hardcoded in the frontend.
            return 200, _chat.suggestions()
        if path == "/api/github":
            return 200, github.status()
        if path == "/api/kiro":
            # Kiro API key status (masked tail only). The key itself lives in the
            # AgentCore Identity Token Vault, never in this response.
            return 200, kiro_config.status()
        if path == "/api/runtimes":
            # The wirable AgentCore runtime ARNs (one per role). These are SET by
            # the attendee after `agentcore deploy`, never hardcoded.
            return 200, runtime_config.status()
        if path == "/api/runs":
            # Newest-first, with optional limit/offset paging so the sidebar can
            # infinite-scroll a long history instead of fetching everything each
            # poll. No params -> the full list (back-compat). `total` lets the
            # client know when it has reached the end.
            all_runs = list(reversed(ENGINE.list()))
            total = len(all_runs)
            qs = parse_qs(query or "")
            try:
                offset = max(0, int(qs.get("offset", ["0"])[0]))
            except ValueError:
                offset = 0
            limit_raw = qs.get("limit", [None])[0]
            window = all_runs[offset:]
            if limit_raw is not None:
                try:
                    window = window[:max(0, int(limit_raw))]
                except ValueError:
                    pass
            return 200, {"runs": [public_run(r) for r in window],
                         "total": total, "offset": offset}
        if path.startswith("/api/runs/"):
            parts = path.split("/")
            run = ENGINE.get(parts[3] if len(parts) > 3 else "")
            if not run:
                return 404, {"error": "run not found"}
            if len(parts) == 5 and parts[4] == "result":
                if run.status in TERMINAL:
                    return 200, public_result(run)
                return 409, {"status": run.status, "phase": run.phase}
            if len(parts) == 5 and parts[4] == "events":
                return 200, {"run_id": run.run_id, "events": run.events}
            if len(parts) == 5 and parts[4] == "terminals":
                # Both surfaces in one payload: `terminals` = the raw per-role
                # shell transcript; `events` = the structured tool_use/thinking/
                # text stream the console renders as real tool calls + reasoning.
                return 200, {"run_id": run.run_id,
                             "terminals": public_terminals(run),
                             "events": public_events(run)}
            if len(parts) == 5 and parts[4] == "diff":
                # The composed change as a per-file unified diff (the session
                # Changes tab): the real `git show` of this run's commit.
                return 200, public_diff(run)
            out = public_run(run)
            out["progress"] = public_progress(run)
            return 200, out
        return 404, {"error": "not found", "path": path}

    if method == "POST":
        if path == "/api/runs":
            if body is None:
                return 400, {"error": "invalid JSON body"}
            run = ENGINE.submit(
                task=body.get("task") or DEFAULT_TASK,
                # agents omitted -> the ROUTER decides which roles dispatch;
                # an explicit list is honored (and validated) for compat.
                agents=body.get("agents"),
                options=body.get("options") or {},
                workflow_ref=body.get("workflow_ref"),
            )
            return 202, public_run(run)
        if path == "/api/github":
            if body is None:
                return 400, {"error": "invalid JSON body"}
            if body.get("clear"):
                return 200, github.clear_settings()
            # A policy-only flip (merge_policy present, no repo) toggles auto-merge
            # without re-entering the connection; it touches only the policy file.
            if body.get("merge_policy") is not None and not body.get("repo"):
                return 200, github.set_merge_policy(body.get("merge_policy"))
            # Gateway model: the attendee supplies their template-derived repo
            # (owner/name); NO token. The gateway URL is normally wired by the
            # workshop (env), but the console may also pass it. merge_policy rides
            # the same POST; an omitted/unknown value fails closed to human_review.
            out = github.save_settings(body.get("repo", ""),
                                       body.get("gateway_url"),
                                       body.get("merge_policy"))
            return (400, out) if "error" in out else (200, out)
        if path == "/api/kiro":
            # Paste a Kiro API key (ksk_...) -> store it in the AgentCore Identity
            # Token Vault so the deployed Kiro runtime authenticates with no redeploy.
            if body is None:
                return 400, {"error": "invalid JSON body"}
            if body.get("clear"):
                return 200, kiro_config.clear_api_key()
            out = kiro_config.save_api_key(body.get("api_key", ""))
            return (400, out) if "error" in out else (200, out)
        if path == "/api/runtimes":
            # Wire (or unwire) a role's deployed AgentCore runtime ARN. The same
            # surface the Settings pane and the terminal write to. A role is a
            # FLEET: `add` grows it (a 2nd/3rd instance of a type), `save` (default)
            # replaces it with a single instance, `clear` unwires the whole role.
            if body is None:
                return 400, {"error": "invalid JSON body"}
            if body.get("clear"):
                return 200, runtime_config.clear_runtime(body.get("role"))
            # Remove ONE instance (the per-instance x button).
            if body.get("remove"):
                out = runtime_config.remove_runtime(body.get("role", ""), body.get("arn", ""))
                return (400, out) if "error" in out else (200, out)
            # Set an INSTANCE description (keyed by its ARN), read by the chatbot to
            # describe its dispatch targets dynamically. Sent with role + arn and
            # NO wire intent (the "describe" flag, or a description without add/save).
            if body.get("describe"):
                out = runtime_config.save_description(
                    body.get("role", ""), body.get("arn", ""), body.get("description", ""))
                return (400, out) if "error" in out else (200, out)
            # A Kiro agent carries its own API key: store it in the Token Vault
            # BEFORE wiring the ARN, so a bad key fails the whole add (never a wired
            # runtime that can't authenticate). Only the kiro role uses this.
            api_key = (body.get("api_key") or "").strip()
            if api_key and body.get("role") == "kiro":
                kout = kiro_config.save_api_key(api_key)
                if "error" in kout:
                    return 400, {"error": f"Kiro API key: {kout['error']}"}
            if body.get("add"):
                out = runtime_config.add_runtime(body.get("role", ""), body.get("arn", ""))
            else:
                out = runtime_config.save_runtime(body.get("role", ""), body.get("arn", ""))
            # An ARN write may carry a description for that same instance.
            if "error" not in out and body.get("description"):
                runtime_config.save_description(
                    body.get("role", ""), body.get("arn", ""), body.get("description", ""))
                out = runtime_config.status()
            return (400, out) if "error" in out else (200, out)
        return 404, {"error": "not found", "path": path}

    return 404, {"error": "method not allowed", "method": method}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # This API triggers real runs and manages the GitHub credential, so a
        # wildcard CORS header would let any website start runs or open PRs on an
        # attendee's repo. Emit CORS only for a genuine same-origin call,
        # reflecting that exact origin; cross-origin browsers are blocked from
        # reading the response. Same-origin GETs and curl send no Origin.
        origin = self._same_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _same_origin(self) -> str | None:
        """Return the request Origin iff same-origin with the Host, else None.

        Missing/garbled Origin (same-origin GETs, curl) yields None: no CORS
        headers, which is correct. Mirrors console/server.py's gate.
        """
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return None
        try:
            authority = urlparse(origin).netloc
        except ValueError:
            return None
        return origin if authority and authority.lower() == host.lower() else None

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return None

    def log_message(self, *args) -> None:  # quiet logs
        pass

    def do_OPTIONS(self) -> None:  # CORS preflight
        self._send(200, {})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        code, out = dispatch("GET", path, None, parsed.query)
        self._send(code, out)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        code, out = dispatch("POST", path, self._body())
        self._send(code, out)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Connection API (embedded engine) on http://localhost:{PORT}")
    print("POST /api/runs runs the REAL blueprint: live MCP server subprocess + "
          "real pytest acceptance gate. GET /api/runs/{id}/events for the journal.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        ENGINE.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
