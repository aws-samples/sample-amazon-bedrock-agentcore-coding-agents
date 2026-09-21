"""Shared AgentCore deployment. Importing this module never contacts AWS.

Runtime revisions and platform versions are separate. Only a GetAgentRuntime
response for the submitted revision, with READY and the selected platform,
completes a deployment. WORKSHOP_RUNTIME_PLATFORM_VERSION overrides the manifest
default (V1); V2 is an explicit opt-in. A timeout retains the resource.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

import cli_versions


PLATFORM_ENV = "WORKSHOP_RUNTIME_PLATFORM_VERSION"
_SELECTED_PLATFORM = object()
READY_TIMEOUT_SECONDS = 900
POLL_SECONDS = 10
# Count UTF-8 key/value bytes plus '=' and a delimiter. V2's documented 2.5 KB
# container budget uses a conservative decimal-KB limit.
CONTAINER_ENV_BYTES = {"V1": 4096, "V2": 2500}
_UPDATE_FIELDS = (
    "agentRuntimeArtifact", "roleArn", "networkConfiguration", "description",
    "authorizerConfiguration", "requestHeaderConfiguration",
    "protocolConfiguration", "lifecycleConfiguration", "metadataConfiguration",
    "environmentVariables", "filesystemConfigurations",
    "capacityProviderConfiguration",
)


class RuntimeDeploymentError(RuntimeError):
    pass


def selected_platform(value: str | None = None) -> str:
    """Resolve once per operation; reject invalid explicit choices, including empty."""
    if value is None:
        value = os.environ.get(PLATFORM_ENV)
        if value is None:
            value = cli_versions.runtime_platform_version()
    if not isinstance(value, str) or value not in CONTAINER_ENV_BYTES:
        raise RuntimeDeploymentError(
            f"{PLATFORM_ENV} must be exactly V1 or V2 (unset uses cli-versions.json)."
        )
    return value


def require_runtime_sdk() -> None:
    """Check the installed service model without resolving credentials."""
    selected_platform()
    try:
        cli_versions.require_deploy_sdk()
    except RuntimeError as exc:
        raise RuntimeDeploymentError(str(exc)) from exc
    import botocore.session

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    for operation, direction in (
        ("CreateAgentRuntime", "input_shape"),
        ("UpdateAgentRuntime", "input_shape"),
        ("GetAgentRuntime", "output_shape"),
    ):
        shape = getattr(model.operation_model(operation), direction)
        if shape is None or "platformVersion" not in shape.members:
            raise RuntimeDeploymentError(
                "Explicit AgentCore platform selection requires boto3/botocore 1.43.95 or newer. Install "
                "the workshop's pinned deployment SDK in this Python interpreter."
            )


def control_client(region: str, session=None):
    require_runtime_sdk()
    import boto3
    from botocore.config import Config

    # SDK retries cannot turn a bounded readiness poll into an indefinite wait.
    # Retryable GET failures are retried by the readiness loop under its deadline.
    return (session or boto3.Session()).client(
        "bedrock-agentcore-control", region_name=region,
        config=Config(connect_timeout=5, read_timeout=10,
                      retries={"total_max_attempts": 1}),
    )


def validate_environment(environment: dict[str, str], *, platform: str | None = None) -> int:
    """Validate the final container environment; never include values in errors."""
    platform = selected_platform(platform)
    limit = CONTAINER_ENV_BYTES[platform]
    if not isinstance(environment, dict) or len(environment) > 50:
        raise RuntimeDeploymentError("Runtime environment must be a map of at most 50 variables.")
    sizes = {}
    for key, value in environment.items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 100 or "\0" in key or "=" in key
                or not isinstance(value, str)):
            raise RuntimeDeploymentError("Runtime environment names or value types are invalid.")
        if "\0" in value:
            raise RuntimeDeploymentError(f"Runtime environment variable {key} contains a NUL byte.")
        sizes[key] = len(key.encode("utf-8")) + len(value.encode("utf-8")) + 2
    total = sum(sizes.values())
    if total > limit:
        largest = ", ".join(f"{key}={size} bytes" for key, size in
                            sorted(sizes.items(), key=lambda item: item[1], reverse=True)[:5])
        raise RuntimeDeploymentError(
            f"{platform} container environment is {total} bytes; the local limit is "
            f"{limit} bytes. Largest variables: {largest}. "
            "Move large configuration out of environment variables."
        )
    return total


def _timeout(value: float | None) -> float:
    if value is None:
        value = os.environ.get("WORKSHOP_RUNTIME_READY_TIMEOUT_SECONDS", READY_TIMEOUT_SECONDS)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise RuntimeDeploymentError("Runtime READY timeout must be between 0 and 3600 seconds.")
    return seconds


def _code(error: Exception) -> str:
    return (getattr(error, "response", None) or {}).get("Error", {}).get("Code", "")


def _safe_reason(reason: object, environment=None) -> str:
    text = str(reason)
    for value in (environment or {}).values():
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "[environment value]")
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bksk_[A-Za-z0-9_-]+",
                  "[redacted]", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    return text[:1000]


def _timestamp(value) -> float:
    """Compare AWS timestamps, never the deployment host's wall clock."""
    try:
        if isinstance(value, datetime):
            result = value.timestamp()
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        else:
            result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    raise RuntimeDeploymentError("AgentCore returned a missing or invalid control-plane timestamp.")


