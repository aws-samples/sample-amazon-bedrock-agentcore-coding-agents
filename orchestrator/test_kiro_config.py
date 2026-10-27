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
    out = kiro_config.save_api_key("", allow_empty=True)  # the console's status-only re-save
    assert out["connected"] is True


def test_an_empty_paste_at_the_prompt_never_reports_the_old_key_as_saved():
    """Rerunning Lab 1's block after an invalid-bearer error, with a paste that did
    not arrive, printed "connected": true with the OLD key's tail."""
    kiro_config.save_api_key("ksk_secret_value_1234")
    assert "hidden prompt" in kiro_config.save_api_key("")["error"]


@pytest.mark.parametrize("pasted", ["Bearer ksk_secret_value_1234",
                                    "KiroApiKey\tksk_secret_value_1234",
                                    "KIRO_API_KEY=ksk_secret_value_1234"])
def test_the_key_is_taken_from_what_people_actually_paste(pasted):
    out = kiro_config.save_api_key(pasted)
    assert out["connected"] is True and out["key_tail"].endswith("1234")


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


# --- Event path: import_from_secret (the central account's per-team key) ----------
DELIVERED = "ksk_delivered_team_key_7f3a"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:kiro-api-key-AbCdEf"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture
def delivered(monkeypatch):
    """Real botocore clients for both services, stubbed at the AWS boundary, so every
    request and response is checked against the real service models."""
    monkeypatch.delenv("WORKSHOP_KIRO_DISABLE_VAULT")
    monkeypatch.setenv("WORKSHOP_BEDROCK_REGION", "us-east-1")
    clients = {name: boto3.client(name, region_name="us-east-1", aws_access_key_id="testing",
                                  aws_secret_access_key="testing")
               for name in ("secretsmanager", "bedrock-agentcore-control")}
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: clients[service])
    with Stubber(clients["secretsmanager"]) as secrets, \
            Stubber(clients["bedrock-agentcore-control"]) as vault:
        yield secrets, vault
        secrets.assert_no_pending_responses()
        vault.assert_no_pending_responses()


def _secret_value(value):
    return {"ARN": SECRET_ARN, "Name": "kiro-api-key", "SecretString": value}


def _expect_vault_create(vault, key=DELIVERED):
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    vault.add_response("get_workload_identity", {
        "name": "kiro-coding-agent", "createdTime": now, "lastUpdatedTime": now,
        "workloadIdentityArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/kiro-coding-agent"},
        {"name": "kiro-coding-agent"})
    vault.add_client_error("get_api_key_credential_provider", "ResourceNotFoundException",
                           expected_params={"name": "kiro-api-key"})
    meta = _provider_metadata()
    vault.add_response("create_api_key_credential_provider", {
        k: meta[k] for k in ("apiKeySecretArn", "name", "credentialProviderArn")},
        {"name": "kiro-api-key", "apiKey": key})


def test_a_late_secret_is_waited_for_then_stored_in_the_token_vault(delivered):
    secrets, vault = delivered
    for _ in range(2):
        secrets.add_client_error("get_secret_value", "ResourceNotFoundException",
                                 expected_params={"SecretId": "kiro-api-key"})
    secrets.add_response("get_secret_value", _secret_value(DELIVERED), {"SecretId": "kiro-api-key"})
    _expect_vault_create(vault)
    clock = _Clock()
    out = kiro_config.import_from_secret(wait_s=300, poll_s=10, sleep=clock.sleep, now=clock.now)
    assert out["key_status"] == "READY"
    assert out["waited_s"] == 20 and out["key_format"] == "ksk"
    assert out["source"] == "central-account" and out["provider"] == "kiro-api-key"
    assert DELIVERED not in json.dumps(out) and DELIVERED[-4:] not in json.dumps(out)


def test_a_missing_secret_is_reported_with_the_fallback_not_raised(delivered):
    secrets, _vault = delivered
    for _ in range(4):
        secrets.add_client_error("get_secret_value", "ResourceNotFoundException",
                                 expected_params={"SecretId": "kiro-api-key"})
    clock = _Clock()
    out = kiro_config.import_from_secret(wait_s=30, poll_s=10, sleep=clock.sleep, now=clock.now)
    assert out["key_status"].startswith("UNAVAILABLE: the secret does not exist after 30 s")
    assert "claude_code_validator" in out["key_status"]
    assert clock.t - 1000.0 <= 30


def test_a_placeholder_value_is_not_a_key(delivered):
    secrets, vault = delivered
    secrets.add_response("get_secret_value", _secret_value("PENDING"), {"SecretId": "kiro-api-key"})
    secrets.add_response("get_secret_value", _secret_value(json.dumps({"key": DELIVERED})),
                         {"SecretId": "kiro-api-key"})
    _expect_vault_create(vault)
    clock = _Clock()
    out = kiro_config.import_from_secret(wait_s=60, poll_s=10, sleep=clock.sleep, now=clock.now)
    assert out["key_status"] == "READY" and out["waited_s"] == 10


