"""Exercise the installer's real curl retry after a reset during a VSIX download."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "coding-agents/install_claude_code_extension.py"
spec = importlib.util.spec_from_file_location("reset_extension", SOURCE)
extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extension)
REAL_RUN = subprocess.run


def test_reset_download_recovers_before_checksum_and_install(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("extension/package.json", json.dumps(
            {"publisher": "Anthropic", "name": "claude-code", "version": "2.1.304"}))
    payload = buffer.getvalue()
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            attempts.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if len(attempts) == 1:
                self.wfile.write(payload[:32])
                self.wfile.flush()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                           struct.pack("ii", 1, 0))
                self.connection.close()
                self.close_connection = True
                return
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}/extension.vsix"
    artifact = {"url": extension.extension_spec()["archives"]["linux-aarch64"]["url"],
                "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    pinned = dict(extension.extension_spec(), archives={"linux-aarch64": artifact})
    monkeypatch.setattr(extension, "extension_spec", lambda: pinned)
    monkeypatch.setattr(extension.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(extension.platform, "system", lambda: "Linux")
    installed = []

    def run(args, **kwargs):
        if args[0] == "curl":
            args = list(args)
            args[args.index(artifact["url"])] = url
            return REAL_RUN(args, **kwargs)
        if "--install-extension" in args:
            package = Path(args[args.index("--install-extension") + 1])
            installed.append(package.read_bytes())
            return subprocess.CompletedProcess(args, 0)
        if "--list-extensions" in args:
            return subprocess.CompletedProcess(args, 0, "anthropic.claude-code@2.1.304\n")
        pytest.fail(f"Unexpected installer command: {args[0]}")

    monkeypatch.setattr(extension.subprocess, "run", run)
    try:
        result = extension.install(home=tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert len(attempts) == 2
    assert installed == [payload]
    assert result["vsix_sha256"] == artifact["sha256"]
