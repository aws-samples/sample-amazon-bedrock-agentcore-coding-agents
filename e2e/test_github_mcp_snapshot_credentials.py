"""Real SDK credential resolution; only MCP registration and AWS I/O are stubbed."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import boto3
from botocore.stub import Stubber
import pytest

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents/gateway_mcp/app/main.py"


@pytest.fixture
def app(monkeypatch, tmp_path):
    class Registration:
        def __init__(self, *args, **kwargs):
            pass

        def add_middleware(self, middleware):
            pass

        def tool(self):
            return lambda function: function

    # FastMCP is installed in the separate locked MCP image, not the host test
    # venv. Keep actual module import and request functions; stub registration.
    fastmcp = types.ModuleType("fastmcp")
    fastmcp.FastMCP = Registration
    middleware = types.ModuleType("fastmcp.server.middleware")
    middleware.Middleware = object
    middleware.MiddlewareContext = object
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "fastmcp.server", types.ModuleType("fastmcp.server"))
    monkeypatch.setitem(sys.modules, "fastmcp.server.middleware", middleware)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("GITHUB_APP_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:123456789012:secret:audit-ABCDEF")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("AWS_CREDENTIAL_EXPIRATION", raising=False)
    monkeypatch.delenv("AWS_SECURITY_TOKEN", raising=False)
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", boto3.Session(
        aws_access_key_id="snapshot-placeholder", aws_secret_access_key="not-a-secret",
        aws_session_token="not-a-token",
    ))
    default_client = Mock(side_effect=AssertionError("must not use the snapshotted default session"))
    monkeypatch.setattr(boto3, "client", default_client)
    spec = importlib.util.spec_from_file_location("github_mcp_snapshot_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not default_client.called, "startup must not resolve AWS credentials"
    assert module.GITHUB_TOKEN is None
    assert module.GITHUB_TOKEN_EXP == 0
    return module


def test_each_secret_read_uses_current_credentials_despite_a_stale_default_session(app, monkeypatch):
    session_type = boto3.Session
    credentials = []
    clients = []
    stubbers = []

    def session():
        current = session_type()
        real_client = current.client

        def client(*args, **kwargs):
            value = real_client(*args, **kwargs)
            credentials.append(value._request_signer._credentials.access_key)
            value.close = Mock(wraps=value.close)
            stub = Stubber(value)
            stub.add_response(
                "get_secret_value",
                {"SecretString": json.dumps({"app_id": str(len(credentials))})},
                {"SecretId": app.GITHUB_APP_SECRET_ARN},
            )
            stub.activate()
            stubbers.append(stub)
            clients.append(value)
            return value

        current.client = client
        return current

    monkeypatch.setattr(boto3, "Session", session)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "not-a-token")
    for index, key in enumerate(("restored-placeholder", "rotated-placeholder"), 1):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", key)
        assert app._load_app_creds() == {"app_id": str(index)}
    assert credentials == ["restored-placeholder", "rotated-placeholder"]
    assert boto3.DEFAULT_SESSION.get_credentials().access_key == "snapshot-placeholder"
    for client, stub in zip(clients, stubbers):
        client.close.assert_called_once()
        stub.assert_no_pending_responses()


def test_installation_token_is_still_lazy_and_refreshed_near_expiry(app, monkeypatch, capsys):
    moment = [1_800_000_000]
    monkeypatch.setattr(app.time, "time", lambda: moment[0])
    minted = []

    def mint():
        minted.append(moment[0])
        return "test-only-token", moment[0] + 3600

    monkeypatch.setattr(app, "_mint_installation_token", mint)

    async def downstream(context):
        return app._headers()

    async def exercise():
        middleware = app.TokenMiddleware()
        moment[0] += 7 * 86400
        await middleware.on_request(None, downstream)
        moment[0] += 60
        await middleware.on_request(None, downstream)
        assert len(minted) == 1
        moment[0] += 3600
        await middleware.on_request(None, downstream)

    assert not minted
    asyncio.run(exercise())
    assert len(minted) == 2
    logged = capsys.readouterr().out
    assert "[TokenMiddleware] minted token expires_in=3600s" in logged
    assert "test-only-token" not in logged
    assert "test-only-token"[:8] not in logged
