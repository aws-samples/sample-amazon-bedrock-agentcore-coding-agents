"""A bundle that gets pasted into a shared channel, and the two bugs it exposed.

`diagnose.py` collects the six things a facilitator asks for when an attendee is
stuck. Because its whole purpose is to be SHARED, its redaction has to be
allowlist-based: every value in the output is either a literal the module names, or
it went through `_safe`, which reports shape only. A denylist would leak the first
field nobody thought of.

The last two tests here exist because running the bundle for real on this machine
found two defects in the durable-run-state work from the previous pass: `recent()`
ordered by filename (a run id is time-of-day only, so it inverts across midnight)
and nothing ever deleted a status file (277 had accumulated). Both are pinned.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Never let this suite read (or write) a developer's real wired GitHub connection.
_ISOLATE = tempfile.mkdtemp()
os.environ.setdefault("WORKSHOP_GITHUB_SETTINGS",
                      os.path.join(_ISOLATE, "gh.json"))
os.environ.setdefault("WORKSHOP_GATEWAY_STATE",
                      os.path.join(_ISOLATE, "gateway.json"))

import diagnose  # noqa: E402
import run_store  # noqa: E402


# ------------------------------------------------------ it is safe to share

def test_a_credential_in_the_environment_is_confirmed_never_printed(monkeypatch):
    """The bundle's reason to exist is being pasted somewhere public."""
    secret = "AKIAIOSFODNN7EXAMPLE-not-a-real-key"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", secret)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_definitelynotarealtokenvalue")
    data = diagnose.bundle()
    blob = json.dumps(data) + diagnose.render(data)
    assert secret not in blob, "the bundle printed a credential"
    assert "ghp_definitelynotarealtokenvalue" not in blob
    # But it must still CONFIRM presence, or it cannot diagnose "no credentials".
    present = [c["name"] for c in data["env"]["credentials_present"] if c["set"]]
    assert "AWS_ACCESS_KEY_ID" in present and "GITHUB_TOKEN" in present


def test_an_unanticipated_env_var_is_not_collected_at_all(monkeypatch):
    """Allowlist, not denylist: a variable nobody thought about must not appear.

    This is the property that makes the bundle shareable. A denylist would have to
    predict every future credential env var; the allowlist simply never reads them.
    """
    monkeypatch.setenv("SOME_FUTURE_VENDOR_API_KEY", "sk-live-oops")
    data = diagnose.bundle()
    blob = json.dumps(data) + diagnose.render(data)
    assert "sk-live-oops" not in blob
    assert "SOME_FUTURE_VENDOR_API_KEY" not in blob


