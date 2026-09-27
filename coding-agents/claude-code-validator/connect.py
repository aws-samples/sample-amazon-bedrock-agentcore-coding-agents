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

The region comes from the Runtime ARN in runtime_config.json. The shared shell
plumbing lives in coding-agents/runtime_connect.py.
"""

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))
import runtime_connect  # noqa: E402


def prompt_command(prompt: str, model_flag: str) -> str:
    safe_prompt = prompt.replace("'", "'\\''")
    return f"/app/run.sh{model_flag} '{safe_prompt}'; exit\n"


def main():
    parser = argparse.ArgumentParser(description="Connect to Claude Code on AgentCore via WebSocket PTY")
    parser.add_argument("--session", help="Runtime session ID (reuse same microVM)")
    parser.add_argument("--prompt", help="Run a prompt in headless mode (one-shot, exits when done)")
    parser.add_argument("--cmd", help="Run a raw shell command on the microVM")
    parser.add_argument("--model", help="Model ID to pass to run.sh (e.g. global.anthropic.claude-opus-4-6-v1)")
    args = parser.parse_args()
    runtime_connect.main(args, script_dir=SCRIPT_DIR, label="Claude Code",
                         model_flag=f" --model {args.model}" if args.model else "",
                         prompt_command=prompt_command, open_attempts=6)


if __name__ == "__main__":
    main()
