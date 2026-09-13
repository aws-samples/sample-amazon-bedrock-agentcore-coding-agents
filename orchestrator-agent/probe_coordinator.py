#!/usr/bin/env python3
"""Require an actual answer from the deployed coordinator before declaring success.

AgentCore CLI 0.29 can return exit code zero after a streamed model error. Its
structured result and response must therefore be checked as well as its exit code.
This probe asks a question; it does not dispatch or grade a build.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

PROMPT = (
    "Call list_presets and tell me which roles the add-a-feature preset routes to. "
    "Do not dispatch anything."
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ERROR_LINE = re.compile(r"(?im)^\s*(?:error:|error\b.*exception|traceback \(most recent call last\))")


class ProbeFailure(RuntimeError):
    pass


def read_result(stdout: str, returncode: int) -> dict:
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError) as error:
        raise ProbeFailure("The CLI did not return a JSON probe result.") from error
    if not isinstance(result, dict):
        raise ProbeFailure("The CLI returned an unexpected probe result.")
    if returncode or result.get("success") is not True:
        reason = result.get("error") or result.get("message") or f"CLI exit status {returncode}"
        raise ProbeFailure(f"The deployed coordinator did not answer: {reason}")
    response = result.get("response")
    if not isinstance(response, str) or not response.strip():
        raise ProbeFailure("The deployed coordinator returned no text answer.")
    clean = ANSI.sub("", response)
    if ERROR_LINE.search(clean):
        raise ProbeFailure(f"The deployed coordinator returned an error: {clean.strip()}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    try:
        completed = subprocess.run(
            ["agentcore", "invoke", "--runtime", "orchestrator", "--json", PROMPT],
            cwd=args.project, capture_output=True, text=True, timeout=180,
        )
        if completed.stderr.strip():
            print(completed.stderr, end="", file=sys.stderr)
        result = read_result(completed.stdout, completed.returncode)
    except subprocess.TimeoutExpired:
        print("ERROR: Coordinator probe did not finish within 180 seconds.", file=sys.stderr)
        return 1
    except (OSError, ProbeFailure) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print("Inspect the probe error before submitting a build.", file=sys.stderr)
        return 1
    print(result["response"])
    if result.get("sessionId"):
        print(f"\nSession: {result['sessionId']}")
    if result.get("logFilePath"):
        print(f"Log: {result['logFilePath']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
