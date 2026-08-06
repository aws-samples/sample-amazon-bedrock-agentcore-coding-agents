"""Wire the AgentCore CLI project to the workshop's deployed resources.

This patches one runtime entry in ``agentcore/agentcore.json`` after
``agentcore add agent``. It keeps generated ARNs and account-specific values out
of source while making the deployed orchestrator self-contained.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import boto3


HERE = Path(__file__).resolve().parent

# The roster comes from the role REGISTRY (``orchestrator/roles.py``), the one place
# it is declared, so this script wires exactly the roles the engine dispatches. This
# module is loaded standalone (by the CLI and by its test), so reach the registry by
# path rather than assuming the orchestrator dir is already importable.
sys.path.insert(0, str(HERE.parent / "orchestrator"))
import roles as _roles  # noqa: E402


def ROLES() -> tuple[str, ...]:
    """The role ids whose deployed runtime ARNs the orchestrator project needs."""
    return _roles.roster_ids()


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
    for role in ROLES():
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
    final_merge_policy = (
        os.environ.get("WORKSHOP_FINAL_MERGE_POLICY")
        or os.environ.get("WORKSHOP_MERGE_POLICY")
        or "human_review"
    )
    env = {
        # One AGENTCORE_RUNTIME_<ROLE> per served role, keyed the way
        # runtime_config._env_key derives it, so the coordinator finds every role
        # it can dispatch and a roster change needs no edit here.
        **{f"AGENTCORE_RUNTIME_{role.replace('-', '_').upper()}": arn
           for role, arn in arns.items()},
        "PERUSER_ROLE_ARN": peruser_role,
        "GITHUB_GATEWAY_URL": os.environ.get("GITHUB_GATEWAY_URL", ""),
        "GITHUB_REPO": os.environ.get("GITHUB_REPO", ""),
        "WORKSHOP_FINAL_MERGE_POLICY": final_merge_policy,
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
    # WORKSHOP_ROLES rides in for the same reason, and its absence was a REAL defect:
    # the roster is read from the coordinator's OWN process env (roles.roster()), so a
    # facilitator who exported the documented Kiro fallback
    # (WORKSHOP_ROLES=claude-code,opencode,claude-code-validator) and redeployed still
    # got a coordinator serving the DEFAULT roster, which then failed pre-flight with
    # RUNTIME_NOT_WIRED:kiro -- exactly the failure the fallback exists to avoid.
    # `agentcore deploy` has no env flag, so this file is the only place it can enter.
    # Only forwarded when actually set, so the default deploy is unchanged.
    roles_override = os.environ.get("WORKSHOP_ROLES", "").strip()
    if roles_override:
        env["WORKSHOP_ROLES"] = roles_override
    # Same argument for the Kiro model pin: the validator's model is resolved from the
    # dispatching process env, so an operator override must reach the coordinator.
    kiro_model = os.environ.get("WORKSHOP_KIRO_MODEL", "").strip()
    if kiro_model:
        env["WORKSHOP_KIRO_MODEL"] = kiro_model
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
