"""Controlled collector process and real loopback HTTP for entrypoint tests."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading


@contextmanager
def collector_environment(tmp_path):
    folder = tmp_path / "collector"
    folder.mkdir()
    tools = folder / "bin"
    tools.mkdir()
    ready = folder / "ready"
    stopped = folder / "stop"
    probes = folder / "probes.jsonl"
    stop = threading.Event()
    state = {"health": "ready", "connected": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["connected"].set()
            if state["health"] == "stalled":
                stop.wait(10)
                return
            self.send_response(200 if ready.exists() and state["health"] == "ready" else 503)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    collector = tools / "otelcol"
    collector.write_text(f"#!{sys.executable}\n" + """
import os, pathlib, sys, time
if os.environ.get("TEST_COLLECTOR_MODE") == "exit":
    raise SystemExit(42)
pathlib.Path(os.environ["TEST_COLLECTOR_READY"]).write_text(str(os.getpid()))
while not pathlib.Path(os.environ["TEST_COLLECTOR_STOP"]).exists():
    time.sleep(0.05)
""")
    collector.chmod(0o700)
    # Preserve real curl's options and timeout behavior. Only relocate the
    # container's fixed health port so tests never use another local collector.
    real_curl = shutil.which("curl")
    if not real_curl:
        raise RuntimeError("entrypoint tests require curl")
    curl = tools / "curl"
    curl.write_text(f"#!{sys.executable}\n" + f"""
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path({str(probes)!r}).open("a") as out:
    out.write(json.dumps(args) + "\\n")
args = [{endpoint!r} if arg == "http://127.0.0.1:13133" else arg for arg in args]
os.execv({real_curl!r}, [{real_curl!r}, *args])
""")
    curl.chmod(0o700)
    config = folder / "config.yaml"
    config.write_text("receivers: {}\n")
    helper = Path(__file__).resolve().parents[1] / "coding-agents/cli_versions.py"
    manifest = json.loads(helper.with_name("cli-versions.json").read_text())
    cli_home = folder / "cli-home"
    cli_bin = folder / "cli-bin"
    cli_bin.mkdir()
    state["cli_binaries"] = {}
    for name in ("claude-code", "codex", "kiro"):
        item = manifest["clis"][name]
        for command in [item["command"], *item.get("siblings", [])]:
            destination = cli_home / ".local/bin" if name == "kiro" else cli_bin
            destination.mkdir(parents=True, exist_ok=True)
            binary = destination / command
            binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{command} {item['version']}'\n")
            binary.chmod(0o700)
            state["cli_binaries"][command] = binary
    # Relocate only the image's fixed helper path; execute the real verifier and
    # its actual version-probe subprocesses. Other Python calls remain unchanged.
    python = tools / "python3"
    python.write_text(f"#!{sys.executable}\n" + f"""
import os, sys
args = sys.argv[1:]
if args and args[0] == "/opt/workshop-cli/cli_versions.py":
    args[0] = {str(helper)!r}
    args[args.index("--home") + 1] = {str(cli_home)!r}
    os.environ["PATH"] = {str(cli_bin)!r} + os.pathsep + os.environ["PATH"]
os.execv({sys.executable!r}, [{sys.executable!r}, *args])
""")
    python.chmod(0o700)
    env = {
        "OTELCOL_BIN": str(collector),
        "OTELCOL_CONFIG": str(config),
        "OTELCOL_LOG": str(folder / "collector.log"),
        "OTELCOL_STARTUP_TIMEOUT_S": "5",
        "TEST_COLLECTOR_READY": str(ready),
        "TEST_COLLECTOR_STOP": str(stopped),
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
    }
    try:
        yield env, state, probes
    finally:
        stopped.touch()
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def run_entrypoint(path, env, args, *, cwd=None, timeout=10):
    """Reap the collector as well as CMD, including when CMD has already exited."""
    stopped = Path(env["TEST_COLLECTOR_STOP"])
    stopped.unlink(missing_ok=True)
    process = subprocess.Popen(
        ["bash", str(path), *args], env=env, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    finally:
        # This also releases an already reparented collector on platforms where
        # signalling a process group after its leader exited returns EPERM.
        stopped.touch()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=3)
