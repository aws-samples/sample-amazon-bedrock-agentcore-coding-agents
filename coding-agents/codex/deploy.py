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


def _load_runtime_id(config_path: str):
    """Read a saved Runtime ID, or recover from a damaged local config."""
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("Warning: runtime_config.json is invalid; recovering from AgentCore.")
        return None
    if not isinstance(config, dict):
        print("Warning: runtime_config.json has an invalid shape; recovering from AgentCore.")
        return None
    runtime_id = config.get("runtime_id")
    return runtime_id if isinstance(runtime_id, str) and runtime_id else None


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
    """IAM Resource list for the S3Files statement.

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


def create_execution_role() -> str:
    session = boto3.Session(region_name=REGION)
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

    # Parse the registry account + region FROM the image URI, not the attendee's
    # infra.config. With a per-account image these equal ACCOUNT_ID/REGION; with a
    # PREBUILT image pulled from a central workshop ECR they are the central
    # account/region, so the ECR-pull grant below lands on the repo that actually
    # holds the image (cross-account pull). URI shape:
    #   <acct>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>
    ecr_repo = (
        ECR_URI.split("/", 1)[1].split("@", 1)[0].split(":", 1)[0]
        if "/" in ECR_URI else "coding-agents-codex"
    )
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
                # Scoped to the registry that actually holds the image (the central
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
            # No Gateway or SecretsManager grant: the coordinator owns GitHub.
            # The worker receives source archives and returns source archives.
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

    created_now = True
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
        created_now = False
        print(f"\nIAM role exists: {role_arn}")

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{AGENT_NAME}-policy",
        PolicyDocument=json.dumps(inline_policy),
    )

    if created_now:
        # A role the service can see is not yet a role the service can ASSUME: the trust
        # policy replicates to STS on its own clock. Ten seconds was enough on every
        # earlier event box; on 2026-09-03 a fresh account rejected a 10s-old role and
        # a 30s-old one alike, so the wait is a floor and deploy_runtime() below also
        # retries the validation failure itself instead of dying on the first answer.
        print("Waiting 20s for IAM propagation (new role)...")
        time.sleep(20)
    return role_arn




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
    session = boto3.Session(region_name=REGION)
    control = runtime_deploy.control_client(REGION, session)

    artifact = {"containerConfiguration": {"containerUri": ECR_URI}}
    network = {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": [SUBNET_1, SUBNET_2],
            "securityGroups": [SECURITY_GROUP],
        },
    }
    # Attach the S3 Files mount only when the access point is known (mountless until
    # the attendee creates it in Stage 1; re-running deploy.py then attaches it).
    fs_kwargs = {}
    if MOUNT_AP_ARN:
        fs_kwargs["filesystemConfigurations"] = [
            {
                "s3FilesAccessPoint": {
                    "accessPointArn": MOUNT_AP_ARN,
                    "mountPath": S3FILES_MOUNT_PATH,
                }
            }
        ]
    return runtime_deploy.deploy(control, dict(
        agentRuntimeName=AGENT_NAME,
        agentRuntimeArtifact=artifact,
        roleArn=role_arn,
        networkConfiguration=network,
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables=_runtime_environment(),
        description='Codex PTY agent',
        **fs_kwargs,
    ), runtime_id=_load_runtime_id(os.path.join(SCRIPT_DIR, "runtime_config.json")))


def main():
    runtime_deploy.require_v2_sdk()
    runtime_deploy.validate_environment(_runtime_environment())
    require_deploy_prereqs()

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
    print(f"  Runtime ARN: {runtime['runtime_arn']}")
    print(f"  S3 Files:    {S3FILES_MOUNT_PATH}")
    print("  Config:      codex/runtime_config.json")
    print("\n  Connect: python codex/connect.py")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
