#!/bin/sh
# Container entrypoint: start the OpenTelemetry collector sidecar at boot (so it
# holds the Runtime EXECUTION-ROLE credentials, not a shell session's), wait for it
# to accept OTLP on 127.0.0.1:4318, then hand off to the real command (the agent's
# healthcheck server / run.sh). The collector re-signs the agents' unsigned OTLP to
# CloudWatch Logs, X-Ray, and CloudWatch metrics (see otel-collector-config.yaml).
set -e

OTELCOL="${OTELCOL_BIN:-/usr/local/bin/otelcol-contrib}"
OTELCOL_CONFIG="${OTELCOL_CONFIG:-/app/otel-collector-config.yaml}"
COLLECTOR_LOG=/tmp/otel-collector.log

if [ -x "$OTELCOL" ] && [ -f "$OTELCOL_CONFIG" ]; then
  echo "[entrypoint] starting otelcol-contrib ($OTELCOL) ..."
  "$OTELCOL" --config "$OTELCOL_CONFIG" > "$COLLECTOR_LOG" 2>&1 &
  # Wait (bounded) for the OTLP HTTP receiver on 127.0.0.1:4318 to come up so the
  # first agent prompt is not dropped. Non-fatal: if it never binds, the agent
  # still runs; the collector log explains why (the content tells attendees to
  # tail /tmp/otel-collector.log on a connection-refused).
  i=0
  while [ "$i" -lt 30 ]; do
    if curl -fsS -o /dev/null "http://127.0.0.1:13133" 2>/dev/null; then
      echo "[entrypoint] collector healthy (health_check :13133)"
      break
    fi
    i=$((i + 1))
    sleep 1
  done
  [ "$i" -ge 30 ] && echo "[entrypoint] WARNING: collector health check not ready after 30s; see $COLLECTOR_LOG"
else
  echo "[entrypoint] otelcol-contrib not installed or config missing; skipping collector (telemetry export disabled)"
fi

# ── Pin the baked config to THIS container's region, before anything runs ─────
# opencode reads provider.options.region from its config FILE, and that value
# WINS over AWS_REGION. The image bakes a region, so a runtime in any other
# region signs Bedrock calls for the baked one and every model call fails with
# AccessDenied on a foreign inference profile (seen live: a us-east-1 runtime
# calling us-west-2). run.sh already rewrites the config, but the orchestrator
# dispatches the `opencode` binary DIRECTLY and never runs run.sh, so the fix has
# to live here at boot, where both paths pass. Fail-soft: a config we cannot
# rewrite leaves the baked file in place, exactly as before.
_CFG="${OPENCODE_CONFIG:-/home/agent/.config/opencode/opencode.json}"
_RGN="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "$_RGN" ] && [ -r /proc/1/environ ]; then
  _RGN=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^AWS_REGION=//p' | head -n 1)
fi
if [ -n "$_RGN" ] && [ -f "$_CFG" ] && [ -f /app/configure_opencode.py ]; then
  if python3 /app/configure_opencode.py --config "$_CFG" --region "$_RGN" \
       ${GATEWAY_URL:+--gateway-url "$GATEWAY_URL"}; then
    echo "[entrypoint] opencode config pinned to region $_RGN"
  else
    echo "[entrypoint] WARNING: could not rewrite $_CFG; keeping the baked region"
  fi
fi

# Hand off to the container's real command (CMD args passed by Docker).
exec "$@"
