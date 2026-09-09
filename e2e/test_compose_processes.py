"""Two real Python processes cannot mix their local evidence commits."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time


_REPO = Path(__file__).resolve().parents[1]
_WORKER = r"""
import os
from pathlib import Path
import sys
import time

sys.path[:0] = [str(Path.cwd() / "orchestrator"), str(Path.cwd() / "e2e")]
import engine
from test_compose_publishes_once import _run, _write, _build_trees, _author_check

tag, signals = sys.argv[1], Path(sys.argv[2])
eng = engine.Engine.__new__(engine.Engine)
run = _run(engine, "run_process_" + tag)
_write(run.roledir("claude-code"), {tag + ".txt": tag + "\n"})
_write(run.roledir("opencode"), {tag + "-ui.txt": tag + " UI\n"})
_build_trees(engine, run)
_author_check(run)
run.gate = {"passed": True, "summary": "test-owned executable"}
source = eng._composed_source_dir
def paused_source(current):
    (signals / (tag + "-inside")).touch()
    if tag == "first":
        deadline = time.monotonic() + 15
        while not (signals / "release-first").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test did not release first compose")
            time.sleep(.01)
    return source(current)
eng._composed_source_dir = paused_source
(signals / (tag + "-starting")).touch()
eng._compose_commit(run)
print(run.composed_commit, flush=True)
"""


def test_two_processes_keep_each_run_on_the_empty_base(tmp_path):
    """Pause one writer after checkout while another tries to compose.

    Before the process lock, the second writer commits inside the first writer's
    transaction. The first commit then inherits the second's unchanged check, so
    its Changes view omits that evidence even though the file exists in its tree.
    """
    signals = tmp_path / "signals"
    signals.mkdir()
    worker = tmp_path / "compose_worker.py"
    worker.write_text(_WORKER)
    env = {**os.environ, "WORKSHOP_RUNS_DIR": str(tmp_path / "runs"),
           "WORKSHOP_GATEWAY_STATE": str(tmp_path / "no-gateway.json"),
           "WORKSHOP_GITHUB_SETTINGS": str(tmp_path / "no-github.json")}
    env.pop("GITHUB_GATEWAY_URL", None)
    children = []

    def start(tag):
        child = subprocess.Popen(
            [sys.executable, str(worker), tag, str(signals)], cwd=_REPO,
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(child)
        return child

    def wait_for(name, child):
        deadline = time.monotonic() + 15
        while not (signals / name).exists():
            if child.poll() is not None:
                raise AssertionError(child.communicate())
            assert time.monotonic() < deadline, f"worker did not reach {name}"
            time.sleep(.01)

    try:
        first = start("first")
        wait_for("first-inside", first)
        second = start("second")
        wait_for("second-starting", second)
        deadline = time.monotonic() + .5
        while time.monotonic() < deadline and not (signals / "second-inside").exists():
            time.sleep(.01)
        assert not (signals / "second-inside").exists(), "two processes entered compose together"
    finally:
        (signals / "release-first").touch()
        outputs = []
        for child in children:
            try:
                outputs.append(child.communicate(timeout=15))
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise

    repo = tmp_path / "runs/composed"

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    root = git("rev-list", "--max-parents=0", "main")
    for tag, child, (stdout, stderr) in zip(("first", "second"), children, outputs):
        assert child.returncode == 0, stderr
        sha = stdout.strip()
        assert git("rev-parse", sha + "^") == root
        assert set(git("show", "--format=", "--name-only", sha).splitlines()) == {
            tag + ".txt", tag + "-ui.txt", "acceptance_check"}
