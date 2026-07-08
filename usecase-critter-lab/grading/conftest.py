"""Test-collection isolation for the Critter Lab grading modules.

This grading folder ships ``contract.py`` and ``adapters.py`` with the SAME basenames as
the sibling ``usecase-sample-to-mcp/grading/`` folder (and the orchestrator engine does a
bare ``from contract import grade`` / ``from adapters import RemoteMCPClient`` at runtime,
relying on those names resolving to the sample-to-mcp versions).

When the WHOLE ``src/`` suite is collected, importing this folder's ``contract``/``adapters``
under those same bare names would poison ``sys.modules`` and make the engine grade against
the wrong contract. To stay a good citizen in a shared interpreter, we load this folder's
modules under UNIQUE names (``critter_contract`` / ``critter_adapters``) and expose them to
the test via importlib; the bare ``contract`` / ``adapters`` names are left untouched for
the engine. Running this folder alone still works exactly the same.
"""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# critter_lab.py lives one level up; adapters.py imports it.
sys.path.insert(0, os.path.dirname(_HERE))


def _load(unique_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(unique_name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


# Loaded under unique names so they never claim the bare ``contract``/``adapters`` slots.
critter_adapters = _load("critter_adapters", "adapters.py")
critter_contract = _load("critter_contract", "contract.py")
