"""A `/develop <idea>` comment starts the team build, and the result returns to the issue.

The PR-review endpoint relays a trusted `/develop` comment on an issue to a NEW
coordinator session as ``{"github_event": {...}}``. That path has no model turn, so
these tests hold it to two properties the model path gets from its steering:

* It admits EXACTLY the task ``./ask.sh "Use preset=team-game. Creative direction:
  <idea>"`` admits, through the same ``run_build`` implementation, or it refuses with
  a code and starts nothing. One build at a time, failing closed: an event starts
  nothing while it can see another build, or while it cannot read the saved run
  state that would show one. It is a check, not a lock.
* The engine reports the terminal result on that issue ONCE, after the record is
  persisted, as reporting only. A Gateway failure never changes the verdict, and the
  comment never invites a retry: the bound has to hold one layer up as well.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat  # noqa: E402
import engine  # noqa: E402
import github  # noqa: E402
import presets  # noqa: E402
import replay  # noqa: E402
import roles  # noqa: E402
import run_store  # noqa: E402
from identity_baggage import get_current_identity  # noqa: E402
from test_chat_request_boundary import invoke_tool, tools  # noqa: E402

REPO = "octo-cat/agent-review"
IDEA = "a lighthouse keeper guides ships through a storm"


class RecordingEngine:
    """Record what admission submits, with no worker, model, or GitHub call."""

    def __init__(self):
        self.runs = []
        self.lock = threading.Lock()

    def submit(self, task, agents=None, options=None, preset=None):
        with self.lock:
            run = SimpleNamespace(
                run_id=f"run_event_{len(self.runs)}",
                task=task,
                agents=agents or chat._presets.resolve(preset=preset).agents,
                options=copy.deepcopy(options or {}),
                preset=preset,
                status="queued",
                user_identity=get_current_identity().to_dict(),
            )
            self.runs.append(run)
        return run

    def get(self, run_id):
        return next(run for run in self.runs if run.run_id == run_id)

    def list(self):
        return list(self.runs)


class FakeMirror:
    """The run-state mirror bucket, in memory: the S3 calls run_store makes, and the
    failures a real bucket answers with (AccessDenied, throttling, a network error)."""

    BUCKET = "coding-agents-mirror-test"

    def __init__(self):
        self.objects = {}            # key -> (body bytes, LastModified)
        self.list_error = None
        self.get_errors = {}         # key -> exception

    def put_object(self, Bucket, Key, Body, **_kwargs):
        assert Bucket == self.BUCKET
        body = Body if isinstance(Body, bytes) else Body.encode("utf-8")
        self.objects[Key] = (body, datetime.now(timezone.utc))
        return {}

    def put_record(self, payload, age_s=0.0):
        """A record another coordinator session wrote, present only in the mirror."""
        saved_at = time.gmtime(time.time() - age_s)
        body = json.dumps({**payload, "_saved_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", saved_at)}).encode("utf-8")
        key = f"orchestrator/run-state/{payload['run_id']}.json"
        self.objects[key] = (body, datetime.now(timezone.utc) - timedelta(seconds=age_s))
        return key

    def list_objects_v2(self, Bucket, Prefix, **_kwargs):
        assert Bucket == self.BUCKET
        if self.list_error:
            raise self.list_error
        return {"IsTruncated": False, "Contents": [
            {"Key": key, "LastModified": modified}
            for key, (_body, modified) in self.objects.items() if key.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        assert Bucket == self.BUCKET
        if Key in self.get_errors:
            raise self.get_errors[Key]
        return {"Body": io.BytesIO(self.objects[Key][0])}


def _access_denied(operation):
    """What S3 answers a role without the grant: its message names the caller's ARN."""
    return ClientError({"Error": {"Code": "AccessDenied", "Message": (
        "User: arn:aws:sts::123456789012:assumed-role/orchestrator-role/s is not "
        "authorized to perform: s3:" + operation)}}, operation)


@pytest.fixture
def mirror(monkeypatch):
    """The deployed coordinator's mirror: WORKSHOP_RUNTIME_BUCKET and its S3 client."""
    fake = FakeMirror()
    monkeypatch.setenv("WORKSHOP_RUNTIME_BUCKET", FakeMirror.BUCKET)
    monkeypatch.setattr(run_store, "_s3", lambda: (fake, FakeMirror.BUCKET))
    return fake


