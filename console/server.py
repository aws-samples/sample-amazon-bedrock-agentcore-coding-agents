"""Unified console backend: one origin for the complete workshop product.

FastAPI + uvicorn. This is the API backend AND, in production, the static host
for the built React console (console/dist). In development the React app is
served by Vite (`npm --prefix console/web run dev`, :5174) with `/api`
proxied here; in production uvicorn serves both `/api/*` and the built dist at
one origin.

    python3 console/server.py            # uvicorn on :8080 (prod-style)
    uvicorn server:app --reload --port 8080  # backend HMR for development

Routes:
  - GET  /                  -> the built console (dist/index.html); SPA fallback
  - *    /api/dev/...        -> Stage 1 interactive engine   (interactive_api.dispatch)
  - *    /api/orchestrator/...        -> Stage 2 orchestration engine  (connection_api.dispatch)
  - *    /api/metrics/...        -> Stage 3 metrics/governance     (metrics_api.dispatch)
  - GET  /api/dev/.../pty/stream -> Server-Sent Events PTY stream (real-time)
  - GET  /api/health        -> rollup health of all three engines

The three `dispatch()` functions are the single source of truth; this server
and the standalone per-stage servers run the exact same engine code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
from binascii import Error as BinasciiError

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import iterate_in_threadpool, run_in_threadpool
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)

# The Stage 1/2/3 engine modules are siblings of this console dir in every layout:
# The single repo has console + interactive-api as siblings; the attendee clone
# flattens to console + interactive-api. So their parent (_ENGINES) is the
# console dir's parent, never a hardcoded "solution" level.
_ENGINES = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ENGINES, "interactive-api"))
sys.path.insert(0, os.path.join(_ENGINES, "orchestrator"))
sys.path.insert(0, os.path.join(_ENGINES, "metrics-api"))

import connection_api   # noqa: E402  (Stage 2)
import interactive_api  # noqa: E402  (Stage 1)
import metrics_api      # noqa: E402  (Stage 3)
import cognito_auth     # noqa: E402  (Cognito OAuth2)
import runtime_shell    # noqa: E402  (Real runtime PTY proxy)

HOST = "0.0.0.0"
PORT = int(os.environ.get("CONSOLE_PORT", os.environ.get("PORT", "8080")))

# The built React app (Vite output). Served in production.
_DIST = os.path.join(_HERE, "dist")
_INDEX = os.path.join(_DIST, "index.html")
_ASSETS = os.path.realpath(os.path.join(_DIST, "assets"))
_DIST_PUBLIC = os.path.realpath(_DIST)

# One URL, always: open :8080 in both dev and prod. In dev (CONSOLE_DEV=1) this
# backend reverse-proxies every non-/api request (HTML, JS, the Vite HMR client +
# its websocket) to the Vite dev server, so the frontend hot-reloads without you
# ever opening Vite's port directly. In prod it serves the built dist instead.
DEV_MODE = os.environ.get("CONSOLE_DEV", "").lower() in ("1", "true", "yes")
_VITE_URL = os.environ.get("VITE_DEV_URL", "http://localhost:5174")
_VITE_WS = _VITE_URL.replace("http://", "ws://").replace("https://", "wss://")

# ---------------------------------------------------------------------------
# Cookie-session login gate. Opt-in: disabled unless CONSOLE_PASSWORD is set, so
# local dev and pytest need zero config. Engages in the deployed stack where
# cfn.yaml feeds CONSOLE_PASSWORD into the systemd unit. Credential reuses the
# code-server password (username `ubuntu` = CodeServerUser); no new secret.
# ---------------------------------------------------------------------------
CONSOLE_USER = os.environ.get("CONSOLE_USER", "ubuntu")
CONSOLE_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "")
AUTH_ENABLED = bool(CONSOLE_PASSWORD)
# Where to land a user after login. Behind the workshop's CloudFront + nginx the
# console is served under /console/ (code-server owns /), so both the password
# gate and the Cognito callback must return here, NOT /. Overridable for any other
# mount; defaults to the same /console/ the password gate already uses.
CONSOLE_BASE_PATH = os.environ.get("CONSOLE_BASE_PATH", "/console/")
COOKIE_NAME = "console_session"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days, no mid-workshop re-prompt
_SESSION_SECRET = secrets.token_bytes(32)  # restart invalidates cookies (fine for a workshop box)


def _mint_token() -> str:
    issued = str(int(time.time())).encode("ascii")
    payload = base64.urlsafe_b64encode(issued).decode("ascii").rstrip("=")
    sig = hmac.new(_SESSION_SECRET, issued, hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _valid_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.partition(".")
    try:
        pad = "=" * (-len(payload) % 4)
        issued = base64.urlsafe_b64decode(payload + pad)
    except (ValueError, BinasciiError):
        return False
    expected = hmac.new(_SESSION_SECRET, issued, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        ts = int(issued.decode("ascii"))
    except ValueError:
        return False
    return (time.time() - ts) <= COOKIE_MAX_AGE


def _authed(request: Request) -> bool:
    if not AUTH_ENABLED and not cognito_auth.COGNITO_ENABLED:
        return True
    if cognito_auth.COGNITO_ENABLED:
        return cognito_auth.get_user_from_request_cookies(request.cookies) is not None
    tok = request.cookies.get(COOKIE_NAME)
    return bool(tok) and _valid_token(tok)


def _current_user(request: Request) -> cognito_auth.CognitoUser | None:
    """Extract the authenticated user (Cognito mode only)."""
    if cognito_auth.COGNITO_ENABLED:
        return cognito_auth.get_user_from_request_cookies(request.cookies)
    return None


# ---------------------------------------------------------------------------
# Engine dispatch hub: the three stages, in-process.
# ---------------------------------------------------------------------------
# The three engines mount under meaningful names (not s1/s2/s3): the interactive
# dev workspace, the orchestrator, and the metrics API. Each engine's own
# dispatch() still matches "/api/<resource>" internally, so the mount layer
# re-adds that "/api" prefix when forwarding; the engines are untouched, only the
# public URL is clean (e.g. /api/orchestrator/runs -> connection_api "/api/runs").
_MOUNTS = {"dev": "s1", "orchestrator": "s2", "metrics": "s3"}


def _route_api(method: str, full_path: str, query: str, body: dict | None):
    """Forward /api/<mount>/<resource> to the right engine.
    full_path looks like /api/orchestrator/runs -> mount=orchestrator, sub=/api/runs."""
    parts = full_path.split("/", 3)  # ['', 'api', 'orchestrator', 'runs']
    mount = parts[2] if len(parts) > 2 else ""
    rest = parts[3] if len(parts) > 3 else ""
    rest = rest.rstrip("/")
    sub = "/api/" + rest if rest else "/api"   # engines match "/api/<resource>"
    if mount == "dev":
        return interactive_api.dispatch(method, sub, body)
    if mount == "orchestrator":
        return connection_api.dispatch(method, sub, body, query)
    if mount == "metrics":
        return metrics_api.dispatch(method, sub, query, body)
    return 404, {"error": "unknown API mount", "mount": mount}


def _health() -> dict:
    engines = {
        "s1": interactive_api.dispatch("GET", "/api/health", None)[1],
        "s2": connection_api.dispatch("GET", "/api/health", None)[1],
        "s3": metrics_api.dispatch("GET", "/api/health", "", None)[1],
    }
    return {"status": "ok", "mode": "engine", "engines": engines}


app = FastAPI(title="Coding Agents Console", docs_url=None, redoc_url=None)


# ---- Cognito OAuth2 routes (active when COGNITO_USER_POOL_ID is set) ------
if cognito_auth.COGNITO_ENABLED:
    @app.get("/auth/login")
    @app.get("/console/auth/login")
    async def cognito_login(request: Request):
        """Render the console's OWN branded sign-in page (email + password). We
        authenticate directly against Cognito (USER_PASSWORD_AUTH) on POST, so the
        attendee never sees the unstyled Hosted UI. The Hosted-UI authorization-code
        flow stays wired (callback below) as a fallback / for any IdP federation."""
        return HTMLResponse(_login_page())

    @app.post("/auth/login")
    @app.post("/console/auth/login")
    async def cognito_login_submit(request: Request):
        """Authenticate email+password against Cognito and set the session cookie."""
        form = await request.form()
        email = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        tokens = await run_in_threadpool(cognito_auth.initiate_password_auth, email, password)
        if not tokens:
            return HTMLResponse(_login_page("Incorrect email or password.", email), status_code=401)
        result = cognito_auth.create_session(tokens)
        if not result:
            return HTMLResponse(_login_page("Sign-in failed. Please try again.", email), status_code=401)
        session_id, _user = result
        resp = RedirectResponse(CONSOLE_BASE_PATH, status_code=302)
        resp.set_cookie(
            cognito_auth.SESSION_COOKIE, session_id,
            max_age=cognito_auth.SESSION_MAX_AGE,
            httponly=True, samesite="lax", secure=True, path="/",
        )
        return resp

    @app.get("/auth/callback")
    async def cognito_callback(request: Request, code: str = "", state: str = ""):
        """Handle Cognito OAuth2 callback: exchange code, create session."""
        if not code:
            return HTMLResponse("<h1>Missing authorization code</h1>", status_code=400)
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
        callback_url = f"{scheme}://{host}{cognito_auth.COGNITO_CALLBACK_PATH}"
        tokens = cognito_auth.exchange_code(code, callback_url)
        if not tokens:
            return HTMLResponse("<h1>Token exchange failed</h1>", status_code=401)
        result = cognito_auth.create_session(tokens)
        if not result:
            return HTMLResponse("<h1>Invalid token</h1>", status_code=401)
        session_id, user = result
        # Land on the console (served under /console/ behind nginx), not / which is
        # code-server in the deployed stack. Matches the password-gate redirect.
        resp = RedirectResponse(CONSOLE_BASE_PATH, status_code=302)
        resp.set_cookie(
            cognito_auth.SESSION_COOKIE, session_id,
            max_age=cognito_auth.SESSION_MAX_AGE,
            httponly=True, samesite="lax", secure=True, path="/",
        )
        return resp

    @app.get("/auth/logout")
    async def cognito_logout(request: Request):
        """Clear session and redirect to Cognito logout."""
        sid = request.cookies.get(cognito_auth.SESSION_COOKIE)
        if sid:
            cognito_auth.destroy_session(sid)
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
        # Post-logout landing must match a registered Cognito LogoutURL (the stack
        # registers the console base path), so send the user back to /console/.
        logout_redirect = f"{scheme}://{host}{CONSOLE_BASE_PATH}"
        resp = RedirectResponse(cognito_auth.get_logout_url(logout_redirect), status_code=302)
        resp.delete_cookie(cognito_auth.SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        """Return the current authenticated user's identity (for the frontend)."""
        user = _current_user(request)
        if not user:
            return JSONResponse({"authenticated": False}, status_code=401)
        return JSONResponse({
            "authenticated": True,
            "user_id": user.sub,
            "email": user.email,
            "name": user.name,
            "groups": user.groups,
        })


