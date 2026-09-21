"""Deploy the existing Gateway MCP Runtime through the shared V2 SDK path."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_deploy


def deploy(args, control=None) -> dict:
    state_path = Path(args.state)
    # Refuse damaged state before making an API change.
    state = runtime_deploy.read_state(state_path)
    parameters = {
        "agentRuntimeName": args.name,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": args.image}},
        "roleArn": args.role,
        "networkConfiguration": {"networkMode": args.network},
        "protocolConfiguration": {"serverProtocol": args.protocol},
        "environmentVariables": {"GITHUB_APP_SECRET_ARN": args.secret_arn},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": args.idle_timeout,
            "maxLifetime": args.max_lifetime,
        },
    }
    runtime_deploy.validate_environment(parameters["environmentVariables"])
    control = control if control is not None else runtime_deploy.control_client(args.region)

    def remember(record):
        # Keep unrelated Gateway/credential state. Save the returned ID before
        # waiting so a timeout remains recoverable without creating a new Runtime.
        latest = runtime_deploy.read_state(state_path)
        latest.update(record)
        runtime_deploy.write_state(state_path, latest)

    result = runtime_deploy.deploy(
        control, parameters, runtime_id=state.get("runtime_id"),
        timeout_s=args.timeout, role_retry_s=300, on_submitted=remember,
    )
    remember(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "name", "image", "role", "region", "secret-arn"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--network", default="PUBLIC", choices=("PUBLIC",))
    parser.add_argument("--protocol", default="MCP", choices=("MCP",))
    parser.add_argument("--idle-timeout", type=int, required=True)
    parser.add_argument("--max-lifetime", type=int, required=True)
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args()
    result = deploy(args)
    print(f"Runtime {result['runtime_id']} revision {result['runtime_version']}: "
          f"{result['platform_version']} READY")
    print(f"Runtime ARN: {result['runtime_arn']}")


if __name__ == "__main__":
    try:
        main()
    except runtime_deploy.RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
