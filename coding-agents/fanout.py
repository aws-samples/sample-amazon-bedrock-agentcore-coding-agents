#!/usr/bin/env python3
"""Run many Claude Code agents on one repository at once: one branch and worktree each.

    python3 fanout.py --init            # ~/lid-game becomes a Git repository with a backlog,
                                        # pushed to a bare repository on the shared mount
    python3 fanout.py --agents 20       # twenty sessions: agent N builds backlog item N
    python3 fanout.py --repo ~/service --init                     # the same, any repository
    python3 fanout.py --repo ~/service --agents 3 --task "... {n} ..."

This is how people run parallel Claude Code on a laptop: one repository, and one branch
and git worktree per agent (Claude Code's own ``claude -w <name>``). The difference is
where each worktree lives. Every agent here is a NEW session on the same Claude Code
Runtime, so every worktree is on its own microVM, with its own memory, processes, and
local disk.

Git data stays on local disk, where Git is fast. The only Git data on the S3 Files mount
is one bare repository (``/mnt/s3files/lid.git``), and only ``clone``, ``fetch``, and
``push`` touch it. Each session clones it to its own /tmp, Claude Code creates its
worktree there (``.claude/worktrees/feature-07`` on branch ``worktree-feature-07``), and
the session commits what the agent left and pushes that one branch. The local worktree
is exchanged through Git; the sessions still share the mounted storage and IAM role.
On the host, the script fetches each branch into your
repository and checks it out as a linked worktree under ~/arcade (``git worktree list``),
where you review, play, and merge.

The board shows, per agent: its branch, its session ID, how long its microVM had been up
when the agent started, two isolation probes, its state, the lines its branch changes
against the base branch (``git diff --shortstat``), and the ``<title>`` its branch's
index.html serves. These are readings, never grades: nothing here judges the code.

The probes count what one agent can see of the others. Each agent leaves a file in its
/tmp and counts the agent files there, and counts the Claude Code processes it can see.
On one shared machine the last agent would count all of them; in separate microVMs every
count is 1. (The kernel's boot ID is no evidence either way: sessions start from the same
prepared image, and a live check on 2026-10-12 found the same boot ID and MAC addresses in
two isolated sessions.)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOUNT = Path(os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files"))
SESSION_MOUNT = "/mnt/s3files"   # where every session of the Runtime mounts the same access point
LAB1 = "lid"                     # Lab 1's folder on the mount
GAME = Path.home() / "lid-game"  # Lab 1 copies that folder here before it publishes the game
BACKLOG = "BACKLOG.md"
BACKLOG_SOURCE = HERE / "game-backlog.md"
NAME = "feature"
OUT = Path.home() / "arcade"
TIMEOUT_S = 900
COLLECT_TIMEOUT_S = 60

TASK = (
    "You are agent {n} of {agents}, working in your own git worktree of this game, on your own "
    "branch. Build item {n} of BACKLOG.md into the game, and follow CLAUDE.md. Keep the game "
    "playable in index.html, with no new libraries, and leave BACKLOG.md as it is. When you are "
    "done, print exactly one final line: DONE <the feature, in a few words>."
)

# For the commits this script makes on the host when your Git has no name or email.
LOCAL_IDENTITY = {"GIT_AUTHOR_NAME": "Workshop (local)", "GIT_AUTHOR_EMAIL": "workshop-local@invalid",
                  "GIT_COMMITTER_NAME": "Workshop (local)", "GIT_COMMITTER_EMAIL": "workshop-local@invalid"}
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str, env: dict | None = None, check: bool = True,
        timeout: float | None = None) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                                env={**os.environ, **(env or {})}, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:3])}: timed out after {timeout:.1f}s") from exc
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout.strip()


def identity(repo: Path) -> dict:
    """No override when your Git already has a name and an email."""
    if git(repo, "config", "user.name", check=False) and git(repo, "config", "user.email", check=False):
        return {}
    return LOCAL_IDENTITY


def is_repo_root(path: Path) -> bool:
    top = git(path, "rev-parse", "--show-toplevel", check=False) if path.is_dir() else ""
    return bool(top) and Path(top).resolve() == path.resolve()


def current_branch(repo: Path) -> str:
    return git(repo, "symbolic-ref", "--short", "HEAD")


def default_bare(repo: Path) -> Path:
    name = LAB1 if repo.resolve() == GAME.resolve() else repo.name
    return MOUNT / f"{name}.git"


def show(path: Path | str) -> str:
    """A path as you would type it: your home as ~."""
    home, text = str(Path.home()), str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def in_session(path: Path) -> str:
    """The same path as a session sees it: every session mounts the access point at /mnt/s3files."""
    try:
        return f"{SESSION_MOUNT}/{path.resolve().relative_to(MOUNT.resolve()).as_posix()}"
    except ValueError:
        sys.exit(f"{path} is not on the shared mount {MOUNT}, so no session could reach it")


def remote_for(repo: Path, bare: Path) -> str | None:
    """The remote of your repository that is the bare repository on the mount, if any."""
    for name in git(repo, "remote", check=False).split():
        url = git(repo, "remote", "get-url", name, check=False)
        if url and not re.match(r"^[a-z]+://|^[^/]+@", url) and Path(url).resolve() == bare.resolve():
            return name
    return None


def backlog_items(text: str) -> int:
    return len(re.findall(r"^\s*\d+\.\s+\S", text, re.M))


# ---------------------------------------------------------------- --init
def init(repo: Path, bare: Path, *, backlog: bool, say=print) -> str:
    """Make the repository shareable with the agents, step by step, and say each step.

    Idempotent: a step that is already done is reported, never repeated. Returns the
    name of the remote that is the bare repository.
    """
    lab1 = MOUNT / LAB1
    if not repo.exists() and repo.resolve() == GAME.resolve() and (lab1 / "index.html").is_file():
        shutil.copytree(lab1, repo)
        say(f"  copied {lab1} to {show(repo)}, as Lab 1 does before it publishes the game")
    if not repo.is_dir():
        sys.exit(f"{show(repo)} does not exist")
    env = identity(repo)
    if is_repo_root(repo):
        say(f"  {show(repo)} is already a Git repository (branch {current_branch(repo)})")
    else:
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", "The game from Lab 1", env=env)
        say(f"  made {show(repo)} a Git repository: branch main, first commit {git(repo, 'rev-parse', '--short', 'HEAD')}")
    base = current_branch(repo)
    if backlog:
        target = repo / BACKLOG
        if target.exists():
            say(f"  {BACKLOG} is already there ({backlog_items(target.read_text())} items)")
        else:
            shutil.copyfile(BACKLOG_SOURCE, target)
            git(repo, "add", BACKLOG)
            git(repo, "commit", "-q", "-m", "Add a backlog: one feature idea per agent", env=env)
            say(f"  added {BACKLOG}: {backlog_items(target.read_text())} feature ideas, one per agent "
                f"(commit {git(repo, 'rev-parse', '--short', 'HEAD')})")
    if (bare / "HEAD").is_file():
        say(f"  {bare} already exists")
    else:
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(bare.parent, "init", "-q", "--bare", "-b", base, str(bare))
        say(f"  created the bare repository {bare}: the only Git data on the shared mount")
    remote = remote_for(repo, bare)
    if remote:
        say(f"  {repo.name}'s remote {remote} is already {bare}")
    else:
        remote = "agents" if "origin" in git(repo, "remote", check=False).split() else "origin"
        git(repo, "remote", "add", remote, str(bare))
        say(f"  set {bare} as {repo.name}'s {remote}")
    # Track the mount only when nothing else is tracked: a repository of your own keeps
    # its upstream (for example GitHub), and you push to the agents' remote by name.
    tracked = git(repo, "rev-parse", "--abbrev-ref", f"{base}@{{upstream}}", check=False)
    git(repo, "push", "-q", *([] if tracked and tracked != f"{remote}/{base}" else ["-u"]), remote, base)
    say(f"  pushed {base} to {remote} ({git(repo, 'rev-parse', '--short', base)})")
    return remote


def push_hint(repo: Path, remote: str, base: str) -> str:
    """How to push base to the agents' remote from this repository."""
    tracked = git(repo, "rev-parse", "--abbrev-ref", f"{base}@{{upstream}}", check=False)
    return "git push" if tracked == f"{remote}/{base}" else f"git push {remote} {base}"


