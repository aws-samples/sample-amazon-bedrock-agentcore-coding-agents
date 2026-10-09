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
# Set by a deployer's __main__: "", "--prepare" or "--adopt" (the console path).
CONSOLE_MODE = ""
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


class RuntimeRecord(dict):
    """The saved connection fields, plus the service response they came from.

    Deployers write the dict items to ``runtime_config.json`` exactly as before.
    ``accepted`` is the GetAgentRuntime (or Create/Update) response itself, kept
    only so a receipt can report what AWS accepted rather than what was asked for.
    """

    accepted: dict

    def __init__(self, *args, accepted: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.accepted = accepted or {}


def runtime_record(response: dict) -> dict:
    record = {"runtime_id": response["agentRuntimeId"],
              "runtime_arn": response["agentRuntimeArn"],
              "runtime_version": response["agentRuntimeVersion"],
              "runtime_status": response["status"]}
    if "platformVersion" in response:
        record["platform_version"] = response["platformVersion"]
    return RuntimeRecord(record, accepted=response)


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
    if EXPLAIN:
        # Same parameters, same platform and environment checks, no write.
        explain_request(control, parameters, runtime_id=runtime_id, platform=platform)
        return None
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


# ---------------------------------------------------------------------------
# Receipts and previews. September 25 attendees said the setup gave "no real
# connection to how we would set up or do a similar process elsewhere": the
# scripts printed an ARN and nothing about what that ARN is. A receipt names the
# resource AWS accepted and the read-only command that shows it; --explain prints
# the exact request a deploy would send, and the AWS CLI line that sends it, before
# anything is created. Both are the reporting path: they never change a request.

CLI_SERVICE = "bedrock-agentcore-control"   # botocore service name == AWS CLI command
EXPLAIN = False   # set by a deployer's --explain; deploy() then previews instead of writing

_WHY = {
    "agentRuntimeId": "which existing Runtime changes (same ID, new revision)",
    "agentRuntimeName": "the Runtime's name in your account",
    "agentRuntimeArtifact": "the container image every session runs",
    "roleArn": "what the agent may call in AWS (the role above)",
    "networkConfiguration": "where sessions run on the network",
    "protocolConfiguration": "how clients talk to a session",
    "filesystemConfigurations": "the shared workspace mounted into every session",
    "environmentVariables": "settings the launcher reads at start (names only here)",
    "lifecycleConfiguration": "how long idle and total sessions may live",
    "platformVersion": "the Runtime platform this workshop selects",
    "description": "a note shown in the AgentCore console",
}

_POLICY_WHY = {
    "Logs": "write its own CloudWatch logs",
    "Telemetry": "export usage telemetry (Lab 3) to CloudWatch and X-Ray",
    "UsageLogs": "write usage records to CloudWatch Logs (Lab 3)",
    "InsightsMetrics": "send usage metrics to CloudWatch Coding Agent Insights (Lab 3)",
    "BedrockInvoke": "call Amazon Bedrock models",
    "BedrockOpenAIInference": "call the OpenAI models on Amazon Bedrock",
    "BedrockRuntimeProjectInvoke": "use the default Bedrock project the OpenAI-compatible API needs",
    "BedrockApiKey": "call Amazon Bedrock with a bearer token",
    "AssumePerUser": "assume the per-user audit role (CloudTrail attribution)",
    "ECRAuth": "log in to Amazon ECR",
    "ECRPull": "pull its own container image",
    "S3Files": "mount and write the shared S3 Files workspace",
    "EFS": "use the NFS client the mount is built on",
    "S3Bucket": "exchange source archives with the coordinator",
    "AgentCoreGateway": "call the GitHub tools behind the Gateway",
    "AgentCoreIdentity": "read its API key from the AgentCore Token Vault",
    "SecretsManagerForTokenVault": "read only the Token Vault's own secrets",
    "EventBridge": "manage EventBridge rules",
    "SecretsManagerAccess": "read the GitHub App key from Secrets Manager",
}


def _field_summary(name: str, value) -> str:
    """One readable line per request field; environment VALUES are never shown."""
    if name == "agentRuntimeArtifact":
        return ((value or {}).get("containerConfiguration") or {}).get("containerUri", "?")
    if name == "networkConfiguration":
        mode = (value or {}).get("networkMode", "?")
        config = (value or {}).get("networkModeConfig") or {}
        if mode == "VPC":
            subnets, groups = config.get("subnets") or [], config.get("securityGroups") or []
            return (f"VPC, {len(subnets)} subnet{'s' * (len(subnets) != 1)}, "
                    f"{len(groups)} security group{'s' * (len(groups) != 1)}"
                    " (private subnets, so a session can reach the mount)")
        return f"{mode} (no VPC)" if mode == "PUBLIC" else mode
    if name == "protocolConfiguration":
        protocol = (value or {}).get("serverProtocol", "?")
        return {"HTTP": "HTTP (the session server agentcore exec connects to)",
                "MCP": "MCP (a tool server, called through the Gateway)"}.get(protocol, protocol)
    if name == "filesystemConfigurations":
        mounts = []
        for item in value or []:
            point = item.get("s3FilesAccessPoint") or {}
            mounts.append(f"{point.get('mountPath', '?')} from {point.get('accessPointArn', '?')}")
        return "; ".join(mounts) or "no mount"
    if name == "environmentVariables":
        return ", ".join(sorted(value or {})) or "(none)"
    if name == "lifecycleConfiguration":
        idle, most = (value or {}).get("idleRuntimeSessionTimeout"), (value or {}).get("maxLifetime")
        return f"idle {idle}s, at most {most}s per session"
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _mount_summary(response: dict) -> str:
    return _field_summary("filesystemConfigurations",
                          response.get("filesystemConfigurations")) if response.get(
        "filesystemConfigurations") else "no mount"


def receipt_lines(record: dict, *, region: str, saved_to: str | None = None,
                  parameters: dict | None = None) -> list[str]:
    """What a finished deployment created, from the response AWS accepted."""
    accepted = {**(parameters or {}), **getattr(record, "accepted", {})}
    runtime_id = record.get("runtime_id") or accepted.get("agentRuntimeId", "?")
    platform = record.get("platform_version") or accepted.get("platformVersion", "?")
    status = record.get("runtime_status") or accepted.get("status", "?")

    def row(label: str, value) -> str:
        return f"  {label + ':':<16} {value}"

    lines = ["What this created: an Amazon Bedrock AgentCore Runtime",
             row("Name", accepted.get("agentRuntimeName", "?")),
             row("Runtime ID", runtime_id),
             row("Runtime ARN", record.get("runtime_arn") or accepted.get("agentRuntimeArn", "?")),
             row("Revision", f"{record.get('runtime_version', '?')}, platform {platform}, {status}")]
    if accepted.get("agentRuntimeArtifact"):
        lines.append(row("Image", _field_summary("agentRuntimeArtifact",
                                                 accepted["agentRuntimeArtifact"])
                         + "  (what every session runs)"))
    if accepted.get("roleArn"):
        lines.append(row("Execution role", accepted["roleArn"] + "  (what the agent may call)"))
    if accepted.get("networkConfiguration"):
        lines.append(row("Network", _field_summary("networkConfiguration",
                                                   accepted["networkConfiguration"])))
    if accepted.get("protocolConfiguration"):
        lines.append(row("Protocol", _field_summary("protocolConfiguration",
                                                    accepted["protocolConfiguration"])))
    lines.append(row("Mount", _mount_summary(accepted)))
    if "environmentVariables" in accepted:
        lines.append(row("Environment", _field_summary("environmentVariables",
                                                       accepted["environmentVariables"])
                         + "  (names only)"))
    if saved_to:
        lines.append(row("Saved on host", saved_to))
    lines += ["  See it yourself (read-only):",
              f"    aws {CLI_SERVICE} get-agent-runtime --agent-runtime-id {runtime_id} "
              f"--region {region}"]
    return lines


def print_receipt(record: dict, *, region: str, saved_to: str | None = None,
                  parameters: dict | None = None, file=None) -> None:
    for line in receipt_lines(record, region=region, saved_to=saved_to, parameters=parameters):
        print(line, file=file or sys.stdout)


def explain_role(iam, role_name: str, trust_policy: dict, inline_policy: dict, *,
                 account_id: str, file=None) -> str:
    """Describe the execution role a deploy would create or reuse; no IAM write."""
    out = file or sys.stdout
    try:
        iam.get_role(RoleName=role_name)
        state = ("exists; a real run keeps its trust policy and rewrites this deploy's "
                 "inline policy with the one below")
    except Exception as exc:  # noqa: BLE001 - a preview never fails on a read
        state = ("does not exist yet; a real run creates it"
                 if _code(exc) == "NoSuchEntity" else f"could not be read ({_code(exc) or type(exc).__name__})")
    principals = sorted({principal for statement in trust_policy.get("Statement", [])
                         for principal in [(statement.get("Principal") or {}).get("Service")]
                         if isinstance(principal, str)})
    print("PREVIEW ONLY: nothing is created or changed.", file=out)
    print("", file=out)
    print(f"1. IAM execution role {role_name}: {state}.", file=out)
    trusted = "Trusted by" if state.startswith("does not exist") else "Created trusting"
    print(f"   {trusted}: {', '.join(principals) or '?'}", file=out)
    print("   Its inline policy, one line per statement:", file=out)
    for statement in inline_policy.get("Statement", []):
        actions = statement.get("Action") or []
        actions = [actions] if isinstance(actions, str) else actions
        sid = statement.get("Sid", "?")
        why = _POLICY_WHY.get(sid, "")
        print(f"     {sid:<28} {why + ': ' if why else ''}{len(actions)} "
              f"action{'s' * (len(actions) != 1)} ({', '.join(actions[:2])}"
              f"{', ...' if len(actions) > 2 else ''})", file=out)
    return f"arn:aws:iam::{account_id}:role/{role_name}"


def explain_request(control, parameters: dict, *, runtime_id: str | None = None,
                    platform: str | None = None, file=None, directory: str | None = None) -> dict:
    """Print the exact Create/UpdateAgentRuntime request a deploy would send.

    Reads only (Get/List). Returns ``{"operation", "request", "path"}``.
    """
    out = file or sys.stdout
    platform = selected_platform(platform)
    current, note = None, ""
    try:
        current = _existing(control, runtime_id) or _by_name(control, parameters["agentRuntimeName"])
    except Exception as exc:  # noqa: BLE001 - a preview never fails on a read
        note = (f"   (Could not read existing Runtimes: {_code(exc) or type(exc).__name__}. "
                "Showing the create request; a real run updates a Runtime that already exists.)")
    if current is not None and current.get("agentRuntimeName") != parameters["agentRuntimeName"]:
        # The real deploy refuses this (see deploy()); a preview must not offer a
        # pasteable update that would overwrite a different Runtime.
        raise RuntimeDeploymentError("Saved Runtime ID belongs to a different Runtime name. "
                                     "A real run stops here too, so nothing is previewed.")
    if current is not None:
        operation = "UpdateAgentRuntime"
        request = update_request(current, parameters, platform=platform)
    else:
        operation = "CreateAgentRuntime"
        request = copy.deepcopy(parameters)
        request["platformVersion"] = platform
    # The CLI fills an idempotency token itself; a fixed one here would be reused.
    request.pop("clientToken", None)
    cli = re.sub(r"(?<!^)(?=[A-Z])", "-", operation).lower()
    print("", file=out)
    print(f"2. The {operation} request, field by field:", file=out)
    for name in sorted(request, key=lambda key: list(_WHY).index(key) if key in _WHY else len(_WHY)):
        summary = str(_field_summary(name, request[name]))
        why = _WHY.get(name, "")
        print(f"   {name:<26} {summary}", file=out)
        if why:
            print(f"   {'':<26} ^ {why}", file=out)
    if note:
        print(note, file=out)
    directory = directory or tempfile.mkdtemp(prefix="agentcore-explain-")
    path = Path(directory) / f"{parameters['agentRuntimeName']}-{cli}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(request, handle, indent=2, sort_keys=True)
        handle.write("\n")
    region = getattr(getattr(control, "meta", None), "region_name", None) or os.environ.get(
        "AWS_REGION", "<region>")
    print("", file=out)
    print("3. The same request by hand, with the AWS CLI:", file=out)
    print(f"   Request JSON (values included): {path}", file=out)
    print(f"   aws {CLI_SERVICE} {cli} --region {region} --cli-input-json file://{path}", file=out)
    print("   (Needs a recent AWS CLI v2: an older one rejects platformVersion as an unknown "
          "parameter before sending anything.)", file=out)
    print("   Then wait for READY (read-only):", file=out)
    print(f"   aws {CLI_SERVICE} get-agent-runtime --region {region} --agent-runtime-id "
          f"{request.get('agentRuntimeId', '<agentRuntimeId from the response>')} --query status",
          file=out)
    print("", file=out)
    print("Without --explain, the same command sends this request and waits for READY.", file=out)
    return {"operation": operation, "request": request, "path": str(path)}


# ------------------------------------------------------------------ role deployers
# The five coding-agents/<role>/deploy.py files share these pieces. Each deploy.py
# keeps its own constants, its own least-privilege statement LIST (codex scopes its
# logs, the Claude roles may assume the per-user role, Kiro and opencode read the
# Token Vault), and its own environment; only identical statements live here.

def load_dotconfig(path) -> dict:
    """KEY=VALUE lines of a .config file (infra.config, agent.config)."""
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


def load_runtime_id(config_path: str):
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


def assert_same_region(ap_arn: str, region: str) -> None:
    """ONE region per workshop, enforced rather than documented.

    The access point ARN carries the region it was created in, so if the mount and
    this Runtime disagree the Runtime comes up unable to reach /mnt/s3files, and the
    failure surfaces much later as an agent that "wrote nothing". With two
    accessible regions an attendee can genuinely end up here (create the file
    system in one terminal's region, deploy from another), so refuse the deploy
    while the fix is still one line.
    """
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


def s3files_policy_resources(ap_arn: str, region: str, account_id: str) -> list:
    """Resource ARNs for the S3Files IAM statement.

    When the access point is known, scope to that AP + its file system. When it is
    NOT known yet (the predeploy-mountless boot path: a later re-run attaches it),
    scope to this account's S3Files file systems / access points in-region. Never
    emit empty-string ARNs, which would make put_role_policy reject the whole policy
    as malformed."""
    if ap_arn:
        return [ap_arn, ap_arn.rsplit("/access-point/", 1)[0]]
    return [
        f"arn:aws:s3files:{region}:{account_id}:file-system/*",
        f"arn:aws:s3files:{region}:{account_id}:access-point/*",
    ]


def trust_policy(region: str, account_id: str) -> dict:
    """AgentCore assumes the role; S3 Files assumes it for this account's file systems."""
    return {
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
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:s3files:{region}:{account_id}:file-system/*"
                    },
                },
            },
        ],
    }


def policy(statements: list) -> dict:
    return {"Version": "2012-10-17", "Statement": statements}


def logs_statement(region: str, account_id: str) -> dict:
    return {
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
            f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*"
        ],
    }


def telemetry_statement() -> dict:
    """Lab 3 telemetry: the baked-in OpenTelemetry collector (started at container
    boot by entrypoint.sh) ships this runtime's signals to CloudWatch Logs
    (/workshop/coding-agents/telemetry + /metrics), X-Ray Transaction Search
    (aws/spans), and CloudWatch metrics (Workshop/CodingAgents, plus the OTLP metrics
    endpoint behind Coding Agent Insights, which also signs for PutMetricData).
    Without these the collector's exporters get AccessDenied and telemetry never lands."""
    return {
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
    }


def bedrock_invoke_statement(region: str, account_id: str) -> dict:
    return {
        "Sid": "BedrockInvoke",
        "Effect": "Allow",
        "Action": [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:ListInferenceProfiles",
        ],
        "Resource": [
            "arn:aws:bedrock:*::foundation-model/*",
            f"arn:aws:bedrock:{region}:{account_id}:*",
        ],
    }


def assume_peruser_statement(region: str, account_id: str) -> dict:
    """Stage 3 per-user cost: let this runtime assume the shared per-user role
    (pre-baked by the workshop CFN) with the user as the role-session-name, so its
    Bedrock calls are logged per user."""
    return {
        "Sid": "AssumePerUser",
        "Effect": "Allow",
        "Action": ["sts:AssumeRole"],
        "Resource": [f"arn:aws:iam::{account_id}:role/cca-peruser-{region}"],
    }


def image_repository(ecr_uri: str, *, default_repo: str, account_id: str,
                     region: str) -> tuple[str, str, str]:
    """(repository, registry account, registry region) parsed FROM the image URI.

    With a per-account image these equal the deploy's own account/region; with a
    PREBUILT image pulled from a central workshop ECR they are the central
    account/region, so the ECR-pull grant lands on the repo that actually holds the
    image (cross-account pull). URI shape: <acct>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>
    """
    repo = (
        ecr_uri.split("/", 1)[1].split("@", 1)[0].split(":", 1)[0]
        if "/" in ecr_uri else default_repo
    )
    registry = ecr_uri.split(".dkr.ecr.")[0] if ".dkr.ecr." in ecr_uri else account_id
    ecr_account = registry.split("/")[-1] if registry else account_id
    ecr_region = ecr_uri.split(".dkr.ecr.")[1].split(".")[0] if ".dkr.ecr." in ecr_uri else region
    return repo, ecr_account, ecr_region


def ecr_statements(ecr_uri: str, *, default_repo: str, account_id: str, region: str) -> list:
    """Pull the role's image, scoped to the registry that actually holds it (the
    central workshop account for a prebuilt pull, else this account)."""
    repo, ecr_account, ecr_region = image_repository(
        ecr_uri, default_repo=default_repo, account_id=account_id, region=region)
    return [
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
            "Resource": [f"arn:aws:ecr:{ecr_region}:{ecr_account}:repository/{repo}"],
        },
    ]


def storage_statements(s3files_resources: list, *, region: str, account_id: str,
                       bucket: str) -> list:
    """The shared /mnt/s3files workspace: S3 Files client access, the EFS-compatible
    mount calls, and the backing bucket."""
    return [
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
            "Resource": s3files_resources,
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
                f"arn:aws:elasticfilesystem:{region}:{account_id}:file-system/*",
                f"arn:aws:elasticfilesystem:{region}:{account_id}:access-point/*",
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
                f"arn:aws:s3:::{bucket}",
                f"arn:aws:s3:::{bucket}/*",
            ],
        },
    ]


def gateway_statement(region: str, account_id: str) -> dict:
    return {
        "Sid": "AgentCoreGateway",
        "Effect": "Allow",
        "Action": ["bedrock-agentcore:InvokeGateway"],
        "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/*"],
    }


def eventbridge_statement() -> dict:
    return {
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
    }


def identity_statement() -> dict:
    """Read the role's key through the AgentCore Identity Token Vault at session start."""
    return {
        "Sid": "AgentCoreIdentity",
        "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetResourceApiKey",
        ],
        "Resource": ["*"],
    }


def create_execution_role(iam, role_name: str, trust: dict, inline: dict, *,
                          agent_name: str, account_id: str, sleep=time.sleep) -> str:
    """Create (or reuse) the role and write its one inline policy; returns its ARN."""
    created_now = True
    try:
        resp = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"Execution role for {agent_name} on AgentCore",
        )
        role_arn = resp["Role"]["Arn"]
        print(f"\nCreated IAM role: {role_arn}")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        created_now = False
        print(f"\nIAM role exists: {role_arn}")

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{agent_name}-policy",
        PolicyDocument=json.dumps(inline),
    )

    if created_now:
        # A role the service can see is not yet a role the service can ASSUME: the trust
        # policy replicates to STS on its own clock. Ten seconds was enough on every
        # earlier event box; on 2026-09-03 a fresh account rejected a 10s-old role and
        # a 30s-old one alike, so the wait is a floor and deploy() also retries the
        # validation failure itself instead of dying on the first answer.
        print("Waiting 20s for IAM propagation (new role)...")
        sleep(20)
    return role_arn


def deploy_role_runtime(control, *, name: str, image: str, role_arn: str,
                        subnets: list, security_groups: list, mount_ap_arn: str,
                        mount_path: str, environment: dict, description: str,
                        runtime_id: str | None) -> dict:
    """The one Runtime request every role sends: its image, role, VPC, and mount.

    The S3 Files mount is attached only when the access point is known; a mountless
    deploy omits ``filesystemConfigurations`` entirely rather than sending an empty
    list, and a later re-run attaches it to the same Runtime.
    """
    artifact = {"containerConfiguration": {"containerUri": image}}
    network = {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": list(subnets),
            "securityGroups": list(security_groups),
        },
    }
    fs_kwargs = {}
    if mount_ap_arn:
        fs_kwargs["filesystemConfigurations"] = [
            {
                "s3FilesAccessPoint": {
                    "accessPointArn": mount_ap_arn,
                    "mountPath": mount_path,
                }
            }
        ]
    return deploy(control, dict(
        agentRuntimeName=name,
        agentRuntimeArtifact=artifact,
        roleArn=role_arn,
        networkConfiguration=network,
        protocolConfiguration={"serverProtocol": "HTTP"},
        environmentVariables=environment,
        description=description,
        **fs_kwargs,
    ), runtime_id=runtime_id)


# --- Building the Runtime in the AWS Management Console -------------------------
# The console path teaches the same request by hand: --prepare makes the execution
# role and prints each form field to choose, the person creates the Runtime in the
# AgentCore console, and --adopt checks what they built before recording it. The
# check is the point: the person makes it, a script they did not write verifies it.

def console_names(session, *, subnets: list, security_groups: list,
                  mount_ap_arn: str) -> dict:
    """The labels the console shows for the workshop's network and storage.

    The console lists these resources by name as well as ID, so the sheet names
    what to pick. Any lookup that fails falls back to the plain ID."""
    names = {"vpc": "", "subnets": {s: s for s in subnets},
             "security_groups": {g: g for g in security_groups},
             "file_system": "", "access_point": mount_ap_arn.rsplit("/", 1)[-1] if mount_ap_arn else ""}
    try:
        ec2 = session.client("ec2")
        for subnet in ec2.describe_subnets(SubnetIds=list(subnets)).get("Subnets", []):
            tag = next((t["Value"] for t in subnet.get("Tags", []) if t["Key"] == "Name"), "")
            names["subnets"][subnet["SubnetId"]] = (f"{subnet['SubnetId']} ({tag})" if tag
                                                    else subnet["SubnetId"])
            vpc_id = subnet.get("VpcId", "")
            if vpc_id and not names["vpc"]:
                vpcs = ec2.describe_vpcs(VpcIds=[vpc_id]).get("Vpcs", [])
                tag = next((t["Value"] for t in (vpcs[0].get("Tags", []) if vpcs else [])
                            if t["Key"] == "Name"), "")
                names["vpc"] = f"{vpc_id} ({tag})" if tag else vpc_id
        for group in ec2.describe_security_groups(
                GroupIds=list(security_groups)).get("SecurityGroups", []):
            names["security_groups"][group["GroupId"]] = (
                f"{group['GroupId']} ({group.get('GroupName', '')})")
    except Exception:  # noqa: BLE001 - names are a convenience; IDs still work
        pass
    if mount_ap_arn and "file-system/" in mount_ap_arn:
        fs_id = mount_ap_arn.split("file-system/", 1)[1].split("/", 1)[0]
        names["file_system"] = fs_id
        try:
            fs = session.client("s3files").get_file_system(fileSystemId=fs_id)
            label = fs.get("name") or next((t.get("value") or t.get("Value") for t in fs.get("tags", [])
                                            if (t.get("key") or t.get("Key")) == "Name"), "")
            if label:
                names["file_system"] = f"{label} ({fs_id})"
        except Exception:  # noqa: BLE001
            pass
    return names


def console_sheet(*, name: str, platform: str, image: str, role_name: str, names: dict,
                  mount_path: str, environment: dict) -> list[str]:
    """Each console field to set, in the order the Create runtime form shows them."""
    mount_leaf = mount_path.removeprefix("/mnt/")
    lines = [
        "Create it in the console: Amazon Bedrock AgentCore > Runtime > Create runtime",
        "",
        "  Runtime details",
        f"    Name                          {name}   (replace the suggested name)",
        f"    Runtime platform versions     {platform}",
        "    Compute type                  microVMs",
        "  Agent source",
        "    Source type                   ECR Container",
        f"    Image URI                     {image}",
        "  Permissions",
        "    IAM permissions               Use another role > Choose an existing role",
        f"                                  {role_name}",
        "  Inbound Auth (expand it)",
        "    Protocol                      HTTP",
        "    Inbound Auth Type             Use IAM permissions",
        "  Filesystem configuration (expand it) > Add filesystem",
        "    Filesystem type               S3 files",
        f"    Filesystem                    {names.get('file_system') or '(the workshop file system)'}",
        f"    Access point                  {names.get('access_point', '')}",
        f"    Mount path                    /mnt/{mount_leaf}   (type {mount_leaf} after /mnt/), then Save",
        "  Advanced configurations (expand it)",
        "    Security                      VPC (Virtual Private Cloud)",
        f"    VPC                           {names.get('vpc') or '(the workshop VPC)'}",
    ]
    for label, values in (("Subnets", names.get("subnets", {})),
                          ("Security groups", names.get("security_groups", {}))):
        for i, value in enumerate(values.values()):
            lines.append(f"    {label if i == 0 else '':<30}{value}")
    lines.append("    Environment variables         Add new variable, once per line:")
    for key, value in environment.items():
        lines.append(f"      {key:<28}{value}")
    lines += ["", "Then choose Create runtime. It is Ready within about a minute.",
              f"Finish with: ./deploy-prebuilt.sh {name.replace('_', '-')} --adopt"]
    return lines


def check_console_runtime(current: dict | None, *, image: str, role_name: str,
                          subnets: list, security_groups: list, mount_ap_arn: str,
                          mount_path: str, environment: dict,
                          platform: str | None = None) -> list[tuple[bool, str, str]]:
    """Compare a person-built Runtime with the request the workshop expects.

    Returns (ok, what was checked, what to change); extra environment variables and
    console-only defaults such as lifecycle are allowed."""
    if current is None:
        return [(False, "Runtime exists", "create it in the console with the --prepare values")]
    checks = []

    def check(ok: bool, label: str, fix: str) -> None:
        checks.append((bool(ok), label, "" if ok else fix))

    status = current.get("status", "UNKNOWN")
    check(status == "READY", f"status READY (now {status})",
          "wait until the console shows Ready, then run --adopt again")
    actual_platform = current.get("platformVersion")
    if platform and actual_platform is not None:
        check(actual_platform == platform, f"platform {platform}",
              f"choose {platform} under Runtime platform versions (Update runtime)")
    uri = ((current.get("agentRuntimeArtifact") or {}).get("containerConfiguration") or {}).get(
        "containerUri", "")
    check(uri == image, "image is the workshop's pre-built image", f"Image URI must be {image}")
    role = current.get("roleArn", "")
    check(role.endswith(f":role/{role_name}"), f"execution role {role_name}",
          f"choose the existing role {role_name}")
    protocol = (current.get("protocolConfiguration") or {}).get("serverProtocol", "")
    check(protocol == "HTTP", "protocol HTTP", "choose HTTP under Inbound Auth > Protocol")
    net = current.get("networkConfiguration") or {}
    config = net.get("networkModeConfig") or {}
    check(net.get("networkMode") == "VPC", "network VPC",
          "choose VPC under Advanced configurations > Security")
    check(set(config.get("subnets") or []) == set(subnets), "both private agent subnets",
          "choose exactly the two agent-private subnets from --prepare")
    check(set(config.get("securityGroups") or []) == set(security_groups),
          "the agent security group", "choose the AgentSecurityGroup from --prepare")
    mounts = [(item.get("s3FilesAccessPoint") or {}) for item in
              current.get("filesystemConfigurations") or []]
    want = mount_path.rstrip("/")
    check(any(m.get("accessPointArn") == mount_ap_arn and
              (m.get("mountPath") or "").rstrip("/") == want for m in mounts),
          f"S3 Files mounted at {want}",
          f"add the S3 files access point with mount path {want}")
    env = current.get("environmentVariables") or {}
    for key, value in environment.items():
        check(env.get(key) == value, f"environment {key}", f"add {key} = {value}")
    return checks


def adopt_console_runtime(control, *, name: str, wait_s: float = 180, **expected) -> tuple:
    """Find the Runtime by name, wait out creation, and check it; (record, checks)."""
    current = _by_name(control, name)
    deadline = time.monotonic() + wait_s
    while current is not None and current.get("status") in {"CREATING", "UPDATING"} \
            and time.monotonic() < deadline:
        print(f"Runtime {current['agentRuntimeId']}: {current['status']}...", file=sys.stderr,
              flush=True)
        time.sleep(POLL_SECONDS)
        current = control.get_agent_runtime(agentRuntimeId=current["agentRuntimeId"])
    checks = check_console_runtime(current, **expected)
    if current is None or not all(ok for ok, _, _ in checks):
        return None, checks
    return runtime_record(current), checks


def print_checks(checks: list, *, file=None) -> None:
    out = file or sys.stdout
    for ok, label, fix in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n        fix: {fix}" if fix else ""), file=out)
    passed = sum(1 for ok, _, _ in checks if ok)
    print(f"  {passed} of {len(checks)} checks passed", file=out)


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
