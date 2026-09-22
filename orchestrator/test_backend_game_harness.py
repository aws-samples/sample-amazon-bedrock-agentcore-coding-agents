"""Check the game guidance delivered by image staging and Runtime archives."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import shlex
import tarfile
from types import SimpleNamespace

import harness_config
import roles
import runtime_stage


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "harness-skills/skills/backend-engineering/SKILL.md"


def assert_game_owned_scoring(text):
    for legacy_requirement in (
        "0 to 1000", "GET /api/scores", "POST /api/scores", "host reporter",
    ):
        assert legacy_requirement not in text
    normalized = " ".join(text.split())
    assert "how actual play earns a score" in normalized
    assert "Persist scores across service restarts." in normalized
    assert "API routes and payloads" in normalized
    assert "Preserve existing interfaces and saved data" in normalized
    assert "without comparing scores or ranking teams" in normalized
    assert "completed round and play again" in normalized
    assert "STAY IN THE FOREGROUND" in normalized
    assert "`PORT`" in normalized


def test_actual_backend_image_context_contains_game_owned_scoring(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "backend_game_cli_versions", ROOT / "coding-agents/cli_versions.py",
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    context = tmp_path / "backend-context"
    cli.stage_context(ROOT / "coding-agents/claude-code", context)

    staged = context / "skills/backend-engineering/SKILL.md"
    assert staged.read_bytes() == SKILL.read_bytes()
    copies = [
        shlex.split(line)[-2:]
        for line in (context / "Dockerfile").read_text().splitlines()
        if line.startswith("COPY ")
    ]
    assert ["skills/", "/home/agent/skills/"] in copies
    assert (context / "CLAUDE.md").read_bytes() == (
        ROOT / "orchestrator/harness/claude-code/CLAUDE.md"
    ).read_bytes()
    assert_game_owned_scoring(staged.read_text())


def test_actual_runtime_archive_contains_game_owned_scoring(tmp_path, monkeypatch):
    backend = next(iter(roles.by_capability("backend")))
    steering = Path(harness_config.harness_file(backend))
    setup = harness_config.parse_setup_spec(str(steering))
    declared = [str((steering.parent / path).resolve()) for path in setup["skills"]]
    assert str(SKILL.parent) in declared
    writes = []
    monkeypatch.delenv("WORKSHOP_S3FILES_DIR", raising=False)
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", "offline-backend-game-skill")
    monkeypatch.setattr(
        runtime_stage, "_client",
        lambda region: SimpleNamespace(put_object=lambda **kwargs: writes.append(kwargs)),
    )

    assert runtime_stage.stage_skills(
        "run_backend_game_skill", declared, region="us-east-1", agent_id=backend,
    ) > 0
    assert len(writes) == 1
    assert writes[0]["Key"] == runtime_stage.archive_key(
        runtime_stage.skills_subdir("run_backend_game_skill", backend),
    )
    payload = writes[0]["Body"]
    (tmp_path / "actual-skills.tar.gz").write_bytes(payload)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        delivered = archive.extractfile("skills/backend-engineering/SKILL.md").read()
    assert delivered == SKILL.read_bytes()
    assert_game_owned_scoring(delivered.decode())
