"""Inspectable host controls and a policy preview that never executes actions."""

from __future__ import annotations

import hashlib
import time
from typing import Any

import metrics_lib


class ControlsError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def configuration(user_identity: dict | None = None) -> dict[str, Any]:
    """Read the same limits, merge setting, roster, and mapping used by dispatch."""
    import engine
    import github
    import reviewer
    import roles
    from identity_baggage import UserIdentity

    identity = UserIdentity.from_dict(user_identity or {})
    try:
        environment = identity.to_otel_env()
        attributes = environment.get("OTEL_RESOURCE_ATTRIBUTES") or None
        if attributes is not None and not isinstance(attributes, str):
            raise TypeError
    except (AttributeError, TypeError, ValueError):
        raise ControlsError("The host's identity mapping did not return a valid environment.", 503) from None
    return {
        "source": "host-configuration",
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "identity": {
            "known": bool(identity.user_id),
            "user_id": identity.user_id or None,
            "email": identity.email or None,
            "name": identity.name or None,
            "mapping_state": ("anonymous" if not identity.user_id
                              else "present" if attributes else "empty"),
            "telemetry_attributes": attributes,
        },
        "merge_policy": github.merge_policy(),
        "limits": {
            "repairs_per_pr": reviewer.MAX_REVIEW_ROUNDS,
            "gate_timeout_seconds": reviewer.GATE_TIMEOUT_S,
            "role_timeout_seconds": engine.HARNESS_ROLE_TIMEOUT_S,
        },
        "roles": [
            {"id": role.id, "label": role.label, "kind": role.kind,
             "capability": role.capability, "role_name": role.role_name}
            for role in roles.roster()
        ],
    }


def evaluate(body: Any, user_identity: dict | None = None) -> dict[str, Any]:
    """Evaluate the real policy as data. No tool, shell, or model is invoked."""
    if not isinstance(body, dict) or set(body) - {"action", "target", "read_only"}:
        raise ControlsError("Supply only action, target, and read_only.")
    action = body.get("action", "run_command")
    if action not in ("run_command", "write_file", "read_file"):
        raise ControlsError("Choose run_command, write_file, or read_file.")
    target = body.get("target")
    try:
        valid_target = (isinstance(target, str) and bool(target.strip())
                        and "\0" not in target and len(target.encode("utf-8")) <= 2048)
    except UnicodeError:
        valid_target = False
    if not valid_target:
        raise ControlsError("Supply a non-empty target of at most 2048 UTF-8 bytes.")
    read_only = body.get("read_only", False)
    if type(read_only) is not bool:
        raise ControlsError("read_only must be a boolean.")
    if metrics_lib._policy is None:
        raise ControlsError("The host policy module is unavailable.", 503)

    decision = metrics_lib._policy.screen(action, target, read_only=read_only)
    outcome = "allow" if decision.allowed else "hold" if decision.gated else "deny"
    result = {
        **decision.public(),
        "outcome": outcome,
        "action": action,
        "read_only": read_only,
        "executed": False,
        "source": "policy-preview",
    }
    # Commands can contain secrets. Record the decision and a content hash,
    # never the command or file contents entered into the preview.
    audit = metrics_lib.record_governance_event(
        "policy_evaluation", user_identity, {
            "action": action, "outcome": outcome, "rule_id": decision.rule_id,
            "tier": decision.tier, "read_only": read_only, "executed": False,
            "target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        })
    return {**result, **audit}
