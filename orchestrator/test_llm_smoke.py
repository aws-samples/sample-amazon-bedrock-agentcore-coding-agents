"""Live LLM smoke: proves the real ``llm.invoke`` model path (Converse + the
mantle Responses endpoint) returns measured usage.

Skipped automatically when AWS credentials are absent (CI, attendee laptops before
setup). The ``llm`` module is what the deployed coding-agent runtimes use to talk
to Bedrock; this pins its two API shapes with real calls when creds are present.

    python3 -m pytest orchestrator/test_llm_smoke.py -v
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm  # noqa: E402

pytestmark = pytest.mark.skipif(
    not llm.available() or os.environ.get("WORKSHOP_SKIP_LIVE") == "1",
    reason="no AWS credentials (or WORKSHOP_SKIP_LIVE=1); live LLM smoke untestable here",
)


def test_invoke_claude_returns_real_usage():
    out = llm.invoke("claude-haiku-4-5", "Reply with exactly: OK", max_tokens=16)
    assert out["text"].strip() == "OK"
    assert out["input_tokens"] > 0 and out["output_tokens"] > 0
    assert out["api"] == "converse"


# An account can have Bedrock (Converse/Claude) access yet NOT be entitled to the
# mantle OpenAI-on-Bedrock endpoint: the bearer-token call then 401/403s with an
# access-denied / not-authorized body. The IAM grant exists (see the CFN
# WorkshopInstanceRole), but the hosting account must also be ENTITLED to Mantle;
# a Workshop Studio account without it returns an org-SCP explicit deny. That is an
# ENTITLEMENT gap in the runner's account (tracking: EE-14394, affecting Codex +
# gpt-5.5), not a defect in llm.py, so the OpenAI path is untestable here and the
# test skips. By design llm.invoke does NOT fall back on a 401 (an auth/SCP denial
# is not model-side), so it raises LLMUnavailable carrying the gateway's text.
_MANTLE_FORBIDDEN_RE = re.compile(
    r"\bHTTP 40[13]\b|access[ _]?denied|not authorized|accessdenied"
    r"|unauthorized|forbidden|createinference",
    re.IGNORECASE)


def test_invoke_gpt55_via_mantle_returns_real_usage():
    # The frontend role asks for gpt-5.5 on the mantle Responses endpoint. If that
    # model id is currently de-registered (the gateway returns "Engine not found"),
    # llm.invoke falls back ONCE to the healthy sibling on the SAME endpoint, a
    # real model with real usage, not canned text. So this asserts the OpenAI-on-
    # Bedrock path itself returns real usage; model_id names who actually served it
    # (gpt-5.5 normally, the sibling when 5.5 is gone).
    try:
        out = llm.invoke("openai.gpt-5.5", "Reply with exactly: OK", max_tokens=64)
    except llm.LLMUnavailable as exc:
        if _MANTLE_FORBIDDEN_RE.search(str(exc)):
            pytest.skip(f"account not entitled to the mantle OpenAI endpoint: {exc}")
        raise
    assert out["text"].strip() == "OK"
    assert out["input_tokens"] > 0 and out["output_tokens"] > 0
    assert out["api"] == "responses"
    assert out["model_id"].startswith("openai.")
