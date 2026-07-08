#!/usr/bin/env bash
# ============================================================
# Codex launcher for AgentCore Runtime (headless)
# ============================================================
# Routes inference through Amazon Bedrock Mantle (us-east-2).
# Auth: the AgentCore Runtime IAM role through the AWS SDK credential chain.
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

# Bedrock Mantle region used by this workshop.
BEDROCK_REGION="${BEDROCK_MANTLE_REGION:-us-east-2}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-2}}"

# ── Configure MCP Gateway (needs GATEWAY_URL from runtime env) ──
if [ -n "${GATEWAY_URL:-}" ]; then
  if ! grep -q "mcp_servers.gateway" /home/agent/.codex/config.toml 2>/dev/null; then
    cat >> /home/agent/.codex/config.toml <<MCPEOF

[mcp_servers.gateway]
command = "node"
args = ["/mnt/s3files/mcp/index.js", "--gateway-url", "${GATEWAY_URL}", "--region", "${AWS_REGION:-us-west-2}"]
MCPEOF
  fi
  echo "MCP gateway configured: ${GATEWAY_URL}"
fi

export AWS_REGION="$BEDROCK_REGION"
echo "Using Bedrock Mantle in ${BEDROCK_REGION} through the runtime IAM role"

# ── Trust the working directory (no first-run prompt) ────────
# Codex asks "Do you trust the contents of this directory?" the first time it
# runs in an untrusted dir; on a headless PTY there is no human to answer, so it
# hangs. --yolo (below) only governs the approval/sandbox policy, NOT the
# directory-trust gate: that gate is cleared by a per-project trust_level in
# config.toml. Write it idempotently at startup (the file lives under HOME, so
# this also fixes a runtime image that predates the fix, no rebuild needed).
CODEX_HOME="/home/agent"
# Preserve an explicit per-run cwd from the orchestrator. A direct interactive
# shell starts in /app, so use the shared mount in that case.
if [ -n "${WORKSHOP_AGENT_WORKDIR:-}" ]; then
  RUN_DIR="$WORKSHOP_AGENT_WORKDIR"
elif [ "$PWD" != "/app" ]; then
  RUN_DIR="$PWD"
elif [ -d /mnt/s3files ]; then
  RUN_DIR="/mnt/s3files"
else
  RUN_DIR="$CODEX_HOME"
fi
mkdir -p "$CODEX_HOME/.codex"
if ! grep -Fq "[projects.\"$RUN_DIR\"]" "$CODEX_HOME/.codex/config.toml" 2>/dev/null; then
  cat >> "$CODEX_HOME/.codex/config.toml" <<TRUSTEOF

[projects."$RUN_DIR"]
trust_level = "trusted"
TRUSTEOF
fi

# ── Parse --model flag ───────────────────────────────────────
MODEL="openai.gpt-5.5"
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
if [ $# -gt 0 ]; then
  PROMPT="$*"
  echo "Running prompt with model: ${MODEL}"
  cd "$RUN_DIR"
  exec codex exec --model "$MODEL" --yolo --skip-git-repo-check "$PROMPT"
else
  cd "$RUN_DIR"
  exec codex --model "$MODEL" --yolo
fi
