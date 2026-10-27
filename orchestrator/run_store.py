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
bucket was unreachable. Reading history is best effort for the same reason, with
one exception: ``active_runs`` answers "is a build active?" for a caller that must
fail closed, so it raises ``RunStateUnavailable`` on anything it cannot read.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import threading
import time
from typing import Any

_STATE_PREFIX = "orchestrator/run-state"   # S3 key prefix for the mirrored copy
# A live engine refreshes active snapshots every 30 seconds. Four missed writes is
# long enough to avoid declaring a slow S3 request dead, but short enough that a
# recycled Runtime does not leave an attendee staring at "running" forever.
ACTIVE_STALE_AFTER_S = int(os.environ.get(
    "WORKSHOP_RUN_STATE_STALE_AFTER_S", "120"))


def _local_dir(runs_dir: str) -> str:
    return os.path.join(runs_dir, "state")


def _local_path(runs_dir: str, run_id: str) -> str:
    return os.path.join(_local_dir(runs_dir), f"{run_id}.json")


def _s3_key(run_id: str) -> str:
    return f"{_STATE_PREFIX}/{run_id}.json"


def reader_mirror_bucket() -> str:
    """The mirror a READ-ONLY tool should look in when nothing told it where.

    Writers are TOLD their bucket: ``configure_deploy`` puts
    ``WORKSHOP_RUNTIME_BUCKET`` in the deployed coordinator's environment, which is
    the only process that mirrors. A reader on the workshop host has no such
    variable, and a live run proved what that costs: with a build underway and its
    snapshot already in the mirror, the taught command
    ``python3 orchestrator/watch_run.py`` answered "no runs found", because the
    coordinator's state was in S3 and the watcher only knew about local disk.

    So a reader resolves the same bucket the writer was given, from the shared
    infrastructure config (``runtime_stage``), rather than a second literal here. Returns ""
    when it cannot be resolved (no SDK, no credentials, no region), because a
    watcher that cannot reach the mirror must still show local runs instead of
    failing.

    This is deliberately NOT wired into ``_s3()``. Making every caller resolve a
    bucket would also point the box console's own run list at the deployed
    coordinator's runs, and the two registries are separate on purpose: the console
    hosts the engine in-process and cannot stream a run it did not dispatch.
    """
    explicit = os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()
    if explicit:
        return explicit
    if os.environ.get("WORKSHOP_RUN_MIRROR_DISCOVERY", "1") == "0":
        # The offline suite turns discovery off: a developer laptop with a real
        # infra.config otherwise resolved its REAL bucket, and fixture runs were
        # mirrored into it (observed 2026-09-27, 25 fixture records).
        return ""
    try:
        import runtime_stage  # noqa: PLC0415 (lazy: keeps the offline import light)
        return runtime_stage.runtime_bucket()
    except Exception:  # noqa: BLE001 (no SDK / no credentials / no region)
        return ""


def attach_reader_mirror() -> str:
    """Point THIS read-only process at the deployed coordinator's mirror, once.

    The watcher, the progress check, and the diagnostic bundle are separate host
    processes (never the console, whose run list stays its own). Each needs the
    same resolution; the diagnostic bundle lacked it and answered "none recorded"
    for every Lab 2 build. Returns the bucket in use, or "" for local disk only.
    """
    if not os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip():
        bucket = reader_mirror_bucket()
        if bucket:
            os.environ["WORKSHOP_RUNTIME_BUCKET"] = bucket
    return os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()


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
        # Ambient region, then boto3's own resolver. Never a literal: a hardcoded
        # region mirrors run state into the wrong region's endpoint.
        region = (os.environ.get("WORKSHOP_BEDROCK_REGION")
                  or os.environ.get("AWS_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION") or None)
        return boto3.client("s3", region_name=region), bucket
    except Exception:  # noqa: BLE001 (no SDK / no credentials: no mirror)
        return None


