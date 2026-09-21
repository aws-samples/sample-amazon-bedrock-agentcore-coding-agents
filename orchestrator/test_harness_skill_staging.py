"""Declared builder skills must reach the real installer and Runtime archive."""
from __future__ import annotations

import io
from pathlib import Path
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness_config
import roles
import runtime_stage
from engine import Engine, TERMINAL
from fixture_executor import FixtureExecutor


BUILDERS = roles.builders()
SKILL_BY_CAPABILITY = {
    "backend": "backend-engineering",
    "frontend": "frontend-design",
}


def expected_skill(role):
    name = SKILL_BY_CAPABILITY[role.capability]
    return HERE.parent / "harness-skills" / "skills" / name


@pytest.mark.parametrize("role", BUILDERS, ids=lambda role: role.id)
def test_served_builder_receives_its_real_skill_before_producing(role):
    """Exercise Engine's installer, not a copy of its setup/cp implementation."""
    observed = {}

    class InspectingProducer(FixtureExecutor):
        def produce(self, run, agent_id, result):
            if agent_id == role.id:
                workdir = Path(run.roledir(agent_id))
                observed.update({
                    path.relative_to(workdir).as_posix(): path.read_bytes()
                    for path in (workdir / "skills").rglob("*") if path.is_file()
                })
            return super().produce(run, agent_id, result)

    engine = Engine(executor_obj=InspectingProducer())
    try:
        run = engine.submit(
            "Exercise the builder setup boundary with an offline producer.",
            [role.id, *roles.checker_ids()],
        )
        deadline = time.monotonic() + 30
        while run.status not in TERMINAL:
            assert time.monotonic() < deadline, (run.status, run.phase)
            time.sleep(0.05)
        source = expected_skill(role) / "SKILL.md"
        staged = f"skills/{source.parent.name}/SKILL.md"
        assert observed.get(staged) == source.read_bytes(), (
            f"{role.id} started without its actual {staged}; observed {sorted(observed)}"
        )
    finally:
        engine.shutdown()


@pytest.mark.parametrize("role", BUILDERS, ids=lambda role: role.id)
def test_parsed_builder_setup_stages_exact_skill_bytes_in_runtime_archive(role, monkeypatch):
    """Use the production parser and packer; only the S3 transport is replaced."""
    steering = Path(harness_config.harness_file(role.id))
    setup = harness_config.parse_setup_spec(str(steering))
    declared = [str((steering.parent / path).resolve()) for path in setup["skills"]]
    writes = []
    monkeypatch.delenv("WORKSHOP_S3FILES_DIR", raising=False)
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", "offline-skill-stage")
    monkeypatch.setattr(
        runtime_stage, "_client",
        lambda region: SimpleNamespace(put_object=lambda **kwargs: writes.append(kwargs)),
    )

    count = runtime_stage.stage_skills(
        "run_skill_staging", declared, region="us-east-1", agent_id=role.id,
    )

    assert count > 0, f"{role.id} declared no usable skills"
    assert len(writes) == 1
    assert writes[0]["Key"] == runtime_stage.archive_key(
        runtime_stage.skills_subdir("run_skill_staging", role.id)
    )
    with tarfile.open(fileobj=io.BytesIO(writes[0]["Body"]), mode="r:gz") as archive:
        source = expected_skill(role)
        for path in source.rglob("*"):
            if path.is_file():
                archived = f"skills/{source.name}/{path.relative_to(source).as_posix()}"
                assert archive.extractfile(archived).read() == path.read_bytes()


def test_codex_image_and_coordinator_ship_the_same_guidance():
    assert (HERE / "harness/codex/AGENTS.md").read_bytes() == (
        HERE.parent / "coding-agents/codex/AGENTS.md"
    ).read_bytes()