def preflight(repo: Path, bare: Path) -> tuple[str, str]:
    """The agents clone the bare repository, so what you have not pushed, they cannot see."""
    remote = remote_for(repo, bare) if is_repo_root(repo) else None
    if not (bare / "HEAD").is_file() or not remote:
        sys.exit(f"No shared repository for {show(repo)} yet. Run: python3 fanout.py --init"
                 + ("" if repo.resolve() == GAME.resolve() else f" --repo {show(repo)}"))
    base = current_branch(repo)
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        sys.exit(f"{show(repo)} has changes you have not committed:\n{dirty}\n"
                 f"The agents clone {show(bare)}, so they would not see them. Commit and push first:\n"
                 f"  cd {show(repo)} && git commit -am \"<what you changed>\" && {push_hint(repo, remote, base)}")
    local = git(repo, "rev-parse", base)
    shared = git(bare, "rev-parse", "--verify", "-q", base, check=False)
    if local != shared:
        sys.exit(f"{base} in {show(repo)} ({local[:7]}) is not the {base} the agents will clone from "
                 f"{show(bare)} ({shared[:7] or 'none yet'}). Push it first:\n"
                 f"  cd {show(repo)} && {push_hint(repo, remote, base)}")
    return remote, base


# ---------------------------------------------------------------- one agent, in its own session
def agent_command(index: int, name: str, clone_url: str, task: str, *, agents: int,
                  clone_dir: str = "/tmp/repo", scratch: str = "/tmp", launcher: str = "/app/run.sh",
                  peer_wait: int = 8) -> str:
    """The command one session runs: probe isolation, clone, run Claude Code in its own
    worktree, then commit what the agent left and push that one branch.

    UP is the microVM's uptime when the agent starts. SIBLINGS counts agent files in /tmp
    after this agent writes its own. PEERS counts the Claude Code processes this agent can
    see once its own is running. Both are 1 in an isolated microVM. The exit status is the
    agent's, unless the clone or the push fails.
    """
    q = shlex.quote
    branch = f"worktree-{name}"
    prompt = task.format(n=index, name=name, branch=branch, agents=agents)
    worktree = f"{clone_dir}/.claude/worktrees/{name}"
    out = f"{scratch}/agent-{index:02d}.out"
    who, mail = f"Agent {index:02d} ({name})", f"agent-{index:02d}@fanout.invalid"
    lines = [
        "echo UP $(cut -d. -f1 /proc/uptime)",
        f"touch {scratch}/fanout-agent-{index:02d}; echo SIBLINGS $(ls {scratch}/fanout-agent-* | wc -l)",
        # The image's agent user has no Git identity; the commits get one per agent.
        f"export GIT_AUTHOR_NAME={q(who)} GIT_AUTHOR_EMAIL={mail} GIT_COMMITTER_NAME={q(who)} "
        f"GIT_COMMITTER_EMAIL={mail}",
        f"rm -rf {q(clone_dir)}; git clone -q {q(clone_url)} {q(clone_dir)} || {{ echo CLONE_FAILED; exit 90; }}",
        f"cd {q(clone_dir)}",
        # stdin is /dev/null on purpose: a one-shot CLI with no terminal can wait on an
        # open stdin for a prompt it already has (`codex exec` does).
        f"WORKSHOP_AGENT_WORKDIR={q(clone_dir)} {launcher} -w {q(name)} {q(prompt)} < /dev/null > {out} 2>&1 & agent=$!",
        f"sleep {int(peer_wait)}; echo PEERS $(grep -las '[-]-dangerously-skip-permissions' "
        "/proc/[0-9]*/cmdline | wc -l)",
        "wait $agent; code=$?",
        f"cat {out}",
        f"done=$(grep '^DONE' {out} | tail -n 1 | sed 's/^DONE[: ]*//')",
        f"if [ -d {q(worktree)} ]; then git -C {q(worktree)} add -A; git -C {q(worktree)} diff --cached --quiet"
        f" || git -C {q(worktree)} commit -q -m \"{name}: ${{done:-the changes its agent left}}\"; fi",
        f"git push -q -f origin {q(branch)} || {{ echo \"PUSH_FAILED agent exit $code\"; exit 91; }}",
        f"echo PUSHED {branch} $(git rev-parse {q(branch)})",
        "exit $code",
    ]
    return "sh -c " + q("\n".join(lines))


