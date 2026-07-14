#!/usr/bin/env python3
"""Write the live opencode config without discarding session telemetry settings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_existing_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print(
            f"WARNING: ignoring invalid existing opencode config at {path}",
            file=sys.stderr,
        )
        return {}

    if not isinstance(value, dict):
        print(
            f"WARNING: ignoring non-object existing opencode config at {path}",
            file=sys.stderr,
        )
        return {}
    return value


def build_config(
    region: str,
    gateway_url: str,
    existing: dict[str, Any],
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "amazon-bedrock": {
                "options": {"region": region},
            },
        },
        "model": "amazon-bedrock/us.anthropic.claude-sonnet-4-6",
        "small_model": "amazon-bedrock/anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    if gateway_url:
        config["mcp"] = {
            "gateway": {
                "type": "local",
                "command": [
                    "node",
                    "/mnt/s3files/mcp/index.js",
                    "--gateway-url",
                    gateway_url,
                    "--region",
                    region,
                ],
            },
        }

    username = existing.get("username")
    if isinstance(username, str) and username:
        config["username"] = username

    experimental = existing.get("experimental")
    if isinstance(experimental, dict) and "openTelemetry" in experimental:
        config["experimental"] = {
            "openTelemetry": experimental["openTelemetry"],
        }
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--region", required=True)
    parser.add_argument("--gateway-url", default="")
    args = parser.parse_args()

    existing = load_existing_config(args.config)
    config = build_config(args.region, args.gateway_url, existing)
    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(json.dumps(config, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
