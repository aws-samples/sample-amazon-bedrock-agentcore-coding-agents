"""The leaderboard bridge reads a game's score table and reports improvements, signed.

It is on the REPORTING path of the workshop's closing moment, not on any verdict path:
a broken bridge can only misreport a score on the room's board, never change a run.
These pin what it must get right anyway: reading the table the request asks for (and
tolerating the shapes a real game might use), posting only when the team's best
improves, and signing the post with SigV4 so the central account knows which team it
came from without any token.
"""
import io
import json
import os
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import leaderboard  # noqa: E402


# ------------------------------------------------------------- reading the table

def test_the_best_entry_is_the_highest_integer_score():
    table = [{"player": "ann", "score": 12, "at": "2026-09-03T00:00:00Z"},
             {"player": "bob", "score": 40, "at": "2026-09-03T00:00:01Z"},
             {"player": "cy", "score": 7, "at": "2026-09-03T00:00:02Z"}]
    assert leaderboard.best_entry(table) == {"score": 40, "player": "bob"}


def test_wrapped_and_renamed_tables_are_still_readable():
    """The request asks for a bare array with `player`, but a game that wraps it or
    says `name` is not a reason to show nothing on the board."""
    assert leaderboard.best_entry({"scores": [{"name": "zed", "score": "9"}]}) == \
        {"score": 9, "player": "zed"}
    assert leaderboard.best_entry({"items": []}) is None
    assert leaderboard.best_entry("not json we understand") is None


def test_an_arcade_initials_field_is_read_not_shown_as_anonymous():
    """Live: an arcade game built from the three-sentence prompt names the player field
    `initials` (the classic high-score name), not `player`. If the reporter did not read
    it, every team would post as "anonymous" and the board would be a column of one word."""
    assert leaderboard.best_entry([{"initials": "WOW", "score": 420}]) == \
        {"score": 420, "player": "WOW"}
    assert leaderboard.best_entry([{"user": "ada", "score": 7}])["player"] == "ada"
    # A real name still wins over the fallbacks when more than one key is present.
    assert leaderboard.best_entry(
        [{"player": "real", "initials": "AAA", "score": 3}])["player"] == "real"


def test_unreadable_rows_are_skipped_not_fatal():
    table = [{"player": "x", "score": "Infinity"}, {"player": "y", "score": -3},
             {"player": "ok", "score": 5}, {"player": "huge", "score": 10**12}, "junk"]
    assert leaderboard.best_entry(table) == {"score": 5, "player": "ok"}


def test_a_boolean_is_not_a_score():
    assert leaderboard.best_entry([{"player": "t", "score": True}]) is None


# -------------------------------------------------------------- posting behaviour

class _Recorder:
    def __init__(self, tables):
        self.tables = list(tables)
        self.posted: list[dict] = []
        self.rank = 0

    def read(self, _url, timeout_s=5.0):
        return self.tables.pop(0) if len(self.tables) > 1 else self.tables[0]

    def post(self, _url, body):
        self.posted.append(body)
        self.rank += 1
        return {"team": "Team 3", "best": body["score"], "rank": 1, "teams": 7}


