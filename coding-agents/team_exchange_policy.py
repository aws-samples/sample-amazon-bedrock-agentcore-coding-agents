#!/usr/bin/env python3
"""Render one Runtime role's source-exchange grant. Does not contact AWS.

The coordinator owns inputs; each role may only CREATE its own outputs. Neither
prefix is mounted through S3 Files. The JSON example alongside this module is
for Claude Code; use this renderer for a console-created Codex or checker role.
"""
from __future__ import annotations

import argparse
import json
import re

ROLE_IDS = ("claude-code", "codex", "kiro", "claude-code-validator", "opencode")


def statements(bucket: str, role_id: str) -> list[dict]:
    if role_id not in ROLE_IDS:
        raise ValueError(f"Unknown archive producer: {role_id}")
    # The wildcard form belongs only to the shipped example. The CLI requires
    # the exact deployed bucket, like every role deployer's infra.config.
    if (bucket != "coding-agents-*"
            and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)):
        raise ValueError("An exact deployed S3 bucket name is required")
    return [
        {
            "Sid": "ReadCoordinatorArchives",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket}/runtime-exchange/coordinator/*"],
        },
        {
            "Sid": "CreateOwnArchives",
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": [f"arn:aws:s3:::{bucket}/runtime-exchange/roles/{role_id}/*"],
            "Condition": {"StringEquals": {"s3:if-none-match": "*"}},
        },
    ]


def document(bucket: str, role_id: str) -> dict:
    return {"Version": "2012-10-17", "Statement": statements(bucket, role_id)}


def is_current(policy: dict, role_id: str) -> bool:
    """Recognize the generated role-specific grant, not merely its IAM name.

    A legacy TeamRunExchange grant let every role replace every archive. Keep a
    stale or hand-expanded grant visible until the attendee replaces it.
    """
    try:
        rows = policy["Statement"]
        for row in rows:
            resources = row["Resource"]
            if isinstance(resources, str):
                resources = [resources]
            for resource in resources:
                match = re.fullmatch(
                    r"arn:aws:s3:::([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/"
                    r"runtime-exchange/coordinator/\*", resource)
                if match:
                    expected = document(match.group(1), role_id)
                    return (policy.get("Version") == expected["Version"]
                            and sorted(rows, key=lambda r: r["Sid"])
                            == sorted(expected["Statement"], key=lambda r: r["Sid"]))
    except (KeyError, TypeError, ValueError):
        pass
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=ROLE_IDS)
    parser.add_argument("--bucket", required=True, help="INFRA_BUCKET from infra.config")
    args = parser.parse_args()
    if "*" in args.bucket:
        parser.error("--bucket must name the exact deployed bucket")
    try:
        result = document(args.bucket, args.role)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