@dataclass
class Tile:
    index: int
    name: str
    session: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: str = "queued"
    up: str = ""
    siblings: str = ""
    peers: str = ""
    last: str = ""
    done_line: str = ""
    pushed: str = ""   # the commit the session pushed
    collected: bool = False  # the host actually prepared the worktree
    lines: str = ""    # +insertions -deletions against the base branch
    title: str = ""    # the <title> the branch's index.html serves
    note: str = ""
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    errors: list = field(default_factory=list)  # every failure, whole, for the summary
    retried: bool = False

    @property
    def branch(self) -> str:
        return f"worktree-{self.name}"

    def elapsed(self, now: float) -> float:
        if not self.started:
            return 0.0
        return (self.finished or now) - self.started


def read_line(tile: Tile, line: str) -> None:
    """One line of a session's output, onto its tile."""
    line = line.strip()
    if not line:
        return
    if line.startswith("UP "):
        tile.up, tile.state = line.split()[-1], "working"
    elif line.startswith("SIBLINGS "):
        tile.siblings = line.split()[-1]
    elif line.startswith("PEERS "):
        tile.peers = line.split()[-1]
    elif line.startswith("PUSHED "):
        tile.pushed = line.split()[-1]
    elif line.startswith(("PUSH_FAILED", "CLONE_FAILED")):
        tile.error = line[:120]
        tile.errors.append(f"session {tile.session}: {line}"[:600])
    else:
        if line.startswith("DONE"):
            tile.done_line = line.removeprefix("DONE").strip(" :")[:120]
        tile.last = line[:120]


