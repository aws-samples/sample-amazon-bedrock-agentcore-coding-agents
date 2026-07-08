"""llm unit tests (offline): the same-provider fallback, with no live creds.

The live model path is smoke-tested in ``test_llm_smoke.py`` (skipped without AWS
credentials). This file pins the deterministic resilience logic by mocking the
transport, so it runs everywhere: a registered model whose inference backend 5xx's
falls back ONCE to the healthy sibling model; a 4xx propagates unchanged.

    python3 -m pytest orchestrator/test_llm.py -v
"""

from __future__ import annotations

import io
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm  # noqa: E402


def test_resolve_passes_through_openai_ids():
    assert llm.resolve("openai.gpt-5.5") == "openai.gpt-5.5"
    assert llm.resolve("claude-opus-4-6") == "us.anthropic.claude-opus-4-6-v1"


def _http_error(code: int, body: bytes = b'{"error":{"message":"server error"}}'):
    return urllib.error.HTTPError(
        url="https://x", code=code, msg="err", hdrs=None, fp=io.BytesIO(body))


# Body the mantle gateway returns for a de-registered model id (observed live for
# openai.gpt-5.5): a 400 envelope whose message wraps the upstream engine's 404.
_ENGINE_GONE_BODY = (
    b'{"error":{"code":"validation_error","message":"JSON-RPC error -32602: '
    b'Job registration failed: Engine bad request: Task submission failed with '
    b'status 404 Not Found: Engine not found","type":"invalid_request_error"}}')


def _ok_response_bytes(model_id: str) -> bytes:
    import json
    return json.dumps({
        "model": model_id,
        "output": [{"content": [{"type": "output_text", "text": "OK"}]}],
        "usage": {"input_tokens": 8, "output_tokens": 5},
    }).encode()


class _FakeResp:
    def __init__(self, data: bytes):
        self._data = data
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_openai_5xx_falls_back_to_the_sibling_model(monkeypatch):
    """A 500 on the primary retries ONCE on the fallback; the returned model_id is
    the model that ACTUALLY served the call (honest attribution)."""
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "openai.gpt-5.4")
    monkeypatch.setattr(llm, "provide_token", lambda **k: "tok", raising=False)
    # Token generator is imported inside the function; stub the import site.
    import aws_bedrock_token_generator as g
    monkeypatch.setattr(g, "provide_token", lambda **k: "tok")

    calls: list[str] = []

    def fake_urlopen(req, timeout=0):
        import json
        model = json.loads(req.data)["model"]
        calls.append(model)
        if model == "openai.gpt-5.5":
            raise _http_error(500)
        return _FakeResp(_ok_response_bytes(model))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    out = llm.invoke("openai.gpt-5.5", "Reply OK", max_tokens=64)
    assert out["text"] == "OK"
    assert out["api"] == "responses"
    assert out["model_id"] == "openai.gpt-5.4"          # honest: who served it
    assert calls == ["openai.gpt-5.5", "openai.gpt-5.4"]  # primary first, then fallback


def test_openai_4xx_does_not_fall_back(monkeypatch):
    """A 4xx is a bad request, not an outage; it raises, never silently retries."""
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "openai.gpt-5.4")
    import aws_bedrock_token_generator as g
    monkeypatch.setattr(g, "provide_token", lambda **k: "tok")

    calls: list[str] = []

    def fake_urlopen(req, timeout=0):
        import json
        calls.append(json.loads(req.data)["model"])
        raise _http_error(400)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(llm.LLMUnavailable):
        llm.invoke("openai.gpt-5.5", "Reply OK", max_tokens=64)
    assert calls == ["openai.gpt-5.5"]   # no fallback retry on a 4xx


def test_openai_5xx_with_fallback_disabled_raises(monkeypatch):
    """With the fallback turned off, a 5xx propagates as LLMUnavailable (the old
    behavior is recoverable via WORKSHOP_OPENAI_FALLBACK="")."""
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "")
    import aws_bedrock_token_generator as g
    monkeypatch.setattr(g, "provide_token", lambda **k: "tok")
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_error(500)))
    with pytest.raises(llm.LLMUnavailable):
        llm.invoke("openai.gpt-5.5", "Reply OK", max_tokens=64)


