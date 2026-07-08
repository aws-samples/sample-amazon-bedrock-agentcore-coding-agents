"""Suite-wide test isolation: the first conftest pytest imports at repo root.

The orchestrator's GitHub finalization and runtime-ARN config read gitignored state
under `.runs/` (a developer's REAL wired PAT, real runtime ARNs). `github.py` and
`runtime_config.py` capture their credential/config file path from an env var AT
MODULE IMPORT (real-seam isolation). Per-package conftests
(orchestrator/, e2e/) set those env vars, but they only run for tests on their own
path: a module in ANOTHER package (e.g. orchestrator-agent/test_orchestrator_agent,
or an e2e module that drives the engine in-process) could import `github` BEFORE any
isolating conftest ran, capture the REAL credential, and open a REAL pull request.

This rootdir conftest closes that hole once for the whole suite: pytest imports the
conftest at the top of the collected tree FIRST, before any package conftest or test
module. Setting the isolation env here (module level) redirects every `github` /
`runtime_config` import to a throwaway tmp location no matter the collection order,
so no test can ever read the dev's real token and open a real PR, or read a real
wired runtime ARN. This is real-seam isolation (the modules read these env vars by
design), never a monkeypatch of internals.

A test that WANTS a credential set (e.g. the connected-rung github tests) sets it
itself via monkeypatch, which wins over this default.
"""

import os
import tempfile

_ISOLATED = tempfile.mkdtemp(prefix="solution-test-isolate-")
# Credential store -> empty tmp file (no developer PAT on the ladder). Runtime ARN
# config -> empty tmp file (no real wired runtime). Both read these env vars at
# import by design. setdefault so a more specific package conftest / a test that
# already pinned its own path keeps it.
os.environ.setdefault("WORKSHOP_GITHUB_SETTINGS",
                      os.path.join(_ISOLATED, "github.local.json"))
os.environ.setdefault("WORKSHOP_RUNTIME_CONFIG",
                      os.path.join(_ISOLATED, "runtime.local.json"))
# Gateway auto-discovery -> empty tmp file, so no test picks up a REAL deployed
# gateway's .deployed-state.json (which, with a repo on the ladder, would open a
# real PR). Same real-seam isolation as the two above.
os.environ.setdefault("WORKSHOP_GATEWAY_STATE",
                      os.path.join(_ISOLATED, "gateway-state.json"))
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_REPO", None)
os.environ.pop("GITHUB_GATEWAY_URL", None)
