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


# ONE region per workshop, enforced rather than documented. The access point ARN
# carries the region it was created in, so if the mount and this Runtime disagree the
# Runtime comes up unable to reach /mnt/s3files, and the failure surfaces much later
# as an agent that "wrote nothing". With two accessible regions an attendee can
# genuinely end up here (create the file system in one terminal's region, deploy from
# another), so refuse the deploy while the fix is still one line.
def _assert_same_region(ap_arn: str, region: str) -> None:
    if not ap_arn or not region:
        return
    parts = ap_arn.split(":")
    ap_region = parts[3] if len(parts) > 3 else ""
    if ap_region and ap_region != region:
        raise SystemExit(
            f"REGION_MISMATCH: this deploy targets {region}, but the S3 Files access\n"
            f"point in coding-agents/infra.config was created in {ap_region}:\n"
            f"  {ap_arn}\n"
            "The mount and the Runtime must be in the SAME region. Either export\n"
            f"AWS_REGION={ap_region} and re-run this deploy, or re-create the file\n"
            f"system in {region} (Lab 1) and update infra.config."
        )


_assert_same_region(S3FILES_AP_ARN, REGION)

S3FILES_BUCKET = infra["INFRA_BUCKET"]
ECR_URI = local.get("ECR_URI") or os.environ.get("ECR_URI")

if not ECR_URI:
    print("Error: ECR_URI not found. Run ./setup.sh first.")
    sys.exit(1)

AGENT_NAME = local.get("AGENT_NAME", "kiro")
S3FILES_MOUNT_PATH = "/mnt/s3files"


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
    ecr_repo = (
        ECR_URI.split("/", 1)[1].split("@", 1)[0].split(":", 1)[0]
        if "/" in ECR_URI else "coding-agents-kiro"
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
                # Lab 3 telemetry: the baked-in OpenTelemetry collector ships this
                # runtime's signals to CloudWatch Logs (/workshop/coding-agents/*),
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
                    "xray:PutTelemetryRecords",
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
                "Action": [
                    "bedrock-agentcore:InvokeGateway",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:gateway/*",
                ],
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
            {
                "Sid": "AgentCoreIdentity",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetResourceApiKey",
                ],
                "Resource": ["*"],
            },
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
        # The collector sidecar names its CloudWatch log stream from this
        # (otel-collector-config.yaml). Unset, every agent shared one stream
        # literally called "agent", so you could not tell which agent wrote what.
        "WORKSHOP_AGENT_NAME": AGENT_NAME,
    }
    gateway_url = resolve_gateway_url()
    if gateway_url:
        env_vars["GATEWAY_URL"] = gateway_url
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
    control = runtime_deploy.control_client(REGION, session)

    artifact = {
        "containerConfiguration": {
            "containerUri": ECR_URI,
        }
    }
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
        description='Kiro coding agent with shared S3 Files skills',
        **fs_kwargs,
    ), runtime_id=_load_runtime_id(os.path.join(SCRIPT_DIR, "runtime_config.json")))


def main():
    runtime_deploy.require_runtime_sdk()
    runtime_deploy.validate_environment(_runtime_environment())
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
    print(f"  Runtime ARN: {runtime['runtime_arn']}")
    print(f"  S3 Files:    {S3FILES_MOUNT_PATH if MOUNT_AP_ARN else '(not attached)'}")
    print("  Config:      kiro/runtime_config.json")
    print("\n  Test: python kiro/invoke.py \"List the files in your working directory "
          "and say which steering file you are reading\"")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
