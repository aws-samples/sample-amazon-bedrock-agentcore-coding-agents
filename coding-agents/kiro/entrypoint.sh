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

# Hand off to the container's real command (CMD args passed by Docker).
exec "$@"
