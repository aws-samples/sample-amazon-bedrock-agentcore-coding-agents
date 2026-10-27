"""Archive handoff regressions; no AWS calls, model work, or manufactured verdicts.

The live October 27 finding used Codex to replace Kiro's archive under the shared
mount prefix. Policy tests cover who may create each new key. These tests exercise
the real consumer: exact producer and nonce selection, server checksum validation,
no fallback to old input, seal failures, and both successful and failed dispatches.
Actual IAM enforcement is verified separately in role Runtime canaries.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import re
import tarfile

import pytest

import roles
import runtime_exec
import runtime_stage

BUCKET = "bucket-1"
SUBDIR = "run_handoff/work/checker"
NONCE = "a" * 32


def pack(data: bytes, name: str = "check") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def sha(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")


class Store:
    """S3 wire fixture. Authentication rules have separate policy/live tests."""

    def __init__(self):
        self.objects = {}
        self.reads, self.writes, self.deleted = [], [], []
        self.fail_seal = False
        self.delete_errors = []

    def seed(self, key, payload, **response):
        self.objects[key] = {
            "payload": payload, "ChecksumSHA256": sha(payload),
            "ChecksumType": "FULL_OBJECT", **response,
        }

    def get_object(self, **kwargs):
        assert kwargs["Bucket"] == BUCKET and kwargs["ChecksumMode"] == "ENABLED"
        self.reads.append(kwargs["Key"])
        record = copy.deepcopy(self.objects[kwargs["Key"]])
        return {"Body": io.BytesIO(record.pop("payload")), **record}

    def put_object(self, **kwargs):
        assert kwargs["Bucket"] == BUCKET
        assert kwargs["Key"].startswith("runtime-exchange/coordinator/")
        if self.fail_seal:
            raise PermissionError("AccessDenied while sealing")
        payload = kwargs["Body"]
        assert kwargs["ChecksumSHA256"] == sha(payload)
        self.writes.append(kwargs)
        self.seed(kwargs["Key"], payload, Metadata=kwargs.get("Metadata", {}))
        return {"ChecksumSHA256": sha(payload)}

    def copy_object(self, **kwargs):
        assert kwargs["ChecksumAlgorithm"] == "SHA256"
        payload = self.objects[kwargs["CopySource"]["Key"]]["payload"]
        self.seed(kwargs["Key"], payload)

    def list_objects_v2(self, **kwargs):
        assert kwargs["Bucket"] == BUCKET
        return {"Contents": [{"Key": key} for key in self.objects
                             if key.startswith(kwargs["Prefix"])]}

    def delete_objects(self, **kwargs):
        for obj in kwargs["Delete"]["Objects"]:
            self.deleted.append(obj["Key"])
            del self.objects[obj["Key"]]
        return {"Errors": self.delete_errors}


@pytest.fixture
def store(monkeypatch):
    result = Store()
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", BUCKET)
    monkeypatch.delenv("WORKSHOP_S3FILES_DIR", raising=False)
    monkeypatch.setattr(runtime_stage, "_client", lambda _region: result)
    result.seed(runtime_stage.archive_key(SUBDIR), pack(b"previous input"))
    return result


@pytest.mark.parametrize("agent", [r.id for r in roles.REGISTRY])
def test_exact_producer_output_is_verified_sealed_and_read_without_mount(store, agent):
    key = runtime_stage.result_key(SUBDIR, agent, NONCE)
    payload = pack(b"new executable bytes")
    # Untrusted metadata cannot choose the producer; the coordinator's assigned
    # namespace does. VersionId and checksum come from the S3 response.
    store.seed(key, payload, Metadata={"producer-role": "invented"}, VersionId="s3-v1")
    receipt = runtime_stage.accept_result(SUBDIR, agent, NONCE, "us-west-2")
    assert receipt == {"producer": agent, "key": key,
                       "sha256": hashlib.sha256(payload).hexdigest(), "version_id": "s3-v1"}
    assert store.reads == [key]
    assert runtime_stage.read_archive(SUBDIR, "us-west-2") == {"check": b"new executable bytes"}
    assert store.writes[-1]["Metadata"]["producer-role"] == agent
    assert "/agents/" not in key and "/agents/" not in store.writes[-1]["Key"]


@pytest.mark.parametrize("wrong_location", ["sibling", "previous-turn", "legacy-mount"])
def test_missing_exact_output_never_consumes_a_substitute_or_old_seed(store, wrong_location):
    key = {
        "sibling": runtime_stage.result_key(SUBDIR, "codex", NONCE),
        "previous-turn": runtime_stage.result_key(SUBDIR, "kiro", "b" * 32),
        "legacy-mount": f"agents/runtime-exchange/{SUBDIR}.tar.gz",
    }[wrong_location]
    store.seed(key, pack(b"substituted check"), Metadata={"producer-role": "kiro"})
    with pytest.raises(KeyError):
        runtime_stage.accept_result(SUBDIR, "kiro", NONCE, "us-west-2")
    assert store.reads == [runtime_stage.result_key(SUBDIR, "kiro", NONCE)]
    assert not store.writes
    assert runtime_stage.read_archive(SUBDIR, "us-west-2") == {"check": b"previous input"}


@pytest.mark.parametrize("fault", ["missing", "wrong", "composite", "only-metadata"])
def test_a_claimed_digest_or_invalid_s3_digest_does_not_authenticate_bytes(store, fault):
    key = runtime_stage.result_key(SUBDIR, "kiro", NONCE)
    payload = pack(b"checker output")
    store.seed(key, payload)
    record = store.objects[key]
    if fault == "wrong":
        record["ChecksumSHA256"] = sha(pack(b"different bytes"))
    elif fault == "composite":
        record["ChecksumType"] = "COMPOSITE"
    else:
        record.pop("ChecksumSHA256")
    if fault == "only-metadata":
        record["Metadata"] = {"sha256": hashlib.sha256(payload).hexdigest()}
    with pytest.raises(RuntimeError, match="ARCHIVE_INTEGRITY_ERROR"):
        runtime_stage.accept_result(SUBDIR, "kiro", NONCE, "us-west-2")
    assert not store.writes


def test_malformed_archive_and_denied_seal_keep_the_previous_seed(store):
    key = runtime_stage.result_key(SUBDIR, "kiro", NONCE)
    store.seed(key, b"this is not an archive")
    with pytest.raises(tarfile.ReadError):
        runtime_stage.accept_result(SUBDIR, "kiro", NONCE, "us-west-2")
    store.seed(key, pack(b"checker output"))
    store.fail_seal = True
    with pytest.raises(PermissionError, match="AccessDenied"):
        runtime_stage.accept_result(SUBDIR, "kiro", NONCE, "us-west-2")
    assert runtime_stage.read_archive(SUBDIR, "us-west-2") == {"check": b"previous input"}


def test_the_consumer_checks_the_sealed_copy_too(store):
    key = runtime_stage.archive_key(SUBDIR)
    store.objects[key]["payload"] = pack(b"corrupted after download")
    with pytest.raises(RuntimeError, match="ARCHIVE_INTEGRITY_ERROR"):
        runtime_stage.read_archive(SUBDIR, "us-west-2")


def test_fresh_turn_can_advance_only_to_its_new_exact_output(store):
    original = runtime_stage.result_key(SUBDIR, "kiro", NONCE)
    later = runtime_stage.result_key(SUBDIR, "kiro", "b" * 32)
    first, second = pack(b"first check"), pack(b"second check")
    store.seed(original, first)
    runtime_stage.accept_result(SUBDIR, "kiro", NONCE, "us-west-2")
    store.seed(later, second)
    runtime_stage.accept_result(SUBDIR, "kiro", "b" * 32, "us-west-2")
    assert store.objects[original]["payload"] == first
    assert runtime_stage.read_archive(SUBDIR, "us-west-2") == {"check": b"second check"}


@pytest.mark.parametrize(("agent", "nonce"), [
    ("../kiro", NONCE), ("kiro/codex", NONCE), ("kiro", "../previous"),
    ("kiro", ""), ("kiro", "a" * 12),
])
def test_a_role_or_nonce_cannot_escape_the_assigned_namespace(agent, nonce):
    with pytest.raises(RuntimeError):
        runtime_stage.result_key(SUBDIR, agent, nonce)


def test_staged_inputs_and_cleanup_are_scoped_to_the_exact_run(store, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("print('fixture')\n")
    runtime_stage.stage_base("run_handoff", str(source), "us-west-2")
    runtime_stage.clone_archive(
        runtime_stage.base_subdir("run_handoff"), "run_handoff/work/backend", "us-west-2")
    assert runtime_stage.read_archive("run_handoff/work/backend", "us-west-2") == {
        "module.py": b"print('fixture')\n"}
    for role in roles.REGISTRY:
        store.seed(runtime_stage.result_key(SUBDIR, role.id, NONCE), pack(b"output"))
    other_run = runtime_stage.archive_key("run_handoff_other/work/backend")
    legacy = f"agents/runtime-exchange/{SUBDIR}.tar.gz"
    evidence = "orchestrator/run-evidence/run_handoff/check"
    for key in (other_run, legacy, evidence):
        store.seed(key, b"retain")
    runtime_stage.cleanup_run("run_handoff", "us-west-2")
    assert set(store.objects) == {other_run, legacy, evidence}
    assert any("/roles/claude-code-validator/" in key for key in store.deleted)


def test_s3_per_object_cleanup_errors_remain_failures(store):
    store.delete_errors = [{"Code": "AccessDenied", "Key": runtime_stage.archive_key(SUBDIR)}]
    with pytest.raises(RuntimeError, match="ARCHIVE_CLEANUP_ERROR"):
        runtime_stage.cleanup_run("run_handoff", "us-west-2")


@pytest.mark.parametrize("exit_code", [0, 7])
def test_shell_success_requires_the_new_output_and_failure_never_uses_old_input(
        store, monkeypatch, exit_code):
    async def shell(_arn, command, _region, _on_line, _timeout, session_id):
        nonce = re.search(r"B1=__ROLE_RUN_BEGIN__-([a-f0-9]{12});", command).group(1)
        return {"exit": exit_code, "session_id": session_id,
                "raw": (f"__ROLE_RUN_BEGIN__-{nonce}\nCLI result\n"
                        f"__ROLE_RUN_END__-{nonce}\n")}
    monkeypatch.setattr(runtime_exec, "_drive_shell", shell)
    args = (
        "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/offline",
        "claude-code", "fixture only", SUBDIR, None, "fixture-model",
        "us-west-2", None, 30,
    )
    if exit_code == 0:
        with pytest.raises(runtime_exec.RoleExecutionError, match="ARTIFACT_TRANSFER_ERROR"):
            runtime_exec._dispatch_once(*args)
        assert len(store.reads) == 1
        assert store.reads[0].startswith("runtime-exchange/roles/claude-code/")
    else:
        result = runtime_exec._dispatch_once(*args)
        assert result["exit"] == 7 and result["transcript"] == "CLI result"
        assert not store.reads
    assert not store.writes
