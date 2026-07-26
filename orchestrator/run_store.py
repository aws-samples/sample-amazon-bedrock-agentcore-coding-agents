"""Durable run state, so a verdict outlives the session that asked for it.

``run_status`` reads the coordinator process's in-memory registry, which means a
run is only answerable from the SAME AgentCore session that submitted it. An
expired or recycled session loses the verdict permanently: the build still
finished, the pull request still opened, but the attendee has no way to ask what
happened and the workshop page has to warn them about it.

This writes the same public result to durable storage on every phase change, and
reads it back by run id alone. The shape is deliberately the one the API already
returns (``engine.public_result``), so nothing new has to be learned or kept in
sync: the file IS the answer ``run_status`` would have given.

WHERE it writes depends on where the engine is running, and both are existing
seams rather than new infrastructure:

  * a local run (the box console, a dev run) writes under ``_RUNS_DIR/state/``,
    beside the ledger it already appends to;
  * the DEPLOYED coordinator additionally mirrors to the S3 bucket already wired
    into its environment as ``WORKSHOP_RUNTIME_BUCKET``, because its own
    filesystem is a container's ``/tmp`` that dies with the microVM.

Failure to persist is logged and never raised. A status file is a convenience for
reading history; it is not on the verdict path, and a run must not fail because a
bucket was unreachable.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

_STATE_PREFIX = "orchestrator/run-state"   # S3 key prefix for the mirrored copy


def _local_dir(runs_dir: str) -> str:
    return os.path.join(runs_dir, "state")


def _local_path(runs_dir: str, run_id: str) -> str:
    return os.path.join(_local_dir(runs_dir), f"{run_id}.json")


def _s3_key(run_id: str) -> str:
    return f"{_STATE_PREFIX}/{run_id}.json"


def _s3() -> tuple[Any, str] | None:
    """(client, bucket) when a durable mirror is configured, else None.

    Only the deployed coordinator has ``WORKSHOP_RUNTIME_BUCKET`` set by
    ``configure_deploy``, so a local run silently skips the mirror instead of
    inventing a bucket name and failing on it.
    """
    bucket = os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()
    if not bucket:
        return None
    try:
        import boto3  # noqa: PLC0415 (lazy, mirrors the other AWS seams)
        region = os.environ.get("WORKSHOP_BEDROCK_REGION",
                                os.environ.get("AWS_REGION", "us-west-2"))
        return boto3.client("s3", region_name=region), bucket
    except Exception:  # noqa: BLE001 (no SDK / no credentials: no mirror)
        return None


def save(runs_dir: str, run_id: str, payload: dict[str, Any],
         log=None) -> None:
    """Persist one run's public result. Never raises."""
    body = json.dumps({**payload, "_saved_at": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2)
    try:
        os.makedirs(_local_dir(runs_dir), exist_ok=True)
        tmp = _local_path(runs_dir, run_id) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        # Atomic replace, so a reader never sees a half-written status.
        os.replace(tmp, _local_path(runs_dir, run_id))
    except OSError as exc:
        if log:
            log(f"run state not written locally: {exc}", "warn")
    hit = _s3()
    if hit is None:
        return
    s3, bucket = hit
    try:
        s3.put_object(Bucket=bucket, Key=_s3_key(run_id),
                      Body=body.encode("utf-8"),
                      ContentType="application/json")
    except Exception as exc:  # noqa: BLE001 (durability is best effort)
        if log:
            log(f"run state not mirrored to s3://{bucket}: {exc}", "warn")


def load(runs_dir: str, run_id: str) -> dict[str, Any] | None:
    """The last persisted result for ``run_id``, or None if there is none.

    Local first (it is free and current), then the durable mirror, which is the
    case that matters: a NEW session has no local file for a run a previous
    coordinator container executed.
    """
    try:
        with open(_local_path(runs_dir, run_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    hit = _s3()
    if hit is None:
        return None
    s3, bucket = hit
    try:
        obj = s3.get_object(Bucket=bucket, Key=_s3_key(run_id))
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:  # noqa: BLE001 (absent or unreadable: no history)
        return None


def recent(runs_dir: str, limit: int = 10) -> list[dict[str, Any]]:
    """The most recent persisted runs, newest first.

    Lets an attendee who lost their session id ask "what did I run?" instead of
    having no way back to their own build. Local only: listing a bucket prefix on
    every call is a cost the answer does not justify, and the local directory is
    what the console and the box actually read.
    """
    out: list[dict[str, Any]] = []
    d = _local_dir(runs_dir)
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
        if len(out) >= limit:
            break
    return out
