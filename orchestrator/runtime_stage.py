"""Move exact role checkouts between the coordinator and AgentCore Runtimes.

The GitHub Gateway gives the coordinator a tracked branch snapshot. This module
packs those source bytes into one normalized archive per isolated work item.
Runtimes download the archive through the S3 API, work on local disk, and upload
one result archive. The orchestration path never copies a repository file by file
through the S3 Files NFS mount.

S3 Files remains the customer-visible shared storage used in Lab 1. The local-dev
seam also uses ``WORKSHOP_S3FILES_DIR`` so tests can exercise the same tree shape
without AWS.

What is deliberately NOT staged is a sample module, a scaffold, or an acceptance
contract. The request is whatever the attendee typed, so there is nothing to stage on
its behalf, and staging an answer would be the predetermined-shape problem this design
exists to remove.

Per-run object names isolate concurrent runs and make cleanup a single prefix
delete. The bucket name follows the infra convention
``coding-agents-<account>-<region>`` (infra/setup.sh), resolvable from the
ambient AWS identity; nothing hardcoded.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import tarfile
import time
from typing import Any, Iterable

_EXCHANGE_PREFIX = "agents/runtime-exchange"
_ARCHIVE_EXCLUDES = {
    "node_modules", "__pycache__", ".git", ".venv", "venv",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".next", ".cache",
}


def mnt_root() -> str:
    """The Lab 1/local-dev shared workspace root.

    A local ``agentcore dev`` or capture run has no managed mount, so
    ``WORKSHOP_S3FILES_DIR`` wires it to a local directory. Deployed orchestration
    uses exchange archives instead; the default remains the path attendees use
    directly in Lab 1."""
    return os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files")


def skill_path(run_id: str) -> str:
    """The in-workspace path the staged module lives at (read by the backend
    agent reads its skill from). Read-only material for one run."""
    return os.path.join(mnt_root(), f"{run_id}-skill")


def candidate_subdir(run_id: str) -> str:
    """The immutable integration candidate prefix visible to every Runtime."""
    return f"{run_id}-candidate"


def candidate_path(run_id: str) -> str:
    return os.path.join(mnt_root(), candidate_subdir(run_id))


def base_subdir(run_id: str) -> str:
    """The latest immutable integration base cloned into role checkouts."""
    return f"{run_id}-base"


def base_path(run_id: str) -> str:
    return os.path.join(mnt_root(), base_subdir(run_id))


def refresh_subdir(run_id: str, work_id: str) -> str:
    """One owner-specific refresh seed after the integration branch advances."""
    return f"{run_id}-refresh-{work_id}"


def refresh_path(run_id: str, work_id: str) -> str:
    return os.path.join(mnt_root(), refresh_subdir(run_id, work_id))


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


def _safe_subdir(value: str) -> str:
    normalized = (value or "").replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if (not normalized or any(part in ("", ".", "..") for part in parts)
            or any("\x00" in part for part in parts)):
        raise RuntimeError(f"unsafe Runtime archive path: {value!r}")
    return "/".join(parts)


def archive_key(subdir: str) -> str:
    """The one S3 object carrying a Runtime checkout.

    Runtime work no longer traverses the S3 Files mount file by file. The host
    checks out an exact Git ref through the GitHub Gateway, stores its tracked
    bytes in one archive, and each Runtime downloads that object to local disk.
    """
    return f"{_EXCHANGE_PREFIX}/{_safe_subdir(subdir)}.tar.gz"


def _bucket_name(region: str) -> str:
    region = region or _s3_region()
    override = os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()
    if override:
        return override
    return _bucket(region, _account_id(region))


def archive_uri(subdir: str, region: str | None = None) -> str:
    region = region or _s3_region()
    return f"s3://{_bucket_name(region)}/{archive_key(subdir)}"


def skills_subdir(run_id: str, agent_id: str | None = None) -> str:
    suffix = f"-{_safe_subdir(agent_id)}" if agent_id else ""
    return f"{run_id}-skills{suffix}"


def skills_archive_uri(run_id: str, agent_id: str | None = None,
                       region: str | None = None) -> str:
    return archive_uri(skills_subdir(run_id, agent_id), region)


def _iter_archive_entries(source: str, prefix: str = ""):
    """Yield normalized archive members containing bytes and executable intent."""
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _ARCHIVE_EXCLUDES)
        rel_dir = os.path.relpath(dirpath, source)
        if rel_dir != ".":
            arcdir = "/".join(
                part for part in (prefix, rel_dir.replace(os.sep, "/")) if part)
            yield dirpath, arcdir, True
        for filename in sorted(filenames):
            if filename in _ARCHIVE_EXCLUDES or filename.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                raise RuntimeError(
                    f"symbolic links are not portable Runtime input: {full}")
            rel = os.path.relpath(full, source).replace(os.sep, "/")
            arcname = "/".join(part for part in (prefix, rel) if part)
            yield full, arcname, False


def _pack_trees(trees: Iterable[tuple[str, str]]) -> tuple[bytes, int]:
    """Pack source bytes with normalized metadata and no dependency caches."""
    out = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for source, prefix in trees:
            if not os.path.isdir(source):
                raise RuntimeError(f"archive source does not exist: {source}")
            for full, arcname, is_dir in _iter_archive_entries(source, prefix):
                info = tf.gettarinfo(full, arcname=arcname)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if is_dir:
                    info.mode = 0o755
                    tf.addfile(info)
                    continue
                info.mode = (
                    0o755 if stat.S_IMODE(os.stat(full).st_mode) & 0o111
                    else 0o644
                )
                with open(full, "rb") as reader:
                    tf.addfile(info, reader)
                count += 1
    return out.getvalue(), count


def _put_archive(subdir: str, trees: Iterable[tuple[str, str]],
                 region: str | None = None) -> int:
    region = region or _s3_region()
    bucket = _bucket_name(region)
    body, count = _pack_trees(trees)
    s3 = _client(region)
    for attempt in range(3):
        try:
            s3.put_object(
                Bucket=bucket,
                Key=archive_key(subdir),
                Body=body,
                ContentType="application/gzip",
            )
            return count
        except Exception:  # noqa: BLE001 (re-raised on final transient failure)
            if attempt == 2:
                raise
            time.sleep(1.0 * (attempt + 1))
    raise AssertionError("archive upload retry loop exhausted")


def clone_archive(source_subdir: str, destination_subdir: str,
                  region: str | None = None) -> None:
    """Clone one immutable checkout with an S3 server-side object copy."""
    region = region or _s3_region()
    bucket = _bucket_name(region)
    s3 = _client(region)
    s3.copy_object(
        Bucket=bucket,
        Key=archive_key(destination_subdir),
        CopySource={"Bucket": bucket, "Key": archive_key(source_subdir)},
        ContentType="application/gzip",
        MetadataDirective="REPLACE",
    )


def read_archive(subdir: str, region: str | None = None) -> dict[str, bytes]:
    """Read one atomically uploaded Runtime result archive."""
    region = region or _s3_region()
    response = _client(region).get_object(
        Bucket=_bucket_name(region), Key=archive_key(subdir))
    payload = response["Body"].read()
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tf:
        for member in tf.getmembers():
            name = member.name.replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            if (not member.isfile() or not name or name.startswith("/")
                    or ".." in name.split("/")):
                continue
            reader = tf.extractfile(member)
            if reader is not None:
                files[name] = reader.read()
    return files


def list_archive(subdir: str, region: str | None = None) -> str:
    return "\n".join(sorted(read_archive(subdir, region))) + "\n"


def cleanup_run(run_id: str, region: str | None = None) -> None:
    """Delete one run's exchange archives after its durable result is saved."""
    region = region or _s3_region()
    bucket = _bucket_name(region)
    s3 = _client(region)
    prefix = f"{_EXCHANGE_PREFIX}/{_safe_subdir(run_id)}"
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        objects = [{"Key": row["Key"]} for row in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        if not page.get("IsTruncated"):
            return
        token = page.get("NextContinuationToken")


def copy_tree_files(source: str, destination: str, *,
                    replace: bool = True,
                    excluded_names: Iterable[str] = (
                        "__pycache__", ".pytest_cache"),
                    ) -> int:
    """Copy file contents without metadata operations S3 Files may reject.

    ``shutil.copytree`` delegates to ``copy2`` and therefore copies timestamps,
    modes, and extended attributes after each file. S3 Files is an NFS surface
    and can return errno 524 for those metadata calls even though the bytes were
    written. A checkout needs the bytes and relative paths, not host ownership or
    xattrs, so copy those explicitly and retry transient writes.
    """
    if not os.path.isdir(source):
        raise RuntimeError(f"source directory does not exist: {source}")
    excluded = set(excluded_names)
    if replace:
        shutil.rmtree(destination, ignore_errors=True)
    os.makedirs(destination, exist_ok=True)
    count = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        rel_dir = os.path.relpath(dirpath, source)
        target_dir = (destination if rel_dir == "."
                      else os.path.join(destination, rel_dir))
        os.makedirs(target_dir, exist_ok=True)
        for filename in sorted(filenames):
            if filename in excluded or filename.endswith(".pyc"):
                continue
            src = os.path.join(dirpath, filename)
            if os.path.islink(src):
                raise RuntimeError(
                    f"symbolic links are not portable Runtime input: {src}")
            dest = os.path.join(target_dir, filename)
            for attempt in range(3):
                try:
                    with open(src, "rb") as reader, open(dest, "wb") as writer:
                        while True:
                            chunk = reader.read(1024 * 1024)
                            if not chunk:
                                break
                            writer.write(chunk)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(1.0 * (attempt + 1))
            try:
                if stat.S_IMODE(os.stat(src).st_mode) & 0o111:
                    os.chmod(dest, 0o755)
            except OSError:
                pass
            count += 1
    return count


def stage_candidate(run_id: str, local_dir: str,
                    region: str | None = None) -> int:
    """Publish the exact integration candidate as immutable Runtime input.

    A candidate is rebuilt after a repair round. The old prefix is removed first
    so a deleted file from round one cannot survive into the tree the validator
    inspects in round two.
    """
    if not os.path.isdir(local_dir):
        raise RuntimeError(f"candidate directory does not exist: {local_dir}")
    if os.environ.get("WORKSHOP_S3FILES_DIR"):
        dest = candidate_path(run_id)
        return copy_tree_files(local_dir, dest)
    return _put_archive(candidate_subdir(run_id), [(local_dir, "")], region)


def stage_base(run_id: str, local_dir: str,
               region: str | None = None) -> int:
    """Publish the latest integration branch as immutable role input."""
    if not os.path.isdir(local_dir):
        raise RuntimeError(f"base directory does not exist: {local_dir}")
    if os.environ.get("WORKSHOP_S3FILES_DIR"):
        dest = base_path(run_id)
        return copy_tree_files(local_dir, dest)
    return _put_archive(base_subdir(run_id), [(local_dir, "")], region)


def stage_refresh(run_id: str, work_id: str, local_dir: str,
                  region: str | None = None) -> int:
    """Publish an owner-specific rebased checkout for one stale role PR."""
    if not os.path.isdir(local_dir):
        raise RuntimeError(f"refresh directory does not exist: {local_dir}")
    if os.environ.get("WORKSHOP_S3FILES_DIR"):
        dest = refresh_path(run_id, work_id)
        return copy_tree_files(local_dir, dest)
    return _put_archive(
        refresh_subdir(run_id, work_id), [(local_dir, "")], region)


def stage_skills(run_id: str, skill_dirs: list[str],
                 region: str | None = None,
                 agent_id: str | None = None) -> int:
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
            n += copy_tree_files(d, dest)
        return n
    trees = [
        (directory, f"skills/{os.path.basename(directory.rstrip(os.sep))}")
        for directory in skill_dirs
    ]
    return _put_archive(skills_subdir(run_id, agent_id), trees, region)
