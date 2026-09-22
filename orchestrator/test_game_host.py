"""Local regression tests for copying, the privilege boundary and publication.

These exercise real files/SQLite/argv and fault-inject the systemd boundary.
They do not claim that a macOS test process proves Linux namespace isolation.
The installed helper checks effective Linux process state before publication.
"""
from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_host as host


SECRET = "aB3" * 16
OLD = "1" * 32
NEW = "2" * 32


def write(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def snapshot(project, tmp_path):
    archive = tmp_path / "source.tar"
    with archive.open("wb") as stream:
        manifest = host.pack_project(project, stream)
    destination = tmp_path / "copied"
    destination.mkdir()
    assert host.unpack_project(archive, destination) == manifest
    return destination


def test_prepared_dependencies_internal_links_and_local_edits_are_copied(tmp_path):
    project = tmp_path / "project"
    write(project / "server.js", b"locally edited source")
    binary = write(project / "node_modules/dependency/addon.node", b"\x00native\xff")
    tool = write(project / "node_modules/dependency/cli.js", b"#!/usr/bin/env node\n")
    tool.chmod(0o755)
    link = project / "node_modules/.bin/tool"
    link.parent.mkdir()
    link.symlink_to("../dependency/cli.js")
    write(project / ".well-known/game.json", b"{}")
    for name in (".env", ".env.production", ".git/config", ".aws/credentials", ".npmrc", "keys/private.pem"):
        write(project / name, b"private bytes must not cross")
    before = hashlib.sha256(binary.read_bytes()).hexdigest()
    copied = snapshot(project, tmp_path)
    assert (copied / "server.js").read_bytes() == b"locally edited source"
    assert (copied / "node_modules/.bin/tool").read_bytes() == tool.read_bytes()
    assert (copied / "node_modules/dependency/cli.js").stat().st_mode & 0o111
    assert (copied / ".well-known/game.json").exists()
    assert (copied / "node_modules/dependency/addon.node").read_bytes() == binary.read_bytes()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == before
    for name in (".env", ".env.production", ".git", ".aws", ".npmrc", "keys/private.pem"):
        assert not (copied / name).exists()


@pytest.mark.parametrize("kind", ["outside", "excluded", "cycle", "broken"])
def test_project_symlinks_cannot_export_private_or_outside_data(tmp_path, kind):
    project = tmp_path / "project"
    project.mkdir()
    write(tmp_path / "secret", b"do not copy")
    write(project / ".env", b"do not copy")
    target = {"outside": "../secret", "excluded": ".env", "cycle": "link", "broken": "missing"}[kind]
    (project / "link").symlink_to(target)
    with pytest.raises(host.HostError, match="symlink"):
        host.pack_project(project, io.BytesIO())


def test_absolute_internal_link_is_rebased_into_the_separate_copy(tmp_path):
    project = tmp_path / "project"
    source = write(project / "assets/game.txt", b"game")
    (project / "current").symlink_to(source)
    copied = snapshot(project, tmp_path)
    assert os.readlink(copied / "current") == "assets/game.txt"
    source.write_text("later user edit")
    assert (copied / "current").read_text() == "game"


def test_hardlinked_private_file_is_rejected(tmp_path):
    secret = write(tmp_path / "secret", b"private")
    project = tmp_path / "project"
    project.mkdir()
    os.link(secret, project / "looks-public.txt")
    with pytest.raises(host.HostError, match="hardlinked"):
        host.pack_project(project, io.BytesIO())
    assert secret.read_bytes() == b"private"


def test_live_sqlite_backup_contains_committed_wal_without_changing_db_or_wal(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    database = project / "scores.db"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE scores(player TEXT, score INTEGER)")
        writer.executemany("INSERT INTO scores VALUES(?,?)", [("Lab3Player", 80), ("awesomeman", 1000)])
        writer.commit()
        writer.execute("INSERT INTO scores VALUES('uncommitted', 7)")
        watched = [database, project / "scores.db-wal"]
        before = {p: p.read_bytes() for p in watched}
        copied = snapshot(project, tmp_path)
        assert all(p.read_bytes() == data for p, data in before.items())
        assert not (copied / "scores.db-wal").exists()
        assert not (copied / "scores.db-shm").exists()
        with sqlite3.connect(copied / "scores.db") as reader:
            assert reader.execute("SELECT * FROM scores ORDER BY score DESC").fetchall() == [
                ("awesomeman", 1000), ("Lab3Player", 80),
            ]
            reader.execute("INSERT INTO scores VALUES('gallery visitor', 900)")
        assert writer.execute("SELECT count(*) FROM scores WHERE player='gallery visitor'").fetchone()[0] == 0
    finally:
        writer.close()


def test_database_backup_deadline_is_real(tmp_path):
    database = tmp_path / "data.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE data(x)")
    with pytest.raises(host.HostError, match="deadline"):
        host.sqlite_backup(database, tmp_path / "copy.sqlite", deadline=time.monotonic() - 1)


def test_file_replaced_during_copy_is_not_a_successful_snapshot(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source = write(project / "game.txt", b"before")
    original = tarfile.TarFile.addfile

    def addfile(archive, item, fileobj=None):
        if item.name == "game.txt":
            source.write_bytes(b"changed while the snapshot reads")
        return original(archive, item, fileobj)

    monkeypatch.setattr(tarfile.TarFile, "addfile", addfile)
    with pytest.raises(host.HostError, match="changed"):
        host.pack_project(project, io.BytesIO())


def test_fifo_is_rejected_without_opening_or_hanging(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    os.mkfifo(project / "pipe")
    with pytest.raises(host.HostError, match="FIFO"):
        host.pack_project(project, io.BytesIO())


def malicious_archive(path, members):
    with tarfile.open(path, "w") as archive:
        for name, kind, value in members:
            member = tarfile.TarInfo(name)
            if kind == "file":
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                member.linkname = value
                archive.addfile(member)


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", ".aws/credentials", "a\\b", "bad\nname"])
def test_privileged_extractor_rejects_unsafe_file_paths(tmp_path, name):
    archive = tmp_path / "input.tar"
    malicious_archive(archive, [(name, "file", b"not allowed")])
    copied = tmp_path / "copied"
    copied.mkdir()
    with pytest.raises(host.HostError):
        host.unpack_project(archive, copied)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_archive_links_cannot_redirect_a_privileged_write(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    archive = tmp_path / "input.tar"
    malicious_archive(archive, [("escape", kind, str(outside)), ("escape/file", "file", b"bad")])
    copied = tmp_path / "copied"
    copied.mkdir()
    with pytest.raises(host.HostError):
        host.unpack_project(archive, copied)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("project", ["relative", "/", "HOME", "HOME/.aws", "HOME/a%20", "HOME/$HOME"])
def test_project_selection_cannot_copy_home_or_system_secrets(tmp_path, project):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".aws").mkdir()
    with pytest.raises(host.HostError):
        host.validate_project(project.replace("HOME", str(home)), home)


@pytest.mark.parametrize("command", [[], ["/home/ubuntu/game/run"], ["../run"], ["-bad"], ["node\ninjected"], ["node", "\x00"]])
def test_invalid_executable_or_arguments_cannot_reach_launch(command):
    with pytest.raises(host.HostError):
        host.validate_command(command)


def test_readme_arguments_are_literal_argv_and_aws_credentials_are_not_inherited(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    state = tmp_path / f"workshop-game-{NEW}"
    (state / "app").mkdir(parents=True)
    command = ["node", "server.js", "--label", "'two words' $HOME %n ; not-a-shell"]
    write(config / "launch.json", host.json_bytes({"snapshot_id": NEW, "port": 8001, "command": command}))
    monkeypatch.setattr(host, "CONFIG_DIR", config)
    monkeypatch.setattr(host, "STATE_LINK_BASE", tmp_path)
    monkeypatch.setattr(host, "startup_barriers", lambda: None)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-game")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "must-not-reach-game")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/home/ubuntu/injected.js")
    execute = Mock(side_effect=SystemExit(0))
    monkeypatch.setattr(os, "execvpe", execute)
    previous = Path.cwd()
    try:
        with pytest.raises(SystemExit):
            host.execute_game()
        program, argv, environment = execute.call_args.args
        assert program == "node" and argv == command
        assert environment["PORT"] == "8001"
        assert environment["HOST"] == "127.0.0.1"
        assert Path.cwd() == state / "app"
        assert not any(key.startswith("AWS_") for key in environment)
        assert "NODE_OPTIONS" not in environment
    finally:
        os.chdir(previous)


def test_socket_plumbing_keeps_the_host_listener_out_of_the_game_unit():
    units = host.render_units(NEW, 8001, "/lib/systemd/systemd-socket-proxyd")
    parsed = {}
    for name, data in units.items():
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(data.decode())
        parsed[name] = parser
    socket_unit = parsed[host.SOCKET]["Socket"]
    assert socket_unit["ListenStream"] == "127.0.0.1:8083"
    assert socket_unit["Service"] == host.PROXY and socket_unit["Accept"] == "no"
    assert "PrivateNetwork" not in socket_unit  # PID1 binds the host namespace.
    game = parsed[host.GAME]
    proxy = parsed[host.PROXY]
    assert proxy["Unit"]["JoinsNamespaceOf"] == host.GAME
    assert proxy["Unit"]["BindsTo"] == host.GAME
    assert proxy["Unit"]["PartOf"] == host.GAME
    assert host.GAME in proxy["Unit"]["After"]
    assert "127.0.0.1:8001" in proxy["Service"]["ExecStart"]
    assert "socket-proxyd" not in game["Service"]["ExecStart"]
    for unit in (game, proxy):
        settings = unit["Service"]
        assert settings["DynamicUser"] == settings["PrivateNetwork"] == "yes"
        assert settings["ProtectSystem"] == "strict"
        assert settings["CapabilityBoundingSet"] == settings["AmbientCapabilities"] == ""
        assert "-/run" in settings["InaccessiblePaths"].split()
        assert "-/mnt" in settings["InaccessiblePaths"].split()
        assert settings["NoNewPrivileges"] == "yes"
    assert game["Service"]["StateDirectory"] == f"workshop-game-{NEW}"


def test_nginx_separates_auth_and_keeps_game_framework_cookies():
    rendered = host.render_nginx(SECRET, True).decode()
    assert "listen 8082 default_server;" in rendered
    assert "proxy_pass http://127.0.0.1:8083;" in rendered
    assert 'proxy_set_header Authorization "";' in rendered
    assert 'proxy_set_header X-Workshop-Game-Origin "";' in rendered
    assert "proxy_ignore_headers X-Accel-Redirect;" in rendered
    assert "proxy_set_header Cookie" not in rendered
    assert "proxy_hide_header Set-Cookie" not in rendered
    assert "proxy_pass" not in host.render_nginx(SECRET, False).decode()
    assert all(bad not in rendered for bad in ("localhost:8080", "localhost:8081", "/proxy/", "alias "))
    with pytest.raises(host.HostError):
        host.render_nginx('"; proxy_pass http://localhost:8080; #', True)


def proc_fixture(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    properties = {}
    for unit, pid, uid in ((host.GAME, 100, 62000), (host.PROXY, 101, 62001)):
        properties[unit] = {
            "ActiveState": "active", "DynamicUser": "yes", "NoNewPrivileges": "yes",
            "PrivateNetwork": "yes", "ProtectHome": "yes", "ProtectSystem": "strict",
            "PrivateDevices": "yes", "PrivateTmp": "yes", "ProtectProc": "invisible",
            "CapabilityBoundingSet": "", "MainPID": str(pid),
        }
        base = proc / str(pid)
        write(base / "status", (
            f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{uid}\t{uid}\t{uid}\t{uid}\nGroups:\t{uid}\nNoNewPrivs:\t1\n"
            + "".join(f"{name}:\t0000000000000000\n" for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
        ).encode())
        write(base / "net/dev", b"header\nheader\n lo: 0 0 0\n")
        (base / "ns").mkdir()
        (base / "ns/net").symlink_to("net:[200]")
        (base / "ns/mnt").symlink_to(f"mnt:[{pid}]")
        (base / "fd").mkdir()
    (proc / "101/fd/3").symlink_to("socket:[777]")
    (proc / "1/ns").mkdir(parents=True)
    (proc / "1/ns/net").symlink_to("net:[1]")
    (proc / "1/ns/mnt").symlink_to("mnt:[1]")
    write(proc / "1/net/tcp", b"header\n0: 0100007F:1F93 00000000:0000 0A 0 0 0 0 0 777\n")
    monkeypatch.setattr(host, "service_properties", lambda unit: properties[unit])
    return proc, properties


def test_effective_process_proof_accepts_only_private_loopback_and_dynamic_users(tmp_path, monkeypatch):
    proc, _ = proc_fixture(tmp_path, monkeypatch)
    proof = host.verify_isolation(1000, proc=proc)
    assert proof["game_uid"] == 62000
    assert proof["proxy_uid"] == 62001
    assert proof["interfaces"] == ["lo"]
    assert proof["host_listener_inherited_by_game"] is False


@pytest.mark.parametrize("defect", ["host-network", "wrong-proxy-network", "root", "supplementary-group", "capability", "eth0", "app-host-socket"])
def test_effective_process_proof_rejects_real_boundary_mismatches(tmp_path, monkeypatch, defect):
    proc, _ = proc_fixture(tmp_path, monkeypatch)
    if defect in ("host-network", "wrong-proxy-network"):
        path = proc / ("100/ns/net" if defect == "host-network" else "101/ns/net")
        path.unlink()
        path.symlink_to("net:[1]" if defect == "host-network" else "net:[300]")
    elif defect == "root":
        path = proc / "100/status"
        path.write_text(path.read_text().replace("62000", "0"))
    elif defect == "capability":
        path = proc / "100/status"
        path.write_text(path.read_text().replace("CapEff:\t0000000000000000", "CapEff:\t0000000000000001"))
    elif defect == "supplementary-group":
        path = proc / "100/status"
        path.write_text(path.read_text().replace("Groups:\t62000", "Groups:\t62000 0 998"))
    elif defect == "eth0":
        path = proc / "100/net/dev"
        path.write_text(path.read_text() + "eth0: 0 0\n")
    else:
        (proc / "100/fd/8").symlink_to("socket:[777]")
    with pytest.raises(host.HostError) as error:
        host.verify_isolation(1000, proc=proc)
    assert error.value.code == "ISOLATION_FAILED"


class FakeHost(host.Host):
    def __init__(self, tmp_path, monkeypatch):
        super().__init__(root=tmp_path / "metadata", config_dir=tmp_path / "config",
                         units=tmp_path / "units", nginx=tmp_path / "nginx.conf",
                         private_state=tmp_path / "private")
        for path in (self.root, self.config_dir, self.units, self.private_state):
            path.mkdir(mode=0o700)
        self.home = tmp_path / "attendee"
        self.project = self.home / "game"
        write(self.project / "server.js", b"original user edit")
        write(self.project / "scores.db", b"not a sqlite fixture; preserve these bytes")
        self.operations = []
        self.live = False
        self.fail_ready = False
        self.fail_rollback = False
        self.capture_error = False
        host.atomic_write(self.config_dir / "config.json", host.json_bytes({
            "origin_secret": SECRET, "public_url": "https://dexample.cloudfront.net/",
            "attendee_home": str(self.home), "attendee_uid": os.getuid(), "attendee_gid": os.getgid(),
            "proxy_binary": "/lib/systemd/systemd-socket-proxyd",
        }))
        monkeypatch.setattr(host, "service_properties",
                            lambda unit: {"LoadState": "loaded", "ActiveState": "active" if self.live else "inactive"})
        monkeypatch.setattr(host, "verify_isolation", lambda _uid: {"tested": "fake boundary only"})
        monkeypatch.setattr(host, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, b"", b""))

    def capture(self, project, snapshot_id, uid, gid):
        self.operations.append("capture")
        if self.capture_error:
            raise host.HostError("SNAPSHOT_FAILED", "injected")
        directory = self.root / "snapshots" / snapshot_id
        directory.mkdir(parents=True)
        copied = self.private_state / snapshot_id
        copied.mkdir()
        write(copied / "scores.db", (project / "scores.db").read_bytes())
        return {"id": snapshot_id, "path": str(copied), "archive_sha256": "a" * 64}

    def nginx_route(self, enabled):
        self.operations.append(("route", enabled))
        host.atomic_write(self.nginx, host.render_nginx(SECRET, enabled))

    def stop(self):
        self.operations.append("stop")
        self.live = False

    def start(self):
        self.operations.append("start")
        self.live = True

    def readiness(self, *, origin=False):
        self.operations.append(("ready", origin))
        if self.fail_ready:
            self.fail_ready = False
            raise host.HostError("GAME_NOT_READY", "injected candidate failure")
        if self.fail_rollback:
            raise host.HostError("GAME_NOT_READY", "injected rollback failure")

    def seed_previous(self):
        old_copy = self.private_state / OLD
        write(old_copy / "scores.db", b"old published scores")
        host.atomic_write(self.launch_path, host.json_bytes({"snapshot_id": OLD, "port": 8000, "command": ["node", "server.js"]}), 0o644)
        host.atomic_write(self.current_path, host.json_bytes({
            "published": True, "project": str(self.project), "port": 8000,
            "snapshot": {"id": OLD, "path": str(old_copy)},
        }))
        for name, content in host.render_units(OLD, 8000, "/lib/systemd/systemd-socket-proxyd").items():
            write(self.units / name, content)
        self.nginx_route(True)
        self.live = True
        self.operations.clear()
        return old_copy


def test_successful_publish_snapshots_before_stopping_and_reports_real_origin(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    original = (instance.project / "scores.db").read_bytes()
    result = instance.publish(str(instance.project), 8001, ["npm", "start"])
    assert result["ok"] and result["active"]
    assert result["public_url"] == "https://dexample.cloudfront.net/"
    assert result["port"] == 8001
    assert result["current_project"] == str(instance.project)
    assert instance.operations.index("capture") < instance.operations.index("stop")
    assert (instance.project / "scores.db").read_bytes() == original
    launch = json.loads(instance.launch_path.read_text())
    assert launch["command"] == ["npm", "start"]
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_failed_replace_restores_same_old_working_copy_and_retains_candidate(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    old_copy = instance.seed_previous()
    prior_units = {p.name: p.read_bytes() for p in instance.units.iterdir()}
    original = (instance.project / "scores.db").read_bytes()
    instance.fail_ready = True
    with pytest.raises(host.HostError) as error:
        instance.publish(str(instance.project), 8001, ["npm", "start"])
    assert error.value.details["rollback"] == {"attempted": True, "succeeded": True, "snapshot_id": OLD}
    assert instance.status()["active"]
    assert instance.status()["snapshot"]["id"] == OLD
    assert old_copy.joinpath("scores.db").read_bytes() == b"old published scores"
    assert (instance.project / "scores.db").read_bytes() == original
    assert {p.name: p.read_bytes() for p in instance.units.iterdir()} == prior_units
    candidate = error.value.details["retained_snapshot"]
    assert (instance.private_state / candidate).is_dir()
    assert (instance.root / "snapshots" / candidate / "failure.json").exists()


def test_preparation_failure_does_not_interrupt_previous_publication(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    instance.seed_previous()
    instance.capture_error = True
    with pytest.raises(host.HostError):
        instance.publish(str(instance.project), 8001, ["npm", "start"])
    assert instance.live
    assert instance.operations == ["capture"]
    assert instance.status()["snapshot"]["id"] == OLD


def test_rollback_failure_closes_candidate_without_claiming_success(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    instance.seed_previous()
    instance.fail_ready = instance.fail_rollback = True
    with pytest.raises(host.HostError) as error:
        instance.publish(str(instance.project), 8001, ["npm", "start"])
    assert error.value.details["rollback"]["succeeded"] is False
    assert not instance.status()["active"]
    assert b"proxy_pass" not in instance.nginx.read_bytes()


def test_unpublish_stops_only_the_copy_and_retains_original_and_copied_scores(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    old_copy = instance.seed_previous()
    original = (instance.project / "scores.db").read_bytes()
    result = instance.unpublish()
    assert result["ok"] and not result["active"]
    assert result["snapshot"]["id"] == OLD
    assert result["public_url"] == "https://dexample.cloudfront.net/"
    assert old_copy.joinpath("scores.db").read_bytes() == b"old published scores"
    assert (instance.project / "scores.db").read_bytes() == original
    assert instance.operations[:2] == [("route", False), "stop"]


def test_json_errors_do_not_echo_accidentally_pasted_secrets(capsys):
    secret = "accidentally-pasted-secret"
    assert host.main(["publish", "--project", "/home/u/game", "--port", secret]) == 1
    raw = capsys.readouterr().out
    response = json.loads(raw)
    assert not response["ok"] and response["error"]["code"] == "INVALID_ARGUMENTS"
    assert secret not in raw


def test_managed_write_rejects_symlink_without_touching_target(tmp_path):
    target = write(tmp_path / "private", b"unchanged")
    link = tmp_path / "managed"
    link.symlink_to(target)
    with pytest.raises(host.HostError):
        host.atomic_write(link, b"changed")
    assert target.read_bytes() == b"unchanged"


def test_subprocess_deadline_cannot_outlive_remaining_operation_budget(monkeypatch):
    captured = []
    monkeypatch.setattr(host, "_deadline", 110.0)
    monkeypatch.setattr(host.time, "monotonic", lambda: 109.0)
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k:
                        (captured.append(k), subprocess.CompletedProcess(a[0], 0, b"", b""))[1])
    host.run(["/usr/bin/systemctl", "show", host.GAME], timeout=30)
    assert captured[0]["timeout"] == 1
    assert captured[0]["env"]["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"


def test_installer_waits_for_cloudfront_parameter_without_requiring_bootstrap_dependency(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    account = "111122223333"
    secret_arn = f"arn:aws:secretsmanager:us-west-2:{account}:secret:game-origin-abcd"
    calls = []
    missing = [True]

    def aws(arguments, region):
        assert region == "us-west-2"
        calls.append(arguments[:2])
        if arguments[0] == "sts":
            return {"Account": account}
        if arguments[0] == "ssm":
            if missing:
                missing.pop()
                raise host.HostError("CONFIG_PENDING", "not created yet")
            return {"Parameter": {"Value": json.dumps({
                "account_id": account, "region": region, "attendee_user": "attendee",
                "public_url": "https://dexample.cloudfront.net/",
                "origin_secret_arn": secret_arn, "distribution_id": "EEXAMPLE",
            })}}
        assert arguments == ["secretsmanager", "get-secret-value", "--secret-id", secret_arn]
        return {"SecretString": SECRET}

    monkeypatch.setattr(host, "aws_json", aws)
    monkeypatch.setattr(host.time, "sleep", Mock())
    monkeypatch.setattr(host.pwd, "getpwnam", lambda _: SimpleNamespace(
        pw_uid=1000, pw_gid=1000, pw_dir=str(instance.home)))
    monkeypatch.setattr(host, "find_proxy_binary", lambda: "/lib/systemd/systemd-socket-proxyd")
    result = host.install("us-west-2", instance)
    assert result["public_url"] == "https://dexample.cloudfront.net/"
    assert result["installed"] and not result["active"]
    assert calls.count(["ssm", "get-parameter"]) == 2
    assert calls.count(["secretsmanager", "get-secret-value"]) == 1
    host.time.sleep.assert_called_once_with(5)
    assert SECRET not in json.dumps(result)
    assert (instance.config_dir / "config.json").stat().st_mode & 0o777 == 0o600
    assert b"proxy_pass" not in instance.nginx.read_bytes()


def test_installer_does_not_retry_permission_failure_or_write_configuration(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    before = (instance.config_dir / "config.json").read_bytes()

    def aws(arguments, _region):
        if arguments[0] == "sts":
            return {"Account": "111122223333"}
        raise host.HostError("AWS_CONFIG_FAILED", "denied")

    monkeypatch.setattr(host, "aws_json", aws)
    monkeypatch.setattr(host.time, "sleep", Mock())
    with pytest.raises(host.HostError) as error:
        host.install("us-west-2", instance)
    assert error.value.code == "AWS_CONFIG_FAILED"
    host.time.sleep.assert_not_called()
    assert (instance.config_dir / "config.json").read_bytes() == before
    assert not instance.nginx.exists()


def test_missing_config_has_a_five_minute_deadline_and_no_host_changes(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    ticks = iter(range(0, 10000, 100))
    monkeypatch.setattr(host.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(host.time, "sleep", lambda _: None)

    def aws(arguments, _region):
        if arguments[0] == "sts":
            return {"Account": "111122223333"}
        raise host.HostError("CONFIG_PENDING", "not created")

    monkeypatch.setattr(host, "aws_json", aws)
    with pytest.raises(host.HostError) as error:
        host.install("us-west-2", instance)
    assert error.value.code == "CONFIG_TIMEOUT"
    assert not instance.nginx.exists()
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_first_publication_failure_keeps_unrelated_units_and_original_game(tmp_path, monkeypatch):
    instance = FakeHost(tmp_path, monkeypatch)
    unrelated = write(instance.units / "stage2-console.service", b"unchanged console unit")
    instance.fail_ready = True
    with pytest.raises(host.HostError):
        instance.publish(str(instance.project), 8000, ["npm", "start"])
    assert not instance.status()["active"]
    assert instance.status()["snapshot"] is None
    assert unrelated.read_bytes() == b"unchanged console unit"
    assert (instance.project / "server.js").read_bytes() == b"original user edit"
