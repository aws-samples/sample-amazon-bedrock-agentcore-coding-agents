"""The REST dispatch path must carry the signed-in user, not just Chat.

Lab 3 attributes cost by `resource.user.id`, which the dispatch stamps from
`identity.to_otel_env()`. That only fires when the engine context holds a
non-anonymous identity. `chat_stream()` always threaded it; `dispatch()` did
NOT, so every run created over `POST /api/runs` (the console's REST path) was
admitted anonymous and its telemetry reached CloudWatch with `run.id` and
`agent.id` but no `user.id` -- the whole fleet collapsing into the UNTAGGED
group in the page-3 query, no matter how correctly the attendee implemented the
seam.

Each case runs in a COPIED context so a set inside one test cannot leak into
another (or into the ambient context) and mask a regression.

    python3 -m pytest orchestrator/test_rest_identity_threading.py -q
"""

from __future__ import annotations

import contextvars
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import connection_api  # noqa: E402
from identity_baggage import UserIdentity, get_current_identity, set_current_identity  # noqa: E402

BAGGAGE = {
    "user_id": "sub-123",
    "user_email": "attendee@workshop.aws",
    "user_name": "Attendee",
}


def _identity_after(*dispatch_args):
    """Run one dispatch in a fresh context and return the identity it left.

    The context is explicitly reset to anonymous first, so this asserts what THIS
    dispatch did rather than inheriting an identity another test left in the
    ambient context (which made the anonymous case pass or fail by test order).
    """
    def go():
        set_current_identity(UserIdentity())
        connection_api.dispatch(*dispatch_args)
        return get_current_identity()
    return contextvars.copy_context().run(go)


def test_rest_dispatch_threads_the_signed_in_user():
    ident = _identity_after("GET", "/api/health", None, "", BAGGAGE)
    assert not ident.is_anonymous()
    assert ident.email == "attendee@workshop.aws"


def test_rest_dispatch_without_identity_stays_anonymous():
    # The anonymous guard must hold: an unauthenticated call is never stamped
    # with an empty user.id, it simply stays untagged.
    ident = _identity_after("GET", "/api/health", None)
    assert ident.is_anonymous()


def test_dispatch_keeps_working_for_existing_callers():
    # Back-compat: the 3-arg and 4-arg forms predate the identity parameter.
    code, _ = connection_api.dispatch("GET", "/api/health", None)
    assert code == 200
    code, _ = connection_api.dispatch("GET", "/api/health", None, "")
    assert code == 200
