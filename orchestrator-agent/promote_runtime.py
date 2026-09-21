"""Promote the exact CLI/CDK-owned Coordinator Runtime to platform V2."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coding-agents"))
import runtime_deploy


def promote_project(project: Path, *, target_name: str = "default",
                    runtime_name: str = "orchestrator", timeout_s=None, control=None) -> dict:
    """Preserve CDK ownership; a successful CDK stack is not proof of V2."""
    config_root = project / "agentcore"
    targets = json.loads((config_root / "aws-targets.json").read_text(encoding="utf-8"))
    matches = [target for target in targets if target.get("name") == target_name]
    if len(matches) != 1:
        raise runtime_deploy.RuntimeDeploymentError("Select exactly one configured deployment target.")
    target = matches[0]
    state_path = config_root / ".cli" / "deployed-state.json"
    state = runtime_deploy.read_state(state_path)
    try:
        entry = state["targets"][target_name]["resources"]["runtimes"][runtime_name]
        runtime_id, arn = entry["runtimeId"], entry["runtimeArn"]
        accepted_version = runtime_deploy.runtime_version_number(entry.get("runtimeVersion"))
    except (KeyError, TypeError) as exc:
        raise runtime_deploy.RuntimeDeploymentError(
            f"No deployed Runtime for {target_name}/{runtime_name}; complete agentcore deploy first."
        ) from exc
    match = re.fullmatch(
        r"arn:(aws(?:-[a-z]+)?):bedrock-agentcore:([^:]+):([0-9]{12}):runtime/([^/]+)", arn
    )
    if (not match or match[2] != target["region"] or match[3] != target["account"]
            or match[4] != runtime_id):
        raise runtime_deploy.RuntimeDeploymentError(
            "Deployed Runtime ID/ARN does not match the configured account and region."
        )
    control = control if control is not None else runtime_deploy.control_client(target["region"])
    result = runtime_deploy.promote(
        control, runtime_id, timeout_s=timeout_s, expected_arn=arn,
        minimum_version=accepted_version,
    )

    # CLI 0.30.0 models runtimeVersion, but has no platformVersion field.
    # Refresh only the revision; preserve all other targets, sessions and resources.
    latest = runtime_deploy.read_state(state_path)
    current = latest["targets"][target_name]["resources"]["runtimes"][runtime_name]
    if (current.get("runtimeId") != runtime_id or current.get("runtimeArn") != arn
            or runtime_deploy.runtime_version_number(current.get("runtimeVersion")) != accepted_version):
        raise runtime_deploy.RuntimeDeploymentError(
            "CLI deployment state changed during promotion; refusing to overwrite it."
        )
    verified_version = runtime_deploy.runtime_version_number(result["runtime_version"])
    if verified_version < accepted_version:
        raise runtime_deploy.RuntimeDeploymentError(
            "Verified Runtime revision predates the accepted CLI deployment; refusing to overwrite it."
        )
    current["runtimeVersion"] = verified_version
    runtime_deploy.write_state(state_path, latest)
    receipt_path = config_root / ".cli" / "runtime-platforms.json"
    receipts = runtime_deploy.read_state(receipt_path)
    receipts.setdefault(target_name, {})[runtime_name] = result
    runtime_deploy.write_state(receipt_path, receipts)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True,
                        help="AgentCore project directory, for example CodingAgents")
    parser.add_argument("--target", default="default")
    parser.add_argument("--runtime", default="orchestrator")
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args()
    result = promote_project(args.project, target_name=args.target,
                             runtime_name=args.runtime, timeout_s=args.timeout)
    print(f"Coordinator {result['runtime_id']} revision {result['runtime_version']}: "
          f"{result['platform_version']} READY")


if __name__ == "__main__":
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
