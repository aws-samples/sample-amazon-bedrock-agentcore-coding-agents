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

print_target_context() {
  local gateway_id target_id target_info target_status runtime_endpoint
  gateway_id=$(state_get "gateway_id")
  target_id=$(state_get "gateway_target_id")
  if [[ -z "$gateway_id" || -z "$target_id" ]]; then
    return
  fi

  target_info=$(aws bedrock-agentcore-control get-gateway-target \
    --gateway-identifier "$gateway_id" \
    --target-id "$target_id" \
    --region "$AWS_REGION" \
    --output json 2>/dev/null || true)
  if [[ -z "$target_info" ]]; then
    return
  fi

  target_status=$(echo "$target_info" |
    jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")
  runtime_endpoint=$(echo "$target_info" |
    jq -r '.targetConfiguration.mcp.mcpServer.endpoint // empty' 2>/dev/null || true)
  echo "Gateway target: ${target_id} (${target_status})" >&2
  if [[ -n "$runtime_endpoint" ]]; then
    echo "Runtime endpoint: ${runtime_endpoint}" >&2
  fi
}

is_runtime_421() {
  grep -Eqi \
    'Received error[[:space:]]*\(421\)|HTTP[^0-9]*421|Misdirected Request' \
    <<<"$1"
}

fail_runtime_421() {
  echo "ERROR: Gateway reached the MCP Runtime, but the Runtime returned HTTP 421." >&2
  print_target_context
  cat >&2 <<'EOF'
FastMCP 3.4+ Host/Origin protection rejected AgentCore's internal proxy Host.
This is not caused by the console GitHub repository setting. The Runtime image
must include host_origin_protection=False and be rebuilt; verify-gateway.sh alone
cannot update an already deployed image.

Run from coding-agents/gateway_mcp:
  ./deploy-runtime.sh
  ./deploy-gateway.sh
  ./verify-gateway.sh
EOF
}

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
    TOOL_NAMES=$(echo "$LAST_RESPONSE" |
      jq -r '.result.tools[]?.name' 2>/dev/null || true)
    if printf '%s\n' "$TOOL_NAMES" | grep -q '^GitHubMCP___'; then
      printf '  %s\n' "$TOOL_NAMES"
      echo "Gateway verification passed: GitHubMCP tools are discoverable."
      exit 0
    fi
  fi
  if is_runtime_421 "${LAST_RESPONSE}"$'\n'"$(cat "$LAST_ERROR")"; then
    fail_runtime_421
    if [[ -n "$LAST_RESPONSE" ]]; then
      echo "$LAST_RESPONSE" | jq . >&2 2>/dev/null || echo "$LAST_RESPONSE" >&2
    fi
    exit 1
  fi
  echo "  tools/list not ready (attempt ${attempt}/${VERIFY_ATTEMPTS}); waiting ${VERIFY_DELAY_SECONDS} seconds..."
  sleep "$VERIFY_DELAY_SECONDS"
done

echo "ERROR: Gateway tools/list never returned GitHubMCP___ tools." >&2
print_target_context
if [[ -n "$LAST_RESPONSE" ]]; then
  echo "$LAST_RESPONSE" | jq . >&2 2>/dev/null || echo "$LAST_RESPONSE" >&2
fi
if [[ -s "$LAST_ERROR" ]]; then
  cat "$LAST_ERROR" >&2
fi
exit 1
