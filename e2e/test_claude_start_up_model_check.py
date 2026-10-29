"""Claude Code's start-up model check must not call a model the workshop does not use.

Claude Code 2.1.304 checks each model tier whose alias is not pinned to a model of
that tier's family. In grant event c27c50a7 (October 29) every Runtime session
therefore called Haiku 4.5 (Marketplace 403) and Haiku 3.5 (404), and an unpinned
Opus tier probed `us.anthropic.claude-opus-5-5`. The images pin the aliases and map
the built-in Haiku 4.5 default with `modelOverrides`, which skips that tier's check.
The override value must not equal any configured id: Claude Code maps an override
value back to its key, so an equal value made Sonnet 5.5 requests carry Haiku 4.5's
thinking type and fail once before a retry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ["claude-code", "claude-code-validator"]
HAIKU_DEFAULT = "claude-haiku-4-5-20251001"


def _settings(image: str) -> dict:
    return json.loads((ROOT / "coding-agents" / image / "settings.json").read_text())


@pytest.mark.parametrize("image", IMAGES)
def test_every_model_alias_is_pinned(image):
    env = _settings(image).get("env", {})
    assert "claude-sonnet-5-5" in env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
    assert "claude-opus-5-5" in env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
    assert env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"), "the Haiku tier needs a model"


@pytest.mark.parametrize("image", IMAGES)
def test_the_builtin_haiku_default_is_overridden_with_a_distinct_id(image):
    cfg = _settings(image)
    override = cfg.get("modelOverrides", {}).get(HAIKU_DEFAULT)
    assert override, "without it Claude Code checks Haiku 4.5 on every start"
    assert "haiku" not in override
    configured = {cfg.get("model"), *cfg.get("env", {}).values()}
    assert override not in configured, (
        "an override value equal to a configured id makes Claude Code treat that "
        "model as Haiku 4.5")
