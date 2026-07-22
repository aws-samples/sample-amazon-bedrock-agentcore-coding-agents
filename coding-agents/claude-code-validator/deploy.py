"""
Deploy Claude Code (PTY/WebSocket) runtime to AgentCore.

Prerequisites:
  - infra.config exists (run ./setup-infra.sh)
  - Image built (run ./setup.sh)

Usage:
    python deploy.py
"""

import json
import os
import sys
import time

import boto3


def load_dotconfig(path):
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                cfg[key] = value.strip('"').strip("'")
    return cfg


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
INFRA_CONFIG = os.path.join(ROOT_DIR, "infra.config")
LOCAL_CONFIG = os.path.join(SCRIPT_DIR, "agent.config")

infra = load_dotconfig(INFRA_CONFIG)
local = load_dotconfig(LOCAL_CONFIG)

if not infra:
    print("Error: infra.config not found. Run ../infra/setup.sh first.")
    sys.exit(1)

REGION = os.environ.get("AWS_REGION", infra.get("INFRA_REGION", "us-west-2"))
ACCOUNT_ID = infra["INFRA_ACCOUNT_ID"]
SUBNET_1 = infra["INFRA_SUBNET_1"]
SUBNET_2 = infra["INFRA_SUBNET_2"]
SECURITY_GROUP = infra["INFRA_SECURITY_GROUP"]
# Optional: empty until the attendee creates the S3 Files access point in Stage 1
# and records it in infra.config. Empty -> deploy MOUNTLESS; re-running deploy.py
# after it is set attaches the mount via update_agent_runtime.
S3FILES_AP_ARN = infra.get("INFRA_S3FILES_AP_ARN", "")
S3FILES_BUCKET = infra["INFRA_BUCKET"]


def _s3files_policy_resources() -> list:
    """Resource ARNs for the S3Files IAM statement.

    When the access point is known, scope to that AP + its file system. When it is
    NOT known yet (the predeploy-mountless boot path: the attendee creates the
    access point on Stage 1 and a later re-run attaches it), scope to this account's
    S3Files file systems / access points in-region. Never emit empty-string ARNs,
    which would make put_role_policy reject the whole policy as malformed."""
    if S3FILES_AP_ARN:
        return [S3FILES_AP_ARN, S3FILES_AP_ARN.rsplit("/access-point/", 1)[0]]
    return [
        f"arn:aws:s3files:{REGION}:{ACCOUNT_ID}:file-system/*",
        f"arn:aws:s3files:{REGION}:{ACCOUNT_ID}:access-point/*",
    ]
ECR_URI = local.get("ECR_URI") or os.environ.get("ECR_URI")

if not ECR_URI:
    print("Error: ECR_URI not found. Run ./setup.sh first.")
    sys.exit(1)

AGENT_NAME = local.get("AGENT_NAME", "claude_code")
S3FILES_MOUNT_PATH = "/mnt/s3files"

session = boto3.Session(region_name=REGION)

print("=" * 60)
print(f"Deploying {AGENT_NAME} to AgentCore Runtime")
print(f"  Region:      {REGION}")
print(f"  Image:       {ECR_URI}")
print(f"  S3 Files:    {S3FILES_AP_ARN}")
print("=" * 60)


