"""Wire the AgentCore CLI project to the workshop's deployed resources.

This patches one runtime entry in ``agentcore/agentcore.json`` after
``agentcore add agent``. It keeps generated ARNs and account-specific values out
of source while making the deployed orchestrator self-contained.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import boto3


HERE = Path(__file__).resolve().parent
# The validator role is a second Claude Code (claude-code-validator); kiro was
# retired from the roster (kept in the codebase, off every roster, like codex).
ROLES = ("claude-code", "opencode", "claude-code-validator")


def _region(value: str | None) -> str:
    return value or os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-west-2")


# The two role ARNs configure_deploy needs. They have DETERMINISTIC names set by
# the workshop CloudFormation (RoleName: agentcore-orchestrator-<region>-role and
# cca-peruser-<region>), so we construct them from account + region and do NOT
# require reading CloudFormation outputs. That matters on the workshop box: the
# instance role's cloudformation:DescribeStacks is scoped to 'coding-agents-*'
# stacks, but Workshop Studio names the stack 'cfn', so a DescribeStacks lookup
# would AccessDenied. CFN outputs, when readable, are used only as an override.
def _derived_role_arns(account_id: str, region: str) -> dict[str, str]:
    return {
        "OrchestratorRuntimeRoleArn":
            f"arn:aws:iam::{account_id}:role/agentcore-orchestrator-{region}-role",
        "PerUserRoleArn":
            f"arn:aws:iam::{account_id}:role/cca-peruser-{region}",
    }


def _stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    """Best-effort CloudFormation outputs (an OVERRIDE source for the role ARNs).

    Never fatal: if the stack is not found under ``stack_name`` or DescribeStacks
    is denied (the common case on the workshop box), return {} and let the caller
    fall back to the deterministic derived ARNs."""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
        if stacks:
            return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    except cfn.exceptions.ClientError:
        pass
    return {}


def _runtime_arns(source_root: Path) -> dict[str, str]:
    arns: dict[str, str] = {}
    for role in ROLES:
        path = source_root / "coding-agents" / role / "runtime_config.json"
        try:
            arn = json.loads(path.read_text(encoding="utf-8"))["runtime_arn"]
        except (OSError, KeyError, ValueError) as exc:
            raise SystemExit(f"runtime ARN missing for {role}: {path} ({exc})") from exc
        if not isinstance(arn, str) or not arn.startswith("arn:aws:bedrock-agentcore:"):
            raise SystemExit(f"invalid runtime ARN for {role}: {arn!r}")
        arns[role] = arn
    return arns


def configure(project_file: Path, source_root: Path, outputs: dict[str, str],
              region: str, account_id: str) -> dict:
    data = json.loads(project_file.read_text(encoding="utf-8"))
    runtime = next((r for r in data.get("runtimes", []) if r.get("name") == "orchestrator"), None)
    if runtime is None:
        raise SystemExit("orchestrator runtime missing; run agentcore add agent first")

    execution_role = outputs.get("OrchestratorRuntimeRoleArn")
    peruser_role = outputs.get("PerUserRoleArn")
    if not execution_role or not peruser_role:
        raise SystemExit(
            "stack outputs OrchestratorRuntimeRoleArn and PerUserRoleArn are required"
        )

    arns = _runtime_arns(source_root)
    env = {
        "AGENTCORE_RUNTIME_CLAUDE_CODE": arns["claude-code"],
        "AGENTCORE_RUNTIME_OPENCODE": arns["opencode"],
        "AGENTCORE_RUNTIME_CLAUDE_CODE_VALIDATOR": arns["claude-code-validator"],
        "PERUSER_ROLE_ARN": peruser_role,
        "GITHUB_GATEWAY_URL": os.environ.get("GITHUB_GATEWAY_URL", ""),
        "GITHUB_REPO": os.environ.get("GITHUB_REPO", ""),
        "WORKSHOP_MERGE_POLICY": os.environ.get("WORKSHOP_MERGE_POLICY", "human_review"),
        "WORKSHOP_BEDROCK_REGION": region,
        "WORKSHOP_EXECUTOR": "agentcore",
        "WORKSHOP_GITHUB_SECRET": "agentcore/workshop/github-connection",
        "WORKSHOP_GITHUB_STORE": "secretsmanager",
        "WORKSHOP_RUNS_DIR": "/tmp/workshop-runs",
        "WORKSHOP_RUNTIME_BUCKET": f"coding-agents-{account_id}-{region}",
    }
    # The engine resolves each role's dispatch model from ITS OWN process env
    # (WORKSHOP_MODEL_<ROLE> then WORKSHOP_MODEL, engine._role_model), so an
    # own-account model override exported at deploy time must ride into the
    # coordinator runtime or accounts without Opus access dispatch a model
    # they cannot invoke.
    for var, value in os.environ.items():
        if var == "WORKSHOP_MODEL" or var.startswith("WORKSHOP_MODEL_"):
            env[var] = value
    runtime["executionRoleArn"] = execution_role
    runtime["envVars"] = [{"name": name, "value": value} for name, value in sorted(env.items())]

    project_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=project_file.parent,
                                     delete=False) as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, project_file)

    # Pin the CLI's deploy target to the workshop region. `agentcore deploy`
    # creates the default target lazily and hardcodes us-east-1 (it does not
    # read AWS_DEFAULT_REGION or the --region passed to `add agent`), which
    # lands the whole stack in a region where the workshop's IAM and runtimes
    # do not exist. Writing aws-targets.json here makes the target explicit.
    targets_file = project_file.parent / "aws-targets.json"
    targets = [{"name": "default",
                "description": f"Workshop target ({region})",
                "account": account_id,
                "region": region}]
    targets_file.write_text(json.dumps(targets, indent=2) + "\n", encoding="utf-8")

    return {"project": str(project_file), "execution_role": execution_role,
            "target": {"account": account_id, "region": region},
            "runtimes": arns, "environment": sorted(env)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path,
                        default=HERE.parent / "CodingAgents" / "agentcore" / "agentcore.json")
    parser.add_argument("--source-root", type=Path, default=HERE.parent)
    parser.add_argument("--stack-name", default=os.environ.get(
        "WORKSHOP_STACK_NAME", "coding-agents-workshop"))
    parser.add_argument("--region")
    args = parser.parse_args()

    region = _region(args.region)
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    # Deterministic role ARNs are the source of truth; CFN outputs (when readable)
    # override them. This works whether the stack is named coding-agents-workshop
    # (own account) or cfn (Workshop Studio), and even when DescribeStacks is denied.
    outputs = {**_derived_role_arns(account_id, region),
               **_stack_outputs(args.stack_name, region)}
    result = configure(args.project.resolve(), args.source_root.resolve(), outputs,
                       region, account_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
