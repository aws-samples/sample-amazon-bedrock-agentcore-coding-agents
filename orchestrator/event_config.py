"""Discover the event endpoints published in the team's own AWS account.

Public API:
  resolve_leaderboard_url(explicit_url=None) / resolve_gallery_url(explicit_url=None)
    Return a validated base URL, in CLI -> matching WORKSHOP_*_URL -> SSM order.
    An override needs no SDK import, AWS session, credentials, or discovery call.
  load_event_config()
    Read /workshop/event-config (String, schema version 1) using the current team's
    credentials and AWS region. Return the validated dict, with normalized URLs.
  validate_endpoint_url(url) / endpoint_region(url)
    Validate a regional API Gateway URL with one named stage before signing.

All returned base URLs end in "/". Discovery makes one GetParameter operation;
there is no central-account API call, role assumption, or endpoint probe here.
The optional session argument accepts a boto3 Session for callers sharing one.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

PARAMETER_NAME = "/workshop/event-config"
_REGION = r"[a-z]{2}(?:-[a-z]+)+-[0-9]+"
_ENDPOINT = re.compile(
    rf"https://(?P<host>[a-z0-9]{{1,63}}\.execute-api\."
    rf"(?P<region>{_REGION})\.amazonaws\.com)/"
    r"(?P<stage>[a-z0-9_-]{1,128})/?",
    re.IGNORECASE | re.ASCII,
)
_GAME_URL = re.compile(
    r"https://(?P<host>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudfront\.net)"
    r"(?P<path>(?-i:/play/)|/?)",
    re.IGNORECASE | re.ASCII,
)
_EXPLICIT_HINT = (
    "For an own-account workshop, supply an explicit endpoint or set "
    "WORKSHOP_LEADERBOARD_URL / WORKSHOP_GALLERY_URL."
)


class EventConfigError(ValueError):
    """An actionable discovery/validation failure; code is stable for callers."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message} {_EXPLICIT_HINT}")


def _endpoint_match(url: str) -> re.Match[str]:
    # Match the entire URL rather than trusting only urlparse().hostname: no
    # userinfo, ports, suffix hosts, query/fragment, escapes, or extra path parts
    # may accompany a request that is about to receive an IAM signature.
    match = _ENDPOINT.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        raise EventConfigError(
            "EVENT_ENDPOINT_INVALID",
            "Expected an HTTPS API Gateway base URL with one named stage, such as "
            "https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/; "
            "userinfo, ports, query strings, fragments, and resource paths are not allowed.",
        )
    return match


def validate_endpoint_url(url: str) -> str:
    """Return the canonical HTTPS API Gateway base URL, or raise EventConfigError."""
    match = _endpoint_match(url)
    return f"https://{match['host'].lower()}/{match['stage']}/"


def endpoint_region(url: str) -> str:
    """Derive the signing region from a fully validated endpoint, not AWS defaults."""
    return _endpoint_match(url)["region"].lower()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate event-config field")
        result[key] = value
    return result


