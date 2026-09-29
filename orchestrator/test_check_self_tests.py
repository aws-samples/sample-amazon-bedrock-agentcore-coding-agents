"""The checker may run its own parser controls before handing the check off.

On 2026-09-28 and 29, Kiro checks failed in the real gate on their own code, not on the
game: 6 of 23 checks failed a hand-written HTML parser's self-test, and live Lab 2 and
Lab 3 builds crashed on `spawnSync` with a fractional timeout (95992.8). Both stopped the
build for a person although the game worked. Letting the checker run ONLY those controls
(`WORKSHOP_CHECK_SELFTEST=1`, no deliverable) and asking for whole-number timeouts
removed the self-test failures in a six-build trial. The acceptance behavior still runs
once, in the engine. These tests pin the wording in both channels the checker reads.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEERING = ROOT / "orchestrator/harness/kiro/.kiro/steering/validator.md"
BAKED = ROOT / "coding-agents/kiro/steering/agent.md"
ENGINE = (ROOT / "orchestrator/engine.py").read_text()


def test_steering_offers_a_controls_only_mode_and_whole_number_timeouts():
    text = STEERING.read_text()
    assert "WORKSHOP_CHECK_SELFTEST=1" in text
    assert "without starting or\n  reading the deliverable" in text
    assert "whole numbers to process and network timeouts" in text
    assert BAKED.read_text() == text


def test_prompt_repeats_both_rules_and_keeps_the_single_real_execution():
    assert "WORKSHOP_CHECK_SELFTEST=1" in ENGINE
    assert "numbers to every process and network timeout" in ENGINE
    assert "not run the acceptance behavior; the engine owns that one real execution." in ENGINE