def wait_ready(control, runtime_id: str, *, expected_version: str,
               timeout_s: float | None = None, platform=_SELECTED_PLATFORM,
               deadline: float | None = None, submitted_at=None) -> dict:
    """Wait for the submitted revision, including multi-minute V2 preparation."""
    # Only callers waiting out an earlier operation pass None. Final admission
    # always verifies the selected platform, captured before submitting changes.
    if platform is _SELECTED_PLATFORM:
        platform = selected_platform()
    elif platform is not None:
        platform = selected_platform(platform)
    deadline = deadline if deadline is not None else time.monotonic() + _timeout(timeout_s)
    not_before = _timestamp(submitted_at) if submitted_at is not None else None
    last = "not observed"
    while time.monotonic() < deadline:
        try:
            current = control.get_agent_runtime(agentRuntimeId=runtime_id)
        except Exception as exc:
            from botocore.exceptions import ConnectionError, HTTPClientError

            if (_code(exc) not in {"ResourceNotFoundException", "ThrottlingException",
                                  "TooManyRequestsException", "ServiceUnavailableException",
                                  "InternalServerException"}
                    and not isinstance(exc, (ConnectionError, HTTPClientError))):
                raise
            last = _code(exc) or type(exc).__name__
        else:
            status = current.get("status", "UNKNOWN")
            revision = str(current.get("agentRuntimeVersion", ""))
            actual_platform = current.get("platformVersion", "unknown")
            last = f"{status}, revision {revision or 'unknown'}, platform {actual_platform}"
            print(f"Runtime {runtime_id}: {last}", file=sys.stderr, flush=True)
            # Get may briefly return an older FAILED as well as an older READY.
            # Update/Get both require lastUpdatedAt in the API contract. A failed
            # update may retain the old artifact revision, so revision equality
            # alone cannot attribute failure. The accepted response's AWS time
            # distinguishes that fresh failure from a stale previous operation.
            fresh = not_before is None or _timestamp(current.get("lastUpdatedAt")) >= not_before
            if fresh and (status.endswith("FAILED") or status == "DELETING"):
                reason = _safe_reason(current.get("failureReason", ""),
                                      current.get("environmentVariables"))
                raise RuntimeDeploymentError(f"Runtime {runtime_id} {status}: {reason}")
            if fresh and revision == str(expected_version):
                if status == "READY" and time.monotonic() < deadline:
                    if platform is not None and actual_platform != platform:
                        raise RuntimeDeploymentError(
                            f"Runtime {runtime_id} is READY on {actual_platform}, expected {platform}."
                        )
                    return current
                if status not in {"CREATING", "UPDATING", "READY"}:
                    raise RuntimeDeploymentError(f"Runtime {runtime_id} has unexpected status {status}.")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_SECONDS, remaining))
    raise RuntimeDeploymentError(
        f"Runtime {runtime_id} did not become READY before the deadline ({last}). "
        "The Runtime was retained; inspect it before retrying deployment."
    )


