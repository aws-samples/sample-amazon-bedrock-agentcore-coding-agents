"""progress.py: one read-only answer to "where am I, and what do I run next?".

Every test builds a throwaway workshop host in a temp tree (repo, coding-agents,
home, mount, run state) and stubs only the live reads: the mount check, the
AgentCore status call, the local port probe, systemctl, and the gallery helper.
Nothing here reaches AWS, GitHub, or a model.
"""
from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import time
import tokenize

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import github  # noqa: E402
import progress  # noqa: E402
import run_store  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{}-AbCdEf1234"
AP = "arn:aws:s3files:us-west-2:123456789012:file-system/fs-1/access-point/fsap-1"
BASE = "https://dtest123.cloudfront.net"
PR = "https://github.com/octocat/moon-game/pull/1"


class Box:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.agents = self.repo / "coding-agents"
        self.home = tmp_path / "home"
        self.mnt = tmp_path / "mnt"
        self.runs = tmp_path / "runs"
        for folder in (self.agents / "gateway_mcp", self.home, self.mnt, self.runs):
            folder.mkdir(parents=True)
        self.mounted = False
        self.listening: set[int] = set()
        self.studio = "inactive"
        env = {
            "WORKSHOP_REPO_ROOT": self.repo, "WORKSHOP_CODING_AGENTS_DIR": self.agents,
            "HOME": self.home, "WORKSHOP_RUNS_DIR": self.runs, "WORKSHOP_S3FILES_DIR": self.mnt,
            "WORKSHOP_KIRO_SETTINGS": tmp_path / "kiro.local.json",
            "WORKSHOP_KIRO_DISABLE_VAULT": "1", "AWS_REGION": "us-west-2",
            "WORKSHOP_GATEWAY_STATE": tmp_path / "gateway-state.json",
            "WORKSHOP_PUBLIC_BASE_URL": BASE,
        }
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        for key in ("GITHUB_REPO", "GITHUB_GATEWAY_URL", "WORKSHOP_ROLES", "WORKSHOP_RUNTIME_BUCKET"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(github, "_SETTINGS", str(tmp_path / "github.local.json"))
        monkeypatch.setattr(run_store, "reader_mirror_bucket", lambda: "")
        monkeypatch.setattr(progress, "_is_mount", lambda path: self.mounted and path == str(self.mnt))
        monkeypatch.setattr(progress, "_service_status", lambda arn: ("READY", ""))
        monkeypatch.setattr(progress.workshop_urls, "local_answer",
                            lambda port, timeout=3: (True, "HTTP 200") if port in self.listening
                            else (False, f"nothing is listening on port {port}"))
        monkeypatch.setattr(progress, "_run_readonly", lambda argv, timeout=5: subprocess.CompletedProcess(
            argv, 0, stdout=self.studio + "\n", stderr=""))
        monkeypatch.setattr(progress, "_gallery_status", lambda: None)

    # --- Lab 1
    def storage(self):
        self.mounted = True
        (self.agents / "infra.config").write_text(f"INFRA_S3FILES_AP_ARN={AP}\n")
        (self.mnt / "notes.md").write_text("# My workspace\n")
        return self

    def runtime(self, harness_dir, mount=True):
        folder = self.agents / harness_dir
        folder.mkdir(exist_ok=True)
        (folder / "runtime_config.json").write_text(json.dumps({
            "runtime_arn": ARN.format(harness_dir.replace("-", "_")), "platform_version": "V1",
            **({"s3files_access_point_arn": AP} if mount else {})}))
        return self

    def kiro_key(self, check="verified"):
        (self.tmp / "kiro.local.json").write_text(json.dumps({
            "stored": True, "region": "us-west-2", "provider": "kiro-api-key",
            "key_tail": "…Q9Z7", "key_check": check}))
        return self

    def steering(self):
        (self.mnt / "AGENTS.md").write_text("# frontend\n")
        (self.mnt / ".kiro" / "steering").mkdir(parents=True)
        (self.mnt / ".kiro" / "steering" / "validator.md").write_text("---\ninclusion: always\n---\n")
        for skill in ("backend-engineering", "frontend-design"):
            (self.mnt / "skills" / skill).mkdir(parents=True)
        return self

    def lab1(self):
        return (self.storage().runtime("codex").runtime("kiro").kiro_key()
                .runtime("claude-code").steering())

    # --- Lab 2
    def github(self):
        gw = self.agents / "gateway_mcp"
        (gw / "github-app.env").write_text("GITHUB_APP_ID=SECRETAPPID\n"
                                           "GITHUB_APP_PRIVATE_KEY_PATH=/keys/SECRETPEMPATH.pem\n")
        (self.home / "app.private-key.pem").write_text("-----BEGIN " + "RSA PRIVATE KEY-----SECRETPEM")
        (self.tmp / "gateway-state.json").write_text(json.dumps({"gateway_url": "https://gw.example/mcp"}))
        (self.tmp / "github.local.json").write_text(json.dumps({
            "repo": "octocat/moon-game", "gateway_url": "https://gw.example/mcp"}))
        return self

    def coordinator(self):
        cli = self.repo / "CodingAgents" / "agentcore" / ".cli"
        cli.mkdir(parents=True)
        arn = ARN.format("orchestrator")
        (cli / "deployed-state.json").write_text(json.dumps({"targets": {"default": {"resources": {
            "runtimes": {"orchestrator": {"runtimeArn": arn, "runtimeId": "orchestrator-AbCdEf1234"}}}}}}))
        (cli / "runtime-platforms.json").write_text(json.dumps({"default": {"orchestrator": {
            "runtime_status": "READY", "platform_version": "V1", "runtime_arn": arn}}}))
        return self

    def run(self, run_id, status, *, pr=PR, submitted_by=None, next_action=None, saved_at=None, **extra):
        payload = {"run_id": run_id, "status": status, "phase": extra.pop("phase", "gate"),
                   "role_prs": [{"work_id": "w1", "agent": "claude-code", "pr_url": pr,
                                 "state": extra.pop("pr_state", "awaiting_review"),
                                 "error": extra.pop("pr_error", None)}] if pr else [],
                   "submitted_by": submitted_by, "next_action": next_action, **extra}
        if saved_at is None:
            run_store.save(str(self.runs), run_id, payload)
        else:
            (self.runs / "state").mkdir(exist_ok=True)
            (self.runs / "state" / f"{run_id}.json").write_text(json.dumps({**payload, "_saved_at": saved_at}))
        time.sleep(0.01)   # recent() orders by mtime
        return self

    def checkout(self, name="game", app=True):
        folder = self.home / name
        folder.mkdir()
        (folder / "README.md").write_text("# game\n")
        if app:
            (folder / "server.js").write_text("// game\n")
        return self

    def report(self):
        checks = progress.collect(budget_s=10)
        return checks, progress.render(checks), progress.next_step(checks)


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def _by_name(checks, name):
    return next(c for c in checks if c.name == name)


def test_a_fresh_box_starts_at_the_first_lab1_gap(box):
    checks, text, step = box.report()
    assert step.name == "Shared storage"
    assert "NEXT  Lab 1 > Confirm the Shared Coding Workspace > 2. Confirm the mount and the ARN every deploy reads" in text
    assert "findmnt --mountpoint /mnt/s3files" in text
    assert text.startswith("Workshop progress for this host (read-only")
    # Lab 2 and 3 are still reported, so a facilitator sees the whole picture.
    assert _by_name(checks, "Coordinator").state == progress.TODO


def test_a_missing_backend_runtime_names_the_exact_deploy_command(box):
    box.storage().runtime("codex").runtime("kiro").kiro_key()
    _checks, text, step = box.report()
    assert step.name == "Claude Code Runtime (yours)"
    assert step.page == "Lab 1 > Create the Backend Runtime in the Console > 1. Print the values the form needs"
    # Printed flush left, exactly as the page shows it, so the pasted block runs.
    assert ("\ncd ~/sample-amazon-bedrock-agentcore-coding-agents/coding-agents\n"
            "WORKSHOP_MODEL=\"${WORKSHOP_MODEL_CLAUDE_CODE:-${WORKSHOP_CLAUDE_MODEL:-}}\" \\\n"
            "  ./deploy-prebuilt.sh claude-code --prepare\n") in text
    # The console path: the person builds it, then --adopt checks and records it.
    assert "AgentCore console" in text and "--adopt" in text


def test_a_runtime_without_the_mount_is_not_done(box):
    box.storage().runtime("codex", mount=False)
    checks, _text, _step = box.report()
    codex = _by_name(checks, "Codex Runtime")
    assert codex.state == progress.TODO and "without the shared /mnt/s3files mount" in codex.detail


def test_the_kiro_key_heredoc_stays_pasteable(box):
    box.storage().runtime("codex").runtime("kiro")
    _checks, text, step = box.report()
    assert step.name == "Kiro API key"
    lines = text.splitlines()
    # An indented heredoc terminator or Python body would break when pasted.
    assert "PY" in lines and "import kiro_config" in lines
    assert "python3 - <<'PY'" in lines


def test_a_passed_build_leads_to_checkout_then_the_real_play_url(box):
    box.lab1().github().coordinator().run("run_100000_aaaaaaaaaaaa", "passed")
    checks, text, step = box.report()
    assert _by_name(checks, "Build").state == progress.DONE
    assert PR in _by_name(checks, "Build").detail
    assert step.name == "Merged game on the host"
    assert "python3 orchestrator/github.py checkout ~/game" in text

    box.checkout("game")
    _checks, text, step = box.report()
    assert step.name == "Game on port 8000"
    assert "cd ~/game && claude" in text
    assert f"{BASE}/proxy/8000/" in text

    box.listening.add(8000)
    checks, text, step = box.report()
    assert f"open {BASE}/proxy/8000/ in the browser where VS Code is signed in" in \
        _by_name(checks, "Game on port 8000").detail
    assert step.name == "Agent Studio"
    assert "sudo systemctl start stage2-console" in text


def test_a_needs_human_run_shows_its_own_advice_and_the_pr(box):
    """ROLE_PR_BLOCKED with a GATE_RED row is what the engine records for a PR still red
    after its one repair; only then is "merge it yourself, or rerun" the advice."""
    advice = "One or more pull requests did not become mergeable after their bounded repair."
    box.lab1().github().coordinator().run("run_100000_bbbbbbbbbbbb", "needs_human",
                                          fail_reason="ROLE_PR_BLOCKED:w1", next_action=advice,
                                          pr_state="blocked", pr_error="GATE_RED")
    checks, text, step = box.report()
    build = _by_name(checks, "Build")
    assert build.state == progress.TODO
    assert "needs_human: ROLE_PR_BLOCKED:w1" in build.detail and PR in build.detail
    assert step.page == "Lab 2 > Run a Build and Follow It > 3. Read the recorded result"
    flat = " ".join(text.split())
    assert advice in flat and f"Open {PR}" in flat
    assert "merge it yourself" in flat and "rerun Run a Build step 1" in flat


def test_a_review_outage_keeps_the_engines_whole_advice(box):
    """The engine says not to rebuild for a review failure. The first version cut that
    sentence at 240 characters and then added "or submit again"."""
    advice = ("The build's executable check passed, but the independent review could not "
              "run twice. Its pull request is open with the gate evidence. Ask a facilitator "
              "to rerun only the review; do not rebuild the application for a review failure.")
    box.lab1().github().coordinator().run("run_100000_rrrrrrrrrrrr", "needs_human",
                                          fail_reason="REVIEW_UNAVAILABLE:w1", next_action=advice,
                                          pr_state="blocked", pr_error="REVIEW_UNAVAILABLE")
    _checks, text, _step = box.report()
    flat = " ".join(text.split())
    assert advice in flat
    assert "submit again" not in flat and "rerun Run a Build" not in flat


def test_a_pr_recorded_only_under_work_items_is_never_reported_missing(box):
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    box.lab1().github().coordinator().run(
        "run_100000_wwwwwwwwwwww", "running", pr=None, saved_at=old, phase="checker_authoring",
        work_items={"claude-code": {"work_id": "w1", "pr": {"pr_url": PR}}})
    checks, text, _step = box.report()
    flat = " ".join(text.split())
    assert PR in _by_name(checks, "Build").detail
    assert "No pull request was opened" not in flat and "agentcore invoke" not in flat
    assert "Do not resubmit the whole build" in flat


def test_a_passed_build_without_a_pr_is_not_done(box):
    advice = "The build passed but the App credential did not resolve, so no PR was opened."
    box.lab1().github().coordinator().run("run_100000_nnnnnnnnnnnn", "passed", pr=None,
                                          next_action=advice)
    checks, text, step = box.report()
    assert _by_name(checks, "Build").state == progress.TODO
    assert step.name == "Build" and advice in " ".join(text.split())


def test_a_lab3_chat_build_is_never_told_to_rerun_lab2(box):
    box.studio = "active"
    box.listening.add(8000)
    (box.lab1().github().coordinator().run("run_100000_lab2aaaaaaaa", "passed")
        .checkout("game")
        .run("run_100000_lab3aaaaaaaa", "needs_human", submitted_by="u@example.com",
             fail_reason="ROLE_PR_BLOCKED:w1", pr_state="blocked", pr_error="GATE_RED"))
    _checks, text, step = box.report()
    flat = " ".join(text.split())
    assert step.name == "Chat build"
    assert "Run a Build step 1" not in flat and "Next action in Agent Studio" in flat


def test_a_token_pasted_into_github_repo_is_never_printed(box, monkeypatch):
    token = "ghp_" + "Z" * 36
    monkeypatch.setenv("GITHUB_REPO", token)
    box.lab1()
    checks, text, _step = box.report()
    assert token not in text
    assert token not in json.dumps([vars(c) for c in checks])
    assert "has no '/'" in _by_name(checks, "App repository").detail


def test_a_run_id_is_not_masked_like_an_account_id():
    assert progress._mask("run_110000_111111111111") == "run_110000_111111111111"
    assert progress._mask("arn:aws:iam::123456789012:role/x") == "arn:aws:iam::1234****9012:role/x"


def test_a_running_build_points_at_the_watcher_not_a_resubmit(box):
    box.lab1().github().coordinator().run("run_100000_cccccccccccc", "running", pr=None, phase="build")
    _checks, text, step = box.report()
    assert step.page == "Lab 2 > Run a Build and Follow It > 2. Watch the agents work"
    assert "python3 orchestrator/watch_run.py --plain run_100000_cccccccccccc" in text
    assert "Do not resubmit" in text


def test_a_build_whose_heartbeat_stopped_is_reported_interrupted(box):
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    box.lab1().github().coordinator().run("run_100000_dddddddddddd", "running", pr=None, saved_at=old)
    checks, text, _step = box.report()
    assert "COORDINATOR_SESSION_INTERRUPTED" in _by_name(checks, "Build").detail
    assert "watch_run.py" not in text


def test_a_readme_only_checkout_means_the_pr_was_not_merged(box):
    box.lab1().github().coordinator().run("run_100000_eeeeeeeeeeee", "passed").checkout("game", app=False)
    _checks, text, step = box.report()
    assert step.name == "Merged game on the host"
    assert "only the repository's starter files" in step.detail
    assert "Merge the pull request on GitHub" in text


def test_lab2_and_lab3_builds_are_told_apart_by_the_recorded_submitter(box):
    (box.lab1().github().coordinator()
        .run("run_100000_ffffffffffff", "passed").checkout("game"))
    box.listening.add(8000)
    box.studio = "active"
    box.run("run_110000_111111111111", "running", pr=None, submitted_by="attendee@workshop.aws")
    checks, text, step = box.report()
    assert _by_name(checks, "Build").state == progress.DONE
    assert "run_110000" not in _by_name(checks, "Build").detail
    assert step.name == "Chat build"
    assert step.page == "Lab 3 > Improve Your Game in Agent Studio > 4. Follow the check and review"
    assert "watch_run.py --plain run_110000_111111111111" in text


def test_everything_done_points_at_the_pages_this_host_cannot_check(box):
    (box.lab1().github().coordinator().run("run_100000_gggggggggggg", "passed")
        .checkout("game").checkout("game-lab3"))
    box.listening.update({8000, 8001})
    box.studio = "active"
    _checks, text, step = box.report()
    assert step is None
    assert "All the checkpoints this host can verify are done." in text
    assert "Lab 3 > Trace the Fix" in text


def test_a_check_that_raises_is_unknown_and_never_hides_the_rest(box, monkeypatch):
    def broken():
        raise RuntimeError("gateway state unreadable for 123456789012")
    monkeypatch.setattr(progress, "check_gateway", broken)
    checks, _text, _step = box.report()
    gateway = _by_name(checks, "GitHub Gateway")
    assert gateway.state == progress.UNKNOWN
    assert gateway.detail.startswith("could not check: RuntimeError")
    assert "123456789012" not in gateway.detail and "1234****9012" in gateway.detail
    assert _by_name(checks, "Coordinator").state == progress.TODO


def test_a_slow_check_times_out_instead_of_hanging(box, monkeypatch):
    monkeypatch.setattr(progress, "check_note", lambda: time.sleep(3) or {})
    started = time.monotonic()
    checks = progress.collect(budget_s=0.5)
    assert time.monotonic() - started < 2.5
    assert _by_name(checks, "Shared note").state == progress.UNKNOWN
    assert "timed out" in _by_name(checks, "Shared note").detail


def test_json_output_is_valid_and_carries_next(box, capsys):
    box.storage()
    assert progress.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["next"]["name"] == "Codex Runtime"
    assert {c["name"] for c in data["checks"]} >= {"Shared storage", "Build", "Agent Studio"}


def test_no_secret_or_full_account_id_ever_reaches_the_output(box, capsys, monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "ksk_SECRETKEYVALUE")
    box.lab1().github().coordinator().run("run_100000_hhhhhhhhhhhh", "passed")
    progress.main([])
    text = capsys.readouterr().out
    progress.main(["--json"])
    text += capsys.readouterr().out
    for secret in ("SECRETAPPID", "SECRETPEM", "ksk_SECRET", "Q9Z7", "123456789012"):
        assert secret not in text, secret
    assert "1234****9012" in text


def test_the_roster_decides_which_checks_exist(box, monkeypatch):
    monkeypatch.setenv("WORKSHOP_ROLES", "claude-code,claude-code-validator")
    box.storage()
    checks, _text, _step = box.report()
    names = [c.name for c in checks]
    assert "Kiro API key" not in names and "Codex Runtime" not in names
    assert "Claude Code Runtime (yours)" in names
    steering = _by_name(checks, "Steering on the mount")
    # Claude Code roles read their baked CLAUDE.md, so only the backend skill is staged.
    assert steering.detail == "missing on the mount: skills/backend-engineering"


def test_run_readonly_refuses_anything_but_its_allowlist():
    with pytest.raises(PermissionError):
        progress._run_readonly(["systemctl", "start", "stage2-console"])
    with pytest.raises(PermissionError):
        progress._run_readonly(["sudo", "systemctl", "is-active", "x"])


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
                 "save", "save_check", "prune", "save_settings", "save_api_key", "clear_api_key",
                 "_write_sidecar", "write_state", "write_text", "write_bytes", "makedirs", "mkdir",
                 "rmtree", "remove", "unlink", "rename", "Popen", "system", "doctor", "publish",
                 "unpublish", "checkout", "_gateway_rpc", "_provision_vault"}
    assert not names & forbidden, sorted(names & forbidden)
    for prefix in ("create_", "update_", "delete_", "put_", "start_", "stop_"):
        assert not [n for n in names if n.startswith(prefix)], prefix
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [k.value.value for k in node.keywords
                      if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            assert not any(set(str(m)) & set("wax+") for m in modes), "progress.py must not write files"
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "run"
            and getattr(n.value, "id", "") == "subprocess"]
    assert len(runs) == 1, "every subprocess must go through _run_readonly"
