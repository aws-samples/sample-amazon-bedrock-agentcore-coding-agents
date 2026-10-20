#!/usr/bin/env bash
# Open a terminal inside a deployed agent's microVM, or come back to the one you left.
#
#   ./connect.sh                       # Claude Code: a new session, a new microVM
#   ./connect.sh --resume              # back to the shell you detached from (Ctrl+])
#   ./connect.sh --stop                # end that session now instead of waiting for it to idle out
#   ./connect.sh codex                 # another agent; any `agentcore exec` option passes through
#
# Ctrl+] detaches and leaves the agent running in its microVM; the CLI prints the exact
# reconnect command. This script keeps that command in ~/.agentcore/last-<role> so
# `--resume` works from any new terminal on this host. The same command works from any
# machine with the AWS CLI credentials for this account and the AgentCore CLI.
set -euo pipefail

role="claude-code"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then role="$1"; shift; fi
here="$(cd "$(dirname "$0")" && pwd)"
config="$here/$role/runtime_config.json"
if [ ! -f "$config" ]; then
  echo "No $role Runtime yet. Create it first:  ./deploy-prebuilt.sh $role" >&2
  exit 1
fi
arn="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["runtime_arn"])' "$config")"
region="$(printf '%s' "$arn" | cut -d: -f4)"
state_dir="$HOME/.agentcore"; mkdir -p "$state_dir"
last="$state_dir/last-$role"

args=()
if [ "${1:-}" = "--stop" ]; then
  if [ ! -s "$last" ]; then
    echo "No recorded $role session to stop." >&2
    exit 0
  fi
  # shellcheck disable=SC1090
  . "$last"
  if out="$(aws bedrock-agentcore stop-runtime-session --agent-runtime-arn "$arn" \
      --runtime-session-id "$SESSION_ID" --region "$region" 2>&1)"; then
    echo "Stopped session $SESSION_ID."
  elif printf '%s' "$out" | grep -q ResourceNotFound; then
    echo "Session $SESSION_ID had already ended."
  else
    echo "Could not stop session $SESSION_ID: $out" >&2
    exit 1
  fi
  rm -f "$last"
  exit 0
fi
if [ "${1:-}" = "--resume" ]; then
  shift
  if [ ! -s "$last" ]; then
    echo "Nothing to resume yet: connect, start work, then press Ctrl+] to detach." >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  . "$last"
  echo "Resuming session $SESSION_ID, shell $SHELL_ID"
  args=(--session-id "$SESSION_ID" --shell-id "$SHELL_ID")
elif [ "${1:-}" != "--session-id" ]; then
  # A session ID is the microVM's identity. Choose it here so it can be recorded.
  args=(--session-id "$(python3 -c 'import uuid; print(uuid.uuid4())')")
fi

# The CLI prints status lines (including "[to reconnect:]") on stderr. Keep a copy so
# the reconnect command survives this terminal; stdin and stdout stay the real TTY.
log="$state_dir/exec-$role.log"
: > "$log"
set +e
agentcore exec --it --runtime "$arn" --region "$region" "${args[@]}" "$@" 2> >(tee "$log" >&2)
status=$?
set -e
sleep 0.2
session="$(sed -n 's/.*--session-id \([^ ]*\).*/\1/p' "$log" | tail -1)"
shell="$(sed -n 's/.*--shell-id \([^ ]*\).*/\1/p' "$log" | tail -1)"
if [ -n "$session" ] && [ -n "$shell" ]; then
  printf 'SESSION_ID=%s\nSHELL_ID=%s\n' "$session" "$shell" > "$last"
  touch "$state_dir/detached-$role"   # progress.py: this person has detached at least once
  if [ "$role" = claude-code ]; then hint="./connect.sh --resume"; else hint="./connect.sh $role --resume"; fi
  echo "Your agent keeps running in its microVM. Come back any time with:  $hint" >&2
  # AgentCore CLI 0.31.1 ends a Ctrl+] detach with "Shell connection closed unexpectedly
  # before the shell reported an exit status" and exit 1, after printing the reconnect
  # command (0.30.0 exited 0). The session keeps running and the reconnect works
  # (measured October 19-20), so a detach is a success.
  if [ "$status" -ne 0 ] \
      && grep -q 'closed unexpectedly before the shell reported an exit status' "$log"; then
    echo "(The 'Shell connection closed unexpectedly' line is how the CLI reports a detach.)" >&2
    status=0
  fi
fi
exit "$status"