@pytest.fixture
def recorded(monkeypatch, tmp_path, mirror):
    rec = RecordingEngine()
    monkeypatch.setattr(chat, "ENGINE", rec)
    monkeypatch.setattr(chat, "_wired_roles", lambda: set(roles.roster_ids()))
    # Saved runs from other tests must not decide "already running" here.
    monkeypatch.setattr(chat._engine, "_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("GITHUB_REPO", REPO)
    return rec


def _event(**over):
    base = {"kind": "develop", "owner": "octo-cat", "repo": "agent-review",
            "issue_number": 7, "author": "octocat", "idea": IDEA,
            "comment_url": "https://github.com/octo-cat/agent-review/issues/7#issuecomment-1"}
    base.update(over)
    return base


def _ask_sh_task(rec, idea=IDEA):
    """What the model path admits for the ask.sh line, driven through the real tool."""
    prompt = f"Use preset=team-game. Creative direction: {idea}"
    with chat.bind_user_request(prompt):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="team-game", creative_direction=idea))
    assert result["status"] == "started", result
    run = rec.runs.pop()
    return run


# ------------------------------------------------------------- the admitted build
def test_the_event_admits_exactly_the_task_ask_sh_admits(recorded):
    asked = _ask_sh_task(recorded)
    out = chat.start_event_build(_event())
    assert set(out) == {"run_id", "status", "agents", "preset"}, out
    assert out["status"] == "started" and out["preset"] == "team-game"
    [run] = recorded.runs
    assert out["run_id"] == run.run_id
    assert run.task == asked.task
    assert run.preset == asked.preset == "team-game"
    assert run.agents == asked.agents == presets.resolve(preset="team-game").agents
    assert out["agents"] == list(run.agents)
    assert run.task.startswith(presets.default_task("team-game"))
    assert run.task.endswith(chat.event_prompt(IDEA))
    assert run.options["chat_request"]["current_user_text"] == chat.event_prompt(IDEA)
    assert run.options["chat_request"]["creative_direction_verified"] is True


def test_the_event_records_its_origin_and_the_github_submitter(recorded):
    chat.start_event_build(_event())
    [run] = recorded.runs
    assert run.options["origin"] == {
        "owner": "octo-cat", "repo": "agent-review", "issue_number": 7,
        "author": "octocat",
        "comment_url": "https://github.com/octo-cat/agent-review/issues/7#issuecomment-1"}
    assert run.user_identity["user_id"] == "github:octocat"
    assert run.user_identity["user_name"] == "octocat"
    # The identity and the request binding stay inside the event's own context.
    assert get_current_identity().is_anonymous()
    with pytest.raises(chat.UserRequestError):
        chat._request.current()


def test_a_chat_build_carries_no_origin(recorded):
    asked = _ask_sh_task(recorded)
    assert "origin" not in asked.options


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("idea, code", [
    ("", "IDEA_MISSING"),
    ("   ", "IDEA_MISSING"),
    ("<your idea, in one sentence>", "IDEA_PLACEHOLDER"),
    ("<Your idea>", "IDEA_PLACEHOLDER"),
    ("<a frog game>", "IDEA_PLACEHOLDER"),
    ("<your team's setting, mood, or play idea>", "IDEA_PLACEHOLDER"),
    ("x" * 1001, "IDEA_TOO_LONG"),
])
def test_a_missing_or_placeholder_idea_starts_nothing(recorded, idea, code):
    out = chat.start_event_build(_event(idea=idea))
    assert out["error"] == code and out["detail"], out
    assert recorded.runs == []


def test_the_page_placeholder_is_refused_on_the_ask_sh_path_too(recorded):
    prompt = "Use preset=team-game. Creative direction: <your idea, in one sentence>"
    with chat.bind_user_request(prompt):
        result = asyncio.run(invoke_tool(
            tools(), "run_build", task="", preset="team-game",
            creative_direction="<your idea, in one sentence>"))
    assert result["error"] == "PLACEHOLDER_NOT_REPLACED", result
    assert recorded.runs == []


def test_an_idea_of_exactly_the_limit_is_built(recorded):
    out = chat.start_event_build(_event(idea="y" * 1000))
    assert out.get("run_id"), out


