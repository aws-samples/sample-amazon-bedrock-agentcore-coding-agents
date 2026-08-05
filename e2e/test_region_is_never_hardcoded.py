"""The engine must work in ANY region, and must never blame an agent for our bug.

Every failure pinned here was found on the FIRST us-east-1 event box, and every one
of them is invisible in us-west-2 by construction: a hardcoded ``us-west-2`` default
is correct there, so no us-west-2 run can ever fail on it. That is exactly why these
are tests and not a code review note.

The live symptom was the worst kind: a role wrote a complete, working deliverable,
the engine could not read it back because it asked the wrong region, and the run
reported ``claude-code finished but wrote no files``. The attendee is told their
agent did nothing while the work sits on the mount.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH = os.path.join(os.path.dirname(_HERE), "orchestrator")
if _ORCH not in sys.path:
    sys.path.insert(0, _ORCH)

# The modules that talk to a REGIONAL endpoint. A literal region in any of these is
# a latent single-region assumption.
_REGIONAL_MODULES = (
    "engine.py", "runtime_exec.py", "executor.py", "llm.py",
    "runtime_stage.py", "run_store.py", "github.py",
)

# us-east-2 in llm.py is a REAL fact about where the mantle OpenAI endpoint is
# served, not a default for our own resources, so it is not a violation.
_ALLOWED = {("llm.py", "us-east-2")}


def _read(name: str) -> str:
    with open(os.path.join(_ORCH, name), encoding="utf-8") as fh:
        return fh.read()


def test_no_regional_module_hardcodes_a_region_default() -> None:
    """No regional module may carry a region literal as a fallback.

    Comments are stripped first: the fix's own explanation naturally mentions
    us-west-2, and matching prose would make this test pass or fail on wording
    rather than on behaviour.
    """
    violations = {}
    for module in _REGIONAL_MODULES:
        src = _read(module)
        code = "\n".join(
            line.split("#", 1)[0] for line in src.splitlines())
        # Also drop docstrings, which explain the history for the same reason.
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        code = re.sub(r"'''.*?'''", "", code, flags=re.S)
        found = {
            match for match in re.findall(
                r"[\"']((?:us|eu|ap|sa|ca|me|af)-[a-z]+-\d)[\"']", code)
        }
        found -= {region for (allowed_module, region) in _ALLOWED
                  if allowed_module == module}
        if found:
            violations[module] = sorted(found)
    assert not violations, (
        f"regional modules hardcode region defaults in code: {violations}. "
        "Derive the region from the resource ARN or ambient AWS configuration.")


def test_efficiency_lab_starter_uses_the_workshop_region() -> None:
    """The optional lab runs on the same host and must not jump regions."""
    path = os.path.join(os.path.dirname(_HERE), "starter-agent", "agent.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert not re.findall(
        r"[\"'](?:us|eu|ap|sa|ca|me|af)-[a-z]+-\d[\"']", code), (
        "starter-agent hardcodes a region; derive the workshop host's region")
    assert "AWS_REGION" in code and "boto3.Session().region_name" in code


def test_region_for_prefers_the_arn_over_any_caller_default() -> None:
    """The ARN is authoritative: ``open_shell`` rejects a mismatched client region.

    This is the exact failure that broke the live run, reduced to one assertion.
    """
    import runtime_exec

    arn = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/claude_code-AbC"
    assert runtime_exec.region_for(arn, "us-west-2") == "us-east-1"
    assert runtime_exec.region_for(arn, None) == "us-east-1"
    assert runtime_exec.region_for(arn) == "us-east-1"


def test_region_for_falls_back_for_a_non_arn_target(monkeypatch) -> None:
    """The local ``agentcore dev`` seam is a URI with no region; the caller's value
    (then the ambient one) is all there is, and it must NOT become a literal."""
    import runtime_exec

    for var in ("WORKSHOP_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    assert runtime_exec.region_for("http://localhost:9000", "eu-west-1") == "eu-west-1"
    # Nothing anywhere: an EMPTY string, so boto3 resolves its own chain. A literal
    # here would silently send the call to one region for every attendee.
    assert runtime_exec.region_for("http://localhost:9000") == ""
    monkeypatch.setenv("AWS_REGION", "ap-northeast-2")
    assert runtime_exec.region_for("http://localhost:9000") == "ap-northeast-2"


def test_read_back_helpers_do_not_default_a_region() -> None:
    """``read_tree_from_runtime`` / ``list_tree_in_runtime`` are THE read-back path.

    They used to declare ``region: str = "us-west-2"``, so every caller that omitted
    the argument silently asked the wrong region. Their default must be None (derive
    from the ARN), never a region string.
    """
    import inspect

    import runtime_exec

    for fn in (runtime_exec.read_tree_from_runtime,
               runtime_exec.list_tree_in_runtime,
               runtime_exec.run_in_runtime):
        default = inspect.signature(fn).parameters["region"].default
        assert default is None, (
            f"{fn.__name__} defaults region={default!r}. A region default here is "
            "the us-east-1 read-back bug: pass None and derive it from the ARN.")


def test_bucket_name_never_interpolates_an_empty_region(monkeypatch) -> None:
    """The region is part of the BUCKET NAME, so "unset" is not "let boto3 decide".

    A blank produced ``coding-agents-<account>-`` (trailing dash), a bucket that does
    not exist, and the failure surfaced much later as a confusing S3 Files error.
    """
    import runtime_stage

    for var in ("WORKSHOP_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION",
                "WORKSHOP_RUNTIME_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(runtime_stage, "_resolved_region", lambda: "")
    with pytest.raises(RuntimeError, match="REGION_NOT_RESOLVED"):
        runtime_stage._bucket("", "111122223333")

    # With a region, the name is the documented convention.
    assert (runtime_stage._bucket("us-east-1", "111122223333")
            == "coding-agents-111122223333-us-east-1")
    # And an unset env still resolves through the session rather than guessing.
    monkeypatch.setattr(runtime_stage, "_resolved_region", lambda: "eu-central-1")
    assert (runtime_stage._bucket("", "111122223333")
            == "coding-agents-111122223333-eu-central-1")


def test_opencode_entrypoint_pins_the_region_at_boot() -> None:
    """opencode reads its region from a CONFIG FILE that the image bakes, and that
    value WINS over AWS_REGION. ``run.sh`` rewrites it, but the orchestrator
    dispatches the ``opencode`` binary directly and never runs ``run.sh``, so the
    rewrite has to happen at container boot, where both paths pass."""
    path = os.path.join(os.path.dirname(_HERE), "coding-agents", "opencode",
                        "entrypoint.sh")
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert "configure_opencode.py" in body, (
        "entrypoint.sh must rewrite the baked opencode config at boot; without it a "
        "dispatched run signs Bedrock calls for the IMAGE's region and fails with "
        "AccessDenied on a foreign inference profile.")
    assert "--region" in body


def test_no_bare_haiku_inference_profile_anywhere() -> None:
    """``anthropic.claude-haiku-4-5-...`` without the ``us.`` prefix cannot be
    invoked on demand: Converse, InvokeModel AND InvokeModelWithResponseStream all
    reject it. It shipped in six places and burned 3 minutes of every bootstrap on
    a request that could never succeed."""
    root = os.path.dirname(_HERE)
    bad: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "node_modules", "__pycache__", ".runs",
                                    "dist", "build", ".pytest_cache"}]
        for fn in filenames:
            if not fn.endswith((".py", ".json", ".yaml", ".yml", ".sh", ".md", ".toml")):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.basename(full) == os.path.basename(__file__):
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for m in re.finditer(r"(?<![.\w])anthropic\.claude-haiku-4-5[\w.:-]*", text):
                bad.append(f"{os.path.relpath(full, root)}: {m.group(0)}")
    assert not bad, (
        "bare (unprefixed) haiku inference profile id found; use "
        "us.anthropic.claude-haiku-4-5-...:\n  " + "\n  ".join(bad))


def test_role_deploys_refuse_a_cross_region_access_point() -> None:
    """ONE region per workshop, enforced in code because two are now accessible.

    The access point ARN carries its own region. If the mount and the Runtime disagree
    the Runtime comes up unable to reach /mnt/s3files and the failure surfaces much
    later as an agent that "wrote nothing" -- the same misleading shape this file
    exists to prevent. Each role's deploy must refuse while the fix is one line.
    """
    import re as _re

    root = os.path.dirname(_HERE)
    for role in ("opencode", "claude-code", "kiro", "claude-code-validator"):
        path = os.path.join(root, "coding-agents", role, "deploy.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert "REGION_MISMATCH" in src, (
            f"{role}/deploy.py has no same-region guard: a mount in one region and a "
            "Runtime in another must fail AT DEPLOY, not later as a phantom empty "
            "workspace.")
        assert "_assert_same_region(S3FILES_AP_ARN, REGION)" in src, (
            f"{role}/deploy.py defines the guard but never calls it")
        # And the region itself must not be a literal (same rule as the engine).
        head = src[:src.find("def ")] if "def " in src else src
        code = "\n".join(l.split("#", 1)[0] for l in head.splitlines())
        assert not _re.findall(r"[\"'](?:us|eu|ap)-[a-z]+-\d[\"']", code), (
            f"{role}/deploy.py hardcodes a region default; derive it from the env, "
            "infra.config, then boto3's own resolver.")


def test_the_small_model_is_wirable_and_always_an_inference_profile() -> None:
    """opencode's cheap background model lives in a CONFIG FILE, so it needs the same
    wirable seam as the others, and it must never end up a bare model id (which every
    Bedrock invoke API rejects for on-demand use)."""
    sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "coding-agents", "opencode"))
    import configure_opencode  # noqa: PLC0415

    saved = os.environ.pop("WORKSHOP_SMALL_MODEL", None)
    try:
        default = configure_opencode._small_model()
        assert default.startswith("amazon-bedrock/us."), default
        # An override without the provider prefix must be normalised, not doubled.
        os.environ["WORKSHOP_SMALL_MODEL"] = "us.anthropic.claude-sonnet-4-6"
        assert configure_opencode._small_model() == (
            "amazon-bedrock/us.anthropic.claude-sonnet-4-6")
        os.environ["WORKSHOP_SMALL_MODEL"] = "amazon-bedrock/us.anthropic.claude-sonnet-4-6"
        assert configure_opencode._small_model() == (
            "amazon-bedrock/us.anthropic.claude-sonnet-4-6")
    finally:
        os.environ.pop("WORKSHOP_SMALL_MODEL", None)
        if saved is not None:
            os.environ["WORKSHOP_SMALL_MODEL"] = saved
