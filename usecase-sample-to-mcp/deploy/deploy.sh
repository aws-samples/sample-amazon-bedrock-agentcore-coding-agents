#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USECASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_ROOT="$(cd "$USECASE_DIR/.." && pwd)"
DELIVERABLE_DIR="${1:-$SOURCE_ROOT/deliverable}"

CONFIGURED_REGION="$(aws configure get region 2>/dev/null || true)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-${CONFIGURED_REGION:-us-west-2}}}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
RUNTIME_NAME="cost_analyzer_mcp_runtime"
ECR_REPO="cost-analyzer-mcp"
ROLE_NAME="agentcore-cost-analyzer-mcp-role"
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:latest"
STATE_FILE="$SCRIPT_DIR/.deployed-state.json"
GATEWAY_CODING_DIR="${WORKSHOP_CODING_AGENTS_DIR:-$HOME/src/coding-agents}"
GATEWAY_STATE="$GATEWAY_CODING_DIR/gateway_mcp/.deployed-state.json"

if [[ ! -f "$DELIVERABLE_DIR/mcp_server.py" ]]; then
  echo "ERROR: missing $DELIVERABLE_DIR/mcp_server.py" >&2
  echo "Usage: $0 <checked-out-pr>/deliverable" >&2
  exit 1
fi
if [[ ! -f "$GATEWAY_STATE" ]]; then
  echo "ERROR: deploy the Gateway first: coding-agents/gateway_mcp/deploy-all.sh" >&2
  exit 1
fi

for command in aws jq; do
  command -v "$command" >/dev/null || { echo "ERROR: $command is required" >&2; exit 1; }
done

BUILD_DIR="$(mktemp -d /tmp/cost-analyzer-mcp.XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT
cp "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR/runtime_app.py" "$BUILD_DIR/"
cp "$DELIVERABLE_DIR/mcp_server.py" "$BUILD_DIR/"
cp "$USECASE_DIR/cost_analyzer.py" "$BUILD_DIR/"

aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" \
  >/dev/null 2>&1 || aws ecr create-repository --repository-name "$ECR_REPO" \
  --image-scanning-configuration scanOnPush=true --region "$REGION" >/dev/null

source "$SOURCE_ROOT/coding-agents/_build_push.sh"
build_and_push_arm64 "$IMAGE_URI" "$BUILD_DIR/Dockerfile" "$BUILD_DIR" \
  "$REGION" "$ACCOUNT_ID"

TRUST_POLICY="$(jq -n --arg account "$ACCOUNT_ID" --arg region "$REGION" '{
  Version:"2012-10-17", Statement:[{
    Effect:"Allow", Principal:{Service:"bedrock-agentcore.amazonaws.com"},
    Action:"sts:AssumeRole", Condition:{
      StringEquals:{"aws:SourceAccount":$account},
      ArnLike:{"aws:SourceArn":("arn:aws:bedrock-agentcore:"+$region+":"+$account+":*")}
    }
  }]
}')"

EXECUTION_POLICY="$(jq -n --arg account "$ACCOUNT_ID" --arg region "$REGION" --arg repo "$ECR_REPO" '{
  Version:"2012-10-17", Statement:[
    {Effect:"Allow",Action:["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
     Resource:("arn:aws:ecr:"+$region+":"+$account+":repository/"+$repo)},
    {Effect:"Allow",Action:"ecr:GetAuthorizationToken",Resource:"*"},
    {Effect:"Allow",Action:["logs:DescribeLogGroups","logs:DescribeLogStreams","logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],Resource:"*"},
    {Effect:"Allow",Action:["xray:PutTraceSegments","xray:PutTelemetryRecords","xray:GetSamplingRules","xray:GetSamplingTargets","cloudwatch:PutMetricData"],Resource:"*"}
  ]
}')"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
    --policy-document "$TRUST_POLICY"
else
  ROLE_ARN="$(aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" --query Role.Arn --output text)"
fi
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name AgentCoreRuntimeExecution --policy-document "$EXECUTION_POLICY"

RUNTIME_ID="$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeId | [0]" \
  --output text 2>/dev/null || true)"
if [[ -n "$RUNTIME_ID" && "$RUNTIME_ID" != "None" ]]; then
  EXISTING="$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION")"
  RUNTIME_ARN="$(jq -r .agentRuntimeArn <<<"$EXISTING")"
  aws bedrock-agentcore-control update-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" \
    --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$IMAGE_URI\"}}" \
    --role-arn "$ROLE_ARN" --network-configuration '{"networkMode":"PUBLIC"}' \
    --protocol-configuration '{"serverProtocol":"MCP"}' \
    --lifecycle-configuration '{"idleRuntimeSessionTimeout":600,"maxLifetime":3300}' \
    >/dev/null
