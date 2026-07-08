#!/usr/bin/env bash
# Deploy a pre-built agent (opencode or kiro) onto the attendee's S3 Files mount.
#
# Normally the workshop stack already built this agent's arm64 image (and, for kiro,
# set up its Token Vault identity) at bootstrap (the slow, mount-independent work),
# so this script just runs the agent's deploy.py: CreateAgentRuntime attaching the
# S3 Files access point the attendee created on Stage 1 page 1. Fast, image in ECR.
#
# But the pre-build is best-effort and NOT guaranteed on every account: a Workshop
# bootstrap normally pre-builds Kiro AND seeds its Token Vault identity from the
# KiroApiKey stack parameter, so this command just re-runs deploy.py to attach the
# mount. On the rare account where the image was NOT pre-built, this script
# self-heals by running setup.sh (build + Token Vault identity) first, then
# deploy.py. Kiro's Token Vault identity is required for it to authenticate, so a
# key must be available: pass KIRO_API_KEY here if the image needs building.
#
# Usage (from coding-agents):
#   ./deploy-prebuilt.sh opencode
#   ./deploy-prebuilt.sh kiro                        # attach mount (image + key already provisioned)
#   KIRO_API_KEY=ksk_... ./deploy-prebuilt.sh kiro   # also build the image + seed the identity now
set -euo pipefail

AGENT="${1:-}"
case "$AGENT" in
  opencode|kiro|codex) ;;   # codex kept as a hidden/legacy target; opencode is the frontend
  *) echo "Usage: $0 <opencode|kiro>" >&2; exit 2 ;;
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
  # Kiro needs its Token Vault identity to authenticate, and setup.sh seeds it from
  # KIRO_API_KEY. If the identity was already seeded at bootstrap (the normal case)
  # but the image is missing, pass KIRO_API_KEY to rebuild+reseed. Fail loud rather
  # than deploy a keyless Kiro that cannot authenticate.
  if [ "$AGENT" = "kiro" ] && [ -z "${KIRO_API_KEY:-}" ]; then
    echo "  ERROR: ${AGENT} image is missing and KIRO_API_KEY is not set." >&2
    echo "  Kiro authenticates via a Token Vault identity seeded from the key." >&2
    echo "  Re-run: KIRO_API_KEY=ksk_... ./deploy-prebuilt.sh kiro" >&2
    exit 1
  fi
  ( cd "${SCRIPT_DIR}/${AGENT}" && ./setup.sh )
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