@pytest.mark.parametrize("configured", ["someone-else/agent-review", ""])
def test_an_event_from_another_repository_is_refused(recorded, monkeypatch, configured):
    if configured:
        monkeypatch.setenv("GITHUB_REPO", configured)
    else:
        monkeypatch.delenv("GITHUB_REPO", raising=False)
    out = chat.start_event_build(_event())
    assert out["error"] == "REPOSITORY_MISMATCH", out
    assert recorded.runs == []


def test_the_repository_comparison_ignores_case(recorded, monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "Octo-Cat/Agent-Review")
    assert chat.start_event_build(_event()).get("run_id")


def test_a_live_build_refuses_a_second_one(recorded):
    first = chat.start_event_build(_event())
    assert first.get("run_id")
    second = chat.start_event_build(_event(idea="a completely different game"))
    assert second["error"] == "BUILD_ALREADY_RUNNING", second
    assert second["running_run_id"] == first["run_id"]
    assert len(recorded.runs) == 1


def test_a_build_finished_here_does_not_block_the_next_one(recorded):
    chat.start_event_build(_event())
    recorded.runs[0].status = "passed"
    assert chat.start_event_build(_event(idea="a second game")).get("run_id")


def test_a_build_another_session_mirrored_refuses_the_event(recorded, mirror):
    # Any submitter, any request: ask.sh's build in another session counts. That
    # session's microVM is gone, so the record exists ONLY in the mirror.
    mirror.put_record({"run_id": "run_000001_abcdefabcdef", "status": "running",
                       "task": "something else", "submitted_by": None})
    assert not os.path.exists(os.path.join(chat._engine._RUNS_DIR, "state"))
    out = chat.start_event_build(_event())
    assert out["error"] == "BUILD_ALREADY_RUNNING", out
    assert out["running_run_id"] == "run_000001_abcdefabcdef"
    assert recorded.runs == []


def test_a_build_saved_by_this_session_refuses_the_event(recorded):
    run_store.save(chat._engine._RUNS_DIR, "run_000003_abcdefabcdef",
                   {"run_id": "run_000003_abcdefabcdef", "status": "queued"})
    out = chat.start_event_build(_event())
    assert out["error"] == "BUILD_ALREADY_RUNNING", out
    assert out["running_run_id"] == "run_000003_abcdefabcdef"


def test_a_stale_saved_build_does_not_block_forever(recorded, mirror):
    state = os.path.join(chat._engine._RUNS_DIR, "state")
    os.makedirs(state, exist_ok=True)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - run_store.ACTIVE_STALE_AFTER_S - 60))
    with open(os.path.join(state, "run_000002_abcdefabcdef.json"), "w") as f:
        json.dump({"run_id": "run_000002_abcdefabcdef", "status": "running",
                   "_saved_at": old}, f)
    mirror.put_record({"run_id": "run_000004_abcdefabcdef", "status": "running"},
                      age_s=run_store.ACTIVE_STALE_AFTER_S + 60)
    assert chat.start_event_build(_event()).get("run_id")


def test_a_finished_mirrored_build_does_not_block(recorded, mirror):
    mirror.put_record({"run_id": "run_000005_abcdefabcdef", "status": "needs_human"})
    assert chat.start_event_build(_event()).get("run_id")


# One build at a time FAILS CLOSED: the deployed coordinator's every /develop is a NEW
# session with an empty engine, so the mirror is the only evidence of a sibling's build.
# These go through the real run_store reader against an S3 client that fails the way
# a real one does, not a stubbed reader.
def _refused_for_unreadable_state(out, recorded, code):
    assert out["error"] == "RUN_STATE_UNAVAILABLE", out
    assert f"({code})" in out["detail"], out
    assert "Nothing was started" in out["detail"]
    assert recorded.runs == []
    # The detail is posted on the issue: an AWS message's ARN and account stay out.
    assert "arn:" not in out["detail"] and "123456789012" not in out["detail"]


def test_a_mirror_that_cannot_be_listed_starts_nothing(recorded, mirror, caplog):
    mirror.list_error = _access_denied("ListBucket")
    with caplog.at_level(logging.WARNING, logger="orchestrator.chat.event"):
        out = chat.start_event_build(_event())
    _refused_for_unreadable_state(out, recorded, "ListObjectsV2:AccessDenied")
    # The full message is kept where an operator reads it, not on GitHub.
    assert "arn:aws:sts::123456789012" in caplog.text


