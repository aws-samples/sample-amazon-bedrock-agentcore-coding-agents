"""Harness-driven builders: the deterministic local stand-in for the agents' work.

The workshop's claim is that three agent harnesses each BUILD a different artifact, and that
editing a harness changes what comes out. Locally no LLM is invoked, but the build is still
driven by the same harness files an attendee edits: pure-Python builders that read each role's
steering file and generate the role's artifact from it.

Two seams, the two things attendees can change:

  * The module seam (backend). ``build_mcp_server`` generates a runnable MCP server whose tool
    surface comes from ``cost_analyzer.TOOL_SPECS`` and whose handlers call
    ``cost_analyzer.dispatch``. Add an instance type to the module and the generated server
    prices it; the server imports the module rather than copying it.
  * The steering seam (frontend). ``build_chatbot`` reads the ``harness:ui`` block in the
    frontend ``AGENTS.md`` and generates ``chatbot.html`` from it: the title, the surfaced tool,
    the input field, and the example chips all come from that file. Edit the steering file
    and the produced UI changes on the next run.

The backend ``CLAUDE.md`` carries a ``harness:build`` block (server name/version, which tools
to expose) and the Kiro steering carries a ``harness:gate`` block (which checks gate the run).
Both are parsed here so the harness, not a hard-coded constant, decides the deliverable.

Everything is generated into a per-run work directory, so two concurrent runs never clobber
each other and the generated files land on disk for the compose step to commit.

On AgentCore the same seams hold: the agent (an LLM in a Runtime microVM) writes these files;
here deterministic code writes them from the same steering. The artifact shape, the wire
contract, and the harness files stay the same; only the author differs.
"""

from __future__ import annotations

import os
import re
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_HARNESS = os.path.join(_HERE, "harness")

# Where each role's steering file lives, in its own real format. The validator is
# a second Claude Code steered by an acceptance-contract CLAUDE.md (it replaced
# Kiro's .kiro/steering); the kiro entry stays for the restorable legacy path.
HARNESS_FILES = {
    "claude-code": os.path.join(_HARNESS, "claude-code", "CLAUDE.md"),
    "claude-code-validator": os.path.join(_HARNESS, "claude-code-validator", "CLAUDE.md"),
    "opencode": os.path.join(_HARNESS, "opencode", "AGENTS.md"),
    "kiro": os.path.join(_HARNESS, "kiro", ".kiro", "steering", "validator.md"),
}

# Per-agent steering file location relative to a harness root (same layout for
# every usecase: the default files above sit at the root, per-usecase variants
# live under harness/<usecase>/).
_STEERING_REL = {
    "claude-code": os.path.join("claude-code", "CLAUDE.md"),
    "claude-code-validator": os.path.join("claude-code-validator", "CLAUDE.md"),
    "opencode": os.path.join("opencode", "AGENTS.md"),
    "kiro": os.path.join("kiro", ".kiro", "steering", "validator.md"),
}


def harness_file(agent: str, usecase: str = "sample-to-mcp") -> str:
    """Resolve an agent's steering file for a usecase (the router picks the usecase)."""
    if usecase == "sample-to-mcp":
        return HARNESS_FILES[agent]
    return os.path.join(_HARNESS, usecase, _STEERING_REL[agent])


