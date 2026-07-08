#!/usr/bin/env bash
# Build and push the Codex (PTY) container image to ECR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_CONFIG="${SCRIPT_DIR}/../infra.config"

if [ ! -f "$INFRA_CONFIG" ]; then
  echo "Error: infra.config not found. Run ../infra/setup.sh first."
  exit 1
fi

source "$INFRA_CONFIG"

ECR_REPO="coding-agents-codex"
IMAGE_TAG="latest"
ECR_URI="${INFRA_ACCOUNT_ID}.dkr.ecr.${INFRA_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
AGENT_NAME="codex"

# PUBLIC mode: pull a public ECR image (public.ecr.aws/...) and re-tag it into THIS
# account's private ECR, then deploy from there. AgentCore Runtime requires the
# container in the account's own ECR (containerUri must be <acct>.dkr.ecr...), so a
# public image cannot be registered directly; we mirror it once. Set
# WORKSHOP_PUBLIC_IMAGE_URI to the public tag. No local Docker build.
if [ -n "${WORKSHOP_PUBLIC_IMAGE_URI:-}" ]; then
  echo "=============================================="
  echo "  Codex (PTY): PUBLIC ECR mirror (no build)"
  echo "  From: ${WORKSHOP_PUBLIC_IMAGE_URI}"
  echo "  Into: ${ECR_URI}"
  echo "=============================================="
  if ! aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${INFRA_REGION}" >/dev/null 2>&1; then
    aws ecr create-repository --repository-name "${ECR_REPO}" --region "${INFRA_REGION}" > /dev/null
  fi
  BUILDER="$(command -v docker || command -v finch)"
  [ -n "$BUILDER" ] || { echo "ERROR: need docker or finch to mirror the public image"; exit 1; }
  # Public ECR needs an auth token for its own registry; private push uses the account login.
  aws ecr-public get-login-password --region us-east-1 2>/dev/null | "$BUILDER" login --username AWS --password-stdin public.ecr.aws 2>/dev/null || true
  aws ecr get-login-password --region "${INFRA_REGION}" | "$BUILDER" login --username AWS --password-stdin "${INFRA_ACCOUNT_ID}.dkr.ecr.${INFRA_REGION}.amazonaws.com"
  "$BUILDER" pull --platform linux/arm64 "${WORKSHOP_PUBLIC_IMAGE_URI}"
  "$BUILDER" tag "${WORKSHOP_PUBLIC_IMAGE_URI}" "${ECR_URI}"
  "$BUILDER" push "${ECR_URI}"
# PREBUILT mode: pull the image from a CENTRAL workshop ECR instead of building it
# in this account. Set WORKSHOP_CENTRAL_ECR_ACCOUNT (and optionally
# WORKSHOP_CENTRAL_ECR_REGION, default = this region) and the image URI points at
# the central registry; we skip the build entirely. The cross-account pull grant is
# derived from this URI by deploy.py. Falls back to a per-account build when unset.
elif [ -n "${WORKSHOP_CENTRAL_ECR_ACCOUNT:-}" ]; then
  CENTRAL_REGION="${WORKSHOP_CENTRAL_ECR_REGION:-${INFRA_REGION}}"
  ECR_URI="${WORKSHOP_CENTRAL_ECR_ACCOUNT}.dkr.ecr.${CENTRAL_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
  echo "=============================================="
  echo "  Codex (PTY): PREBUILT (central ECR, no build)"
  echo "  Image: ${ECR_URI}"
  echo "=============================================="
else
  echo "=============================================="
  echo "  Codex (PTY): Build & Push"
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
