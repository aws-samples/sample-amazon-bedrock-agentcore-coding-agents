"""Drift guard: the attendee's template copy of cost_analyzer.py must match ours.

`cost_analyzer.py` deliberately exists in two places, with two different jobs:

  * HERE (``usecase-sample-to-mcp/cost_analyzer.py``) it is the workshop's
    REFERENCE / answer-key: the deterministic grading floor, the reference MCP
    server, and the efficiency-lab starter agent all ``import cost_analyzer`` from
    this copy, offline, to prove and grade the contract.
  * In the public TEMPLATE repo (``didhd/agentcore-coding-agents-starter``) it is
    the attendee's RAW MATERIAL: they create their own repo from that template and
    seed it onto ``/mnt/s3files`` in Lab 1, and the coding agents convert it.

Because the grading floor scores what the agents build against THIS reference, the
two copies must be byte-identical. If the template drifts (someone edits one and not
the other), an attendee could convert a module the floor never scored against, and
the mismatch would surface as a confusing gate failure. This test catches that drift
at CI time instead.

Network policy (mirrors CLAUDE.md: never add a hard network dependency to the
offline floor): the check FETCHES the template copy over the network, so by default
it SKIPS loudly when the template cannot be reached (offline dev, sandbox). Set
``WORKSHOP_DRIFT_STRICT=1`` (do this in the pre-publish gate) to turn an
unreachable template or a mismatch into a hard FAILURE instead of a skip.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REFERENCE = os.path.join(_HERE, "cost_analyzer.py")

# Where the attendee gets their copy. Wirable so a fork / aws-samples move or a
# private mirror can be pointed at without editing the test.
_TEMPLATE_REPO = os.environ.get("WORKSHOP_TEMPLATE_REPO",
                                "didhd/agentcore-coding-agents-starter")
_TEMPLATE_REF = os.environ.get("WORKSHOP_TEMPLATE_REF", "main")
_RAW_URL = (f"https://raw.githubusercontent.com/{_TEMPLATE_REPO}/"
            f"{_TEMPLATE_REF}/cost_analyzer.py")

_STRICT = os.environ.get("WORKSHOP_DRIFT_STRICT") == "1"


def _fetch_template_module() -> bytes:
    req = urllib.request.Request(_RAW_URL, headers={"User-Agent": "workshop-drift-guard"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def test_template_cost_analyzer_matches_reference():
    """The template's cost_analyzer.py is byte-identical to the reference here."""
    with open(_REFERENCE, "rb") as f:
        reference = f.read()

    try:
        template = _fetch_template_module()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        msg = (f"could not fetch the template module from {_RAW_URL}: {exc}. "
               "Set WORKSHOP_DRIFT_STRICT=1 to make this a hard failure "
               "(the pre-publish gate does).")
        if _STRICT:
            pytest.fail(msg)
        pytest.skip(msg)

    assert template == reference, (
        f"cost_analyzer.py has DRIFTED between the code repo reference "
        f"({_REFERENCE}) and the attendee template ({_TEMPLATE_REPO}@{_TEMPLATE_REF}). "
        "They must be byte-identical so the grading floor scores what attendees "
        "actually convert. Re-sync the template copy from this reference "
        "(or vice versa) and push the template."
    )
