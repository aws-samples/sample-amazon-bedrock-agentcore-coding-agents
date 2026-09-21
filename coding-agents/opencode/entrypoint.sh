#!/usr/bin/env bash
# Container entrypoint: start the OpenTelemetry collector sidecar at boot (so it
# holds the Runtime EXECUTION-ROLE credentials, not a shell session's), wait for it
# to accept OTLP on 127.0.0.1:4318, then hand off to the real command (the agent's
# healthcheck server / run.sh). The collector re-signs the agents' unsigned OTLP to
# CloudWatch Logs, X-Ray, and CloudWatch metrics (see otel-collector-config.yaml).
set -euo pipefail

OTELCOL="${OTELCOL_BIN:-/usr/local/bin/otelcol-contrib}"
OTELCOL_CONFIG="${OTELCOL_CONFIG:-/app/otel-collector-config.yaml}"
COLLECTOR_LOG="${OTELCOL_LOG:-/tmp/otel-collector.log}"
collector_budget="${OTELCOL_STARTUP_TIMEOUT_S:-60}"

collector_fail() {
  echo "[entrypoint] COLLECTOR_STARTUP_ERROR: $*; inspect $COLLECTOR_LOG" >&2
  exit 1
}

# Keep the whole collector gate below 64 seconds, including a final probe/sleep.
# A shorter budget is useful for diagnostics; it cannot extend Runtime startup.
[[ "$collector_budget" =~ ^([1-9]|[1-5][0-9]|60)$ ]] \
  || collector_fail "OTELCOL_STARTUP_TIMEOUT_S must be an integer from 1 to 60"
[ -x "$OTELCOL" ] || collector_fail "collector executable is missing"
[ -f "$OTELCOL_CONFIG" ] || collector_fail "collector configuration is missing"
"$OTELCOL" --config "$OTELCOL_CONFIG" > "$COLLECTOR_LOG" 2>&1 &
collector_pid=$!
# Initialization failure must not leave a collector running beside a failed CMD.
trap 'kill "$collector_pid" 2>/dev/null || true' EXIT
collector_deadline=$((SECONDS + collector_budget))
collector_ready=false
while (( SECONDS < collector_deadline )); do
  kill -0 "$collector_pid" 2>/dev/null || collector_fail "collector exited"
  if curl --connect-timeout 1 --max-time 2 -fsS -o /dev/null \
      "http://127.0.0.1:13133" 2>/dev/null; then
    kill -0 "$collector_pid" 2>/dev/null || collector_fail "collector exited"
    collector_ready=true
    break
  fi
  sleep 1
done
if [ "$collector_ready" != true ]; then
  collector_fail "collector did not become healthy within ${collector_budget}s"
fi

# ── Pin the baked config to THIS container's region, before anything runs ─────
# opencode reads provider.options.region from its config FILE, and that value
# WINS over AWS_REGION. The image bakes a region, so a runtime in any other
# region signs Bedrock calls for the baked one and every model call fails with
# AccessDenied on a foreign inference profile (seen live: a us-east-1 runtime
# calling us-west-2). run.sh already rewrites the config, but the orchestrator
# dispatches the `opencode` binary DIRECTLY and never runs run.sh, so the fix has
# to live here at boot, where both paths pass. A failed rewrite must not become
# a healthy snapshot whose every restored session signs for the wrong region.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_CFG="${OPENCODE_CONFIG:-/home/agent/.config/opencode/opencode.json}"
_RGN="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$_RGN" ] && [ -r /proc/1/environ ]; then
  _RGN=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^AWS_REGION=//p' | head -n 1)
fi
if [ -z "$_RGN" ]; then
  echo "[entrypoint] OPENCODE_STARTUP_ERROR: no AWS region for provider configuration" >&2
  exit 1
fi
if ! python3 "$HERE/configure_opencode.py" --config "$_CFG" --region "$_RGN" \
     --gateway-url "${GATEWAY_URL:-}"; then
  echo "[entrypoint] OPENCODE_STARTUP_ERROR: could not rewrite $_CFG" >&2
  exit 1
fi

# Hand off to the container's real command (CMD args passed by Docker).
trap - EXIT
exec "$@"