def _validate_config(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("expected an object with version 1")
        for field in ("team_id", "team_name", "event_id"):
            value = data.get(field)
            if (not isinstance(value, str) or not value.strip()
                    or value != value.strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError(f"{field} must be nonempty text without control characters")
        for field in ("gallery_url", "leaderboard_url"):
            try:
                data[field] = validate_endpoint_url(data.get(field))
            except EventConfigError:
                raise ValueError(f"{field} must be an HTTPS API Gateway base URL with a stage") from None
        if data["gallery_url"] != data["leaderboard_url"]:
            raise ValueError("gallery_url and leaderboard_url must name the same event endpoint")
        game_url = data.get("game_url")
        match = _GAME_URL.fullmatch(game_url) if isinstance(game_url, str) else None
        if match is None:
            raise ValueError("game_url must be an HTTPS CloudFront root or /play/ URL")
        # Keep schema-1 root URLs readable for legacy score reporting. New
        # galleries use /play/; dropping that prefix would point visitors at IDE.
        data["game_url"] = f"https://{match['host'].lower()}{match['path'] or '/'}"
        return data
    except (TypeError, ValueError) as exc:
        # Do not include the parameter contents or JSON parser's input excerpt.
        detail = str(exc) if not isinstance(exc, json.JSONDecodeError) else "invalid JSON"
        raise EventConfigError(
            "EVENT_CONFIG_MALFORMED", f"{PARAMETER_NAME} is malformed: {detail}.",
        ) from None


def load_event_config(*, session: Any = None) -> dict[str, Any]:
    """Read and validate the current team's version-1 SSM String parameter.

    Region precedence is AWS_REGION, AWS_DEFAULT_REGION, then the session/profile.
    SSM uses 3-second connect and 5-second read timeouts, with at most two attempts.
    SDK/credential failures stay distinct from a confirmed missing parameter.
    """
    try:
        import boto3  # noqa: PLC0415 (explicit URL paths do not need the SDK)
        from botocore.config import Config  # noqa: PLC0415
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415
    except ImportError:
        raise EventConfigError(
            "EVENT_CONFIG_UNAVAILABLE", "Install boto3 to discover the team's event configuration.",
        ) from None

    try:
        if session is None:
            session = boto3.Session()
        region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
                  or session.region_name)
        if not region:
            raise EventConfigError(
                "EVENT_CONFIG_REGION_MISSING",
                "Set AWS_REGION or AWS_DEFAULT_REGION, or configure your AWS profile's region.",
            )
        if not isinstance(region, str) or re.fullmatch(_REGION, region) is None:
            raise EventConfigError(
                "EVENT_CONFIG_REGION_INVALID", "The configured AWS region is invalid.",
            )
        client = session.client(
            "ssm", region_name=region,
            config=Config(connect_timeout=3, read_timeout=5,
                          retries={"mode": "standard", "total_max_attempts": 2}),
        )
        response = client.get_parameter(Name=PARAMETER_NAME, WithDecryption=False)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ParameterNotFound":
            raise EventConfigError(
                "EVENT_CONFIG_MISSING",
                f"{PARAMETER_NAME} is missing in this team's AWS account and region. "
                "Ask the facilitator to complete event setup.",
            ) from None
        if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
            raise EventConfigError(
                "EVENT_CONFIG_ACCESS_DENIED",
                f"SSM denied GetParameter for {PARAMETER_NAME} in this team's AWS account. "
                "Check the team's permission to read that parameter.",
            ) from None
        raise EventConfigError(
            "EVENT_CONFIG_UNAVAILABLE",
            f"SSM could not read {PARAMETER_NAME}; check the team's AWS session and service availability.",
        ) from None
    except BotoCoreError:
        raise EventConfigError(
            "EVENT_CONFIG_UNAVAILABLE",
            "Could not read the team's event configuration; check AWS credentials, profile, and connectivity.",
        ) from None

    parameter = response.get("Parameter", {})
    if (not isinstance(parameter, dict) or parameter.get("Type") != "String"
            or not isinstance(parameter.get("Value"), str)):
        raise EventConfigError(
            "EVENT_CONFIG_MALFORMED", f"{PARAMETER_NAME} must be an SSM String containing version-1 JSON.",
        )
    return _validate_config(parameter["Value"])


def _resolve_url(kind: str, explicit_url: str | None, session: Any) -> str:
    if explicit_url is not None:
        return validate_endpoint_url(explicit_url)
    override = os.environ.get(f"WORKSHOP_{kind.upper()}_URL")
    if override:
        return validate_endpoint_url(override)
    return load_event_config(session=session)[f"{kind}_url"]


def resolve_leaderboard_url(explicit_url: str | None = None, *, session: Any = None) -> str:
    """CLI endpoint, then WORKSHOP_LEADERBOARD_URL, then this team's SSM config."""
    return _resolve_url("leaderboard", explicit_url, session)


def resolve_gallery_url(explicit_url: str | None = None, *, session: Any = None) -> str:
    """CLI endpoint, then WORKSHOP_GALLERY_URL, then this team's SSM config."""
    return _resolve_url("gallery", explicit_url, session)
