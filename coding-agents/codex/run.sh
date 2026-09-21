#!/usr/bin/env bash
# Codex on Bedrock Runtime Responses, authenticated by the Runtime IAM role.
# /app/run.sh [--model MODEL] [PROMPT] opens the TUI or runs one headless turn.
set -euo pipefail

# Command-shell sessions may not inherit the container's configured environment.
# Copy only named non-secret settings; never materialize AWS or GitHub keys.
for name in AWS_REGION AWS_DEFAULT_REGION WORKSHOP_CODEX_MODEL WORKSHOP_MODEL_CODEX WORKSHOP_MODEL; do
  if [ -z "${!name:-}" ] && [ -r /proc/1/environ ]; then
    value=$(tr '\0' '\n' < /proc/1/environ | grep "^${name}=" | cut -d= -f2- || true)
    if [ -n "$value" ]; then export "$name=$value"; fi
  fi
done
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
: "${AWS_REGION:?No AWS region. Set AWS_REGION or AWS_DEFAULT_REGION.}"
export AWS_DEFAULT_REGION="$AWS_REGION"

# The coordinator owns a named linked worktree on local disk. A hand-opened
# shell can start in / or /app; both use the Lab 1 shared mount when attached.
if [ -n "${WORKSHOP_AGENT_WORKDIR:-}" ]; then
  run_dir="$WORKSHOP_AGENT_WORKDIR"
elif [ "$PWD" != "/" ] && [ "$PWD" != "/app" ]; then
  run_dir="$PWD"
elif [ -d /mnt/s3files ]; then
  run_dir="/mnt/s3files"
else
  run_dir="$HOME"
fi
cd "$run_dir"
run_dir="$PWD"

model="${WORKSHOP_MODEL_CODEX:-${WORKSHOP_MODEL:-${WORKSHOP_CODEX_MODEL:-us.openai.gpt-5.6-sol}}}"
prompt_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --model)
      [ $# -ge 2 ] && [ -n "$2" ] || { echo "--model needs a value" >&2; exit 2; }
      model="$2"; shift 2 ;;
    --) shift; prompt_args+=("$@"); break ;;
    *) prompt_args+=("$1"); shift ;;
  esac
done

# Trust exactly this checkout, including paths with quotes, without changing
# CODEX_HOME or racing other sessions by rewriting the shared config file.
trust_setting=$(python3 -c 'import json,os; print("projects={"+json.dumps(os.path.realpath(os.getcwd()),ensure_ascii=False)+"={trust_level=\"trusted\"}}")')
region_setting=$(python3 -c 'import json,os; print("model_providers.amazon-bedrock-runtime.aws.region="+json.dumps(os.environ["AWS_REGION"]))')
codex_args=(--model "$model" --dangerously-bypass-approvals-and-sandbox --cd "$run_dir"
            -c 'model_provider="amazon-bedrock-runtime"'
            -c "$region_setting" -c "$trust_setting")
echo "Using Bedrock Runtime in $AWS_REGION with model $model"
if [ ${#prompt_args[@]} -gt 0 ]; then
  exec codex exec "${codex_args[@]}" --skip-git-repo-check -- "${prompt_args[*]}"
else
  exec codex "${codex_args[@]}"
fi
