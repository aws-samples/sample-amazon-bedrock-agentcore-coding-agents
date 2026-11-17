"""The GitHub endpoint: signature, event filter, hand-off, command size, output, and new issues.

Runs offline with stubbed AWS clients. The function itself is
``coding-agents/pr_review/handler.py``; the endpoint's template.yaml packages that exact
file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import shlex
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "coding-agents" / "pr_review" / "handler.py"
spec = importlib.util.spec_from_file_location("pr_review_handler", SOURCE)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def signed(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _now(offset_s: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


def pr_event(action="opened", draft=False, state="open", updated=None) -> dict:
    return {"action": action, "repository": {"name": "game", "owner": {"login": "octo"}},
            "pull_request": {"number": 7, "title": 'Add "pause"', "draft": draft, "state": state,
                             "updated_at": updated or _now(), "author_association": "OWNER",
                             "user": {"login": "dev"}, "head": {"sha": "abc1234def"}}}


class Ctx:
    invoked_function_arn = "arn:aws:lambda:us-west-2:111122223333:function:stack-pr-review"


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


class FakeTable:
    """DynamoDB's conditional put: the first writer of an id wins."""
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self):
        self.ids = set()
        self.broken = None

    def put_item(self, TableName, Item, ConditionExpression):
        assert ConditionExpression == "attribute_not_exists(id)"
        if self.broken:
            raise self.broken
        if Item["id"]["S"] in self.ids:
            raise self.exceptions.ConditionalCheckFailedException()
        self.ids.add(Item["id"]["S"])

    def delete_item(self, TableName, Key):
        self.ids.discard(Key["id"]["S"])


class FakeSession:
    def __init__(self, lam, table=None):
        self.lam, self.table = lam, table or FakeTable()

    def client(self, name, **_):
        assert name in ("lambda", "dynamodb"), name
        return self.lam if name == "lambda" else self.table


@pytest.fixture
def lam(monkeypatch):
    fake = FakeLambda()
    fake.session = FakeSession(fake)
    monkeypatch.setattr(hook, "_session", fake.session)
    monkeypatch.setattr(hook, "webhook_secret", lambda: "s3cret")
    monkeypatch.setenv("DELIVERIES_TABLE", "coding-agents-pr-review-deliveries")
    return fake


def call(body: dict, kind: str, secret="s3cret", signature=None):
    raw = json.dumps(body).encode()
    event = {"headers": {"X-GitHub-Event": kind,
                         "X-Hub-Signature-256": signed(secret, raw) if signature is None else signature},
             "body": base64.b64encode(raw).decode(), "isBase64Encoded": True}
    return hook.handler(event, Ctx())


def test_a_signed_pull_request_is_handed_off_and_answered_at_once(lam):
    r = call(pr_event(), "pull_request")
    assert r["statusCode"] == 202 and len(lam.calls) == 1
    assert lam.calls[0]["InvocationType"] == "Event"
    job = json.loads(lam.calls[0]["Payload"])["job"]
    assert job["mode"] == "review"
    assert (job["owner"], job["repo"], job["number"], job["sha"]) == ("octo", "game", 7, "abc1234def")


def test_a_wrong_or_missing_signature_is_refused(lam):
    assert call(pr_event(), "pull_request", signature="sha256=" + "0" * 64)["statusCode"] == 401
    assert call(pr_event(), "pull_request", signature="")["statusCode"] == 401
    assert lam.calls == []


def test_only_a_reviewable_pull_request_starts_a_session(lam):
    for event, kind in ((pr_event("closed"), "pull_request"), (pr_event(draft=True), "pull_request"),
                        (pr_event(state="closed"), "pull_request"), ({"action": "created"}, "issue_comment")):
        assert call(event, kind)["statusCode"] == 202
    assert call({"zen": "x"}, "ping")["statusCode"] == 200
    assert lam.calls == []
    assert call(pr_event("synchronize"), "pull_request")["statusCode"] == 202 and len(lam.calls) == 1


def test_a_team_builds_pull_request_is_left_to_its_own_gate_and_review(lam):
    """Lab 3's coordinator opens its pull requests from workshop/runs/... branches in
    the same repository connect-github.sh connected; each already has an executed check
    and an independent Assessment, so the event reviewer adds no second verdict."""
    team = pr_event()
    team["pull_request"]["head"]["ref"] = "workshop/runs/run-153232-c415c8e32151/codex-4920a1"
    assert call(team, "pull_request")["statusCode"] == 202 and lam.calls == []
    person = pr_event()
    person["pull_request"]["head"]["ref"] = "add-pause"
    call(person, "pull_request")
    assert len(lam.calls) == 1


def test_a_signed_event_is_acted_on_once(lam):
    """A redelivery or a replay of the same signed body starts nothing new."""
    body = pr_event()
    assert call(body, "pull_request")["statusCode"] == 202
    again = call(body, "pull_request")
    assert again["statusCode"] == 202 and "already handled" in again["body"]
    assert len(lam.calls) == 1


