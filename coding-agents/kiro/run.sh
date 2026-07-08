#!/usr/bin/env bash
# ============================================================
# Kiro CLI launcher for AgentCore Runtime (headless, no browser)
# ============================================================
# This script is baked into the container image and called by connect.py.
# The container already runs as the agent user (USER agent in Dockerfile).
#
# Security model:
#   - The KIRO_API_KEY is fetched ON-DEMAND from Token Vault using the
#     runtime's IAM role (GetWorkloadAccessToken + GetResourceApiKey)
#   - The key never touches disk; it lives only in this shell's memory
#   - Each new PTY session fetches a fresh key (rotation-friendly)
#   - The runtime IAM role is the only principal that can read the key
#
# Authentication methods (tried in order):
#   1. KIRO_API_KEY from AgentCore Identity Token Vault (Pro+ headless)
#   2. Fallback: device-flow login (prints URL + code for browser auth)
#
# Usage (from connect.py):
#   /app/run.sh                         # interactive kiro-cli
#   /app/run.sh chat "fix the bug"      # non-interactive command
#   /app/run.sh login                   # force re-login via device-flow
# ============================================================
set -euo pipefail

# Inherit env vars from PID 1 (container entrypoint) if not already set
if [ -z "${GATEWAY_URL:-}" ] && [ -r /proc/1/environ ]; then
  GATEWAY_URL=$(cat /proc/1/environ | tr '\0' '\n' | grep ^GATEWAY_URL= | cut -d= -f2- || true)
  export GATEWAY_URL
fi
if [ -z "${AWS_REGION:-}" ] && [ -r /proc/1/environ ]; then
  AWS_REGION=$(cat /proc/1/environ | tr '\0' '\n' | grep ^AWS_REGION= | cut -d= -f2- || true)
  export AWS_REGION
fi

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
export AWS_REGION="${AWS_REGION:-$AWS_DEFAULT_REGION}"
export HOME="/home/agent"

# ── Configure MCP Gateway (needs GATEWAY_URL from runtime env) ──
if [ -n "${GATEWAY_URL:-}" ]; then
  mkdir -p "$HOME/.kiro/settings"
  cat > "$HOME/.kiro/settings/mcp.json" <<MCPEOF
{
  "mcpServers": {
    "gateway": {
      "command": "node",
      "args": ["/mnt/s3files/mcp/index.js", "--gateway-url", "${GATEWAY_URL}", "--region", "${AWS_REGION}"],
      "autoApprove": ["*"]
    }
  }
}
MCPEOF
  echo "[mcp] Gateway configured: ${GATEWAY_URL}"
fi

WORKLOAD_NAME="${AGENTCORE_WORKLOAD_NAME:-kiro-coding-agent}"
CREDENTIAL_PROVIDER="${AGENTCORE_CREDENTIAL_PROVIDER:-kiro-api-key}"

# ── Resolve KIRO_API_KEY (Token Vault identity path) ───
# Kiro's only headless auth is the API key (ksk_...); device flow needs a human in
# a browser, so it is never the automated path. The key is fetched at session start
# from the AgentCore Identity Token Vault (the workload identity + api-key
# credential provider that kiro/setup.sh created, KMS-encrypted in Secrets Manager),
# using the runtime's OWN role. deploy.py deliberately does NOT inject the key as a
# runtime environment variable, because a plaintext env var is readable by anyone
# who can GetAgentRuntime and would leak the key. The KIRO_API_KEY-in-environment
# branch below stays only as an escape hatch for a hand-run local test where an
# operator exports it themselves; the shipped runtime never has it set.
fetch_api_key() {
  python3 -W ignore -c "
import boto3, sys, warnings
warnings.filterwarnings('ignore')

from botocore.config import Config

config = Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 2})
client = boto3.client('bedrock-agentcore', region_name='${AWS_DEFAULT_REGION}', config=config)

try:
    token = client.get_workload_access_token(workloadName='${WORKLOAD_NAME}')['workloadAccessToken']
    key = client.get_resource_api_key(
        workloadIdentityToken=token,
        resourceCredentialProviderName='${CREDENTIAL_PROVIDER}'
    )['apiKey']
    print(key, end='')
except Exception as e:
    print(f'[identity] Failed to fetch key: {e}', file=sys.stderr)
"
}