def create_runtime_with_role_retry(control, parameters: dict, budget_s: float = 240,
                                   *, platform: str | None = None) -> dict:
    """Retry only IAM role propagation, using the same idempotency token."""
    platform = selected_platform(platform)
    request = copy.deepcopy(parameters)
    request["platformVersion"] = platform
    request.setdefault("clientToken", str(uuid.uuid4()))
    validate_environment(request.get("environmentVariables", {}), platform=platform)
    deadline = time.monotonic() + _timeout(budget_s)
    propagation_error = None
    while True:
        # Sleeping may consume the last of the budget. Do not start another
        # mutating request at the deadline; retain the actual IAM failure.
        if time.monotonic() >= deadline:
            if propagation_error is not None:
                raise propagation_error
            raise RuntimeDeploymentError("Runtime creation deadline expired before submission.")
        try:
            return control.create_agent_runtime(**request)
        except Exception as exc:
            if (_code(exc) != "ValidationException" or "Role validation failed" not in str(exc)
                    or time.monotonic() >= deadline):
                raise
            propagation_error = exc
            print("Runtime execution role is propagating; retrying in 20 seconds.",
                  file=sys.stderr, flush=True)
            time.sleep(min(20, max(0, deadline - time.monotonic())))


def update_request(current: dict, changes: dict | None = None,
                   *, platform: str | None = None) -> dict:
    """Preserve settings without replaying Get-only or immutable fields."""
    platform = selected_platform(platform)
    request = {key: copy.deepcopy(current[key]) for key in _UPDATE_FIELDS
               if key in current and current[key] is not None}
    changes = copy.deepcopy(changes or {})
    if "environmentVariables" in changes:
        request["environmentVariables"] = {
            **request.get("environmentVariables", {}), **changes.pop("environmentVariables"),
        }
    request.update({key: value for key, value in changes.items() if key in _UPDATE_FIELDS})
    network = request.get("networkConfiguration", {})
    mode_config = network.get("networkModeConfig")
    if isinstance(mode_config, dict):
        mode_config.pop("requireServiceS3Endpoint", None)
    elif mode_config is None:
        network.pop("networkModeConfig", None)
    request["agentRuntimeId"] = current["agentRuntimeId"]
    request["platformVersion"] = platform
    request["clientToken"] = str(uuid.uuid4())
    validate_environment(request.get("environmentVariables", {}), platform=platform)
    return request


def runtime_record(response: dict) -> dict:
    record = {"runtime_id": response["agentRuntimeId"],
              "runtime_arn": response["agentRuntimeArn"],
              "runtime_version": response["agentRuntimeVersion"],
              "runtime_status": response["status"]}
    if "platformVersion" in response:
        record["platform_version"] = response["platformVersion"]
    return record


def _existing(control, runtime_id: str | None) -> dict | None:
    if not runtime_id:
        return None
    try:
        return control.get_agent_runtime(agentRuntimeId=runtime_id)
    except Exception as exc:
        if _code(exc) != "ResourceNotFoundException":
            raise
        return None


def _by_name(control, name: str) -> dict | None:
    for page in control.get_paginator("list_agent_runtimes").paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return control.get_agent_runtime(agentRuntimeId=runtime["agentRuntimeId"])
    return None


def deploy(control, parameters: dict, *, runtime_id: str | None = None,
           timeout_s: float | None = None, role_retry_s: float = 240,
           on_submitted=None) -> dict:
    """Create, update or recover by exact name, then verify the selected platform."""
    platform = selected_platform()
    timeout_s = _timeout(timeout_s)
    validate_environment(parameters.get("environmentVariables", {}), platform=platform)
    current = _existing(control, runtime_id)
    if current and current.get("agentRuntimeName") != parameters["agentRuntimeName"]:
        raise RuntimeDeploymentError("Saved Runtime ID belongs to a different Runtime name.")
    if current is None:
        try:
            response = create_runtime_with_role_retry(
                control, parameters, budget_s=role_retry_s, platform=platform)
            submitted_at = response["createdAt"]
        except Exception as exc:
            if _code(exc) != "ConflictException":
                raise
            current = _by_name(control, parameters["agentRuntimeName"])
            if current is None:
                raise
    deadline = time.monotonic() + timeout_s
    if current is not None:
        if current["status"] in {"CREATING", "UPDATING"}:
            current = wait_ready(control, current["agentRuntimeId"],
                                 expected_version=current["agentRuntimeVersion"],
                                 platform=None, deadline=deadline,
                                 submitted_at=current["lastUpdatedAt"])
        response = control.update_agent_runtime(**update_request(
            current, parameters, platform=platform))
        submitted_at = response["lastUpdatedAt"]
    print(f"Submitted Runtime {response['agentRuntimeId']} revision "
          f"{response['agentRuntimeVersion']} for platform {platform}.", file=sys.stderr, flush=True)
    if on_submitted is not None:
        on_submitted(runtime_record(response))
    ready = wait_ready(control, response["agentRuntimeId"],
                       expected_version=response["agentRuntimeVersion"], deadline=deadline,
                       submitted_at=submitted_at, platform=platform)
    return runtime_record(ready)