def test_a_stranger_s_pull_request_never_reaches_a_session(lam):
    """The review runs the change; a fork from someone the repository does not trust must
    not get a microVM with the Runtime's role and the shared mount."""
    for association in ("NONE", "FIRST_TIME_CONTRIBUTOR", "CONTRIBUTOR", None):
        event = pr_event()
        event["pull_request"]["author_association"] = association
        assert call(event, "pull_request")["statusCode"] == 202
    assert lam.calls == []


def _from(event, head_repo, association, login="dev"):
    event["repository"]["full_name"] = "octo/game"
    event["pull_request"]["head"]["repo"] = {"full_name": head_repo} if head_repo else None
    event["pull_request"]["author_association"] = association
    event["pull_request"]["user"] = {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}
    return event


def test_the_fixers_own_pull_request_is_reviewed(lam):
    """The App opens the fixer's pull request from a branch of the same repository; its
    association is not OWNER, and a different session must still review the fix."""
    assert call(_from(pr_event(), "octo/game", "NONE", "workshop-app[bot]"), "pull_request")["statusCode"] == 202
    assert len(lam.calls) == 1


def test_a_strangers_fork_is_not_reviewed_but_an_owners_fork_is(lam):
    call(_from(pr_event(), "stranger/game", "NONE", "stranger"), "pull_request")
    call(_from(pr_event(), "stranger/game", "FIRST_TIME_CONTRIBUTOR", "stranger"), "pull_request")
    assert lam.calls == []
    call(_from(pr_event(), "octo-fork/game", "OWNER", "octo"), "pull_request")
    assert len(lam.calls) == 1


def test_a_failed_hand_off_can_be_redelivered(lam):
    """If the asynchronous hand-off raises, the digest is forgotten, so GitHub's
    Redeliver of the same body is acted on instead of answered 'already handled'."""
    body = pr_event()

    def refuse(**kw):
        raise RuntimeError("TooManyRequestsException")

    lam.invoke = refuse
    with pytest.raises(RuntimeError):
        call(body, "pull_request")
    assert lam.session.table.ids == set()
    del lam.invoke
    assert call(body, "pull_request")["statusCode"] == 202 and len(lam.calls) == 1