if [ -n "${KIRO_API_KEY:-}" ]; then
  echo "[auth] Using KIRO_API_KEY from the runtime environment"
  export KIRO_API_KEY
else
  echo "[auth] Fetching KIRO_API_KEY from AgentCore Identity Token Vault..."
  KIRO_API_KEY="$(fetch_api_key)"
  export KIRO_API_KEY
  if [ -n "$KIRO_API_KEY" ]; then
    echo "[auth] KIRO_API_KEY retrieved successfully (Pro+ headless mode)"
  else
    echo "[auth] WARNING: Could not retrieve KIRO_API_KEY (no env var, no Token Vault provider)"
  fi
fi

# ── Parse --model flag ───────────────────────────────────────
MODEL="auto"
REMAINING_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --model)
      MODEL="$2"
      shift 2
      ;;
    *)
      REMAINING_ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${REMAINING_ARGS[@]}"

mkdir -p "$HOME/.kiro/settings"
# chat.disableTrustAllConfirmation suppresses the one-time interactive "Kiro is
# running in trust all tools mode" acceptance prompt, so the headless PTY starts
# straight into work instead of hanging on a "Yes, I accept" picker. Paired with
# the `chat --trust-all-tools` launch below.
cat > "$HOME/.kiro/settings/cli.json" <<EOF
{
  "chat.defaultModel": "${MODEL}",
  "chat.disableTrustAllConfirmation": true
}
EOF

# ── Determine the action ─────────────────────────────────────
ACTION="${1:-interactive}"
shift 2>/dev/null || true
PROMPT="$*"

# ── Choose the working directory (mirror codex/run.sh) ───────
# The validator reads its role + contract from the working tree it starts in. The
# attendee stages `.kiro/steering/` and the graded module onto the shared mount, so
# run there when it exists: a relative path like `.kiro/steering/validator.md` then
# resolves to the STAGED file, not the image-baked ~/.kiro copy. An explicit per-run
# cwd from the orchestrator wins; HOME is the last resort (baked steering only).
if [ -n "${WORKSHOP_AGENT_WORKDIR:-}" ]; then
  RUN_DIR="$WORKSHOP_AGENT_WORKDIR"
elif [ -d /mnt/s3files ]; then
  RUN_DIR="/mnt/s3files"
else
  RUN_DIR="$HOME"
fi
cd "$RUN_DIR"

# ── Login flow (explicit only) ────────────────────────────────
# Device-flow login runs ONLY when the operator explicitly asks for it
# (/app/run.sh login). It is never the silent fallback: a browser-based picker
# in a headless PTY just hangs, which is the exact dead-end we are avoiding.
if [ "$ACTION" = "login" ]; then
  echo ""
  echo "Starting device-flow login. A URL and code will appear; open the URL in a browser."
  echo ""
  exec kiro-cli login --use-device-flow
fi

# ── Require an API key for non-interactive use ───────────────
# Without KIRO_API_KEY the CLI would drop into an interactive "Select login
# method" picker and hang the headless PTY. Fail loud with ONE actionable line
# instead of opening a browser login. Two real remediations:
#   1. Token Vault: create the workload identity + api-key credential provider
#      for this account so fetch_api_key() above succeeds, OR
#   2. pass KIRO_API_KEY as a runtime env var at deploy time.
if [ -z "$KIRO_API_KEY" ]; then
  echo "[auth] ERROR: no KIRO_API_KEY. Set it via Token Vault (create-workload-identity + create-api-key-credential-provider for this account) or pass KIRO_API_KEY at deploy time. Run '/app/run.sh login' for an interactive device-flow login." >&2
  exit 1
fi

# ── Launch kiro-cli ──────────────────────────────────────────
# Interactive launch goes through `kiro-cli chat --trust-all-tools`, NOT bare
# `kiro-cli`: bare kiro-cli rejects --trust-all-tools ("unexpected argument") and
# would stop on a per-tool approval prompt for every shell/file action, which a
# headless PTY cannot answer. `chat --trust-all-tools` (no --no-interactive) is the
# trusted INTERACTIVE TUI, so the validator runs straight through without prompts.
case "$ACTION" in
  interactive)
    exec kiro-cli chat --trust-all-tools
    ;;
  chat)
    exec kiro-cli chat --no-interactive --trust-all-tools "$PROMPT"
    ;;
  *)
    exec kiro-cli "$ACTION" "$PROMPT"
    ;;
esac
