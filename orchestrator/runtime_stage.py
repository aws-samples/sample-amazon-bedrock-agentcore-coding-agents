"""Stage a usecase module + grading contract onto the shared S3Files mount.

The deployed coding agents build INSIDE their AgentCore Runtime container, where
the only shared, writable workspace is ``/mnt/s3files``, backed by an S3Files
access point all three runtimes mount. That mount is read-through from S3: an
object uploaded to ``s3://<bucket>/agents/mnt/s3files/<key>`` appears at
``/mnt/s3files/<key>`` inside every runtime. So to let the backend agent
``import cost_analyzer`` and the validator run the grading contract, we upload
those files there before dispatch.

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
    agent's ``sys.path.insert``). Mirrors where ``stage_usecase`` puts it."""
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


def stage_usecase(run_id: str, uc: dict[str, str], region: str | None = None) -> str:
    """Stage the usecase module module + grading contract to /mnt/s3files/<run_id>.

    Returns the runtime workspace path the agents should ``cd`` into. Raises on
    any AWS failure (fail loud: a missing module means the backend cannot
    build)."""
    # LOCAL mount seam: when WORKSHOP_S3FILES_DIR wires the mount to a local dir
    # (the on-laptop `agentcore dev` / capture path), there is no S3 read-through,
    # so COPY the build inputs straight into <mount>/<run_id>-skill. The layout the
    # agent sees is identical to the deployed mount; only the transport differs.
    if os.environ.get("WORKSHOP_S3FILES_DIR"):
        skill_dir = skill_path(run_id)
        os.makedirs(os.path.join(skill_dir, "grading"), exist_ok=True)
        module_file = os.path.join(uc["dir"], uc["module"] + ".py")
        shutil.copy(module_file, os.path.join(skill_dir, uc["module"] + ".py"))
        shutil.copytree(uc["grading"], os.path.join(skill_dir, "grading"),
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
        return skill_dir

    region = region or _s3_region()
    account_id = _account_id(region)
    bucket = _bucket(region, account_id)
    s3 = _client(region)

    # Keep immutable build inputs and writable outputs in separate prefixes. The
    # S3 Files access point maps the runtime identity to uid/gid 1000, so this is
    # an isolation boundary, not a claim that the mount is root-owned. The agent
    # reads ``<run_id>-skill/`` and creates its artifact under ``<run_id>/``.
    skill_key = f"{_MOUNT_PREFIX}/{run_id}-skill"
    module_file = os.path.join(uc["dir"], uc["module"] + ".py")
    s3.upload_file(module_file, bucket, f"{skill_key}/{uc['module']}.py")
    # the grading contract dir: the offline floor grades against it.
    _upload_tree(s3, bucket, uc["grading"], f"{skill_key}/grading")
    # Return the in-runtime input path; the agent's output dir is the separate
    # /mnt/s3files/<run_id> prefix created at dispatch.
    return skill_path(run_id)


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


def unstage_usecase(run_id: str, region: str | None = None) -> dict[str, Any]:
    """Delete a run's staged prefix (cleanup / return-to-clean-state)."""
    # Local mount seam: remove the local <mount>/<run_id>* dirs, no S3.
    if os.environ.get("WORKSHOP_S3FILES_DIR"):
        deleted = 0
        for d in (skill_path(run_id), os.path.join(mnt_root(), run_id)):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                deleted += 1
        return {"deleted": deleted, "run_id": run_id}
    region = region or _s3_region()
    account_id = _account_id(region)
    bucket = _bucket(region, account_id)
    s3 = _client(region)
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    # Both the staged-skill prefix and (any) agent work prefix for this run.
    for prefix in (f"{_MOUNT_PREFIX}/{run_id}-skill/", f"{_MOUNT_PREFIX}/{run_id}/"):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                deleted += len(objs)
    return {"deleted": deleted, "run_id": run_id}
