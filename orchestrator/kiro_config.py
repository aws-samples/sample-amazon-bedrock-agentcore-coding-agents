"""Kiro API key: provision it into the AgentCore Identity Token Vault from Settings.

Kiro's only non-interactive auth is its API key (``ksk_...``); device flow needs a
human in a browser. The deployed Kiro runtime fetches that key at session start from
the Token Vault (``get_workload_access_token`` -> ``get_resource_api_key`` for the
``kiro-api-key`` credential provider). This module is the Settings-pane path that puts
the key THERE: an operator pastes ``ksk_...`` in the console, and we create/update the
``kiro-api-key`` credential provider so the ALREADY-DEPLOYED runtime authenticates with
NO redeploy.

Mirrors ``github.py``: the secret itself lives only in the Token Vault (encrypted in
Secrets Manager via KMS), never in a tracked file. A gitignored 0600 sidecar records
only NON-secret status (provider name, region, the key's last 4 chars) so the console
can show "connected" + a masked tail without ever re-reading the key.

Wirable seams (tests set these, never patch internals):
  WORKSHOP_KIRO_SETTINGS      path of the 0600 status sidecar (default .runs/kiro.local.json)
  WORKSHOP_KIRO_PROVIDER       credential provider name (default kiro-api-key)
  WORKSHOP_KIRO_WORKLOAD       workload identity name (default kiro-coding-agent)
  WORKSHOP_BEDROCK_REGION      optional region override; otherwise use the AWS environment/profile
  WORKSHOP_KIRO_DISABLE_VAULT  "1" to skip the boto3 vault call (offline unit tests):
                               the key is validated + the sidecar written, no AWS.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_RUNS_DIR = os.environ.get("WORKSHOP_RUNS_DIR", os.path.join(_REPO_ROOT, ".runs"))

# Kiro API keys are the ksk_ prefix per the Kiro docs; we validate the shape so a
# wrong paste (a GitHub PAT, a blank) fails loud instead of poisoning the vault.
_KEY_PREFIX = "ksk_"
_MIN_KEY_LEN = 8


def _settings_path() -> str:
    return os.environ.get("WORKSHOP_KIRO_SETTINGS",
                          os.path.join(_RUNS_DIR, "kiro.local.json"))


# The vault names come from the ROLE REGISTRY, not from a second literal here: the
# provisioning side (this module) and the dispatch side (``runtime_exec``) must
# resolve the same workload identity and credential provider, or the key is written
# where the runtime does not look. ``Role.vault_names()`` applies the same
# WORKSHOP_KIRO_* operator overrides these functions always honoured.
def _vault_names() -> tuple[str, str]:
    import roles as _roles
    return _roles.get("kiro").vault_names()


def _provider_name() -> str:
    return _vault_names()[1]


def _workload_name() -> str:
    return _vault_names()[0]


def _region() -> str:
    import boto3
    region = (os.environ.get("WORKSHOP_BEDROCK_REGION")
              or os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION")
              or boto3.Session().region_name)
    if not region:
        raise ValueError("Configure an AWS region before managing the Kiro credential.")
    return region


def _tail(key: str) -> str:
    """The last 4 chars, prefixed, for a masked display. Never the full key."""
    return "…" + key[-4:] if len(key) >= 4 else "…"


# --- status sidecar (NON-secret: provider/region/tail only) -------------------
def _load_sidecar() -> dict:
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_sidecar(data: dict) -> None:
    path = _settings_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --- Token Vault provisioning -------------------------------------------------
def _control_client():
    # Metadata discovery and provisioning both use the deployment region.
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415
    return boto3.client("bedrock-agentcore-control", region_name=_region(),
                        config=Config(connect_timeout=3, read_timeout=10,
                                      retries={"max_attempts": 1}))


def _ensure_workload(client) -> None:
    """The credential provider hangs off a workload identity; make sure it exists
    (idempotent: get, else create). Mirrors kiro/setup.sh."""
    name = _workload_name()
    try:
        client.get_workload_identity(name=name)
    except client.exceptions.ResourceNotFoundException:
        try:
            client.create_workload_identity(name=name)
        except client.exceptions.ConflictException:
            pass


def _provision_vault(api_key: str) -> None:
    """Create or update the kiro-api-key credential provider (idempotent: get ->
    update else create), the exact shape kiro/setup.sh uses over the CLI."""
    client = _control_client()
    _ensure_workload(client)
    name = _provider_name()
    try:
        client.get_api_key_credential_provider(name=name)
    except client.exceptions.ResourceNotFoundException:
        client.create_api_key_credential_provider(name=name, apiKey=api_key)
    else:
        client.update_api_key_credential_provider(name=name, apiKey=api_key)


def save_api_key(api_key: str) -> dict[str, Any]:
    """Store a pasted Kiro API key in the Token Vault and record its status.

    The key shape is validated, the vault provider is created/updated, and a 0600
    sidecar records the provider/region/tail (never the key). Returns status() on
    success or {"error": ...} on a bad key / vault failure (fail loud, never a
    silent half-write)."""
    # A copied key can pick up surrounding quotes or a wrapped line break.
    api_key = "".join((api_key or "").split()).strip("'\"")
    if not api_key:
        # Empty on save = a status-only re-save; keep the stored provider untouched.
        if _load_sidecar().get("stored"):
            return status()
        return {"error": "API key is empty: the hidden prompt received nothing. Paste the "
                         f"key (it starts with {_KEY_PREFIX}) and press Enter; the prompt "
                         "shows no characters while you paste."}
    if not api_key.startswith(_KEY_PREFIX) or len(api_key) < _MIN_KEY_LEN:
        return {"error": f"not a Kiro API key (expected a '{_KEY_PREFIX}...' value; copy the "
                         "key itself, not a username or password)"}
    key_check = _check_with_kiro(api_key)
    if key_check == "rejected":
        # The live event's "bearer token is invalid" surfaced only inside a Runtime
        # session; saving the key is the cheapest moment to catch it.
        return {"error": "Kiro rejected this key (invalid bearer token). Copy the whole key "
                         "again and rerun this step; if it still fails, ask a facilitator "
                         "for a new key. Nothing was saved."}

    if os.environ.get("WORKSHOP_KIRO_DISABLE_VAULT") != "1":
        try:
            _provision_vault(api_key)
        except Exception as exc:  # noqa: BLE001 (surface the real reason)
            return {"error": f"Token Vault write failed: {exc}"}

    _write_sidecar({
        "stored": True,
        "provider": _provider_name(),
        "workload": _workload_name(),
        "region": _region(),
        "key_tail": _tail(api_key),
        "key_check": key_check or "unavailable",
    })
    return status()


def _check_with_kiro(api_key: str) -> str | None:
    """One tiny Kiro request with this key: 'verified', 'rejected', or None if unknown.

    Uses the host's pinned kiro-cli-chat in a throwaway HOME, so no login state, key,
    or history is left behind. Only an explicit authorization failure blocks a save;
    a missing CLI, a timeout, or any other answer never turns into a rejection."""
    if (os.environ.get("WORKSHOP_KIRO_DISABLE_VAULT") == "1"
            or os.environ.get("WORKSHOP_KIRO_VERIFY") == "0"):
        return None
    chat = shutil.which("kiro-cli-chat") or str(Path.home() / ".local/bin/kiro-cli-chat")
    if not Path(chat).is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="kiro-key-check-") as home:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "KIRO_API_KEY": api_key,
               "LANG": os.environ.get("LANG", "C.UTF-8")}
        try:
            done = subprocess.run([chat, "chat", "--no-interactive",
                                   "Reply with exactly: KIRO_KEY_OK"],
                                  env=env, capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.SubprocessError):
            return None
    answer = f"{done.stdout}\n{done.stderr}"
    if "KIRO_KEY_OK" in answer:
        return "verified"
    if re.search(r"bearer token|unauthori[sz]ed|invalid (api )?key|\b40[13]\b", answer, re.I):
        return "rejected"
    return None


def status(*, refresh: bool = False) -> dict[str, Any]:
    """Read credential metadata, including keys provisioned by the event/CLI.

    GetApiKeyCredentialProvider returns provider metadata, not the API key.
    A lookup failure is unknown, never an assertion that no key exists.
    """
    s = _load_sidecar()
    offline = os.environ.get("WORKSHOP_KIRO_DISABLE_VAULT") == "1"
    out: dict[str, Any] = {"connected": None, "source": None,
                           "provider": _provider_name()}
    try:
        out["region"] = _region()
        cache_matches = (s.get("region") == out["region"]
                         and s.get("provider", out["provider"]) == out["provider"])
        if s.get("stored") and cache_matches and (not refresh or offline):
            return {**out, "connected": True, "source": "settings",
                    "key_tail": s.get("key_tail", ""),
                    **({"key_check": s["key_check"]} if s.get("key_check") else {})}
        if offline:
            return {**out, "connected": False}
        client = _control_client()
        try:
            client.get_api_key_credential_provider(name=_provider_name())
        except client.exceptions.ResourceNotFoundException:
            # A confirmed deletion must stay absent after leaving Settings and
            # returning. An older sidecar cannot turn that result green again.
            if cache_matches and s.get("stored"):
                _write_sidecar({**s, "stored": False, "key_tail": ""})
            return {**out, "connected": False}
        return {**out, "connected": True, "source": "token-vault"}
    except Exception as exc:  # noqa: BLE001 (metadata unavailable is not unconfigured)
        return {**out, "error": f"Could not verify the Token Vault credential: {exc}"}


def clear_api_key() -> dict[str, Any]:
    """Remove the provider; preserve local status if AWS refuses the deletion."""
    if os.environ.get("WORKSHOP_KIRO_DISABLE_VAULT") != "1":
        try:
            client = _control_client()
            try:
                client.delete_api_key_credential_provider(name=_provider_name())
            except client.exceptions.ResourceNotFoundException:
                pass
        except Exception as exc:  # noqa: BLE001 (never report a failed delete as success)
            return {"error": f"Token Vault deletion failed: {exc}"}
    try:
        os.remove(_settings_path())
    except OSError:
        pass
    return {"connected": False, "source": None, "provider": _provider_name(),
            "region": _region()}
