"""Stage a role's SKILLS onto the shared S3Files mount, for its container to read.

The deployed coding agents build INSIDE their AgentCore Runtime container, where
the only shared, writable workspace is ``/mnt/s3files``, backed by an S3Files
access point all three runtimes mount. That mount is read-through from S3: an
object uploaded to ``s3://<bucket>/agents/mnt/s3files/<key>`` appears at
``/mnt/s3files/<key>`` inside every runtime. So to give a role the SKILL it applies
(its principle-based harness), we upload it there before dispatch.

What is deliberately NOT staged is a sample module, a scaffold, or an acceptance
contract. The request is whatever the attendee typed, so there is nothing to stage on
its behalf, and staging an answer would be the predetermined-shape problem this design
exists to remove.

Per-run prefix (``<run_id>/``) isolates concurrent runs and makes cleanup a
single prefix delete. The bucket name follows the infra convention
``coding-agents-<account>-<region>`` (infra/setup.sh), resolvable from the
ambient AWS identity; nothing hardcoded.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

_MOUNT_PREFIX = "agents/mnt/s3files"  # S3 key prefix that maps to /mnt/s3files


def mnt_root() -> str:
    """The shared workspace root the coding agents build in.

    On a deployed AgentCore Runtime this is ``/mnt/s3files`` (the S3Files mount,
    fed by the read-through S3 upload below). A LOCAL ``agentcore dev`` / capture
    run has no such mount (S3 read-through only materializes inside a deployed
    runtime), so ``WORKSHOP_S3FILES_DIR`` wires it to a real local directory the
    local-dev CLIs read and write directly. Unset (the deployed default) keeps the
    exact ``/mnt/s3files`` path, so the shipped runtime path is unchanged."""
    return os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files")


def skill_path(run_id: str) -> str:
    """The in-workspace path the staged module lives at (read by the backend
    agent reads its skill from). Read-only material for one run."""
    return os.path.join(mnt_root(), f"{run_id}-skill")


def _bucket(region: str, account_id: str) -> str:
    # Wirable override first, then the infra/setup.sh convention.
    return os.environ.get("WORKSHOP_RUNTIME_BUCKET",
                          f"coding-agents-{account_id}-{region}")


def _s3_region() -> str:
    return os.environ.get("WORKSHOP_BEDROCK_REGION",
                          os.environ.get("AWS_REGION", "us-west-2"))


def _client(region: str):
    import boto3  # noqa: PLC0415 (lazy, mirrors llm.py / executor.py)
    return boto3.client("s3", region_name=region)


def _account_id(region: str) -> str:
    import boto3  # noqa: PLC0415
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def _upload_tree(s3, bucket: str, local_dir: str, key_prefix: str) -> int:
    """Upload every file under local_dir to bucket/key_prefix, preserving layout.
    Skips caches. Returns the file count."""
    n = 0
    for dp, dns, fns in os.walk(local_dir):
        dns[:] = [d for d in dns if d not in ("__pycache__", ".pytest_cache")]
        for fn in fns:
            if fn.endswith(".pyc"):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, local_dir)
            s3.upload_file(full, bucket, f"{key_prefix}/{rel}")
            n += 1
    return n


def stage_skills(run_id: str, skill_dirs: list[str],
                 region: str | None = None) -> int:
    """Upload each harness skill dir to ``<run_id>-skill/skills/<name>``, the
    run's READ-ONLY inputs prefix, so the dispatched CLI can read the SKILL.md
    its prompt names. The backend image also bakes its skill at ~/skills, but
    opencode's image does not, so without this staging the frontend prompt
    references a file that does not exist in its container.

    Deliberately NOT ``<run_id>/skills``: S3 read-through materializes uploaded
    prefixes root-owned, and pre-creating the agent's WRITABLE ``<run_id>/``
    workspace that way makes the artifact write fail for uid 1000. The
    ``-skill`` prefix is already the immutable-inputs side of that split."""
    if not skill_dirs:
        return 0
    if os.environ.get("WORKSHOP_S3FILES_DIR"):
        n = 0
        for d in skill_dirs:
            dest = os.path.join(skill_path(run_id), "skills", os.path.basename(d))
            shutil.copytree(d, dest, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            n += 1
        return n
    region = region or _s3_region()
    account_id = _account_id(region)
    bucket = _bucket(region, account_id)
    s3 = _client(region)
    n = 0
    for d in skill_dirs:
        key = f"{_MOUNT_PREFIX}/{run_id}-skill/skills/{os.path.basename(d)}"
        n += _upload_tree(s3, bucket, d, key)
    return n
