"""Open an interactive or one-shot shell on a role's deployed AgentCore Runtime.

Every coding-agents/<role>/connect.py uses this: it keeps its own arguments and the
command it launches (``/app/run.sh`` with that role's prompt form), and this module
owns the shared WebSocket shell plumbing. The region always comes from the Runtime
ARN in ``runtime_config.json``: four connectors used to default to a literal
``us-west-2``, which fails for every Runtime deployed in another region.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import termios
import tty
import uuid
from typing import Callable

from bedrock_agentcore.runtime import AgentCoreRuntimeClient
from bedrock_agentcore.runtime.shell import ShellChannel, ShellSession


def load_config(script_dir: str) -> dict:
    config_path = os.path.join(script_dir, "runtime_config.json")
    try:
        with open(config_path) as f:
            return json.load(f)
    except FileNotFoundError:
        print("Error: runtime_config.json not found. Run deploy.py first.")
        sys.exit(1)


def runtime_region(runtime_arn: str) -> str:
    """The region in the Runtime's own ARN; open_shell rejects any other."""
    parts = runtime_arn.split(":")
    if len(parts) < 6 or not parts[3]:
        raise SystemExit("Invalid Runtime ARN in runtime_config.json")
    return parts[3]


async def interactive_pty(shell: ShellSession, initial_cmd: str | None = None):
    """Full interactive PTY: forward local stdin to shell, shell output to stdout."""
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin.fileno())

        cols, rows = os.get_terminal_size()
        await shell.resize(cols, rows)

        if initial_cmd:
            await shell.send(initial_cmd)

        loop = asyncio.get_event_loop()
        stdin_fd = sys.stdin.fileno()
        input_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def on_stdin_ready():
            try:
                data = os.read(stdin_fd, 4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                loop.remove_reader(stdin_fd)
                input_queue.put_nowait(None)
                return
            if data:
                input_queue.put_nowait(data)
            else:
                loop.remove_reader(stdin_fd)
                input_queue.put_nowait(None)

        async def read_stdin():
            while True:
                data = await input_queue.get()
                if data is None:
                    return
                await shell.send_bytes(data)

        # Read stdin from the event loop, NOT an executor thread. A blocking
        # os.read() parked in the default executor cannot be cancelled, so closing
        # the TUI would leave that thread holding process exit open.
        loop.add_reader(stdin_fd, on_stdin_ready)
        stdin_task = asyncio.create_task(read_stdin())

        try:
            async for frame in shell:
                if frame.channel == ShellChannel.STDOUT:
                    os.write(sys.stdout.fileno(), frame.payload)
                elif frame.channel == ShellChannel.STDERR:
                    os.write(sys.stderr.fileno(), frame.payload)
                elif frame.channel == ShellChannel.STATUS:
                    break
                elif frame.channel == ShellChannel.CLOSE:
                    break
        finally:
            loop.remove_reader(stdin_fd)
            stdin_task.cancel()
            try:
                await stdin_task
            except asyncio.CancelledError:
                pass

    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        print()


async def stream_output(shell: ShellSession, initial_cmd: str):
    """Read output from shell until the command finishes."""
    await shell.send(initial_cmd)

    async for frame in shell:
        if frame.channel == ShellChannel.STDOUT:
            print(frame.text, end="", flush=True)
        elif frame.channel == ShellChannel.STDERR:
            print(frame.text, end="", file=sys.stderr, flush=True)
        elif frame.channel == ShellChannel.STATUS:
            break
        elif frame.channel == ShellChannel.CLOSE:
            break


async def run(args, *, script_dir: str, label: str, model_flag: str,
              prompt_command: Callable[[str, str], str],
              open_attempts: int = 1, retry_seconds: float = 5):
    """Open the shell and run ``--prompt``, ``--cmd``, or the role's interactive TUI.

    ``open_attempts`` > 1 retries only while the Runtime has not yet accepted a
    shell (a freshly deployed Runtime can still be warming); a failure after the
    shell opened is raised as is.
    """
    config = load_config(script_dir)
    runtime_arn = config["runtime_arn"]
    session_id = args.session or str(uuid.uuid4())
    client = AgentCoreRuntimeClient(region=runtime_region(runtime_arn))

    # Status banners go to STDERR so a `--cmd` run can be redirected
    # (`connect.py --cmd "cat file" > out`) and capture ONLY the command's STDOUT.
    print("Connecting to AgentCore Runtime...", file=sys.stderr)
    print(f"  Runtime: {runtime_arn}", file=sys.stderr)
    print(f"  Session: {session_id}", file=sys.stderr)
    print(file=sys.stderr)

    async def use(shell):
        if args.prompt:
            print(f"Running prompt: {args.prompt}\n", file=sys.stderr)
            await stream_output(shell, prompt_command(args.prompt, model_flag))
        elif args.cmd:
            print(f"Running command: {args.cmd}\n", file=sys.stderr)
            await stream_output(shell, f"{args.cmd}; exit\n")
        else:
            print(f"Connected! Launching {label}...\n", file=sys.stderr)
            await interactive_pty(shell, f"/app/run.sh{model_flag}\n")

    for attempt in range(1, open_attempts + 1):
        opened = False
        try:
            async with client.open_shell(runtime_arn=runtime_arn, session_id=session_id,
                                         shell_id=str(uuid.uuid4())) as shell:
                opened = True
                await use(shell)
        except (TimeoutError, OSError) as error:
            if opened or open_attempts == 1:
                raise
            if attempt == open_attempts:
                raise RuntimeError(
                    f"Runtime did not accept a command shell after {open_attempts} attempts."
                ) from error
            print(f"Runtime is still warming (attempt {attempt}/{open_attempts}); "
                  f"retrying in {retry_seconds}s.", file=sys.stderr)
            await asyncio.sleep(retry_seconds)
            continue
        break

    print(f"\nTo reconnect: python connect.py --session {session_id}", file=sys.stderr)


def main(args, **options) -> None:
    try:
        asyncio.run(run(args, **options))
    except KeyboardInterrupt:
        print("\nDisconnecting...")