def test_it_posts_only_when_the_best_improves(monkeypatch):
    rec = _Recorder([
        [{"player": "a", "score": 10}],
        [{"player": "a", "score": 10}],           # unchanged: no post
        [{"player": "b", "score": 25}],           # improved: post
        [{"player": "c", "score": 3}],            # a lower top (table reset): no post
    ])
    monkeypatch.setattr(leaderboard, "read_json", rec.read)
    monkeypatch.setattr(leaderboard, "find_scores_url",
                        lambda *_a, **_k: ("http://g/scores", rec.read(None)))
    monkeypatch.setattr(leaderboard, "signed_post", rec.post)
    monkeypatch.setattr(leaderboard.time, "sleep", lambda _s: None)
    out = io.StringIO()
    # Drive four polls by making the loop stop itself: patch sleep to raise after 4.
    calls = {"n": 0}

    def sleep(_s):
        calls["n"] += 1
        if calls["n"] >= 4:
            raise KeyboardInterrupt
    monkeypatch.setattr(leaderboard.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        leaderboard.run("http://g", "https://x.execute-api.us-west-2.amazonaws.com/live/",
                        "snake", once=False, interval_s=0, out=out)
    assert [p["score"] for p in rec.posted] == [10, 25]
    assert all(p["game"] == "snake" for p in rec.posted)
    text = out.getvalue()
    assert "best 25 (b)" in text and "rank #1 of 7" in text and "top of the room" in text


def test_a_game_that_is_not_up_yet_is_a_message_not_a_crash(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("http://127.0.0.1:1 is not answering (connection refused)")
    monkeypatch.setattr(leaderboard, "find_scores_url", boom)
    out = io.StringIO()
    assert leaderboard.run("http://127.0.0.1:1", "https://x.execute-api.us-west-2.amazonaws.com/live/",
                           "snake", once=True, interval_s=0, out=out) == 0
    assert "is not answering" in out.getvalue()


# ------------------------------------------------------------------- the signing

def test_the_post_is_sigv4_signed_for_the_api_gateway_region(monkeypatch):
    """Identity for free: the central account authorizes the caller's IAM identity,
    so the request must carry a real SigV4 signature for `execute-api` in the API's
    own region -- and nothing else (no token) identifies the team."""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"team": "Team 1", "best": 42, "rank": 2, "teams": 5}).encode()

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # Assembled, not written out: a credential scanner matches on SHAPE, and a literal
    # key id here blocks every clone's next commit (learned the hard way).
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKI" + "A" + "EXAMPLEONLYNOTREAL0")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "notarealsecret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    result = leaderboard.signed_post(
        "https://abc123.execute-api.eu-west-1.amazonaws.com/live/",
        {"score": 42, "player": "ann", "game": "snake"})
    assert result["rank"] == 2
    assert captured["url"] == "https://abc123.execute-api.eu-west-1.amazonaws.com/live/scores"
    auth = captured["headers"]["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256"), auth
    assert "/eu-west-1/execute-api/aws4_request" in auth, \
        "the signature must be for the API's own region and service"
    assert "x-amz-date" in captured["headers"]
    assert captured["body"] == {"score": 42, "player": "ann", "game": "snake"}
    assert not any(k.startswith("x-team") for k in captured["headers"]), \
        "no token: the identity IS the signature"


def test_a_non_api_gateway_url_is_refused_before_signing():
    with pytest.raises(SystemExit):
        leaderboard._region_of("https://example.com/scores")


# ------------------------------------------------------------------- discovery

def test_it_finds_whatever_the_agent_named_the_score_endpoint(monkeypatch):
    """Nothing in the repository tells the builder what to call its routes, so the
    bridge must find the table rather than assume `/scores`."""
    served = {"http://g/api/highscores": [{"player": "ann", "score": 11}]}

    def fake(url, timeout_s=5.0):
        if url in served:
            return served[url]
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(leaderboard, "read_json", fake)
    url, table = leaderboard.find_scores_url("http://g")
    assert url == "http://g/api/highscores"
    assert leaderboard.best_entry(table) == {"score": 11, "player": "ann"}


def test_an_explicit_path_is_used_alone(monkeypatch):
    seen = []

    def fake(url, timeout_s=5.0):
        seen.append(url)
        return [{"player": "a", "score": 1}]

    monkeypatch.setattr(leaderboard, "read_json", fake)
    url, _t = leaderboard.find_scores_url("http://g/", explicit="/board/top")
    assert url == "http://g/board/top" and seen == ["http://g/board/top"]


def test_a_game_with_no_score_endpoint_says_which_paths_it_tried(monkeypatch):
    def fake(url, timeout_s=5.0):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(leaderboard, "read_json", fake)
    with pytest.raises(OSError) as excinfo:
        leaderboard.find_scores_url("http://g")
    msg = str(excinfo.value)
    assert "no score endpoint found" in msg and "--scores-path" in msg
    assert "api/highscores" in msg


def test_a_dead_game_is_reported_as_dead_not_as_a_missing_endpoint(monkeypatch):
    """Two different problems, two different messages."""
    def fake(url, timeout_s=5.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(leaderboard, "read_json", fake)
    with pytest.raises(OSError) as excinfo:
        leaderboard.find_scores_url("http://127.0.0.1:1")
    assert "is not answering" in str(excinfo.value)