def run_tile(tile: Tile, arn: str, region: str, command: str, lock: threading.Lock) -> None:
    import boto3
    from botocore.config import Config
    client = boto3.client("bedrock-agentcore", region_name=region,
                          config=Config(read_timeout=TIMEOUT_S, retries={"max_attempts": 2}))
    tile.started, tile.state = time.time(), "starting"
    try:
        resp = client.invoke_agent_runtime_command(
            agentRuntimeArn=arn, runtimeSessionId=tile.session,
            body={"command": command, "timeout": TIMEOUT_S})
        buffer, code = "", None
        for event in resp["stream"]:
            chunk = event.get("chunk") or {}
            delta = chunk.get("contentDelta") or {}
            text = (delta.get("stdout") or "") + (delta.get("stderr") or "")
            if text:
                buffer += text
                *lines, buffer = buffer.split("\n")
                with lock:
                    for line in lines:
                        read_line(tile, line)
            if "contentStop" in chunk:
                code = chunk["contentStop"].get("exitCode")
        with lock:
            read_line(tile, buffer)
            tile.state = "done" if code == 0 and tile.pushed else "failed"
            if tile.state == "failed" and not tile.error:
                tile.error = f"exit {code}" + ("" if tile.pushed else ", no branch pushed")
    except Exception as exc:  # noqa: BLE001 (shown on the board, never hidden)
        tile.state, tile.error = "failed", f"{type(exc).__name__}: {exc}"[:120]
        tile.errors.append(f"session {tile.session}: {type(exc).__name__}: {exc}"[:600])
    finally:
        tile.finished = time.time()
        try:
            client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=tile.session)
        except Exception:  # noqa: BLE001 (the session may already have ended)
            pass