# ---- Auth endpoints (only meaningful when AUTH_ENABLED) -------------------
@app.post("/login")
@app.post("/console/login")
async def login(request: Request):
    form = await request.form()
    user = str(form.get("username", ""))
    pw = str(form.get("password", ""))
    ok = AUTH_ENABLED and hmac.compare_digest(user, CONSOLE_USER) and hmac.compare_digest(pw, CONSOLE_PASSWORD)
    if not ok:
        return HTMLResponse(_login_page("Incorrect username or password."), status_code=401)
    resp = RedirectResponse("/console/", status_code=302)
    resp.set_cookie(
        COOKIE_NAME, _mint_token(), max_age=COOKIE_MAX_AGE, httponly=True,
        samesite="lax", secure=AUTH_ENABLED, path="/",
    )
    return resp


@app.get("/logout")
@app.get("/console/logout")
async def logout():
    resp = RedirectResponse("/console/", status_code=302)
    # Emit the exact clearing cookie the console contract expects:
    # `console_session=; ...; Max-Age=0` (empty value, no quotes). FastAPI's
    # delete_cookie uses an Expires date and can quote the empty value, so set
    # the header verbatim instead.
    secure = "; Secure" if AUTH_ENABLED else ""
    resp.headers["Set-Cookie"] = (
        f"{COOKIE_NAME}=; HttpOnly{secure}; SameSite=Lax; Path=/; Max-Age=0"
    )
    return resp


