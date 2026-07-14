#!/usr/bin/env bash
# ============================================================
# opencode launcher for AgentCore Runtime (headless)
# ============================================================
# Runs opencode against Amazon Bedrock (the runtime's own region), authenticated
# by the AgentCore Runtime IAM role through the AWS SDK credential chain. No API
# key and no OpenAI/mantle path, so it is unaffected by GPT-5.x allowlisting.
#
# Usage:
#   /app/run.sh "Fix the bug in main.py"       # one-shot headless
#   /app/run.sh                                 # interactive TUI
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

# opencode runs against plain Bedrock in the runtime's own region (no mantle).
export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$AWS_REGION}"
export HOME="/home/agent"
CONFIG_DIR="$HOME/.config/opencode"
CONFIG="$CONFIG_DIR/opencode.json"
mkdir -p "$CONFIG_DIR"

echo "Using Bedrock in ${AWS_REGION} through the runtime IAM role"

# ── Materialize the role credentials for opencode's SigV4 signer ─────────────
# opencode's Bedrock provider (Vercel AI SDK, Node) signs SigV4 but does NOT
# resolve the container credential chain (AWS_CONTAINER_CREDENTIALS_FULL_URI /
# IMDS) the way boto3 does; without static env keys every model call fails with
# "SigV4 authentication requires AWS credentials". Export the runtime role's
# temporary keys into the env the SDK reads. Fail-soft: if awscli can't resolve
# them here, the CLI still tries its own chain (unchanged behavior). This is the
# same prelude the orchestrator's headless dispatch runs; baking it here covers
# the INTERACTIVE TUI path too (the muxed live session).
eval "$(aws configure export-credentials --format env 2>/dev/null)" 2>/dev/null || true

# ── Regenerate the opencode config with the live region (+ MCP gateway) ──────
# opencode reads ~/.config/opencode/opencode.json. Rewrite it at startup so the
# region is always the live one, and add the MCP gateway block when GATEWAY_URL
# is set (the same brokered-tools pattern the other harnesses use).
MCP_BLOCK=""
if [ -n "${GATEWAY_URL:-}" ]; then
  MCP_BLOCK=$(cat <<MCPEOF
  "mcp": {
    "gateway": {
      "type": "local",
      "command": ["node", "/mnt/s3files/mcp/index.js", "--gateway-url", "${GATEWAY_URL}", "--region", "${AWS_REGION}"]
    }
  },
MCPEOF
)
  echo "MCP gateway configured: ${GATEWAY_URL}"
fi

cat > "$CONFIG" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
${MCP_BLOCK}
  "provider": {
    "amazon-bedrock": {
      "options": { "region": "${AWS_REGION}" }
    }
  },
  "model": "amazon-bedrock/us.anthropic.claude-sonnet-4-6",
  "small_model": "amazon-bedrock/anthropic.claude-haiku-4-5-20251001-v1:0"
}
EOF

# Preserve an explicit per-run cwd from the orchestrator. Interactive AgentCore
# shells start at `/`, so prefer the staged project guidance on the shared mount
# instead of treating that shell root as the project directory.
if [ -n "${WORKSHOP_AGENT_WORKDIR:-}" ]; then
  RUN_DIR="$WORKSHOP_AGENT_WORKDIR"
elif [ -f /mnt/s3files/AGENTS.md ]; then
  RUN_DIR="/mnt/s3files"
else
  RUN_DIR="$HOME"
fi

# ── Parse --model flag (default: Bedrock Claude Sonnet 4.6) ──────────────────
MODEL="amazon-bedrock/us.anthropic.claude-sonnet-4-6"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --model)
      MODEL="$2"
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${ARGS[@]}"

# ── Run ──────────────────────────────────────────────────────
cd "$RUN_DIR"
if [ $# -gt 0 ]; then
  PROMPT="$*"
  echo "Running prompt with model: ${MODEL}"
  exec opencode run --dangerously-skip-permissions -m "$MODEL" "$PROMPT"
else
  exec opencode
fi
