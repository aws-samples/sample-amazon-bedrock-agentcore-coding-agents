"""Kiro API key -> Token Vault provisioning (kiro_config.py).

The point: a pasted ksk_ key is validated, recorded as masked status, and never
written to a tracked file. Isolated via the REAL env seams (WORKSHOP_KIRO_SETTINGS
points at a tmp file; WORKSHOP_KIRO_DISABLE_VAULT=1 skips the boto3 call), never a
monkeypatch of module internals.
"""
from __future__ import annotations

import json
import os

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
