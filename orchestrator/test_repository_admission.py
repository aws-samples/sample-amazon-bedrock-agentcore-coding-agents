"""Real admission/lifecycle code against an atomic object-store test double.

No model, GitHub, or AWS calls. Race tests synchronize competing callers before
the conditional write; they do not serialize callers in the code under test.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat  # noqa: E402
import engine  # noqa: E402
import repository_admission as admission  # noqa: E402
import run_store  # noqa: E402

REPOSITORY = "octo-cat/agent-review"
BUCKET = "test-repository-admission"


def error(code, operation="PutObject"):
    return ClientError({"Error": {"Code": code, "Message": "test failure"}}, operation)


class ObjectStore:
    """S3's conditional-create and compare-delete behavior, atomic across clients."""
    def __init__(self):
        self.lock = threading.Lock()
        self.objects = {}
        self.calls = []
        self.put_error = None
        self.get_error = None
        self.delete_error = None
        self.lose_put_response = False

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, **kwargs):
        assert Bucket == BUCKET
        with self.lock:
            self.calls.append(("put", Key, IfNoneMatch))
            if self.put_error:
                raise self.put_error
            if IfNoneMatch == "*" and Key in self.objects:
                raise error("PreconditionFailed")
            body = Body if isinstance(Body, bytes) else Body.encode()
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            self.objects[Key] = (body, etag)
            if self.lose_put_response:
                raise ConnectionError("PUT may already have succeeded")
            return {"ETag": etag}

    def get_object(self, Bucket, Key):
        assert Bucket == BUCKET
        with self.lock:
            if self.get_error:
                raise self.get_error
            if Key not in self.objects:
                raise error("NoSuchKey", "GetObject")
            body, etag = self.objects[Key]
            return {"Body": io.BytesIO(body), "ETag": etag}

    def delete_object(self, Bucket, Key, IfMatch=None):
        assert Bucket == BUCKET
        with self.lock:
            self.calls.append(("delete", Key, IfMatch))
            if self.delete_error:
                raise self.delete_error
            if Key not in self.objects:
                raise error("NoSuchKey", "DeleteObject")
            if self.objects[Key][1] != IfMatch:
                raise error("PreconditionFailed", "DeleteObject")
            del self.objects[Key]
            return {}

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        assert Bucket == BUCKET
        with self.lock:
            return {"IsTruncated": False, "Contents": [
                {"Key": key, "LastModified": datetime.now(timezone.utc)}
                for key in self.objects if key.startswith(Prefix)]}


@pytest.fixture
def store(monkeypatch):
    store = ObjectStore()
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", BUCKET)
    monkeypatch.setattr(run_store, "_s3", lambda: (store, BUCKET))
    return store


def acquire(tmp_path, run_id="run_first"):
    return admission.acquire(REPOSITORY, run_id, str(tmp_path))


def test_two_independent_clients_have_exactly_one_winner(store, tmp_path):
    barrier = threading.Barrier(2)

    def contender(number):
        barrier.wait(timeout=5)
        try:
            return acquire(tmp_path, f"run_{number}")
        except admission.RepositoryBusy as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, range(2)))
    [winner] = [r for r in results if isinstance(r, admission.Claim)]
    [refused] = [r for r in results if isinstance(r, admission.RepositoryBusy)]
    assert refused.run_id == winner.run_id
    assert len(store.objects) == 1
    assert all(call[2] == "*" for call in store.calls if call[0] == "put")
    winner.release()


def test_claim_is_case_insensitive_and_outside_the_agent_mount(store, tmp_path):
    claim = acquire(tmp_path)
    assert claim.key.startswith("orchestrator/repository-admission/")
    with pytest.raises(admission.RepositoryBusy):
        admission.acquire(REPOSITORY.upper(), "run_other", str(tmp_path))
    claim.release()


def test_no_heartbeat_or_age_can_expire_a_claim(store, tmp_path, monkeypatch):
    claim = acquire(tmp_path)
    # A missing/stale history record is not a claim-release signal.
    monkeypatch.setattr(admission.time, "time", lambda: 10 ** 12)
    with pytest.raises(admission.RepositoryBusy) as refused:
        acquire(tmp_path, "run_after_years")
    assert refused.value.run_id == claim.run_id
    claim.release()


def test_a_late_holder_cannot_delete_the_successor(store, tmp_path):
    old = acquire(tmp_path)
    store.delete_object(Bucket=BUCKET, Key=old.key, IfMatch=old.etag)
    successor = acquire(tmp_path, "run_successor")
    assert old.etag != successor.etag
    old.release()
    assert successor.key in store.objects
    successor.release()


