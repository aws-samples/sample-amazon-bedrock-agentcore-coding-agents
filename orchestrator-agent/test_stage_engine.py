"""The coordinator image must carry what the engine reads at dispatch: the steering
AND the skills the steering names.

Live defect: `stage_engine` shipped orchestrator/ and orchestrator/harness/ but not
harness-skills/, so inside the container every steering file's relative skill path
(`../../../harness-skills/skills/<name>`) resolved to a directory that did not exist. The
engine wrote `skill path not found` into the role's terminal lane and the builder fell
back to the copy baked into its own image, which means an edit to a SKILL.md never
reached a served build. This test resolves the skill path FROM THE STAGED STEERING, the
way the engine does, instead of asserting a directory listing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "orchestrator"))

import stage_engine  # noqa: E402
import harness_config  # noqa: E402
import roles  # noqa: E402


def test_every_served_roles_skill_resolves_from_the_staged_steering():
    copied = stage_engine.stage()
    assert "harness/" in copied and "harness-skills/" in copied
    checked = 0
    for role in roles.roster():
        # The SAME path the engine reads at context hydration, relocated from the
        # source tree to the staged copy the image is built from.
        source = harness_config.harness_file(role.id)
        rel = os.path.relpath(source, stage_engine._SRC)
        steering = os.path.join(stage_engine._DST, rel)
        assert os.path.isfile(steering), f"{role.id}: staged steering missing at {steering}"
        for rel in harness_config.parse_setup_spec(steering)["skills"]:
            full = os.path.normpath(os.path.join(os.path.dirname(steering), rel))
            assert os.path.isdir(full), (
                f"{role.id}: the staged steering names skill {rel!r}, which resolves to "
                f"{full} inside the coordinator image, and it is not there; the builder "
                "would silently fall back to its baked copy")
            assert os.path.isfile(os.path.join(full, "SKILL.md"))
            checked += 1
    assert checked >= 1, "no served role names a skill; the seam this test guards is gone"