def run_tile_with_one_retry(tile: Tile, arn: str, region: str, command: str,
                            lock: threading.Lock, run=None) -> None:
    """A session that failed before its agent reported anything did no work, so it gets
    one more try in a NEW session (a new microVM). A failure after work began is a
    result: it stays on the board, never retried."""
    run = run or run_tile
    run(tile, arn, region, command, lock)
    if tile.state == "failed" and not tile.up and tile.errors and not tile.retried:
        with lock:
            tile.retried, tile.session = True, str(uuid.uuid4())
            tile.state, tile.error, tile.last = "starting", "", ""
            tile.finished = 0.0
        run(tile, arn, region, command, lock)


# ---------------------------------------------------------------- back on the host
GIT_LOCK = threading.Lock()  # one Git command at a time in your repository


def worktrees(repo: Path) -> dict[Path, str]:
    """{path: branch} for every worktree of the repository."""
    found, path = {}, None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):]).resolve()
            found[path] = ""
        elif line.startswith("branch ") and path is not None:
            found[path] = line[len("branch "):].removeprefix("refs/heads/")
    return found


def add_worktree(repo: Path, branch: str, start: str, path: Path) -> str:
    """Check the branch out at path as a linked worktree of the repository, or move the
    worktree already there to start. Your own uncommitted edits in it are never reset.
    Returns a note for the board, empty when there is nothing to say."""
    git(repo, "worktree", "prune")
    if path.resolve() in worktrees(repo):
        if git(path, "status", "--porcelain"):
            return "kept your uncommitted edits in its worktree"
        git(path, "reset", "-q", "--hard", start)
        return ""
    if path.exists():
        raise GitError(f"{path} exists and is not a worktree of {repo}")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", "-B", branch, str(path), start)
    return ""


def shortstat(repo: Path, base: str, ref: str) -> str:
    """+insertions -deletions of ref against where it left base (git diff base...ref)."""
    out = git(repo, "diff", "--shortstat", f"{base}...{ref}")
    plus, minus = re.search(r"(\d+) insertion", out), re.search(r"(\d+) deletion", out)
    return f"+{plus.group(1) if plus else 0} -{minus.group(1) if minus else 0}"


def page_title(repo: Path, ref: str, page: str = "index.html") -> str:
    match = TITLE.search(git(repo, "show", f"{ref}:{page}", check=False))
    return html.unescape(" ".join(match.group(1).split()))[:60] if match else ""


def collect(tile: Tile, repo: Path, remote: str, base: str, out: Path, tries: int = 31) -> None:
    """Wait at most a minute for the pushed Git objects to become readable on this
    mount, then prepare the worktree. A push receipt alone is not a ready worktree.

    A fresh event returned ``upload-pack: not our ref`` through all five old retries
    (eight seconds), although the exact pushed commit became readable later. Retrying
    this host-side transfer never starts another agent or changes the pushed branch.
    """
    ref = f"{remote}/{tile.branch}"
    deadline = time.monotonic() + COLLECT_TIMEOUT_S
    last_error = GitError(f"Timed out collecting {tile.branch}")
    for attempt in range(tries):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise last_error
        try:
            with GIT_LOCK:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise last_error
                git(repo, "fetch", "-q", remote, f"+refs/heads/{tile.branch}:refs/remotes/{ref}",
                    timeout=min(10.0, remaining))
            break
        except GitError as exc:
            last_error = exc
            remaining = deadline - time.monotonic()
            if attempt == tries - 1 or remaining <= 0:
                raise
            # Do not hold up the other agents' Git operations while this branch waits.
            time.sleep(min(2.0, remaining))
    else:
        raise last_error
    with GIT_LOCK:
        fetched = git(repo, "rev-parse", f"{ref}^{{commit}}")
        if tile.pushed and not fetched.startswith(tile.pushed):
            raise GitError(f"{tile.branch} moved: session pushed {tile.pushed}, fetched {fetched}; "
                           "leaving its worktree unchanged")
        tile.note = add_worktree(repo, tile.branch, ref, out / tile.name)
        tile.lines = shortstat(repo, base, ref)
        tile.title = page_title(repo, ref)
        tile.collected = True