def test_successful_release_allows_exactly_the_next_explicit_request(store, tmp_path):
    first = acquire(tmp_path)
    first.release()
    first.release()  # local repeat is a no-op
    next_claim = acquire(tmp_path, "run_next")
    assert next_claim.run_id == "run_next"
    next_claim.release()


@pytest.mark.parametrize("failure", [
    error("AccessDenied"), error("SlowDown"), error("ConditionalRequestConflict"),
    ConnectionError("network reset"),
])
def test_failed_create_never_falls_back_to_an_unlocked_run(store, tmp_path, failure):
    store.put_error = failure
    with pytest.raises(admission.AdmissionUnavailable):
        acquire(tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_an_ambiguous_put_does_not_erase_a_claim_or_retry(store, tmp_path):
    store.lose_put_response = True
    with pytest.raises(admission.AdmissionUnavailable):
        acquire(tmp_path)
    assert len(store.objects) == 1
    assert len(store.calls) == 1
    store.lose_put_response = False
    with pytest.raises(admission.RepositoryBusy):
        acquire(tmp_path, "run_second")


def test_an_unreadable_holder_is_unknown_never_idle(store, tmp_path):
    first = acquire(tmp_path)
    store.get_error = error("AccessDenied", "GetObject")
    with pytest.raises(admission.AdmissionUnavailable):
        acquire(tmp_path, "run_second")
    assert len(store.objects) == 1
    first.release()


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"version": 1}', b"null"])
def test_a_corrupt_claim_is_not_stolen(store, tmp_path, body):
    key = admission.key_for(REPOSITORY)
    store.objects[key] = (body, '"old"')
    with pytest.raises(admission.AdmissionUnavailable):
        acquire(tmp_path)
    assert store.objects[key][0] == body


def test_release_failure_preserves_the_claim(store, tmp_path):
    claim = acquire(tmp_path)
    store.delete_error = error("AccessDenied", "DeleteObject")
    with pytest.raises(admission.AdmissionUnavailable):
        claim.release()
    assert not claim.released
    assert claim.key in store.objects
    with pytest.raises(admission.RepositoryBusy):
        acquire(tmp_path, "run_second")


