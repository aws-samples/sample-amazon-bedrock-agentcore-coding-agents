#!/usr/bin/env bash
# Direct dispatch bypasses run.sh. Configure the provider before serving /ping.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
export AWS_DEFAULT_REGION="$AWS_REGION"
python3 "$HERE/configure_codex.py"

# The collector starts with the Runtime role, before a shell receives a user's
# telemetry attributes. It exports only native token-bearing completion events.
collector="${OTELCOL_BIN:-/usr/local/bin/otelcol-contrib}"
collector_config="${OTELCOL_CONFIG:-$HERE/otel-collector-config.yaml}"
if [ -x "$collector" ] && [ -f "$collector_config" ]; then
  "$collector" --config "$collector_config" > /tmp/otel-collector.log 2>&1 &
  collector_pid=$!
  collector_ready=false
  for ((attempt=0; attempt<30; attempt++)); do
    if ! kill -0 "$collector_pid" 2>/dev/null; then break; fi
    if curl -fsS -o /dev/null http://127.0.0.1:13133 2>/dev/null; then
      collector_ready=true
      break
    fi
    sleep 1
  done
  if [ "$collector_ready" != true ]; then
    echo "[entrypoint] Codex usage export is unavailable; inspect /tmp/otel-collector.log" >&2
  fi
else
  echo "[entrypoint] Codex usage export is unavailable: collector or configuration missing" >&2
fi
exec "$@"
