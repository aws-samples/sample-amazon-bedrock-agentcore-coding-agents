#!/usr/bin/env bash
# ============================================================
# Claude Code launcher for AgentCore Runtime (headless)
# ============================================================
# This script is baked into the container image and called by connect.py.
# The container already runs as the agent user (USER agent in Dockerfile).
#
# Authentication:
#   Claude Code on AgentCore uses Bedrock (CLAUDE_CODE_USE_BEDROCK=1).
#   The microVM's IAM role already has bedrock:InvokeModel permissions,
#   so no API key is needed; AWS credentials are provided automatically
#   by the instance metadata service.
#
# MCP:
#   ~/.mcp.json is generated at startup from GATEWAY_URL env var.
#   permissionMode=dontAsk in settings.json auto-accepts the MCP server.
#
# Usage (from connect.py):
#   /app/run.sh                          # interactive claude (--continue)
#   /app/run.sh --print "fix the bug"   # non-interactive one-shot
#   /app/run.sh <any claude args>        # pass-through
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
export CLAUDE_CODE_USE_BEDROCK=1
export HOME="/home/agent"

# ── No first-run prompts (self-heal on a stale image) ─────────
# --dangerously-skip-permissions still shows a one-time "Bypass Permissions mode?
# 1.No 2.Yes" acceptance on a headless PTY (it would hang with no human). The
# setting that suppresses it is skipDangerousModePermissionPrompt=true in
# ~/.claude/settings.json. It is baked into the image, but we also merge it in here
# so an already-deployed image that predates the bake self-heals with no rebuild.
# Runs silently (the file lives under HOME, never echoed to the PTY).
python3 - <<'PYSETTINGS' >/dev/null 2>&1 || true
import json, os
p = os.path.expanduser("~/.claude/settings.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    cfg = json.load(open(p))
except Exception:
    cfg = {}
cfg.setdefault("permissionMode", "dontAsk")
cfg["hasCompletedOnboarding"] = True
cfg["skipDangerousModePermissionPrompt"] = True
json.dump(cfg, open(p, "w"), indent=2)
PYSETTINGS

# ── Configure MCP Gateway (needs GATEWAY_URL from runtime env) ──
if [ -n "${GATEWAY_URL:-}" ]; then
  cat > "$HOME/.mcp.json" <<MCPEOF
{
  "mcpServers": {
    "gateway": {
      "type": "stdio",
      "command": "node",
      "args": ["/mnt/s3files/mcp/index.js", "--gateway-url", "${GATEWAY_URL}", "--region", "${AWS_REGION}"]
    }
  }
}
MCPEOF
fi

# ── Parse --model flag ───────────────────────────────────────
# Default model is wirable: WORKSHOP_MODEL (a deploy-time runtime env var) wins over
# the baked default, so an event whose account has not enabled Opus 4.6 (Bedrock
# Marketplace subscription) can point the backend at an enabled model
# (e.g. us.anthropic.claude-sonnet-4-6) WITHOUT editing this image. An explicit
# --model on the command line still overrides both.
CLAUDE_EFFORT="${WORKSHOP_CLAUDE_EFFORT:-xhigh}"
MODEL="${WORKSHOP_MODEL:-us.anthropic.claude-opus-4-6-v1}"
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

# An orchestrated interactive PTY hydrates one run-local linked worktree and
# supplies it here. Manual Lab 1 shells keep the familiar HOME default.
RUN_DIR="${WORKSHOP_AGENT_WORKDIR:-$HOME}"
cd "$RUN_DIR"

# ── Run ──────────────────────────────────────────────────────
if [ $# -gt 0 ]; then
  # --effort: reasoning budget, high by default and wirable. `claude --effort`
  # accepts low|medium|high|xhigh|max and WARNS-and-ignores anything else, so a
  # bad value degrades to the default rather than failing the run.
  exec claude --dangerously-skip-permissions --print --max-turns 50 \
    ${CLAUDE_EFFORT:+--effort "$CLAUDE_EFFORT"} --model "$MODEL" "$@"
else
  exec claude --dangerously-skip-permissions \
    ${CLAUDE_EFFORT:+--effort "$CLAUDE_EFFORT"} --model "$MODEL"
fi