def test_an_unreachable_table_answers_503_not_a_crash(lam):
    class ClientError(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

    lam.session.table.broken = ClientError("denied")
    r = call(pr_event(), "pull_request")
    assert r["statusCode"] == 503 and lam.calls == []


def test_a_pull_request_from_a_deleted_fork_is_not_reviewed(lam):
    call(_from(pr_event(), None, "NONE", "gone"), "pull_request")
    assert lam.calls == []


def test_a_stale_signed_event_is_ignored(lam):
    """GitHub's signature carries no time, so the event's own time bounds a replay."""
    r = call(pr_event(updated=_now(-2 * hook.REPLAY_WINDOW_S)), "pull_request")
    assert r["statusCode"] == 202 and "replay window" in r["body"]
    no_time = pr_event()
    del no_time["pull_request"]["updated_at"]
    call(no_time, "pull_request")
    assert lam.calls == []


def test_the_endpoint_reads_the_webhook_secret_never_the_app_key():
    assert hook.SECRET_ID == "agentcore/github-mcp/webhook"


def test_no_secret_means_refuse_never_accept_unsigned(lam, monkeypatch):
    monkeypatch.setattr(hook, "webhook_secret", lambda: "")
    assert call(pr_event(), "pull_request")["statusCode"] == 503
    assert lam.calls == []


def fix_comment(body="/fix please", association="OWNER", user_type="User", on_pr=True):
    issue = {"number": 7, "title": "Add pause"}
    if on_pr:
        issue["pull_request"] = {"url": "x"}
    return {"action": "created", "repository": {"name": "game", "owner": {"login": "octo"}},
            "issue": issue, "comment": {"body": body, "author_association": association,
                                        "created_at": _now(),
                                        "user": {"login": "dev", "type": user_type}}}


def test_a_trusted_fix_comment_starts_a_fixer(lam):
    assert call(fix_comment("/fix keep it short"), "issue_comment")["statusCode"] == 202
    job = json.loads(lam.calls[0]["Payload"])["job"]
    assert job["mode"] == "fix" and job["number"] == 7 and job["request"] == "keep it short"


def test_untrusted_bot_and_ordinary_comments_start_nothing(lam):
    for event in (fix_comment(association="NONE"), fix_comment(user_type="Bot"),
                  fix_comment(body="looks good"), fix_comment(on_pr=False)):
        assert call(event, "issue_comment")["statusCode"] == 202
    assert lam.calls == [], "only a person with write access can wake a fixer"


def test_a_members_or_collaborators_fix_still_starts_a_fixer(lam):
    """/fix keeps its trust rule: its pull request is merged by a person, so a member or
    collaborator may ask for one. Only starting a team is narrowed to the owner."""
    for association in ("MEMBER", "COLLABORATOR"):
        lam.calls.clear()
        assert call(fix_comment(f"/fix as {association}", association=association),
                    "issue_comment")["statusCode"] == 202
        (handed,) = lam.calls
        job = json.loads(handed["Payload"])["job"]
        assert job["mode"] == "fix" and job["request"] == f"as {association}"


def test_the_bundle_round_trips_and_fits_one_command():
    files = {f"src{i}.py": "x = 1\n" * 4000 + f"# {i}\n" for i in range(12)}
    encoded, dropped = hook.fit_bundle("+x", files, {"RULES.md": "Use Decimal for money."})
    assert len(encoded) <= hook.MAX_BUNDLE_CHARS
    job = {"owner": "octo", "repo": "game", "number": 7, "title": 'Add "pause" mode', "author": "dev"}
    for mode in ("review", "fix"):
        command = hook.build_command(job, encoded, mode=mode)
        assert len(command) < 65_536, "InvokeAgentRuntimeCommand accepts at most 65,536 characters"
    inner = shlex.split(hook.build_command(job, encoded, mode="fix"))[2]
    assert "WORKSHOP_AGENT_WORKDIR=/tmp/work" in inner and hook.FILES_BEGIN in inner
    import base64 as b64, io as _io, tarfile as _tar
    with _tar.open(fileobj=_io.BytesIO(b64.b64decode(encoded)), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert {"pr.diff", "RULES.md"} <= names
    assert all(f"src/{p}" in names for p in files if p not in dropped)


def test_a_too_large_change_drops_the_largest_files_first():
    import random
    rnd = random.Random(7)
    noise = lambda n: "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))
    files = {"big.py": noise(90_000), "small.py": "y = 2\n"}
    encoded, dropped = hook.fit_bundle("+y", files, {})
    assert dropped == ["big.py"] and len(encoded) <= hook.MAX_BUNDLE_CHARS


def test_the_fixers_files_are_read_back_from_its_output():
    out_tar = hook.bundle({"src/discount.py": "def f(): return 1\n", "src/../escape.py": "x", "notes.md": "n"})
    stdout = f"noise\n{hook.BEGIN}\n- discount.py: fixed\n{hook.END}\n{hook.FILES_BEGIN}\n{out_tar}\n{hook.FILES_END}\n"
    assert hook.extract_files(stdout) == {"discount.py": "def f(): return 1\n"}
    assert hook.extract_review(stdout) == "- discount.py: fixed"
    assert hook.extract_files("no files") == {}


def test_the_latest_review_is_the_one_the_fixer_reads():
    comments = [{"body": hook.REVIEW_HEADING + "\nold"}, {"body": "chat"},
                {"body": hook.REVIEW_HEADING + "\nnew"}, {"body": "Opened #9"}]
    assert hook.latest_review(comments).endswith("new")
    assert hook.latest_review([]) == ""


def test_the_diff_carries_new_file_line_numbers():
    patch = "@@ -1,3 +1,4 @@\n def f():\n-    return 1\n+    x = 2\n+    return x\n \n\\ No newline at end of file"
    numbered = hook.number_patch(patch).splitlines()
    assert numbered[0].startswith("@@")
    assert numbered[1] == "   1  def f():"
    assert numbered[2] == "     -    return 1"
    assert numbered[3] == "   2 +    x = 2" and numbered[4] == "   3 +    return x"
    assert numbered[6].startswith("\\")
    assert "   1 +def apply" in hook.build_diff({"files": [{"filename": "d.py", "status": "added", "additions": 1,
                                                         "deletions": 0, "patch": "@@ -0,0 +1 @@\n+def apply(): pass"}]})


def test_the_review_is_read_between_its_markers():
    out = f"noise\n{hook.BEGIN}\nLooks fine.\n\nVerdict: looks good\n{hook.END}\ntrailing"
    assert hook.extract_review(out) == "Looks fine.\n\nVerdict: looks good"
    assert hook.extract_review("no markers") == ""


def test_the_review_session_does_not_claim_a_kernel_identity():
    command = hook.build_command({"owner": "o", "repo": "r", "number": 1, "title": "t", "author": "a"}, "+x")
    assert "boot_id" not in command, "sessions share a boot ID; it identifies nothing"


def test_the_team_branch_prefix_matches_the_engine():
    source = (SOURCE.parents[2] / "orchestrator" / "work_items.py").read_text()
    assert 'branch=f"' + hook.TEAM_BUILD_BRANCH in source


def test_rules_come_from_the_default_branch_so_a_stacked_fix_is_judged_by_them(monkeypatch):
    """/fix opens a pull request whose base is the feature branch, which was
    created before REVIEW.md landed on main; reading the rules from that base gave the
    fix's reviewer no rules at all."""
    asked = []

    def tool(url, name, args, raw=False):
        asked.append((name, args.get("path"), args.get("ref")))
        return "# rules" if args.get("path") == hook.RULES_FILE else "code"

    monkeypatch.setattr(hook, "tool", tool)
    job = hook.job_from_event("pull_request", {
        **pr_event(), "repository": {"name": "game", "owner": {"login": "octo"},
                                     "default_branch": "main"}})
    assert job["default_branch"] == "main"
    pr = {"files": [{"filename": "discount.py", "status": "modified"}], "head_sha": "abc",
          "base": "add-pause"}
    _files, rules = hook.gather("https://gw", job, pr)
    assert rules == "# rules"
    assert (("get_file", hook.RULES_FILE, "main")) in asked


# ---------------- A new issue starts the coordinator's team ----------------

COORDINATOR_ARN = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:"
                   "runtime/CodingAgents_orchestrator-Zz9AbCdEf1")
TITLE = "Build a lighthouse game"
BODY = "A keeper guides ships through a storm."
IDEA = f"{TITLE}\n\n{BODY}"
STARTED = {"run_id": "run_153232_c415c8e32151", "status": "queued",
           "agents": ["claude-code", "codex"], "preset": "team-game"}


def issue_opened(title=TITLE, body=BODY, association="OWNER", user_type="User", action="opened"):
    return {"action": action, "repository": {"name": "game", "owner": {"login": "octo"}},
            "issue": {"number": 7, "title": title, "body": body, "author_association": association,
                      "created_at": _now(), "html_url": "https://github.com/octo/game/issues/7",
                      "user": {"login": "dev", "type": user_type}}}


def sse(*objects, encode_twice=True) -> bytes:
    """Frames as the Runtime SDK writes them. The coordinator yields json.dumps(reply), a
    string, which the SDK JSON-encodes again; the SDK's own error event is an object."""
    return "".join(f"data: {json.dumps(json.dumps(o) if encode_twice else o)}\n\n"
                   for o in objects).encode()


def streaming(data: bytes):
    from botocore.response import StreamingBody
    return StreamingBody(io.BytesIO(data), len(data))


class FakeAgentCore:
    """The Runtime list and the coordinator invocation. Stopping the session would end
    the team's build, so the fake refuses it outright."""

    def __init__(self):
        self.runtimes = [
            {"agentRuntimeName": "claude_code",
             "agentRuntimeArn": COORDINATOR_ARN.replace("CodingAgents_orchestrator-Zz9AbCdEf1", "claude_code-AbC")},
            {"agentRuntimeName": "CodingAgents_orchestrator", "agentRuntimeArn": COORDINATOR_ARN}]
        self.stream, self.content_type, self.error = sse(STARTED), "text/event-stream", None
        self.invocations, self.configs, self.comments, self.stopped = [], [], [], []
        self.listed = 0

    def list_agent_runtimes(self, **_):
        self.listed += 1
        return {"agentRuntimes": self.runtimes}

    def invoke_agent_runtime(self, **kw):
        self.invocations.append(kw)
        if self.error:
            raise self.error
        return {"contentType": self.content_type, "statusCode": 200,
                "response": streaming(self.stream)}

    def stop_runtime_session(self, **kw):
        self.stopped.append(kw)
        raise AssertionError("the team's build runs in this session; it must never be stopped")


class TeamSession:
    def __init__(self, fake, base):
        self.fake, self.base = fake, base

    def client(self, name, **kw):
        if name in ("lambda", "dynamodb"):
            return self.base.client(name, **kw)
        assert name in ("bedrock-agentcore-control", "bedrock-agentcore"), name
        if name == "bedrock-agentcore":
            self.fake.configs.append(kw.get("config"))
        return self.fake


@pytest.fixture
def team(lam, monkeypatch):
    """The hand-off and the job's world: a Gateway that records comments, fake AgentCore."""
    fake = FakeAgentCore()
    monkeypatch.setattr(hook, "_session", TeamSession(fake, lam.session))
    monkeypatch.setattr(hook, "gateway_url", lambda: "https://gw")

    def tool(url, name, args, raw=False):
        assert name == "comment_on_issue", name
        fake.comments.append(args)
        return {}

    monkeypatch.setattr(hook, "tool", tool)
    return fake


def _job(**overrides):
    job = hook.job_from_event("issues", issue_opened())
    job.update(overrides)
    return job


def test_an_owner_opening_an_issue_hands_off_a_team_job(lam):
    r = call(issue_opened(), "issues")
    assert r["statusCode"] == 202 and "agent team is waking up" in r["body"]
    job = json.loads(lam.calls[0]["Payload"])["job"]
    assert (job["mode"], job["owner"], job["repo"], job["number"], job["author"]) == (
        "team", "octo", "game", 7, "dev")
    assert job["idea"] == IDEA and job["title"] == TITLE
    assert job["by_owner"] is True


def test_the_owners_issue_reaches_the_coordinator(lam, team):
    call(issue_opened(association="OWNER"), "issues")
    (handed,) = lam.calls
    result = hook.handler(json.loads(handed["Payload"]), Ctx())
    assert result["started"] is True and len(team.invocations) == 1


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR"])
def test_only_the_repository_owner_can_start_a_team(lam, team, capsys, association):
    """The taught coordinator merges green, approved work on its own, and GitHub gives
    MEMBER and COLLABORATOR to people with read-only access too. They are told so on the
    issue, and the coordinator is never listed or invoked."""
    answer = call(issue_opened(association=association), "issues")
    assert answer["statusCode"] == 202 and "only the repository owner" in answer["body"]
    (handed,) = lam.calls
    job = json.loads(handed["Payload"])["job"]
    assert job["mode"] == "team" and job["by_owner"] is False
    result = hook.handler({"job": job}, Ctx())
    assert result["started"] is False and result["reason"] == "NOT_OWNER"
    (comment,) = team.comments
    assert comment["issue_number"] == 7
    assert comment["body"].startswith("Only the repository owner can start a team, never a bot")
    assert "started nothing" in comment["body"] and "@" not in comment["body"]
    assert team.invocations == [] and team.configs == [] and team.listed == 0
    assert '"reason": "NOT_OWNER"' in capsys.readouterr().out


def test_a_job_without_the_owner_mark_starts_nothing(team):
    """A job that does not say the owner asked fails closed."""
    job = _job()
    del job["by_owner"]
    assert hook.team(job)["reason"] == "NOT_OWNER"
    assert team.invocations == []


def test_the_idea_is_the_issue_title_then_its_description():
    for title, body, idea in ((TITLE, BODY, IDEA),
                              (f"  {TITLE}  ", f"\n {BODY}\n", IDEA),
                              (TITLE, None, TITLE), (TITLE, "   ", TITLE),
                              ("", BODY, BODY), (None, None, "")):
        assert hook.idea_from(title, body) == idea, (title, body)


def test_a_bots_or_strangers_issue_is_not_even_answered(lam):
    """The App is a Bot, so nothing the team does on GitHub can start another team."""
    for event in (issue_opened(association="NONE"), issue_opened(association="CONTRIBUTOR"),
                  issue_opened(association="FIRST_TIME_CONTRIBUTOR"),
                  issue_opened(association=None), issue_opened(user_type="Bot")):
        assert call(event, "issues")["statusCode"] == 202
    assert lam.calls == []


def test_only_a_new_issue_starts_a_team(lam):
    """Editing, reopening, labeling, or commenting on an issue starts nothing, so a running
    build is never changed under it and a person retries nothing by accident."""
    for action in ("edited", "reopened", "labeled", "assigned", "closed", "deleted"):
        assert call(issue_opened(action=action), "issues")["statusCode"] == 202, action
    pull = issue_opened()
    pull["issue"]["pull_request"] = {"url": "x"}
    call(pull, "issues")
    comment = fix_comment(body=BODY, on_pr=False)
    call(comment, "issue_comment")
    assert lam.calls == []


def test_every_idea_reply_asks_for_a_new_issue_because_an_edit_is_not_read():
    """A person who fixes the placeholder by editing the issue gets no answer and no
    build, so each reply says to open a new issue."""
    for code, reply in hook.IDEA_REPLIES.items():
        assert "new issue" in reply and "editing this one starts nothing" in reply, code


def test_a_stale_or_repeated_issue_starts_nothing_new(lam):
    old = issue_opened()
    old["issue"]["created_at"] = _now(-2 * hook.REPLAY_WINDOW_S)
    assert "replay window" in call(old, "issues")["body"]
    body = issue_opened()
    call(body, "issues")
    assert "already handled" in call(body, "issues")["body"]
    assert len(lam.calls) == 1


def test_an_issue_without_a_usable_idea_starts_no_team(lam, team):
    """Each is answered on GitHub by the hand-off's copy and never invokes the coordinator."""
    cases = (("   ", "", "no idea to build"),
             ("Team game", "<your idea, in one sentence>", "placeholder"),
             ("<a game idea>", "", "placeholder"),
             ("Team game", "Creative direction: <your idea, in one sentence>.", "placeholder"),
             ("Team game", "x" * (hook.MAX_IDEA_CHARS + 1), "longer than"))
    for title, body, reply in cases:
        lam.calls.clear()
        answer = call(issue_opened(title, body), "issues")
        assert answer["statusCode"] == 202
        (handed,) = lam.calls
        result = hook.handler(json.loads(handed["Payload"]), Ctx())
        assert result["started"] is False, body
        assert reply in team.comments[-1]["body"], body
        assert "no team was started" in team.comments[-1]["body"]
    assert team.invocations == [] and team.configs == []
    assert len(team.comments) == len(cases)


def test_a_started_team_is_invoked_once_and_its_session_is_never_stopped(team, capsys):
    result = hook.handler({"job": _job(received_at=time.time())}, Ctx())
    (sent,) = team.invocations
    assert sent["agentRuntimeArn"] == COORDINATOR_ARN
    session = sent["runtimeSessionId"]
    assert len(session) >= 33, "AgentCore rejects a session ID under 33 characters"
    assert session.startswith("gh-issue-7-")
    assert (sent["contentType"], sent["accept"]) == ("application/json", "text/event-stream")
    assert json.loads(sent["payload"]) == {"github_event": {
        "kind": "develop", "owner": "octo", "repo": "game", "issue_number": 7, "author": "dev",
        "idea": IDEA}}
    (config,) = team.configs
    assert config.read_timeout == hook.START_READ_TIMEOUT_S == 300
    assert config.retries == {"total_max_attempts": 1}, "a resent invocation could start a second build"
    assert result["started"] is True and result["run_id"] == STARTED["run_id"]
    assert team.stopped == []
    woke, started = (c["body"] for c in team.comments)
    assert woke.startswith("**An agent team woke up.**") and session[-12:] in woke
    assert "`dev` opened this issue" in woke and "the issue did" in woke
    assert f"`{STARTED['run_id']}`" in started and "`claude-code`, `codex`" in started
    assert "`python3 orchestrator/watch_run.py --plain`" in started
    assert all(c["issue_number"] == 7 for c in team.comments)
    assert not any("@" in c["body"] for c in team.comments), "a login in backticks notifies nobody"
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("TEAM_STARTED "))
    logged = json.loads(line.split(" ", 1)[1])
    assert (logged["run_id"], logged["session"], logged["number"]) == (STARTED["run_id"], session, 7)


