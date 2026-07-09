#!/usr/bin/env bash
# Deploy a pre-built agent (opencode or claude-code-validator) onto the attendee's
# S3 Files mount.
#
# Normally the workshop stack already built this agent's arm64 image at bootstrap
# (the slow, mount-independent work), so this script just runs the agent's
# deploy.py: CreateAgentRuntime attaching the S3 Files access point the attendee
# created on Stage 1 page 1. Fast, image in ECR.
#
# But the pre-build is best-effort and NOT guaranteed on every account. To keep ONE
# command working everywhere (the governing test), this script self-heals: if the
# image was not pre-built, it runs the agent's setup.sh first (build + push), then
# deploy.py. Both opencode and the Claude Code validator are Bedrock-native, so
# there is no vendor key to provision.
#
# The kiro/codex targets are kept as hidden/legacy restore paths (both were retired
# from the served roster): kiro's build works WITHOUT a key via --skip-identity.
#
# Usage (from coding-agents):
#   ./deploy-prebuilt.sh opencode
#   ./deploy-prebuilt.sh claude-code-validator       # the validator (Bedrock-native, no key)
#   ./deploy-prebuilt.sh kiro                        # hidden/legacy: builds --skip-identity if not pre-built
set -euo pipefail

AGENT="${1:-}"
case "$AGENT" in
  # opencode + claude-code-validator are the pre-provisioned pair; kiro/codex are
  # kept as hidden/legacy restore targets.
  opencode|claude-code-validator|kiro|codex) ;;
  *) echo "Usage: $0 <opencode|claude-code-validator>" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_CONFIG="${SCRIPT_DIR}/infra.config"

if [ ! -f "$INFRA_CONFIG" ]; then
  echo "Error: infra.config not found at ${INFRA_CONFIG}." >&2
  echo "  Create the S3 Files access point first (Stage 1 page 1), which writes it." >&2
  exit 1
fi

# Self-heal: if the agent was NOT pre-built (no agent.config with an ECR_URI), build
# it now so deploy.py has an image. deploy.py otherwise fails "ECR_URI not found".
if ! grep -q '^ECR_URI=.\+' "${SCRIPT_DIR}/${AGENT}/agent.config" 2>/dev/null; then
  echo "No pre-built ${AGENT} image found (agent.config has no ECR_URI); building it now..."
  # Kiro's build only needs a key to ALSO provision its Token Vault identity. With
  # no key (the common Workshop Studio case: WS temp accounts cannot issue a ksk_),
  # build the image WITHOUT identity via --skip-identity, exactly like the bootstrap
  # does; deploy.py still creates the Runtime + ARN, and the attendee adds their key
  # on the wired instance in console Settings later (run.sh reads it from Token Vault
  # at session start). This keeps the ONE command working keyless everywhere.
  if [ "$AGENT" = "kiro" ] && [ -z "${KIRO_API_KEY:-}" ]; then
    echo "  No KIRO_API_KEY set; building kiro without its Token Vault identity"
    echo "  (--skip-identity). Add your ksk_ key on the wired Kiro instance in"
    echo "  console Settings after it deploys."
    ( cd "${SCRIPT_DIR}/${AGENT}" && ./setup.sh --skip-identity )
  else
    ( cd "${SCRIPT_DIR}/${AGENT}" && ./setup.sh )
  fi
fi

# The access point ARN is the piece the attendee adds on Stage 1 page 1. It is
# OPTIONAL here: with it, deploy.py attaches the /mnt/s3files mount; without it,
# the runtime deploys MOUNTLESS and the attendee attaches the mount later by
# re-running deploy.py once the access point exists. Just note which path we are on.
if grep -q '^INFRA_S3FILES_AP_ARN=.\+' "$INFRA_CONFIG"; then
  echo "Deploying pre-built ${AGENT} with the shared S3 Files mount attached..."
else
  echo "Deploying pre-built ${AGENT} MOUNTLESS (no S3 Files access point in infra.config yet);" >&2
  echo "  re-run after 'Set up shared storage' on Stage 1 page 1 to attach /mnt/s3files." >&2
fi
( cd "${SCRIPT_DIR}/${AGENT}" && python3 deploy.py )
echo "Done. ${AGENT} runtime_config.json written; the console shelf will reconcile it to ready."
