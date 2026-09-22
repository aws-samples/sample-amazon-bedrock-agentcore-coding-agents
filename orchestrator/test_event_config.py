"""Team-local discovery, explicit overrides, and safe destinations for IAM signing."""
from __future__ import annotations

import builtins
import json
import socket
from types import SimpleNamespace

import boto3
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
from botocore.stub import Stubber
import pytest

import event_config

EVENT_URL = "https://central123.execute-api.eu-west-1.amazonaws.com/live/"
OVERRIDE_URL = "https://own123.execute-api.us-east-1.amazonaws.com/event/"


@pytest.fixture(autouse=True)
def isolated_aws(tmp_path, monkeypatch):
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
                 "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "WORKSHOP_GALLERY_URL", "WORKSHOP_LEADERBOARD_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "empty-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "empty-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    def no_network(*_args, **_kwargs):
        pytest.fail("Event configuration tests must never open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.fixture
def config():
    return {
        "version": 1,
        "gallery_url": EVENT_URL,
        "leaderboard_url": EVENT_URL,
        "game_url": "https://games123.cloudfront.net/",
        "team_id": "team-1",
        "team_name": "Team 1",
        "event_id": "event-1",
    }


@pytest.fixture
def ssm(monkeypatch):
    """A real SDK serializer with only its SSM transport replaced by Stubber."""
    aws_session = boto3.Session(
        region_name="eu-west-1", aws_access_key_id="testing", aws_secret_access_key="testing",
    )
    client = aws_session.client("ssm")
    calls = []

    def make_client(service, **kwargs):
        calls.append((service, kwargs))
        return client

    session = SimpleNamespace(region_name="eu-west-1", client=make_client)
    monkeypatch.setattr(boto3, "Session", lambda: session)
    with Stubber(client) as stub:
        yield stub, calls, session
        stub.assert_no_pending_responses()


def _parameter(stub, value, parameter_type="String"):
    stub.add_response(
        "get_parameter",
        {"Parameter": {"Name": "/workshop/event-config", "Type": parameter_type, "Value": value}},
        {"Name": "/workshop/event-config", "WithDecryption": False},
    )


@pytest.fixture
def forbid_sdk(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"boto3", "botocore"}:
            pytest.fail("An explicit endpoint must not import or consult the AWS SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


@pytest.mark.parametrize("kind", ["leaderboard", "gallery"])
def test_explicit_endpoint_wins_without_sdk_or_a_region(kind, monkeypatch, forbid_sdk):
    monkeypatch.setenv(f"WORKSHOP_{kind.upper()}_URL", "invalid lower-priority endpoint")
    resolver = getattr(event_config, f"resolve_{kind}_url")
    assert resolver(OVERRIDE_URL.rstrip("/")) == OVERRIDE_URL


@pytest.mark.parametrize("kind", ["leaderboard", "gallery"])
def test_matching_environment_override_needs_no_sdk_or_region(kind, monkeypatch, forbid_sdk):
    other = "gallery" if kind == "leaderboard" else "leaderboard"
    monkeypatch.setenv(f"WORKSHOP_{kind.upper()}_URL", OVERRIDE_URL)
    monkeypatch.setenv(f"WORKSHOP_{other.upper()}_URL", "not this client's endpoint")
    assert getattr(event_config, f"resolve_{kind}_url")() == OVERRIDE_URL


@pytest.mark.parametrize("kind", ["leaderboard", "gallery"])
def test_invalid_cli_override_never_falls_back_to_environment_or_ssm(kind, monkeypatch, forbid_sdk):
    monkeypatch.setenv(f"WORKSHOP_{kind.upper()}_URL", EVENT_URL)
    with pytest.raises(event_config.EventConfigError) as error:
        getattr(event_config, f"resolve_{kind}_url")("")
    assert error.value.code == "EVENT_ENDPOINT_INVALID"


@pytest.mark.parametrize("kind", ["leaderboard", "gallery"])
def test_invalid_environment_override_never_falls_back_to_ssm(kind, monkeypatch, forbid_sdk):
    monkeypatch.setenv(f"WORKSHOP_{kind.upper()}_URL", "https://untrusted.example/live/")
    with pytest.raises(event_config.EventConfigError) as error:
        getattr(event_config, f"resolve_{kind}_url")()
    assert error.value.code == "EVENT_ENDPOINT_INVALID"


def test_validated_urls_keep_named_stage_case_and_derive_their_own_region():
    url = "HTTPS://API123.EXECUTE-API.EU-WEST-1.AMAZONAWS.COM/Event_2026"
    assert event_config.validate_endpoint_url(url) == \
        "https://api123.execute-api.eu-west-1.amazonaws.com/Event_2026/"
    assert event_config.endpoint_region(url) == "eu-west-1"


@pytest.mark.parametrize("url", [
    "http://abc.execute-api.eu-west-1.amazonaws.com/live/",
    "https://abc.execute-api.eu-west-1.amazonaws.com.evil.example/live/",
    "https://abc.execute-api.eu-west-1.amazonaws.com./live/",
    "https://abc.execute-api.eu-west-1.amazonaws.com:443/live/",
    "https://user:password@abc.execute-api.eu-west-1.amazonaws.com/live/",
    "https://abc.execute-api.eu-west-1.amazonaws.com@evil.example/live/",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live/?token=private",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live/?",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live/#fragment",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live/#",
    "https://abc.execute-api.eu-west-1.amazonaws.com/",
    "https://abc.execute-api.eu-west-1.amazonaws.com",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live/scores",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live//",
    "https://abc.execute-api.eu-west-1.amazonaws.com//live/",
    "https://abc.execute-api.eu-west-1.amazonaws.com/%6cive/",
    "https://abc.execute-api.eu-west-1.amazonaws.com/../",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live%2fscores/",
    "https://abc.execute-api.eu-west-1.amazonaws.com\\@evil.example/live/",
    "https://abc.execute-api.not-a-region.amazonaws.com/live/",
    "https://abc.execute-api.eu-west-1.amazonaws.com/live/\n",
    " https://abc.execute-api.eu-west-1.amazonaws.com/live/",
    None,
])
def test_unsafe_or_ambiguous_destinations_are_rejected_without_echoing_them(url):
    for operation in (event_config.validate_endpoint_url, event_config.endpoint_region):
        with pytest.raises(event_config.EventConfigError) as error:
            operation(url)
        assert error.value.code == "EVENT_ENDPOINT_INVALID"
        if isinstance(url, str):
            assert url not in str(error.value)


def test_loads_complete_config_through_only_the_team_ssm_parameter(ssm, config):
    stub, calls, _session = ssm
    config["gallery_url"] = config["gallery_url"].rstrip("/")
    config["game_url"] = config["game_url"].rstrip("/")
    _parameter(stub, json.dumps(config))
    loaded = event_config.load_event_config()
    assert loaded == {**config, "gallery_url": EVENT_URL, "game_url": "https://games123.cloudfront.net/"}
    assert len(calls) == 1
    service, kwargs = calls[0]
    assert service == "ssm"
    assert kwargs["region_name"] == "eu-west-1"
    sdk_config = kwargs["config"]
    assert 0 < sdk_config.connect_timeout <= 3
    assert 0 < sdk_config.read_timeout <= 5
    assert sdk_config.retries == {"mode": "standard", "total_max_attempts": 2}


def test_reporter_cli_without_endpoint_uses_the_team_parameter(ssm, config, monkeypatch):
    import leaderboard

    stub, calls, _session = ssm
    _parameter(stub, json.dumps(config))
    destinations = []
    monkeypatch.setattr(
        leaderboard, "run", lambda *args, **kwargs: destinations.append(args[1]) or 0,
    )
    assert leaderboard.main(["--once"]) == 0
    assert destinations == [EVENT_URL]
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["leaderboard", "gallery"])
def test_no_matching_override_uses_ssm_even_if_other_endpoint_is_overridden(
        kind, monkeypatch, ssm, config):
    stub, calls, _session = ssm
    other = "gallery" if kind == "leaderboard" else "leaderboard"
    monkeypatch.setenv(f"WORKSHOP_{other.upper()}_URL", OVERRIDE_URL)
    _parameter(stub, json.dumps(config))
    assert getattr(event_config, f"resolve_{kind}_url")() == EVENT_URL
    assert len(calls) == 1


@pytest.mark.parametrize("aws_region,default_region,expected", [
    ("us-east-1", "us-west-2", "us-east-1"),
    (None, "us-west-2", "us-west-2"),
    (None, None, "eu-west-1"),
])
def test_discovery_region_is_environment_then_session(
        aws_region, default_region, expected, monkeypatch, ssm, config):
    stub, calls, session = ssm
    if aws_region:
        monkeypatch.setenv("AWS_REGION", aws_region)
    if default_region:
        monkeypatch.setenv("AWS_DEFAULT_REGION", default_region)
    _parameter(stub, json.dumps(config))
    assert event_config.load_event_config(session=session)["leaderboard_url"] == EVENT_URL
    assert calls[0][1]["region_name"] == expected
    assert event_config.endpoint_region(EVENT_URL) == "eu-west-1"


def test_missing_region_fails_before_creating_any_client(monkeypatch):
    session = SimpleNamespace(
        region_name=None, client=lambda *_a, **_k: pytest.fail("No resolved region"),
    )
    monkeypatch.setattr(boto3, "Session", lambda: session)
    with pytest.raises(event_config.EventConfigError) as error:
        event_config.load_event_config()
    assert error.value.code == "EVENT_CONFIG_REGION_MISSING"
    assert "AWS_DEFAULT_REGION" in str(error.value)
    assert "own-account" in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("version", None), ("version", True), ("version", 1.0), ("version", 2),
    ("team_id", ""), ("team_id", 123), ("team_name", "  "), ("event_id", "bad\nid"),
    ("gallery_url", "https://untrusted.example/live/"),
    ("leaderboard_url", OVERRIDE_URL),
    ("game_url", "https://games.cloudfront.net.evil.example/"),
    ("game_url", "https://user:password@games.cloudfront.net/"),
    ("game_url", EVENT_URL), ("game_url", "https://games.cloudfront.net/game/"),
    ("game_url", "https://games.cloudfront.net/?private=value"),
    ("game_url", "https://-games.cloudfront.net/"),
    ("game_url", "https://games-.cloudfront.net/"),
])
def test_malformed_schema_fails_without_publishing_any_of_its_contents(ssm, config, field, value):
    stub, _calls, _session = ssm
    config[field] = value
    _parameter(stub, json.dumps(config))
    with pytest.raises(event_config.EventConfigError) as error:
        event_config.load_event_config()
    assert error.value.code == "EVENT_CONFIG_MALFORMED"
    assert "private=value" not in str(error.value)
    assert "user:password" not in str(error.value)


