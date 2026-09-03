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

S3FILES_BUCKET = infra.get("INFRA_BUCKET", "")
ECR_URI = local.get("ECR_URI") or os.environ.get("ECR_URI", "")

AGENT_NAME = local.get("AGENT_NAME", "opencode")
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
    ecr_repo = ECR_URI.split("/")[1].split(":")[0] if "/" in ECR_URI else "coding-agents-opencode"
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
                    "bedrock:GetFoundationModel",
                    "bedrock:ListFoundationModels",
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
                "Sid": "BedrockApiKey",
                "Effect": "Allow",
                "Action": [
                    "bedrock:CallWithBearerToken",
                    "sts:GetCallerIdentity",
                ],
                "Resource": ["*"],
            },
            # No SecretsManager grant: opencode authenticates to Bedrock with the
            # runtime role's SigV4 creds and reaches GitHub only through the Gateway
            # (InvokeGateway below). run.sh never calls GetSecretValue. A blanket
            # secret:* read would let prompt-injected model output exfiltrate other
            # secrets (e.g. the isolated GitHub App private key), so it is omitted.
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


def _create_runtime_with_role_retry(control, kwargs: dict, budget_s: int = 240):
    """CreateAgentRuntime, retrying ONLY the 'Role validation failed' answer.

    That answer means the control plane could not yet assume the execution role it was
    handed, which for a role created seconds ago is IAM propagation, not a wrong trust
    policy (the policy is the same one the pre-deployed roles pass with). Seen live on
    2026-09-03: a 10s-old role and a 30s-old role both got it, and a one-shot script that
    dies on it leaves an attendee re-running a command that then fails the same way. The
    retry says what it is waiting for and gives up loudly after the budget so a genuinely
    wrong role still fails, later but honestly. Every OTHER error is raised immediately.
    """
    deadline = time.monotonic() + budget_s
    attempt = 0
    while True:
        attempt += 1
        try:
            return control.create_agent_runtime(**kwargs)
        except Exception as exc:  # noqa: BLE001 - narrowed on the very next line
            # Deliberately NOT control.exceptions.ValidationException: that attribute is
            # generated per API version, so naming it can itself raise AttributeError
            # inside the error path, which is the worst possible place to learn that.
            # The message is what identifies this condition, and anything else re-raises
            # unchanged on the next line, including the ConflictException the caller
            # handles.
            if "Role validation failed" not in str(exc) or time.monotonic() >= deadline:
                raise
            print(f"  Role not assumable by the service yet (attempt {attempt}); "
                  f"IAM is still propagating the new role. Retrying in 20s...")
            time.sleep(20)


def deploy_runtime(role_arn: str) -> dict:
    session = boto3.Session(region_name=REGION)
    control = session.client("bedrock-agentcore-control", region_name=REGION)

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

    config_path = os.path.join(SCRIPT_DIR, "runtime_config.json")
    existing_id = _load_runtime_id(config_path)

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
                description="opencode PTY agent",
                **fs_kwargs,
            )
            runtime_id = existing_id
            runtime_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{existing_id}"
        except control.exceptions.ResourceNotFoundException:
            existing_id = None

    if not existing_id:
        print(f"\nCreating runtime '{AGENT_NAME}'...")
        try:
            response = _create_runtime_with_role_retry(control, dict(
                agentRuntimeName=AGENT_NAME,
                agentRuntimeArtifact=artifact,
                roleArn=role_arn,
                networkConfiguration=network,
                protocolConfiguration={"serverProtocol": "HTTP"},
                environmentVariables=env_vars,
                description="opencode PTY agent",
                **fs_kwargs,
            ))
            runtime_id = response["agentRuntimeId"]
            runtime_arn = response["agentRuntimeArn"]
        except control.exceptions.ConflictException:
            # A runtime with this name already exists (e.g. the local
            # runtime_config.json was lost but the boot pre-deploy already created
            # it). Look it up by name and UPDATE instead of failing, so deploy.py is
            # idempotent against the real AWS state, not just the local file.
            print(f"Runtime '{AGENT_NAME}' already exists; updating it instead...")
            found = None
            paginator = control.get_paginator("list_agent_runtimes")
            for page in paginator.paginate():
                for rt in page.get("agentRuntimes", []):
                    if rt.get("agentRuntimeName") == AGENT_NAME:
                        found = rt["agentRuntimeId"]
                        break
                if found:
                    break
            if not found:
                raise
            control.update_agent_runtime(
                agentRuntimeId=found,
                agentRuntimeArtifact=artifact,
                roleArn=role_arn,
                networkConfiguration=network,
                environmentVariables=env_vars,
                description="opencode PTY agent",
                **fs_kwargs,
            )
            runtime_id = found
            runtime_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/{found}"

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
    require_deploy_prereqs()

    print("=" * 60)
    print(f"Deploying {AGENT_NAME} to AgentCore Runtime")
    print(f"  Region:      {REGION}")
    print(f"  Image:       {ECR_URI}")
    print(f"  S3 Files:    {S3FILES_AP_ARN}")
    if GATEWAY_URL:
        print(f"  Gateway URL: {GATEWAY_URL}")
    print("=" * 60)

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
    print("  Config:      opencode/runtime_config.json")
    print("\n  Connect: python opencode/connect.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
