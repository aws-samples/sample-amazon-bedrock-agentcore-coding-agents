#!/usr/bin/env bash
# Build and push the Claude Code validator (PTY) container image to ECR.
# The validator is a second Claude Code, steered by an acceptance-contract
# CLAUDE.md; it is otherwise identical to the backend Claude Code harness.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_CONFIG="${SCRIPT_DIR}/../infra.config"

if [ ! -f "$INFRA_CONFIG" ]; then
  echo "Error: infra.config not found. Run ../infra/setup.sh first."
  exit 1
fi

source "$INFRA_CONFIG"

ECR_REPO="coding-agents-claude-code-validator"
IMAGE_TAG="latest"
ECR_URI="${INFRA_ACCOUNT_ID}.dkr.ecr.${INFRA_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
AGENT_NAME="claude_code_validator"

# PREBUILT mode: pull the image from a CENTRAL workshop ECR instead of building it
# in this account. Set WORKSHOP_CENTRAL_ECR_ACCOUNT (and optionally
# WORKSHOP_CENTRAL_ECR_REGION, default = this region) and the image URI points at the
# central registry; the build is skipped entirely and deploy.py derives the
# cross-account pull grant from this URI. Falls back to a per-account build when unset.
#
# This branch mirrors opencode's. Without it, CentralEcrAccount (whose CFN description
# names this role explicitly) silently did only half its job: opencode pulled, while
# every box still paid a full arm64 build for the validator with no signal that the
# parameter had been ignored.
if [ -n "${WORKSHOP_CENTRAL_ECR_ACCOUNT:-}" ]; then
  CENTRAL_REGION="${WORKSHOP_CENTRAL_ECR_REGION:-${INFRA_REGION}}"
  ECR_URI="${WORKSHOP_CENTRAL_ECR_ACCOUNT}.dkr.ecr.${CENTRAL_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
  echo "=============================================="
  echo "  Claude Code validator (PTY): PREBUILT (central ECR, no build)"
  echo "  Image: ${ECR_URI}"
  echo "=============================================="
else
  echo "=============================================="
  echo "  Claude Code validator (PTY): Build & Push"
  echo "  Region: ${INFRA_REGION}  Account: ${INFRA_ACCOUNT_ID}"
  echo "=============================================="

  # ── ECR repo ───────────────────────────────────────────────────────────────
  if aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${INFRA_REGION}" >/dev/null 2>&1; then
    echo "ECR repo exists: ${ECR_REPO}"
  else
    echo "Creating ECR repo: ${ECR_REPO}"
    aws ecr create-repository --repository-name "${ECR_REPO}" --region "${INFRA_REGION}" > /dev/null
  fi

  # ── Build & push (builder-portable: docker buildx or finch) ────────────────
  source "${SCRIPT_DIR}/../_build_push.sh"
  build_and_push_arm64 "${ECR_URI}" "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}" \
    "${INFRA_REGION}" "${INFRA_ACCOUNT_ID}"
fi

# ── Save agent config ────────────────────────────────────────────────────────
cat > "${SCRIPT_DIR}/agent.config" <<EOF
AGENT_NAME=${AGENT_NAME}
ECR_REPO=${ECR_REPO}
ECR_URI=${ECR_URI}
EOF

echo ""
echo "Config saved to: agent.config"
echo "Next: python deploy.py"
