"""The tree read-back must not try to carry a dependency tree.

A role's work comes back from its Runtime as one `tar | base64` payload over a
WebSocket shell, so the payload has to stay small. An agent that runs
`npm install` creates a `node_modules` of thousands of files: a live run of the
big preset produced 615 files / 4.6MB, the read-back failed, the frontend role
came back holding only its harness files, and the run failed the empty-tree guard
and ended `needs_human` even though the deliverable was sitting on the mount.

Dependency, cache, and virtualenv directories are therefore excluded AT THE TAR,
so they never enter the channel at all. They are reproducible from the manifest
the agent wrote (`package.json`, `requirements.txt`), so nothing a reviewer needs
is lost.
"""

from __future__ import annotations

import base64
import io
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_exec  # noqa: E402


def _tar_like_the_readback(root: str) -> dict[str, bytes]:
    """Run the SAME tar the read-back builds, and decode it the same way."""
    excludes = " ".join(f"--exclude={shlex.quote(p)}"
                        for p in runtime_exec._TREE_EXCLUDES)
    cmd = f"tar -C {shlex.quote(root)} {excludes} -czf - . 2>/dev/null | base64"
    blob = subprocess.run(["bash", "-c", cmd], capture_output=True,
                          text=True, check=True).stdout
    raw = base64.b64decode("".join(blob.split()))
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            rel = os.path.relpath(m.name, ".")
            base = os.path.basename(rel)
            if base.startswith("._"):
                continue          # macOS AppleDouble sidecar; absent on the box
            out[rel] = tf.extractfile(m).read()
    return out


def _workspace() -> str:
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "static"))
    os.makedirs(os.path.join(d, "node_modules", "express", "lib"))
    os.makedirs(os.path.join(d, "__pycache__"))
    os.makedirs(os.path.join(d, ".venv", "lib"))
    with open(os.path.join(d, "server.py"), "w") as f:
        f.write("# the deliverable\n")
    with open(os.path.join(d, "package.json"), "w") as f:
        f.write('{"name":"tracker"}\n')      # the MANIFEST must survive
    with open(os.path.join(d, "static", "index.html"), "w") as f:
        f.write("<h1>ui</h1>\n")
    # The heavy, reproducible parts. INCOMPRESSIBLE on purpose: the payload is
    # gzipped, so filler bytes would shrink to nothing and the size assertion below
    # would pass without the exclusions doing any work.
    import os as _os
    with open(_os.path.join(d, "node_modules", "express", "lib", "app.js"), "wb") as f:
        f.write(_os.urandom(200_000))
    with open(_os.path.join(d, "__pycache__", "server.cpython-311.pyc"), "wb") as f:
        f.write(_os.urandom(20_000))
    with open(_os.path.join(d, ".venv", "lib", "big.so"), "wb") as f:
        f.write(_os.urandom(200_000))
    return d


def test_the_deliverable_survives_and_the_dependency_tree_does_not():
    root = _workspace()
    got = _tar_like_the_readback(root)

    for rel in ("server.py", "package.json", "static/index.html"):
        assert rel in got, (rel, sorted(got))
    assert got["server.py"] == b"# the deliverable\n"

    leaked = [k for k in got
              if any(part in k.split("/") for part in runtime_exec._TREE_EXCLUDES)]
    assert not leaked, (
        "a dependency/cache directory entered the read-back payload, which is what "
        f"broke a live run's tree read-back: {leaked}")


def test_the_payload_stays_small_enough_for_the_shell_channel():
    """420KB of dependencies must not become a base64 payload on the wire."""
    root = _workspace()
    excludes = " ".join(f"--exclude={shlex.quote(p)}"
                        for p in runtime_exec._TREE_EXCLUDES)
    with_excludes = subprocess.run(
        ["bash", "-c", f"tar -C {shlex.quote(root)} {excludes} -czf - . 2>/dev/null | base64 | wc -c"],
        capture_output=True, text=True, check=True).stdout.strip()
    without = subprocess.run(
        ["bash", "-c", f"tar -C {shlex.quote(root)} -czf - . 2>/dev/null | base64 | wc -c"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert int(with_excludes) < int(without) / 5, (with_excludes, without)
    assert int(with_excludes) < 100_000, with_excludes


def test_node_modules_is_excluded_by_name():
    """Pin the specific directory that broke a live run."""
    assert "node_modules" in runtime_exec._TREE_EXCLUDES
    assert "__pycache__" in runtime_exec._TREE_EXCLUDES


def test_a_transfer_replaces_the_previous_rounds_files_but_keeps_the_harness():
    """A re-implement round must not leave round 1's files behind.

    A live run had round 2 rewrite `server.py` while one role's directory still
    held round 1's copy, so compose reported a CONFLICT between two ROUNDS of the
    same file rather than a disagreement between two roles.
    """
    import importlib
    import shutil
    engine = importlib.import_module("engine")
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "skills", "backend-engineering"))
    with open(os.path.join(root, "skills", "backend-engineering", "SKILL.md"), "w") as f:
        f.write("harness skill\n")
    with open(os.path.join(root, "CLAUDE.md"), "w") as f:
        f.write("harness steering\n")
    # Round 1's work, including a file round 2 will not write again.
    with open(os.path.join(root, "server.py"), "w") as f:
        f.write("# round 1\n")
    with open(os.path.join(root, "dropped_in_round_2.py"), "w") as f:
        f.write("# round 1 only\n")

    engine.Engine._clear_transferred(root)

    left = sorted(os.listdir(root))
    assert "CLAUDE.md" in left, left        # the engine's harness stays
    assert "skills" in left, left
    assert "server.py" not in left, left    # round 1's work goes
    assert "dropped_in_round_2.py" not in left, left
    shutil.rmtree(root, ignore_errors=True)