def test_a_running_record_that_cannot_be_read_starts_nothing(recorded, mirror):
    key = mirror.put_record({"run_id": "run_000006_abcdefabcdef", "status": "running"})
    mirror.get_errors[key] = _access_denied("GetObject")
    out = chat.start_event_build(_event())
    _refused_for_unreadable_state(out, recorded, "GetObject:AccessDenied")


def test_a_network_error_reading_a_record_starts_nothing(recorded, mirror):
    key = mirror.put_record({"run_id": "run_000007_abcdefabcdef", "status": "running"})
    mirror.get_errors[key] = ConnectionError("connection reset")
    out = chat.start_event_build(_event())
    _refused_for_unreadable_state(out, recorded, "GetObject:ConnectionError")


def test_an_old_record_is_not_read_at_all(recorded, mirror):
    # Written long before any live heartbeat could be: it cannot be an active run,
    # so it is never fetched, and its failure cannot block a build.
    key = mirror.put_record({"run_id": "run_000008_abcdefabcdef", "status": "running"},
                            age_s=run_store.ACTIVE_STALE_AFTER_S + 3600)
    mirror.get_errors[key] = _access_denied("GetObject")
    assert chat.start_event_build(_event()).get("run_id")


def test_a_deployed_coordinator_without_a_mirror_bucket_starts_nothing(recorded, monkeypatch):
    monkeypatch.delenv("WORKSHOP_RUNTIME_BUCKET", raising=False)
    out = chat.start_event_build(_event())
    _refused_for_unreadable_state(out, recorded, "NoRunStateMirror")


def test_a_mirror_without_an_s3_client_starts_nothing(recorded, monkeypatch):
    monkeypatch.setattr(run_store, "_s3", lambda: None)
    out = chat.start_event_build(_event())
    _refused_for_unreadable_state(out, recorded, "S3ClientUnavailable")


def test_the_history_reader_stays_best_effort(mirror, tmp_path):
    # The strict reader is new; `recent` (list_runs, the watcher) still never raises.
    mirror.list_error = _access_denied("ListBucket")
    assert run_store.recent(str(tmp_path / "runs")) == []


@pytest.mark.parametrize("kind", ["fix", "review", "", None])
def test_any_other_event_kind_starts_nothing(recorded, kind):
    out = chat.start_event_build(_event(kind=kind))
    assert out["error"] == "NOT_DEVELOP", out
    assert recorded.runs == []


@pytest.mark.parametrize("author", ["octocat", "octo-cat", "octo_cat", "mona_acme"])
def test_a_person_s_login_is_admitted_including_a_managed_user_s(recorded, author):
    # Every Enterprise Managed User login carries an underscore (handle_shortcode).
    out = chat.start_event_build(_event(author=author))
    assert out.get("run_id"), out
    assert recorded.runs[0].user_identity["user_id"] == f"github:{author}"


@pytest.mark.parametrize("over", [
    {"author": "team-bot[bot]"}, {"author": "octo_cat[bot]"}, {"author": ""},
    {"author": "bad login"}, {"author": "-leading-hyphen"}, {"author": "x" * 40},
    {"issue_number": 0}, {"issue_number": "seven"}, {"issue_number": True},
    {"owner": ""}, {"repo": "../etc"},
])
def test_a_malformed_event_starts_nothing(recorded, over):
    out = chat.start_event_build(_event(**over))
    assert out["error"] == "EVENT_INVALID", out
    assert recorded.runs == []


def test_a_non_object_event_is_answered_not_raised(recorded):
    assert chat.start_event_build("develop please")["error"] == "EVENT_INVALID"
    assert chat.start_event_build(None)["error"] == "EVENT_INVALID"


def test_an_unexpected_failure_is_answered_with_a_code(recorded, monkeypatch, caplog):
    def boom(**_kwargs):
        raise RuntimeError("routing exploded near arn:aws:iam::123456789012:role/x")
    monkeypatch.setattr(chat._presets, "resolve", boom)
    with caplog.at_level(logging.WARNING, logger="orchestrator.chat.event"):
        out = chat.start_event_build(_event())
    assert out["error"] == "BUILD_NOT_STARTED" and "RuntimeError" in out["detail"]
    # The detail is posted on the issue; the message (and its ARN) is only logged.
    assert "routing exploded" not in out["detail"] and "arn:" not in out["detail"]
    assert "routing exploded" in caplog.text
    assert recorded.runs == []