def agent_job(tile: Tile, arn: str, region: str, command: str, lock: threading.Lock,
              repo: Path, remote: str, base: str, out: Path) -> None:
    run_tile_with_one_retry(tile, arn, region, command, lock)
    if not tile.pushed:
        return
    agent_state = tile.state
    if agent_state == "done":
        with lock:
            tile.state = "collecting"
    try:
        collect(tile, repo, remote, base, out)
        with lock:
            tile.state = agent_state
    except GitError as exc:
        with lock:
            tile.state, tile.error = "failed", f"host: {exc}"[:120]
            tile.errors.append(f"host: {exc}"[:600])


COLORS = {"queued": "90", "starting": "33", "working": "36", "collecting": "33", "done": "32", "failed": "31"}
DOTS = {"queued": "·", "starting": "◌", "working": "●", "collecting": "◌", "done": "✓", "failed": "✗"}


def render(tiles: list[Tile], started: float, color: bool = True, base: str = "main",
           base_title: str = "") -> str:
    now = time.time()
    vms = sum(bool(t.up) for t in tiles)
    done = sum(t.state == "done" for t in tiles)
    pushed = sum(bool(t.pushed) for t in tiles)
    head = (f"  {len(tiles)} agents  ·  {vms} microVMs up  ·  {done} finished  ·  "
            f"{pushed} branches pushed  ·  {now - started:5.0f}s")
    rows = [head]
    if base_title:
        rows.append(f"  {base} serves: {base_title}")
    rows.append("  " + "─" * (len(head) - 2))
    width = max(len(t.branch) for t in tiles) if tiles else 0
    for t in tiles:
        seen = f"{t.siblings or '·'}/{t.peers or '·'}"
        tail = t.error if t.state == "failed" else (t.title or t.last)
        line = (f"  {DOTS[t.state]} {t.branch:{width}}  session {t.session[:8]}  "
                f"up {(t.up + 's') if t.up else '·':>5}  sees {seen:3}  {t.state:8} {t.elapsed(now):5.0f}s  "
                f"{(t.lines or ''):11} {'(second session) ' if t.retried else ''}{tail}")[:170]
        rows.append(f"\033[{COLORS[t.state]}m{line}\033[0m" if color else line)
    return "\n".join(rows)


# The first line of every index page fanout writes; a page without it is someone else's.
INDEX_MARK = "<!-- written by coding-agents/fanout.py; rerunning the job rewrites it -->\n"


