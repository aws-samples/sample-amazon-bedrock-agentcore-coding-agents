"""Execute the generated Runtime shell offline; retain output and actual exits."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tarfile

import pytest
from bedrock_agentcore.runtime.shell import ShellChannel, ShellFrame

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import identity_baggage
import runtime_exec
import runtime_stage


# These bytes and transcript expectations also exercise the newline-terminated
# controls. A fix must not "pass" by deleting whitespace from the expected data.
_OUTPUT_CASES = [
    ("stdout_lf_success", b"fixture stdout\n", b"", 0, "fixture stdout"),
    ("stdout_lf_failure", b"fixture stdout\n", b"", 7, "fixture stdout"),
    ("stdout_joined_end_success", b"fixture stdout", b"", 0, "fixture stdout"),
    ("stdout_joined_end_failure", b"fixture stdout", b"", 7, "fixture stdout"),
    ("stderr_lf_success", b"", b"fixture stderr\n", 0, "fixture stderr"),
    ("stderr_lf_failure", b"", b"fixture stderr\n", 7, "fixture stderr"),
    ("stderr_joined_end_success", b"", b"fixture stderr", 0, "fixture stderr"),
    ("stderr_joined_end_failure", b"", b"fixture stderr", 7, "fixture stderr"),
    ("both_channels_lf_failure", b"fixture stdout\n", b"fixture stderr\n", 7,
     "fixture stdout\nfixture stderr"),
    ("stderr_utf8_and_invalid_byte", b"", "fixture \u03bb ".encode() + b"\xff\n", 7,
     "fixture \u03bb \ufffd"),
    ("empty_output_success", b"", b"", 0, ""),
]

_FIXTURE_EXECUTABLE = r'''
import base64, json, os, shutil, signal, sys, time
from pathlib import Path
c = json.loads(Path(os.environ["FRAMING_CASE"]).read_text())
a = sys.argv[1:]
tool = Path(sys.argv[0]).name
if tool == "tar":
    # The generated Linux command uses GNU --touch. On macOS only, remove this
    # unsupported metadata option; still execute real tar on the actual archive.
    if sys.platform == "darwin":
        a = [arg for arg in a if arg != "--touch"]
    os.execv(c["tar"], [c["tar"], *a])
if tool == "aws":
    assert a[:2] == ["s3", "cp"], a
    src, dst = a[2:4]
    assert a[4:] == ["--region", "us-west-2", "--only-show-errors"], a
    if src == c["archive_uri"]:
        if c["hydrate_fd"]:
            os.write(c["hydrate_fd"], b"fixture hydration error")
            sys.exit(19)
        assert dst.startswith("/tmp/workshop-source-")
        shutil.copyfile(c["seed"], dst)
    else:
        assert dst == c["archive_uri"] and src.startswith("/tmp/workshop-result-"), a
        shutil.copyfile(src, c["result"])
    sys.exit(0)
assert tool == "claude", tool
Path(c["argv"]).write_text(json.dumps(a))
stdout, stderr = (base64.b64decode(c[key]) for key in ("stdout", "stderr"))
for fd, data in ((1, stdout), (2, stderr)):
    while data:
        n = os.write(fd, data)
        data = data[n:]
    if fd == 1 and stdout and stderr:
        # Make the control's cross-channel order causal, rather than depending on
        # which pipe the local event loop happens to read first under load.
        deadline = time.monotonic() + 5
        while not Path(c["stdout_ack"]).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("fixture stdout was not observed")
            time.sleep(0.005)
if c["interrupt"]:
    while True:
        signal.pause()
sys.exit(c["exit"])
'''


class _LocalShell:
    """Replace transport only; execute the actual command and deliver real bytes."""

    def __init__(self, root, config, env, fragment_size, echo):
        self.root, self.config, self.env = root, config, env
        self.fragment_size, self.echo = fragment_size, echo
        self.queue = asyncio.Queue()
        self.payloads = []
        self.proc = None
        self.exit_code = None
        self.paths = []

    async def __aenter__(self):
        return self

    async def send(self, command):
        self.command = command
        match = re.search(
            r"B1=(__ROLE_RUN_BEGIN__-([0-9a-f]{12})); E1=(__ROLE_RUN_END__-[0-9a-f]{12});",
            command,
        )
        assert match
        self.begin, nonce, self.end = match.groups()
        self.paths = [
            Path(f"/tmp/workshop-{nonce}"), Path(f"/tmp/workshop-seed-{nonce}"),
            Path(f"/tmp/workshop-source-{nonce}.tar.gz"),
            Path(f"/tmp/workshop-result-{nonce}.tar.gz"),
        ]
        assert all(not p.exists() and not p.is_symlink() for p in self.paths)
        if self.echo:
            await self.queue.put(ShellFrame(
                ShellChannel.STDOUT, 1, ("outside output\n" + command).encode()))
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", command, cwd=self.root, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.pumps = [
            asyncio.create_task(self._pump(self.proc.stdout, ShellChannel.STDOUT)),
            asyncio.create_task(self._pump(self.proc.stderr, ShellChannel.STDERR)),
        ]
        self.waiter = asyncio.create_task(self._finish())

    async def _pump(self, stream, channel):
        observed = b""
        while data := await stream.read(4096):
            self.payloads.append((channel, data))
            observed += data
            for offset in range(0, len(data), self.fragment_size):
                await self.queue.put(ShellFrame(
                    channel, int(channel), data[offset:offset + self.fragment_size]))
            if channel == ShellChannel.STDOUT:
                expected = base64.b64decode(self.config["stdout"])
                if expected and expected in observed:
                    Path(self.config["stdout_ack"]).touch()
            if self.config["interrupt"] and b"fixture interrupt ready" in observed:
                os.killpg(self.proc.pid, signal.SIGTERM)  # This fixture's group only.

    async def _finish(self):
        self.returncode = await self.proc.wait()
        await asyncio.gather(*self.pumps)
        if self.config["interrupt"]:
            await self.queue.put(ShellFrame(ShellChannel.CLOSE, 255, b""))
            return
        self.exit_code = self.returncode
        status = (
            {"status": "Success"} if self.returncode == 0 else
            {"status": "Failure", "details": {"causes": [
                {"reason": "ExitCode", "message": str(self.returncode)},
            ]}}
        )
        await self.queue.put(ShellFrame(
            ShellChannel.STATUS, 3, json.dumps(status).encode()))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def __aexit__(self, *args):
        if self.proc and self.proc.returncode is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            await self.proc.wait()
        if hasattr(self, "waiter"):
            await self.waiter
        for p in self.paths:
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.exists():
                shutil.rmtree(p)
        assert all(not p.exists() and not p.is_symlink() for p in self.paths)

    @property
    def raw(self):
        return "".join(data.decode("utf-8", "replace") for _, data in self.payloads)


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    real_tar = shutil.which("tar")
    assert real_tar and shutil.which("git") and Path("/bin/sh").exists()
    for name in ("bin", "home", "tmp"):
        (tmp_path / name).mkdir()
    for name in ("aws", "claude", "tar"):
        executable = tmp_path / "bin" / name
        executable.write_text(f"#!{sys.executable}\n" + _FIXTURE_EXECUTABLE)
        executable.chmod(0o700)
    seed = tmp_path / "seed.tar.gz"
    with tarfile.open(seed, "w:gz") as archive:
        data = b"Offline fixture source; no credentials or history.\n"
        entry = tarfile.TarInfo("README.md")
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    monkeypatch.setattr(identity_baggage, "get_current_identity",
                        lambda: identity_baggage.ANONYMOUS)
    monkeypatch.delenv("PERUSER_ROLE_ARN", raising=False)
    monkeypatch.setattr(runtime_stage, "archive_uri", lambda *a: "s3://offline-fixture/source.tar.gz")
    monkeypatch.setattr(runtime_stage, "skills_archive_uri", lambda *a: None)

    def forbid_network(*args, **kwargs):
        raise AssertionError("Framing tests must not use a network client")

    monkeypatch.setattr("socket.socket.connect", forbid_network)
    monkeypatch.setattr("socket.socket.connect_ex", forbid_network)
    monkeypatch.setattr("socket.create_connection", forbid_network)
    shells = []

    def run(stdout=b"", stderr=b"", exit_code=0, *, interrupt=False,
            hydrate_fd=0, fragment_size=4096, echo=False):
        config = {
            "stdout": base64.b64encode(stdout).decode(),
            "stderr": base64.b64encode(stderr).decode(), "exit": exit_code,
            "interrupt": interrupt, "hydrate_fd": hydrate_fd,
            "seed": str(seed), "result": str(tmp_path / "result.tar.gz"),
            "archive_uri": "s3://offline-fixture/source.tar.gz",
            "argv": str(tmp_path / "cli-argv.json"),
            "stdout_ack": str(tmp_path / "stdout-ack"), "tar": real_tar,
        }
        (tmp_path / "case.json").write_text(json.dumps(config))
        env = {
            "PATH": str(tmp_path / "bin") + ":/usr/bin:/bin",
            "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"),
            "FRAMING_CASE": str(tmp_path / "case.json"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1", "AWS_EC2_METADATA_DISABLED": "true",
        }

        class Client:
            def open_shell(self, **bindings):
                shell = _LocalShell(tmp_path, config, env, fragment_size, echo)
                shell.bindings = bindings
                shells.append(shell)
                return shell

        monkeypatch.setattr(runtime_exec, "_client", lambda region: Client())
        return runtime_exec._dispatch_once(
            "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/offline-fixture",
            "claude-code", "Emit only the offline fixture.", "run_framing/work/backend",
            None, "offline-fixture-model", "us-west-2", lambda line: None, 10.0,
        )

    run.shells = shells
    run.argv = tmp_path / "cli-argv.json"
    run.result_archive = tmp_path / "result.tar.gz"
    return run


@pytest.mark.parametrize(
    "name,stdout,stderr,exit_code,expected", _OUTPUT_CASES,
    ids=[case[0] for case in _OUTPUT_CASES],
)
def test_generated_shell_preserves_output_and_exit(dispatch, name, stdout, stderr,
                                                  exit_code, expected):
    result = dispatch(stdout, stderr, exit_code)
    shell = dispatch.shells[0]
    assert result["exit"] == shell.returncode == exit_code
    assert result["transcript"] == expected
    assert b"".join(data for channel, data in shell.payloads
                    if channel == ShellChannel.STDERR) == stderr
    assert result["session_id"] == shell.bindings["session_id"]
    assert result["session_id"] != shell.bindings["shell_id"]
    argv = json.loads(dispatch.argv.read_text())
    assert argv.count("--print") == 1
    assert argv[argv.index("--max-turns") + 1] == "50"
    assert "--output-format" not in argv
    assert dispatch.result_archive.exists()  # Nonzero CLI exits still upload.
    assert runtime_exec.run_window_marker(shell.end) == "end"


def test_interrupted_shell_does_not_invent_an_end_marker_or_exit(dispatch):
    with pytest.raises(runtime_exec.RoleExecutionError, match="without a command exit code"):
        dispatch(b"fixture interrupt ready\n", interrupt=True)
    shell = dispatch.shells[0]
    assert shell.returncode == -signal.SIGTERM
    assert shell.end not in shell.raw
    assert runtime_exec._slice(shell.raw, shell.begin, shell.end) == ""
    assert shell.exit_code is None


@pytest.mark.parametrize("channel_fd", [1, 2], ids=["stdout", "stderr"])
def test_unterminated_hydration_failure_retains_reason_and_exit(dispatch, channel_fd):
    result = dispatch(hydrate_fd=channel_fd)
    assert result["exit"] == dispatch.shells[0].returncode == 19
    assert result["transcript"] == "fixture hydration error"
    assert not dispatch.argv.exists()
    assert not dispatch.result_archive.exists()


@pytest.mark.parametrize("fragment_size", [1, 7])
def test_split_frames_and_command_echo_keep_the_exact_output_window(dispatch, fragment_size):
    result = dispatch(b"fixture stdout\n", fragment_size=fragment_size, echo=True)
    shell = dispatch.shells[0]
    assert result["exit"] == 0
    assert result["transcript"] == "fixture stdout"
    assert runtime_exec._slice(
        shell.command + shell.raw + "outside trailing output\n", shell.begin, shell.end,
    ) == "fixture stdout"
