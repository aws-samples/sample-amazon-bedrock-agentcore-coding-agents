"""Identity baggage: propagate the authenticated user through the agent chain.

The "baggage" is the Cognito-authenticated user's identity, threaded from the
console login → orchestrator dispatch → runtime execution → governance audit.
Every layer reads and forwards it; Stage 3 uses it for per-user cost attribution
and audit. It does not prove OAuth token exchange or GitHub authorship.

The baggage is a dict:
  {"user_id": "cognito-sub", "user_email": "user@corp.com", "user_name": "Alice"}

Propagation path:
  1. Console: extracts from Cognito session, attaches to orchestrator call
  2. Orchestrator: threads through Engine → Executor → runtime dispatch headers
  3. Runtime: receives the metadata as AGENTCORE_USER_* environment variables
  4. Governance: reads from run metadata for per-user cost, session attribution
"""
from __future__ import annotations

import os
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field

_identity: ContextVar[dict[str, str]] = ContextVar("identity_baggage", default={})


@dataclass
class UserIdentity:
    user_id: str = ""
    email: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "user_email": self.email,
            "user_name": self.name,
        }

    def to_env(self) -> dict[str, str]:
        return {
            "AGENTCORE_USER_ID": self.user_id,
            "AGENTCORE_USER_EMAIL": self.email,
            "AGENTCORE_USER_NAME": self.name,
        }

    def to_otel_env(self) -> dict[str, str]:
        """OpenTelemetry identity for the dispatched agent process.

        Lab 3 seam: the dispatch already knows WHO submitted the run (this
        object) and every agent image already runs a collector sidecar on
        127.0.0.1:4318, but the two are not connected: nothing tells the agent
        CLI to emit telemetry, and nothing stamps the run's telemetry with the
        submitting user. This method is where the identity becomes an OTel
        resource attribute. Attendees complete it in Lab 3; until then it
        returns {} and dispatched runs land in CloudWatch UNTAGGED.

        The finished mapping (the Lab 3 reference implementation). The
        anonymous guard matters: a run with no signed-in user must stay
        unstamped, never stamped as "user.id=" with an empty value:

            ident = self.email or self.user_id
            if not ident:
                return {}
            return {
                "OTEL_RESOURCE_ATTRIBUTES": (
                    f"user.id={ident},team.id=workshop"),
            }
        """
        return {}

    def to_headers(self) -> dict[str, str]:
        return {
            "X-AgentCore-User-Id": self.user_id,
            "X-AgentCore-User-Email": self.email,
            "X-AgentCore-User-Name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "UserIdentity":
        return cls(
            user_id=d.get("user_id", ""),
            email=d.get("user_email", ""),
            name=d.get("user_name", ""),
        )

    @classmethod
    def from_env(cls) -> "UserIdentity":
        return cls(
            user_id=os.environ.get("AGENTCORE_USER_ID", ""),
            email=os.environ.get("AGENTCORE_USER_EMAIL", ""),
            name=os.environ.get("AGENTCORE_USER_NAME", ""),
        )

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "UserIdentity":
        return cls(
            user_id=headers.get("X-AgentCore-User-Id", headers.get("x-agentcore-user-id", "")),
            email=headers.get("X-AgentCore-User-Email", headers.get("x-agentcore-user-email", "")),
            name=headers.get("X-AgentCore-User-Name", headers.get("x-agentcore-user-name", "")),
        )

    def is_anonymous(self) -> bool:
        return not self.user_id


ANONYMOUS = UserIdentity()


def set_current_identity(identity: UserIdentity) -> None:
    _identity.set(identity.to_dict())


def get_current_identity() -> UserIdentity:
    d = _identity.get()
    if not d:
        return ANONYMOUS
    return UserIdentity.from_dict(d)
