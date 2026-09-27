"""Tear down one role's AgentCore Runtime, IAM role, and ECR repository.

Every role folder's ``cleanup.py`` calls this, so there is one teardown instead of
five near-identical copies. The copies also shared a real defect: with no
``runtime_config.json`` (a role whose image the stack pre-built but whose Runtime was
never deployed, such as the ``claude-code-validator`` restore path) they printed
"Nothing to clean up" and exited before deleting the image repository, which kept
billing on an account the attendee believed was clean. The repository is now
located from ``agent.config`` when there is no Runtime to delete.

Shared infrastructure (VPC, S3 Files) is never touched here.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable

import boto3


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_dotconfig(path: str) -> dict:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, sep, value = line.strip().partition("=")
                if sep and key and not key.startswith("#"):
                    values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def _region(runtime: dict, script_dir: str) -> str | None:
    infra = _read_dotconfig(os.path.join(os.path.dirname(script_dir), "infra.config"))
    return (runtime.get("region") or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION") or infra.get("INFRA_REGION"))


def parse_args(description: str, argv: list[str] | None = None, *,
               keep_identity: bool = False):
    """Parse a cleanup.py command line: `--help` must PRINT help, never tear down.

    The copies ignored their arguments, so `python3 cleanup.py --help` ran a real
    teardown (observed 2026-09-27; only fake credentials stopped the AWS deletes).
    """
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(description=description)
    if keep_identity:
        parser.add_argument("--keep-identity", action="store_true",
                            help="Keep Identity resources (workload + credential provider) for reuse")
    return parser.parse_args(argv)


def cleanup(script_dir: str, *, extra: Callable[[str], None] | None = None) -> int:
    """Delete what this role's setup.sh and deploy.py created. Returns an exit code.

    Local configuration is removed only when every delete succeeded or found
    nothing to delete: removing it after a failed delete lost the Runtime ID the
    next attempt needs.
    """
    runtime = _read_json(os.path.join(script_dir, "runtime_config.json"))
    agent = _read_dotconfig(os.path.join(script_dir, "agent.config"))
    agent_name = runtime.get("agent_name") or agent.get("AGENT_NAME")
    if not agent_name:
        print("No runtime_config.json or agent.config found. Nothing to clean up.")
        return 0
    region = _region(runtime, script_dir)
    if not region:
        print("No region: set AWS_REGION, then run this again.")
        return 1

    session = boto3.Session(region_name=region)
    print(f"Cleaning up: {agent_name}\n")
    failed = False

    runtime_id = runtime.get("runtime_id")
    if runtime_id:
        try:
            print(f"  Deleting runtime: {runtime_id}")
            session.client("bedrock-agentcore-control").delete_agent_runtime(
                agentRuntimeId=runtime_id)
            print("  Waiting for deletion...")
            time.sleep(30)
        except Exception as exc:  # noqa: BLE001 - report and continue the teardown
            print(f"  Warning: {exc}")
            failed = True
    else:
        print("  No Runtime was deployed for this role (no runtime_config.json).")

    iam = session.client("iam")
    role_name = f"agentcore-{agent_name}-{region}-role"
    try:
        for policy_name in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
        iam.delete_role(RoleName=role_name)
        print(f"  Deleted IAM role: {role_name}")
    except iam.exceptions.NoSuchEntityException:
        print(f"  IAM role not found: {role_name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  Warning: {exc}")
        failed = True

    # The image repository, forcibly with its images, so nothing keeps billing.
    # Naming mirrors setup.sh: coding-agents-<agent-name-with-dashes>.
    ecr = session.client("ecr")
    repo = agent.get("ECR_REPO") or f"coding-agents-{agent_name.replace('_', '-')}"
    try:
        ecr.delete_repository(repositoryName=repo, force=True)
        print(f"  Deleted ECR repo: {repo}")
    except ecr.exceptions.RepositoryNotFoundException:
        print(f"  ECR repo not found: {repo}")
    except Exception as exc:  # noqa: BLE001
        print(f"  Warning: {exc}")
        failed = True

    if extra is not None:
        extra(region)

    if failed:
        print("\nSome deletes failed (warnings above), so runtime_config.json and "
              "agent.config were kept for the next attempt. Fix the cause and run again.")
        return 1
    for name in ("runtime_config.json", "agent.config"):
        path = os.path.join(script_dir, name)
        if os.path.exists(path):
            os.remove(path)
    print("\nDone. Shared infra (VPC, S3 Files) was kept.")
    return 0