def create_execution_role() -> str:
    iam = session.client("iam")
    role_name = f"agentcore-{AGENT_NAME}-{REGION}-role"

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
                "Action": "sts:AssumeRole",
            },
            {
                "Effect": "Allow",
                "Principal": {"Service": "elasticfilesystem.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:s3files:{REGION}:{ACCOUNT_ID}:file-system/*"
                    },
                },
            },
        ],
    }

    # Parse the registry account + region FROM the image URI (per-account image ->
    # this account; PREBUILT image from a central workshop ECR -> that account), so
    # the ECR-pull grant below lands on the repo that actually holds the image.
    ecr_repo = ECR_URI.split("/")[1].split(":")[0] if "/" in ECR_URI else "coding-agents-claude-code"
    _reg = ECR_URI.split(".dkr.ecr.")[0] if ".dkr.ecr." in ECR_URI else ACCOUNT_ID
    ecr_account = _reg.split("/")[-1] if _reg else ACCOUNT_ID
    ecr_region = ECR_URI.split(".dkr.ecr.")[1].split(".")[0] if ".dkr.ecr." in ECR_URI else REGION

    inline_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "Logs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                "Resource": [
                    f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/*"
                ],
            },
            {
                # Lab 3 telemetry: the baked-in OpenTelemetry collector (started at
                # container boot by entrypoint.sh) ships this runtime's signals to
                # CloudWatch Logs (/workshop/coding-agents/telemetry + /metrics),
                # X-Ray Transaction Search (aws/spans), and CloudWatch metrics
                # (Workshop/CodingAgents). Without these the collector's exporters
                # get AccessDenied and telemetry never lands.
                "Sid": "Telemetry",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                    "xray:PutTraceSegments",
                    "xray:PutSpans",
                    "xray:PutSpansForIndexing",
                    "cloudwatch:PutMetricData",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "BedrockInvoke",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ListInferenceProfiles",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{REGION}:{ACCOUNT_ID}:*",
                ],
            },
            {
                # Stage 3 per-user cost: let this runtime assume the shared per-user
                # role (pre-baked by the workshop CFN) with the user as the
                # role-session-name, so its Bedrock calls are logged per user.
                "Sid": "AssumePerUser",
                "Effect": "Allow",
                "Action": ["sts:AssumeRole"],
                "Resource": [f"arn:aws:iam::{ACCOUNT_ID}:role/cca-peruser-{REGION}"],
            },
            {
                "Sid": "ECRAuth",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": ["*"],
            },
            {
                "Sid": "ECRPull",
                "Effect": "Allow",
                "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                # Scoped to the registry that actually holds the image (central
                # workshop account for a prebuilt pull, else this account).
                "Resource": [f"arn:aws:ecr:{ecr_region}:{ecr_account}:repository/{ecr_repo}"],
            },
            {
                "Sid": "S3Files",
                "Effect": "Allow",
                "Action": [
                    "s3files:GetAccessPoint",
                    "s3files:GetFileSystem",
                    "s3files:GetMountTarget",
                    "s3files:DescribeMountTargets",
                    "s3files:ListMountTargets",
                    "s3files:ClientMount",
                    "s3files:ClientWrite",
                    "s3files:ClientRootAccess",
                ],
                "Resource": _s3files_policy_resources(),
            },
            {
                "Sid": "EFS",
                "Effect": "Allow",
                "Action": [
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientWrite",
                    "elasticfilesystem:DescribeAccessPoints",
                    "elasticfilesystem:DescribeMountTargets",
                ],
                "Resource": [
                    f"arn:aws:elasticfilesystem:{REGION}:{ACCOUNT_ID}:file-system/*",
                    f"arn:aws:elasticfilesystem:{REGION}:{ACCOUNT_ID}:access-point/*",
                ],
            },
            {
                "Sid": "S3Bucket",
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:GetObject*",
                    "s3:PutObject*",
                    "s3:DeleteObject*",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [
                    f"arn:aws:s3:::{S3FILES_BUCKET}",
                    f"arn:aws:s3:::{S3FILES_BUCKET}/*",
                ],
            },
            {
                "Sid": "AgentCoreGateway",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeGateway"],
                "Resource": [f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/*"],
            },
            {
                "Sid": "EventBridge",
                "Effect": "Allow",
                "Action": [
                    "events:DeleteRule",
                    "events:DisableRule",
                    "events:EnableRule",
                    "events:PutRule",
                    "events:PutTargets",
                    "events:RemoveTargets",
                    "events:DescribeRule",
                    "events:ListRules",
                    "events:ListTargetsByRule",
                ],
                "Resource": ["arn:aws:events:*:*:rule/*"],
            },
        ],
    }

    try:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=f"Execution role for {AGENT_NAME} on AgentCore",
        )
        role_arn = resp["Role"]["Arn"]
        print(f"\nCreated IAM role: {role_arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"
        print(f"\nIAM role exists: {role_arn}")

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{AGENT_NAME}-policy",
        PolicyDocument=json.dumps(inline_policy),
    )

    print("Waiting 10s for IAM propagation...")
    time.sleep(10)
    return role_arn


def deploy_runtime(role_arn: str) -> dict:
    control = session.client("bedrock-agentcore-control", region_name=REGION)

    artifact = {"containerConfiguration": {"containerUri": ECR_URI}}
    network = {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": [SUBNET_1, SUBNET_2],
            "securityGroups": [SECURITY_GROUP],
        },
    }
    # Attach the S3 Files mount only when the access point is known. Mountless until
    # the attendee creates it in Stage 1; re-running deploy.py then attaches it. Pass
    # filesystemConfigurations only when there is an AP, so a mountless deploy omits
    # the key entirely rather than sending an empty list.
    fs_kwargs = {}
    if S3FILES_AP_ARN:
        fs_kwargs["filesystemConfigurations"] = [
            {
                "s3FilesAccessPoint": {
                    "accessPointArn": S3FILES_AP_ARN,
                    "mountPath": S3FILES_MOUNT_PATH,
                }
            }
        ]
    env_vars = {
        "AWS_REGION": REGION,
    }
    # Optional deploy-time model override: pass WORKSHOP_MODEL into the runtime so
    # run.sh uses it as the default (for accounts without Opus 4.6 Marketplace
    # access). Only forwarded when set, so the baked default stays the norm.
    if os.environ.get("WORKSHOP_MODEL"):
        env_vars["WORKSHOP_MODEL"] = os.environ["WORKSHOP_MODEL"]

    # Check if runtime already exists
    existing_id = None
    config_path = os.path.join(SCRIPT_DIR, "runtime_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            existing_id = json.load(f).get("runtime_id")

    if existing_id:
        try:
            control.get_agent_runtime(agentRuntimeId=existing_id)
            print(f"\nUpdating existing runtime '{existing_id}'...")
            control.update_agent_runtime(
                agentRuntimeId=existing_id,
                agentRuntimeArtifact=artifact,
                roleArn=role_arn,
                networkConfiguration=network,
                environmentVariables=env_vars,
                description="Claude Code PTY agent",
                **fs_kwargs,
            )
            runtime_id = existing_id
            runtime_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{existing_id}"
        except control.exceptions.ResourceNotFoundException:
            existing_id = None

    if not existing_id:
        print(f"\nCreating runtime '{AGENT_NAME}'...")
        response = control.create_agent_runtime(
            agentRuntimeName=AGENT_NAME,
            agentRuntimeArtifact=artifact,
            roleArn=role_arn,
            networkConfiguration=network,
            protocolConfiguration={"serverProtocol": "HTTP"},
            environmentVariables=env_vars,
            description="Claude Code PTY agent",
            **fs_kwargs,
        )
        runtime_id = response["agentRuntimeId"]
        runtime_arn = response["agentRuntimeArn"]

    print(f"Runtime ID: {runtime_id}")
    print("Waiting for READY...")
    while True:
        status_resp = control.get_agent_runtime(agentRuntimeId=runtime_id)
        status = status_resp["status"]
        print(f"  Status: {status}")
        if status == "READY":
            break
        if status in ("CREATE_FAILED", "UPDATE_FAILED"):
            print(f"Failed: {status_resp.get('failureReason', 'Unknown')}")
            sys.exit(1)
        time.sleep(15)

    return {"runtime_id": runtime_id, "runtime_arn": runtime_arn}


def main():
    role_arn = create_execution_role()
    runtime = deploy_runtime(role_arn)

    config = {
        "agent_name": AGENT_NAME,
        "runtime_id": runtime["runtime_id"],
        "runtime_arn": runtime["runtime_arn"],
        "region": REGION,
        "ecr_uri": ECR_URI,
        "s3files_access_point_arn": S3FILES_AP_ARN,
        "s3files_mount_path": S3FILES_MOUNT_PATH,
    }

    config_path = os.path.join(SCRIPT_DIR, "runtime_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    print("\n" + "=" * 60)
    print("Deployment complete!")
    print(f"  Runtime ARN: {runtime['runtime_arn']}")
    print(f"  S3 Files:    {S3FILES_MOUNT_PATH}")
    print("  Config:      claude-code-validator/runtime_config.json")
    print("\n  Connect: python claude-code-validator/connect.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