def test_the_run_id_is_read_from_either_frame_shape():
    frames = (sse(STARTED), sse(STARTED, encode_twice=False),
              b": keep-alive\n\ndata: \"thinking\"\n\n" + sse({"note": "no id"}) + sse(STARTED),
              ("data: {\"run_id\": \"%s\",\r\ndata: \"status\": \"queued\", \"agents\": "
               "[\"claude-code\", \"codex\"], \"preset\": \"team-game\"}\r\n\r\n" % STARTED["run_id"]).encode())
    for data in frames:
        assert hook.first_reply(streaming(data).iter_lines()) == STARTED, data
    # The last frame may end without its blank line; nothing at all is no answer.
    assert hook.first_reply([b'data: {"error": "IDEA_MISSING"}']) == {"error": "IDEA_MISSING"}
    assert hook.first_reply([]) == {}


def test_a_json_answer_and_an_empty_stream_are_both_understood(team):
    team.content_type, team.stream = "application/json", json.dumps(STARTED).encode()
    assert hook.start_team(COORDINATOR_ARN, "gh-issue-7-" + "0" * 36, {"kind": "develop"}) == STARTED
    team.content_type, team.stream = "text/event-stream", b""
    assert hook.start_team(COORDINATOR_ARN, "gh-issue-7-" + "1" * 36, {"kind": "develop"})["error"] == "NO_REPLY"


