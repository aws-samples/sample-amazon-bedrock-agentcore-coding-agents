"""The executable must outlive a disposable checkout without changing its grade."""
import hashlib
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_store


class Mirror:
    def __init__(self):
        self.objects = {}

    def put_object(self, **request):
        self.objects[request["Key"]] = request


def test_full_check_survives_original_checkout_and_local_runtime_loss(tmp_path, monkeypatch):
    mirror = Mirror()
    monkeypatch.setattr(run_store, "_s3", lambda: (mirror, "workshop-evidence"))
    source = b"#!/usr/bin/env python3\n" + b"print('authored check')\n" * 80
    runtime = tmp_path / "runtime"
    evidence = run_store.save_check(str(runtime), "run_123", "work_backend", source)
    assert (runtime / evidence["local_path"]).read_bytes() == source
    key = evidence["s3_uri"].removeprefix("s3://workshop-evidence/")
    shutil.rmtree(runtime)
    saved = mirror.objects[key]
    assert saved["Body"] == source
    assert saved["Metadata"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert evidence["bytes"] == len(source)
    assert saved["ServerSideEncryption"] == "AES256"


def test_repair_retains_the_same_executable_and_a_new_check_cannot_replace_it(tmp_path, monkeypatch):
    mirror = Mirror()
    monkeypatch.setattr(run_store, "_s3", lambda: (mirror, "workshop-evidence"))
    first = run_store.save_check(str(tmp_path), "run_123", "work_backend", b"original")
    repair = run_store.save_check(str(tmp_path), "run_123", "work_backend", b"original")
    refreshed = run_store.save_check(str(tmp_path), "run_123", "work_backend", b"new base")
    assert first == repair
    assert first["s3_uri"] != refreshed["s3_uri"]
    assert len(mirror.objects) == 2
    assert (tmp_path / first["local_path"]).read_bytes() == b"original"


def test_failed_mirror_does_not_claim_durable_storage_or_invent_a_verdict(tmp_path, monkeypatch):
    class Unavailable:
        def put_object(self, **_request):
            raise OSError("bucket unavailable")

    monkeypatch.setattr(run_store, "_s3", lambda: (Unavailable(), "unavailable"))
    messages = []
    evidence = run_store.save_check(
        str(tmp_path), "run_123", "work_backend", b"check", lambda *args: messages.append(args))
    assert "s3_uri" not in evidence
    assert "passed" not in evidence
    assert (tmp_path / evidence["local_path"]).read_bytes() == b"check"
    assert "not mirrored" in messages[0][0]


def test_evidence_identifiers_cannot_escape_the_run_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "_s3", lambda: None)
    evidence = run_store.save_check(str(tmp_path), "../other", "work_backend", b"check")
    assert "local_path" not in evidence
    assert not list(tmp_path.iterdir())
