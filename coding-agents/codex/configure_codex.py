"""Write the Runtime's non-secret Codex configuration before any shell opens.

Both a hand-opened shell and a coordinator's direct ``codex exec`` read this
file. AWS credentials stay in the SDK chain; only the deployment region and
model are written here. GitHub access belongs to the coordinator.
"""

import json
import os
from pathlib import Path


DEFAULT_MODEL = "us.openai.gpt-5.6-sol"
PROVIDER = "amazon-bedrock-runtime"


def region_from_env(env):
    region = (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        raise ValueError("No AWS region. Set AWS_REGION or AWS_DEFAULT_REGION.")
    return region


def model_from_env(env):
    return (env.get("WORKSHOP_MODEL_CODEX") or env.get("WORKSHOP_MODEL")
            or env.get("WORKSHOP_CODEX_MODEL") or DEFAULT_MODEL)


def render_config(region, model, workdirs=()):
    """Render only workshop-owned settings, without embedding credentials."""
    if not region.strip():
        raise ValueError("No AWS region. Set AWS_REGION or AWS_DEFAULT_REGION.")
    # JSON basic strings are also TOML basic strings; paths/model overrides must
    # not become TOML syntax when they contain quotes or backslashes.
    def quote(value):
        return json.dumps(value, ensure_ascii=False)
    text = (
        "# Generated at container startup; AWS credentials come from the SDK chain.\n"
        f"model = {quote(model)}\n"
        f"model_provider = {quote(PROVIDER)}\n"
        'model_reasoning_effort = "medium"\n'
        '\n[otel]\n'
        'environment = "workshop"\n'
        'log_user_prompt = false\n'
        'exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary" } }\n'
        'trace_exporter = "none"\n'
        f"\n[model_providers.{PROVIDER}.aws]\n"
        f"region = {quote(region)}\n"
    )
    for workdir in dict.fromkeys(str(Path(p).resolve()) for p in workdirs):
        text += f'\n[projects.{quote(workdir)}]\ntrust_level = "trusted"\n'
    return text


def configure(env=None):
    env = os.environ if env is None else env
    region = region_from_env(env)
    # CODEX_HOME, when supplied, is Codex's configuration directory, not HOME.
    config_dir = Path(env.get("CODEX_HOME") or str(Path(env["HOME"]) / ".codex"))
    config_dir.mkdir(parents=True, exist_ok=True)
    text = render_config(region, model_from_env(env))
    target = config_dir / "config.toml"
    target.write_text(text, encoding="utf-8")
    return target


if __name__ == "__main__":
    try:
        configure()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
