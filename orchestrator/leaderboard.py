#!/usr/bin/env python3
"""Report your team's best score to the event leaderboard.

    python3 orchestrator/leaderboard.py --game http://127.0.0.1:8000 \
        --leaderboard https://<api-id>.execute-api.<region>.amazonaws.com/live/

Why this exists. The room's Lab 2 build is a browser game each team runs on its own
box, and the wow moment is a single leaderboard on the projector that every team's
scores land on. The game itself knows nothing about that board: the request asks it
only for a local `GET /scores`. This bridge reads that table every few seconds and,
whenever the team's best improves, posts it to the central account's leaderboard.

Identity comes for free. The post is SigV4-signed with the box's own instance role,
and the leaderboard authorizes it with IAM, so the central account learns which AWS
account (which team) scored without any token being minted, pushed, or pasted. A team
can only ever post as itself.

Reporting only: this never writes to the game, and a broken bridge cannot change a
score anywhere but on the board. Stdlib plus botocore, which every workshop box has
(the AWS CLI depends on it).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

POLL_INTERVAL_S = 5.0
# A score has to be a real integer within a sane range; the leaderboard enforces the
# same rule, so a game that prints "Infinity" fails here with a readable line instead
# of a 400 from the other end.
MAX_SCORE = 10**9

# The agent chooses its own routes, and nothing in this repository tells it what to name
# them: the skill asks for a real high-score table read and written through its API, and
# stops there. So DISCOVER the endpoint instead of pinning one. First readable candidate
# wins, and `--scores-path` overrides when a team named it something else entirely.
SCORE_PATHS = ("scores", "api/scores", "highscores", "api/highscores",
               "high-scores", "api/high-scores", "leaderboard", "api/leaderboard")


def best_entry(table: Any) -> dict[str, Any] | None:
    """The top entry of a game's `GET /scores` table, or None when there is none.

    Tolerant on shape: the request asks for a JSON array of {player, score, at}, but
    a game that wraps it (`{"scores": [...]}`) or names the fields slightly differently
    (`name` for player) is still readable. Anything without an integer score is skipped
    rather than crashing the bridge: the board should show what CAN be read.
    """
    if isinstance(table, dict):
        for key in ("scores", "items", "results", "data"):
            if isinstance(table.get(key), list):
                table = table[key]
                break
    if not isinstance(table, list):
        return None
    best: dict[str, Any] | None = None
    for row in table:
        if not isinstance(row, dict):
            continue
        score = row.get("score")
        if isinstance(score, bool) or not isinstance(score, int):
            try:
                score = int(str(score))
            except (TypeError, ValueError):
                continue
        if score < 0 or score > MAX_SCORE:
            continue
        if best is None or score > best["score"]:
            best = {"score": score,
                    "player": str(row.get("player") or row.get("name") or "anonymous")[:40]}
    return best


def read_json(url: str, timeout_s: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout_s) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def find_scores_url(game_url: str, explicit: str = "",
                    timeout_s: float = 5.0) -> tuple[str, Any]:
    """(url, table) for the game's score endpoint. Raises OSError if none answers.

    Tries the conventional names in order and accepts the first that returns something
    `best_entry` can read, so a team whose agent called it `api/highscores` needs no
    flag. The last connection error is re-raised, because "your game is not up" and
    "your game has no score endpoint" are different problems and the message should say
    which.
    """
    base = game_url.rstrip("/")
    candidates = [explicit.strip("/")] if explicit else list(SCORE_PATHS)
    last: Exception | None = None
    for path in candidates:
        url = f"{base}/{path}"
        try:
            table = read_json(url, timeout_s)
        except (urllib.error.HTTPError, ValueError) as exc:
            last = exc          # answered, but not with a score table
            continue
        except (urllib.error.URLError, OSError) as exc:
            raise OSError(f"{base} is not answering ({exc})") from exc
        if best_entry(table) is not None or isinstance(table, (list, dict)):
            return url, table
    raise OSError(
        f"no score endpoint found under {base} (tried {', '.join(candidates)}); "
        f"pass --scores-path with the path your game actually serves. Last: {last}")


def _region_of(url: str) -> str:
    """`https://abc.execute-api.us-west-2.amazonaws.com/live/` -> us-west-2."""
    host = urllib.parse.urlparse(url).hostname or ""
    parts = host.split(".")
    if len(parts) >= 4 and parts[1] == "execute-api":
        return parts[2]
    raise SystemExit(f"not an API Gateway URL (cannot derive its region): {url}")


def signed_post(leaderboard_url: str, body: dict[str, Any],
                timeout_s: float = 10.0) -> dict[str, Any]:
    """POST to the leaderboard's /scores, SigV4-signed with this box's credentials."""
    from botocore.auth import SigV4Auth  # noqa: PLC0415 (only the post path needs it)
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415
    from botocore.session import Session  # noqa: PLC0415

    url = leaderboard_url.rstrip("/") + "/scores"
    creds = Session().get_credentials()
    if creds is None:
        raise SystemExit("no AWS credentials: run this on the workshop box, whose "
                         "instance role is what the leaderboard authorizes")
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = AWSRequest(method="POST", url=url, data=data,
                         headers={"Content-Type": "application/json"})
    SigV4Auth(creds.get_frozen_credentials(), "execute-api",
              _region_of(leaderboard_url)).add_auth(request)
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers=dict(request.headers.items()))
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SystemExit(f"leaderboard refused the score: HTTP {exc.code} {detail}") from exc


def _announce(result: dict[str, Any], best: dict[str, Any]) -> str:
    rank = result.get("rank")
    teams = result.get("teams")
    where = f"rank #{rank}" + (f" of {teams}" if teams else "") if rank else "posted"
    crown = "  <- top of the room!" if rank == 1 else ""
    return (f"  ^ {result.get('team', 'your team')}: best {best['score']} "
            f"({best['player']}) -> {where}{crown}")


def run(game_url: str, leaderboard_url: str, game_name: str, once: bool,
        interval_s: float, out=sys.stdout, scores_path: str = "") -> int:
    last_posted = -1
    scores_url = ""
    print(f"reporting {game_url} to {leaderboard_url}", file=out, flush=True)
    while True:
        try:
            if not scores_url:
                scores_url, table = find_scores_url(game_url, scores_path)
                print(f"  found the score table at {scores_url}", file=out, flush=True)
            else:
                table = read_json(scores_url)
            best = best_entry(table)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"  ... {exc}", file=out, flush=True)
            scores_url = ""       # rediscover: the game may still be starting
            best = None
        if best and best["score"] > last_posted:
            result = signed_post(leaderboard_url, {
                "score": best["score"], "player": best["player"], "game": game_name})
            last_posted = best["score"]
            print(_announce(result, best), file=out, flush=True)
        if once:
            return 0
        time.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", default="http://127.0.0.1:8000",
                    help="where your team's game is listening (default %(default)s)")
    ap.add_argument("--leaderboard", required=True,
                    help="the LeaderboardUrl from Event Outputs")
    ap.add_argument("--name", default="arcade", help="how the board labels your game")
    ap.add_argument("--scores-path", default="",
                    help="your game's score path, if it is not one of the usual names")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL_S)
    ap.add_argument("--once", action="store_true", help="report the current best and exit")
    args = ap.parse_args(argv)
    try:
        return run(args.game, args.leaderboard, args.name, args.once, args.interval,
                   scores_path=args.scores_path)
    except KeyboardInterrupt:
        print("\nstopped reporting. Your game is unaffected; your scores stay on the board.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
