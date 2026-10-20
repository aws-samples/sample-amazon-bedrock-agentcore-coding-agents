"""progress.py: one read-only answer to "where am I, and what do I run next?".

Every test builds a throwaway workshop host in a temp tree (repo, coding-agents,
home, mount) and stubs only the live reads: the mount check and the AWS clients.
Nothing here reaches AWS, GitHub, or a model.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import time
import tokenize

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import progress  # noqa: E402
import run_store  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/claude_code-AbCdEf1234"


class NotFound(Exception):
    response = {"Error": {"Code": "ResourceNotFoundException"}}


class NoSuchEntity(Exception):
    response = {"Error": {"Code": "NoSuchEntity"}}


class ParameterNotFound(Exception):
    response = {"Error": {"Code": "ParameterNotFound"}}


class ImageNotFound(Exception):
    response = {"Error": {"Code": "ImageNotFoundException"}}


class FakeAws:
    """The handful of read calls progress.py makes, answering from the Box's state."""

    def __init__(self, box):
        self.box = box

    # bedrock-agentcore-control
    def get_agent_runtime(self, agentRuntimeId):
        if self.box.runtime_status is None:
            raise NotFound()
        return {"status": self.box.runtime_status,
                "roleArn": "arn:aws:iam::123456789012:role/service-role/"
                           "AmazonBedrockAgentCoreRuntimeDefaultServiceRole-abc12"}

    # iam
    def get_role_policy(self, RoleName, PolicyName):
        if not self.box.grants:
            raise NoSuchEntity()
        return {"PolicyName": PolicyName}

    def get_api_key_credential_provider(self, name):
        if not self.box.kiro_key:
            raise NotFound()
        return {"name": name}

    def get_paginator(self, name):
        assert name == "list_agent_runtimes"
        box = self.box

        class Pages:
            def paginate(self):
                names = (["claude_code"] if box.console_runtime else []) + box.team_runtimes
                runtimes = [{"agentRuntimeName": n, "agentRuntimeArn": ARN.rsplit("/", 1)[0] + "/" + n + "-x"}
                            for n in names]
                return [{"agentRuntimes": runtimes}]
        return Pages()

    # ecr
    def describe_images(self, repositoryName, imageIds):
        if not self.box.image:
            raise ImageNotFound()
        return {"imageDetails": [{"imageDigest": "sha256:" + "ab" * 32}]}

    def list_gateways(self, **_):
        return {"items": [{"name": "github-mcp-gateway"}] if self.box.gateway else []}

    # secretsmanager
    def describe_secret(self, SecretId):
        if not self.box.secret:
            raise NotFound()
        return {"Name": SecretId}

    # ssm
    def get_parameter(self, Name):
        if Name == progress.KIRO_KEY_STATUS_PARAM:
            if self.box.kiro_status is None:
                raise ParameterNotFound()
            return {"Parameter": {"Value": json.dumps({"key_status": self.box.kiro_status,
                                                        "source": "central-account"})}}
        if not self.box.endpoint:
            raise ParameterNotFound()
        return {"Parameter": {"Value": "https://abc123.execute-api.us-west-2.amazonaws.com/github"}}

    # logs
    def describe_log_groups(self, logGroupNamePattern):
        return {"logGroups": [{"logGroupName": "/stack/PrReviewWebhook"}]}

    def filter_log_events(self, **_):
        if not self.box.reviewed:
            return {"events": []}
        line = 'REVIEW_POSTED {"repo": "octo/game", "number": 3, "seconds": 74}'
        return {"events": [{"message": line}]}


