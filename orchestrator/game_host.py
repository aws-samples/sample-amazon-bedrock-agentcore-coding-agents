#!/usr/bin/python3 -I
"""Publish one copied workshop game behind its separate public origin.

Installed, hash-verified code: /usr/local/bin/workshop-game-host

    sudo workshop-game-host status
    sudo workshop-game-host publish --project /home/ubuntu/game --port 8000 -- npm start
    sudo workshop-game-host unpublish

``install --region REGION`` is an operator action used by the narrow CFN SSM
document. It reads /workshop/game-host and retrieves the origin header directly
from Secrets Manager. Neither credentials nor that header appear in stdout.

The project is never executed by root. A bounded, read-only systemd job copies
source and prepared dependencies as the attendee; SQLite uses its backup API.
The game then runs as a DynamicUser with only loopback networking. A separate
socket-proxyd inherits a host loopback listener and joins the game's network.
Old archives, working copies, and copied score data survive replacement and
unpublish. This helper does not build dependencies or infer a start command.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import signal
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlsplit
import uuid


SCHEMA_VERSION = 1
HELPER = Path("/usr/local/bin/workshop-game-host")
CONFIG_DIR = Path("/etc/workshop-game-host")
ROOT = Path("/var/lib/workshop-game-host")
PRIVATE_STATE = Path("/var/lib/private")
STATE_LINK_BASE = Path("/var/lib")
UNITS = Path("/etc/systemd/system")
NGINX = Path("/etc/nginx/conf.d/workshop-game.conf")
GAME = "workshop-game.service"
PROXY = "workshop-game-proxy.service"
SOCKET = "workshop-game-proxy.socket"
SOURCE_PARAMETER = "/workshop/game-host"
PORTS = (8000, 8001)
HOST_PORT = 8083
ORIGIN_PORT = 8082
ORIGIN_HEADER = "X-Workshop-Game-Origin"
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 100_000
COPY_SECONDS = 45
READY_SECONDS = 40
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
BASE_ENV = {
    "PATH": SAFE_PATH, "LANG": "C.UTF-8", "HOME": "/root",
    "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
}
SNAPSHOT_RE = re.compile(r"^[0-9a-f]{32}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9]{48}$")
EXCLUDED_NAMES = {
    ".git", ".hg", ".svn", ".aws", ".ssh", ".gnupg", ".docker", ".kube",
    ".claude", ".codex", ".kiro", ".runs", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", ".bash_history", ".zsh_history", "__pycache__",
    "credentials", "credentials.json", "secrets.json", "secrets.yaml",
    "secrets.yml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
# /run includes the system bus, Docker and credential-agent sockets. /mnt hides
# the shared workshop mount. ProtectSystem alone would still allow host reads.
HIDDEN_PATHS = (
    "/run", "/mnt", "/media", "/var/lib/cloud", "/var/lib/docker",
    "/var/lib/amazon", "/var/log", "/etc/nginx", "/etc/ssh",
    "/etc/systemd/system", "/etc/stage2-console.env",
    "/etc/workshop-game-host/config.json", "/var/lib/workshop-game-host",
)
_deadline: float | None = None
_saved_signal_handlers: dict[int, Any] = {}


class HostError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_json(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def set_deadline(seconds: float) -> None:
    """The client waits 180s: promotion 110s + rollback 35s + cleanup 15s."""
    global _deadline
    _deadline = time.monotonic() + seconds
    if not _saved_signal_handlers:
        for number in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
            _saved_signal_handlers[number] = signal.getsignal(number)
    signal.signal(signal.SIGALRM, _expired)
    signal.signal(signal.SIGTERM, _cancelled)
    signal.signal(signal.SIGINT, _cancelled)
    signal.setitimer(signal.ITIMER_REAL, seconds)


def _expired(_signum: int, _frame: Any) -> None:
    raise HostError("OPERATION_TIMEOUT", "The bounded hosting operation exceeded its deadline.")


def _cancelled(_signum: int, _frame: Any) -> None:
    raise HostError("OPERATION_CANCELLED", "The hosting operation was cancelled; owned cleanup is being applied.")


def clear_deadline() -> None:
    global _deadline
    signal.setitimer(signal.ITIMER_REAL, 0)
    _deadline = None
    for number, handler in _saved_signal_handlers.items():
        signal.signal(number, handler)
    _saved_signal_handlers.clear()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n").encode()


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace only an owned regular file in a previously secured directory."""
    if path.is_symlink():
        raise HostError("UNSAFE_MANAGED_PATH", "A managed file is a symlink.")
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise HostError("UNSAFE_MANAGED_PATH", "A managed path is not a regular file.")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def secure_directory(path: Path, mode: int = 0o700) -> None:
    """Never chown or follow a preexisting, attacker-controlled directory."""
    if path.is_symlink():
        raise HostError("UNSAFE_MANAGED_PATH", "A managed directory is a symlink.")
    path.mkdir(mode=mode, parents=False, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise HostError("UNSAFE_MANAGED_PATH", "A managed directory has unsafe ownership or permissions.")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise HostError("INVALID_STATE", "A managed JSON file is invalid.")
    try:
        return json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise HostError("INVALID_STATE", "A managed JSON file is unreadable.") from exc


def run(arguments: list[str], *, timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess:
    if _deadline is not None:
        timeout = min(timeout, _deadline - time.monotonic())
        if timeout <= 0:
            raise HostError("OPERATION_TIMEOUT", "The bounded hosting operation exceeded its deadline.")
    try:
        result = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=BASE_ENV, cwd="/", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HostError("COMMAND_TIMEOUT", f"{Path(arguments[0]).name} exceeded its deadline.") from exc
    except OSError as exc:
        raise HostError("COMMAND_UNAVAILABLE", f"{Path(arguments[0]).name} could not be started.") from exc
    if check and result.returncode:
        # Child output can contain nginx's secret header or app-supplied data.
        raise HostError("COMMAND_FAILED", f"{Path(arguments[0]).name} failed.", exit_code=result.returncode)
    return result


def excluded(path: PurePosixPath) -> bool:
    return any(
        part.lower() in EXCLUDED_NAMES
        or part.lower() == ".env" or part.lower().startswith(".env.")
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        for part in path.parts
    )


def safe_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name or path.is_absolute() or ".." in path.parts
        or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
        or len(name.encode()) > 4096
    ):
        raise HostError("UNSAFE_PROJECT_PATH", "The project contains an unsafe path.")
    return path


def validate_project(project: str, home: Path) -> Path:
    path = Path(project)
    if not path.is_absolute() or any(c in project for c in "\n\r\x00$%:"):
        raise HostError("INVALID_PROJECT", "Use an absolute project path without control characters, $, %, or :.")
    try:
        real = path.resolve(strict=True)
        relative = real.relative_to(home.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise HostError("INVALID_PROJECT", "Select a project directory inside the configured attendee's home.") from exc
    if not relative.parts or excluded(PurePosixPath(relative.as_posix())) or not real.is_dir():
        raise HostError("INVALID_PROJECT", "Select the game directory, not the home or a private configuration directory.")
    return real


def validate_command(command: list[str]) -> list[str]:
    if not isinstance(command, list) or not command or len(command) > 128:
        raise HostError("INVALID_COMMAND", "Supply the README start command after --.")
    if any(not isinstance(x, str) or "\x00" in x or "\n" in x or "\r" in x for x in command):
        raise HostError("INVALID_COMMAND", "Command arguments cannot contain NUL or line breaks.")
    if sum(len(x.encode()) for x in command) > 16384:
        raise HostError("INVALID_COMMAND", "The supplied command is too large.")
    executable = command[0]
    if not executable or executable.startswith("-") or Path(executable).is_absolute():
        raise HostError("INVALID_COMMAND", "Use an executable on the system PATH or a project-relative executable.")
    if "/" in executable:
        safe_relative(executable)
    # No shell joining or interpolation: even quotes, $, %, and spaces in later
    # arguments remain literal argv entries in the root-owned launch manifest.
    return list(command)


def system_python_link(target: Path) -> bool:
    """A prepared venv may link to the host's root-owned Python interpreter."""
    if not re.fullmatch(r"/usr/(?:local/)?bin/python3(?:\.[0-9]+)?", str(target)):
        return False
    try:
        real = target.resolve(strict=True)
        return all(p.stat().st_uid == 0 and not p.stat().st_mode & 0o022
                   for p in (real, *real.parents))
    except OSError:
        return False


def safe_link(project: Path, relative: PurePosixPath, link: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in link) or "\\" in link:
        raise HostError("UNSAFE_SYMLINK", "The project contains an unsafe symlink.")
    source = project / relative
    try:
        target = (source.parent / link).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HostError("UNSAFE_SYMLINK", "A project symlink is broken or cyclic.") from exc
    if target.is_relative_to(project):
        target_relative = PurePosixPath(target.relative_to(project).as_posix())
        if excluded(target_relative):
            raise HostError("UNSAFE_SYMLINK", "A project symlink points to excluded private data.")
        return os.path.relpath(project / target_relative, source.parent)
    if system_python_link(target):
        return str(target)
    raise HostError("UNSAFE_SYMLINK", "A project symlink leaves the selected project.")


def sqlite_backup(source: Path, destination: Path, *, deadline: float) -> None:
    """Executed under ProtectSystem=strict: even WAL/SHM cannot be written."""
    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() >= deadline:
            raise HostError("SNAPSHOT_TIMEOUT", "The database could not be backed up within the snapshot deadline.")

    try:
        with contextlib.closing(sqlite3.connect(
            f"file:{quote(str(source), safe='/')}?mode=ro", uri=True, timeout=2,
        )) as reader, contextlib.closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer, pages=128, progress=progress, sleep=0.05)
    except sqlite3.Error as exc:
        raise HostError(
            "DATABASE_BACKUP_FAILED",
            "SQLite could not take a read-only online backup. The original database was not modified.",
        ) from exc


def pack_project(project: Path, output: Any, *, deadline: float | None = None) -> dict:
    """Stream a bounded source archive; never follow source directory symlinks."""
    deadline = deadline if deadline is not None else time.monotonic() + COPY_SECONDS
    files: list[dict] = []
    entries: list[tuple[Path, PurePosixPath, os.stat_result]] = []
    sqlite_paths: set[str] = set()
    total = 0
    for directory, dirs, names in os.walk(project, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(d for d in dirs if not excluded(PurePosixPath((base / d).relative_to(project).as_posix())))
        for name in sorted(dirs + names):
            path = base / name
            relative = safe_relative(path.relative_to(project).as_posix())
            if str(relative) == ".workshop-game-snapshot.json":
                raise HostError("RESERVED_PROJECT_FILE", "The selected directory already contains a game-host snapshot manifest.")
            if excluded(relative):
                continue
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise HostError("SPECIAL_PROJECT_FILE", "Remove sockets, devices, or FIFOs from the selected project.")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise HostError("HARDLINKED_PROJECT_FILE", "Copy hardlinked dependencies into the project before publishing.")
                if info.st_size > MAX_FILE_BYTES:
                    raise HostError("SNAPSHOT_TOO_LARGE", "A project file exceeds the publication limit.")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if os.read(fd, 16) == b"SQLite format 3\x00":
                        sqlite_paths.add(str(relative))
                finally:
                    os.close(fd)
            entries.append((path, relative, info))
            if len(entries) > MAX_FILES or time.monotonic() >= deadline:
                raise HostError("SNAPSHOT_LIMIT", "The project exceeds the bounded snapshot allowance.")

    with tempfile.TemporaryDirectory(prefix="workshop-game-db-") as temporary, tarfile.open(
        fileobj=output, mode="w|", format=tarfile.PAX_FORMAT,
    ) as archive:
        for path, relative, original in entries:
            if any(str(relative) == db + suffix for db in sqlite_paths for suffix in ("-wal", "-shm", "-journal")):
                continue  # The online backup already contains the committed state.
            if time.monotonic() >= deadline:
                raise HostError("SNAPSHOT_TIMEOUT", "The project snapshot exceeded its deadline.")
            item = tarfile.TarInfo(str(relative))
            item.mtime = int(original.st_mtime)
            item.uid = item.gid = 0
            item.uname = item.gname = ""
            if stat.S_ISLNK(original.st_mode):
                item.type = tarfile.SYMTYPE
                item.linkname = safe_link(project, relative, os.readlink(path))
                item.mode = 0o777
                archive.addfile(item)
                files.append({"path": str(relative), "symlink": item.linkname})
                continue
            if stat.S_ISDIR(original.st_mode):
                item.type, item.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(item)
                continue
            backup = str(relative) in sqlite_paths
            actual = path
            if backup:
                actual = Path(temporary) / f"{len(files)}.sqlite"
                sqlite_backup(path, actual, deadline=deadline)
            fd = os.open(actual, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as reader:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise HostError("PROJECT_CHANGED", "A project file changed during snapshotting.")
                if not backup and (before.st_dev, before.st_ino) != (original.st_dev, original.st_ino):
                    raise HostError("PROJECT_CHANGED", "A project file was replaced during snapshotting.")
                total += before.st_size
                if total > MAX_TOTAL_BYTES or before.st_size > MAX_FILE_BYTES:
                    raise HostError("SNAPSHOT_TOO_LARGE", "The project exceeds the publication size limit.")
                item.size = before.st_size
                item.mode = 0o755 if original.st_mode & 0o111 else 0o644
                archive.addfile(item, reader)
                after = os.fstat(reader.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise HostError("PROJECT_CHANGED", "A project file changed during snapshotting; try again.")
                files.append({"path": str(relative), "bytes": item.size, "sqlite_backup": backup})
        manifest = {"schema_version": SCHEMA_VERSION, "files": files, "bytes": total}
        data = json_bytes(manifest)
        item = tarfile.TarInfo(".workshop-game-snapshot.json")
        item.size, item.mode = len(data), 0o644
        archive.addfile(item, io.BytesIO(data))
    return manifest


def unpack_project(archive_path: Path, destination: Path) -> dict:
    """Extract our unprivileged producer's archive without tarfile.extract()."""
    links: list[tarfile.TarInfo] = []
    total, count = 0, 0
    with tarfile.open(archive_path, "r:") as archive:
        for item in archive:
            relative = safe_relative(item.name)
            if excluded(relative):
                raise HostError("UNSAFE_ARCHIVE", "The archive includes excluded private data.")
            target = destination / relative
            if any(p.is_symlink() for p in (target, *target.parents) if p != destination.parent):
                raise HostError("UNSAFE_ARCHIVE", "The archive traverses a symlink.")
            count += 1
            if count > MAX_FILES + 1 or item.size < 0 or item.size > MAX_FILE_BYTES:
                raise HostError("UNSAFE_ARCHIVE", "The archive exceeds its file allowance.")
            total += item.size
            if total > MAX_TOTAL_BYTES + 16 * 1024 * 1024:
                raise HostError("UNSAFE_ARCHIVE", "The archive exceeds its size allowance.")
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            elif item.issym():
                links.append(item)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with archive.extractfile(item) as source:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as writer:
                        shutil.copyfileobj(source, writer)
                    target.chmod(0o755 if item.mode & 0o111 else 0o644)
            else:
                raise HostError("UNSAFE_ARCHIVE", "The archive contains a special or hardlinked file.")
    for item in links:
        relative = safe_relative(item.name)
        target = destination / relative
        if target.exists() or target.is_symlink():
            raise HostError("UNSAFE_ARCHIVE", "The archive contains duplicate paths.")
        # Links were normalized by the producer. Recheck the final destination
        # and create links last, so none can redirect a privileged file write.
        candidate = (target.parent / item.linkname).resolve()
        if candidate.is_relative_to(destination):
            if excluded(PurePosixPath(candidate.relative_to(destination).as_posix())):
                raise HostError("UNSAFE_ARCHIVE", "An archive symlink targets private data.")
        elif not system_python_link(candidate):
            raise HostError("UNSAFE_ARCHIVE", "An archive symlink escapes the copied project.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.symlink_to(item.linkname)
    for item in links:
        # A producer cannot smuggle a dangling/cyclic link through a sequence of
        # individually in-bounds link targets.
        safe_link(destination, safe_relative(item.name), item.linkname)
    manifest = read_json(destination / ".workshop-game-snapshot.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise HostError("UNSAFE_ARCHIVE", "The snapshot manifest is missing.")
    return manifest


def sandbox_properties() -> dict[str, str]:
    return {
        "DynamicUser": "yes", "NoNewPrivileges": "yes",
        "ProtectHome": "yes", "ProtectSystem": "strict",
        "PrivateNetwork": "yes", "PrivateTmp": "yes", "PrivateDevices": "yes",
        "PrivateIPC": "yes", "ProtectProc": "invisible",
        "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes", "ProtectControlGroups": "yes",
        "RestrictNamespaces": "yes", "RestrictSUIDSGID": "yes",
        "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6",
        "SystemCallArchitectures": "native", "LockPersonality": "yes",
        "CapabilityBoundingSet": "", "AmbientCapabilities": "",
        "MemoryMax": "1G", "TasksMax": "128", "CPUQuota": "100%",
        "InaccessiblePaths": " ".join("-" + p for p in HIDDEN_PATHS),
        "TimeoutStartSec": "35", "TimeoutStopSec": "5",
        "KillMode": "control-group", "UMask": "0077",
    }


def render_units(snapshot_id: str, port: int, proxy_binary: str) -> dict[str, bytes]:
    if not SNAPSHOT_RE.fullmatch(snapshot_id) or port not in PORTS:
        raise HostError("INVALID_STATE", "The publication identity is invalid.")
    if proxy_binary not in ("/usr/lib/systemd/systemd-socket-proxyd", "/lib/systemd/systemd-socket-proxyd"):
        raise HostError("INVALID_STATE", "The socket proxy executable is invalid.")
    common = "\n".join(f"{key}={value}" for key, value in sandbox_properties().items())
    state = f"workshop-game-{snapshot_id}"
    return {
        GAME: f"""[Unit]
Description=Published workshop game copy

[Service]
Type=exec
{common}
StateDirectory={state}
StateDirectoryMode=0700
WorkingDirectory=/var/lib/{state}/app
ExecStart=/usr/bin/python3 -I {HELPER} _exec
""".encode(),
        PROXY: f"""[Unit]
Description=Socket bridge to the isolated workshop game
BindsTo={GAME}
After={GAME} {SOCKET}
Requires={SOCKET}
PartOf={GAME}
JoinsNamespaceOf={GAME}

[Service]
Type=exec
{common}
ExecStart={proxy_binary} 127.0.0.1:{port}
""".encode(),
        SOCKET: f"""[Unit]
Description=Host loopback ingress for the isolated workshop game
PartOf={GAME}

[Socket]
ListenStream=127.0.0.1:{HOST_PORT}
Accept=no
Service={PROXY}
""".encode(),
    }


def render_nginx(secret: str, enabled: bool) -> bytes:
    if not SECRET_RE.fullmatch(secret):
        raise HostError("INVALID_CONFIG", "The game origin secret has an invalid format.")
    route = f"""proxy_pass http://127.0.0.1:{HOST_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Authorization "";
        proxy_set_header Proxy-Authorization "";
        proxy_set_header {ORIGIN_HEADER} "";
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $workshop_game_connection;
        proxy_ignore_headers X-Accel-Redirect;
        proxy_buffering off;
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
        proxy_intercept_errors on;
        error_page 502 503 504 =503 @game_unavailable;""" if enabled else "return 503;"
    # Game cookies and Set-Cookie stay intact on this separate browser origin.
    # No aliases, filesystem roots, IDE locations, or viewer-selected upstreams.
    return f"""# Owned by workshop-game-host. This listener serves only a copied game.
map $http_upgrade $workshop_game_connection {{
    default upgrade;
    '' close;
}}
server {{
    listen {ORIGIN_PORT} default_server;
    server_name _;
    access_log off;
    if ($http_x_workshop_game_origin != "{secret}") {{ return 403; }}
    client_max_body_size 2m;
    location / {{
        {route}
    }}
    location @game_unavailable {{
        default_type text/plain;
        return 503 "This team has not published an available game.\\n";
    }}
}}
""".encode()


def service_properties(unit: str) -> dict[str, str]:
    names = (
        "LoadState", "ActiveState", "SubState", "MainPID", "DynamicUser",
        "NoNewPrivileges", "PrivateNetwork", "ProtectHome", "ProtectSystem",
        "PrivateDevices", "PrivateTmp", "ProtectProc", "CapabilityBoundingSet",
    )
    result = run(["/usr/bin/systemctl", "show", unit, "--property=" + ",".join(names)], check=False)
    return dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)


def process_status(pid: int, proc: Path) -> dict[str, str]:
    return dict(line.split(":", 1) for line in (proc / str(pid) / "status").read_text().splitlines() if ":" in line)


def socket_fds(pid: int, proc: Path) -> set[str]:
    result = set()
    for fd in (proc / str(pid) / "fd").iterdir():
        with contextlib.suppress(FileNotFoundError):
            value = os.readlink(fd)
            if value.startswith("socket:["):
                result.add(value[8:-1])
    return result


def verify_isolation(attendee_uid: int, *, proc: Path = Path("/proc")) -> dict:
    """Check live PIDs/namespaces/capabilities, not just desired unit text."""
    units = {unit: service_properties(unit) for unit in (GAME, PROXY)}
    pids: dict[str, int] = {}
    namespaces: dict[str, str] = {}
    identities: dict[str, int] = {}
    host_network = os.readlink(proc / "1/ns/net")
    for unit, properties in units.items():
        expected = {
            "ActiveState": "active", "DynamicUser": "yes", "NoNewPrivileges": "yes",
            "PrivateNetwork": "yes", "ProtectHome": "yes", "ProtectSystem": "strict",
            "PrivateDevices": "yes", "PrivateTmp": "yes", "ProtectProc": "invisible",
            "CapabilityBoundingSet": "",
        }
        if any(properties.get(key) != value for key, value in expected.items()):
            raise HostError("ISOLATION_FAILED", "A game service does not have the required effective sandbox settings.")
        try:
            pid = int(properties["MainPID"])
            if pid <= 1:
                raise ValueError
            observed = process_status(pid, proc)
            uids = {int(value) for value in observed["Uid"].split()}
            if len(uids) != 1:
                raise ValueError
            uid = next(iter(uids))
            if not 61184 <= uid <= 65519 or uid == attendee_uid:
                raise ValueError
            gids = {int(value) for value in observed["Gid"].split()}
            groups = {int(value) for value in observed.get("Groups", "").split()}
            if gids != {uid} or not groups.issubset(gids):
                raise ValueError
            if observed["NoNewPrivs"].strip() != "1" or any(
                int(observed[key].strip(), 16)
                for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
            ):
                raise ValueError
            network = os.readlink(proc / str(pid) / "ns/net")
            mounts = os.readlink(proc / str(pid) / "ns/mnt")
            interfaces = {
                line.split(":", 1)[0].strip()
                for line in (proc / str(pid) / "net/dev").read_text().splitlines()[2:]
            }
            if network == host_network or mounts == os.readlink(proc / "1/ns/mnt") or interfaces != {"lo"}:
                raise ValueError
        except (OSError, KeyError, ValueError) as exc:
            raise HostError("ISOLATION_FAILED", "The live game process does not satisfy namespace or identity isolation.") from exc
        pids[unit], namespaces[unit], identities[unit] = pid, network, uid
    if namespaces[GAME] != namespaces[PROXY]:
        raise HostError("ISOLATION_FAILED", "The socket proxy did not join the game's private network.")
    listeners = [
        row.split() for row in (proc / "1/net/tcp").read_text().splitlines()[1:]
        if row.split()[1].endswith(f":{HOST_PORT:04X}") and row.split()[3] == "0A"
    ]
    if len(listeners) != 1 or listeners[0][1] != f"0100007F:{HOST_PORT:04X}":
        raise HostError("ISOLATION_FAILED", "The bridge must have exactly one host loopback listener.")
    inode = listeners[0][9]
    if inode not in socket_fds(pids[PROXY], proc) or inode in socket_fds(pids[GAME], proc):
        raise HostError("ISOLATION_FAILED", "Only socket-proxyd may inherit the host listener.")
    if any(service_properties(unit).get("MainPID") != str(pid) for unit, pid in pids.items()):
        raise HostError("ISOLATION_FAILED", "A game service changed during isolation verification.")
    return {"game_uid": identities[GAME], "proxy_uid": identities[PROXY],
            "network_namespace": namespaces[GAME], "interfaces": ["lo"],
            "capabilities_empty": True, "no_new_privileges": True,
            "host_listener_inherited_by_game": False}


def startup_barriers() -> None:
    """Runs inside the actual game service before any project code executes."""
    if os.geteuid() == 0:
        raise HostError("ISOLATION_FAILED", "Project code cannot execute as root.")
    for path in ("/home", "/root", "/run", "/mnt", "/var/lib/cloud", "/etc/nginx"):
        try:
            os.listdir(path)
        except (PermissionError, FileNotFoundError):
            continue
        raise HostError("ISOLATION_FAILED", "A host directory remains readable inside the game sandbox.")
    for path in ("/proc/1/status", "/etc/stage2-console.env", str(CONFIG_DIR / "config.json")):
        try:
            with open(path, "rb") as reader:
                reader.read(1)
        except (PermissionError, FileNotFoundError):
            continue
        raise HostError("ISOLATION_FAILED", "Host process or secret data remains readable inside the game sandbox.")
    for address in ("169.254.169.254", "127.0.0.1"):
        for port in ((80,) if address.startswith("169.") else (8080, 8081)):
            try:
                with socket.create_connection((address, port), timeout=0.3):
                    pass
            except OSError:
                continue
            raise HostError("ISOLATION_FAILED", "The sandbox can reach a forbidden host service.")


def execute_game() -> None:
    startup_barriers()
    launch = read_json(CONFIG_DIR / "launch.json")
    snapshot_id = launch.get("snapshot_id", "")
    if not SNAPSHOT_RE.fullmatch(snapshot_id) or launch.get("port") not in PORTS:
        raise HostError("INVALID_STATE", "The root-owned launch manifest is invalid.")
    command = validate_command(launch.get("command", []))
    state = STATE_LINK_BASE / f"workshop-game-{snapshot_id}"
    os.chdir(state / "app")
    home = state / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {
        "PATH": SAFE_PATH, "HOME": str(home), "LANG": "C.UTF-8",
        "TMPDIR": "/tmp", "USER": "workshop-game", "LOGNAME": "workshop-game",
        "PORT": str(launch["port"]), "HOST": "127.0.0.1",
    }
    os.execvpe(command[0], command, env)


class Host:
    def __init__(self, *, root: Path = ROOT, config_dir: Path = CONFIG_DIR,
                 units: Path = UNITS, nginx: Path = NGINX, private_state: Path = PRIVATE_STATE):
        self.root, self.config_dir, self.units = root, config_dir, units
        self.nginx, self.private_state = nginx, private_state
        self.current_path = root / "current.json"
        self.launch_path = config_dir / "launch.json"

    def config(self) -> dict:
        value = read_json(self.config_dir / "config.json")
        if not isinstance(value, dict) or not SECRET_RE.fullmatch(value.get("origin_secret", "")):
            raise HostError("NOT_INSTALLED", "Run the workshop's game-host installer first.")
        return value

    @contextlib.contextmanager
    def locked(self):
        secure_directory(self.root)
        path = self.root / "operation.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.geteuid() or os.fstat(fd).st_nlink != 1:
                raise HostError("UNSAFE_MANAGED_PATH", "The operation lock has unsafe ownership.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HostError("BUSY", "Another game publication operation is in progress.") from exc
            yield
        finally:
            os.close(fd)

    def status(self) -> dict:
        config = read_json(self.config_dir / "config.json", {})
        current = read_json(self.current_path, {})
        active = bool(current.get("published")) and all(
            service_properties(unit).get("ActiveState") == "active" for unit in (GAME, PROXY, SOCKET)
        )
        return {
            "schema_version": SCHEMA_VERSION, "ok": True, "installed": bool(config),
            "active": active, "port": current.get("port"),
            "current_project": current.get("project"), "snapshot": current.get("snapshot"),
            "origin": f"http://127.0.0.1:{ORIGIN_PORT}" if config else None,
            "public_url": config.get("public_url"),
        }

    def nginx_route(self, enabled: bool) -> None:
        before = self.nginx.read_bytes() if self.nginx.exists() else None
        atomic_write(self.nginx, render_nginx(self.config()["origin_secret"], enabled))
        if run(["/usr/sbin/nginx", "-t"], check=False).returncode:
            if before is not None:
                atomic_write(self.nginx, before)
            else:
                self.nginx.unlink()
            raise HostError("NGINX_INVALID", "nginx rejected the configuration; the previous configuration was retained.")
        run(["/usr/bin/systemctl", "reload", "nginx"])

    def stop(self) -> None:
        loaded = [u for u in (SOCKET, PROXY, GAME) if service_properties(u).get("LoadState") == "loaded"]
        if loaded:
            run(["/usr/bin/systemctl", "stop", *loaded], timeout=40)
        if any(service_properties(u).get("ActiveState") in ("active", "activating", "deactivating") for u in loaded):
            raise HostError("STOP_FAILED", "An owned publication service did not stop.")

    def start(self) -> None:
        run(["/usr/bin/systemctl", "daemon-reload"])
        run(["/usr/bin/systemctl", "start", GAME, SOCKET, PROXY], timeout=50)

    def readiness(self, *, origin: bool = False) -> None:
        config = self.config()
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            if service_properties(GAME).get("ActiveState") not in ("active", "activating"):
                raise HostError("GAME_START_FAILED", "The copied game exited; use a foreground README start command. Inspect journalctl -u workshop-game.service.")
            connection = http.client.HTTPConnection("127.0.0.1", ORIGIN_PORT if origin else HOST_PORT, timeout=1)
            try:
                headers = {"Host": urlsplit(config["public_url"]).netloc, "X-Forwarded-Proto": "https"}
                if origin:
                    headers[ORIGIN_HEADER] = config["origin_secret"]
                connection.request("GET", "/", headers=headers)
                response = connection.getresponse()
                if 200 <= response.status < 400:
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.25)
        raise HostError("GAME_NOT_READY", "The copied game did not serve HTTP at the selected port. Check the explicit README command and prepared dependencies.")

    def capture(self, project: Path, snapshot_id: str, uid: int, gid: int) -> dict:
        directory = self.root / "snapshots" / snapshot_id
        directory.mkdir(mode=0o700, parents=True)
        archive = directory / "source.tar"
        unit = f"workshop-game-snapshot-{snapshot_id}.service"
        source_home = self.config()["attendee_home"]
        private_source_paths = " ".join(
            json.dumps("-" + str(Path(source_home) / name), ensure_ascii=False)
            for name in (".aws", ".ssh", ".gnupg", ".docker", ".kube", ".claude", ".codex", ".kiro")
        )
        arguments = [
            "/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
            "--service-type=exec", "--unit=" + unit,
            "--property=User=" + str(uid), "--property=Group=" + str(gid),
            "--property=SupplementaryGroups=", "--property=ProtectSystem=strict",
            "--property=ProtectHome=read-only", "--property=PrivateTmp=yes",
            "--property=PrivateNetwork=yes", "--property=PrivateDevices=yes",
            "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=",
            "--property=RestrictNamespaces=yes",
            "--property=InaccessiblePaths=" + " ".join("-" + p for p in HIDDEN_PATHS) + " " + private_source_paths,
            "--property=ProtectProc=invisible", "--property=RuntimeMaxSec=" + str(COPY_SECONDS),
            "--property=TimeoutStopSec=5", "--property=KillMode=control-group",
            "--property=MemoryMax=1G", "--property=TasksMax=32",
            "/usr/bin/python3", "-I", str(HELPER), "_pack", "--project", str(project),
        ]
        try:
            with archive.open("xb") as output:
                result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=output,
                                        stderr=subprocess.PIPE, env=BASE_ENV, cwd="/",
                                        timeout=COPY_SECONDS + 15)
            if result.returncode:
                raise HostError("SNAPSHOT_FAILED", "The read-only project snapshot failed. Check private files, symlinks, prepared dependencies, and SQLite backup readiness.")
        except subprocess.TimeoutExpired as exc:
            raise HostError("SNAPSHOT_TIMEOUT", "The read-only snapshot exceeded its deadline.") from exc
        finally:
            # Only the unique snapshot job we just created, never a game/agent.
            run(["/usr/bin/systemctl", "stop", unit], timeout=5, check=False)
        secure_directory(self.private_state)
        state = self.private_state / f"workshop-game-{snapshot_id}"
        state.mkdir(mode=0o700)
        app = state / "app"
        app.mkdir(mode=0o755)
        manifest = unpack_project(archive, app)
        receipt = {
            "id": snapshot_id, "created_at": utc_now(), "path": str(state / "app"),
            "archive_sha256": digest_file(archive), "files": len(manifest["files"]),
            "bytes": manifest["bytes"],
            "manifest_sha256": hashlib.sha256(json_bytes(manifest)).hexdigest(),
        }
        atomic_write(directory / "snapshot.json", json_bytes(receipt))
        return receipt

    def save_owned_configuration(self) -> dict[Path, bytes | None]:
        paths = [self.launch_path, self.current_path, self.nginx, *(self.units / u for u in (GAME, PROXY, SOCKET))]
        if any(p.is_symlink() for p in paths):
            raise HostError("UNSAFE_MANAGED_PATH", "A managed configuration file is a symlink.")
        return {path: path.read_bytes() if path.exists() else None for path in paths}

    def restore(self, saved: dict[Path, bytes | None], previous: dict) -> None:
        self.stop()
        for path, data in saved.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data, 0o644 if path == self.launch_path or path.parent == self.units else 0o600)
        run(["/usr/bin/systemctl", "daemon-reload"])
        if previous.get("published"):
            self.start()
            self.readiness()
            verify_isolation(int(self.config()["attendee_uid"]))
            self.nginx_route(True)
            self.readiness(origin=True)
        else:
            self.nginx_route(False)

    def publish(self, project_name: str, port: int, command: list[str]) -> dict:
        set_deadline(110)
        try:
            return self._publish(project_name, port, command)
        finally:
            clear_deadline()

    def _publish(self, project_name: str, port: int, command: list[str]) -> dict:
        with self.locked():
            config = self.config()
            project = validate_project(project_name, Path(config["attendee_home"]))
            command = validate_command(command)
            if port not in PORTS:
                raise HostError("INVALID_PORT", "Choose port 8000 or 8001.")
            snapshot_id = uuid.uuid4().hex
            # Preparation does not interrupt the previous shared game.
            snapshot = self.capture(project, snapshot_id, int(config["attendee_uid"]), int(config["attendee_gid"]))
            previous = read_json(self.current_path, {})
            saved = self.save_owned_configuration()
            candidate = {"project": str(project), "port": port, "snapshot": snapshot,
                         "published": True, "published_at": utc_now()}
            try:
                self.nginx_route(False)
                self.stop()
                atomic_write(self.launch_path, json_bytes({
                    "snapshot_id": snapshot_id, "port": port, "command": command,
                }), 0o644)
                for name, content in render_units(snapshot_id, port, config["proxy_binary"]).items():
                    atomic_write(self.units / name, content, 0o644)
                self.start()
                self.readiness()
                candidate["isolation"] = verify_isolation(int(config["attendee_uid"]))
                self.nginx_route(True)
                self.readiness(origin=True)
                atomic_write(self.current_path, json_bytes(candidate))
                return self.status()
            except Exception as exc:
                set_deadline(35)
                rollback = {"attempted": True, "succeeded": False,
                            "snapshot_id": previous.get("snapshot", {}).get("id")}
                try:
                    self.restore(saved, previous)
                    rollback["succeeded"] = True
                except Exception:
                    # Close owned serving processes even if unrelated nginx
                    # configuration prevents reload. Never leave a candidate live.
                    set_deadline(15)
                    with contextlib.suppress(Exception):
                        self.stop()
                    with contextlib.suppress(Exception):
                        self.nginx_route(False)
                error = exc if isinstance(exc, HostError) else HostError("PUBLICATION_FAILED", "The copied game could not be published.")
                atomic_write(self.root / "snapshots" / snapshot_id / "failure.json",
                             json_bytes({"at": utc_now(), "error": error.as_json(), "rollback": rollback}))
                raise HostError(error.code, error.message, rollback=rollback,
                                retained_snapshot=snapshot_id) from exc

    def unpublish(self) -> dict:
        set_deadline(50)
        try:
            return self._unpublish()
        finally:
            clear_deadline()

    def _unpublish(self) -> dict:
        with self.locked():
            self.config()
            try:
                self.nginx_route(False)
            finally:
                self.stop()
            current = read_json(self.current_path, {})
            current.update(published=False, unpublished_at=utc_now())
            atomic_write(self.current_path, json_bytes(current))
            return self.status()


def aws_json(arguments: list[str], region: str) -> dict:
    aws = "/usr/local/bin/aws" if Path("/usr/local/bin/aws").exists() else "/usr/bin/aws"
    # No caller env/profile credentials. This operator path uses the instance
    # role, and only during installation, never in the sandbox or startup code.
    result = run([
        aws, *arguments, "--region", region, "--output", "json", "--no-cli-pager",
        "--cli-connect-timeout", "5", "--cli-read-timeout", "15",
    ], timeout=30, check=False)
    if result.returncode:
        if b"ParameterNotFound" in result.stderr:
            raise HostError("CONFIG_PENDING", "The game hosting configuration is not available yet.")
        raise HostError("AWS_CONFIG_FAILED", "The instance role could not read game hosting configuration.")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise HostError("AWS_CONFIG_FAILED", "AWS returned invalid game hosting configuration.") from exc


def find_proxy_binary() -> str:
    for name in ("/usr/lib/systemd/systemd-socket-proxyd", "/lib/systemd/systemd-socket-proxyd"):
        path = Path(name)
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        real = path.resolve()
        if all(p.stat().st_uid == 0 and not p.stat().st_mode & 0o022 for p in (real, *real.parents)):
            return name
    raise HostError("MISSING_SOCKET_PROXY", "Install the root-owned Ubuntu systemd socket proxy before enabling game hosting.")


def install(region: str, host: Host | None = None) -> dict:
    set_deadline(420)
    try:
        return _install(region, host)
    finally:
        clear_deadline()


def _install(region: str, host: Host | None = None) -> dict:
    host = host or Host()
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region):
        raise HostError("INVALID_REGION", "Supply the stack's AWS region.")
    identity = aws_json(["sts", "get-caller-identity"], region)
    deadline = time.monotonic() + 300
    while True:
        try:
            document = aws_json(["ssm", "get-parameter", "--name", SOURCE_PARAMETER], region)
            config = json.loads(document["Parameter"]["Value"])
            break
        except HostError as exc:
            if exc.code != "CONFIG_PENDING":
                raise
            if time.monotonic() >= deadline:
                raise HostError("CONFIG_TIMEOUT", "Game hosting configuration did not appear within five minutes. Inspect the game CloudFront/SSM resources, then rerun only the game-host installer.") from exc
            time.sleep(5)
    if config.get("account_id") != identity.get("Account") or config.get("region") != region:
        raise HostError("ACCOUNT_REGION_MISMATCH", "Hosting configuration must belong to this instance account and region.")
    public_url = config.get("public_url", "")
    url = urlsplit(public_url)
    if (url.scheme != "https" or not re.fullmatch(r"d[a-z0-9]+\.cloudfront\.net", url.netloc)
            or url.path not in ("", "/") or url.query or url.fragment):
        raise HostError("INVALID_CONFIG", "The public game URL must be its separate CloudFront origin.")
    secret_arn = config.get("origin_secret_arn", "")
    if not re.fullmatch(
        rf"arn:aws:secretsmanager:{re.escape(region)}:{re.escape(identity['Account'])}:secret:[A-Za-z0-9/_+=.@-]+",
        secret_arn,
    ):
        raise HostError("INVALID_CONFIG", "The game origin secret must belong to this account and region.")
    secret = aws_json(["secretsmanager", "get-secret-value", "--secret-id", secret_arn], region).get("SecretString", "")
    if not SECRET_RE.fullmatch(secret):
        raise HostError("INVALID_CONFIG", "The game origin secret must be 48 alphanumeric characters.")
    try:
        attendee = pwd.getpwnam(config["attendee_user"])
    except (KeyError, TypeError) as exc:
        raise HostError("INVALID_CONFIG", "The configured attendee user does not exist.") from exc
    if attendee.pw_uid == 0:
        raise HostError("INVALID_CONFIG", "The attendee user cannot be root.")
    if not Path(attendee.pw_dir).is_absolute() or any(c in attendee.pw_dir for c in "\r\n\x00$%:"):
        raise HostError("INVALID_CONFIG", "The attendee home has an unsupported path.")
    proxy = find_proxy_binary()
    secure_directory(host.config_dir, 0o755)
    secure_directory(host.root)
    with host.locked():
        if any(service_properties(u).get("ActiveState") in ("active", "activating") for u in (GAME, PROXY, SOCKET)):
            raise HostError("PUBLICATION_ACTIVE", "Unpublish the copied game before updating its hosting installation.")
        local = {
            "schema_version": SCHEMA_VERSION, "account_id": identity["Account"],
            "region": region, "public_url": public_url.rstrip("/") + "/",
            "distribution_id": config.get("distribution_id"), "origin_secret": secret,
            "attendee_uid": attendee.pw_uid, "attendee_gid": attendee.pw_gid,
            "attendee_home": attendee.pw_dir, "proxy_binary": proxy,
        }
        atomic_write(host.config_dir / "config.json", json_bytes(local))
        host.nginx_route(False)
    return host.status()


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally echoes invalid values, which might be a mistakenly
        # pasted secret. The public JSON error does not echo command arguments.
        raise HostError("INVALID_ARGUMENTS", "Use status, publish --project ABS --port 8000|8001 -- COMMAND ARGS, or unpublish.")


def parser() -> argparse.ArgumentParser:
    result = JsonArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True, parser_class=JsonArgumentParser)
    commands.add_parser("status")
    commands.add_parser("unpublish")
    publication = commands.add_parser("publish")
    publication.add_argument("--project", required=True)
    publication.add_argument("--port", required=True, type=int, choices=PORTS)
    publication.add_argument("command", nargs=argparse.REMAINDER)
    installation = commands.add_parser("install")
    installation.add_argument("--region", required=True)
    packing = commands.add_parser("_pack", help=argparse.SUPPRESS)
    packing.add_argument("--project", required=True)
    commands.add_parser("_exec", help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.action == "_pack":
            if os.geteuid() == 0:
                raise HostError("ROOT_PROJECT_READ", "Project snapshotting must run as the attendee.")
            project = validate_project(args.project, Path(pwd.getpwuid(os.getuid()).pw_dir))
            pack_project(project, sys.stdout.buffer)
            return 0
        if args.action == "_exec":
            execute_game()
            return 0
        if os.geteuid() != 0:
            raise HostError("ROOT_REQUIRED", "Run the installed workshop-game-host helper with sudo.")
        host = Host()
        if args.action == "status":
            result = host.status()
        elif args.action == "publish":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            result = host.publish(args.project, args.port, command)
        elif args.action == "unpublish":
            result = host.unpublish()
        else:
            result = install(args.region, host)
        print(json.dumps(result, sort_keys=True))
        return 0
    except HostError as exc:
        output = {"schema_version": SCHEMA_VERSION, "ok": False, "error": exc.as_json()}
        if argv is None and len(sys.argv) > 1 and sys.argv[1] in ("_pack", "_exec"):
            print(json.dumps(output, sort_keys=True), file=sys.stderr)
        else:
            print(json.dumps(output, sort_keys=True))
        return 1
    except Exception as exc:
        # Never echo arbitrary child output, command arguments, or secret values.
        print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": False, "error": {
            "code": "HOST_OPERATION_FAILED", "message": f"Hosting operation failed ({type(exc).__name__}).",
        }}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