def test_the_run_build_tool_is_a_thin_wrapper_over_the_shared_body():
    import inspect
    source = inspect.getsource(chat.build_tools)
    body = source[source.index("def run_build("):source.index("def resubmit_run(")]
    assert "_run_build_impl(" in body
    assert "_request.admit(" not in body and "_kick(" not in body


# --------------------------------------------------------------- the GitHub call
def _cfg(repo=REPO):
    return {"gateway_url": "https://gw.example", "repo": repo, "target": "GitHubMCP",
            "region": "us-west-2", "source": "environment"}


def test_comment_on_origin_uses_the_comment_tool_on_the_configured_repository(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "_gateway_config", lambda: _cfg())
    monkeypatch.setattr(github, "_tool", lambda cfg, name, args, **kw: calls.append(
        (name, args)) or {"id": 1, "url": "https://github.com/x/1#c"})
    out = github.comment_on_origin(_event(), "body")
    assert out == {"commented": True, "comment_url": "https://github.com/x/1#c"}
    assert calls == [("comment_on_issue", {"owner": "octo-cat", "repo": "agent-review",
                                           "issue_number": 7, "body": "body"})]


def test_comment_on_origin_refuses_another_repository(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "_gateway_config", lambda: _cfg("octo-cat/other"))
    monkeypatch.setattr(github, "_tool", lambda *a, **k: calls.append(a))
    out = github.comment_on_origin(_event(), "body")
    assert out["error"].startswith("ORIGIN_REPOSITORY_MISMATCH")
    assert calls == []


def test_comment_on_origin_reports_a_gateway_failure_without_raising(monkeypatch):
    def fail(*_a, **_k):
        raise github.GatewayError("gateway HTTP 403: nope")
    monkeypatch.setattr(github, "_gateway_config", lambda: _cfg())
    monkeypatch.setattr(github, "_tool", fail)
    assert "403" in github.comment_on_origin(_event(), "body")["error"]


def test_comment_on_origin_without_a_gateway_or_origin_is_skipped(monkeypatch):
    monkeypatch.setattr(github, "_gateway_config", lambda: None)
    assert "skipped" in github.comment_on_origin(_event(), "body")
    assert "skipped" in github.comment_on_origin(None, "body")


# ------------------------------------------------------------ the engine reports
class _Stub:
    """An executor that is not the offline fixture, so reporting is attempted."""
    name = "agentcore"


def _engine_with_terminal(monkeypatch, status="needs_human", reason="ROLE_PR_BLOCKED:w1"):
    # The exchange cleanup is a no-op when a shared mount is configured.
    monkeypatch.setenv("WORKSHOP_S3FILES_DIR", "/nonexistent-shared-mount")
    eng = engine.Engine(executor_obj=_Stub())

    def admission(run):
        run.status, run.fail_reason = status, reason
        return False
    eng._admission = admission
    return eng


def _run(origin=True):
    options = {"origin": {"owner": "octo-cat", "repo": "agent-review", "issue_number": 7,
                          "author": "octocat", "comment_url": ""}} if origin else {}
    return engine.Run(run_id="run_120000_aaaaaaaaaaaa", task="t", agents=[], roles={},
                      options=options)


def test_the_result_is_reported_once_after_the_terminal_record_is_saved(monkeypatch):
    eng = _engine_with_terminal(monkeypatch)
    posted, saved_before_post = [], []
    monkeypatch.setattr(engine.github, "comment_on_origin", lambda origin, body: (
        saved_before_post.append(run_store.load(engine._RUNS_DIR, run.run_id))
        or posted.append((origin, body)) or {"commented": True, "comment_url": "u"}))
    run = _run()
    eng._drive(run)
    eng._report_origin(run)          # a second call is a no-op
    assert len(posted) == 1
    origin, body = posted[0]
    assert origin["issue_number"] == 7
    assert body == replay.origin_comment(run)
    # The terminal verdict was durable before anything was posted.
    assert saved_before_post[0]["status"] == "needs_human"
    assert run.status == "needs_human" and run.fail_reason == "ROLE_PR_BLOCKED:w1"
    assert any("result reported on issue #7" in e["message"] for e in run.events)


