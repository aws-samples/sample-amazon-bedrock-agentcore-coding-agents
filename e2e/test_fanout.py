"""fanout.py: the shared repository, the per-agent command, the host's worktrees, the board.

Offline: no session is started and no model is called. Git is real: each test builds a
repository and a bare repository in a temp tree, runs the exact shell command a session
runs (with a stand-in for /app/run.sh that does what `claude -w <name>` does: a worktree
at .claude/worktrees/<name> on branch worktree-<name>, edits left uncommitted), then
collects the pushed branch on the "host" as a linked worktree.
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents" / "fanout.py"
spec = importlib.util.spec_from_file_location("workshop_fanout", SOURCE)
fanout = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fanout
spec.loader.exec_module(fanout)

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

GAME = "<!doctype html><title>Mina's Dumpling Dash</title><canvas></canvas>\n"
RULES = "# House rules\n- A day palette.\n"

# What `claude -w <name> --print <prompt>` does to a repository, minus the model.
FAKE_CLAUDE = r"""#!/bin/sh
cd "$WORKSHOP_AGENT_WORKDIR" || exit 2
[ "$1" = "-w" ] || { echo "expected -w, got $1"; exit 2; }
name=$2; prompt=$3
git worktree add -q ".claude/worktrees/$name" -b "worktree-$name" || exit 3
cd ".claude/worktrees/$name" || exit 3
sed "s|<title>\([^<]*\)</title>|<title>\1: $name</title>|" index.html > index.tmp && mv index.tmp index.html
printf '<p>%s</p>\n' "$prompt" >> index.html
echo "working on $name"
echo "DONE $name feature"
exit "${FAKE_EXIT:-0}"
"""


def _repo(tmp_path: Path) -> Path:
    game = tmp_path / "lid-game"
    game.mkdir()
    (game / "index.html").write_text(GAME)
    (game / "CLAUDE.md").write_text(RULES)
    return game


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """A game shared with the agents through --init: (repo, bare, remote)."""
    monkeypatch.setattr(fanout, "MOUNT", tmp_path / "mnt")
    (tmp_path / "mnt").mkdir()
    for key, value in fanout.LOCAL_IDENTITY.items():
        monkeypatch.setenv(key, value)
    repo, bare = _repo(tmp_path), tmp_path / "mnt" / "lid.git"
    said = []
    remote = fanout.init(repo, bare, backlog=True, say=said.append)
    return repo, bare, remote, said


def _launcher(tmp_path: Path) -> str:
    path = tmp_path / "fake-run.sh"
    path.write_text(FAKE_CLAUDE)
    path.chmod(0o755)
    return str(path)


def _session(tmp_path: Path, bare: Path, index: int, name: str, env=None) -> subprocess.CompletedProcess:
    """Run the exact command a session runs, locally, in its own scratch /tmp."""
    scratch = tmp_path / f"vm-{index:02d}"
    scratch.mkdir()
    command = fanout.agent_command(index, name, str(bare), "item {n} as {name}", agents=3,
                                   clone_dir=str(scratch / "lid"), scratch=str(scratch),
                                   launcher=_launcher(scratch), peer_wait=0)
    return subprocess.run(shlex.split(command), capture_output=True, text=True,
                          env={**dict(__import__("os").environ), **(env or {})}, timeout=60)


def test_init_makes_the_game_a_repository_with_a_backlog_and_a_bare_copy(shared):
    repo, bare, remote, said = shared
    assert remote == "origin"
    assert fanout.current_branch(repo) == "main"
    log = fanout.git(repo, "log", "--format=%s", "main").splitlines()
    assert log == ["Add a backlog: one feature idea per agent", "The game from Lab 1"]
    assert fanout.backlog_items((repo / "BACKLOG.md").read_text()) == 20
    assert (bare / "HEAD").is_file() and fanout.git(bare, "rev-parse", "--is-bare-repository") == "true"
    assert fanout.git(bare, "rev-parse", "main") == fanout.git(repo, "rev-parse", "main")
    assert fanout.git(repo, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main", \
        "a plain git push must work after --init"
    text = "\n".join(said)
    for step in ("made", "added BACKLOG.md", "created the bare repository", "set", "pushed main"):
        assert step in text, f"--init must say what it did: {step}"


def test_init_twice_repeats_nothing(shared):
    repo, bare, _, _ = shared
    before = fanout.git(repo, "rev-parse", "HEAD")
    said = []
    assert fanout.init(repo, bare, backlog=True, say=said.append) == "origin"
    assert fanout.git(repo, "rev-parse", "HEAD") == before
    assert all("already" in line or "pushed" in line for line in said), said


def test_init_copies_the_lab1_game_home_when_it_is_missing(tmp_path, monkeypatch):
    mount = tmp_path / "mnt"
    (mount / "lid").mkdir(parents=True)
    (mount / "lid" / "index.html").write_text(GAME)
    monkeypatch.setattr(fanout, "MOUNT", mount)
    monkeypatch.setattr(fanout, "GAME", tmp_path / "home" / "lid-game")
    for key, value in fanout.LOCAL_IDENTITY.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "home").mkdir()
    said = []
    fanout.init(fanout.GAME, mount / "lid.git", backlog=True, say=said.append)
    assert (fanout.GAME / "index.html").read_text() == GAME and "copied" in said[0]


def test_a_repository_with_its_own_origin_shares_through_another_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(fanout, "MOUNT", tmp_path / "mnt")
    (tmp_path / "mnt").mkdir()
    for key, value in fanout.LOCAL_IDENTITY.items():
        monkeypatch.setenv(key, value)
    repo = _repo(tmp_path)
    fanout.git(repo, "init", "-q", "-b", "main")
    fanout.git(repo, "add", "-A")
    fanout.git(repo, "commit", "-q", "-m", "x")
    fanout.git(repo, "remote", "add", "origin", "https://github.com/example/game.git")
    assert fanout.init(repo, tmp_path / "mnt" / "game.git", backlog=False, say=lambda _: None) == "agents"
    assert fanout.git(repo, "remote", "get-url", "origin") == "https://github.com/example/game.git"
    assert not (repo / "BACKLOG.md").exists(), "a repository of your own gets no game backlog"


def test_the_run_refuses_work_the_agents_could_not_see(shared):
    repo, bare, _, _ = shared
    assert fanout.preflight(repo, bare) == ("origin", "main")
    with (repo / "CLAUDE.md").open("a") as f:
        f.write("- Put the feature in the page title.\n")
    with pytest.raises(SystemExit, match="not committed"):
        fanout.preflight(repo, bare)
    fanout.git(repo, "commit", "-qam", "A rule for every agent")
    with pytest.raises(SystemExit, match="Push it first"):
        fanout.preflight(repo, bare)
    fanout.git(repo, "push", "-q")
    assert fanout.preflight(repo, bare) == ("origin", "main")


def test_a_repository_never_shared_is_told_to_run_init(tmp_path, monkeypatch):
    monkeypatch.setattr(fanout, "MOUNT", tmp_path)
    with pytest.raises(SystemExit, match="--init"):
        fanout.preflight(_repo(tmp_path), tmp_path / "lid.git")


def test_each_agent_reports_its_microvm_age_and_two_isolation_probes():
    inner = shlex.split(fanout.agent_command(3, "feature-03", "/mnt/s3files/lid.git", "build {n} as {name}",
                                             agents=20, clone_dir="/tmp/lid"))[2]
    assert "echo UP $(cut -d. -f1 /proc/uptime)" in inner
    assert "touch /tmp/fanout-agent-03" in inner and "SIBLINGS" in inner and "PEERS $(grep -las" in inner
    assert "git clone -q /mnt/s3files/lid.git /tmp/lid" in inner, "the clone goes to the microVM's own disk"
    assert "WORKSHOP_AGENT_WORKDIR=/tmp/lid /app/run.sh -w feature-03 'build 3 as feature-03'" in inner
    assert "git push -q -f origin worktree-feature-03" in inner
    assert inner.rstrip().endswith("exit $code"), "the command's exit status stays the agent's"
    assert "GIT_AUTHOR_EMAIL=agent-03@fanout.invalid" in inner, "the image's agent user has no identity"
    assert "boot_id" not in inner, "sessions share a boot ID; it identifies nothing"
    assert inner.count("/mnt/s3files") == 1, "only the clone touches the mount; the push goes to its origin"


def test_a_session_commits_what_its_agent_left_and_pushes_only_its_branch(shared, tmp_path):
    repo, bare, _, _ = shared
    run = _session(tmp_path, bare, 2, "feature-02")
    assert run.returncode == 0, run.stdout + run.stderr
    out = run.stdout.splitlines()
    assert any(line.startswith("SIBLINGS 1") for line in out) and "DONE feature-02 feature" in out
    pushed = [line for line in out if line.startswith("PUSHED ")]
    assert pushed and pushed[0].split()[1] == "worktree-feature-02"
    assert fanout.git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == [
        "main", "worktree-feature-02"]
    assert fanout.git(bare, "log", "-1", "--format=%an|%s|%b", "worktree-feature-02") == \
        "Agent 02 (feature-02)|feature-02: feature-02 feature|"
    assert "item 2 as feature-02" in fanout.git(bare, "show", "worktree-feature-02:index.html")
    assert fanout.git(bare, "rev-parse", "main") == fanout.git(repo, "rev-parse", "main"), "main is untouched"


def test_a_failed_agent_still_pushes_what_it_left_but_keeps_its_exit_status(shared, tmp_path):
    _, bare, _, _ = shared
    run = _session(tmp_path, bare, 1, "feature-01", env={"FAKE_EXIT": "1"})
    assert run.returncode == 1 and "PUSHED worktree-feature-01" in run.stdout


def test_the_host_checks_each_branch_out_as_a_worktree_and_reads_it(shared, tmp_path):
    repo, bare, remote, _ = shared
    for i in (1, 2):
        assert _session(tmp_path, bare, i, f"feature-{i:02d}").returncode == 0
    out = tmp_path / "arcade"
    tiles = [fanout.Tile(i, f"feature-{i:02d}",
                         pushed=fanout.git(bare, "rev-parse", f"worktree-feature-{i:02d}"))
             for i in (1, 2)]
    for t in tiles:
        fanout.collect(t, repo, remote, "main", out)
    listed = fanout.worktrees(repo)
    assert listed[(out / "feature-01").resolve()] == "worktree-feature-01"
    assert listed[(out / "feature-02").resolve()] == "worktree-feature-02"
    assert tiles[0].title == "Mina's Dumpling Dash: feature-01"
    assert tiles[0].lines == "+2 -1", "one changed title line and one added paragraph"
    assert fanout.page_title(repo, "main") == "Mina's Dumpling Dash"
    assert (out / "feature-02" / "index.html").read_text().count("item 2 as feature-02") == 1
    # Merging the one you like is plain Git, and it reaches the game in your repository.
    fanout.git(repo, "merge", "-q", "--ff-only", "worktree-feature-02")
    assert "feature-02" in (repo / "index.html").read_text()


def test_a_second_run_moves_the_worktree_to_the_new_branch_but_never_your_edits(shared, tmp_path):
    repo, bare, remote, _ = shared
    out = tmp_path / "arcade"
    assert _session(tmp_path, bare, 1, "feature-01").returncode == 0
    fanout.collect(fanout.Tile(1, "feature-01"), repo, remote, "main", out)
    first = fanout.git(out / "feature-01", "rev-parse", "HEAD")
    # Change the input, so a fast run cannot produce the same tree, parent and
    # second-resolution commit timestamp as its predecessor.
    (repo / "CLAUDE.md").write_text(RULES + "- Show the feature in the title.\n")
    fanout.git(repo, "commit", "-qam", "Update the shared house rules")
    fanout.git(repo, "push", "-q", remote, "main")
    shutil.rmtree(tmp_path / "vm-01")
    assert _session(tmp_path, bare, 1, "feature-01").returncode == 0, "a rerun replaces its own branch"
    again = fanout.Tile(1, "feature-01")
    fanout.collect(again, repo, remote, "main", out)
    second = fanout.git(out / "feature-01", "rev-parse", "HEAD")
    assert second != first and second == fanout.git(bare, "rev-parse", "worktree-feature-01")
    (out / "feature-01" / "index.html").write_text("my own edit\n")
    fanout.git(repo, "commit", "-q", "--allow-empty", "-m", "Next base revision")
    fanout.git(repo, "push", "-q", remote, "main")
    shutil.rmtree(tmp_path / "vm-01")
    assert _session(tmp_path, bare, 1, "feature-01").returncode == 0
    kept = fanout.Tile(1, "feature-01")
    fanout.collect(kept, repo, remote, "main", out)
    assert (out / "feature-01" / "index.html").read_text() == "my own edit\n"
    assert "kept your uncommitted edits" in kept.note


def test_a_folder_that_is_not_a_worktree_is_never_replaced(shared, tmp_path):
    repo, bare, remote, _ = shared
    assert _session(tmp_path, bare, 1, "feature-01").returncode == 0
    out = tmp_path / "arcade"
    (out / "feature-01").mkdir(parents=True)
    (out / "feature-01" / "mine.txt").write_text("keep")
    with pytest.raises(fanout.GitError, match="not a worktree"):
        fanout.collect(fanout.Tile(1, "feature-01"), repo, remote, "main", out)
    assert (out / "feature-01" / "mine.txt").read_text() == "keep"


def test_a_session_line_lands_on_its_tile():
    tile = fanout.Tile(7, "feature-07")
    for line in ("UP 31", "SIBLINGS 1", "PEERS 1", "working", "DONE: night mode with a flashlight",
                 "PUSHED worktree-feature-07 1a2b3c4"):
        fanout.read_line(tile, line)
    assert (tile.up, tile.siblings, tile.peers, tile.state) == ("31", "1", "1", "working")
    assert tile.done_line == "night mode with a flashlight" and tile.pushed == "1a2b3c4"
    failed = fanout.Tile(8, "feature-08")
    fanout.read_line(failed, "PUSH_FAILED agent exit 1")
    assert failed.error == "PUSH_FAILED agent exit 1" and failed.errors


def test_the_board_shows_branch_session_age_isolation_lines_and_title():
    done = fanout.Tile(1, "feature-01", up="31", siblings="1", peers="1", state="done", pushed="abc",
                       lines="+120 -4", title="Mina's Dumpling Dash: Boss Fight")
    done.started, done.finished = time.time() - 90, time.time()
    waiting = fanout.Tile(2, "feature-02")
    board = fanout.render([done, waiting], time.time() - 100, color=False, base_title="Mina's Dumpling Dash")
    head, base, _, row1, row2 = board.splitlines()
    assert "2 agents" in head and "1 microVMs up" in head and "1 branches pushed" in head
    assert base.strip() == "main serves: Mina's Dumpling Dash"
    assert "worktree-feature-01" in row1 and f"session {done.session[:8]}" in row1
    assert "up   31s" in row1 and "sees 1/1" in row1 and "+120 -4" in row1 and row1.endswith("Boss Fight")
    assert "queued" in row2 and "sees ·/·" in row2


def test_the_arcade_page_has_a_card_per_branch_linking_its_worktree(tmp_path):
    tiles = [fanout.Tile(1, "feature-01", state="done", collected=True,
                         title="Dash: Boss <b>", lines="+9 -1",
                         done_line="a boss fish"),
             fanout.Tile(2, "feature-02", state="failed", error="exit 1")]
    page = fanout.write_index(tmp_path, tiles, "Arcade").read_text()
    assert 'href="feature-01/"' in page and "Dash: Boss &lt;b&gt;" in page and "+9 -1" in page
    assert 'class="card failed"' in page and "exit 1" in page
    assert 'href="feature-02/"' not in page and "Worktree unavailable" in page


def test_an_index_someone_else_wrote_is_left_alone(tmp_path):
    tiles = [fanout.Tile(1, "feature-01", state="done")]
    mine = tmp_path / "index.html"
    mine.write_text("<h1>my own arcade page</h1>")
    assert fanout.write_index(tmp_path, tiles, "Arcade") is None
    assert mine.read_text() == "<h1>my own arcade page</h1>"
    mine.unlink()
    assert fanout.write_index(tmp_path, tiles, "Arcade") == mine, "an absent page is written"
    assert fanout.write_index(tmp_path, tiles, "Arcade") == mine, "fanout may rewrite its own page"
    assert mine.read_text().startswith(fanout.INDEX_MARK)


def test_the_backlog_ships_one_item_for_each_of_twenty_agents():
    assert fanout.backlog_items(fanout.BACKLOG_SOURCE.read_text()) == 20
    assert "{n}" in fanout.TASK and "BACKLOG.md" in fanout.TASK and "CLAUDE.md" in fanout.TASK


def test_only_the_bare_repository_is_on_the_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(fanout, "MOUNT", tmp_path / "mnt")
    (tmp_path / "mnt").mkdir()
    assert fanout.in_session(tmp_path / "mnt" / "lid.git") == "/mnt/s3files/lid.git"
    with pytest.raises(SystemExit, match="not on the shared mount"):
        fanout.in_session(tmp_path / "elsewhere.git")


def test_a_session_that_failed_before_any_work_gets_one_new_session():
    calls = []

    def flaky(tile, *args):
        calls.append(tile.session)
        if len(calls) == 1:
            tile.state, tile.error = "failed", "RuntimeClientError: x"
            tile.started, tile.finished = 100.0, 101.0
            tile.errors.append("RuntimeClientError: x")
        else:
            tile.started = 102.0
            assert tile.elapsed(103.0) == 1.0, "the board must time the new session"
            tile.state, tile.up = "done", "25"

    tile = fanout.Tile(1, "feature-01")
    fanout.run_tile_with_one_retry(tile, "arn", "us-east-1", "cmd", threading.Lock(), run=flaky)
    assert tile.state == "done" and tile.retried and len(set(calls)) == 2
    assert tile.errors, "the first failure is kept for the summary"


def test_a_failure_after_work_began_is_a_result_not_a_retry():
    calls = []

    def fails_late(tile, *args):
        calls.append(1)
        tile.up, tile.state, tile.error = "25", "failed", "exit 1"
        tile.errors.append("exit 1")

    tile = fanout.Tile(1, "feature-01")
    fanout.run_tile_with_one_retry(tile, "arn", "us-east-1", "cmd", threading.Lock(), run=fails_late)
    assert len(calls) == 1 and tile.state == "failed" and not tile.retried


def test_the_run_record_is_written_beside_the_worktrees(shared, tmp_path, monkeypatch):
    """main() end to end with the session replaced: the board, the page, and the record."""
    repo, bare, _, _ = shared

    def local_session(tile, arn, region, command, lock):
        tile.started = time.time()
        scratch = tmp_path / f"vm-{tile.index:02d}"
        scratch.mkdir()
        cmd = fanout.agent_command(tile.index, tile.name, str(bare), "item {n}", agents=2,
                                   clone_dir=str(scratch / "lid"), scratch=str(scratch),
                                   launcher=_launcher(scratch), peer_wait=0)
        run = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=60)
        for line in run.stdout.splitlines():
            fanout.read_line(tile, line)
        tile.state = "done" if run.returncode == 0 and tile.pushed else "failed"
        tile.finished = time.time()

    monkeypatch.setattr(fanout, "run_tile", local_session)
    monkeypatch.setenv("ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/claude_code-x")
    out = tmp_path / "arcade"
    code = fanout.main(["--repo", str(repo), "--bare", str(bare), "--agents", "2", "--out", str(out)])
    assert code == 0
    summary = json.loads((out / "fanout-summary.json").read_text())
    assert summary["agents"] == 2 and summary["pushed"] == 2 and summary["finished"] == 2
    assert summary["collected"] == 2
    assert [b["branch"] for b in summary["branches"]] == ["worktree-feature-01", "worktree-feature-02"]
    assert summary["branches"][0]["title"] == "Mina's Dumpling Dash: feature-01"
    assert (out / "index.html").read_text().count('class="card done"') == 2
    assert len(fanout.worktrees(repo)) == 3, "the repository itself and one worktree per branch"


def test_collect_recovers_a_delayed_git_object_without_another_agent(shared, tmp_path, monkeypatch):
    """The actual event exhausted five fetches; the sixth must still be a Git transfer."""
    repo, bare, remote, _ = shared
    run = _session(tmp_path, bare, 13, "feature-13")
    assert run.returncode == 0
    expected = fanout.git(bare, "rev-parse", "worktree-feature-13")
    assert len(expected) == 40 and f"PUSHED worktree-feature-13 {expected}" in run.stdout
    tile = fanout.Tile(13, "feature-13", pushed=expected)
    real_git, attempts, sleeps = fanout.git, [], []

    def temporarily_unreadable(repo, *args, **kwargs):
        if args[0] == "fetch":
            attempts.append(kwargs["timeout"])
            if len(attempts) <= 5:
                raise fanout.GitError(f"fatal: git upload-pack: not our ref {expected}")
        return real_git(repo, *args, **kwargs)

    def pause(seconds):
        assert fanout.GIT_LOCK.acquire(blocking=False), "other branches can collect during the wait"
        fanout.GIT_LOCK.release()
        sleeps.append(seconds)

    monkeypatch.setattr(fanout, "git", temporarily_unreadable)
    monkeypatch.setattr(fanout.time, "sleep", pause)
    out = tmp_path / "arcade"
    fanout.collect(tile, repo, remote, "main", out)
    assert len(attempts) == 6 and len(sleeps) == 5
    assert all(0 < timeout <= 10 for timeout in attempts)
    assert tile.collected and real_git(out / "feature-13", "rev-parse", "HEAD") == expected


def test_collect_stops_at_its_deadline_and_never_links_a_missing_worktree(shared, tmp_path, monkeypatch):
    repo, _, remote, _ = shared
    tile = fanout.Tile(13, "feature-13", state="failed", pushed="a" * 40, done_line="finished the game")
    now, calls = [0.0], []

    def unavailable(repo, *args, **kwargs):
        assert args[0] == "fetch"
        calls.append(kwargs["timeout"])
        now[0] += kwargs["timeout"]
        raise fanout.GitError("fatal: git upload-pack: not our ref " + tile.pushed)

    monkeypatch.setattr(fanout, "git", unavailable)
    monkeypatch.setattr(fanout.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(fanout.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    out = tmp_path / "arcade"
    with pytest.raises(fanout.GitError, match="not our ref") as error:
        fanout.collect(tile, repo, remote, "main", out)
    assert now[0] == fanout.COLLECT_TIMEOUT_S and calls
    assert not tile.collected and not (out / tile.name).exists()
    tile.error = str(error.value)
    page = fanout.write_index(out, [tile], "Arcade").read_text()
    assert "not our ref" in page and "Worktree unavailable" in page
    assert 'href="feature-13/"' not in page


def test_collect_refuses_a_branch_that_moved_since_the_push_receipt(shared, tmp_path):
    repo, bare, remote, _ = shared
    assert _session(tmp_path, bare, 1, "feature-01").returncode == 0
    old_commit = fanout.git(bare, "rev-parse", "main")
    tile = fanout.Tile(1, "feature-01", pushed=old_commit)
    out = tmp_path / "arcade"
    with pytest.raises(fanout.GitError, match="moved: session pushed"):
        fanout.collect(tile, repo, remote, "main", out)
    assert not tile.collected and not (out / tile.name).exists()


def test_run_receipt_distinguishes_pushed_branches_from_ready_worktrees(shared, tmp_path, monkeypatch, capsys):
    repo, bare, _, _ = shared
    real_collect = fanout.collect

    def session(tile, *args):
        run = _session(tmp_path, bare, tile.index, tile.name)
        assert run.returncode == 0
        for line in run.stdout.splitlines():
            fanout.read_line(tile, line)
        tile.state = "done"

    def collect(tile, *args, **kwargs):
        if tile.index == 2:
            raise fanout.GitError("fatal: git upload-pack: not our ref")
        real_collect(tile, *args, **kwargs)

    monkeypatch.setattr(fanout, "run_tile", session)
    monkeypatch.setattr(fanout, "collect", collect)
    monkeypatch.setenv("ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/claude_code-x")
    out = tmp_path / "arcade"
    assert fanout.main(["--repo", str(repo), "--bare", str(bare), "--agents", "2", "--out", str(out)]) == 1
    summary = json.loads((out / "fanout-summary.json").read_text())
    assert summary["pushed"] == 2 and summary["collected"] == summary["finished"] == 1
    assert summary["errors"]["worktree-feature-02"]
    receipt = capsys.readouterr().out
    assert "2 branches pushed" in receipt and "1 worktrees prepared" in receipt
    assert "each checked out" not in receipt and "instead of running all agents again" in receipt
    assert 'href="feature-02/"' not in (out / "index.html").read_text()


def test_paths_in_your_home_print_as_you_would_type_them():
    assert fanout.show(Path.home() / "lid-game") == "~/lid-game"
    assert fanout.show(Path.home()) == "~"
    assert fanout.show("/mnt/s3files/lid.git") == "/mnt/s3files/lid.git"
    assert fanout.show(str(Path.home()) + "-other/x") == str(Path.home()) + "-other/x"


def test_your_own_repository_keeps_its_upstream(tmp_path, monkeypatch):
    monkeypatch.setattr(fanout, "MOUNT", tmp_path / "mnt")
    (tmp_path / "mnt").mkdir()
    for key, value in fanout.LOCAL_IDENTITY.items():
        monkeypatch.setenv(key, value)
    upstream = tmp_path / "github.git"
    fanout.git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    repo = _repo(tmp_path)
    fanout.git(repo, "init", "-q", "-b", "main")
    fanout.git(repo, "add", "-A")
    fanout.git(repo, "commit", "-q", "-m", "x")
    fanout.git(repo, "remote", "add", "origin", str(upstream))
    fanout.git(repo, "push", "-q", "-u", "origin", "main")
    bare = tmp_path / "mnt" / "lid-game.git"
    assert fanout.init(repo, bare, backlog=False, say=lambda _: None) == "agents"
    assert fanout.git(repo, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    assert fanout.push_hint(repo, "agents", "main") == "git push agents main"
    fanout.git(repo, "commit", "-q", "--allow-empty", "-m", "y")
    with pytest.raises(SystemExit, match="git push agents main"):
        fanout.preflight(repo, bare)
