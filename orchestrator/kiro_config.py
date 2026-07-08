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
  WORKSHOP_BEDROCK_REGION      region for the control-plane calls (default AWS_REGION/us-west-2)
  WORKSHOP_KIRO_DISABLE_VAULT  "1" to skip the boto3 vault call (offline unit tests):
                               the key is validated + the sidecar written, no AWS.
"""
from __future__ import annotations

import json
import os
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


def _provider_name() -> str:
    return os.environ.get("WORKSHOP_KIRO_PROVIDER", "kiro-api-key")


def _workload_name() -> str:
    return os.environ.get("WORKSHOP_KIRO_WORKLOAD", "kiro-coding-agent")


def _region() -> str:
    return os.environ.get("WORKSHOP_BEDROCK_REGION",
                          os.environ.get("AWS_REGION", "us-west-2"))


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
    os.makedirs(_RUNS_DIR, exist_ok=True)
    try:
        os.chmod(_RUNS_DIR, 0o700)
    except OSError:
        pass
    path = _settings_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --- Token Vault provisioning -------------------------------------------------
def _control_client():
    # boto3 stays optional: imported only here, only when actually provisioning.
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
    except Exception:  # noqa: BLE001 (ResourceNotFound or first run -> create)
        try:
            client.create_workload_identity(name=name)
        except Exception:  # noqa: BLE001 (a race that created it is fine)
            pass


def _provision_vault(api_key: str) -> None:
    """Create or update the kiro-api-key credential provider (idempotent: get ->
    update else create), the exact shape kiro/setup.sh uses over the CLI."""
    client = _control_client()
    _ensure_workload(client)
    name = _provider_name()
    try:
        client.get_api_key_credential_provider(name=name)
        client.update_api_key_credential_provider(name=name, apiKey=api_key)
    except Exception:  # noqa: BLE001 (not found -> create)
        client.create_api_key_credential_provider(name=name, apiKey=api_key)


def save_api_key(api_key: str) -> dict[str, Any]:
    """Store a pasted Kiro API key in the Token Vault and record its status.

    The key shape is validated, the vault provider is created/updated, and a 0600
    sidecar records the provider/region/tail (never the key). Returns status() on
    success or {"error": ...} on a bad key / vault failure (fail loud, never a
    silent half-write)."""
    api_key = (api_key or "").strip()
    if not api_key:
        # Empty on save = a status-only re-save; keep the stored provider untouched.
        if _load_sidecar().get("stored"):
            return status()
        return {"error": "API key is empty"}
    if not api_key.startswith(_KEY_PREFIX) or len(api_key) < _MIN_KEY_LEN:
        return {"error": f"not a Kiro API key (expected a '{_KEY_PREFIX}...' value)"}

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
    })
    return status()


def status() -> dict[str, Any]:
    """Connection status for the Settings card: connected + a masked tail, never
    the key. ``source`` is 'settings' once provisioned from the console."""
    s = _load_sidecar()
    if s.get("stored"):
        return {
            "connected": True,
            "source": "settings",
            "provider": s.get("provider", _provider_name()),
            "region": s.get("region", _region()),
            "key_tail": s.get("key_tail", ""),
        }
    return {"connected": False, "source": None, "provider": _provider_name(),
            "region": _region()}


def clear_api_key() -> dict[str, Any]:
    """Disconnect: delete the vault provider (best-effort) and drop the sidecar."""
    if os.environ.get("WORKSHOP_KIRO_DISABLE_VAULT") != "1":
        try:
            _control_client().delete_api_key_credential_provider(name=_provider_name())
        except Exception:  # noqa: BLE001 (already gone / no creds: still clear local)
            pass
    try:
        os.remove(_settings_path())
    except OSError:
        pass
    return status()
