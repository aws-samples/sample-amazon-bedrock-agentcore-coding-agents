"""Tests for the agent guardrails (policy.py).

The point of these tests is that the policy is ENFORCED, not displayed: the same
screen() the engine calls at its command boundary must actually deny the dangerous
actions, and the list get_policies() shows must be the list screen() checks.
"""

from __future__ import annotations

import policy


# ----------------------------------------------------------------- hard denies
def test_rm_root_is_denied():
    d = policy.screen("run_command", "rm -rf /")
    assert not d.allowed and d.tier == "hard" and d.rule_id == "forbid_rm_root"


def test_rm_root_glob_is_denied():
    assert not policy.screen("run_code", "import os; os.system('rm -rf /*')").allowed


def test_write_under_git_is_denied():
    d = policy.screen("write_file", ".git/config")
    assert not d.allowed and d.rule_id == "forbid_write_git_internals"


def test_write_in_readonly_workflow_is_denied():
    d = policy.screen("write_file", "mcp_server.py", read_only=True)
    assert not d.allowed and d.rule_id == "forbid_write_in_readonly_workflow"


def test_readonly_still_allows_reads_implicitly():
    # read_file is not a write/run action, so the read-only rule does not touch it
    assert policy.screen("read_file", "anything.py", read_only=True).allowed


# ----------------------------------------------------------------- soft gates
def test_credential_write_is_gated():
    d = policy.screen("write_file", "config/.env")
    assert not d.allowed and d.tier == "soft" and d.gated
    assert d.rule_id == "gate_write_credentials"


def test_force_push_main_is_gated():
    d = policy.screen("run_command", "git push --force origin main")
    assert not d.allowed and d.tier == "soft" and d.rule_id == "gate_force_push_main"


# ----------------------------------------------------------------- allows
def test_ordinary_write_is_allowed():
    assert policy.screen("write_file", "mcp_server.py").allowed


def test_ordinary_code_is_allowed():
    assert policy.screen("run_code", "print(2 + 2)").allowed


def test_ordinary_rm_of_a_local_file_is_allowed():
    # removing a file inside the workspace is fine; only root/absolute is denied
    assert policy.screen("run_command", "rm build/tmp.txt").allowed


# ----------------------------------------------------------- displayed == enforced
def test_get_policies_lists_every_rule_screen_enforces():
    shown = {p["rule_id"] for p in policy.get_policies()["policies"]}
    enforced = {
        policy.screen("run_command", "rm -rf /").rule_id,
        policy.screen("write_file", ".git/x").rule_id,
        policy.screen("write_file", "x", read_only=True).rule_id,
        policy.screen("write_file", ".env").rule_id,
        policy.screen("run_command", "git push --force main").rule_id,
    }
    assert enforced.issubset(shown), f"enforced-but-not-shown: {enforced - shown}"
    assert policy.get_policies()["enforced"] is True


# --------------------------------------------------- enforced at the engine boundary
def test_engine_term_blocks_a_denied_command(tmp_path, monkeypatch):
    """The guardrail is real, not decorative: the engine screens every shell command
    a role runs (Run.term) and a hard-denied command is NOT executed; it is recorded
    as a POLICY_DENIED transcript line with the matched rule id and a non-zero exit."""
    monkeypatch.setenv("WORKSHOP_RUNS_DIR", str(tmp_path))
    import importlib

    import engine
    importlib.reload(engine)
    run = engine.Run(run_id="run_000000_001", task="t", agents=["claude-code"],
                     roles={"claude-code": "backend-mcp"})
    run._t0 = 0.0

    # A benign command runs and lands in the transcript with exit 0.
    out = run.term("claude-code", "echo hello")
    assert out.strip() == "hello"
    assert run.terminals["claude-code"][-1]["exit"] == 0

    # A hard-denied command is blocked BEFORE execution: a marker file the command
    # would create must never appear, and the transcript records the deny.
    sentinel = tmp_path / "should-not-exist"
    run.term("claude-code", f"rm -rf / ; touch {sentinel}")
    assert not sentinel.exists(), "denied command must not execute"
    blocked = run.terminals["claude-code"][-1]
    assert blocked["exit"] == 126
    assert "POLICY_DENIED" in blocked["output"]
    assert "forbid_rm_root" in blocked["output"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