def write_index(out: Path, tiles: list[Tile], heading: str) -> Path | None:
    """One page with a card per branch, linking to its worktree, so twenty branches of a
    game are an arcade. None when out/ already has an index.html fanout did not write."""
    cards = []
    for t in tiles:
        name = html.escape(t.name)
        title = html.escape(t.title or t.name)
        meta = html.escape(" · ".join(x for x in (t.branch, t.lines) if x))
        note = html.escape(t.error or t.done_line or t.state)
        body = f'<b>{title}</b><small>{meta}</small><span>{note}</span>'
        if t.collected:
            cards.append(f'<a class="card {t.state}" href="{name}/">{body}</a>')
        else:
            cards.append(f'<div class="card {t.state}">{body}<span>Worktree unavailable</span></div>')
    page = ("<!doctype html><meta charset=utf-8><title>Arcade</title><style>"
            "body{font:16px system-ui;background:#0b1020;color:#e8ecff;margin:2rem}"
            "main{display:grid;grid-template-columns:repeat(auto-fill,minmax(16.5rem,1fr));gap:1rem}"
            ".card{display:flex;flex-direction:column;gap:.4rem;padding:1rem;border-radius:.6rem;"
            "background:#18214a;color:inherit;text-decoration:none}.card:hover{background:#26337a}"
            ".failed{opacity:.5}small{font:.8rem ui-monospace,monospace;color:#8f9bd6;white-space:nowrap}"
            "span{font-size:.9rem;color:#b8c2ff}</style>"
            f"<h1>{html.escape(heading)}</h1><main>{''.join(cards)}</main>\n")
    target = out / "index.html"
    if target.exists() and INDEX_MARK not in target.read_text(errors="replace"):
        return None
    out.mkdir(parents=True, exist_ok=True)
    target.write_text(INDEX_MARK + page)
    return target