def save(runs_dir: str, run_id: str, payload: dict[str, Any],
         log=None) -> None:
    """Persist one run's public result. Never raises."""
    # default=str, and inside the guard: this ran ABOVE the first try, so one value
    # json could not encode raised TypeError straight out of a function whose whole
    # contract is not to. The caller is the liveness heartbeat, and an unwritten
    # snapshot is read two minutes later as COORDINATOR_SESSION_INTERRUPTED, whose
    # next_action tells the attendee to resubmit a build that is still running. A
    # degraded field is worth vastly more than a lost pulse.
    try:
        body = json.dumps({**payload, "_saved_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        if log:
            log(f"run state not serializable, snapshot skipped: {exc}", "warn")
        return
    try:
        os.makedirs(_local_dir(runs_dir), exist_ok=True)
        # A heartbeat and the terminal write can land together. Give each writer
        # its own temporary path so one atomic replace cannot remove another
        # thread's still-open temp file.
        tmp = (_local_path(runs_dir, run_id)
               + f".tmp.{os.getpid()}.{threading.get_ident()}")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        # Atomic replace, so a reader never sees a half-written status.
        os.replace(tmp, _local_path(runs_dir, run_id))
        prune(runs_dir)
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


def save_check(runs_dir: str, run_id: str, work_id: str, body: bytes,
               log=None) -> dict[str, Any]:
    """Keep the exact executable outside the disposable checkout and microVM.

    A digest and a source excerpt identify a check but cannot re-run it. Store
    its full bytes under that digest. This records evidence only: storage failure
    never changes the executable's verdict, and a URI is returned only after the
    corresponding write succeeds.
    """
    digest = hashlib.sha256(body).hexdigest()
    evidence: dict[str, Any] = {"sha256": digest, "bytes": len(body)}
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (run_id, work_id)):
        if log:
            log("check evidence not stored: invalid run or work identifier", "warn")
        return evidence
    relative = os.path.join("evidence", run_id, work_id, digest, "acceptance_check")
    path = os.path.join(runs_dir, relative)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(temporary, "wb") as handle:
            handle.write(body)
        os.replace(temporary, path)
        evidence["local_path"] = relative
    except OSError as exc:
        if log:
            log(f"check evidence not written locally: {exc}", "warn")
    hit = _s3()
    if hit is not None:
        s3, bucket = hit
        key = f"orchestrator/run-evidence/{run_id}/{work_id}/{digest}/acceptance_check"
        try:
            s3.put_object(
                Bucket=bucket, Key=key, Body=body,
                ContentType="application/octet-stream", ServerSideEncryption="AES256",
                Metadata={"sha256": digest, "run-id": run_id, "work-id": work_id})
            evidence["s3_uri"] = f"s3://{bucket}/{key}"
        except Exception as exc:  # noqa: BLE001 (evidence storage cannot grade work)
            if log:
                log(f"check evidence not mirrored to s3://{bucket}: {exc}", "warn")
    return evidence


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


def active_snapshot_is_stale(payload: dict[str, Any],
                             now: float | None = None) -> bool:
    """True when a persisted active run has stopped receiving heartbeats.

    A coordinator Runtime can be recycled after the chat invocation returns. Its
    worker thread disappears with the microVM, so the last honest state is
    non-terminal even though no process can advance it. Treat that as an explicit
    interruption instead of reporting "running" forever.
    """
    if payload.get("status") not in ("queued", "running"):
        return False
    saved_at = str(payload.get("_saved_at") or "")
    try:
        stamp = calendar.timegm(time.strptime(
            saved_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (OverflowError, ValueError):
        return False
    return (now if now is not None else time.time()) - stamp > ACTIVE_STALE_AFTER_S


def recent(runs_dir: str, limit: int | None = 10) -> list[dict[str, Any]]:
    """The most recent persisted runs, newest first.

    Lets an attendee who lost their session id ask "what did I run?" instead of
    having no way back to their own build. Read both the local state directory and,
    when configured, the S3 mirror. A new deployed coordinator session has a fresh
    ``/tmp`` and therefore MUST list the mirror; otherwise ``run_status <known-id>``
    works while the recovery command ``list_runs`` falsely returns an empty list.
    This listing happens only on an explicit history request.

    Ordered by local MTIME or S3 LastModified, not by name. A run id starts with
    ``run_<HHMMSS>`` -- time of day with no date -- so sorting filenames puts last
    night's 23:59 run ahead of this morning's 00:05 one.
    """
    # None lists the retained history for the console's paginated index. Pruning
    # still bounds the files on disk; pagination must not lose older saved runs.
    if limit is not None and limit <= 0:
        return []

    # run_id -> (modified timestamp, already-loaded payload, S3 key)
    candidates: dict[str, tuple[float, dict[str, Any] | None, str | None]] = {}
    d = _local_dir(runs_dir)
    try:
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            try:
                modified = os.path.getmtime(path)
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            run_id = str(payload.get("run_id") or name[:-5])
            candidates[run_id] = (modified, payload, None)
    except OSError:
        pass

    hit = _s3()
    if hit is not None:
        s3, bucket = hit
        try:
            token: str | None = None
            while True:
                args: dict[str, Any] = {
                    "Bucket": bucket,
                    "Prefix": _STATE_PREFIX + "/",
                    "MaxKeys": 1000,
                }
                if token:
                    args["ContinuationToken"] = token
                page = s3.list_objects_v2(**args)
                for obj in page.get("Contents", []):
                    key = str(obj.get("Key") or "")
                    if not key.endswith(".json"):
                        continue
                    run_id = os.path.basename(key)[:-5]
                    stamp = obj.get("LastModified")
                    modified = float(stamp.timestamp()) if hasattr(
                        stamp, "timestamp") else 0.0
                    current = candidates.get(run_id)
                    if current is None or modified > current[0]:
                        candidates[run_id] = (modified, None, key)
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
                if not token:
                    break
        except Exception:
            # History is best effort and never changes a build verdict.
            pass

    out: list[dict[str, Any]] = []
    for _modified, payload, key in sorted(
            candidates.values(), key=lambda row: row[0], reverse=True):
        if payload is None and key and hit is not None:
            s3, bucket = hit
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                payload = json.loads(obj["Body"].read().decode("utf-8"))
            except Exception:
                continue
        if payload is None:
            continue
        out.append(payload)
        if limit is not None and len(out) >= limit:
            break
    return out


class RunStateUnavailable(RuntimeError):
    """The saved run state could not be read, so "no build is active" is unknown.

    ``code`` is short and safe to show anywhere (an operation and an AWS error code,
    never a bucket, ARN, or account); the original exception is chained as
    ``__cause__`` for the log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error_code(exc: BaseException) -> str:
    """An AWS error code (``AccessDenied``) or the exception type; never its message."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
            return code
    return type(exc).__name__


# A mirrored record last written longer ago than this cannot belong to a live run:
# an active run rewrites it every heartbeat, and ``active_snapshot_is_stale`` gives up
# on it after ACTIVE_STALE_AFTER_S. The slack covers clock skew between the writer,
# S3's LastModified, and the reader, so a fresh record is never skipped unread.
_ACTIVE_SKEW_SLACK_S = 600


def active_runs(runs_dir: str, *, require_mirror: bool = False,
                now: float | None = None) -> list[dict[str, Any]]:
    """Every saved run still queued or running with a fresh heartbeat, newest first.

    The STRICT reader, for a decision that must fail closed: the `/develop` event
    path refuses to start a build while it can see another one. ``recent`` is a
    history view and skips what it cannot read; read here, a skipped record would be
    "nothing is running", so every doubt raises ``RunStateUnavailable`` instead: an
    S3 list or get failure, an S3 client that cannot be built, an unreadable local
    record, and, with ``require_mirror``, no mirror bucket at all. That last one is
    the deployed coordinator's case: each `/develop` gets a NEW session, a fresh
    microVM whose local disk holds only its own runs, so without the mirror it
    cannot see a build another session started.

    Only records written recently enough to be live are fetched. This is a check,
    not a lock: two sessions reading at the same moment can both see nothing.
    """
    stamp_now = now if now is not None else time.time()
    horizon = stamp_now - ACTIVE_STALE_AFTER_S - _ACTIVE_SKEW_SLACK_S
    # run_id -> (modified timestamp, local path or None, S3 key or None)
    candidates: dict[str, tuple[float, str | None, str | None]] = {}

    d = _local_dir(runs_dir)
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        names = []
    except OSError as exc:
        raise RunStateUnavailable(
            f"LocalList:{_error_code(exc)}",
            f"listing the local run state failed ({_error_code(exc)})") from exc
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            modified = os.path.getmtime(path)
        except FileNotFoundError:
            continue  # pruned or replaced between the listing and here
        except OSError as exc:
            raise RunStateUnavailable(
                f"LocalStat:{_error_code(exc)}",
                f"reading the local run state failed ({_error_code(exc)})") from exc
        if modified >= horizon:
            candidates[name[:-5]] = (modified, path, None)

    bucket = os.environ.get("WORKSHOP_RUNTIME_BUCKET", "").strip()
    hit = None
    if not bucket:
        if require_mirror:
            raise RunStateUnavailable(
                "NoRunStateMirror",
                "no run-state mirror is configured (WORKSHOP_RUNTIME_BUCKET is unset), "
                "so builds started by other coordinator sessions cannot be seen")
    else:
        hit = _s3()
        if hit is None:
            raise RunStateUnavailable(
                "S3ClientUnavailable", "the S3 client for the run-state mirror could "
                "not be created")
    if hit is not None:
        s3, bucket = hit
        token: str | None = None
        while True:
            args: dict[str, Any] = {"Bucket": bucket, "Prefix": _STATE_PREFIX + "/",
                                    "MaxKeys": 1000}
            if token:
                args["ContinuationToken"] = token
            try:
                page = s3.list_objects_v2(**args)
            except Exception as exc:  # noqa: BLE001 (unknown means busy, never idle)
                raise RunStateUnavailable(
                    f"ListObjectsV2:{_error_code(exc)}",
                    f"listing the run-state mirror failed ({_error_code(exc)})") from exc
            for obj in page.get("Contents", []):
                key = str(obj.get("Key") or "")
                if not key.endswith(".json"):
                    continue
                stamp = obj.get("LastModified")
                # No timestamp: it cannot be ruled out, so it is read.
                modified = (float(stamp.timestamp()) if hasattr(stamp, "timestamp")
                            else stamp_now)
                if modified < horizon:
                    continue
                run_id = os.path.basename(key)[:-5]
                current = candidates.get(run_id)
                if current is None or modified > current[0]:
                    candidates[run_id] = (modified, None, key)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                raise RunStateUnavailable(
                    "ListObjectsV2:NoContinuationToken",
                    "the run-state mirror listing was truncated with no continuation token")

    out: list[tuple[float, dict[str, Any]]] = []
    for modified, path, key in candidates.values():
        if path is not None:
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                raise RunStateUnavailable(
                    f"LocalRead:{_error_code(exc)}",
                    f"a local run-state record could not be read ({_error_code(exc)})"
                ) from exc
        else:
            s3, bucket = hit  # type: ignore[misc]  (a key implies a mirror)
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                payload = json.loads(obj["Body"].read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 (an unread record may be running)
                raise RunStateUnavailable(
                    f"GetObject:{_error_code(exc)}",
                    f"a mirrored run-state record could not be read ({_error_code(exc)})"
                ) from exc
        if not isinstance(payload, dict):
            raise RunStateUnavailable("RecordNotAnObject",
                                      "a run-state record is not a JSON object")
        if (payload.get("status") in ("queued", "running")
                and not active_snapshot_is_stale(payload, now=stamp_now)):
            out.append((modified, payload))
    return [payload for _modified, payload in sorted(out, key=lambda row: row[0],
                                                       reverse=True)]


# Cap how many status files accumulate. Each is a couple of KB, but nothing ever
# deleted them, so a long-lived box (or a test suite) grows the directory without
# limit -- the same unbounded-growth shape `_MAX_WORK_DIRS` already guards for build
# trees. Keep the newest N; overridable for an operator who wants deeper history.
_MAX_STATE_FILES = int(os.environ.get("WORKSHOP_MAX_RUN_STATE", "200"))


def prune(runs_dir: str, keep: int | None = None) -> None:
    """Delete all but the ``keep`` newest status files. Best effort, never raises.

    ``keep`` defaults to ``_MAX_STATE_FILES`` read AT CALL TIME, not baked into the
    signature: a default argument is evaluated once at import, which would make the
    cap unchangeable after this module loads.
    """
    if keep is None:
        keep = _MAX_STATE_FILES
    if keep < 0:
        return
    d = _local_dir(runs_dir)
    try:
        entries = []
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            path = os.path.join(d, name)
            try:
                entries.append((os.path.getmtime(path), path))
            except OSError:
                continue
    except OSError:
        return
    for _mtime, path in sorted(entries, reverse=True)[keep:]:
        try:
            os.remove(path)
        except OSError:
            continue