class Box:
    def __init__(self, tmp_path, monkeypatch):
        self.repo = tmp_path / "repo"
        self.agents = self.repo / "coding-agents"
        self.home = tmp_path / "home"
        self.mnt = tmp_path / "mnt"
        for folder in (self.agents / "claude-code", self.home, self.mnt):
            folder.mkdir(parents=True)
        self.mounted = False
        self.runtime_status = "READY"
        self.gateway = self.secret = self.reviewed = self.kiro_key = self.console_runtime = False
        self.image = False
        self.endpoint = False
        self.grants = False
        self.kiro_status = None  # the event key import's record, absent outside an event
        self.runs: list[dict] = []
        self.team_runtimes: list[str] = []
        monkeypatch.setattr(run_store, "attach_reader_mirror", lambda: "")
        monkeypatch.setattr(run_store, "recent", lambda runs_dir, limit=10: list(self.runs))
        for key, value in {"WORKSHOP_REPO_ROOT": self.repo, "WORKSHOP_CODING_AGENTS_DIR": self.agents,
                           "HOME": self.home, "WORKSHOP_S3FILES_DIR": self.mnt,
                           "AWS_REGION": "us-west-2"}.items():
            monkeypatch.setenv(key, str(value))
        real_ismount = os.path.ismount
        monkeypatch.setattr(progress.os.path, "ismount",
                            lambda p: (self.mounted and p == str(self.mnt)) or
                            (p != str(self.mnt) and real_ismount(p)))
        fake = FakeAws(self)
        monkeypatch.setattr(progress, "_client", lambda service: fake)

    def host(self):
        (self.agents / "infra.config").write_text("INFRA_S3FILES_AP_ARN=arn:x\n")
        self.mounted = True
        (self.agents / "claude-code" / "agent.config").write_text(
            "ECR_URI=123456789012.dkr.ecr.us-west-2.amazonaws.com/coding-agents-claude-code:abc\n")
        return self

    def runtime(self):
        (self.agents / "claude-code" / "runtime_config.json").write_text(json.dumps({"runtime_arn": ARN}))
        return self

    def lid(self):
        (self.mnt / "lid").mkdir(exist_ok=True)
        (self.mnt / "lid" / "index.html").write_text("<canvas></canvas>")
        return self

    def shared_repo(self):
        (self.home / "lid-game" / ".git").mkdir(parents=True, exist_ok=True)
        (self.mnt / "lid.git").mkdir(exist_ok=True)
        (self.mnt / "lid.git" / "HEAD").write_text("ref: refs/heads/main\n")
        return self

    def fanout(self, agents=10, pushed=10):
        (self.home / "arcade").mkdir(exist_ok=True)
        (self.home / "arcade" / "fanout-summary.json").write_text(json.dumps(
            {"agents": agents, "pushed": pushed, "microvms": agents, "wall_s": 140}))
        return self


    def coordinator(self, roles=None):
        project = self.repo / "CodingAgents" / "agentcore"
        (project / ".cli").mkdir(parents=True, exist_ok=True)
        env = [{"name": "WORKSHOP_ROLES", "value": roles}] if roles else []
        (project / "agentcore.json").write_text(json.dumps({"runtimes": [{"envVars": env}]}))
        (project / ".cli" / "deployed-state.json").write_text(json.dumps({"targets": {"default": {
            "runtimeArn": ARN.replace("claude_code-AbCdEf1234", "CodingAgents_orchestrator-Zz9")}}}))
        return self


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def _next(box):
    return progress.next_step(progress._run_all(progress.SPECS))


def test_a_fresh_box_waits_for_the_host_first(box):
    step = _next(box)
    assert step.part == progress.GET_IN and step.name == "Workshop host"
    assert "facilitator" in step.note


def test_a_ready_host_is_told_to_build_then_create_the_runtime(box):
    box.host()
    step = _next(box)
    assert step.part == progress.PART1 and step.name == "Claude Code image"
    assert step.commands == progress.CMD_BUILD
    assert step.commands[-1] == "docker buildx bake claude-code --push"
    box.image = True
    step = _next(box)
    assert step.name == "Claude Code Runtime" and not step.commands
    assert "console" in step.note and "Create default role" in step.note


def test_a_fresh_clone_is_told_to_record_the_stack_ids(box):
    box.mounted = True
    step = _next(box)
    assert step.name == "Your clone's infra.config" and step.commands == progress.CMD_INFRA


def test_get_in_is_the_host_and_the_clone_because_the_attendee_builds_the_image(box):
    box.host()
    checks = progress._run_all(progress.SPECS)
    assert [c.name for c in checks if c.part == progress.GET_IN] == [
        "Workshop host", "Your clone's infra.config"]
    assert all(c.state == progress.DONE for c in checks if c.part == progress.GET_IN)
    image = next(c for c in checks if c.name == "Claude Code image")
    assert image.state == progress.TODO and "coding-agents-claude-code:latest" in image.detail


