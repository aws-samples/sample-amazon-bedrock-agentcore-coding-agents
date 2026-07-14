"""
Connect to Claude Code on AgentCore Runtime via WebSocket Shell.

This gives you an interactive terminal session on the microVM.
Use it to run `claude` interactively, just like a local terminal.

Usage:
    # New session (launches claude with --continue)
    python connect.py

    # Reuse an existing runtime session (same microVM)
    python connect.py --session <session-id>

    # Run a specific command instead of interactive claude
    python connect.py --cmd "ls /mnt/s3files/skills/"

Environment:
    AWS_REGION                                  (default: us-west-2)
"""

import argparse
import asyncio
import json
import os
import sys
import termios
import tty
import uuid

from bedrock_agentcore.runtime import AgentCoreRuntimeClient
from bedrock_agentcore.runtime.shell import ShellChannel, ShellSession


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REGION = os.environ.get("AWS_REGION", "us-west-2")
SHELL_OPEN_ATTEMPTS = 6
SHELL_OPEN_RETRY_SECONDS = 5


def load_config() -> dict:
    config_path = os.path.join(SCRIPT_DIR, "runtime_config.json")
    try:
        with open(config_path) as f:
            return json.load(f)
    except FileNotFoundError:
        print("Error: runtime_config.json not found. Run deploy.py first.")
        sys.exit(1)


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

        async def read_stdin():
            while True:
                data = await loop.run_in_executor(None, os.read, stdin_fd, 4096)
                if not data:
                    break
                await shell.send_bytes(data)

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


async def run(args):
    config = load_config()
    runtime_arn = config["runtime_arn"]

    session_id = args.session or str(uuid.uuid4())

    client = AgentCoreRuntimeClient(region=REGION)

    # Status banners go to STDERR so a `--cmd` run can be redirected
    # (`connect.py --cmd "cat file" > out`) and capture ONLY the command's STDOUT,
    # not these connection lines.
    print("Connecting to AgentCore Runtime...", file=sys.stderr)
    print(f"  Runtime: {runtime_arn}", file=sys.stderr)
    print(f"  Session: {session_id}", file=sys.stderr)
    print(file=sys.stderr)

    model_flag = f" --model {args.model}" if args.model else ""

    for attempt in range(1, SHELL_OPEN_ATTEMPTS + 1):
        opened = False
        try:
            async with client.open_shell(
                runtime_arn=runtime_arn,
                session_id=session_id,
                shell_id=str(uuid.uuid4()),
            ) as shell:
                opened = True
                if args.prompt:
                    safe_prompt = args.prompt.replace("'", "'\\''")
                    cmd = f"/app/run.sh{model_flag} '{safe_prompt}'; exit\n"
                    print(f"Running prompt: {args.prompt}\n", file=sys.stderr)
                    await stream_output(shell, cmd)
                elif args.cmd:
                    cmd = f"{args.cmd}; exit\n"
                    print(f"Running command: {args.cmd}\n", file=sys.stderr)
                    await stream_output(shell, cmd)
                else:
                    cmd = f"/app/run.sh{model_flag}\n"
                    print("Connected! Launching Claude Code...\n", file=sys.stderr)
                    await interactive_pty(shell, cmd)
        except (TimeoutError, OSError) as error:
            if opened:
                raise
            if attempt == SHELL_OPEN_ATTEMPTS:
                raise RuntimeError(
                    f"Runtime did not accept a command shell after "
                    f"{SHELL_OPEN_ATTEMPTS} attempts."
                ) from error
            print(
                f"Runtime is still warming (attempt {attempt}/"
                f"{SHELL_OPEN_ATTEMPTS}); retrying in "
                f"{SHELL_OPEN_RETRY_SECONDS}s.",
                file=sys.stderr,
            )
            await asyncio.sleep(SHELL_OPEN_RETRY_SECONDS)
            continue
        break

    print(f"\nTo reconnect: python connect.py --session {session_id}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Connect to Claude Code on AgentCore via WebSocket PTY")
    parser.add_argument("--session", help="Runtime session ID (reuse same microVM)")
    parser.add_argument("--prompt", help="Run a prompt in headless mode (one-shot, exits when done)")
    parser.add_argument("--cmd", help="Run a raw shell command on the microVM")
    parser.add_argument("--model", help="Model ID to pass to run.sh (e.g. global.anthropic.claude-opus-4-6-v1)")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nDisconnecting...")


if __name__ == "__main__":
    main()
