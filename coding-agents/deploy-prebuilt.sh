#!/usr/bin/env bash
# Deploy a pre-built agent onto the attendee's S3 Files mount.
#
# Normally the workshop stack already built this agent's arm64 image at bootstrap
# (the slow, mount-independent work), so this script just runs the agent's
# deploy.py: CreateAgentRuntime attaching the S3 Files access point the attendee
# prepared by the workshop stack. Fast, image in ECR.
#
# But the pre-build is best-effort and NOT guaranteed on every account. To keep ONE
# command working everywhere (the governing test), this script self-heals: if the
# image was not pre-built, it runs the agent's setup.sh first (build + push), then
# deploy.py. Codex uses Bedrock Runtime with no vendor key. Kiro's IMAGE also builds
# keyless: the key is per-attendee and is minted AFTER provisioning, then read from
# the Token Vault at session start (see --skip-identity below).
#
# claude-code-validator and opencode stay accepted targets because both are kept
# REGISTERED restore paths (restorable with WORKSHOP_ROLES alone): the Claude Code
# validator is the Bedrock-native, no-key checker for an account with no Kiro
# subscription, and opencode is the alternate native Bedrock frontend.
#
# claude-code is accepted for the same reason, from the other direction: the stack
# pre-builds its IMAGE but deliberately does NOT create its Runtime, so this is the
# one command that turns that image into the attendee's own mounted backend Runtime.
#
# Usage (from coding-agents):
#   ./deploy-prebuilt.sh claude-code                 # the backend; image pre-built, Runtime created here
#   ./deploy-prebuilt.sh codex                      # the frontend
#   ./deploy-prebuilt.sh kiro                        # the served validator; builds --skip-identity if keyless
#   ./deploy-prebuilt.sh claude-code-validator       # restore path (Bedrock-native, no key)
#   ./deploy-prebuilt.sh opencode                   # alternate frontend
#   ./deploy-prebuilt.sh claude-code --explain       # print the exact request; create nothing
#   ./deploy-prebuilt.sh claude-code --prepare       # role only, then the console form values
#   ./deploy-prebuilt.sh claude-code --adopt         # check the console-built Runtime, record it
#
# --explain reads only: it prints the IAM execution role the deploy would create or
# reuse, the CreateAgentRuntime (or UpdateAgentRuntime) request field by field, and the
# AWS CLI command that sends the same request by hand. It never builds an image, writes
# IAM, or creates/updates a Runtime, so it is safe to run before the real command.
set -euo pipefail

AGENT="${1:-}"
MODE="${2:-}"
case "$AGENT" in
  # All registered harnesses remain valid explicit targets, including restores.
  claude-code|opencode|kiro|claude-code-validator|codex) ;;
  *) echo "Usage: $0 <claude-code|opencode|kiro|claude-code-validator|codex> [--explain]" >&2; exit 2 ;;
esac
case "$MODE" in
  ""|--explain) ;;
  --prepare|--adopt)
    # The console path exists for the Runtime the attendee creates themselves.
    [ "$AGENT" = "claude-code" ] || { echo "$MODE is for claude-code, the Runtime you create in Lab 1." >&2; exit 2; } ;;
  *) echo "Usage: $0 $AGENT [--explain|--prepare|--adopt]" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSHOP_RUNTIME_PLATFORM_VERSION=$(python3 "$SCRIPT_DIR/runtime_deploy.py" --print-platform)
export WORKSHOP_RUNTIME_PLATFORM_VERSION
INFRA_CONFIG="${SCRIPT_DIR}/infra.config"

if [ ! -f "$INFRA_CONFIG" ]; then
  echo "Error: infra.config not found at ${INFRA_CONFIG}." >&2
  echo "  The workshop stack writes it when it creates the shared storage; ask a facilitator." >&2
  exit 1
fi

if [ "$MODE" = "--prepare" ] || [ "$MODE" = "--adopt" ]; then
  if ! grep -q '^ECR_URI=.\+' "${SCRIPT_DIR}/${AGENT}/agent.config" 2>/dev/null; then
    echo "No pre-built ${AGENT} image yet (agent.config has no ECR_URI); run ./deploy-prebuilt.sh ${AGENT} instead." >&2
    exit 1
  fi
  ( cd "${SCRIPT_DIR}/${AGENT}" && python3 deploy.py "$MODE" )
  exit 0
fi

if [ "$MODE" = "--explain" ]; then
  # A preview never builds: without the pre-built image there is no real request to show.
  if ! grep -q '^ECR_URI=.\+' "${SCRIPT_DIR}/${AGENT}/agent.config" 2>/dev/null; then
    echo "No pre-built ${AGENT} image yet (agent.config has no ECR_URI)." >&2
    echo "  The real command builds it first with setup.sh; --explain never builds." >&2
    exit 1
  fi
  ( cd "${SCRIPT_DIR}/${AGENT}" && python3 deploy.py --explain )
  exit 0
fi

# Self-heal: if the agent was NOT pre-built (no agent.config with an ECR_URI), build
# it now so deploy.py has an image. deploy.py otherwise fails "ECR_URI not found".
if ! grep -q '^ECR_URI=.\+' "${SCRIPT_DIR}/${AGENT}/agent.config" 2>/dev/null; then
  echo "No pre-built ${AGENT} image found (agent.config has no ECR_URI); building it now..."
  # Kiro's build only needs a key to ALSO provision its Token Vault identity, and at
  # image-build time there is no key yet BY DESIGN: the event provisions a per-team
  # Kiro subscription, and the attendee then mints their OWN ksk_ at app.kiro.dev
  # afterwards. So build the image WITHOUT identity via --skip-identity, exactly like
  # the bootstrap does; deploy.py still creates the Runtime + ARN, and the attendee
  # saves their key with Lab 1's hidden key prompt later (run.sh reads it from the
  # Token Vault at session start). This keeps the ONE command working keyless.
  if [ "$AGENT" = "kiro" ] && [ -z "${KIRO_API_KEY:-}" ]; then
    echo "  No KIRO_API_KEY set; building kiro without its Token Vault identity"
    echo "  (--skip-identity). Save your ksk_ key afterwards with Lab 1's hidden key"
    echo "  prompt (Lab 1, Read a Harness and Store the Kiro Key, step 2); no redeploy is needed."
    ( cd "${SCRIPT_DIR}/${AGENT}" && bash ./setup.sh --skip-identity )
  else
    ( cd "${SCRIPT_DIR}/${AGENT}" && bash ./setup.sh )
  fi
fi

# The access point ARN comes from the stack's shared storage. It is
# OPTIONAL here: with it, deploy.py attaches the /mnt/s3files mount; without it,
# the runtime deploys MOUNTLESS and the attendee attaches the mount later by
# re-running deploy.py once the access point exists. Just note which path we are on.
if grep -q '^INFRA_S3FILES_AP_ARN=.\+' "$INFRA_CONFIG"; then
  echo "Deploying pre-built ${AGENT} with the shared S3 Files mount attached..."
else
  echo "Deploying pre-built ${AGENT} MOUNTLESS (no S3 Files access point in infra.config yet);" >&2
  echo "  The stack normally writes the access point; ask a facilitator, then re-run to attach /mnt/s3files." >&2
fi
( cd "${SCRIPT_DIR}/${AGENT}" && python3 deploy.py )
echo "Done. ${AGENT} runtime_config.json written; the console shelf will reconcile it to ready."
