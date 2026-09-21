"""Promote the exact CLI/CDK-owned Coordinator Runtime to platform V2."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coding-agents"))
import runtime_deploy


def _remaining(deadline):
    seconds = deadline - runtime_deploy.time.monotonic()
    if seconds <= 0:
        raise runtime_deploy.RuntimeDeploymentError("Coordinator promotion deadline expired.")
    return seconds


def _sdk_property(value):
    if isinstance(value, list):
        return [_sdk_property(item) for item in value]
    if isinstance(value, dict):
        return {key[:1].lower() + key[1:]: _sdk_property(item)
                for key, item in value.items()}
    return value


def _contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value) for key, value in expected.items())
    return actual == expected


class _CdkControl:
    """Gate service reads against the completed CDK operation, not a guessed revision."""

    def __init__(self, control, target, resources, entry, deadline):
        import boto3
        from botocore.config import Config

        self.control, self.entry, self.deadline = control, entry, deadline
        self.cfn = boto3.Session(region_name=target["region"]).client(
            "cloudformation", config=Config(
                connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1}))
        self.stack = self.cfn.describe_stacks(StackName=resources["stackName"])["Stacks"][0]
        stack_arn = self.stack.get("StackId", "").split(":", 5)
        if (len(stack_arn) != 6 or stack_arn[2:5] != [
                "cloudformation", target["region"], target["account"]]
                or self.stack.get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}):
            raise runtime_deploy.RuntimeDeploymentError(
                "Coordinator CDK stack is not complete in the selected account and region.")
        complete, started = None, None
        request = {"StackName": self.stack["StackId"]}
        finished = False
        for _ in range(10):
            _remaining(deadline)
            page = self.cfn.describe_stack_events(**request)
            for event in page["StackEvents"]:
                if event.get("ResourceType") != "AWS::BedrockAgentCore::Runtime":
                    continue
                if complete is None:
                    if event.get("PhysicalResourceId") != entry["runtimeId"]:
                        continue
                    if event.get("ResourceStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
                        raise runtime_deploy.RuntimeDeploymentError(
                            "The selected Runtime's latest CDK operation is not complete.")
                    complete = event
                elif event.get("LogicalResourceId") == complete["LogicalResourceId"]:
                    operation = complete["ResourceStatus"].removesuffix("_COMPLETE")
                    if event.get("ResourceStatus") == operation + "_IN_PROGRESS":
                        started = runtime_deploy._timestamp(event["Timestamp"])
                        if operation == "CREATE" and not event.get("PhysicalResourceId"):
                            finished = True
                            break
                    else:
                        finished = True
                        break
            if finished or not page.get("NextToken"):
                break
            request["NextToken"] = page["NextToken"]
        else:
            raise runtime_deploy.RuntimeDeploymentError("CDK Runtime event lookup exceeded its bounded history.")
        if complete is None or started is None:
            raise runtime_deploy.RuntimeDeploymentError(
                "No completed CDK Runtime operation and start time match the selected Runtime.")
        properties = json.loads(complete.get("ResourceProperties") or "{}")
        required = {"AgentRuntimeName", "AgentRuntimeArtifact", "RoleArn", "NetworkConfiguration"}
        if not required <= properties.keys() or properties["RoleArn"] != entry.get("roleArn"):
            raise runtime_deploy.RuntimeDeploymentError("The completed CDK Runtime properties are incomplete or mismatched.")
        fields = (*runtime_deploy._UPDATE_FIELDS, "agentRuntimeName")
        self.expected = {
            field: _sdk_property(properties[field[:1].upper() + field[1:]])
            for field in fields if field[:1].upper() + field[1:] in properties
        }
        # Environment names are user keys, not CloudFormation property names.
        self.expected["environmentVariables"] = properties.get("EnvironmentVariables", {})
        self.started = started
        self.assert_unchanged()

    def assert_unchanged(self):
        _remaining(self.deadline)
        latest = self.cfn.describe_stacks(StackName=self.stack["StackId"])["Stacks"][0]
        if any(latest.get(key) != self.stack.get(key) for key in (
                "StackId", "StackStatus", "CreationTime", "LastUpdatedTime")):
            raise runtime_deploy.RuntimeDeploymentError("CDK stack changed during promotion; retry after deployment completes.")

    def get_agent_runtime(self, **kwargs):
        while True:
            _remaining(self.deadline)
            current = self.control.get_agent_runtime(**kwargs)
            if (current.get("agentRuntimeId") != self.entry["runtimeId"]
                    or current.get("agentRuntimeArn") != self.entry["runtimeArn"]):
                raise runtime_deploy.RuntimeDeploymentError("Runtime ID/ARN does not match the selected CDK deployment.")
            if (runtime_deploy._timestamp(current.get("lastUpdatedAt")) >= self.started
                    and _contains(current, self.expected)
                    and current.get("environmentVariables", {}) == self.expected["environmentVariables"]):
                return current
            print("Ignoring Runtime readback that predates or differs from the completed CDK deployment.",
                  file=sys.stderr, flush=True)
            runtime_deploy.time.sleep(min(runtime_deploy.POLL_SECONDS, _remaining(self.deadline)))

    def update_agent_runtime(self, **kwargs):
        self.assert_unchanged()
        return self.control.update_agent_runtime(**kwargs)


def _connection(resources, runtime_name):
    entry = resources["runtimes"][runtime_name]
    return (resources.get("stackName"), resources.get("deployHash"),
            {key: entry[key] for key in ("runtimeId", "runtimeArn", "roleArn", "runtimeVersion")
             if key in entry})


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
        resources = state["targets"][target_name]["resources"]
        entry = resources["runtimes"][runtime_name]
        runtime_id, arn = entry["runtimeId"], entry["runtimeArn"]
        accepted_version = (
            runtime_deploy.runtime_version_number(entry["runtimeVersion"])
            if "runtimeVersion" in entry else None
        )
        connection = _connection(resources, runtime_name)
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
    if accepted_version is None and not resources.get("stackName"):
        raise runtime_deploy.RuntimeDeploymentError(
            "CLI state has no accepted Runtime revision or CDK stack; complete agentcore deploy first.")
    deadline = runtime_deploy.time.monotonic() + runtime_deploy._timeout(timeout_s)
    control = control if control is not None else runtime_deploy.control_client(target["region"])
    if resources.get("stackName"):
        control = _CdkControl(control, target, resources, entry, deadline)
    result = runtime_deploy.promote(
        control, runtime_id, timeout_s=_remaining(deadline), expected_arn=arn,
        minimum_version=accepted_version,
    )
    if isinstance(control, _CdkControl):
        control.assert_unchanged()

    # CLI 0.30.0's CDK path omits optional runtimeVersion. Record only the
    # service-verified revision; it has no platformVersion field.
    # Refresh only the revision; preserve all other targets, sessions and resources.
    latest = runtime_deploy.read_state(state_path)
    latest_resources = latest["targets"][target_name]["resources"]
    current = latest_resources["runtimes"][runtime_name]
    if _connection(latest_resources, runtime_name) != connection:
        raise runtime_deploy.RuntimeDeploymentError(
            "CLI deployment state changed during promotion; refusing to overwrite it."
        )
    verified_version = runtime_deploy.runtime_version_number(result["runtime_version"])
    if accepted_version is not None and verified_version < accepted_version:
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
