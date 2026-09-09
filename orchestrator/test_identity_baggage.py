"""Identity propagation and the intentionally empty Lab 3 exercise seam.

The default suite verifies the shipped empty mapping. WORKSHOP_LAB3_COMPLETE=1
requires the attendee's saved implementation, including email preference, user-ID
fallback and anonymous behavior. It must fail if the edit is still empty.
"""
from __future__ import annotations

import os

from identity_baggage import (
    ANONYMOUS,
    UserIdentity,
    get_current_identity,
    set_current_identity,
)

IDENT = UserIdentity(user_id="c0ffee-sub", email="attendee@workshop.aws",
                     name="Attendee")


def test_to_env_carries_the_full_attribution_triplet():
    env = IDENT.to_env()
    assert env == {
        "AGENTCORE_USER_ID": "c0ffee-sub",
        "AGENTCORE_USER_EMAIL": "attendee@workshop.aws",
        "AGENTCORE_USER_NAME": "Attendee",
    }


def test_to_otel_env_ships_empty():
    # CI pins the shipped Lab 3 gap. After the attendee implements the mapping,
    # completion mode switches this same test to the finished contract so a
    # correct workshop edit produces a green suite, not an intentional red.
    out = IDENT.to_otel_env()
    if os.environ.get("WORKSHOP_LAB3_COMPLETE") == "1":
        assert out, "Lab 3 completion mode requires a non-empty OTel identity stamp"
    else:
        assert out == {}


def test_to_otel_env_contract_once_implemented():
    # The shipped gap is intentional; completion mode is checked independently
    # above. Once implemented, both identity sources must retain their meaning.
    if not IDENT.to_otel_env():
        return
    assert IDENT.to_otel_env() == {
        "OTEL_RESOURCE_ATTRIBUTES": "user.id=attendee@workshop.aws,team.id=workshop",
    }
    assert UserIdentity(user_id="c0ffee-sub").to_otel_env() == {
        "OTEL_RESOURCE_ATTRIBUTES": "user.id=c0ffee-sub,team.id=workshop",
    }


def test_anonymous_identity_never_stamps_telemetry():
    assert ANONYMOUS.is_anonymous()
    assert ANONYMOUS.to_otel_env() == {}


def test_contextvar_roundtrip():
    set_current_identity(IDENT)
    got = get_current_identity()
    assert got.email == "attendee@workshop.aws"
    assert not got.is_anonymous()
