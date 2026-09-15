"""Known submitters are attributed by default; anonymous requests stay anonymous."""
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


def test_known_submitter_is_attributed_without_an_exercise_flag(monkeypatch):
    monkeypatch.delenv("WORKSHOP_LAB3_COMPLETE", raising=False)
    assert IDENT.to_otel_env(), "A signed-in request must not ship without its user label"


def test_to_otel_env_prefers_email_and_falls_back_to_subject():
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