def test_a_missing_coordinator_is_reported_on_the_issue(team, capsys):
    team.runtimes = [r for r in team.runtimes if r["agentRuntimeName"] != hook.COORDINATOR_NAME]
    result = hook.team(_job())
    assert result["started"] is False and result["reason"] == "COORDINATOR_MISSING"
    (comment,) = team.comments
    assert comment["body"].startswith("No coordinator yet: run `./deploy-coordinator.sh` "
                                      "(Lab 3, Deploy the Coordinator, step 3)")
    assert team.invocations == []
    assert '"reason": "COORDINATOR_MISSING"' in capsys.readouterr().out


ACCOUNT_MESSAGE = ("User: arn:aws:sts::111122223333:assumed-role/agentcore-pr-review-us-west-2/"
                   "stack-pr-review is not authorized to perform: bedrock-agentcore:InvokeAgentRuntime "
                   "on resource: " + COORDINATOR_ARN)


def _never_leaks(body):
    assert "111122223333" not in body and "arn:" not in body, body
    assert "Detail:" not in body, "GitHub gets a code and a fixed hint, never a raw message"


def _refused_line(out):
    line = next(x for x in out.splitlines() if x.startswith("TEAM_REFUSED "))
    return json.loads(line.split(" ", 1)[1])


@pytest.mark.parametrize("reply, code, hint", [
    ({"error": "BUILD_ALREADY_RUNNING:run_101010_aaaaaaaaaaaa", "detail": "one build at a time"},
     "BUILD_ALREADY_RUNNING:run_101010_aaaaaaaaaaaa", "starts nothing while another build is running"),
    ({"error": "BUILD_ALREADY_RUNNING", "running_run_id": "run_101010_aaaaaaaaaaaa",
      "detail": f"Could not confirm that no build is running (ClientError: {ACCOUNT_MESSAGE})"},
     "BUILD_ALREADY_RUNNING", "It sees `run_101010_aaaaaaaaaaaa` still running."),
    ({"error": "RUN_STATE_UNAVAILABLE", "detail": "could not read its saved run state (AccessDenied)"},
     "RUN_STATE_UNAVAILABLE", "could not read its saved runs"),
    ({"error": "REPOSITORY_MISMATCH", "detail": "this coordinator builds octo/other"},
     "REPOSITORY_MISMATCH", "deployed with"),
    ({"error": "IDEA_PLACEHOLDER"}, "IDEA_PLACEHOLDER", "placeholder"),
    # Codes the endpoint has no hint for: the code and the generic hint, never the detail.
    ({"error": "NOT_DEVELOP", "detail": "kind is not develop"}, "NOT_DEVELOP", hook.SEE_LOG),
    ({"error": "EVENT_INVALID", "detail": "The event needs the GitHub login of the person who asked."},
     "EVENT_INVALID", hook.SEE_LOG),
    ({"error": "BUILD_NOT_STARTED", "detail": f"ClientError: {ACCOUNT_MESSAGE}"},
     "BUILD_NOT_STARTED", hook.SEE_LOG),
    ({"error": "SOMETHING_NEW", "detail": "a code added to the coordinator later"},
     "SOMETHING_NEW", hook.SEE_LOG),
])
def test_a_coordinator_refusal_is_commented_once_and_not_retried(team, capsys, reply, code, hint):
    team.stream = sse(reply)
    result = hook.team(_job())
    assert len(team.invocations) == 1
    assert result["started"] is False and result["reason"] == code
    woke, refused = (c["body"] for c in team.comments)
    assert refused.startswith(f"The coordinator did not start a build: `{code}`.") and hint in refused
    assert "may still have started" not in refused
    _never_leaks(refused)
    assert not reply.get("detail") or reply["detail"] not in refused
    logged = _refused_line(capsys.readouterr().out)
    assert logged["reason"] == code and logged["may_have_started"] is False
    assert logged["detail"] == " ".join(str(reply.get("detail") or "").split())


