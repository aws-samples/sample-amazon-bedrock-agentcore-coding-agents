"""
Deploy opencode (PTY/WebSocket) runtime to AgentCore.

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
# ECR_URI, GATEWAY_URL) are only enforced inside main() when an actual deploy runs.
# Region: the env (the box exports the STACK region), then infra.config, then
# boto3's own resolver. Never a literal: a hardcoded region deploys the Runtime
# somewhere the attendee's mount is not.
REGION = (os.environ.get("AWS_REGION")
          or infra.get("INFRA_REGION")
          or boto3.session.Session().region_name or "")
ACCOUNT_ID = infra.get("INFRA_ACCOUNT_ID", "")
SUBNET_1 = infra.get("INFRA_SUBNET_1", "")
SUBNET_2 = infra.get("INFRA_SUBNET_2", "")
SECURITY_GROUP = infra.get("INFRA_SECURITY_GROUP", "")
S3FILES_AP_ARN = infra.get("INFRA_S3FILES_AP_ARN", "")
# Bootstrap may prepare the Runtime's VPC networking before S3 Files mount
# targets are available. Keep IAM scoped to the real AP; defer only attachment.
MOUNT_AP_ARN = "" if os.environ.get("WORKSHOP_DEFER_MOUNT") == "1" else S3FILES_AP_ARN

_assert_same_region = runtime_deploy.assert_same_region
_assert_same_region(S3FILES_AP_ARN, REGION)

S3FILES_BUCKET = infra.get("INFRA_BUCKET", "")
ECR_URI = local.get("ECR_URI") or os.environ.get("ECR_URI", "")

AGENT_NAME = local.get("AGENT_NAME", "opencode")
S3FILES_MOUNT_PATH = "/mnt/s3files"


def _s3files_policy_resources() -> list:
    """Resource ARNs for the S3Files IAM statement (runtime_deploy explains the scoping)."""
    return runtime_deploy.s3files_policy_resources(S3FILES_AP_ARN, REGION, ACCOUNT_ID)


# GATEWAY_URL comes from env first. The optional gateway_mcp deployed-state file may not
# exist in this layout, so only read it when present, never hard-fail at import.
GATEWAY_MCP_STATE = os.path.join(ROOT_DIR, "..", "gateway_mcp", ".deployed-state.json")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
if not GATEWAY_URL and os.path.exists(GATEWAY_MCP_STATE):
    with open(GATEWAY_MCP_STATE) as f:
        GATEWAY_URL = json.load(f).get("gateway_url", "")


def require_deploy_prereqs():
    """Enforce deploy prerequisites. Called from main(), not at import."""
    if not infra:
        print("Error: infra.config not found. Run ../infra/setup.sh first.")
        sys.exit(1)
    if not ECR_URI:
        print("Error: ECR_URI not found. Run ./setup.sh first.")
        sys.exit(1)
    if not GATEWAY_URL:
        # Lab 1 attaches the shared mount BEFORE the Lab 2 gateway exists, so a
        # missing GATEWAY_URL is the expected state there, not an error.
        print("Note: no GATEWAY_URL set; deploying without gateway support.")
        print("  This is expected in Lab 1. The Lab 2 gateway deploy wires it later.")


def execution_role_documents() -> tuple[str, dict, dict]:
    """The role name, trust policy and inline policy, shared by deploy and --explain.

    Identical statements come from runtime_deploy; this role's own least-privilege
    choices are the inline ones below."""
    statements = [
        runtime_deploy.logs_statement(REGION, ACCOUNT_ID),
        runtime_deploy.telemetry_statement(),
        {
            # Broader than runtime_deploy.bedrock_invoke_statement: opencode also
            # calls GetFoundationModel and ListFoundationModels.
            "Sid": "BedrockInvoke",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:ListInferenceProfiles",
                "bedrock:GetFoundationModel",
                "bedrock:ListFoundationModels",
            ],
            "Resource": [
                "arn:aws:bedrock:*::foundation-model/*",
                f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:*",
            ],
        },
        *runtime_deploy.ecr_statements(ECR_URI, default_repo='coding-agents-opencode',
                                       account_id=ACCOUNT_ID, region=REGION),
        *runtime_deploy.storage_statements(_s3files_policy_resources(), region=REGION,
                                           account_id=ACCOUNT_ID, bucket=S3FILES_BUCKET),
        runtime_deploy.identity_statement(),
        {
            "Sid": "BedrockApiKey",
            "Effect": "Allow",
            "Action": [
                "bedrock:CallWithBearerToken",
                "sts:GetCallerIdentity",
            ],
            "Resource": ["*"],
        },
        runtime_deploy.gateway_statement(REGION, ACCOUNT_ID),
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
        # The collector sidecar names its CloudWatch log stream from this
        # (otel-collector-config.yaml). Unset, every agent shared one stream
        # literally called "agent", so you could not tell which agent wrote what.
        "WORKSHOP_AGENT_NAME": AGENT_NAME,
    }
    if GATEWAY_URL:
        env_vars["GATEWAY_URL"] = GATEWAY_URL
    # Model overrides, forwarded so entrypoint.sh can rewrite the baked config with
    # them at boot. Without this the container falls back to the id baked into the
    # image, which is exactly the drift that made a us-east-1 runtime call us-west-2.
    # Only forwarded when SET, so the image default stays the default.
    for _var in ("WORKSHOP_SMALL_MODEL", "WORKSHOP_OPENCODE_MODEL"):
        _val = (os.environ.get(_var) or "").strip()
        if _val:
            env_vars[_var] = _val
    return env_vars


def deploy_runtime(role_arn: str) -> dict:
    return runtime_deploy.deploy_role_runtime(
        runtime_deploy.control_client(REGION, boto3.Session(region_name=REGION)), name=AGENT_NAME,
        image=ECR_URI, role_arn=role_arn, subnets=[SUBNET_1, SUBNET_2],
        security_groups=[SECURITY_GROUP], mount_ap_arn=MOUNT_AP_ARN,
        mount_path=S3FILES_MOUNT_PATH, environment=_runtime_environment(),
        description='opencode PTY agent',
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
    print(f"  S3 Files:    {MOUNT_AP_ARN or '(not attached)'}")
    if GATEWAY_URL:
        print(f"  Gateway URL: {GATEWAY_URL}")
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
                                 saved_to="coding-agents/opencode/runtime_config.json")
    print("\n  Connect: python opencode/connect.py")
    print("=" * 60)


if __name__ == "__main__":
    # --explain: print the exact request (and the role) without any AWS write.
    runtime_deploy.EXPLAIN = "--explain" in sys.argv[1:]
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