def test_is_model_gone_classifies_only_model_unavailability():
    """A 404, or a 400 whose body carries the gateway's engine-not-found signal, is
    'model gone'. A plain 400 bad request is NOT."""
    assert llm._is_model_gone(404, "anything") is True
    assert llm._is_model_gone(400, _ENGINE_GONE_BODY.decode()) is True
    assert llm._is_model_gone(400, "'minimal' is not supported with the model") is False
    assert llm._is_model_gone(401, "no service control policy allows") is False
    assert llm._is_model_gone(500, "Engine not found") is False  # 5xx handled separately


def test_openai_deregistered_model_falls_back_to_sibling(monkeypatch):
    """A retired model id surfaces as a 400 wrapping a 404 'Engine not found' (the
    live gpt-5.5 signature). The same-provider fallback fires ONCE to the healthy
    sibling, served over the SAME Responses endpoint: real usage, honest model_id."""
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "openai.gpt-5.4")
    import aws_bedrock_token_generator as g
    monkeypatch.setattr(g, "provide_token", lambda **k: "tok")

    calls: list[str] = []

    def fake_urlopen(req, timeout=0):
        import json
        model = json.loads(req.data)["model"]
        calls.append(model)
        if model == "openai.gpt-5.5":
            raise _http_error(400, _ENGINE_GONE_BODY)
        return _FakeResp(_ok_response_bytes(model))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    out = llm.invoke("openai.gpt-5.5", "Reply OK", max_tokens=64)
    assert out["text"] == "OK"
    assert out["api"] == "responses"
    assert out["model_id"] == "openai.gpt-5.4"
    assert calls == ["openai.gpt-5.5", "openai.gpt-5.4"]


def test_cli_model_is_down_matches_outage_signatures_only():
    """The CLI-text classifier (codex talks to mantle itself) flags de-registration
    and transient backend outages, but NOT a plain build/config error."""
    assert llm.cli_model_is_down("status 404 Not Found: Engine not found") is True
    assert llm.cli_model_is_down("the server had an error (stream disconnected)") is True
    assert llm.cli_model_is_down("HTTP 503 Service Unavailable") is True
    assert llm.cli_model_is_down("AGENTS.md not found in workspace") is False
    assert llm.cli_model_is_down("invalid api key") is False
    assert llm.cli_model_is_down("") is False


def test_openai_sibling_resolution(monkeypatch):
    """openai_sibling returns the fallback for an OpenAI id, None otherwise / when
    disabled / when the id already IS the sibling."""
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "openai.gpt-5.4")
    assert llm.openai_sibling("openai.gpt-5.5") == "openai.gpt-5.4"
    assert llm.openai_sibling("openai.gpt-5.4") is None         # already the sibling
    assert llm.openai_sibling("us.anthropic.claude-opus-4-6-v1") is None  # not OpenAI
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "")
    assert llm.openai_sibling("openai.gpt-5.5") is None          # fallback disabled


def test_openai_404_falls_back_to_sibling(monkeypatch):
    """A bare 404 (model id not found) also triggers the same-provider fallback."""
    monkeypatch.setattr(llm, "OPENAI_FALLBACK_MODEL", "openai.gpt-5.4")
    import aws_bedrock_token_generator as g
    monkeypatch.setattr(g, "provide_token", lambda **k: "tok")

    calls: list[str] = []

    def fake_urlopen(req, timeout=0):
        import json
        model = json.loads(req.data)["model"]
        calls.append(model)
        if model == "openai.gpt-5.5":
            raise _http_error(404, b'{"error":{"message":"not found"}}')
        return _FakeResp(_ok_response_bytes(model))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    out = llm.invoke("openai.gpt-5.5", "Reply OK", max_tokens=64)
    assert out["model_id"] == "openai.gpt-5.4"
    assert calls == ["openai.gpt-5.5", "openai.gpt-5.4"]