def test_a_denied_read_stops_at_once(delivered):
    secrets, _vault = delivered
    secrets.add_client_error("get_secret_value", "AccessDeniedException",
                             expected_params={"SecretId": "kiro-api-key"})
    clock = _Clock()
    out = kiro_config.import_from_secret(wait_s=300, sleep=clock.sleep, now=clock.now)
    assert out["key_status"].startswith("UNAVAILABLE: cannot read the kiro-api-key secret (AccessDeniedException)")
    assert out["waited_s"] == 0


def test_a_key_without_the_ksk_prefix_is_still_stored(delivered):
    secrets, vault = delivered
    other = "kiro_service_key_0123456789abcdef"
    secrets.add_response("get_secret_value", _secret_value(other), {"SecretId": "kiro-api-key"})
    _expect_vault_create(vault, key=other)
    out = kiro_config.import_from_secret(wait_s=10, sleep=_Clock().sleep, now=_Clock().now)
    assert out["key_status"] == "READY" and out["key_format"] == "other"


def test_a_failed_vault_write_names_the_error_and_never_the_key(delivered):
    secrets, vault = delivered
    secrets.add_response("get_secret_value", _secret_value(DELIVERED), {"SecretId": "kiro-api-key"})
    vault.add_client_error("get_workload_identity", "AccessDeniedException",
                           expected_params={"name": "kiro-coding-agent"})
    out = kiro_config.import_from_secret(wait_s=10, sleep=_Clock().sleep, now=_Clock().now)
    assert out["key_status"].startswith("UNAVAILABLE: the Token Vault write failed (AccessDeniedException)")
    assert DELIVERED not in json.dumps(out)


def test_import_secret_cli_prints_status_only(delivered, capsys):
    secrets, vault = delivered
    secrets.add_response("get_secret_value", _secret_value(DELIVERED), {"SecretId": "team-key"})
    _expect_vault_create(vault)
    assert kiro_config.main(["import-secret", "--secret-id", "team-key", "--wait", "5"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["key_status"] == "READY" and DELIVERED not in printed


def test_a_central_kak_key_is_stored_and_reported_as_kak(delivered):
    """Test event c83a9584 (October 20): the key a WS central account mints is kak_."""
    secrets, vault = delivered
    kak = "kak_0123456789abcdefABCDEF0123456789"
    secrets.add_response("get_secret_value", _secret_value(kak), {"SecretId": "kiro-api-key"})
    _expect_vault_create(vault, key=kak)
    out = kiro_config.import_from_secret(wait_s=10, sleep=_Clock().sleep, now=_Clock().now)
    assert out["key_status"] == "READY" and out["key_format"] == "kak"
    assert kak not in json.dumps(out)


def test_a_pasted_kak_key_is_not_rejected_as_a_wrong_paste():
    out = kiro_config.save_api_key("not-a-key")
    assert "not a Kiro API key" in out["error"]
    import re as _re
    assert _re.findall(r"(?:ksk|kak)_[A-Za-z0-9_\-]+", "KIRO_API_KEY=kak_abcdef0123456789") == ["kak_abcdef0123456789"]


# The KiroKeyStatus output and /workshop/kiro/key-status carry whichever FALLBACK wrote
# the status: this module's (the usual failure paths of import-secret) or the
# template's own (the import never ran). Both must name the same page and step.
_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                         "static", "aws", "cfn.yaml")


def test_the_fallback_names_the_page_that_has_the_no_key_checker():
    assert kiro_config.FALLBACK == (
        "Fallback: use the checker that needs no key (claude_code_validator, Deploy the "
        "Coordinator, step 1), or ask a facilitator to rerun the team's Kiro key import.")


@pytest.mark.skipif(not os.path.isfile(_TEMPLATE),
                    reason="the event template ships in the content repository")
def test_the_fallback_matches_the_template_byte_for_byte():
    import re as _re
    text = open(_TEMPLATE, encoding="utf-8").read()
    shell = _re.findall(r'^\s*FALLBACK="([^"]*)"\s*$', text, _re.M)
    # Implicit concatenation joins the pieces with nothing in between.
    python = ["".join(_re.findall(r'"([^"]*)"', block))
              for block in _re.findall(r'^\s*FALLBACK = \(((?:\s*"[^"]*"\s*)+)\)', text, _re.M)]
    assert shell and python, "the template no longer defines both FALLBACK texts"
    assert set(shell) | set(python) == {kiro_config.FALLBACK}
