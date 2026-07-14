#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

echo "==> Deploying AgentCore Gateway: ${GATEWAY_NAME}"

# Read runtime ARN from state
RUNTIME_ARN=$(state_get "runtime_arn")

if [[ -z "$RUNTIME_ARN" ]]; then
  echo "ERROR: No runtime ARN in state. Run deploy-runtime.sh first."
  exit 1
fi

# 1. Create IAM role for the gateway
echo ""
echo "--- Step 1: Gateway IAM Role ---"
GW_ROLE_NAME="${GATEWAY_NAME}-role"

GW_TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "bedrock-agentcore.amazonaws.com"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "aws:SourceAccount": "'"${AWS_ACCOUNT_ID}"'"
        },
        "ArnLike": {
          "aws:SourceArn": "arn:aws:bedrock-agentcore:'"${AWS_REGION}"':'"${AWS_ACCOUNT_ID}"':*"
        }
      }
    }
  ]
}'

GW_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeRuntime",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:InvokeAgentRuntime"
      ],
      "Resource": [
        "'"${RUNTIME_ARN}"'",
        "'"${RUNTIME_ARN}"'/*"
      ]
    }
  ]
}'

if aws iam get-role --role-name "$GW_ROLE_NAME" &>/dev/null; then
  echo "Gateway IAM role '${GW_ROLE_NAME}' already exists."
  GW_ROLE_ARN=$(aws iam get-role --role-name "$GW_ROLE_NAME" --query 'Role.Arn' --output text)
  aws iam update-assume-role-policy \
    --role-name "$GW_ROLE_NAME" \
    --policy-document "$GW_TRUST_POLICY"
else
  echo "Creating gateway IAM role '${GW_ROLE_NAME}'..."
  GW_ROLE_ARN=$(aws iam create-role \
    --role-name "$GW_ROLE_NAME" \
    --assume-role-policy-document "$GW_TRUST_POLICY" \
    --query 'Role.Arn' --output text)

  echo "Waiting for role to propagate..."
  sleep 10
fi
aws iam put-role-policy \
  --role-name "$GW_ROLE_NAME" \
  --policy-name "AgentCoreGatewayExecution" \
  --policy-document "$GW_POLICY"
state_set "gateway_role_arn" "$GW_ROLE_ARN"
state_set "gateway_role_name" "$GW_ROLE_NAME"
echo "Gateway Role ARN: ${GW_ROLE_ARN}"

# 2. Create the gateway
echo ""
echo "--- Step 2: Create Gateway ---"

# Try state first, then list to find by name
GATEWAY_ID=$(state_get "gateway_id")
EXISTING_GW=""
if [[ -n "$GATEWAY_ID" ]]; then
  EXISTING_GW=$(aws bedrock-agentcore-control get-gateway \
    --gateway-identifier "$GATEWAY_ID" \
    --region "$AWS_REGION" 2>/dev/null || true)
fi
if [[ -z "$EXISTING_GW" ]]; then
  # Search by name in the list
  GATEWAY_ID=$(aws bedrock-agentcore-control list-gateways \
    --region "$AWS_REGION" \
    --query "items[?name=='${GATEWAY_NAME}'].gatewayId | [0]" \
    --output text 2>/dev/null || true)
  if [[ -n "$GATEWAY_ID" && "$GATEWAY_ID" != "None" ]]; then
    EXISTING_GW=$(aws bedrock-agentcore-control get-gateway \
      --gateway-identifier "$GATEWAY_ID" \
      --region "$AWS_REGION" 2>/dev/null || true)
  else
    GATEWAY_ID=""
  fi
fi

if [[ -n "$EXISTING_GW" ]]; then
  echo "Gateway '${GATEWAY_NAME}' already exists (${GATEWAY_ID})."
  GATEWAY_URL=$(echo "$EXISTING_GW" | jq -r '.gatewayUrl // empty')
else
  echo "Creating gateway '${GATEWAY_NAME}'..."
  GW_RESPONSE=$(aws bedrock-agentcore-control create-gateway \
    --name "$GATEWAY_NAME" \
    --region "$AWS_REGION" \
    --description "MCP Gateway for GitHub tools (IAM auth)" \
    --role-arn "$GW_ROLE_ARN" \
    --protocol-type "MCP" \
    --authorizer-type "AWS_IAM" \
    --exception-level "DEBUG" \
    --output json)
  GATEWAY_ID=$(echo "$GW_RESPONSE" | jq -r '.gatewayId')
  GATEWAY_URL=$(echo "$GW_RESPONSE" | jq -r '.gatewayUrl // empty')
fi

state_set "gateway_id" "$GATEWAY_ID"
if [[ -n "$GATEWAY_URL" ]]; then
  state_set "gateway_url" "$GATEWAY_URL"
