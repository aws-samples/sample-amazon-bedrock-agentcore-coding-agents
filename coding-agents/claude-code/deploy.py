"""
Deploy Claude Code (PTY/WebSocket) runtime to AgentCore.

Prerequisites:
  - infra.config exists (run ./setup-infra.sh)
  - Image built (run ./setup.sh)

Usage:
    python deploy.py              # create or update the Runtime
    python deploy.py --explain    # print the request; write nothing
    python deploy.py --prepare    # create the role, print the console form values
    python deploy.py --adopt      # check the console-built Runtime, then record it
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
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
INFRA_CONFIG = os.path.join(ROOT_DIR, "infra.config")
LOCAL_CONFIG = os.path.join(SCRIPT_DIR, "agent.config")

infra = load_dotconfig(INFRA_CONFIG)
local = load_dotconfig(LOCAL_CONFIG)

if not infra:
    print("Error: infra.config not found. Run ../infra/setup.sh first.")
    sys.exit(1)

# Region: the env (the box exports the STACK region), then infra.config, then
# boto3's own resolver. Never a literal: a hardcoded region deploys the Runtime
# somewhere the attendee's mount is not.
REGION = (os.environ.get("AWS_REGION")
          or infra.get("INFRA_REGION")
          or boto3.session.Session().region_name or "")
ACCOUNT_ID = infra["INFRA_ACCOUNT_ID"]
SUBNET_1 = infra["INFRA_SUBNET_1"]
SUBNET_2 = infra["INFRA_SUBNET_2"]
SECURITY_GROUP = infra["INFRA_SECURITY_GROUP"]
# Optional: empty until the attendee creates the S3 Files access point in Stage 1
# and records it in infra.config. Empty -> deploy MOUNTLESS; re-running deploy.py
# after it is set attaches the mount via update_agent_runtime.
S3FILES_AP_ARN = infra.get("INFRA_S3FILES_AP_ARN", "")

_assert_same_region = runtime_deploy.assert_same_region
_assert_same_region(S3FILES_AP_ARN, REGION)

S3FILES_BUCKET = infra["INFRA_BUCKET"]


def _s3files_policy_resources() -> list:
    """Resource ARNs for the S3Files IAM statement (runtime_deploy explains the scoping)."""
    return runtime_deploy.s3files_policy_resources(S3FILES_AP_ARN, REGION, ACCOUNT_ID)


ECR_URI = local.get("ECR_URI") or os.environ.get("ECR_URI")

if not ECR_URI:
    print("Error: ECR_URI not found. Run ./setup.sh first.")
    sys.exit(1)

AGENT_NAME = local.get("AGENT_NAME", "claude_code")
S3FILES_MOUNT_PATH = "/mnt/s3files"

session = boto3.Session(region_name=REGION)

# A preview or the console path says so itself; neither is a deployment.
if not {"--explain", "--prepare", "--adopt"} & set(sys.argv[1:]):
    print("=" * 60)
    print(f"Deploying {AGENT_NAME} to AgentCore Runtime")
    print(f"  Region:      {REGION}")
    print(f"  Image:       {ECR_URI}")
    print(f"  S3 Files:    {S3FILES_AP_ARN}")
    print("=" * 60)


def execution_role_documents() -> tuple[str, dict, dict]:
    """The role name, trust policy and inline policy, shared by deploy and --explain.

    Identical statements come from runtime_deploy; this role's own least-privilege
    choices are the inline ones below."""
    statements = [
        runtime_deploy.logs_statement(REGION, ACCOUNT_ID),
        runtime_deploy.telemetry_statement(),
        runtime_deploy.bedrock_invoke_statement(REGION, ACCOUNT_ID),
        runtime_deploy.assume_peruser_statement(REGION, ACCOUNT_ID),
        *runtime_deploy.ecr_statements(ECR_URI, default_repo='coding-agents-claude-code',
                                       account_id=ACCOUNT_ID, region=REGION),
        *runtime_deploy.storage_statements(_s3files_policy_resources(), region=REGION,
                                           account_id=ACCOUNT_ID, bucket=S3FILES_BUCKET),
        runtime_deploy.gateway_statement(REGION, ACCOUNT_ID),
        runtime_deploy.eventbridge_statement(),
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
    # Keep the stack default and the more specific model overrides available to
    # the launcher. Forward only these public settings, never host credentials.
    for name in ("WORKSHOP_CLAUDE_MODEL", "WORKSHOP_MODEL",
                 "WORKSHOP_MODEL_CLAUDE_CODE"):
        value = os.environ.get(name, "").strip()
        if value:
            env_vars[name] = value
    # An explicitly empty effort means "use the CLI's own default"; omitting it
    # would restore the launcher's medium setting after deployment.
    if "WORKSHOP_CLAUDE_EFFORT" in os.environ:
        env_vars["WORKSHOP_CLAUDE_EFFORT"] = os.environ["WORKSHOP_CLAUDE_EFFORT"].strip()

    return env_vars


def deploy_runtime(role_arn: str) -> dict:
    return runtime_deploy.deploy_role_runtime(
        runtime_deploy.control_client(REGION, session), name=AGENT_NAME,
        image=ECR_URI, role_arn=role_arn, subnets=[SUBNET_1, SUBNET_2],
        security_groups=[SECURITY_GROUP], mount_ap_arn=S3FILES_AP_ARN,
        mount_path=S3FILES_MOUNT_PATH, environment=_runtime_environment(),
        description='Claude Code PTY agent',
        runtime_id=_load_runtime_id(os.path.join(SCRIPT_DIR, "runtime_config.json")))


def _console_expectations() -> dict:
    return dict(image=ECR_URI, role_name=execution_role_documents()[0],
                subnets=[SUBNET_1, SUBNET_2], security_groups=[SECURITY_GROUP],
                mount_ap_arn=S3FILES_AP_ARN, mount_path=S3FILES_MOUNT_PATH,
                environment=_runtime_environment())


def prepare_for_console() -> None:
    """Create the execution role, then print what to enter in the console form."""
    if not S3FILES_AP_ARN:
        raise SystemExit("infra.config has no S3 Files access point yet; ask a facilitator.")
    role_arn = create_execution_role()
    names = runtime_deploy.console_names(session, subnets=[SUBNET_1, SUBNET_2],
                                         security_groups=[SECURITY_GROUP],
                                         mount_ap_arn=S3FILES_AP_ARN)
    print(f"Execution role ready: {role_arn}")
    print("  It lets the agent pull its image, call Bedrock, mount the shared folder and")
    print("  send telemetry. Read it in IAM > Roles; the console form only selects it.\n")
    for line in runtime_deploy.console_sheet(
            name=AGENT_NAME, platform=runtime_deploy.selected_platform(), image=ECR_URI,
            role_name=execution_role_documents()[0], names=names,
            mount_path=S3FILES_MOUNT_PATH, environment=_runtime_environment()):
        print(line)


def adopt_console_runtime() -> None:
    """Check the Runtime the person created in the console, then record it."""
    control = runtime_deploy.control_client(REGION, session)
    record, checks = runtime_deploy.adopt_console_runtime(
        control, name=AGENT_NAME, platform=runtime_deploy.selected_platform(),
        **_console_expectations())
    print(f"Checking the Runtime named {AGENT_NAME} that you created in the console:")
    runtime_deploy.print_checks(checks)
    if record is None:
        raise SystemExit("\nNot recorded. Fix the FAIL lines in the console (Update runtime), "
                         "then run --adopt again.")
    config = {"agent_name": AGENT_NAME, **record, "region": REGION, "ecr_uri": ECR_URI,
              "s3files_access_point_arn": S3FILES_AP_ARN,
              "s3files_mount_path": S3FILES_MOUNT_PATH, "created_in": "console"}
    with open(os.path.join(SCRIPT_DIR, "runtime_config.json"), "w") as f:
        json.dump(config, f, indent=2)
    print("")
    runtime_deploy.print_receipt(record, region=REGION,
                                 saved_to="coding-agents/claude-code/runtime_config.json")


def main():
    runtime_deploy.require_runtime_sdk()
    runtime_deploy.validate_environment(_runtime_environment())
    if runtime_deploy.CONSOLE_MODE == "--prepare":
        prepare_for_console()
        return
    if runtime_deploy.CONSOLE_MODE == "--adopt":
        adopt_console_runtime()
        return
    if runtime_deploy.EXPLAIN:
        role_name, trust_policy, inline_policy = execution_role_documents()
        deploy_runtime(runtime_deploy.explain_role(
            boto3.Session(region_name=REGION).client("iam"), role_name, trust_policy,
            inline_policy, account_id=ACCOUNT_ID))
        return
    role_arn = create_execution_role()
    runtime = deploy_runtime(role_arn)

    config = {
        "agent_name": AGENT_NAME,
        **runtime,
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
    runtime_deploy.print_receipt(runtime, region=REGION,
                                 saved_to="coding-agents/claude-code/runtime_config.json")
    print("\n  Connect: python claude-code/connect.py")
    print("=" * 60)


if __name__ == "__main__":
    # --explain: print the exact request (and the role) without any AWS write.
    runtime_deploy.EXPLAIN = "--explain" in sys.argv[1:]
    # --prepare / --adopt: the console path (role and form values, then the check).
    runtime_deploy.CONSOLE_MODE = next(
        (arg for arg in sys.argv[1:] if arg in ("--prepare", "--adopt")), "")
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
