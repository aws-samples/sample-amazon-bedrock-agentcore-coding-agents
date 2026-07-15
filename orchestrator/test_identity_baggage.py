"""Identity baggage: the run-attribution contract, including the Lab 3 seam.

to_env() is the run-ledger/audit propagation (AGENTCORE_USER_*). to_otel_env()
is the Lab 3 telemetry seam: it SHIPS returning {} (dispatched runs land in
CloudWatch untagged), and attendees implement the mapping in Lab 3 page 2.
These tests pin the shipped contract; the attendee's fix flips
test_to_otel_env_ships_empty red, which is exactly the observable change the
lab asks them to verify (the content tells them to re-run this file and
expect that one failure).
"""
from __future__ import annotations

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
    # The Lab 3 gap, pinned: no OTel identity leaves the dispatch until the
    # attendee implements the mapping. If you just implemented it and this
    # fails, that is the lab working as intended; the companion test below
    # defines what the finished mapping must satisfy.
    assert IDENT.to_otel_env() == {}


def test_to_otel_env_contract_once_implemented():
    # Contract for the attendee's implementation (reference version is in the
    # to_otel_env docstring). An empty dict (the shipped state) passes
    # vacuously; a non-empty result must be a well-formed OTel resource stamp
    # that names the submitting user.
    out = IDENT.to_otel_env()
    for key, value in out.items():
        assert key == "OTEL_RESOURCE_ATTRIBUTES"
        assert "user.id=" in value
        assert "attendee@workshop.aws" in value or "c0ffee-sub" in value
        # W3C baggage-style k=v pairs, comma separated, no spaces around '='
        for pair in value.split(","):
            k, _, v = pair.partition("=")
            assert k and v, f"malformed resource attribute pair: {pair!r}"


def test_anonymous_identity_never_stamps_telemetry():
    assert ANONYMOUS.is_anonymous()
    assert ANONYMOUS.to_otel_env() == {} or (
        "user.id=" not in ANONYMOUS.to_otel_env().get(
            "OTEL_RESOURCE_ATTRIBUTES", "user.id="))


def test_contextvar_roundtrip():
    set_current_identity(IDENT)
    got = get_current_identity()
    assert got.email == "attendee@workshop.aws"
    assert not got.is_anonymous()
