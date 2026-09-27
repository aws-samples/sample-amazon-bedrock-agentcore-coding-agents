"""
Connect to Kiro on AgentCore Runtime via WebSocket Shell.

This gives you an interactive terminal session on the microVM.
Use it to run `kiro-cli` interactively, just like a local terminal.

Usage:
    # New session (launches kiro-cli interactive)
    python connect.py

    # Reuse an existing runtime session (same microVM)
    python connect.py --session <session-id>

    # Run a prompt in headless mode (one-shot, exits when done)
    python connect.py --prompt "Build a REST API"

    # Run a raw shell command on the microVM
    python connect.py --cmd "ls /mnt/s3files/"

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
    return f"/app/run.sh{model_flag} chat '{safe_prompt}'; exit\n"


def main():
    parser = argparse.ArgumentParser(description="Connect to Kiro on AgentCore via WebSocket PTY")
    parser.add_argument("--session", help="Runtime session ID (reuse same microVM)")
    parser.add_argument("--prompt", help="Run a prompt in headless mode (one-shot, exits when done)")
    parser.add_argument("--cmd", help="Run a raw shell command on the microVM")
    parser.add_argument("--model", help="Model ID to pass to run.sh (e.g. auto, claude-sonnet-4-5)")
    args = parser.parse_args()
    runtime_connect.main(args, script_dir=SCRIPT_DIR, label="Kiro",
                         model_flag=f" --model {args.model}" if args.model else "",
                         prompt_command=prompt_command)


if __name__ == "__main__":
    main()
