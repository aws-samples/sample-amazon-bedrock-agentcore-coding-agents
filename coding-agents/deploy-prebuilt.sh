#!/usr/bin/env bash
# Deploy a pre-built agent (opencode or kiro) onto the attendee's S3 Files mount.
#
# Normally the workshop stack already built this agent's arm64 image (and, for kiro,
# set up its Token Vault identity) at bootstrap (the slow, mount-independent work),
# so this script just runs the agent's deploy.py: CreateAgentRuntime attaching the
# S3 Files access point the attendee created on Stage 1 page 1. Fast, image in ECR.
#
# But the pre-build is best-effort and NOT guaranteed on every account: a Workshop
# Studio account often ships with a blank KiroApiKey (WS temp accounts cannot issue
# a Kiro key), so the bootstrap skips Kiro's build and there is no image in ECR. To
# keep ONE command working everywhere (the governing test), this script self-heals:
# if the image was not pre-built, it runs the agent's setup.sh first (build + push),
# then deploy.py. For kiro that build works WITHOUT a key too: with no KIRO_API_KEY
# it builds --skip-identity (no Token Vault identity), and the attendee adds their
# ksk_ key on the wired instance in console Settings afterwards. Pass KIRO_API_KEY
# only if you want the identity provisioned during this build.
#
# Usage (from src/coding-agents):
#   ./deploy-prebuilt.sh opencode
#   ./deploy-prebuilt.sh kiro                        # keyless: builds --skip-identity if not pre-built
#   KIRO_API_KEY=ksk_... ./deploy-prebuilt.sh kiro   # also provision the Token Vault identity now
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
