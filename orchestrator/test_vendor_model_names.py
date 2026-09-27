"""A role's CLI must receive a model name it knows.

Bedrock roles take a Bedrock id, so the engine resolves llm aliases for them.
Kiro names its own models, and some of those names are also llm aliases:
`claude-sonnet-5` resolved to `global.anthropic.claude-sonnet-5`, which kiro-cli
rejects (a live dispatch on 2026-09-27). No model, Runtime, or network is used.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine  # noqa: E402
import llm  # noqa: E402
import roles  # noqa: E402


def test_kiro_keeps_its_own_name_even_when_it_is_also_a_bedrock_alias():
    assert {"claude-sonnet-5", "claude-opus-5"} <= set(llm.BEDROCK_MODEL_MAP)  # real collisions
    assert engine.Engine._wire_model("kiro", "claude-sonnet-5") == "claude-sonnet-5"
    assert engine.Engine._wire_model("kiro", "claude-opus-5") == "claude-opus-5"


def test_bedrock_roles_still_resolve_aliases_and_pass_full_ids_through():
    assert (engine.Engine._wire_model("claude-code", "claude-sonnet-5")
            == llm.BEDROCK_MODEL_MAP["claude-sonnet-5"])
    full = "global.openai.gpt-5.6-sol"
    assert engine.Engine._wire_model("codex", full) == full


def test_every_vendor_key_role_declares_its_own_model_names():
    # A role that authenticates with a vendor key talks to that vendor's models.
    for role in roles.REGISTRY:
        if role.credential == "api-key":
            assert role.model_namespace != "bedrock", role.id