@pytest.mark.parametrize("failure", [
    lambda origin, body: (_ for _ in ()).throw(RuntimeError("gateway down")),
    lambda origin, body: {"error": "gateway comment failed: HTTP 502"},
])
def test_a_failed_report_is_logged_and_never_changes_the_verdict(monkeypatch, failure):
    eng = _engine_with_terminal(monkeypatch, status="passed", reason=None)
    calls = []
    monkeypatch.setattr(engine.github, "comment_on_origin",
                        lambda origin, body: calls.append(1) or failure(origin, body))
    run = _run()
    eng._drive(run)                  # never raises
    eng._report_origin(run)          # and never retries
    assert calls == [1]
    assert run.status == "passed" and run.fail_reason is None
    assert any("not reported" in e["message"] and e["level"] == "warn"
               for e in run.events)


def test_a_run_without_an_origin_reports_nowhere(monkeypatch):
    eng = _engine_with_terminal(monkeypatch)
    calls = []
    monkeypatch.setattr(engine.github, "comment_on_origin",
                        lambda *a: calls.append(a) or {"commented": True})
    eng._drive(_run(origin=False))
    assert calls == []


def test_the_offline_fixture_never_reaches_github(monkeypatch):
    from fixture_executor import FixtureExecutor
    eng = engine.Engine(executor_obj=FixtureExecutor())
    calls = []
    monkeypatch.setattr(engine.github, "comment_on_origin",
                        lambda *a: calls.append(a) or {"commented": True})
    run = _run()
    run.status = "passed"
    eng._report_origin(run)
    assert calls == [] and run._origin_reported


def test_a_run_that_is_still_active_is_not_reported(monkeypatch):
    eng = _engine_with_terminal(monkeypatch)
    calls = []
    monkeypatch.setattr(engine.github, "comment_on_origin",
                        lambda *a: calls.append(a) or {"commented": True})
    run = _run()
    run.status = "running"
    eng._report_origin(run)
    assert calls == [] and not run._origin_reported


