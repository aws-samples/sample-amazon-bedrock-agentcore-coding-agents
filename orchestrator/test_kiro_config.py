"""Kiro API key -> Token Vault provisioning (kiro_config.py).

The point: a pasted ksk_ key is validated, recorded as masked status, and never
written to a tracked file. Isolated via the REAL env seams (WORKSHOP_KIRO_SETTINGS
points at a tmp file; WORKSHOP_KIRO_DISABLE_VAULT=1 skips the boto3 call), never a
monkeypatch of module internals.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import boto3
from botocore.stub import Stubber
import pytest

import kiro_config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_KIRO_SETTINGS", str(tmp_path / "kiro.local.json"))
    monkeypatch.setenv("WORKSHOP_KIRO_DISABLE_VAULT", "1")  # offline: no AWS
    monkeypatch.setenv("WORKSHOP_BEDROCK_REGION", "us-west-2")


def test_unset_is_not_connected():
    s = kiro_config.status()
    assert s["connected"] is False
    assert s["provider"] == "kiro-api-key"


def test_rejects_non_ksk_key():
    out = kiro_config.save_api_key("ghp_github_token")
    assert "error" in out
    # nothing persisted on a rejected key
    assert kiro_config.status()["connected"] is False


def test_rejects_empty_key_when_unset():
    assert "error" in kiro_config.save_api_key("")


def test_save_then_status_masks_the_key():
    out = kiro_config.save_api_key("ksk_secret_value_1234")
    assert "error" not in out
    assert out["connected"] is True
    assert out["key_tail"] == "…1234"
    # the full key NEVER appears in status or the sidecar file
    assert "secret_value" not in json.dumps(kiro_config.status())
    sidecar = open(os.environ["WORKSHOP_KIRO_SETTINGS"]).read()
    assert "ksk_secret_value_1234" not in sidecar
    assert "secret_value" not in sidecar


def test_sidecar_is_0600():
    kiro_config.save_api_key("ksk_secret_value_1234")
    mode = os.stat(os.environ["WORKSHOP_KIRO_SETTINGS"]).st_mode & 0o777
    assert mode == 0o600


def test_clear_disconnects():
    kiro_config.save_api_key("ksk_secret_value_1234")
    assert kiro_config.status()["connected"] is True
    kiro_config.clear_api_key()
    assert kiro_config.status()["connected"] is False


def test_empty_save_after_set_is_a_noop_keep():
    kiro_config.save_api_key("ksk_secret_value_1234")
    out = kiro_config.save_api_key("")  # status-only re-save keeps the stored key
    assert out["connected"] is True


@pytest.fixture
def vault(monkeypatch):
    # Stub the AWS boundary; exercise the real region, status, and mutation code.
    client = boto3.client("bedrock-agentcore-control", region_name="us-east-1",
                          aws_access_key_id="testing", aws_secret_access_key="testing")
    monkeypatch.delenv("WORKSHOP_KIRO_DISABLE_VAULT")
    monkeypatch.setenv("WORKSHOP_BEDROCK_REGION", "us-east-1")
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: client)
    with Stubber(client) as stub:
        yield stub
        stub.assert_no_pending_responses()


def _provider_metadata():
    return {
        "name": "kiro-api-key",
        "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/apikeycredentialprovider/kiro-api-key",
        "apiKeySecretArn": {"secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:kiro-testing"},
        "createdTime": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "lastUpdatedTime": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def test_discovers_cli_provisioned_credential_without_a_console_sidecar(vault):
    vault.add_response("get_api_key_credential_provider", _provider_metadata(), {"name": "kiro-api-key"})
    result = kiro_config.status()
    assert result["connected"] is True
    assert result["source"] == "token-vault"
    assert result["region"] == "us-east-1"
    assert "apiKey" not in json.dumps(result)
    assert "secretArn" not in json.dumps(result)
    assert not os.path.exists(os.environ["WORKSHOP_KIRO_SETTINGS"])


def test_missing_provider_is_not_configured(vault):
    vault.add_client_error("get_api_key_credential_provider", "ResourceNotFoundException",
                          expected_params={"name": "kiro-api-key"})
    assert kiro_config.status()["connected"] is False


def test_access_denied_is_unknown_not_missing(vault):
    vault.add_client_error("get_api_key_credential_provider", "AccessDeniedException",
                          expected_params={"name": "kiro-api-key"})
    result = kiro_config.status()
    assert result["connected"] is None
    assert "error" in result


def test_failed_delete_preserves_status_for_the_key_that_still_exists(vault):
    path = os.environ["WORKSHOP_KIRO_SETTINGS"]
    with open(path, "w") as stream:
        json.dump({"stored": True, "region": "us-east-1"}, stream)
    vault.add_client_error("delete_api_key_credential_provider", "AccessDeniedException",
                          expected_params={"name": "kiro-api-key"})
    assert "error" in kiro_config.clear_api_key()
    assert json.load(open(path))["stored"] is True


def test_refresh_checks_aws_even_with_cached_status(vault):
    path = os.environ["WORKSHOP_KIRO_SETTINGS"]
    with open(path, "w") as stream:
        json.dump({"stored": True, "region": "us-east-1"}, stream)
    vault.add_client_error("get_api_key_credential_provider", "ResourceNotFoundException",
                          expected_params={"name": "kiro-api-key"})
    assert kiro_config.status(refresh=True)["connected"] is False
    vault.add_client_error("get_api_key_credential_provider", "ResourceNotFoundException",
                          expected_params={"name": "kiro-api-key"})
    assert kiro_config.status()["connected"] is False
    assert json.load(open(path))["stored"] is False


@pytest.mark.parametrize("region,provider", [
    ("us-west-2", "kiro-api-key"),
    ("us-east-1", "previous-provider"),
])
def test_cached_key_for_another_target_does_not_connect_this_target(vault, region, provider):
    path = os.environ["WORKSHOP_KIRO_SETTINGS"]
    with open(path, "w") as stream:
        json.dump({"stored": True, "region": region, "provider": provider}, stream)
    vault.add_client_error("get_api_key_credential_provider", "ResourceNotFoundException",
                          expected_params={"name": "kiro-api-key"})
    result = kiro_config.status()
    assert result["connected"] is False
    assert result["region"] == "us-east-1"
    assert result["provider"] == "kiro-api-key"
    # Discovery in this target does not rewrite another target's saved metadata.
    assert json.load(open(path))["stored"] is True


def test_region_uses_aws_default_region_without_a_hardcoded_fallback(monkeypatch):
    monkeypatch.delenv("WORKSHOP_BEDROCK_REGION")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    assert kiro_config.status()["region"] == "eu-west-1"


# --- September 25 event: keys that only failed inside a Runtime session -------------

def _fake_kiro(tmp_path, monkeypatch, output):
    chat = tmp_path / "bin" / "kiro-cli-chat"
    chat.parent.mkdir(exist_ok=True)
    chat.write_text(f"#!/bin/sh\necho '{output}'\n")
    chat.chmod(0o755)
    monkeypatch.setenv("PATH", f"{chat.parent}:{os.environ['PATH']}")
    monkeypatch.delenv("WORKSHOP_KIRO_DISABLE_VAULT")
    monkeypatch.setattr(kiro_config, "_provision_vault", lambda key: None)


def test_a_key_kiro_rejects_is_not_saved(tmp_path, monkeypatch):
    _fake_kiro(tmp_path, monkeypatch, "The bearer token included in the request is invalid.")
    out = kiro_config.save_api_key("ksk_secret_value_1234")
    assert "Kiro rejected this key" in out["error"]
    assert not (tmp_path / "kiro.local.json").exists()


def test_a_key_kiro_answers_is_saved_as_verified(tmp_path, monkeypatch):
    _fake_kiro(tmp_path, monkeypatch, "KIRO_KEY_OK")
    monkeypatch.setattr(kiro_config, "_control_client", lambda: pytest.fail("cached status expected"))
    monkeypatch.setenv("WORKSHOP_KIRO_DISABLE_VAULT", "1")  # status() reads the sidecar
    monkeypatch.setattr(kiro_config, "_check_with_kiro", lambda key: "verified")
    out = kiro_config.save_api_key("ksk_secret_value_1234")
    assert out["connected"] is True and out.get("key_check") == "verified"


def test_an_unclear_answer_never_blocks_a_save(tmp_path, monkeypatch):
    _fake_kiro(tmp_path, monkeypatch, "network unreachable")
    assert kiro_config._check_with_kiro("ksk_secret_value_1234") is None


def test_a_pasted_key_with_quotes_or_a_wrapped_line_is_cleaned():
    out = kiro_config.save_api_key(' "ksk_secret_\nvalue_1234" ')
    assert out["connected"] is True and out["key_tail"].endswith("1234")


def test_an_empty_paste_explains_the_hidden_prompt():
    assert "hidden prompt" in kiro_config.save_api_key("")["error"]