# ---- Health (always reachable; details gated) -----------------------------
@app.get("/api/health")
async def health(request: Request):
    if not _authed(request):
        return JSONResponse({"status": "ok"})
    return JSONResponse(_health())


# ---- Real-time PTY over SSE ----------------------------------------------
@app.get("/api/dev/sessions/{session_id}/pty/stream")
async def pty_stream(session_id: str, request: Request, offset: int = 0):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # interactive_api.pty_stream_async is a NATIVE async generator: its only wait
    # is `await asyncio.sleep`, so when the client disconnects or the server is
    # shutting down (dev `--reload`), Starlette/asyncio cancels it at that await
    # and the connection releases immediately. No worker thread, no manual
    # disconnect polling; cancellation is structural. (A sync generator parked
    # in time.sleep, by contrast, could not be cancelled and hung shutdown.)
    return StreamingResponse(
        interactive_api.pty_stream_async(session_id, offset),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx proxy buffering
        },
    )


# ---- Runtime shell proxy: connect browser to a REAL AgentCore Runtime ------
@app.get("/api/dev/runtime-sessions")
async def runtime_session_list(request: Request, agent_id: str | None = None):
    """Server-side session registry: the source of truth for terminal tabs. The
    orchestrator opens sessions itself when it dispatches a role (opened_by=
    "orchestrator"), so the Agents page polls this list and shows those live
    PTYs alongside the human-opened ones -- one shared session, both surfaces."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(runtime_shell.list_sessions(agent_id))


@app.post("/api/dev/runtime-sessions")
async def runtime_session_open(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import json as _json
    raw = await request.body()
    body = _json.loads(raw) if raw else {}
    agent_id = body.get("agent_id", "claude-code")
    cols = body.get("cols", 80)
    rows = body.get("rows", 24)
    # Optional: which wired instance to open against (a fleet has more than one).
    instance_arn = body.get("instance_arn") or None
    # open_runtime_session reads runtime_config (file I/O) before spawning the
    # connection thread; run it off the event loop so a slow read can't stall the
    # loop and freeze the other pages.
    result = await run_in_threadpool(
        runtime_shell.open_runtime_session, agent_id, cols, rows, instance_arn)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return JSONResponse(result, status_code=201)


@app.delete("/api/dev/runtime-sessions/{session_id}")
async def runtime_session_close(session_id: str, request: Request):
    """Close a session and drop it from the registry (the human closed the tab).
    Without this the backend session stays alive and the Agents page's server-
    registry sync re-adds it as a tab, so a closed tab reappears."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(runtime_shell.close_runtime_session(session_id))


