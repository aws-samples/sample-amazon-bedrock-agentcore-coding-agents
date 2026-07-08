#!/usr/bin/env bash
# Build and push the Claude Code (PTY) container image to ECR.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INFRA_CONFIG="${SCRIPT_DIR}/../infra.config"

if [ ! -f "$INFRA_CONFIG" ]; then
  echo "Error: infra.config not found. Run ../infra/setup.sh first."
  exit 1
fi

source "$INFRA_CONFIG"

ECR_REPO="coding-agents-claude-code"
IMAGE_TAG="latest"
ECR_URI="${INFRA_ACCOUNT_ID}.dkr.ecr.${INFRA_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
AGENT_NAME="claude_code"

echo "=============================================="
echo "  Claude Code (PTY): Build & Push"
echo "  Region: ${INFRA_REGION}  Account: ${INFRA_ACCOUNT_ID}"
echo "=============================================="

# ── ECR repo ─────────────────────────────────────────────────────────────────
if aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${INFRA_REGION}" >/dev/null 2>&1; then
  echo "ECR repo exists: ${ECR_REPO}"
else
  echo "Creating ECR repo: ${ECR_REPO}"
  aws ecr create-repository --repository-name "${ECR_REPO}" --region "${INFRA_REGION}" > /dev/null
fi

# ── Build & push (builder-portable: docker buildx or finch) ──────────────────
source "${SCRIPT_DIR}/../_build_push.sh"
build_and_push_arm64 "${ECR_URI}" "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}" \
  "${INFRA_REGION}" "${INFRA_ACCOUNT_ID}"

# ── Save agent config ────────────────────────────────────────────────────────
cat > "${SCRIPT_DIR}/agent.config" <<EOF
AGENT_NAME=${AGENT_NAME}
ECR_REPO=${ECR_REPO}
ECR_URI=${ECR_URI}
EOF

echo ""
echo "Config saved to: agent.config"
echo "Next: python deploy.py"