def test_a_runtime_built_in_the_console_is_found_by_its_name(box):
    box.host()
    box.console_runtime = True
    check = next(c for c in progress._run_all(progress.SPECS) if c.name == "Claude Code Runtime")
    assert check.state == progress.DONE


def test_a_recorded_runtime_that_is_gone_is_not_done(box):
    box.host().runtime()
    box.runtime_status = None
    checks = progress._run_all(progress.SPECS)
    runtime = next(c for c in checks if c.name == "Claude Code Runtime")
    assert runtime.state == progress.TODO and "no longer exists" in runtime.detail


def test_the_walk_follows_the_four_parts_in_order(box):
    box.host().runtime()
    box.image = True
    step = _next(box)
    assert step.name == "The work it finished" and progress.CMD_START[0] in step.commands
    box.lid()
    step = _next(box)
    assert step.name == "The shared repository" and step.commands == progress.CMD_SHARE
    assert step.commands[-1] == "python3 fanout.py --init"
    box.shared_repo()
    step = _next(box)
    assert step.commands == progress.CMD_FANOUT and "commit and push" in step.note
    box.fanout(pushed=7)
    assert _next(box).part == progress.PART2, "a run where some branches never arrived is not done"
    box.fanout()
    step = _next(box)
    assert step.name == "The review endpoint" and step.commands == progress.CMD_ENDPOINT
    review = next(c for c in progress._run_all(progress.SPECS) if c.name == "An agent woke up")
    assert review.state == progress.TODO, "a missing endpoint is a step to do, not unknown"
    box.endpoint = True
    assert _next(box).commands == progress.CMD_GITHUB
    box.secret = True
    assert "not deployed" in next(c for c in progress._run_all(progress.SPECS)
                                  if c.name == "GitHub connected").detail
    box.gateway = True
    step = _next(box)
    assert step.name == "An agent woke up" and "pull request" in step.note
    box.reviewed = True
    step = _next(box)
    # Part 4 opens with its prerequisite: the checker's key, before any image.
    assert step.part == progress.PART4 and step.commands == progress.CMD_KIRO_KEY
    assert "Check Kiro's API Key" in step.note and "KiroKeyStatus" in step.note
    box.kiro_key = True
    step = _next(box)
    assert step.part == progress.PART4 and step.commands == progress.CMD_TEAM_IMAGES
    box.team_runtimes = ["codex"]
    assert "kiro" in next(c for c in progress._run_all(progress.SPECS)
                          if c.name == "Codex and the checker").detail
    box.team_runtimes = ["codex", "kiro"]
    step = _next(box)
    assert step.commands == progress.CMD_COORDINATOR and "SKILL.md" in step.note
    box.coordinator()
    box.console_runtime = True
    step = _next(box)
    assert step.commands == progress.CMD_GRANTS, "the grants are step 5, during the deploy"
    box.grants = True
    step = _next(box)
    assert step.name == "A team build" and progress.CMD_ASK[-1] in step.commands and "Creative direction" in progress.CMD_ASK[-1]
    box.runs = [{"run_id": "run_1", "status": "running", "role_prs": [{"pr_url": "https://x/1"}]}]
    assert _next(box).name == "A team build", "a build still running is not done"
    box.runs[0]["status"] = "needs_human"
    assert _next(box) is None
    assert "All four labs are done" in progress.render(progress._run_all(progress.SPECS))


def test_the_event_key_that_did_not_arrive_names_its_reason_and_fallback(box):
    box.kiro_status = ("UNAVAILABLE: the secret does not exist after 420 s, so the central account "
                       "delivered no key. Fallback: use the checker that needs no key "
                       "(claude_code_validator, Build and Run the Team, step 2), or ask a facilitator "
                       "to rerun the team's Kiro key import.")
    check = next(c for c in progress._run_all(progress.SPECS) if c.name == "The checker's key")
    assert check.state == progress.TODO
    assert "did not arrive" in check.detail and "claude_code_validator" in check.detail