# --------------------------------------------------------------------------- spec parsing
def _fenced_block(text: str, tag: str) -> str:
    """Return the body of a ```<tag> ... ``` fenced block, or "" if absent."""
    m = re.search(r"```" + re.escape(tag) + r"\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else ""


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def parse_build_spec(claude_md_path: str | None = None) -> dict[str, Any]:
    """Parse the backend ``harness:build`` block from the Claude Code CLAUDE.md.

    Returns ``server_name``, ``server_version``, and ``expose`` (the literal string
    "all" or a list of tool names). Missing keys fall back to safe defaults so a
    half-edited harness still produces a runnable server.
    """
    path = claude_md_path or HARNESS_FILES["claude-code"]
    body = _fenced_block(_read(path), "harness:build")
    spec: dict[str, Any] = {"server_name": "cost-analyzer-mcp",
                            "server_version": "1.0.0", "expose": "all"}
    for line in body.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "expose" and val != "all":
            spec["expose"] = [t.strip() for t in val.split(",") if t.strip()]
        elif key in ("server_name", "server_version", "expose"):
            spec[key] = val
    return spec


def parse_setup_spec(steering_path: str) -> dict[str, Any]:
    """Parse the OPTIONAL ``harness:setup`` block from any role's steering file.

    The named blocks (``harness:build`` / ``harness:ui`` / ``harness:gate``) are
    the defaults the workshop ships, but a harness is the attendee's to extend.
    Anything listed here is set up in the role's container before it works, the
    way a developer extends their own harness with MCP servers, extra skills, or
    install steps.

        ```harness:setup
        mcp:
          - name: github
            url: https://<gateway-id>.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp
        skills:
          - skills/my-team-skill
        install:
          - pip install --quiet rich
        ```

    Returns ``{"mcp": [{name,url}], "skills": [paths], "install": [commands]}``,
    all empty when the block is absent (the defaults need no setup).
    """
    body = _fenced_block(_read(steering_path), "harness:setup")
    spec: dict[str, Any] = {"mcp": [], "skills": [], "install": []}
    section = None
    pending_mcp: dict[str, str] | None = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped in ("mcp:", "skills:", "install:"):
            if pending_mcp:
                spec["mcp"].append(pending_mcp)
                pending_mcp = None
            section = stripped[:-1]
            continue
        if section == "mcp":
            if stripped.startswith("- "):
                if pending_mcp:
                    spec["mcp"].append(pending_mcp)
                pending_mcp = {}
                stripped = stripped[2:].strip()
            if ":" in stripped and pending_mcp is not None:
                k, _, v = stripped.partition(":")
                pending_mcp[k.strip()] = v.strip()
        elif section in ("skills", "install") and stripped.startswith("- "):
            spec[section].append(stripped[2:].strip())
    if pending_mcp:
        spec["mcp"].append(pending_mcp)
    return spec


def parse_ui_spec(agents_md_path: str | None = None) -> dict[str, Any]:
    """Parse the frontend ``harness:ui`` block from the frontend AGENTS.md.

    Returns ``title``, ``tool``, ``input_label``, ``input_field``, and ``examples``
    (a list). This is the steering seam: the generated chatbot is built from exactly
    these values, so editing the file changes the UI.
    """
    path = agents_md_path or HARNESS_FILES["opencode"]
    body = _fenced_block(_read(path), "harness:ui")
    spec: dict[str, Any] = {"title": "Cost Analyzer Chat",
                            "tool": "estimate_ec2_monthly_cost",
                            "input_label": "instance type, e.g. m5.large",
                            "input_field": "instance_type", "examples": []}
    in_examples = False
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.match(r"\s*-\s+", line) and in_examples:
            spec["examples"].append(re.sub(r"^\s*-\s+", "", line).strip())
            continue
        in_examples = False
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "examples":
            in_examples = True
            if val:  # inline list form: examples: a, b, c
                spec["examples"] = [t.strip() for t in val.split(",") if t.strip()]
        elif key in spec:
            spec[key] = val
    return spec


def parse_gate_spec(gate_md_path: str | None = None) -> dict[str, Any]:
    """Parse the validator ``harness:gate`` block from the validator steering file
    (the Claude Code validator's ``CLAUDE.md``; the legacy Kiro steering carried
    the same block).

    Returns ``contract`` (path), ``checks`` (list), ``max_iterations`` (int).
    """
    path = gate_md_path or HARNESS_FILES["claude-code-validator"]
    body = _fenced_block(_read(path), "harness:gate")
    spec: dict[str, Any] = {"contract": "usecase-sample-to-mcp/grading/",
                            "checks": [], "max_iterations": 2}
    in_checks = False
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.match(r"\s*-\s+", line) and in_checks:
            spec["checks"].append(re.sub(r"^\s*-\s+", "", line).strip())
            continue
        in_checks = False
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "checks":
            in_checks = True
        elif key == "max_iterations":
            try:
                spec["max_iterations"] = int(val)
            except ValueError:
                pass
        elif key == "contract":
            spec["contract"] = val
    return spec


# --------------------------------------------------------------------------- backend build
# The generated MCP server. It imports cost_analyzer at boot (sys.path is set to the usecase
# dir), so it reflects the current module (the module seam). The exposed tool set is filtered by
# the harness:build `expose` spec. Standard library only.
_SERVER_TEMPLATE = '''"""Generated MCP server: the BACKEND role's deliverable.

Built by the harness from {claude_md_rel} (harness:build) against the {module_name}
module. Not hand-written and not a copy: it imports {module_name} live and wraps the
tools the harness said to expose, over MCP's tools/list + tools/call wire shape.
"""
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, {usecase_dir!r})
import {module_name}  # noqa: E402

SERVER_INFO = {{"name": {server_name!r}, "version": {server_version!r}}}
EXPOSE = {expose!r}   # "all" or an explicit list of tool names
PROTOCOL_VERSION = "2025-03-26"


def _exposed_specs():
    specs = {module_name}.list_tools()
    if EXPOSE == "all":
        return specs
    keep = set(EXPOSE)
    return [s for s in specs if s.get("name") in keep]


def _result(i, r): return {{"jsonrpc": "2.0", "id": i, "result": r}}
def _error(i, c, m): return {{"jsonrpc": "2.0", "id": i, "error": {{"code": c, "message": m}}}}


def handle_rpc(payload):
    i = payload.get("id")
    method = payload.get("method", "")
    params = payload.get("params") or {{}}
    if method == "initialize":
        return _result(i, {{"protocolVersion": PROTOCOL_VERSION,
                           "serverInfo": SERVER_INFO, "capabilities": {{"tools": {{}}}}}})
    if method == "tools/list":
        return _result(i, {{"tools": _exposed_specs()}})
    if method == "tools/call":
        name = params.get("name", "")
        if EXPOSE != "all" and name not in set(EXPOSE):
            return _error(i, -32601, "tool not exposed: " + name)
        try:
            out = {module_name}.dispatch(name, params.get("arguments") or {{}})
        except KeyError as e:
            return _error(i, -32601, "unknown tool: " + str(e))
        except (ValueError, TypeError) as e:
            return _error(i, -32602, "invalid arguments for " + name + ": " + str(e))
        return _result(i, {{"content": [{{"type": "text", "text": json.dumps(out)}}],
                           "isError": False}})
    return _error(i, -32601, "method not found: " + method)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        self._send(200, {{"status": "ok", "server": SERVER_INFO["name"]}})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{{}}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return self._send(400, _error(None, -32700, "parse error"))
        self._send(200, handle_rpc(payload))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "9000")))
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("generated %s on http://%s:%d (%d tools)" % (
        SERVER_INFO["name"], a.host, a.port, len(_exposed_specs())))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
'''


def build_mcp_server(workdir: str, usecase_dir: str,
                     claude_md_path: str | None = None,
                     module_name: str = "cost_analyzer") -> str:
    """Generate the backend MCP server into ``workdir`` from the CLAUDE.md build spec.

    Returns the path to the generated ``mcp_server.py``. The server imports
    ``module_name`` from ``usecase_dir`` live, so it reflects the current module:
    the router decides which usecase (and therefore which module) a run builds.
    """
    path = claude_md_path or HARNESS_FILES["claude-code"]
    spec = parse_build_spec(path)
    src = _SERVER_TEMPLATE.format(
        claude_md_rel=os.path.relpath(path, _HERE),
        usecase_dir=usecase_dir,
        module_name=module_name,
        server_name=spec["server_name"],
        server_version=spec["server_version"],
        expose=spec["expose"],
    )
    os.makedirs(workdir, exist_ok=True)
    out = os.path.join(workdir, "mcp_server.py")
    with open(out, "w", encoding="utf-8") as f:
        f.write(src)
    return out


# --------------------------------------------------------------------------- frontend build
def build_chatbot(workdir: str, endpoint: str,
                  agents_md_path: str | None = None) -> str:
    """Generate ``chatbot.html`` into ``workdir`` from the frontend AGENTS.md UI spec.

    The title, surfaced tool, input field/label, and example chips all come from the
    ``harness:ui`` block, so editing that steering file changes the produced UI. The
    page is thin by construction: it POSTs a tools/call to ``endpoint`` and renders the
    result; it holds no pricing logic.
    """
    spec = parse_ui_spec(agents_md_path)
    title = spec["title"]
    tool = spec["tool"]
    field = spec["input_field"]
    label = spec["input_label"]
    chips = "".join(
        '<button type="button" class="chip" onclick="prefill(this.textContent)">'
        f'{ex}</button>' for ex in spec["examples"]
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:ui-sans-serif,system-ui;max-width:640px;margin:40px auto">
<h2>{title}</h2>
<p>Asks the deployed MCP server (<code>{endpoint}</code>) via <code>{tool}</code>.</p>
<div class="chips">{chips}</div>
<input id="q" style="width:70%" placeholder="{label}"/>
<button onclick="ask()">Estimate</button><pre id="out"></pre>
<script>
function prefill(v){{ document.getElementById("q").value = v; }}
async function ask(){{
  const body={{jsonrpc:"2.0",method:"tools/call",id:1,
    params:{{name:"{tool}",arguments:{{"{field}":document.getElementById("q").value}}}}}};
  const r=await fetch("{endpoint}",{{method:"POST",
    headers:{{"Content-Type":"application/json"}},body:JSON.stringify(body)}});
  const j=await r.json();
  document.getElementById("out").textContent=JSON.stringify(j.result??j.error,null,2);
}}
</script></body></html>
"""
    os.makedirs(workdir, exist_ok=True)
    out = os.path.join(workdir, "chatbot.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


# --------------------------------------------------------------- project docs
# The deliverable is a runnable mini-project, not two loose files: the backend
# role also authors a README (how to run it) and a smoke test (proof it runs).
# On the shipped path the agent writes these (its dispatch prompt requires them);
# here the deterministic builders are the offline stand-in, exactly like
# build_mcp_server / build_chatbot. The SHAPE is pinned (a required-output set the
# validator can check); the CONTENT is authored, never a hardcoded answer.

# The smoke test is a self-contained, dependency-free runnable: it boots the
# co-located mcp_server.py on an ephemeral port, then exercises the live
# tools/list + tools/call wire contract and asserts the server actually answers.
# It locates the usecase module the same portable way the server does
# (COST_ANALYZER_DIR, else a repo-relative search), so it runs from a fresh clone
# of the attendee's template-derived repo with no arguments.
_SMOKE_TEMPLATE = '''#!/usr/bin/env python3
"""Smoke test for the generated MCP server: boot it, call it, prove it answers.

Run it straight from a clone of this repo:

    python deliverable/smoke_test.py

It starts ``mcp_server.py`` (next to this file) on a free loopback port, sends a
``tools/list`` and one ``tools/call``, and exits non-zero if the live server does
not answer the MCP wire contract. Standard library only; no network, no AWS.
"""
import json, os, subprocess, sys, time, urllib.request
from http.client import RemoteDisconnected

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")


def _find_module_dir():
    # The server imports {module_name}; help it resolve from a fresh clone by
    # pointing COST_ANALYZER_DIR at the usecase dir if we can find it. When the
    # env is already set (the workshop host), respect it.
    if os.environ.get("COST_ANALYZER_DIR"):
        return os.environ["COST_ANALYZER_DIR"]
    here = HERE
    for _ in range(6):  # walk up to the repo root looking for the usecase package
        cand = os.path.join(here, {usecase_subdir!r})
        if os.path.isfile(os.path.join(cand, {module_name!r} + ".py")):
            return cand
        here = os.path.dirname(here)
    return HERE  # fall back to the deliverable dir (server may hardcode its path)


def _rpc(url, method, params):
    body = json.dumps({{"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={{"Content-Type": "application/json"}})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def main():
    port = 0
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    url = "http://127.0.0.1:%d" % port
    env = dict(os.environ, COST_ANALYZER_DIR=_find_module_dir())
    proc = subprocess.Popen([sys.executable, SERVER, "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(40):  # wait for the server to bind
            try:
                _rpc(url, "tools/list", {{}}); break
            except (urllib.error.URLError, RemoteDisconnected, ConnectionError):
                if proc.poll() is not None:
                    sys.stderr.write(proc.stderr.read().decode()); return 1
                time.sleep(0.25)
        else:
            sys.stderr.write("server never became ready\\n"); return 1
        tools = _rpc(url, "tools/list", {{}})["result"]["tools"]
        assert tools, "tools/list returned no tools"
        names = [t["name"] for t in tools]
        print("tools/list ok: %d tools (%s)" % (len(names), ", ".join(names)))
        call = _rpc(url, "tools/call",
                    {{"name": names[0], "arguments": {sample_args!r}}})
        assert "result" in call or "error" in call, "tools/call returned neither result nor error"
        print("tools/call ok: %s -> %s"
              % (names[0], json.dumps(call.get("result", call.get("error")))[:120]))
        print("SMOKE OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
'''


def build_smoke_test(workdir: str, usecase_subdir: str = "usecase-sample-to-mcp",
                     module_name: str = "cost_analyzer",
                     sample_args: dict | None = None) -> str:
    """Generate ``smoke_test.py`` (the offline stand-in for the agent's own).

    The test is a runnable proof: it boots the co-located ``mcp_server.py`` and
    exercises the live MCP contract. ``sample_args`` seeds the one tools/call; a
    safe default works for the cost-analyzer's first tool. Returns the path.
    """
    if sample_args is None:
        sample_args = {"instance_type": "m5.large", "count": 2}
    os.makedirs(workdir, exist_ok=True)
    out = os.path.join(workdir, "smoke_test.py")
    with open(out, "w", encoding="utf-8") as f:
        f.write(_SMOKE_TEMPLATE.format(usecase_subdir=usecase_subdir,
                                       module_name=module_name,
                                       sample_args=sample_args))
    return out


def build_readme(workdir: str, run_id: str, task: str, workflow_ref: str,
                 module_name: str, roles: list[str], has_frontend: bool,
                 gate_line: str = "") -> str:
    """Generate ``README.md`` (the offline stand-in for the agent's own).

    Describes the composed deliverable and how to run it. All values come from the
    run's own facts (never a hardcoded answer); the shape is what the validator
    checks. Returns the path.
    """
    files = ["- `mcp_server.py` - the MCP server wrapping the "
             f"`{module_name}` module (stdlib only, JSON-RPC over HTTP).",
             "- `smoke_test.py` - boots the server and proves the live "
             "tools/list + tools/call contract answers.",
             "- `gate_report.json` / `critique.md` - the acceptance gate result "
             "and the reviewer's verdict."]
    if has_frontend:
        files.insert(1, "- `chatbot.html` - a thin UI that calls the server; "
                     "no pricing logic of its own.")
    body = [
        f"# {module_name} MCP deliverable",
        "",
        f"Generated by the coding-agent team for run `{run_id}` "
        f"(workflow `{workflow_ref}`, roles: {', '.join(roles)}).",
        "",
        "## Task",
        "",
        f"> {task}",
        "",
        "## What is here",
        "",
        *files,
        "",
        "## Run it from a clone",
        "",
        "```bash",
        "python deliverable/smoke_test.py",
        "```",
        "",
        "The smoke test starts the server on a free loopback port, calls it over "
        "the MCP wire contract, and prints `SMOKE OK` on success. It finds the "
        f"`{module_name}` module from this repo automatically, or set "
        "`COST_ANALYZER_DIR` to its directory.",
        "",
    ]
    if gate_line:
        body += ["## Acceptance gate", "", f"```\n{gate_line}\n```", ""]
    os.makedirs(workdir, exist_ok=True)
    out = os.path.join(workdir, "README.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    return out
