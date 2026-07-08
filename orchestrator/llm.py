"""Model invocation: every generated artifact comes from a live LLM call.

Two wire paths, matching how each model is served on Bedrock:

  * **Claude (Converse)**: ``boto3 bedrock-runtime.converse``. Opus 4.6 keeps
    Converse/InvokeModel compatibility, so the same call shape serves every
    Claude tier. Short model aliases resolve through ``BEDROCK_MODEL_MAP``:
    Fable 5 ships ONLY a ``global.`` profile; the 4.x family routes ``us.``.
  * **OpenAI (Responses API)**: GPT-5.5/5.4 do not appear in the standard
    Bedrock model catalog; they are served from the OpenAI-compatible endpoint
    ``https://bedrock-mantle.{region}.api.aws/openai/v1/responses`` with a
    bearer token minted by ``aws-bedrock-token-generator``. GPT-5.5 is
    us-east-2 (us-west-2 carries only 5.4).

The contract is fail-loud: no credentials or a dead endpoint raises
``LLMUnavailable``; the caller decides what "offline" means. The usage figures
it reports are the API's own token counts, not estimates.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any

# Short alias -> Bedrock model id. Fable 5 ships ONLY a `global.` profile
# (us.anthropic.claude-fable-5 is invalid); the 4.x family routes `us.`.
BEDROCK_MODEL_MAP: dict[str, str] = {
    "claude-fable-5": "global.anthropic.claude-fable-5",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-sonnet-4-5": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}

CLAUDE_REGION = os.environ.get("WORKSHOP_BEDROCK_REGION", "us-west-2")
# GPT-5.5 is served from us-east-2 on the mantle endpoint (us-west-2 = 5.4 only).
OPENAI_REGION = os.environ.get("WORKSHOP_MANTLE_REGION", "us-east-2")
OPENAI_RESPONSES_URL = f"https://bedrock-mantle.{OPENAI_REGION}.api.aws/openai/v1/responses"

# Same-provider resilience: if the primary OpenAI-on-Bedrock model is registered
# but its inference backend 5xxs (a provider-side outage of one model, distinct
# from an SCP denial or a 4xx bad request), fall back to a sibling OpenAI model
# that is healthy in the same region. Observed live: gpt-5.5 in us-east-2 returned
# HTTP 500 on every inference while gpt-5.4 served the identical request. Set
# WORKSHOP_OPENAI_FALLBACK="" to disable (then a 5xx propagates as LLMUnavailable).
OPENAI_FALLBACK_MODEL = os.environ.get("WORKSHOP_OPENAI_FALLBACK", "openai.gpt-5.4")

_TIMEOUT_S = 120


class LLMUnavailable(RuntimeError):
    """Bedrock cannot be reached (no credentials / no access / endpoint down).

    The engine catches this to enter its labeled offline mode; a failed call
    never substitutes generated-looking text.
    """


def resolve(model: str) -> str:
    """Resolve a short model alias to a Bedrock model id (pass through full ids)."""
    return BEDROCK_MODEL_MAP.get(model, model)


# ----------------------------------------------------------------- availability
_PROBE_LOCK = threading.Lock()
_PROBE: dict[str, bool | None] = {"ok": None}


def available() -> bool:
    """True if AWS credentials resolve on this machine (cheap, no model call).

    A positive probe means invoke() is worth attempting; a model call can still
    fail (no model access in the account) and raises LLMUnavailable then.
    """
    with _PROBE_LOCK:
        if _PROBE["ok"] is None:
            try:
                import boto3  # noqa: PLC0415
                _PROBE["ok"] = boto3.session.Session().get_credentials() is not None
            except Exception:
                _PROBE["ok"] = False
        return bool(_PROBE["ok"])


# --------------------------------------------------------------------- invoke
def invoke(model: str, prompt: str, system: str | None = None,
           max_tokens: int = 8000) -> dict[str, Any]:
    """One model call. Returns the API's own text + token counts.

    ``{"text", "input_tokens", "output_tokens", "model_id", "api"}`` where
    ``api`` is ``converse`` (Claude) or ``responses`` (OpenAI-on-Bedrock).
    Raises LLMUnavailable on any transport/credential/access failure.
    """
    model_id = resolve(model)
    if model_id.startswith("openai."):
        return _invoke_openai(model_id, prompt, system, max_tokens)
    return _invoke_claude(model_id, prompt, system, max_tokens)


def _invoke_claude(model_id: str, prompt: str, system: str | None,
                   max_tokens: int) -> dict[str, Any]:
    try:
        import boto3  # noqa: PLC0415
        rt = boto3.client("bedrock-runtime", region_name=CLAUDE_REGION)
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        resp = rt.converse(**kwargs)
    except Exception as exc:
        raise LLMUnavailable(f"converse({model_id}) failed: {exc}") from exc
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    usage = resp.get("usage", {})
    return {"text": text, "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "model_id": model_id, "api": "converse"}


# Signature of "this model id is no longer served" coming back from the mantle
# gateway. A retired/de-registered model surfaces as a literal 404, or (because
# the gateway wraps the upstream engine's reply) as a 400 whose body says
# "Engine not found" / "404 Not Found". Distinct from a real bad request (e.g.
# "'minimal' is not supported with the 'openai.gpt-5.5' model"), which is the
# request's fault and must NOT fall back. Match the gone-signal on the body text.
_MODEL_GONE_RE = re.compile(r"engine not found|404 not found|model.*not found",
                            re.IGNORECASE)


def _is_model_gone(code: int, detail: str) -> bool:
    """True if an HTTP error means the MODEL is unavailable (retired/de-registered),
    not that the REQUEST was bad. A 404 always qualifies; a 400 qualifies only when
    its body carries the gateway's engine/model-not-found signal."""
    if code == 404:
        return True
    if code == 400:
        return bool(_MODEL_GONE_RE.search(detail or ""))
    return False