def test_the_event_key_in_the_vault_is_done_and_says_where_it_came_from(box):
    box.kiro_status, box.kiro_key = "READY", True
    check = next(c for c in progress._run_all(progress.SPECS) if c.name == "The checker's key")
    assert check.state == progress.DONE and "central account" in check.detail


def test_a_coordinator_deployed_with_the_no_key_checker_needs_no_kiro_key(box):
    box.coordinator(roles="claude-code,codex,claude-code-validator")
    check = next(c for c in progress._run_all(progress.SPECS) if c.name == "The checker's key")
    assert check.state == progress.DONE and "claude-code-validator" in check.detail


def test_the_no_key_checker_is_looked_up_by_its_own_runtime_name(box):
    box.coordinator(roles="claude-code,codex,claude-code-validator")
    box.team_runtimes = ["codex", "kiro"]
    check = next(c for c in progress._run_all(progress.SPECS) if c.name == "Codex and the checker")
    assert check.state == progress.TODO and "claude_code_validator" in check.detail
    box.team_runtimes = ["codex", "claude_code_validator"]
    check = next(c for c in progress._run_all(progress.SPECS) if c.name == "Codex and the checker")
    assert check.state == progress.DONE


def test_every_build_next_step_names_its_registry_for_a_new_terminal():
    for commands in (progress.CMD_BUILD, progress.CMD_TEAM_IMAGES):
        assert any(line.startswith("export REGISTRY=") for line in commands)
        assert commands.index(next(l for l in commands if l.startswith("export REGISTRY="))) < \
            commands.index(next(l for l in commands if l.startswith("docker buildx bake")))


def test_a_check_that_raises_is_unknown_and_never_hides_the_rest(box, monkeypatch):
    box.host()
    specs = [progress.Spec("w", "boom", lambda: 1 / 0), progress.Spec("w", "fine", progress.check_host)]
    checks = progress._run_all(specs)
    assert checks[0].state == progress.UNKNOWN and checks[1].state == progress.DONE


def test_a_slow_check_times_out_instead_of_hanging(box):
    started = time.time()
    checks = progress._run_all([progress.Spec("w", "slow", lambda: time.sleep(5) or {})], budget_s=0.3)
    assert time.time() - started < 2 and checks[0].state == progress.UNKNOWN


def test_json_output_is_valid_and_carries_next(box, capsys):
    box.host()
    progress.main(["--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["next"]["name"] == "Claude Code image"


def test_no_full_account_id_ever_reaches_the_output(box, capsys):
    box.host().runtime()
    progress.main([])
    out = capsys.readouterr().out
    assert "123456789012" not in out
    assert progress._mask(ARN) == ARN.replace("123456789012", "1234****9012")


def test_every_printed_command_block_is_one_the_pages_can_teach():
    for name in (n for n in dir(progress) if n.startswith("CMD_")):
        block = getattr(progress, name)
        assert block and all(isinstance(line, str) and line == line.strip() for line in block), name
        assert len(block) <= 3, f"{name}: keynote rule, at most three lines per copy block"


def _code_names(path: str) -> list[str]:
    """The module's identifiers, ignoring string literals (the printed commands) and comments."""
    with open(path, "rb") as handle:
        tokens = tokenize.tokenize(io.BytesIO(handle.read()).readline)
        return [t.string for t in tokens if t.type == tokenize.NAME]


def test_the_source_is_reporting_only():
    """Same rule as watch_run.py and replay.py: this can never touch a run or a resource."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress.py")
    names = set(_code_names(path))
    forbidden = {"llm", "reviewer", "engine", "executor", "runtime_exec", "invoke_agent_runtime",
                 "get_secret_value", "write_text", "write_bytes", "makedirs", "mkdir", "rmtree",
                 "remove", "unlink", "rename", "subprocess", "Popen", "system", "doctor"}
    assert not names & forbidden, sorted(names & forbidden)
    for prefix in ("create_", "update_", "delete_", "put_", "start_", "stop_", "invoke_"):
        assert not [n for n in names if n.startswith(prefix)], prefix
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [k.value.value for k in node.keywords
                      if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            assert not any(set(str(m)) & set("wax+") for m in modes), "progress.py must not write files"
