"""
Deploy Codex (PTY/WebSocket) runtime to AgentCore.

Prerequisites:
  - infra.config exists (run ../infra/setup.sh)
  - Image built (run ./setup.sh)

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
# infra.config lives at the src/coding-agents/ root (sibling of this harness dir).
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
INFRA_CONFIG = os.path.join(ROOT_DIR, "infra.config")
LOCAL_CONFIG = os.path.join(SCRIPT_DIR, "agent.config")

infra = load_dotconfig(INFRA_CONFIG)
local = load_dotconfig(LOCAL_CONFIG)

# Resolve everything tolerantly at import time so the module can be imported for
# tests/tooling without the deploy prerequisites present. Hard requirements (infra.config,
# ECR_URI) are only enforced inside main() when an actual deploy runs.
REGION = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
          or infra.get("INFRA_REGION") or boto3.session.Session().region_name or "")
ACCOUNT_ID = infra.get("INFRA_ACCOUNT_ID", "")
SUBNET_1 = infra.get("INFRA_SUBNET_1", "")
SUBNET_2 = infra.get("INFRA_SUBNET_2", "")
SECURITY_GROUP = infra.get("INFRA_SECURITY_GROUP", "")
S3FILES_AP_ARN = infra.get("INFRA_S3FILES_AP_ARN", "")
MOUNT_AP_ARN = "" if os.environ.get("WORKSHOP_DEFER_MOUNT") == "1" else S3FILES_AP_ARN
S3FILES_BUCKET = infra.get("INFRA_BUCKET", "")
ECR_URI = local.get("ECR_URI") or os.environ.get("ECR_URI", "")

AGENT_NAME = local.get("AGENT_NAME", "codex")
S3FILES_MOUNT_PATH = "/mnt/s3files"


def _assert_same_region(ap_arn, region):
    if not ap_arn or not region:
        return
    parts = ap_arn.split(":")
    ap_region = parts[3] if len(parts) > 3 else ""
    if ap_region and ap_region != region:
        raise SystemExit(
            f"REGION_MISMATCH: S3 Files access point is in {ap_region}, "
            f"but the Runtime would deploy to {region}. Set AWS_REGION to "
            f"{ap_region}, or use an access point in {region}.")


_assert_same_region(S3FILES_AP_ARN, REGION)


def _s3files_policy_resources() -> list:
    """Resource ARNs for the S3Files IAM statement (runtime_deploy explains the scoping)."""
    return runtime_deploy.s3files_policy_resources(S3FILES_AP_ARN, REGION, ACCOUNT_ID)


def require_deploy_prereqs():
    """Enforce deploy prerequisites. Called from main(), not at import."""
    if not infra:
        print("Error: infra.config not found. Run ../infra/setup.sh first.")
        sys.exit(1)
    if not REGION:
        raise SystemExit("No AWS region. Set AWS_REGION or INFRA_REGION.")
    if not ECR_URI:
        print("Error: ECR_URI not found. Run ./setup.sh first.")
        sys.exit(1)


def execution_role_documents() -> tuple[str, dict, dict]:
    """The role name, trust policy and inline policy, shared by deploy and --explain.

    Identical statements come from runtime_deploy; this role's own least-privilege
    choices are the inline ones below."""
    statements = [
        runtime_deploy.logs_statement(REGION, ACCOUNT_ID),
        {
            # Codex's collector writes usage to this one log group; the grant is
            # scoped to it instead of the broader Telemetry statement other roles use.
            "Sid": "UsageLogs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
            ],
            "Resource": [
                f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/workshop/coding-agents/telemetry",
                f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/workshop/coding-agents/telemetry:*",
            ],
        },
        {
            # The collector also sends Codex's OTLP metrics to the CloudWatch metrics
            # endpoint behind Coding Agent Insights, which authorizes each SigV4-signed
            # request as PutMetricData. That action has no resource-level scoping.
            "Sid": "InsightsMetrics",
            "Effect": "Allow",
            "Action": ["cloudwatch:PutMetricData"],
            "Resource": ["*"],
        },
        {
            "Sid": "BedrockOpenAIInference",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            "Resource": [
                # Cross-region profiles can route to another model region.
                # Grant only the OpenAI family, not every foundation model.
                "arn:aws:bedrock:*::foundation-model/openai.*",
                f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:inference-profile/*.openai.*",
            ],
        },
        {
            "Sid": "BedrockRuntimeProjectInvoke",
            "Effect": "Allow",
            # The OpenAI-compatible API also requires the default project,
            # in addition to the inference target grants above.
            "Action": ["bedrock:InvokeModel"],
            "Resource": [
                f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:project/default",
            ],
        },
        *runtime_deploy.ecr_statements(ECR_URI, default_repo='coding-agents-codex',
                                       account_id=ACCOUNT_ID, region=REGION),
        *runtime_deploy.storage_statements(_s3files_policy_resources(), region=REGION,
                                           account_id=ACCOUNT_ID, bucket=S3FILES_BUCKET),
        # No Gateway or SecretsManager grant: the coordinator owns GitHub.
        # The worker receives source archives and returns source archives.
        runtime_deploy.eventbridge_statement(),
    ]
    return (f"agentcore-{AGENT_NAME}-{REGION}-role",
            runtime_deploy.trust_policy(REGION, ACCOUNT_ID), runtime_deploy.policy(statements))


def create_execution_role() -> str:
    role_name, trust_policy, inline_policy = execution_role_documents()
    return runtime_deploy.create_execution_role(
        boto3.Session(region_name=REGION).client("iam"), role_name, trust_policy, inline_policy,
        agent_name=AGENT_NAME, account_id=ACCOUNT_ID, sleep=time.sleep)


def _runtime_environment() -> dict:
    env_vars = {
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "WORKSHOP_AGENT_NAME": AGENT_NAME,
    }
    for name in ("WORKSHOP_CODEX_MODEL", "WORKSHOP_MODEL_CODEX", "WORKSHOP_MODEL"):
        value = os.environ.get(name, "").strip()
        if value:
            env_vars[name] = value
    return env_vars


def deploy_runtime(role_arn: str) -> dict:
    return runtime_deploy.deploy_role_runtime(
        runtime_deploy.control_client(REGION, boto3.Session(region_name=REGION)), name=AGENT_NAME,
        image=ECR_URI, role_arn=role_arn, subnets=[SUBNET_1, SUBNET_2],
        security_groups=[SECURITY_GROUP], mount_ap_arn=MOUNT_AP_ARN,
        mount_path=S3FILES_MOUNT_PATH, environment=_runtime_environment(),
        description='Codex PTY agent',
        runtime_id=_load_runtime_id(os.path.join(SCRIPT_DIR, "runtime_config.json")))


def main():
    runtime_deploy.require_runtime_sdk()
    runtime_deploy.validate_environment(_runtime_environment())
    require_deploy_prereqs()

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
    print(f"  S3 Files:    {MOUNT_AP_ARN or '(mount deferred or not configured)'}")
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
                                 saved_to="coding-agents/codex/runtime_config.json")
    print("\n  Connect: python codex/connect.py")
    print("=" * 60)


if __name__ == "__main__":
    # --explain: print the exact request (and the role) without any AWS write.
    runtime_deploy.EXPLAIN = "--explain" in sys.argv[1:]
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
