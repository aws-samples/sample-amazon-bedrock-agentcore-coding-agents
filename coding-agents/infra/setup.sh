#!/usr/bin/env bash
# Write infra.config for the coding-agent deploy from the SHARED, pre-provisioned
# VPC and the S3 Files access point you create yourself.
#
# In this workshop the VPC + private subnets + security group are pre-provisioned
# (the workshop CloudFormation stack creates them and publishes their IDs as SSM
# parameters under /workshop/agent/*). You create the S3 Files file system, access
# point, and mount targets BY HAND (Stage 1, page 1) so you learn how the mount is
# set up. This script does the last mile: it gathers those two sets of values and
# writes infra.config, the file that claude-code/setup.sh and deploy.py read.
#
# It does NOT create a VPC, does NOT create the S3 Files mount, and does NOT upload
# skills (you do that with `aws s3 cp` in the content). It only records what exists.
#
# Inputs (env vars; the script auto-discovers the VPC pieces from SSM if unset):
#   Pre-provisioned VPC (auto-discovered from SSM /workshop/agent/* when unset):
#     INFRA_VPC_ID, INFRA_SUBNET_1, INFRA_SUBNET_2, INFRA_SECURITY_GROUP
#   Your S3 Files mount (from the `aws s3files create-*` commands you ran):
#     INFRA_S3FILES_FS_ID, INFRA_S3FILES_AP_ID, INFRA_S3FILES_AP_ARN   (required)
#   Optional:
#     INFRA_BUCKET (default: coding-agents-<account>-<region>)
#
# Usage:
#   INFRA_S3FILES_AP_ARN=arn:aws:s3files:... INFRA_S3FILES_FS_ID=fs-... \
#     INFRA_S3FILES_AP_ID=ap-... ./setup.sh us-west-2
set -euo pipefail

REGION="${1:-us-west-2}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET_NAME="${INFRA_BUCKET:-coding-agents-${ACCOUNT_ID}-${REGION}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=============================================="
echo "  Writing infra.config for the coding agent"
echo "  Region: $REGION  Account: $ACCOUNT_ID"
echo "=============================================="

# ── Resolve the pre-provisioned VPC ──────────────────────────────────────────
# Prefer explicit env vars; otherwise read the SSM parameters the workshop stack
# published. The workshop instance role can read parameter/workshop/*.
ssm_param() {
  aws ssm get-parameter --name "$1" --region "$REGION" \
    --query "Parameter.Value" --output text 2>/dev/null || true
}

INFRA_VPC_ID="${INFRA_VPC_ID:-$(ssm_param /workshop/agent/vpc-id)}"
INFRA_SUBNET_1="${INFRA_SUBNET_1:-$(ssm_param /workshop/agent/private-subnet-1)}"
INFRA_SUBNET_2="${INFRA_SUBNET_2:-$(ssm_param /workshop/agent/private-subnet-2)}"
INFRA_SECURITY_GROUP="${INFRA_SECURITY_GROUP:-$(ssm_param /workshop/agent/security-group-id)}"

# ── Validate everything is present ───────────────────────────────────────────
missing=""
for var in INFRA_VPC_ID INFRA_SUBNET_1 INFRA_SUBNET_2 INFRA_SECURITY_GROUP \
           INFRA_S3FILES_FS_ID INFRA_S3FILES_AP_ID INFRA_S3FILES_AP_ARN; do
  if [ -z "${!var:-}" ]; then
    missing="${missing} ${var}"
  fi
done

if [ -n "$missing" ]; then
  echo "ERROR: missing required value(s):${missing}" >&2
  echo "" >&2
  echo "  The VPC values come from the pre-provisioned workshop stack (SSM" >&2
  echo "  /workshop/agent/*). The S3 Files values come from the access point you" >&2
  echo "  created; export them from your 'aws s3files create-*' command output:" >&2
  echo "    export INFRA_S3FILES_FS_ID=fs-...      # create-file-system output" >&2
  echo "    export INFRA_S3FILES_AP_ID=ap-...      # create-access-point output" >&2
  echo "    export INFRA_S3FILES_AP_ARN=arn:aws:s3files:...:access-point/ap-..." >&2
  exit 1
fi

# ── Write the shared config ──────────────────────────────────────────────────
cat > "${SCRIPT_DIR}/../infra.config" <<EOF
INFRA_REGION=${REGION}
INFRA_ACCOUNT_ID=${ACCOUNT_ID}
INFRA_BUCKET=${BUCKET_NAME}
INFRA_VPC_ID=${INFRA_VPC_ID}
INFRA_SUBNET_1=${INFRA_SUBNET_1}
INFRA_SUBNET_2=${INFRA_SUBNET_2}
INFRA_SECURITY_GROUP=${INFRA_SECURITY_GROUP}
INFRA_S3FILES_FS_ID=${INFRA_S3FILES_FS_ID}
INFRA_S3FILES_AP_ID=${INFRA_S3FILES_AP_ID}
INFRA_S3FILES_AP_ARN=${INFRA_S3FILES_AP_ARN}
EOF

echo ""
echo "  VPC:             ${INFRA_VPC_ID}"
echo "  Private Subnet1: ${INFRA_SUBNET_1}"
echo "  Private Subnet2: ${INFRA_SUBNET_2}"
echo "  Security Group:  ${INFRA_SECURITY_GROUP}"
echo "  S3 Files FS:     ${INFRA_S3FILES_FS_ID}"
echo "  S3 Files AP ARN: ${INFRA_S3FILES_AP_ARN}"
echo ""
echo "Config saved to: ../infra.config"
echo "Next: cd ../claude-code && ./setup.sh   (build + push the image to ECR)"