# ------------------------------------------------------------ the issue comment
def _reported_run(**over):
    base = dict(
        run_id="run_120000_aaaaaaaaaaaa", status="needs_human",
        fail_reason="ROLE_PR_BLOCKED:work_codex_2", pr=None, pr_url=None,
        options={"origin": {"owner": "octo-cat", "repo": "agent-review",
                            "issue_number": 7, "author": "octocat",
                            "comment_url": "https://github.com/octo-cat/agent-review/issues/7#issuecomment-1"}},
        role_prs=[
            {"work_id": "work_claude_1", "agent": "claude-code", "role": "backend",
             "pr_url": "https://github.com/octo-cat/agent-review/pull/8", "state": "merged"},
            {"work_id": "work_codex_2", "agent": "codex", "role": "frontend",
             "pr_url": "https://github.com/octo-cat/agent-review/pull/9", "state": "blocked",
             "error": "GATE_RED"},
        ],
        work_items={},
        gate_history=[
            {"sequence": 1, "stage": "work_claude_1 round 1", "passed": True,
             "check_sha256": "a" * 64, "summary": "12 checks passed"},
            {"sequence": 2, "stage": "work_codex_2 round 1", "passed": False,
             "check_sha256": "b" * 64, "summary": "1 failed: mailto @someone `x`"},
            {"sequence": 3, "stage": "work_codex_2 round 2", "passed": False,
             "check_sha256": "b" * 64, "summary": "1 failed"},
        ],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _outside_code(body):
    return re.sub(r"`[^`\n]*`", "", body)


def test_the_issue_comment_reports_prs_rounds_next_action_and_run_id():
    run = _reported_run()
    body = replay.origin_comment(run)
    assert "needs a person" in body and "`needs_human`" in body
    assert run.run_id in body
    assert "https://github.com/octo-cat/agent-review/pull/8" in body
    assert "https://github.com/octo-cat/agent-review/pull/9" in body
    assert "`merged`" in body and "`blocked (GATE_RED)`" in body
    assert body.count("**PASS**") == 1 and body.count("**FAIL**") == 2
    assert "`" + "a" * 12 + "`" in body and "`" + "b" * 12 + "`" in body
    assert "a" * 13 not in body          # a prefix, not the whole digest
    expected = engine.next_action(run.status, run.fail_reason, None, None, run.role_prs,
                                  work_items={})
    assert expected and expected in body
    assert "`octocat`" in body
    assert "[this comment](https://github.com/octo-cat/agent-review/issues/7#issuecomment-1)" in body


def test_the_issue_comment_mentions_nobody_closes_nothing_and_invites_no_retry():
    body = replay.origin_comment(_reported_run())
    assert "@" not in _outside_code(body), body
    assert "closes" not in body.lower() and "fixes #" not in body.lower()
    assert body.rstrip().endswith(
        "A failed pull request is not retried automatically; do not comment "
        "/develop again to retry it.")
    # The endpoint starts work only on a comment that BEGINS with a command.
    assert not body.lstrip().startswith(("/develop", "/fix"))


def test_an_issue_comment_for_a_run_with_no_pr_or_check_says_so():
    body = replay.origin_comment(_reported_run(
        status="needs_human", fail_reason="ROLE_EXECUTION_ERROR: no files",
        role_prs=[], gate_history=[]))
    assert "_No pull request was opened._" in body
    assert "_No check was executed._" in body
    assert "`ROLE_EXECUTION_ERROR: no files`" in body


@pytest.mark.parametrize("reason", [
    "ENGINE_STALL", "ROLE_EXECUTION_ERROR: no files", "ARTIFACT_TRANSFER_ERROR: x",
    "COORDINATOR_SESSION_INTERRUPTED", "WORK_TREE_MISSING: w1",
])
def test_a_run_with_no_pr_names_the_one_way_to_submit_it_again(reason):
    # Their advice says "Resubmit" or "Submit the SAME request again", and the comment
    # ends "do not comment /develop again". The person on the issue gets ONE defined
    # way to follow the advice: ./ask.sh, once.
    run = _reported_run(fail_reason=reason, role_prs=[], gate_history=[])
    body = replay.origin_comment(run)
    assert replay.ONCE_MORE_SENTENCE in body
    assert body.index(replay.ONCE_MORE_SENTENCE) < body.index(replay.NO_RETRY_SENTENCE)
    assert body.rstrip().endswith(replay.NO_RETRY_SENTENCE)
    assert "`./ask.sh`" in body and "once" in replay.ONCE_MORE_SENTENCE


def test_a_run_with_a_pr_gets_no_resubmission_sentence():
    body = replay.origin_comment(_reported_run())
    assert replay.ONCE_MORE_SENTENCE not in body


def test_a_publish_failure_beside_a_published_sibling_asks_for_no_resubmission():
    # One builder's pull request was published, the other's was refused: the opened one
    # keeps its evidence, so the advice must not say "submit the request again" while
    # the comment ends "do not comment /develop again".
    run = _reported_run(
        fail_reason="ROLE_PR_PUBLISH_ERROR: 403",
        role_prs=[{"work_id": "work_claude_1", "agent": "claude-code", "role": "backend",
                   "pr_url": "https://github.com/octo-cat/agent-review/pull/8",
                   "state": "merged"}])
    body = replay.origin_comment(run)
    assert replay.ONCE_MORE_SENTENCE not in body
    assert "Do not resubmit the whole build" in body
    assert "submit the request again" not in body


def test_the_issue_summary_and_the_watcher_give_one_resubmission_rule():
    import watch_run  # noqa: PLC0415
    import inspect  # noqa: PLC0415
    watcher = " ".join(inspect.getsource(watch_run).split())
    assert "once, with ./ask.sh and the same idea" in watcher.replace('" "', "")
    assert "once, with `./ask.sh` and the same idea" in replay.ONCE_MORE_SENTENCE
    assert "a new /develop comment" not in watcher.replace('" "', "")


def test_a_github_build_is_not_an_agent_studio_chat_build():
    import run_advice  # noqa: PLC0415
    github = {"submitted_by": "github:octocat", "preset": "team-game"}
    origin = {"submitted_by": "octocat", "options": {"origin": {"owner": "o", "repo": "r",
                                                                  "issue_number": 1}}}
    assert run_advice.is_github_build(github) and not run_advice.is_chat_build(github)
    assert run_advice.is_github_build(origin) and not run_advice.is_chat_build(origin)
    chat = {"submitted_by": "frontend-dev@workshop.aws"}
    assert run_advice.is_chat_build(chat) and not run_advice.is_github_build(chat)
    assert not run_advice.is_chat_build({}) and not run_advice.is_github_build({})


def test_the_issue_comment_carries_no_aws_arn_or_account_id():
    # A recorded AWS error (a planner turn denied, a preflight 403, a refused publish)
    # names the role ARN and the account; the issue gets neither.
    denied = ("An error occurred (AccessDeniedException) when calling the Converse operation: "
              "User: arn:aws:sts::123456789012:assumed-role/agentcore-orchestrator-role/s "
              "is not authorized on arn:aws:bedrock:us-west-2:123456789012:inference-profile/p")
    for over in (
            {"fail_reason": f"INTEGRATION_BRIEF_UNAVAILABLE: {denied}"},
            {"fail_reason": f"PR_PREFLIGHT_ERROR:gateway: HTTP 403 {denied}",
             "role_prs": [], "gate_history": []},
            {"role_prs": [{"work_id": "work_codex_2", "agent": "codex", "role": "frontend",
                           "pr_url": "https://github.com/octo-cat/agent-review/pull/9",
                           "state": "blocked", "error": denied}]}):
        body = replay.origin_comment(_reported_run(**over))
        assert "arn:aws" not in body and "123456789012" not in body, body
        assert "<arn>" in body


def test_recorded_details_in_the_advice_mention_nobody():
    # PR_PREFLIGHT_ERROR's advice embeds the recorded cause, and a passed run's embeds
    # the Gateway's error: neither may notify anyone from the issue thread.
    preflight = _reported_run(
        fail_reason="PR_PREFLIGHT_ERROR:gateway: contact @admin-team or @octo-org/ops",
        role_prs=[], gate_history=[])
    passed = _reported_run(status="passed", fail_reason=None, role_prs=[], gate_history=[],
                           pr={"error": "PR_CREATE_FAILED: ask @someone (see `doctor`)"})
    for run in (preflight, passed):
        body = replay.origin_comment(run)
        assert "@" not in _outside_code(body), body
        assert "What to do now" in body
    assert "`@admin-team`" in replay.origin_comment(preflight)
    assert "`@octo-org/ops`" in replay.origin_comment(preflight)


def test_unpaired_backticks_in_the_advice_become_one_literal_span():
    run = _reported_run(fail_reason="PR_PREFLIGHT_ERROR:x: odd ` tick @admin",
                        role_prs=[], gate_history=[])
    body = replay.origin_comment(run)
    assert "@" not in _outside_code(body), body


def test_a_managed_user_login_is_shown_in_the_comment():
    run = _reported_run()
    run.options["origin"]["author"] = "mona_acme"
    assert "Requested by `mona_acme`" in replay.origin_comment(run)
    run.options["origin"]["author"] = "team-bot[bot]"
    assert "team-bot" not in replay.origin_comment(run)


def test_the_issue_comment_does_not_change_the_run():
    run = _reported_run()
    before = copy.deepcopy(vars(run))
    replay.origin_comment(run)
    assert vars(run) == before


def test_the_pull_request_body_links_the_issue_outside_the_request_fence():
    run = SimpleNamespace(run_id="run_120000_aaaaaaaaaaaa", task="Build it @everyone",
                          integration_brief={}, options=_reported_run().options)
    item = SimpleNamespace(work_id="work_claude_1", agent="claude-code", role="backend",
                           capability="backend", base_branch="main", base_sha="abc",
                           patch_digest="def", changed_files=["app.py"], deleted_files=[])
    body = replay.work_item_narrative(run, item)
    line = "Requested on #7 by `octocat`."
    assert line in body
    assert body.index(line) < body.index("## Request")
    assert "closes" not in body.lower()
    assert "@" not in _outside_code(body.split("## Request")[0])


def test_a_pull_request_body_without_an_origin_names_no_issue():
    run = SimpleNamespace(run_id="run_x", task="t", integration_brief={})
    item = SimpleNamespace(work_id="w", agent="claude-code", role="backend",
                           capability="backend", base_branch="main", base_sha="",
                           patch_digest="", changed_files=[], deleted_files=[])
    assert "Requested on #" not in replay.work_item_narrative(run, item)


def test_the_issue_report_stays_off_the_verdict_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("replay.py", "run_views.py"):
        source = open(os.path.join(here, name), encoding="utf-8").read()
        for forbidden in ("import llm", "import reviewer", "from llm", "from reviewer"):
            assert forbidden not in source, f"{name} must not {forbidden!r}"
