"""
Delete the Kiro AgentCore runtime, its IAM role, and Identity resources.
Does NOT delete shared infra (VPC, S3 Files); use ./cleanup-infra.sh for that.

Usage:
    python cleanup.py
    python cleanup.py --keep-identity   # Keep Token Vault credentials (reusable)
"""

import os
import sys

import boto3


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))
import role_cleanup  # noqa: E402

# Must match what setup.sh and run.sh use
WORKLOAD_NAME = "kiro-coding-agent"
CREDENTIAL_NAME = "kiro-api-key"


def destroy_identity(region: str):
    """
    Remove AgentCore Identity resources (workload identity + credential provider).
    The credential provider holds the encrypted API key in Secrets Manager;
    deleting it permanently removes the key.
    """
    session = boto3.Session(region_name=region)
    control = session.client("bedrock-agentcore-control", region_name=region)

    # Delete credential provider (encrypted API key in Secrets Manager)
    try:
        control.delete_api_key_credential_provider(name=CREDENTIAL_NAME)
        print(f"  Deleted credential provider: {CREDENTIAL_NAME}")
    except Exception as e:
        if "ResourceNotFoundException" in str(type(e).__name__) or "not found" in str(e).lower():
            print(f"  Credential provider not found: {CREDENTIAL_NAME}")
        else:
            print(f"  Warning deleting credential provider: {e}")

    # Delete workload identity
    try:
        control.delete_workload_identity(name=WORKLOAD_NAME)
        print(f"  Deleted workload identity: {WORKLOAD_NAME}")
    except Exception as e:
        if "ResourceNotFoundException" in str(type(e).__name__) or "not found" in str(e).lower():
            print(f"  Workload identity not found: {WORKLOAD_NAME}")
        else:
            print(f"  Warning deleting workload identity: {e}")


def main():
    args = role_cleanup.parse_args("Clean up the Kiro AgentCore runtime", keep_identity=True)

    def identity(region: str) -> None:
        if args.keep_identity:
            print("  Keeping Identity resources (--keep-identity)")
        else:
            print("  Destroying Identity resources...")
            destroy_identity(region)
            print("  Identity resources destroyed. Re-run ./setup.sh to recreate them.")

    # The Runtime, IAM role, and ECR repository go through the shared teardown.
    return role_cleanup.cleanup(SCRIPT_DIR, extra=identity)


if __name__ == "__main__":
    sys.exit(main())