def test_an_account_id_inside_an_arn_is_masked():
    """The one identifier an attendee cannot rotate after pasting it somewhere."""
    masked = diagnose._mask_arn(
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/claude_code-Ab1")
    assert "123456789012" not in masked
    assert masked.startswith("arn:aws:bedrock-agentcore:us-west-2:")
    assert masked.endswith("runtime/claude_code-Ab1"), (
        "masking destroyed the part that identifies WHICH runtime, which is the "
        "reason the ARN is in the bundle at all")


def test_safe_reports_shape_and_never_content():
    out = diagnose._safe("X", "supersecretvalue-1234567890")
    assert out["set"] is True and out["length"] == 27
    assert "supersecret" not in json.dumps(out)
    assert diagnose._safe("X", None) == {"name": "X", "set": False}
    assert diagnose._safe("X", "")["set"] is False


def test_the_bundle_opens_no_credential_file(monkeypatch):
    """Behavioural, not a source grep: watch every file it actually opens.

    A grep would match this module's own prose about NOT reading a .pem. What
    matters is the syscall: no path in the bundle may open a private key, a token
    store, or a Secrets Manager value.
    """
    opened: list[str] = []
    real_open = open

    def watched(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", watched)
    diagnose.bundle()
    monkeypatch.undo()
    for path in opened:
        low = path.lower()
        assert not low.endswith(".pem"), f"the bundle opened a private key: {path}"
        assert "credential" not in low, f"the bundle opened {path}"
        assert "secret" not in low, f"the bundle opened {path}"


# ------------------------------------------------------- it still diagnoses

def test_the_bundle_names_the_roles_that_are_not_wired():
    """The answer a facilitator wants first, and it must come from the registry."""
    data = diagnose.bundle()
    roles = data["roles"]
    assert roles.get("served_roles"), roles
    # not_wired is derived, so it can never disagree with served/wired.
    for role in roles.get("not_wired", []):
        assert role in roles["served_roles"]
        assert role not in (roles.get("wired") or {})


def test_the_markdown_says_ready_or_not_ready_in_words():
    """It is read by a person under time pressure, not parsed."""
    text = diagnose.render(diagnose.bundle())
    assert "# Workshop diagnostic bundle" in text
    assert "## GitHub" in text and "## Roles" in text
    assert ("READY" in text or "not wired" in text)


def test_a_plain_directory_is_not_reported_as_an_s3_files_mount(
        monkeypatch, tmp_path):
    """A leftover /mnt/s3files directory is not evidence that NFS is mounted."""
    plain_directory = tmp_path / "s3files"
    plain_directory.mkdir()
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", str(plain_directory))

    host = diagnose._host_section()
    assert host["s3files_path_exists"] is True
    assert host["s3files_mounted"] is False
    text = diagnose.render({"host": host})
    assert "NOT MOUNTED (the path is only a directory)" in text


def test_a_broken_section_does_not_lose_the_whole_bundle(monkeypatch):
    """This runs when things are already broken; partial beats nothing."""
    def boom():
        raise RuntimeError("registry exploded")
    monkeypatch.setattr(diagnose, "_runtime_section", boom)
    try:
        data = diagnose.bundle()
    except RuntimeError:
        raise AssertionError("bundle() raised; a diagnostic must always return")
    # The other sections still arrived.
    assert "github" in data and "host" in data


def test_a_collection_failure_is_not_reported_as_not_wired():
    """"not wired" sends a facilitator to the wiring step. It must be earned.

    If the GitHub section could not be collected at all, we do not KNOW the wiring
    state, and saying "not wired" costs exactly the time this bundle exists to save.
    """
    text = diagnose.render({"github": {"error": "boom: import failed"}})
    assert "could not determine the wiring" in text
    assert "not wired" not in text, text


def test_an_unknown_run_id_says_so_rather_than_inventing_a_run():
    data = diagnose.bundle(run_id="run_000000_777")
    assert data["run"]["found"] is False
    assert data["run"]["hint"]


def test_the_cli_writes_a_file_and_says_it_carries_no_credentials(capsys):
    out = os.path.join(tempfile.mkdtemp(), "bundle.md")
    assert diagnose._main(["--out", out]) == 0
    assert os.path.isfile(out)
    printed = capsys.readouterr().out
    assert "no credentials" in printed
    assert open(out, encoding="utf-8").read().startswith("# Workshop diagnostic")


def test_the_cli_json_mode_is_valid_json(capsys):
    assert diagnose._main(["--json"]) == 0
    json.loads(capsys.readouterr().out)


# ------------------------- the two run_store bugs the real bundle uncovered

def test_recent_is_ordered_by_time_not_by_filename():
    """Found by reading a real bundle: the "most recent run" was the wrong one.

    A run id is `run_<HHMMSS>_<NNN>` -- time of day, no date. Sorting the filenames
    therefore puts 23:59 ahead of the next morning's 00:05, so any session crossing
    midnight UTC has its newest run reported last. Ordering by mtime is correct
    regardless of what the id encodes.
    """
    d = tempfile.mkdtemp()
    run_store.save(d, "run_235959_001", {"run_id": "run_235959_001"})
    run_store.save(d, "run_000501_001", {"run_id": "run_000501_001"})
    # Make the second one unambiguously newer on disk.
    os.utime(os.path.join(d, "state", "run_235959_001.json"), (1_700_000_000, 1_700_000_000))
    os.utime(os.path.join(d, "state", "run_000501_001.json"), (1_700_003_600, 1_700_003_600))
    ids = [r["run_id"] for r in run_store.recent(d, limit=5)]
    assert ids[0] == "run_000501_001", (
        f"recent() reported the OLDER run first: {ids}")


def test_status_files_do_not_accumulate_without_limit(monkeypatch):
    """Nothing deleted them, so the directory grew forever (277 on this machine).

    Asserted through `save`, not by calling `prune` directly: a cap that only works
    when someone remembers to invoke it is not a cap. Saving is the only event that
    can grow the directory, so it is where the bound has to be enforced.
    """
    d = tempfile.mkdtemp()
    keep = 5
    monkeypatch.setattr(run_store, "_MAX_STATE_FILES", keep)
    for i in range(12):
        run_store.save(d, f"run_1000{i:02d}_001", {"run_id": f"run_1000{i:02d}_001"})
    left = [n for n in os.listdir(os.path.join(d, "state")) if n.endswith(".json")]
    assert len(left) == keep, (
        f"{len(left)} status files after 12 saves with a cap of {keep}: the directory "
        "grows without bound")


def test_pruning_keeps_the_NEWEST_and_load_still_finds_them():
    """A cap that dropped the run you just did would be worse than no cap."""
    d = tempfile.mkdtemp()
    for i in range(8):
        run_store.save(d, f"run_2000{i:02d}_001", {"run_id": f"run_2000{i:02d}_001",
                                                   "status": "passed"})
        os.utime(os.path.join(d, "state", f"run_2000{i:02d}_001.json"),
                 (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
    run_store.prune(d, keep=3)
    assert run_store.load(d, "run_200007_001") is not None, "dropped the newest run"
    assert run_store.load(d, "run_200000_001") is None, "kept the oldest run"