# The codex CLI talks to the mantle endpoint itself (it is not an llm.invoke call),
# so when a model is down it surfaces the failure as TEXT on a nonzero exit, not an
# HTTPError. These are the signatures of "this OpenAI-on-Bedrock model is unusable
# for a reason that is the model's, not the request's": the de-registration body
# (Engine not found / 404) OR a transient backend 5xx ("server had an error",
# "stream disconnected", an explicit 5xx status). Same policy as _invoke_openai's
# HTTP path, matched on the CLI's stderr/stdout text.
_CLI_MODEL_DOWN_RE = re.compile(
    r"engine not found|404 not found|model.*not found"
    r"|server had an error|stream disconnected|stream closed"
    r"|\b5\d\d\b|internal server error|service unavailable",
    re.IGNORECASE)


def cli_model_is_down(output: str) -> bool:
    """True if a coding-agent CLI's failure text means its OpenAI-on-Bedrock model
    is unusable (de-registered or backend erroring): the CLI-level analogue of
    ``_is_model_gone`` for the codex subprocess, which reports failures as text on a
    nonzero exit rather than as an HTTP status. A plain auth/usage error does not
    match, so a real config bug still fails loud."""
    return bool(_CLI_MODEL_DOWN_RE.search(output or ""))


def openai_sibling(model_id: str) -> str | None:
    """The healthy same-provider fallback model for an OpenAI-on-Bedrock id, or
    None when fallback is disabled / the id already IS the sibling / it is not an
    OpenAI id. Mirrors the model the HTTP path falls back to in ``_invoke_openai``."""
    if (model_id.startswith("openai.") and OPENAI_FALLBACK_MODEL
            and OPENAI_FALLBACK_MODEL != model_id):
        return OPENAI_FALLBACK_MODEL
    return None


def _invoke_openai(model_id: str, prompt: str, system: str | None,
                   max_tokens: int, _allow_fallback: bool = True) -> dict[str, Any]:
    try:
        from aws_bedrock_token_generator import provide_token  # noqa: PLC0415
        token = provide_token(region=OPENAI_REGION)
    except Exception as exc:
        raise LLMUnavailable(
            f"cannot mint a Bedrock bearer token for {OPENAI_REGION} "
            f"(pip install aws-bedrock-token-generator): {exc}") from exc
    body = {
        "model": model_id,
        "input": ([{"role": "system", "content": system}] if system else [])
        + [{"role": "user", "content": prompt}],
        "max_output_tokens": max_tokens,
        "store": False,
    }
    req = urllib.request.Request(
        OPENAI_RESPONSES_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        # Same-provider resilience: retry ONCE on the healthy sibling model when
        # THIS model is unusable for a reason that is the model's, not the
        # request's:
        #   * a 5xx: the inference backend is down (registered, valid request,
        #     server erred); OR
        #   * the model is GONE: the mantle gateway returns "Engine not found"
        #     when a model id is de-registered/retired. Observed as a literal 404
        #     and (because the gateway wraps the engine response) as a 400 whose
        #     body carries "Engine not found" / "404 Not Found". Either way the
        #     sibling that is still registered serves the identical request.
        # A real 4xx bad request (unsupported param) or the SCP-denial 401 that
        # re-routes to Claude upstream is NOT model-side and must propagate.
        model_gone = _is_model_gone(exc.code, detail)
        if (_allow_fallback and (500 <= exc.code < 600 or model_gone)
                and OPENAI_FALLBACK_MODEL and OPENAI_FALLBACK_MODEL != model_id):
            return _invoke_openai(OPENAI_FALLBACK_MODEL, prompt, system,
                                  max_tokens, _allow_fallback=False)
        raise LLMUnavailable(f"responses({model_id}) HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise LLMUnavailable(f"responses({model_id}) failed: {exc}") from exc
    # Responses shape: output[] holds reasoning + message items; text lives in
    # message items' content[] as output_text blocks.
    pieces: list[str] = []
    for item in resp.get("output", []) or []:
        for block in item.get("content", []) or []:
            if block.get("type") == "output_text":
                pieces.append(block.get("text", ""))
    usage = resp.get("usage", {})
    # model_id reflects which model served the call (the fallback when the
    # primary 5xx'd), so usage attribution and the run record stay accurate.
    return {"text": "".join(pieces), "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "model_id": resp.get("model", model_id), "api": "responses"}


def extract_code_block(text: str, language: str = "") -> str:
    """Pull the first fenced code block out of a model reply (or the raw text).

    Models wrap files in ```lang fences; the engine wants the file body. If no
    fence is present the reply IS the file (system prompts ask for raw output).
    """
    fence = "```"
    start = text.find(fence)
    if start == -1:
        return text.strip()
    nl = text.find("\n", start)
    if nl == -1:
        return text.strip()
    end = text.find(fence, nl)
    if end == -1:
        return text[nl + 1:].strip()
    return text[nl + 1:end].strip()