def runtime_version_number(value) -> int:
    """Service revisions are positive integers, independently of platform version."""
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or not re.fullmatch(r"[0-9]+", str(value)) or int(value) < 1):
        raise RuntimeDeploymentError("Missing or invalid accepted Runtime revision.")
    return int(value)


def promote(control, runtime_id: str, *, timeout_s: float | None = None,
            expected_arn: str | None = None, minimum_version=None,
            platform: str | None = None) -> dict:
    """Promote an existing resource without provisioning roles or other resources."""
    platform = selected_platform(platform)
    deadline = time.monotonic() + _timeout(timeout_s)
    minimum = runtime_version_number(minimum_version) if minimum_version is not None else None

    def require_time():
        if time.monotonic() >= deadline:
            raise RuntimeDeploymentError(
                f"Runtime {runtime_id} promotion deadline expired; the Runtime was retained."
            )

    # The CLI already knows the revision accepted by its deployment. Get can
    # briefly return an older READY (or FAILED), so it cannot lower that bound.
    # A later revision from a previous failed promotion remains an explicit
    # retry candidate rather than waiting forever for the older CLI revision.
    while True:
        require_time()
        current = control.get_agent_runtime(agentRuntimeId=runtime_id)
        require_time()
        if expected_arn is not None and current.get("agentRuntimeArn") != expected_arn:
            raise RuntimeDeploymentError("Deployed Runtime ARN does not match the selected project target.")
        observed = runtime_version_number(current.get("agentRuntimeVersion"))
        if minimum is None or observed >= minimum:
            break
        print(f"Runtime {runtime_id}: ignoring revision {observed}; "
              f"CLI accepted revision {minimum}.", file=sys.stderr, flush=True)
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    if current["status"] in {"CREATING", "UPDATING"}:
        current = wait_ready(control, runtime_id, expected_version=current["agentRuntimeVersion"],
                             platform=None, deadline=deadline,
                             submitted_at=current["lastUpdatedAt"])
    elif current["status"] not in {"READY", "CREATE_FAILED", "UPDATE_FAILED"}:
        raise RuntimeDeploymentError(
            f"Runtime {runtime_id} cannot be promoted from {current['status']}."
        )
    if current.get("platformVersion") not in CONTAINER_ENV_BYTES:
        raise RuntimeDeploymentError("GetAgentRuntime did not return a supported platform version.")
    validate_environment(current.get("environmentVariables", {}), platform=platform)
    require_time()
    if current["status"] == "READY" and current["platformVersion"] == platform:
        return runtime_record(current)
    # A new explicit invocation may retry a previous failed preparation once.
    # Failures while waiting for an operation above, or for this update below,
    # still terminate this invocation. Never automatically repeat a failed update.
    request = update_request(current, platform=platform)
    require_time()
    response = control.update_agent_runtime(**request)
    ready = wait_ready(control, runtime_id, expected_version=response["agentRuntimeVersion"],
                       deadline=deadline, submitted_at=response["lastUpdatedAt"], platform=platform)
    return runtime_record(ready)


def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeDeploymentError(f"Expected a JSON object in {path}.")
    return data


def write_state(path: Path, data: dict) -> None:
    """Atomic private state write; callers merge only the fields they own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    # Used by shell entry points before their slow build/provisioning steps.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-platform", action="store_true",
                        help="Print only the selected platform after SDK preflight.")
    args = parser.parse_args()
    try:
        require_runtime_sdk()
        platform = selected_platform()
    except RuntimeDeploymentError as error:
        raise SystemExit(str(error)) from None
    print(platform if args.print_platform else
          f"Deployment SDK supports the selected AgentCore platform {platform}.")
