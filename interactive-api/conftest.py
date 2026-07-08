"""Test isolation for the Stage 1 interactive API.

The deploy reconciliation reads the REAL ``runtime_config.json`` each harness's
``deploy.py`` writes under ``coding-agents/<harness>/`` (the single source
of truth for "deployed on AgentCore Runtime"). On a developer box those files may
exist (the dev's own real deploy), which would leak real deploy state into the unit
suite and make the empty-shelf pedagogy untestable.

So point ``WORKSHOP_CODING_AGENTS_DIR`` at an empty tmp dir for the whole module
BEFORE ``interactive_api`` resolves it. The module reads that env var at call time
by design (real-seam isolation, never a monkeypatch of internals). A test that wants
to simulate a real deploy writes a real-ARN ``runtime_config.json`` into this dir.
"""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_coding_agents_dir(monkeypatch, tmp_path):
    """Each test sees an empty coding-agents dir (no agent 'deployed') unless it
    writes a real runtime_config.json itself, AND a fresh EMPTY /mnt/s3files agent
    home (so a `dev` session starts on a blank mount, never a leaked shared one).
    Both are env vars the code reads at call time (real-seam isolation)."""
    d = tmp_path / "coding-agents"
    d.mkdir()
    monkeypatch.setenv("WORKSHOP_CODING_AGENTS_DIR", str(d))
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(tmp_path / "s3files"))
    yield str(d)
