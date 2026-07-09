#!/usr/bin/env bash
# run-as-user.sh: run the coding agent AS a specific user, so its Bedrock calls
# are attributed to that user in the Bedrock model-invocation log.
#
#   run-as-user.sh <user> "<prompt>"
#
# What it does (the one idea this lab teaches): it makes the agent's model calls
# sign as the USER instead of the shared agent role. It does that by taking on a
# short-lived session whose NAME is the user; that session name is exactly what
# the Bedrock invocation log records as the caller. Nothing else about the agent
# changes. The role and its trust are pre-provisioned for you; you only pass who
# the run is for.
set -euo pipefail

USER_ID="${1:?usage: run-as-user.sh <user> \"<prompt>\"}"
PROMPT="${2:?usage: run-as-user.sh <user> \"<prompt>\"}"
ROLE_ARN="${PERUSER_ROLE_ARN:?set PERUSER_ROLE_ARN to the pre-provisioned per-user role ARN}"
REGION="${AWS_REGION:-us-west-2}"
MODEL="${ANTHROPIC_MODEL:-us.anthropic.claude-opus-4-6-v1}"

# Become the user: a short-lived STS session named for the user. The session name
# is what lands in the invocation log as assumed-role/<role>/<user>.
CREDS=$(aws sts assume-role \
  --role-arn "$ROLE_ARN" \
  --role-session-name "$USER_ID" \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
  --output text)
export AWS_ACCESS_KEY_ID="$(echo "$CREDS" | cut -f1)"
export AWS_SECRET_ACCESS_KEY="$(echo "$CREDS" | cut -f2)"
export AWS_SESSION_TOKEN="$(echo "$CREDS" | cut -f3)"
export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION="$REGION"

# Run the agent normally; its Bedrock calls now carry the user identity.
claude --dangerously-skip-permissions --print --model "$MODEL" "$PROMPT"