def runtime_arn(role: str = "claude-code") -> str:
    """$ARN if set, else a recorded deploy, else the Runtime named after the role."""
    if os.environ.get("ARN", "").startswith("arn:"):
        return os.environ["ARN"]
    config = HERE / role / "runtime_config.json"
    if config.exists():
        return json.loads(config.read_text())["runtime_arn"]
    import boto3  # noqa: PLC0415
    name = role.replace("-", "_")
    pages = boto3.client("bedrock-agentcore-control").get_paginator("list_agent_runtimes")
    for page in pages.paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return runtime["agentRuntimeArn"]
    sys.exit(f"No Runtime named {name} yet. Create it first (Lab 1, Create the Runtime and Open a Session, step 1).")


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value)).absolute()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true",
                    help="make the repository a Git repository with a bare copy on the mount, and push it")
    ap.add_argument("--agents", type=int, default=10, help="how many sessions (microVMs) to start")
    ap.add_argument("--task", default=TASK,
                    help="the job; {n} is the agent's number, {name} its worktree, {agents} how many")
    ap.add_argument("--repo", type=_path, default=GAME, help="your repository (default ~/lid-game)")
    ap.add_argument("--bare", type=_path, default=None,
                    help=f"the bare repository on the mount (default {MOUNT}/lid.git for the game, "
                         f"else {MOUNT}/<repo name>.git)")
    ap.add_argument("--name", default=NAME, help="worktree names are <name>-01, <name>-02, ...")
    ap.add_argument("--out", type=_path, default=None,
                    help="where the host checks out each branch (default ~/arcade for the game, "
                         "else <repo>-worktrees next to it)")
    a = ap.parse_args(argv)
    if not 1 <= a.agents <= 50:
        ap.error("--agents must be between 1 and 50")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", a.name):
        ap.error("--name takes lowercase letters, digits, and hyphens")
    if not MOUNT.is_dir():
        sys.exit(f"{MOUNT} is not mounted on this host")
    repo, bare = a.repo, a.bare or default_bare(a.repo)
    game = repo.resolve() == GAME.resolve()
    a.out = a.out or (OUT if game else repo.parent / f"{repo.name}-worktrees")
    in_session(bare)
    if a.init:
        print(f"Sharing {show(repo)} with the agents:")
        remote = init(repo, bare, backlog=a.task == TASK and game)
        print(f"\n  Next: add your rule to {show(repo)}/CLAUDE.md, then commit and push it:\n"
              f"  cd {show(repo)} && git commit -am \"<your rule>\" && {push_hint(repo, remote, current_branch(repo))}")
        return 0 if remote else 1
    remote, base = preflight(repo, bare)
    if a.task == TASK:
        listed = backlog_items(git(repo, "show", f"{base}:{BACKLOG}", check=False))
        if listed < a.agents:
            sys.exit(f"{BACKLOG} on {base} lists {listed} items, so start at most {listed} agents, "
                     f"or add items and push them." if listed else
                     f"The default job builds item N of {BACKLOG}, and {repo} has none on {base}. "
                     f"Run --init, or pass your own --task.")
    arn = runtime_arn()
    region = arn.split(":")[3]
    url, clone_dir = in_session(bare), f"/tmp/{bare.name.removesuffix('.git')}"
    tiles = [Tile(i, f"{a.name}-{i:02d}") for i in range(1, a.agents + 1)]
    base_title = page_title(repo, base)
    lock = threading.Lock()
    started = time.time()
    threads = [threading.Thread(target=agent_job, daemon=True, args=(
        t, arn, region, agent_command(t.index, t.name, url, a.task, agents=a.agents, clone_dir=clone_dir),
        lock, repo, remote, base, a.out)) for t in tiles]
    for th in threads:
        th.start()
    tty = sys.stdout.isatty()
    lines = 0
    while True:
        alive = any(th.is_alive() for th in threads)
        with lock:
            frame = render(tiles, started, color=tty, base=base, base_title=base_title)
        if tty:
            sys.stdout.write(("\033[F" * lines) + "\033[J" + frame + "\n")
            lines = frame.count("\n") + 1
        sys.stdout.flush()
        if not alive:
            break
        time.sleep(0.5)
    if not tty:
        print(frame)
    wall = time.time() - started
    serial = sum(t.elapsed(time.time()) for t in tiles)
    pushed = [t for t in tiles if t.pushed]
    collected = [t for t in tiles if t.collected]
    print(f"\n  Wall clock {wall:.0f}s for {len(tiles)} agents. One after another, the same work "
          f"would have taken about {serial / 60:.0f} min.")
    counts = sorted({t.siblings for t in tiles if t.siblings})
    peers = sorted({t.peers for t in tiles if t.peers})
    print(f"  {sum(bool(t.up) for t in tiles)} sessions, each its own microVM. What each agent could see "
          f"of the others: agent files in /tmp {', '.join(counts) or '?'}; Claude Code processes "
          f"{', '.join(peers) or '?'} (1 means only its own).")
    for t in tiles:
        for err in t.errors:
            print(f"  {t.branch}: {err}")
        if t.note:
            print(f"  {t.branch}: {t.note}")
    print(f"  {len(pushed)} branches pushed to {show(bare)}; "
          f"{len(collected)} worktrees prepared in {show(a.out)}:")
    print(f"    git -C {show(repo)} worktree list")
    if write_index(a.out, tiles, f"Arcade: {len(tiles)} branches of {repo.name}, one microVM each"):
        print(f"  One page for all of them: {show(a.out)}/index.html")
    else:
        print(f"  Left {show(a.out)}/index.html alone: fanout did not write it.")
    if collected:
        print(f"  Merge the one you like:  cd {show(repo)} && git merge {collected[0].branch}")
    if len(collected) < len(pushed):
        print("  A pushed branch could not be collected. Keep this result; retry the Git fetch "
              "for that branch instead of running all agents again.")
    # A record of this run for progress.py; it describes the run and judges nothing.
    summary = {"at": int(time.time()), "agents": len(tiles), "repo": str(repo), "bare": str(bare),
               "base": base, "finished": sum(t.state == "done" for t in tiles), "pushed": len(pushed),
               "collected": len(collected),
               "microvms": sum(bool(t.up) for t in tiles), "sibling_counts": counts, "peer_counts": peers,
               "wall_s": round(wall), "serial_s": round(serial), "retried": sum(t.retried for t in tiles),
               "branches": [{"branch": t.branch, "session": t.session, "state": t.state, "commit": t.pushed,
                             "collected": t.collected, "lines": t.lines,
                             "title": t.title, "done": t.done_line} for t in tiles],
               "errors": {t.branch: t.errors for t in tiles if t.errors}}
    try:
        (a.out / "fanout-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    except OSError:
        pass
    return 0 if all(t.state == "done" for t in tiles) else 1


if __name__ == "__main__":
    sys.exit(main())