def test_no_mirror_cannot_admit_a_deployed_build(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET", raising=False)
    monkeypatch.setattr(run_store, "reader_mirror_bucket", lambda: "")
    with pytest.raises(admission.AdmissionUnavailable, match="shared admission bucket"):
        acquire(tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_host_and_deployed_coordinator_use_the_same_bucket_without_history_env_mutation(
        monkeypatch, tmp_path, store):
    deployed = acquire(tmp_path)
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET")
    monkeypatch.setattr(run_store, "reader_mirror_bucket", lambda: BUCKET)
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: store)
    with pytest.raises(admission.RepositoryBusy) as refused:
        acquire(tmp_path, "run_host")
    assert refused.value.run_id == deployed.run_id
    assert "WORKSHOP_RUNTIME_BUCKET" not in os.environ
    deployed.release()


def _local_contender(runs_dir, number, ready, go, results):
    # Spawned processes have no module monkeypatches and no AWS configuration.
    os.environ.pop("WORKSHOP_RUNTIME_BUCKET", None)
    os.environ["WORKSHOP_RUN_MIRROR_DISCOVERY"] = "0"
    ready.put(number)
    go.wait(10)
    try:
        claim = admission.acquire(REPOSITORY, f"run_{number}", runs_dir,
                                  require_mirror=False)
        results.put(("acquired", claim.run_id))
        # Intentionally exit without release: death must leave the claim closed.
    except admission.RepositoryBusy as exc:
        results.put(("busy", exc.run_id))


def test_local_claim_is_cross_process_and_survives_holder_exit(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET", raising=False)
    monkeypatch.setenv("WORKSHOP_RUN_MIRROR_DISCOVERY", "0")
    ctx = multiprocessing.get_context("spawn")
    ready, results, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [ctx.Process(target=_local_contender,
                             args=(str(tmp_path), i, ready, go, results))
                 for i in range(2)]
    try:
        for proc in processes:
            proc.start()
        assert {ready.get(timeout=20), ready.get(timeout=20)} == {0, 1}
        go.set()
        outcomes = [results.get(timeout=20), results.get(timeout=20)]
        for proc in processes:
            proc.join(20)
            assert proc.exitcode == 0
        assert sorted(value[0] for value in outcomes) == ["acquired", "busy"]
        assert outcomes[0][1] == outcomes[1][1]
        with pytest.raises(admission.RepositoryBusy):
            admission.acquire(REPOSITORY, "run_after_crash", str(tmp_path),
                              require_mirror=False)
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join()


@pytest.fixture
def live_engine(monkeypatch, tmp_path, store):
    """Actual Engine.submit/_drive lifecycle, with execution held at admission."""
    monkeypatch.setenv("GITHUB_REPO", REPOSITORY)
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", "/unused-test-mount")
    monkeypatch.setattr(engine, "_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(engine.github, "comment_on_origin",
                        lambda *args, **kwargs: {"skipped": "offline test"})
    eng = engine.Engine(executor_obj=SimpleNamespace(name="agentcore"))
    entered, finish, released = threading.Event(), threading.Event(), threading.Event()

    def phase(run):
        run.status = "running"
        entered.set()
        assert finish.wait(10), "test did not allow the worker to finish"
        run.status, run.fail_reason = "needs_human", "ITERATION_CAP"
        return False

    eng._admission = phase
    original_release = eng._release_repository

    def release(run):
        original_release(run)
        released.set()

    eng._release_repository = release
    try:
        yield eng, entered, finish, released
    finally:
        finish.set()
        if entered.is_set():
            assert released.wait(10), "worker did not finish"


def test_actual_engine_holds_claim_until_worker_finishes_even_if_status_says_terminal(
        live_engine, store):
    eng, entered, finish, released = live_engine
    first = eng.submit("first request")
    assert entered.wait(5)
    first.status = "needs_human"  # a stale/reconciled status is not worker completion
    second_engine = engine.Engine(executor_obj=SimpleNamespace(name="agentcore"))
    with pytest.raises(admission.RepositoryBusy) as refused:
        second_engine.submit("different request")
    assert refused.value.run_id == first.run_id
    assert second_engine.list() == []
    finish.set()
    assert released.wait(5)
    assert first.status == "needs_human" and first.fail_reason == "ITERATION_CAP"
    assert admission.key_for(REPOSITORY) not in store.objects


def test_simultaneous_owner_events_get_one_real_engine_submission(
        live_engine, monkeypatch):
    eng, entered, finish, released = live_engine
    monkeypatch.setattr(chat, "ENGINE", eng)
    barrier = threading.Barrier(2)
    original_precheck = chat._running_build

    def both_observe_idle():
        assert original_precheck() is None
        barrier.wait(timeout=5)  # reproduces the old empty-listing race exactly
        return None

    monkeypatch.setattr(chat, "_running_build", both_observe_idle)
    event = {
        "kind": "develop", "owner": "octo-cat", "repo": "agent-review",
        "issue_number": 7, "author": "octocat", "idea": "a storm lighthouse",
        "comment_url": "https://github.com/octo-cat/agent-review/issues/7#issuecomment-1",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(
            lambda n: chat.start_event_build({**event, "idea": event["idea"] + str(n)}),
            range(2)))
    assert entered.wait(5)
    [started] = [r for r in replies if r.get("status") == "started"]
    [refused] = [r for r in replies if r.get("error") == "BUILD_ALREADY_RUNNING"]
    assert refused["running_run_id"] == started["run_id"]
    assert len(eng.list()) == 1
    finish.set()
    assert released.wait(5)
    assert len(eng.list()) == 1  # a red terminal result never resubmits itself


def test_worker_start_failure_releases_claim_and_does_not_leave_active_history(
        monkeypatch, live_engine, store):
    eng, entered, finish, released = live_engine

    def cannot_start(self):
        raise RuntimeError("thread limit")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    with pytest.raises(RuntimeError, match="thread limit"):
        eng.submit("will not start")
    assert eng.list() == [] and not entered.is_set()
    assert admission.key_for(REPOSITORY) not in store.objects
    assert run_store.active_runs(engine._RUNS_DIR, require_mirror=True) == []


def test_lost_claim_put_response_never_starts_a_worker(live_engine, store):
    eng, entered, finish, released = live_engine
    store.lose_put_response = True
    with pytest.raises(admission.AdmissionUnavailable):
        eng.submit("uncertain admission")
    assert not entered.is_set() and eng.list() == []
    assert admission.key_for(REPOSITORY) in store.objects


def test_release_failure_does_not_change_an_executed_red_result(live_engine, store):
    eng, entered, finish, released = live_engine
    run = eng.submit("first request")
    assert entered.wait(5)
    store.delete_error = error("AccessDenied", "DeleteObject")
    finish.set()
    assert released.wait(5)
    assert run.status == "needs_human" and run.fail_reason == "ITERATION_CAP"
    assert any("admission release failed" in e["message"] for e in run.events)
    assert admission.key_for(REPOSITORY) in store.objects


def test_a_settings_change_cannot_redirect_the_admitted_worker(
        live_engine, monkeypatch, store):
    eng, entered, finish, released = live_engine
    monkeypatch.setenv("GITHUB_GATEWAY_URL", "https://gateway.example")
    monkeypatch.setattr(engine.github, "_load_config_file",
                        lambda: {"region": "us-west-2"})
    seen = []

    def phase(run):
        entered.set()
        assert finish.wait(10)
        seen.append(engine.github.configured_repository())
        seen.append(engine.github._gateway_config()["repo"])
        run.status = "passed"
        return False

    eng._admission = phase
    eng.submit("build in the selected repository")
    assert entered.wait(5)
    monkeypatch.setenv("GITHUB_REPO", "someone/different-repository")
    finish.set()
    assert released.wait(5)
    assert seen == [REPOSITORY, REPOSITORY]
    assert engine.github.configured_repository() == "someone/different-repository"


def test_an_empty_submission_target_cannot_gain_a_repository_later(
        live_engine, monkeypatch):
    eng, entered, finish, released = live_engine
    monkeypatch.delenv("GITHUB_REPO")
    monkeypatch.setenv("GITHUB_GATEWAY_URL", "https://gateway.example")
    seen = []

    def phase(run):
        entered.set()
        assert finish.wait(10)
        seen.append(engine.github.configured_repository())
        seen.append(engine.github._gateway_config())
        run.status = "failed"
        return False

    eng._admission = phase
    eng.submit("no target selected")
    assert entered.wait(5)
    monkeypatch.setenv("GITHUB_REPO", REPOSITORY)
    finish.set()
    assert released.wait(5)
    assert seen == ["", None]


def test_event_keeps_validated_repository_if_settings_change_before_submit(
        live_engine, monkeypatch, store):
    eng, entered, finish, released = live_engine
    monkeypatch.setattr(chat, "ENGINE", eng)

    def edit_settings_after_validation():
        monkeypatch.setenv("GITHUB_REPO", "different/application")
        return None

    monkeypatch.setattr(chat, "_running_build", edit_settings_after_validation)
    reply = chat.start_event_build({
        "kind": "develop", "owner": "octo-cat", "repo": "agent-review",
        "issue_number": 7, "author": "octocat", "idea": "a storm lighthouse",
    })
    assert reply.get("run_id"), reply
    [run] = eng.list()
    assert run._submitted_repository == REPOSITORY
    assert run._repository_claim.repository == REPOSITORY
    assert admission.key_for("different/application") not in store.objects
    finish.set()
    assert released.wait(5)


def test_unknown_remote_completion_retains_claim_and_reports_exact_sessions(
        live_engine, store):
    eng, entered, finish, released = live_engine
    run = eng.submit("first request")
    assert entered.wait(5)
    uncertain = {
        "agent": "codex",
        "runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
        "session_id": "owned-unconfirmed-00000000000000000000",
    }
    run._unsettled_runtime_sessions.append(uncertain)
    finish.set()
    assert released.wait(5)
    assert admission.key_for(REPOSITORY) in store.objects
    assert run.status == "needs_human"
    assert run.fail_reason == "REMOTE_EXECUTION_UNSETTLED:ITERATION_CAP"
    saved = run_store.load(engine._RUNS_DIR, run.run_id)
    assert saved["unsettled_runtime_sessions"] == [uncertain]
    assert not saved["resubmission_allowed"]
    assert "Do not resubmit" in saved["next_action"]
    with pytest.raises(admission.RepositoryBusy):
        eng.submit("a different request still cannot overlap")


@pytest.mark.parametrize("attempted,exited,expected", [
    (False, False, ConnectionError),
    (True, False, "unsettled"),
    (True, True, ConnectionError),
])
def test_shell_failure_tracks_delivery_and_actual_exit(
        monkeypatch, attempted, exited, expected):
    import asyncio
    import runtime_exec

    async def lose_connection(*args):
        args[-1].update(attempted=attempted, exited=exited)
        raise ConnectionError("test disconnect")

    monkeypatch.setattr(runtime_exec, "_drive_shell_connected", lose_connection)
    wanted = runtime_exec.RemoteExecutionUnsettled if expected == "unsettled" else expected
    with pytest.raises(wanted) as caught:
        asyncio.run(runtime_exec._drive_shell(
            "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
            "unused", "us-west-2", None, 1, "owned-000000000000000000000000000000"))
    if expected == "unsettled":
        assert caught.value.session_id == "owned-000000000000000000000000000000"
    else:
        assert not isinstance(caught.value, runtime_exec.RemoteExecutionUnsettled)
