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
import time
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
    override = os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()
    if override:
        return override
    # The region is part of the NAME here, so an empty one is not "let boto3
    # decide" but a WRONG bucket: `coding-agents-<acct>-` with a trailing dash,
    # which is exactly the name a live run failed on. Ask the session for the
    # real region rather than interpolating a blank.
    if not region:
        region = _resolved_region()
    if not region:
        raise RuntimeError(
            "REGION_NOT_RESOLVED: cannot build the runtime bucket name without a "
            "region. Set AWS_REGION (or WORKSHOP_RUNTIME_BUCKET) and retry.")
    return f"coding-agents-{account_id}-{region}"


def _resolved_region() -> str:
    """The session's real region, asked of boto3 when the env does not say.

    boto3 resolves ``~/.aws/config`` then IMDS, so on the workshop host this
    returns the box's own region even when no AWS_* variable is exported (the
    non-interactive-shell case that produced a trailing-dash bucket name)."""
    try:
        import boto3  # noqa: PLC0415
        return boto3.session.Session().region_name or ""
    except Exception:  # noqa: BLE001 (no SDK: caller raises a clear error)
        return ""


def _s3_region() -> str:
    """The region to stage into: the ambient one. "" means "let boto3 resolve it"
    (config file, then IMDS), which is right on the workshop host; a hardcoded
    region would silently target another region's bucket."""
    return (os.environ.get("WORKSHOP_BEDROCK_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION") or "")


def _client(region: str):
    import boto3  # noqa: PLC0415 (lazy, mirrors llm.py / executor.py)
    # region or None: an EMPTY string is not a region, and passing one to boto3
    # raises instead of letting its own resolver (config file, IMDS) answer.
    return boto3.client("s3", region_name=region or None)


def _account_id(region: str) -> str:
    import boto3  # noqa: PLC0415
    return boto3.client("sts",
                        region_name=region or None).get_caller_identity()["Account"]


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
            # Retry a transient upload. The caller treats a staging failure as
            # non-fatal (the role falls back to its baked-in guidance), which means a
            # single throttled PutObject silently changes what the agent was told --
            # two attendees running the same request get different-quality work and
            # nothing on the run says why. A retry keeps that fallback for real
            # outages instead of for one unlucky packet.
            for attempt in range(3):
                try:
                    s3.upload_file(full, bucket, f"{key_prefix}/{rel}")
                    break
                except Exception:  # noqa: BLE001 (re-raised on the last attempt)
                    if attempt == 2:
                        raise
                    time.sleep(1.0 * (attempt + 1))
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