@pytest.mark.parametrize("field", [
    "version", "gallery_url", "leaderboard_url", "game_url", "team_id", "team_name", "event_id",
])
def test_incomplete_config_is_not_accepted_as_the_version_one_schema(ssm, config, field):
    stub, _calls, _session = ssm
    del config[field]
    _parameter(stub, json.dumps(config))
    with pytest.raises(event_config.EventConfigError) as error:
        event_config.load_event_config()
    assert error.value.code == "EVENT_CONFIG_MALFORMED"


@pytest.mark.parametrize("raw", ["not json", "null", "[]", '{"version":1,"version":2}'])
def test_malformed_json_and_duplicate_fields_are_not_silently_accepted(ssm, raw):
    stub, _calls, _session = ssm
    _parameter(stub, raw)
    with pytest.raises(event_config.EventConfigError) as error:
        event_config.load_event_config()
    assert error.value.code == "EVENT_CONFIG_MALFORMED"


@pytest.mark.parametrize("parameter_type", ["SecureString", "StringList"])
def test_discovery_requires_a_plain_string_and_never_requests_decryption(ssm, config, parameter_type):
    stub, _calls, _session = ssm
    _parameter(stub, json.dumps(config), parameter_type)
    with pytest.raises(event_config.EventConfigError) as error:
        event_config.load_event_config()
    assert error.value.code == "EVENT_CONFIG_MALFORMED"
    assert "SSM String" in str(error.value)