else
  CREATED="$(aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "$RUNTIME_NAME" --region "$REGION" \
    --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$IMAGE_URI\"}}" \
    --role-arn "$ROLE_ARN" --network-configuration '{"networkMode":"PUBLIC"}' \
    --protocol-configuration '{"serverProtocol":"MCP"}' \
    --lifecycle-configuration '{"idleRuntimeSessionTimeout":600,"maxLifetime":3300}')"
  RUNTIME_ID="$(jq -r .agentRuntimeId <<<"$CREATED")"
  RUNTIME_ARN="$(jq -r .agentRuntimeArn <<<"$CREATED")"
fi

for _ in $(seq 1 90); do
  STATUS="$(aws bedrock-agentcore-control get-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$REGION" --query status --output text)"
  [[ "$STATUS" == "READY" ]] && break
  [[ "$STATUS" == "CREATE_FAILED" || "$STATUS" == "UPDATE_FAILED" ]] && {
    echo "ERROR: runtime entered $STATUS" >&2; exit 1;
  }
  sleep 5
done
[[ "${STATUS:-}" == "READY" ]] || { echo "ERROR: runtime did not reach READY" >&2; exit 1; }

GATEWAY_ID="$(jq -r .gateway_id "$GATEWAY_STATE")"
GATEWAY_URL="$(jq -r .gateway_url "$GATEWAY_STATE")"
GATEWAY_ROLE_NAME="$(jq -r .gateway_role_name "$GATEWAY_STATE")"
GITHUB_RUNTIME_ARN="$(jq -r .runtime_arn "$GATEWAY_STATE")"

GATEWAY_POLICY="$(jq -n --arg github "$GITHUB_RUNTIME_ARN" --arg cost "$RUNTIME_ARN" '{
  Version:"2012-10-17", Statement:[{Effect:"Allow",Action:"bedrock-agentcore:InvokeAgentRuntime",
  Resource:[$github,($github+"/*"),$cost,($cost+"/*")]}]
}')"
aws iam put-role-policy --role-name "$GATEWAY_ROLE_NAME" \
  --policy-name AgentCoreGatewayExecution --policy-document "$GATEWAY_POLICY"

ENCODED_RUNTIME_ARN="$(jq -rn --arg value "$RUNTIME_ARN" '$value | @uri')"
RUNTIME_ENDPOINT="https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ENCODED_RUNTIME_ARN}/invocations?qualifier=DEFAULT"
TARGET_CONFIG="$(jq -n --arg endpoint "$RUNTIME_ENDPOINT" \
  '{mcp:{mcpServer:{endpoint:$endpoint,listingMode:"DYNAMIC"}}}')"
CREDENTIAL_CONFIG="$(jq -n --arg region "$REGION" \
  '[{credentialProviderType:"GATEWAY_IAM_ROLE",credentialProvider:{iamCredentialProvider:{service:"bedrock-agentcore",region:$region}}}]')"

TARGET_ID="$(aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier "$GATEWAY_ID" --region "$REGION" \
  --query "items[?name=='CostAnalyzerMCP'].targetId | [0]" --output text 2>/dev/null || true)"
if [[ -n "$TARGET_ID" && "$TARGET_ID" != "None" ]]; then
  aws bedrock-agentcore-control update-gateway-target --gateway-identifier "$GATEWAY_ID" \
    --target-id "$TARGET_ID" --name CostAnalyzerMCP --region "$REGION" \
    --target-configuration "$TARGET_CONFIG" \
    --credential-provider-configurations "$CREDENTIAL_CONFIG" >/dev/null
else
  TARGET_CREATED="$(aws bedrock-agentcore-control create-gateway-target --gateway-identifier "$GATEWAY_ID" \
    --name CostAnalyzerMCP --description "Graded cost analyzer MCP server" \
    --region "$REGION" --target-configuration "$TARGET_CONFIG" \
    --credential-provider-configurations "$CREDENTIAL_CONFIG")"
  TARGET_ID="$(jq -r .targetId <<<"$TARGET_CREATED")"
fi

jq -n --arg runtime_id "$RUNTIME_ID" --arg runtime_arn "$RUNTIME_ARN" \
  --arg gateway_id "$GATEWAY_ID" --arg gateway_url "$GATEWAY_URL" \
  --arg gateway_target_id "$TARGET_ID" \
  '{runtime_id:$runtime_id,runtime_arn:$runtime_arn,gateway_id:$gateway_id,
    gateway_url:$gateway_url,gateway_target_name:"CostAnalyzerMCP",
    gateway_target_id:$gateway_target_id}' > "$STATE_FILE"

echo "Runtime READY: $RUNTIME_ARN"
echo "Gateway target: CostAnalyzerMCP"
echo "Gateway URL: $GATEWAY_URL"