def test_the_sdks_own_error_event_may_have_come_after_a_build_started(team, capsys):
    """The Runtime SDK sends its own event when the entrypoint raises; the endpoint
    cannot tell whether the coordinator had already submitted the build."""
    team.stream = sse({"error": f"boom `@x` {ACCOUNT_MESSAGE}", "error_type": "RuntimeError",
                       "message": "An error occurred during streaming"}, encode_twice=False)
    result = hook.team(_job())
    assert len(team.invocations) == 1 and result["reason"] == "RuntimeError"
    refused = team.comments[-1]["body"]
    assert "`RuntimeError`" in refused and "may still have started" in refused
    assert "watch_run.py --plain" in refused and "did not start a build" not in refused
    assert "`@x`" not in refused
    _never_leaks(refused)
    assert ACCOUNT_MESSAGE in _refused_line(capsys.readouterr().out)["detail"]


def test_a_code_shaped_sdk_error_event_still_may_have_started(team):
    """The SDK's error event can carry a message that looks like a refusal code (for
    example RUNTIME_NOT_WIRED raised outside the admission); only a reply WITHOUT
    error_type is the coordinator's own definite refusal."""
    team.stream = sse({"error": "RUNTIME_NOT_WIRED:codex", "error_type": "RuntimeError",
                       "message": "An error occurred during streaming"}, encode_twice=False)
    result = hook.team(_job())
    refused = team.comments[-1]["body"]
    assert result["may_have_started"] is True
    assert "may still have started" in refused and "did not start a build" not in refused


