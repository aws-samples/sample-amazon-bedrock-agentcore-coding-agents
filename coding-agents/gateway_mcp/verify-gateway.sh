#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

for command in awscurl jq; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: ${command} is required to verify the Gateway." >&2
    exit 1
  }
done

GATEWAY_URL=$(state_get "gateway_url")
if [[ -z "$GATEWAY_URL" ]]; then
  echo "ERROR: No gateway URL in ${STATE_FILE}. Run deploy-all.sh first." >&2
  exit 1
fi

echo "==> Verifying Gateway tools/list"
LAST_RESPONSE=""
LAST_ERROR=$(mktemp)
trap 'rm -f "$LAST_ERROR"' EXIT
VERIFY_ATTEMPTS="${GATEWAY_VERIFY_ATTEMPTS:-36}"
VERIFY_DELAY_SECONDS="${GATEWAY_VERIFY_DELAY_SECONDS:-5}"

for attempt in $(seq 1 "$VERIFY_ATTEMPTS"); do
  if LAST_RESPONSE=$(awscurl --service bedrock-agentcore --region "$AWS_REGION" \
    -X POST "$GATEWAY_URL" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","method":"tools/list","id":1,"params":{}}' \
    2>"$LAST_ERROR"); then
    TOOL_NAMES=$(echo "$LAST_RESPONSE" | jq -r '.result.tools[]?.name')
    if printf '%s\n' "$TOOL_NAMES" | grep -q '^GitHubMCP___'; then
      printf '  %s\n' "$TOOL_NAMES"
      echo "Gateway verification passed: GitHubMCP tools are discoverable."
      exit 0
    fi
  fi
  echo "  tools/list not ready (attempt ${attempt}/${VERIFY_ATTEMPTS}); waiting ${VERIFY_DELAY_SECONDS} seconds..."
  sleep "$VERIFY_DELAY_SECONDS"
done

echo "ERROR: Gateway tools/list never returned GitHubMCP___ tools." >&2
if [[ -n "$LAST_RESPONSE" ]]; then
  echo "$LAST_RESPONSE" | jq . >&2 2>/dev/null || echo "$LAST_RESPONSE" >&2
fi
if [[ -s "$LAST_ERROR" ]]; then
  cat "$LAST_ERROR" >&2
fi
exit 1
