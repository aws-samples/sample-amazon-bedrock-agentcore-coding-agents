"""One admitted build per repository, across coordinator sessions and entrypoints.

A run-state listing is useful evidence, but cannot grant exclusive admission:
two fresh microVMs can both read an empty listing. The engine instead conditionally
creates one repository claim before it starts its worker. Only that worker releases
the claim, after finalization. The claim is outside the agents' shared S3 mount.

There is deliberately no TTL or automatic stale-heartbeat takeover. A missing
heartbeat does not prove that the old worker and its role sessions have stopped.
An interrupted coordinator therefore leaves a claim for an operator to inspect and
release after stopping the old execution. Losing availability is preferable to
silently starting a second auto-merging team.

S3 uses If-None-Match on creation and the returned ETag on deletion. Every claim has
a fresh nonce, so a late release cannot delete a successor's claim. Local-only
fixture/development runs use the same persistent claim with a POSIX file guard.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any

import run_store

PREFIX = "orchestrator/repository-admission"
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepositoryBusy(RuntimeError):
    def __init__(self, repository: str, run_id: str):
        self.repository = repository
        self.run_id = run_id
        super().__init__(f"BUILD_ALREADY_RUNNING:{run_id}")


class AdmissionUnavailable(run_store.RunStateUnavailable):
    """No worker may start unless exclusive admission was positively acquired."""


def _unavailable(operation: str, exc: BaseException) -> AdmissionUnavailable:
    return AdmissionUnavailable(
        f"{operation}:{run_store._error_code(exc)}",
        "Repository admission could not be established; nothing was started.")


def key_for(repository: str) -> str:
    canonical = repository.strip().lower()
    if not _REPOSITORY.fullmatch(canonical):
        raise ValueError("Repository admission needs an owner/repository name")
    return f"{PREFIX}/{hashlib.sha256(canonical.encode()).hexdigest()}.json"


def _holder(body: bytes, repository: str) -> str:
    """An invalid claim is unknown, never permission to replace it."""
    try:
        value = json.loads(body)
        if (value.get("version") != 1 or value.get("repository") != repository
                or not _RUN_ID.fullmatch(value.get("run_id", ""))
                or not isinstance(value.get("nonce"), str)
                or not value["nonce"]):
            raise ValueError("invalid repository claim")
        return value["run_id"]
    except (TypeError, ValueError, AttributeError) as exc:
        raise _unavailable("ClaimRead", exc) from exc


def _mirror(require_mirror: bool) -> tuple[Any, str] | None:
    explicit = os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()
    if explicit:
        hit = run_store._s3()
        if hit is None:
            raise AdmissionUnavailable("S3ClientUnavailable",
                                       "The repository admission client is unavailable.")
        return hit
    # The attendee's Studio engine and the deployed coordinator must contend on
    # the SAME bucket. Discovery reads infra.config without changing the process's
    # run-history writer settings. Offline tests explicitly disable discovery.
    bucket = run_store.reader_mirror_bucket()
    if bucket:
        try:
            import boto3  # noqa: PLC0415
            region = (os.environ.get("WORKSHOP_BEDROCK_REGION")
                      or os.environ.get("AWS_REGION")
                      or os.environ.get("AWS_DEFAULT_REGION") or None)
            return boto3.client("s3", region_name=region), bucket
        except Exception as exc:  # noqa: BLE001
            raise _unavailable("S3Client", exc) from exc
    if require_mirror:
        raise AdmissionUnavailable(
            "NoAdmissionMirror",
            "No shared admission bucket is configured for this repository.")
    return None


@contextmanager
def _file_guard(path: Path):
    """Serialize local compare/create/delete; the guard inode is never removed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".guard", "a+b") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(guard, fcntl.LOCK_UN)


@dataclass
class Claim:
    repository: str
    run_id: str
    key: str
    body: bytes
    etag: str = ""
    client: Any = None
    bucket: str = ""
    path: Path | None = None
    released: bool = False

    def release(self) -> None:
        """Release only this exact claim. Failure leaves admission closed."""
        if self.released:
            return
        if self.client is not None:
            try:
                self.client.delete_object(Bucket=self.bucket, Key=self.key,
                                          IfMatch=self.etag)
            except Exception as exc:  # noqa: BLE001
                # A missing claim or a successor with another ETag means this
                # claim no longer owns admission. Never retry an unconditional
                # delete, including after an ambiguous network response.
                if run_store._error_code(exc) not in {
                        "NoSuchKey", "NotFound", "404", "PreconditionFailed", "412"}:
                    raise _unavailable("ClaimRelease", exc) from exc
        else:
            try:
                with _file_guard(self.path):
                    try:
                        current = self.path.read_bytes()
                    except FileNotFoundError:
                        current = None
                    if current == self.body:
                        self.path.unlink()
            except OSError as exc:
                raise _unavailable("ClaimRelease", exc) from exc
        self.released = True


def acquire(repository: str, run_id: str, runs_dir: str, *,
            require_mirror: bool = True) -> Claim:
    """Acquire once or refuse. Never wait, retry a build, or steal an old claim."""
    repository = repository.strip().lower()
    key = key_for(repository)
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid run ID for repository admission")
    body = json.dumps({
        "version": 1, "repository": repository, "run_id": run_id,
        "nonce": secrets.token_hex(32),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, sort_keys=True).encode()
    hit = _mirror(require_mirror)
    if hit is None:
        path = Path(runs_dir, "repository-admission", key.rsplit("/", 1)[-1])
        try:
            with _file_guard(path):
                if path.exists():
                    raise RepositoryBusy(repository, _holder(path.read_bytes(), repository))
                # The file guard keeps another process from observing partial
                # bytes. A process death may leave an invalid claim; fail closed.
                with open(path, "xb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise _unavailable("ClaimCreate", exc) from exc
        return Claim(repository, run_id, key, body, path=path)

    client, bucket = hit
    try:
        response = client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json",
            ServerSideEncryption="AES256", IfNoneMatch="*")
    except Exception as exc:  # noqa: BLE001
        if run_store._error_code(exc) in {"PreconditionFailed", "412"}:
            try:
                obj = client.get_object(Bucket=bucket, Key=key)
                holder = _holder(obj["Body"].read(), repository)
            except Exception as read_exc:  # noqa: BLE001
                # It might have finished between the failed create and this read.
                # That is a refusal for THIS attempt, never an implicit retry.
                raise _unavailable("ClaimRead", read_exc) from read_exc
            raise RepositoryBusy(repository, holder) from exc
        # A timeout may mean the PUT succeeded. Leave the claim alone: no worker
        # starts, and a later invocation must not overwrite an uncertain claim.
        raise _unavailable("ClaimCreate", exc) from exc
    etag = str(response.get("ETag") or "")
    if not etag:
        raise AdmissionUnavailable("ClaimCreate:NoETag",
                                   "The admission claim has no safe release token.")
    return Claim(repository, run_id, key, body, etag=etag, client=client, bucket=bucket)