def test_a_session_excerpt_posted_on_github_carries_no_arn_or_account():
    text = ("AccessDeniedException: User: arn:aws:sts::123456789012:assumed-role/agentcore-x/s "
            "is not authorized on arn:aws:bedrock:us-west-2:123456789012:inference-profile/p")
    redacted = hook.redact(text)
    assert "arn:aws" not in redacted and "123456789012" not in redacted
    assert redacted.startswith("AccessDeniedException: User: <arn>")


def _client_error(code, status, message=ACCOUNT_MESSAGE):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "InvokeAgentRuntime")


def _definitely_not_started():
    from botocore import exceptions as e
    url = {"endpoint_url": "https://bedrock-agentcore.us-west-2.amazonaws.com"}
    return [
        (_client_error("AccessDeniedException", 403), "AccessDeniedException",
         "redeploy the endpoint stack (Lab 3, Deploy the Endpoint, step 1)"),
        (_client_error("ResourceNotFoundException", 404), "ResourceNotFoundException",
         "Lab 3, Deploy the Coordinator, step 3"),
        (_client_error("ValidationException", 400), "ValidationException", hook.SEE_LOG),
        (_client_error("ThrottlingException", 429), "ThrottlingException", "Wait a minute"),
        (e.EndpointConnectionError(**url), "EndpointConnectionError", hook.SEE_LOG),
        (e.ConnectTimeoutError(**url), "ConnectTimeoutError", hook.SEE_LOG),
    ]


