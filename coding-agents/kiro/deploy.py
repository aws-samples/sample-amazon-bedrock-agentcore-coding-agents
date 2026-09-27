"""
Deploy Kiro runtime to AgentCore.

Prerequisites:
  - ../infra/setup.sh already ran (../infra.config exists)
  - ./setup.sh already ran (agent.config exists with ECR_URI)
  - GATEWAY_URL exported (the orchestrator provides it at deploy time)

Usage:
    python deploy.py
"""

import json
import os
import sys
import time

import boto3


# Share deployment behavior across served and restored roles.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runtime_deploy


load_dotconfig = runtime_deploy.load_dotconfig


def _load_runtime_id(config_path: str):
    """A saved Runtime ID, or None (see runtime_deploy.load_runtime_id)."""
    return runtime_deploy.load_runtime_id(config_path)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INFRA_CONFIG = os.path.join(SCRIPT_DIR, "..", "infra.config")
LOCAL_CONFIG = os.path.join(SCRIPT_DIR, "agent.config")

infra = load_dotconfig(INFRA_CONFIG)
local = load_dotconfig(LOCAL_CONFIG)

if not infra:
    print("Error: infra.config not found. Run ../infra/setup.sh first.")
    sys.exit(1)

# Region: the env (the box exports the STACK region), then infra.config, then boto3's
# own resolver. Never a literal: a hardcoded region deploys the Runtime somewhere the
# attendee's mount is not.
REGION = (os.environ.get("AWS_REGION")
          or infra.get("INFRA_REGION")
          or boto3.session.Session().region_name or "")
ACCOUNT_ID = infra["INFRA_ACCOUNT_ID"]
SUBNET_1 = infra["INFRA_SUBNET_1"]
SUBNET_2 = infra["INFRA_SUBNET_2"]
SECURITY_GROUP = infra["INFRA_SECURITY_GROUP"]
# Optional: empty until the attendee creates the S3 Files access point in Stage 1.
# Empty -> deploy MOUNTLESS; re-running deploy.py after it is set attaches the mount.
S3FILES_AP_ARN = infra.get("INFRA_S3FILES_AP_ARN", "")
# Bootstrap may prepare the Runtime's VPC networking before S3 Files mount
# targets are available. Keep IAM scoped to the real AP; defer only attachment.
MOUNT_AP_ARN = "" if os.environ.get("WORKSHOP_DEFER_MOUNT") == "1" else S3FILES_AP_ARN


_assert_same_region = runtime_deploy.assert_same_region
_assert_same_region(S3FILES_AP_ARN, REGION)

S3FILES_BUCKET = infra["INFRA_BUCKET"]
ECR_URI = local.get("ECR_URI") or os.environ.get("ECR_URI")

if not ECR_URI:
    print("Error: ECR_URI not found. Run ./setup.sh first.")
    sys.exit(1)

AGENT_NAME = local.get("AGENT_NAME", "kiro")
S3FILES_MOUNT_PATH = "/mnt/s3files"


def _s3files_policy_resources() -> list:
    """Resource ARNs for the S3Files IAM statement (runtime_deploy explains the scoping)."""
    return runtime_deploy.s3files_policy_resources(S3FILES_AP_ARN, REGION, ACCOUNT_ID)


def resolve_gateway_url() -> str:
    """Resolve GATEWAY_URL at deploy time, not import time.

    Order: env GATEWAY_URL first, then an optional sibling gateway state file
    if one happens to be present. Direct gateway access is optional: the
    workshop coordinator handles GitHub operations throughout the labs.
    """
    gateway_url = os.environ.get("GATEWAY_URL", "")
    gateway_state = os.path.join(SCRIPT_DIR, "..", "gateway", ".deployed-state.json")
    if not gateway_url and os.path.exists(gateway_state):
        with open(gateway_state) as f:
            gateway_url = json.load(f).get("gateway_url", "")
    # run.sh skips direct MCP configuration when this is empty. GitHub
    # credentials remain on the coordinator's Gateway in the served workshop.
    return gateway_url


session = boto3.Session(region_name=REGION)


def execution_role_documents() -> tuple[str, dict, dict]:
    """The role name, trust policy and inline policy, shared by deploy and --explain.

    Identical statements come from runtime_deploy; this role's own least-privilege
    choices are the inline ones below."""
    statements = [
        runtime_deploy.logs_statement(REGION, ACCOUNT_ID),
        {
            # Lab 3 telemetry, as runtime_deploy.telemetry_statement, plus
            # xray:PutTelemetryRecords for Kiro's collector.
            "Sid": "Telemetry",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogGroups",
                "logs:DescribeLogStreams",
                "xray:PutTraceSegments",
                "xray:PutTelemetryRecords",
                "xray:PutSpans",
                "xray:PutSpansForIndexing",
                "cloudwatch:PutMetricData",
            ],
            "Resource": ["*"],
        },
        runtime_deploy.bedrock_invoke_statement(REGION, ACCOUNT_ID),
        *runtime_deploy.ecr_statements(ECR_URI, default_repo='coding-agents-kiro',
                                       account_id=ACCOUNT_ID, region=REGION),
        *runtime_deploy.storage_statements(_s3files_policy_resources(), region=REGION,
                                           account_id=ACCOUNT_ID, bucket=S3FILES_BUCKET),
        runtime_deploy.gateway_statement(REGION, ACCOUNT_ID),
        runtime_deploy.eventbridge_statement(),
        runtime_deploy.identity_statement(),
        {
            # Kiro's API key is read at runtime via the AgentCore Identity Token
            # Vault API (GetWorkloadAccessToken + GetResourceApiKey above), not a
            # direct GetSecretValue; run.sh never calls GetSecretValue. Scope this
            # to ONLY the Identity-managed credential-provider secrets so a
            # prompt-injected agent cannot read unrelated secrets (e.g. the
            # isolated GitHub App private key at agentcore/github-mcp/*).
            "Sid": "SecretsManagerForTokenVault",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
            ],
            "Resource": [
                f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:bedrock-agentcore-identity*",
            ],
        },
    ]
    return (f"agentcore-{AGENT_NAME}-{REGION}-role",
            runtime_deploy.trust_policy(REGION, ACCOUNT_ID), runtime_deploy.policy(statements))