fi
echo "Gateway ID: ${GATEWAY_ID}"

# 3. Create the gateway target (runtime as MCP server)
echo ""
echo "--- Step 3: Create Gateway Target ---"
TARGET_NAME="GitHubMCP"

TARGET_ID=$(aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier "$GATEWAY_ID" \
  --region "$AWS_REGION" \
  --query "items[?name=='${TARGET_NAME}'].targetId | [0]" \
  --output text 2>/dev/null || true)
if [[ "$TARGET_ID" == "None" ]]; then
  TARGET_ID=""
fi

RUNTIME_ENDPOINT=$(agentcore_runtime_mcp_endpoint "$RUNTIME_ARN")
echo "Runtime MCP endpoint: ${RUNTIME_ENDPOINT}"

TARGET_CONFIG='{
  "mcp": {
    "mcpServer": {
      "endpoint": "'"${RUNTIME_ENDPOINT}"'",
      "listingMode": "DYNAMIC"
    }
  }
}'

CRED_CONFIG='[
  {
    "credentialProviderType": "GATEWAY_IAM_ROLE",
    "credentialProvider": {
      "iamCredentialProvider": {
        "service": "bedrock-agentcore",
        "region": "'"${AWS_REGION}"'"
      }
    }
  }
]'

if [[ -n "$TARGET_ID" ]]; then
  echo "Gateway target '${TARGET_NAME}' already exists (${TARGET_ID}). Updating..."
  TARGET_RESPONSE=$(aws bedrock-agentcore-control update-gateway-target \
    --gateway-identifier "$GATEWAY_ID" \
    --target-id "$TARGET_ID" \
    --name "$TARGET_NAME" \
    --region "$AWS_REGION" \
    --target-configuration "$TARGET_CONFIG" \
    --credential-provider-configurations "$CRED_CONFIG" \
    --output json)
else
  echo "Creating gateway target '${TARGET_NAME}'..."
  TARGET_RESPONSE=$(aws bedrock-agentcore-control create-gateway-target \
    --gateway-identifier "$GATEWAY_ID" \
    --name "$TARGET_NAME" \
    --region "$AWS_REGION" \
    --description "GitHub MCP Server on AgentCore Runtime" \
    --target-configuration "$TARGET_CONFIG" \
    --credential-provider-configurations "$CRED_CONFIG" \
    --output json)
  TARGET_ID=$(echo "$TARGET_RESPONSE" | jq -r '.targetId')
fi

state_set "gateway_target_name" "$TARGET_NAME"
state_set "gateway_target_id" "$TARGET_ID"

echo "Waiting for gateway target '${TARGET_NAME}' to become READY..."
TARGET_STATUS=""
for _ in $(seq 1 60); do
  TARGET_INFO=$(aws bedrock-agentcore-control get-gateway-target \
    --gateway-identifier "$GATEWAY_ID" \
    --target-id "$TARGET_ID" \
    --region "$AWS_REGION" \
    --output json)
  TARGET_STATUS=$(echo "$TARGET_INFO" | jq -r '.status // empty')
  if [[ "$TARGET_STATUS" == "READY" ]]; then
    break
  fi
  if [[ "$TARGET_STATUS" == "FAILED" \
    || "$TARGET_STATUS" == "UPDATE_UNSUCCESSFUL" \
    || "$TARGET_STATUS" == "SYNCHRONIZE_UNSUCCESSFUL" ]]; then
    echo "ERROR: Gateway target entered ${TARGET_STATUS}." >&2
    echo "$TARGET_INFO" | jq . >&2
    exit 1
  fi
  echo "  Status: ${TARGET_STATUS:-unknown}"
  sleep 5
done
if [[ "$TARGET_STATUS" != "READY" ]]; then
  echo "ERROR: Gateway target did not become READY." >&2
  exit 1
fi

# Fetch final gateway URL if not set yet
if [[ -z "$(state_get 'gateway_url')" ]]; then
  GW_INFO=$(aws bedrock-agentcore-control get-gateway \
    --gateway-identifier "$GATEWAY_ID" \
    --region "$AWS_REGION" 2>/dev/null || true)
  GATEWAY_URL=$(echo "$GW_INFO" | jq -r '.gatewayUrl // empty')
  if [[ -n "$GATEWAY_URL" ]]; then
    state_set "gateway_url" "$GATEWAY_URL"
  fi
fi

echo ""
echo "==> Gateway deployment complete."
echo "    Gateway ID: ${GATEWAY_ID}"
echo "    Target ID:  ${TARGET_ID}"
echo "    Gateway URL: $(state_get 'gateway_url')"
echo "    Auth: AWS IAM (SigV4)"