def _maybe_started():
    from botocore import exceptions as e
    url = {"endpoint_url": "https://bedrock-agentcore.us-west-2.amazonaws.com"}
    return [
        (e.ReadTimeoutError(**url), "COORDINATOR_NO_REPLY"),
        (e.ResponseStreamingError(error="IncompleteRead(512 bytes read)"), "ResponseStreamingError"),
        (e.ConnectionClosedError(**url), "ConnectionClosedError"),
        # 424: the Runtime itself failed, which may be after the coordinator submitted.
        (_client_error("RuntimeClientError", 424), "RuntimeClientError"),
        (_client_error("InternalServerException", 500), "InternalServerException"),
        (RuntimeError("something unexpected"), "RuntimeError"),
    ]


def test_a_failed_invocation_that_never_reached_the_coordinator_says_so(team, capsys):
    for error, code, hint in _definitely_not_started():
        team.error, team.invocations, team.comments = error, [], []
        result = hook.team(_job())
        assert len(team.invocations) == 1 and result["reason"] == code, code
        refused = team.comments[-1]["body"]
        assert refused.startswith(f"The coordinator did not start a build: `{code}`."), refused
        assert hint in refused and "may still have started" not in refused, code
        _never_leaks(refused)
    logged = [json.loads(x.split(" ", 1)[1]) for x in capsys.readouterr().out.splitlines()
              if x.startswith("TEAM_REFUSED ")]
    assert logged[0]["detail"] == ACCOUNT_MESSAGE, "the full message stays in the Lambda log"


def test_a_lost_answer_is_never_reported_as_no_build(team):
    """After the request may have reached the coordinator, which submits before it
    answers, the issue is told to check before opening another, never that nothing
    started: a second issue could start a second build."""
    for error, code in _maybe_started():
        team.error, team.invocations, team.comments = error, [], []
        result = hook.team(_job())
        assert len(team.invocations) == 1 and result["reason"] == code, code
        refused = team.comments[-1]["body"]
        assert f"`{code}`" in refused and "may still have started" in refused, refused
        assert "did not start a build" not in refused and "watch_run.py --plain" in refused
        _never_leaks(refused)
        if code == "COORDINATOR_NO_REPLY":
            assert refused.startswith("The coordinator did not answer within 300 s")


def test_a_stream_without_an_answer_may_have_started_a_build(team):
    team.error, team.stream = None, b": keep-alive\n\n"
    result = hook.team(_job())
    assert result["reason"] == "NO_REPLY" and result["may_have_started"] is True
    refused = team.comments[-1]["body"]
    assert "`NO_REPLY`" in refused and "may still have started" in refused
    assert "did not start a build" not in refused


def test_fix_on_a_team_builds_pull_request_is_refused_with_a_comment(monkeypatch, capsys):
    """The engine's bounded repair commits onto workshop/runs/...; a fixer's branch there
    would conflict with it, and a second maker would blur which check decides."""
    said = []

    def tool(url, name, args, raw=False):
        if name == "get_pull_request_files":
            return {"same_repo": True, "head_ref": "workshop/runs/run-153232-c415c8e32151/codex-4920a1",
                    "head_sha": "abc", "base": "main", "files": []}
        assert name == "comment_on_issue", name
        said.append(args)
        return {}

    monkeypatch.setattr(hook, "tool", tool)
    monkeypatch.setattr(hook, "gateway_url", lambda: "https://gw")
    monkeypatch.setattr(hook, "run_session", lambda *a, **k: pytest.fail("a team pull request got a fixer"))
    result = hook.fix({"owner": "octo", "repo": "game", "number": 7, "title": "t", "author": "dev"})
    assert result == {"number": 7, "fixed": False, "reason": "team_build"}
    (comment,) = said
    assert comment["issue_number"] == 7 and "belongs to a team build" in comment["body"]
    assert "FIX_REFUSED " in capsys.readouterr().out


def test_the_coordinator_name_is_the_one_deploy_coordinator_creates():
    script = (SOURCE.parents[2] / "orchestrator-agent" / "deploy-coordinator.sh").read_text()
    project, agent = hook.COORDINATOR_NAME.split("_", 1)
    assert f"agentcore create --name {project} " in script
    assert f"agentcore add agent --name {agent} " in script
