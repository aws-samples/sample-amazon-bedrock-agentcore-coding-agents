"""The checker exercises its own controls and harness before handing the check off.

On 2026-09-28 and 29, Kiro checks failed in the real gate on their own code, not on the
game: 6 of 23 checks failed a hand-written HTML parser's self-test, and live Lab 2 and
Lab 3 builds crashed on `spawnSync` with a fractional timeout (95992.8). Both stopped the
build for a person although the game worked. Letting the checker run ONLY those controls
(`WORKSHOP_CHECK_SELFTEST=1`, no deliverable) and asking for whole-number timeouts
removed the self-test failures in a six-build trial. The acceptance behavior still runs
once, in the engine.

On 2026-10-27 and 29 (grant event c27c50a7, PR #4) Kiro's frontend checks crashed in
their OWN harness after many passing lines: first on a replaced timing API, then on a
test double assigned to a sandbox built-in through the object the context was created
from. Their self-test mode covered only the parser, so it passed. The rule is now that
the self-test drives every helper the check defines, on the check's own trivial input,
and still never reads or starts the deliverable. These tests pin the wording in every
channel a checker reads.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEERING = ROOT / "orchestrator/harness/kiro/.kiro/steering/validator.md"
BAKED = ROOT / "coding-agents/kiro/steering/agent.md"
CLAUDE_STEERING = ROOT / "orchestrator/harness/claude-code-validator/CLAUDE.md"
CLAUDE_BAKED = ROOT / "coding-agents/claude-code-validator/CLAUDE.md"
SKILLS = [ROOT / "harness-skills/skills/configure-kiro-validator/SKILL.md",
          ROOT / "harness-skills/skills/configure-claude-code-validator/SKILL.md"]
ENGINE = (ROOT / "orchestrator/engine.py").read_text()


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_steering_offers_a_controls_only_mode_and_whole_number_timeouts():
    for path, baked in ((STEERING, BAKED), (CLAUDE_STEERING, CLAUDE_BAKED)):
        text = path.read_text()
        flat = _flat(text)
        assert "WORKSHOP_CHECK_SELFTEST=1" in text, path
        assert "without starting or reading the deliverable" in flat, path
        assert baked.read_text() == text, f"{baked} must match {path}"
    assert "whole numbers to process and network timeouts" in STEERING.read_text()


def test_the_self_test_covers_every_helper_but_never_the_deliverable():
    """The class of mistake, not one line of code: sandbox built-ins, replaced globals
    and timing APIs are the checker's own harness, and a crash there is its defect."""
    for path in (STEERING, CLAUDE_STEERING):
        flat = _flat(path.read_text())
        assert "Exercise your own harness in that mode too." in flat, path
        for kind in ("DOM or sandbox fixture", "replaced global", "timer", "random source"):
            assert kind in flat, (path, kind)
        assert "built-ins are not properties" in flat, path
        assert "timing API" in flat, path
        assert "Never load the deliverable's files in this mode." in flat, path
        assert "the check's own defect, not the work's" in flat, path
        assert "no builder can repair it" in flat, path
        assert "the orchestrator owns that one real execution" in flat, path
    for skill in SKILLS:
        flat = _flat(skill.read_text())
        assert "drives every helper it defines" in flat, skill
        assert "without reading or starting the deliverable" in flat, skill


def test_prompt_repeats_both_rules_and_keeps_the_single_real_execution():
    flat = _flat(ENGINE.replace('"\n', "").replace('            "', ""))
    assert "WORKSHOP_CHECK_SELFTEST=1" in ENGINE
    assert "numbers to every process and network timeout" in ENGINE
    assert "drives EVERY such helper once on a trivial input" in flat
    assert "without reading or starting the deliverable" in flat
    assert "no builder can repair it" in flat
    assert "not run the acceptance behavior; the engine owns that one real execution." in ENGINE
