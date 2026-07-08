"""Test-only console entrypoint: the SAME FastAPI app, deterministic engine.

THIS FILE IS TEST-ONLY. The shipped console launcher is ``server.py``, which runs
the real-only Stage-2 engine (``AgentCoreExecutor``: dispatch to deployed runtimes,
fail loud on a missing wired ARN). The e2e suite boots the console as a SUBPROCESS,
so it cannot pass a constructor arg into the engine, and the shipped binary must
stay real-only with no env flag selecting a fake. So the e2e suite launches THIS
entrypoint instead: it imports the real ``server.app`` unchanged, then REBINDS the
Stage-2 engine to one driven by the test-only ``FixtureExecutor`` (deterministic
builders, no model, no live AWS) BEFORE serving.

``connection_api.dispatch`` reads the module-global ``ENGINE`` at call time, so
rebinding ``connection_api.ENGINE`` here makes every Stage-2 request the app serves
run on the fixture engine: exactly the deterministic pytest acceptance gate the
workshop grades with. Stage 1 (interactive) and Stage 3 (metrics) are untouched.

    python3 console/test_server.py    # uvicorn, fixture-backed Stage 2

No shipped module imports this file; it lives next to ``server.py`` only so it can
reuse the exact same app object.
"""

from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))

# GitHub + runtime isolation: this is a TEST entrypoint, so it must NEVER read a
# developer's real wired PAT (.runs/github.local.json) and open a REAL pull request,
# nor read real wired runtime ARNs. github.py / runtime_config.py capture these paths
# at MODULE IMPORT, so set them BEFORE importing connection_api (which imports both).
# setdefault so a caller (e.g. the e2e conftest) that already isolated them wins.
# Real-seam isolation (the modules read these env vars by design), never a monkeypatch.
_ISO = tempfile.mkdtemp(prefix="test-server-iso-")
os.environ.setdefault("WORKSHOP_GITHUB_STORE", "local")
os.environ.setdefault("WORKSHOP_GITHUB_SETTINGS", os.path.join(_ISO, "github.local.json"))
os.environ.setdefault("WORKSHOP_RUNTIME_CONFIG", os.path.join(_ISO, "runtime.local.json"))
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_REPO", None)

# Import the REAL app and the Stage-2 module unchanged.
import connection_api  # noqa: E402
import server  # noqa: E402
from engine import Engine  # noqa: E402
from fixture_executor import FixtureExecutor  # noqa: E402

# Rebind the Stage-2 engine to the deterministic fixture-backed one. dispatch()
# reads connection_api.ENGINE at call time, so this is the only seam needed.
connection_api.ENGINE = Engine(executor_obj=FixtureExecutor())

# The app the e2e suite drives over HTTP is the production app: same routes,
# same login wall, same Stage 1/3, with only the Stage-2 producer swapped.
app = server.app


def main() -> None:
    import uvicorn  # noqa: PLC0415

    uvicorn.run(app, host=server.HOST, port=server.PORT, log_level="warning")


if __name__ == "__main__":
    main()
