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
#   - The runtime IAM role is authorized to read its credential provider
#
# Authentication methods (tried in order):
#   1. KIRO_API_KEY from AgentCore Identity Token Vault (headless)
#   2. Explicit /app/run.sh login: device flow for a hand-opened shell
#
# Usage (from connect.py):
#   /app/run.sh                         # interactive kiro-cli
#   /app/run.sh chat "fix the bug"      # non-interactive command
#   /app/run.sh login                   # force re-login via device-flow
# ============================================================
set -euo pipefail

# AgentCore exec shells may omit the image's virtual environment from PATH.
# Use its installed SDK; retain PATH-based Python for host/test invocations.
KIRO_PYTHON="/opt/workshop-python/bin/python3"
if [ ! -x "$KIRO_PYTHON" ]; then
  KIRO_PYTHON=python3
fi

# Command shells can omit Runtime environment variables. Recover only Kiro's
# named model controls, preserving explicit values, including an empty effort.
for setting in WORKSHOP_KIRO_MODEL WORKSHOP_KIRO_EFFORT; do
  if [ "${!setting+x}" != x ] && [ -r /proc/1/environ ]; then
    while IFS= read -r -d '' entry; do
      if [[ "$entry" == "$setting="* ]]; then
        export "$entry"
        break
      fi
    done < /proc/1/environ
  fi
done

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
  "$KIRO_PYTHON" -W ignore -c "
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
    echo "[auth] KIRO_API_KEY retrieved successfully (API-key headless mode)"
  else
    echo "[auth] WARNING: Could not retrieve KIRO_API_KEY (no env var, no Token Vault provider)"
  fi
fi

# ── Parse --model flag ───────────────────────────────────────
# The validator gets the STRONGEST model on the roster, not the `auto` router, and it
# is wirable for the same reason every other role's model is: an account without Opus
# access must be able to run the workshop by exporting one variable.
#
# `auto` was the old default and it is the wrong default HERE. The checker's whole job
# is to reason about a deliverable nobody pinned an answer for, and `auto` picks by
# task for "optimal usage", i.e. it may route a gate decision to a cheap model. The
# other roles already pin Opus-class models explicitly (WORKSHOP_CLAUDE_MODEL), so
# leaving the checker on a router made it the only role whose model nobody chose.
#
# The id is kiro-cli's OWN vendor name, not a Bedrock inference profile: verified with
# `kiro-cli chat --list-models` inside a live Runtime, which lists `claude-opus-5`
# (2.20x credits, 1M context) alongside `auto`, `claude-sonnet-5`, `claude-opus-4.8`
# and the rest. A Bedrock model id here would be silently rejected by the CLI.
MODEL="${WORKSHOP_KIRO_MODEL:-claude-opus-5}"
EFFORT="${WORKSHOP_KIRO_EFFORT-}"
REMAINING_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --model)
      MODEL="$2"
      shift 2
      ;;
    --effort)
      if [ "$#" -lt 2 ]; then
        echo "Error: --effort requires a value" >&2
        exit 2
      fi
      EFFORT="$2"
      shift 2
      ;;
    *)
      REMAINING_ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${REMAINING_ARGS[@]}"
KIRO_CHAT_ARGS=(--trust-all-tools)
case "$EFFORT" in
  "") ;;
  low|medium|high|xhigh|max) KIRO_CHAT_ARGS+=(--effort "$EFFORT") ;;
  *)
    echo "Error: WORKSHOP_KIRO_EFFORT must be low, medium, high, xhigh, max, or empty" >&2
    exit 2
    ;;
esac

mkdir -p "$HOME/.kiro/settings"
# chat.disableTrustAllConfirmation suppresses the one-time interactive "Kiro is
# running in trust all tools mode" acceptance prompt, so the headless PTY starts
# straight into work instead of hanging on a "Yes, I accept" picker. Paired with
# the `chat --trust-all-tools` launch below.
"$KIRO_PYTHON" "${WORKSHOP_CLI_HELPER:-/opt/workshop-cli/cli_versions.py}" configure \
  --cli kiro --home "$HOME" --kiro-model "$MODEL" >/dev/null

# ── Determine the action ─────────────────────────────────────
ACTION="${1:-interactive}"
shift 2>/dev/null || true
PROMPT="$*"

# ── Choose the working directory ────────────────────────────
# Kiro also discovers AGENTS.md in its project. Starting on the shared mount
# therefore imports the frontend's role instructions into the checker (observed
# in a real Lab 1 response). Refresh ONLY Kiro's staged steering into its private
# home, then start there. The shared notes remain available at /mnt/s3files.
# An explicit dispatched worktree already has its own role context and wins.
if [ -n "${WORKSHOP_AGENT_WORKDIR:-}" ]; then
  RUN_DIR="$WORKSHOP_AGENT_WORKDIR"
else
  if [ -f /mnt/s3files/.kiro/steering/validator.md ]; then
    mkdir -p "$HOME/.kiro/steering"
    cp /mnt/s3files/.kiro/steering/validator.md "$HOME/.kiro/steering/validator.md"
  fi
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
# method" picker and hang the headless PTY. The attendee updates Token Vault
# through the hidden prompt in Lab 1; never recommend putting a plaintext key
# in Runtime settings.
if [ -z "$KIRO_API_KEY" ]; then
  echo "[auth] ERROR: no Kiro API key in Token Vault. Re-run Lab 1's hidden key prompt to update the kiro-api-key credential provider, then open a new session. Do not put the key in Runtime environment variables." >&2
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
    exec kiro-cli chat "${KIRO_CHAT_ARGS[@]}"
    ;;
  chat)
    exec kiro-cli chat --no-interactive "${KIRO_CHAT_ARGS[@]}" "$PROMPT"
    ;;
  *)
    exec kiro-cli "$ACTION" "$PROMPT"
    ;;
esac
