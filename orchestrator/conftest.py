"""Test isolation for the orchestrator suite.

The orchestrator's GitHub finalization and runtime-ARN config read gitignored
state under ``.runs/`` (a developer's real PAT, real wired runtime ARNs). Tests
must NEVER touch those: a local-mode run with a real credential on the ladder
would open a REAL pull request, and a wired ARN would make "no runtime" tests
fail. pytest imports this conftest before any test module, so setting these env
vars here (module level) redirects every path to a throwaway tmp location BEFORE
``github`` / ``runtime_config`` are imported and capture their module-level
constants.

This is real-seam isolation (the modules read these env vars by design), never a
monkeypatch of internals.
"""

import os
import tempfile

# Neutralize any REAL GitHub credential / wired runtime ARN so the suite never
# opens a real PR or dispatches to a real runtime. We DON'T relocate the whole
# runs dir (tests that exercise compose/PR manage their own .runs and mock the
# wire), only:
#   * point the GitHub credential store at an empty tmp file (no developer PAT on
#     the ladder), and the runtime config likewise; both read these env vars by
#     design (real-seam isolation, not an internal monkeypatch);
#   * clear any GITHUB_TOKEN/REPO from the environment.
# Tests that WANT a credential set it themselves via monkeypatch, which wins.
_ISOLATED = tempfile.mkdtemp(prefix="orch-test-isolate-")
os.environ.setdefault("WORKSHOP_GITHUB_SETTINGS", os.path.join(_ISOLATED, "github.local.json"))
os.environ["WORKSHOP_RUNTIME_CONFIG"] = os.path.join(_ISOLATED, "runtime.local.json")
# Point gateway auto-discovery at a nonexistent tmp file so the suite never picks
# up a REAL deployed gateway's .deployed-state.json (which, with a repo on the
# ladder, would open a real PR). Same real-seam isolation as the two above.
os.environ.setdefault("WORKSHOP_GATEWAY_STATE", os.path.join(_ISOLATED, "gateway-state.json"))
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_REPO", None)
os.environ.pop("GITHUB_GATEWAY_URL", None)