@pytest.mark.parametrize("aws_code,expected", [
    ("ParameterNotFound", "EVENT_CONFIG_MISSING"),
    ("AccessDeniedException", "EVENT_CONFIG_ACCESS_DENIED"),
    ("ExpiredTokenException", "EVENT_CONFIG_UNAVAILABLE"),
    ("InternalServerError", "EVENT_CONFIG_UNAVAILABLE"),
])
def test_aws_failure_categories_stay_distinct_and_service_details_are_not_leaked(ssm, aws_code, expected):
    stub, _calls, _session = ssm
    stub.add_client_error(
        "get_parameter", aws_code, service_message="untrusted-private-detail",
        expected_params={"Name": "/workshop/event-config", "WithDecryption": False},
    )
    with pytest.raises(event_config.EventConfigError) as error:
        event_config.resolve_gallery_url()
    assert error.value.code == expected
    assert "own-account" in str(error.value)
    assert "untrusted-private-detail" not in str(error.value)


@pytest.mark.parametrize("failure", [
    EndpointConnectionError(endpoint_url="https://do-not-echo.example/private"),
    NoCredentialsError(),
])
def test_sdk_transport_and_credentials_failures_do_not_claim_missing_config(failure):
    def fail(*_args, **_kwargs):
        raise failure

    session = SimpleNamespace(region_name="eu-west-1", client=fail)
    with pytest.raises(event_config.EventConfigError) as error:
        event_config.load_event_config(session=session)
    assert error.value.code == "EVENT_CONFIG_UNAVAILABLE"
    assert "do-not-echo" not in str(error.value)