@app.post("/api/dev/runtime-sessions/{session_id}/input")
async def runtime_session_input(session_id: str, request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import json as _json
    raw = await request.body()
    body = _json.loads(raw) if raw else {}
    text = body.get("input", "")
    return JSONResponse(runtime_shell.send_input(session_id, text))


@app.post("/api/dev/runtime-sessions/{session_id}/resize")
async def runtime_session_resize(session_id: str, request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import json as _json
    raw = await request.body()
    body = _json.loads(raw) if raw else {}
    return JSONResponse(runtime_shell.resize(session_id, body.get("cols", 80), body.get("rows", 24)))


@app.get("/api/dev/runtime-sessions/{session_id}/stream")
async def runtime_session_stream(session_id: str, request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return StreamingResponse(
        runtime_shell.stream_output(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ---- Stage 2 chat over SSE: talk to the orchestrator agent ----------------
# The chat box drives the Strands orchestrator (chat.py). A turn is a normal
# conversation; a run (and "Running") is born ONLY when the agent calls a
# dispatch_*/run_build tool, surfaced as a `run_started` event mid-stream, so
# "야 안녕?" answers like a chatbot instead of kicking off a build.
@app.post("/api/orchestrator/chat")
async def s2_chat(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import json as _json
    raw = await request.body()
    try:
        payload = _json.loads(raw) if raw else {}
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    prompt = (payload.get("prompt") or "").strip()
    conversation_id = payload.get("conversation_id") or "default"
    model_id = payload.get("model") or None
    attachments = payload.get("attachments") or None
    if not prompt and not attachments:
        return JSONResponse({"error": "empty prompt"}, status_code=400)

    # Thread the authenticated user's identity into the orchestrator chain
    user_baggage: dict[str, str] = {}
    cognito_user = _current_user(request)
    if cognito_user:
        user_baggage = cognito_user.to_baggage()

    def gen():
        try:
            for ev in connection_api.chat_stream(conversation_id, prompt, model_id,
                                                 attachments, user_identity=user_baggage):
                yield f"data: {_json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001 (surface the real error, never hang)
            yield f"data: {_json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
        yield "event: end\ndata: {}\n\n"

    # chat_stream is a BLOCKING generator: it drives the Strands agent, which makes
    # synchronous Bedrock network calls between yields. A sync generator handed to
    # StreamingResponse is iterated on the event-loop thread, so each blocking yield
    # would freeze every other page for the whole turn. iterate_in_threadpool pumps
    # it from a worker thread, keeping the loop free while tokens stream.
    return StreamingResponse(
        iterate_in_threadpool(gen()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- Stage API dispatch (s1/s2/s3, all methods) ---------------------------
@app.api_route("/api/{rest:path}", methods=["GET", "POST", "DELETE"])
async def api(rest: str, request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    full_path = "/api/" + rest
    query = request.url.query
    body = None
    if request.method in ("POST", "PUT", "DELETE"):
        raw = await request.body()
        if raw:
            try:
                import json as _json
                body = _json.loads(raw)
            except ValueError:
                return JSONResponse({"error": "bad json"}, status_code=400)
        else:
            body = {}
    # _route_api dispatches into the SYNCHRONOUS engine handlers, some of which
    # block (subprocess.run, urlopen, time.sleep, boto3). Calling them directly in
    # this async route would block the single uvicorn event loop and freeze EVERY
    # page (the recurring "localhost died" hang). Offload to a worker thread so the
    # loop stays responsive while a slow engine call runs.
    code, out = await run_in_threadpool(_route_api, request.method, full_path, query, body)
    return JSONResponse(out, status_code=code)


# ---- Static: built dist (assets) + SPA fallback ---------------------------
@app.get("/assets/{rel:path}")
async def assets(rel: str):
    full = os.path.realpath(os.path.join(_ASSETS, rel))
    if not (full == _ASSETS or full.startswith(_ASSETS + os.sep)) or not os.path.isfile(full):
        return JSONResponse({"error": "not found", "path": rel}, status_code=404)
    return FileResponse(full, headers={"Cache-Control": "max-age=31536000, immutable"})


if DEV_MODE:
    from fastapi import WebSocket  # noqa: PLC0415

    @app.websocket("/{full_path:path}")
    async def vite_hmr(ws: WebSocket, full_path: str):
        """Bridge the Vite HMR websocket through :8080 so hot updates reach the
        browser on the one URL. Vite negotiates the 'vite-hmr' subprotocol."""
        import websockets  # noqa: PLC0415
        sub = ws.headers.get("sec-websocket-protocol")
        await ws.accept(subprotocol=sub.split(",")[0].strip() if sub else None)
        url = _VITE_WS + "/" + full_path
        if ws.url.query:
            url += "?" + ws.url.query
        try:
            up = await websockets.connect(
                url, subprotocols=[sub] if sub else None, open_timeout=10)
        except Exception:  # noqa: BLE001 (Vite not up; just close cleanly)
            await ws.close()
            return
        import asyncio  # noqa: PLC0415

        async def c2u():  # browser -> Vite
            try:
                while True:
                    await up.send(await ws.receive_text())
            except Exception:  # noqa: BLE001
                pass

        async def u2c():  # Vite -> browser
            try:
                async for msg in up:
                    await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                pass

        try:
            await asyncio.gather(c2u(), u2c())
        finally:
            await up.close()


async def _proxy_to_vite(full_path: str, request: Request):
    """Dev mode: forward a non-/api request to the Vite HMR server and stream the
    response back, so the whole app lives behind :8080 (one URL) while still
    hot-reloading. Vite serves index.html, the JS modules, and the HMR client."""
    import httpx  # noqa: PLC0415
    url = _VITE_URL + "/" + full_path
    if request.url.query:
        url += "?" + request.url.query
    client = httpx.AsyncClient(timeout=30.0)
    try:
        upstream = client.build_request(
            request.method, url,
            headers=[(k, v) for k, v in request.headers.raw
                     if k.lower() not in (b"host", b"accept-encoding")],
            content=await request.body())
        resp = await client.send(upstream, stream=True)
    except Exception as exc:  # noqa: BLE001 (Vite not up yet)
        await client.aclose()
        return HTMLResponse(
            f"<h1>Vite dev server not reachable</h1><p>CONSOLE_DEV is on but "
            f"{_VITE_URL} did not answer ({exc}). Start it: "
            f"<code>npm --prefix console/web run dev</code>.</p>", status_code=502)

    async def _body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    hop = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in hop}
    return StreamingResponse(_body(), status_code=resp.status_code,
                             headers=headers, media_type=resp.headers.get("content-type"))


@app.get("/{full_path:path}")
async def spa(full_path: str, request: Request):
    """Serve a real dist file when one matches; otherwise the SPA index so
    BrowserRouter owns /agents, /fleets/:id, /governance on reload/deep-link.
    In dev mode every non-/api request is proxied to Vite (one URL, HMR intact).
    Login wall is shown for the root when auth is on and the caller is anon."""
    path = "/" + full_path
    if not _authed(request):
        if cognito_auth.COGNITO_ENABLED:
            return RedirectResponse("/auth/login", status_code=302)
        if AUTH_ENABLED:
            if path in ("/", "/index.html", "/console", "/console/"):
                return HTMLResponse(_login_page())
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Dev: the frontend lives on Vite; proxy everything non-/api so :8080 is the
    # only URL and HMR still works.
    if DEV_MODE:
        return await _proxy_to_vite(full_path, request)
    # A concrete static file under dist (favicon, etc.).
    if full_path and "." in os.path.basename(full_path):
        cand = os.path.realpath(os.path.join(_DIST_PUBLIC, full_path))
        if (cand == _DIST_PUBLIC or cand.startswith(_DIST_PUBLIC + os.sep)) and os.path.isfile(cand):
            return FileResponse(cand)
        return JSONResponse({"error": "not found", "path": full_path}, status_code=404)
    # SPA routes + "/" -> index.html.
    if os.path.isfile(_INDEX):
        return FileResponse(_INDEX)
    return HTMLResponse(
        "<h1>Console not built</h1><p>Run "
        "<code>npm --prefix console/web install &amp;&amp; "
        "npm --prefix console/web run build</code>, or use the Vite dev server.</p>",
        status_code=503,
    )


def _login_page(error: str = "", email: str = "") -> str:
    """The console's branded sign-in page, shared by both auth modes. In Cognito
    mode it posts email+password to /auth/login (direct USER_PASSWORD_AUTH); in the
    password-gate fallback it posts username+password to ./login. The visual is the
    console's own: shadcn neutral light, the brand mark, the mesh-gradient backdrop."""
    import html as _html
    cognito = cognito_auth.COGNITO_ENABLED
    # Cognito mode authenticates by email and the page lives at /auth/login, so the
    # form posts to itself; the password gate keeps its ./login relative action and
    # prefills the fixed CONSOLE_USER. One template, both modes.
    action = "" if cognito else "login"       # "" = post to current path (/auth/login)
    id_label = "Email" if cognito else "Username"
    id_value = _html.escape(email if cognito else CONSOLE_USER, quote=True)
    id_type = 'type="email" ' if cognito else ""
    id_auto = "username"
    id_ph = ' placeholder="name@host.com"' if cognito else ""
    err = f'<p class="err">{_html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>Sign in · Coding Agents Console</title>
<style>
 *{{box-sizing:border-box}}
 body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
 background:#fafafa;color:#171717;-webkit-font-smoothing:antialiased;
 font-family:"Geist Variable",Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 /* The brand mesh gradient as an atmospheric backdrop (hero scale, low opacity). */
 background-image:
  radial-gradient(at 18% 12%,#007cf022 0,transparent 50%),
  radial-gradient(at 80% 8%,#ff008016 0,transparent 48%),
  radial-gradient(at 55% 0%,#7928ca1c 0,transparent 46%),
  radial-gradient(at 92% 40%,#f9cb281c 0,transparent 44%)}}
 .card{{width:360px;background:#fff;border:1px solid #ebebeb;border-radius:12px;padding:32px;
 box-shadow:0 1px 1px rgba(0,0,0,.02),0 8px 16px -4px rgba(0,0,0,.06),inset 0 0 0 1px rgba(0,0,0,.02)}}
 .brand{{display:flex;align-items:center;gap:10px;margin:0 0 22px}}
 .mark{{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;
 background:linear-gradient(135deg,#007cf0,#7928ca 52%,#ff0080);color:#fff;
 font-weight:600;font-size:17px;letter-spacing:-.03em;flex:0 0 auto}}
 .brand b{{font-size:14px;font-weight:600;letter-spacing:-.01em;line-height:1.1;display:block}}
 .brand span{{font-family:"Geist Mono Variable",ui-monospace,SFMono-Regular,Menlo,monospace;
 font-size:10.5px;letter-spacing:.04em;color:#888}}
 h1{{font-size:20px;font-weight:600;letter-spacing:-.02em;margin:0 0 4px}}
 .sub{{color:#4d4d4d;font-size:13px;margin:0 0 18px}}
 .err{{color:#dc2626;font-size:13px;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;
 padding:8px 11px;margin:0 0 14px}}
 label{{display:block;font-size:12px;font-weight:500;color:#404040;margin:14px 0 6px}}
 input{{width:100%;height:40px;padding:0 12px;border:1px solid #e5e5e5;border-radius:6px;font-size:14px;
 background:#fff;color:#171717;outline:none;transition:border-color .15s,box-shadow .15s}}
 input::placeholder{{color:#a3a3a3}}
 input:focus{{border-color:#0070f3;box-shadow:0 0 0 3px rgba(0,112,243,.16)}}
 button{{width:100%;height:40px;margin-top:22px;border:0;border-radius:6px;background:#171717;color:#fafafa;
 font-size:14px;font-weight:500;letter-spacing:-.01em;cursor:pointer;transition:background .15s,transform .1s}}
 button:hover{{background:#000}}button:active{{transform:scale(.99)}}
 .foot{{margin:18px 0 0;font-size:12px;color:#8a8a8a;text-align:center}}
</style></head><body><form class="card" method="post" action="{action}">
<div class="brand"><div class="mark">◆</div><div><b>AgentCore</b><span>CODING AGENTS</span></div></div>
<h1>Sign in</h1><p class="sub">Sign in with your email and password to open the console.</p>{err}
<label>{id_label}</label><input name="username" {id_type}value="{id_value}" autocomplete="{id_auto}"{id_ph}{' autofocus' if not id_value else ''}>
<label>Password</label><input name="password" type="password" autocomplete="current-password"{' autofocus' if id_value else ''}>
<button type="submit">Sign in</button>
<p class="foot">Credentials are in your event's Outputs panel.</p></form></body></html>"""


def main() -> None:
    import uvicorn
    os.makedirs(os.path.join(_REPO, ".runs", "stage1"), exist_ok=True)
    print(f"Coding-agents console backend (FastAPI/uvicorn) on http://localhost:{PORT}")
    print(f"  auth: {'ENABLED (cookie login)' if AUTH_ENABLED else 'disabled (open)'}")
    # Backstop: bound graceful shutdown so a long-lived SSE stream (PTY follow,
    # an in-flight chat/build turn) can NEVER block the server from stopping or
    # restarting. Without this, an open stream makes uvicorn wait indefinitely
    # ("Waiting for connections to close"). The PTY route also detects client
    # disconnect, so this only bites a genuinely stuck stream.
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning",
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
