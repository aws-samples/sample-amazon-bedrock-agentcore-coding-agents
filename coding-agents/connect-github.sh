#!/usr/bin/env bash
# Connect one GitHub repository so an agent wakes up on every pull request, and the
# coordinator's team on a /develop comment on an issue.
#
#   ./connect-github.sh owner/repository
#   ./connect-github.sh --restart          # start the GitHub App setup over (e.g. after Confirm access)
#
# 1. Registers a GitHub App for this workshop (two clicks on GitHub: Create, Install).
#    It subscribes to pull request and issue comment events and sends them to the
#    endpoint you deployed.
# 2. Stores the App's credential and deploys the GitHub tools the review uses. The
#    credential stays with those tools; no agent session ever receives it.
# 3. Prints the one thing left to do: open a pull request.
#
# Safe to run again: a finished App setup is reused, and the deploy updates in place.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
restart=()
if [ "${1:-}" = "--restart" ]; then restart=(--restart); shift; fi
repo="${1:-${GITHUB_REPO:-}}"
if [ -z "$repo" ]; then
  read -r -p "Your GitHub repository (owner/name): " repo
fi
export GITHUB_REPO="$repo"

cd "$here/gateway_mcp"
if [ ! -f github-app.env ] || [ ${#restart[@]} -gt 0 ]; then
  python3 create-github-app.py "${restart[@]}" --repo "$repo"
fi
# shellcheck disable=SC1091
. ./github-app.env
if [ -z "${GITHUB_APP_WEBHOOK_SECRET:-}" ]; then
  echo "This App was created without the review webhook, so pull requests will not wake an agent." >&2
  echo "Start over with: rm gateway_mcp/github-app.env && python3 gateway_mcp/create-github-app.py --restart --repo $repo" >&2
  exit 1
fi
./deploy-all.sh

# Save the repository and the Gateway for every later terminal. This script's export
# ends with it, and github.py doctor and the coordinator (Lab 3, Deploy the Coordinator)
# read the saved values.
GATEWAY_URL="$(jq -r '.gateway_url // empty' .deployed-state.json)" \
PYTHONPATH="$here/../orchestrator${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import os
import github
result = github.save_settings(repo=os.environ["GITHUB_REPO"],
                              gateway_url=os.environ.get("GATEWAY_URL") or None)
if result.get("error"):
    raise SystemExit("Could not save the GitHub settings: " + result["error"])
PY

echo
echo "Connected. An agent now wakes up for every pull request in $repo."
echo "Open one on GitHub (edit any file in the web editor and choose 'Create a new branch"
echo "and start a pull request'), then watch the pull request's conversation."