def create_execution_role() -> str:
    role_name, trust_policy, inline_policy = execution_role_documents()
    return runtime_deploy.create_execution_role(
        session.client("iam"), role_name, trust_policy, inline_policy,
        agent_name=AGENT_NAME, account_id=ACCOUNT_ID, sleep=time.sleep)


def _runtime_environment() -> dict:
    env_vars = {
        "AWS_REGION": REGION,
        # The collector sidecar names its CloudWatch log stream from this
        # (otel-collector-config.yaml). Unset, every agent shared one stream
        # literally called "agent", so you could not tell which agent wrote what.
        "WORKSHOP_AGENT_NAME": AGENT_NAME,
    }
    gateway_url = resolve_gateway_url()
    if gateway_url:
        env_vars["GATEWAY_URL"] = gateway_url
    model = os.environ.get("WORKSHOP_KIRO_MODEL", "").strip()
    if model:
        env_vars["WORKSHOP_KIRO_MODEL"] = model
    if "WORKSHOP_KIRO_EFFORT" in os.environ:
        env_vars["WORKSHOP_KIRO_EFFORT"] = os.environ["WORKSHOP_KIRO_EFFORT"].strip()
    # The Kiro API key is NEVER injected as a runtime environment variable: a
    # plaintext env var is readable by anyone who can GetAgentRuntime (the
    # participant can), which would leak the key. The key lives only in the
    # AgentCore Identity credential provider (Token Vault, KMS-encrypted in Secrets
    # Manager), provisioned by setup.sh from KIRO_API_KEY at deploy time; run.sh
    # fetches it on demand at session start via GetWorkloadAccessToken +
    # GetResourceApiKey using the runtime's own role. So KIRO_API_KEY is a
    # DEPLOY-TIME-only input to setup.sh, not a runtime env var here.

    return env_vars


def deploy_runtime(role_arn: str) -> dict:
    return runtime_deploy.deploy_role_runtime(
        runtime_deploy.control_client(REGION, session), name=AGENT_NAME,
        image=ECR_URI, role_arn=role_arn, subnets=[SUBNET_1, SUBNET_2],
        security_groups=[SECURITY_GROUP], mount_ap_arn=MOUNT_AP_ARN,
        mount_path=S3FILES_MOUNT_PATH, environment=_runtime_environment(),
        description='Kiro coding agent with shared S3 Files skills',
        runtime_id=_load_runtime_id(os.path.join(SCRIPT_DIR, "runtime_config.json")))


def main():
    runtime_deploy.require_runtime_sdk()
    runtime_deploy.validate_environment(_runtime_environment())
    if runtime_deploy.EXPLAIN:
        role_name, trust_policy, inline_policy = execution_role_documents()
        deploy_runtime(runtime_deploy.explain_role(
            boto3.Session(region_name=REGION).client("iam"), role_name, trust_policy,
            inline_policy, account_id=ACCOUNT_ID))
        return

    print("=" * 60)
    print(f"Deploying {AGENT_NAME} to AgentCore Runtime")
    print(f"  Region:      {REGION}")
    print(f"  Image:       {ECR_URI}")
    print(f"  S3 Files:    {MOUNT_AP_ARN or '(not attached)'}")
    print(f"  Direct gateway: {resolve_gateway_url() or 'not configured (the coordinator handles GitHub)'}")
    print("=" * 60)

    role_arn = create_execution_role()
    runtime = deploy_runtime(role_arn)

    config = {
        "agent_name": AGENT_NAME,
        **runtime,
        "region": REGION,
        "ecr_uri": ECR_URI,
        "s3files_access_point_arn": MOUNT_AP_ARN,
        "s3files_mount_path": S3FILES_MOUNT_PATH,
    }

    config_path = os.path.join(SCRIPT_DIR, "runtime_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    print("\n" + "=" * 60)
    print("Deployment complete!")
    runtime_deploy.print_receipt(runtime, region=REGION,
                                 saved_to="coding-agents/kiro/runtime_config.json")
    print("\n  Connect: python kiro/connect.py")
    print("=" * 60)


if __name__ == "__main__":
    # --explain: print the exact request (and the role) without any AWS write.
    runtime_deploy.EXPLAIN = "--explain" in sys.argv[1:]
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
