"""Role steering has ONE source, and it describes the ROLE, never the deliverable.

Two failure modes are pinned here, and the first one already happened:

1. **Drift between the shipped steering and the BAKED copy.** Each role's steering
   lives at ``orchestrator/harness/<role>/<file>`` (what Lab 1 scaffolds and what the
   orchestrator stages onto the mount) and is COPIED into the container image from
   ``coding-agents/<role>/<file>`` by its Dockerfile. The baked copy is what the agent
   reads whenever the mount has no steering yet, which is exactly the event's
   pre-deploy state. So a stale baked copy silently steers a live agent with
   instructions nobody can see in the repo's source of truth. They must be identical.

2. **Steering that encodes the answer.** The attendee's request is whatever they type,
   so steering that names a specific module, protocol, filename, or expected value is
   the predetermined-answer problem this workshop exists to remove: it tells the agent
   what to build before anyone asked. The dead ``harness:build`` / ``harness:ui`` /
   ``harness:gate`` blocks are checked for too, because their PARSER IS DELETED, which
   makes them config that looks live and is not.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "orchestrator"))

import roles  # noqa: E402

# Every REGISTERED role, not just the served ones: a hidden role is a restore path, and
# a restore path that steers the agent at a deleted sample is not a working restore.
_ROLE_IDS = [r.id for r in roles.REGISTRY]


def _shipped(role_id: str) -> str:
    r = roles.get(role_id)
    return os.path.join(_REPO, "orchestrator", "harness", r.harness_dir, r.steering_file)


def _baked(role_id: str) -> str:
    """The copy the Dockerfile bakes into the image.

    A build context cannot COPY from outside itself, so a role whose steering lives at a
    NESTED path upstream is staged flat in its harness dir (Kiro's
    ``.kiro/steering/validator.md`` is baked as ``steering/agent.md`` and the Dockerfile
    puts it back under ``/home/agent/.kiro/steering/``). Resolve the real on-disk file
    rather than assuming the upstream layout, so this guard covers every role instead of
    skipping the ones that are staged differently.
    """
    r = roles.get(role_id)
    role_dir = os.path.join(_REPO, "coding-agents", r.harness_dir)
    direct = os.path.join(role_dir, r.steering_file)
    if os.path.isfile(direct):
        return direct
    # Flattened staging: same basename, or the role's single steering file under a
    # steering/ dir. Only a file the Dockerfile actually COPYs counts.
    for candidate in (os.path.join(role_dir, os.path.basename(r.steering_file)),
                      os.path.join(role_dir, "steering", "agent.md")):
        if os.path.isfile(candidate):
            return candidate
    return direct   # nonexistent: the caller reports it as missing


def test_shipped_steering_exists_for_every_registered_role():
    """The registry's steering path must actually resolve: the engine stages this file
    on every dispatch, so a missing one is a fail-loud run, not a cosmetic problem."""
    for role_id in _ROLE_IDS:
        path = _shipped(role_id)
        assert os.path.isfile(path), f"{role_id}: no shipped steering at {path}"
        assert os.path.getsize(path) > 200, (
            f"{role_id}: steering at {path} is suspiciously empty")


def test_the_published_steering_path_names_a_file_that_exists():
    """``Role.steering_path`` is projected to attendees through ``GET /api/agents``.

    A role whose steering is staged FLAT (Kiro's nested ``.kiro/steering/validator.md``
    is baked as ``steering/agent.md``, because a build context cannot COPY from outside
    itself) must not publish the upstream path: an attendee who tries to open it finds
    nothing. Checked for EVERY registered role, since a restore path is published too
    the moment it is served.
    """
    for role_id in _ROLE_IDS:
        published = os.path.join(_REPO, roles.get(role_id).steering_path)
        assert os.path.isfile(published), (
            f"{role_id}: /api/agents publishes {published}, which does not exist")


def test_baked_steering_matches_the_shipped_source_for_every_role():
    """The image's copy must be byte-identical to the one source of truth.

    If this fails, run:
        cp orchestrator/harness/<role>/<file> coding-agents/<role>/<file>
    and REBUILD that role's image, because the container carries its own copy.
    """
    compared = []
    for role_id in _ROLE_IDS:
        baked = _baked(role_id)
        if not os.path.isfile(baked):
            continue
        with open(_shipped(role_id), encoding="utf-8") as f:
            want = f.read()
        with open(baked, encoding="utf-8") as f:
            got = f.read()
        assert got == want, (
            f"{role_id}: the baked steering has DRIFTED from "
            f"orchestrator/harness/. The baked copy is what a deployed agent reads "
            "before the mount has steering, so this silently changes live behavior.")
        compared.append(role_id)
    assert compared, "no registered role has a baked steering copy"


# Names from the deleted sample use case, plus the dead harness blocks. Any of these in
# steering means the agent is being told what to build before the attendee has asked.
_FORBIDDEN = [
    "cost_analyzer", "cost-analyzer", "usecase-sample-to-mcp", "usecase-critter-lab",
    "critter", "TOOL_SPECS", "mcp_server.py", "chatbot.html", "reference-server",
    "estimate_ec2_monthly_cost", "harness:build", "harness:ui", "harness:gate",
]


def test_steering_does_not_encode_the_deliverable_for_any_role():
    """Steering describes the ROLE. It must not name the retired sample, a fixed
    artifact filename, or a dead harness block whose parser no longer exists."""
    for role_id in _ROLE_IDS:
        for path in (_shipped(role_id), _baked(role_id)):
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            hits = [tok for tok in _FORBIDDEN if tok in text]
            assert not hits, f"{path} still encodes the deliverable: {hits}"


def test_the_checker_is_told_the_verdict_is_the_exit_code():
    """The load-bearing sentence of the whole loop. The checker must be told that it
    AUTHORS an executable and that its real exit code is the gate; steering that
    invites a prose verdict would let a run be graded by assertion instead of by
    execution."""
    checkers = roles.checker_ids()
    assert checkers, "a roster with no checker cannot gate anything"
    with open(_shipped(checkers[0]), encoding="utf-8") as f:
        text = f.read().lower()
    assert "exit code" in text
    assert "executable" in text
    # And it must be told not to edit the work it grades (maker is never checker).
    assert "never edit" in text


def test_builders_are_told_they_do_not_grade_themselves():
    """Each maker must know a separate role decides acceptance, so it does not try to
    self-certify (or edit the check)."""
    for role_id in roles.builder_ids():
        with open(_shipped(role_id), encoding="utf-8") as f:
            text = f.read().lower()
        assert "validator" in text, role_id
        assert "exit code" in text or "gate" in text, role_id


def test_roles_leave_git_and_github_delivery_to_the_coordinator():
    """A role writes one tree; the coordinator alone assembles and publishes it.

    A live Lab 1 opencode turn followed the old steering, initialized the shared
    mount as a repository, tried to open a PR before GitHub existed, and printed a
    temporary Runtime token while searching its environment. Pin both boundaries.
    """
    for role_id in _ROLE_IDS:
        with open(_shipped(role_id), encoding="utf-8") as f:
            text = " ".join(f.read().lower().split())
        stale_instructions = (
            "use them to branch",
            "add the label `agent:",
            "submit for human review only",
            "gateway` mcp server connected",
        )
        hits = [instruction for instruction in stale_instructions
                if instruction in text]
        assert not hits, (
            f"{role_id}: steering still delegates GitHub work: {hits}")
        assert "do not initialize git" in text, role_id
        assert "coordinator" in text and "pull request" in text, role_id
        assert (
            "do not inspect or print credential-bearing environment variables"
            in text
        ), role_id


def test_the_image_bakes_steering_at_the_path_dispatch_looks_for():
    """The Dockerfile's COPY DESTINATION must be the registry's ``steering_file``.

    ``runtime_exec.py`` stages a role's steering with
    ``if test -f $HOME/<steering_file>; then cp ...; fi``. That guard is silent: when the
    baked destination filename differs, nothing is copied, nothing fails, and the role
    dispatches into its worktree with NO steering at all, so a checker becomes a generic
    agent with no role. Kiro shipped exactly that way (baked
    ``/home/agent/.kiro/steering/agent.md`` while the registry declared
    ``.kiro/steering/validator.md``), and every other guard passed: the drift test
    compares CONTENT, and the flattened SOURCE name is legitimate because a build context
    cannot COPY from outside itself. Only the destination was wrong.

    So this asserts the one thing nothing else did: for every registered role, some COPY
    in its Dockerfile lands the steering at ``$HOME/<steering_file>``.
    """
    for role_id in _ROLE_IDS:
        r = roles.get(role_id)
        dockerfile = os.path.join(_REPO, "coding-agents", r.harness_dir, "Dockerfile")
        if not os.path.isfile(dockerfile):
            continue
        with open(dockerfile, encoding="utf-8") as f:
            body = f.read()
        wanted = r.steering_file.replace("\\", "/")
        destinations = [
            line.split()[-1].rstrip("/")
            for line in body.splitlines()
            if line.strip().startswith("COPY") and not line.rstrip().endswith("\\")
        ]
        assert any(d.rstrip("/").endswith("/" + wanted) for d in destinations), (
            f"{role_id}: no COPY in {dockerfile} bakes steering to $HOME/{wanted}. "
            f"runtime_exec.py stages with `test -f $HOME/{wanted}` and SILENTLY skips "
            f"when absent, so this role would dispatch with no steering. "
            f"COPY destinations found: {destinations}")
