"""Tests for the wirable AgentCore runtime config (runtime_config.py).

The point: runtime ARNs are SET (env or the Settings/terminal file), never
hardcoded. We verify the env→file ladder, shape validation, and that the
AgentCoreExecutor reads what was wired.
"""

from __future__ import annotations

import json
import os

import pytest

import runtime_config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the config at a temp file via the REAL env var the module reads
    (WORKSHOP_RUNTIME_CONFIG, no patching of module internals) and clear the
    role env vars so each test starts from a clean, unwired slate."""
    monkeypatch.setenv("WORKSHOP_RUNTIME_CONFIG", str(tmp_path / "runtime.local.json"))
    for role in runtime_config.ROLES:
        monkeypatch.delenv(runtime_config._env_key(role), raising=False)
    monkeypatch.delenv("WORKSHOP_EXECUTOR", raising=False)
    # Point the deployed-ARN auto-discovery at an EMPTY temp coding-agents dir via
    # the real env var, so a role is unwired unless a test wires it. Without this
    # the ladder's third source would read the developer's own deployed
    # runtime_config.json files and a "clean slate" test would see them wired.
    monkeypatch.setenv("WORKSHOP_CODING_AGENTS_DIR", str(tmp_path / "coding-agents"))
    # The round-robin cursor is module-global; reset it so pick() starts at the
    # first instance in every test (deterministic round-robin assertions).
    runtime_config._RR_CURSOR.clear()


def test_unset_role_resolves_to_none():
    assert runtime_config.resolve("claude-code") is None


def test_save_then_resolve_from_settings():
    arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc-abc"
    out = runtime_config.save_runtime("claude-code", arn)
    assert "error" not in out
    hit = runtime_config.resolve("claude-code")
    assert hit == (arn, "settings")


def test_env_wins_over_file(monkeypatch):
    runtime_config.save_runtime("kiro", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/file")
    monkeypatch.setenv(runtime_config._env_key("kiro"),
                       "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/env")
    hit = runtime_config.resolve("kiro")
    assert hit[1] == "environment" and hit[0].endswith("runtime/env")


def test_bare_runtime_id_is_accepted():
    out = runtime_config.save_runtime("opencode", "opencode-runtime-7f3a9")
    assert "error" not in out
    assert runtime_config.resolve("opencode")[0] == "opencode-runtime-7f3a9"


def test_junk_arn_is_rejected():
    out = runtime_config.save_runtime("claude-code", "not a valid !!! arn @@@")
    assert "error" in out
    assert runtime_config.resolve("claude-code") is None


def test_unknown_role_is_rejected():
    out = runtime_config.save_runtime("frontend", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/x")
    assert "error" in out and "unknown role" in out["error"]


def test_resolve_map_collects_wired_roles():
    runtime_config.save_runtime("claude-code", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc")
    runtime_config.save_runtime("opencode", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cx")
    m = runtime_config.resolve_map()
    assert set(m) == {"claude-code", "opencode"}


def test_clear_one_role():
    runtime_config.save_runtime("claude-code", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc")
    runtime_config.save_runtime("kiro", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/k")
    runtime_config.clear_runtime("claude-code")
    assert runtime_config.resolve("claude-code") is None
    assert runtime_config.resolve("kiro") is not None  # untouched


def test_clear_all():
    runtime_config.save_runtime("claude-code", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc")
    runtime_config.clear_runtime()
    assert runtime_config.resolve_map() == {}


def test_status_shape_and_executor(monkeypatch):
    # Real-only: the shipped executor defaults to agentcore (no local executor). With
    # WORKSHOP_EXECUTOR unset, status() reports agentcore + remote_dispatch True.
    monkeypatch.delenv("WORKSHOP_EXECUTOR", raising=False)
    runtime_config.save_runtime("orchestrator", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/o")
    st = runtime_config.status()
    assert st["executor"] == "agentcore" and st["remote_dispatch"] is True
    orch = next(r for r in st["roles"] if r["role"] == "orchestrator")
    assert orch["wired"] is True and orch["source"] == "settings"


def test_settings_file_is_owner_only():
    runtime_config.save_runtime("claude-code", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc")
    mode = os.stat(runtime_config._settings_path()).st_mode & 0o777
    assert mode == 0o600


def test_executor_reads_wired_arn(monkeypatch):
    """The AgentCoreExecutor resolves a role's ARN from runtime_config (not hardcoded)."""
    import executor
    runtime_config.save_runtime("claude-code", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc")
    ex = executor.AgentCoreExecutor(runtime_arns={})  # no explicit mapping
    assert ex.runtime_arn("claude-code") == "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/cc"
    assert ex.runtime_arn("kiro") is None  # unwired


# ------------------------------------------------------------------ fleet (#76)
def _arn(tag: str) -> str:
    return f"arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/{tag}"


def test_add_runtime_grows_a_fleet():
    """'3 types' is not 3 instances: a role can hold a FLEET. add_runtime keeps the
    existing instances rather than replacing (unlike save_runtime)."""
    runtime_config.add_runtime("opencode", _arn("cx-1"))
    runtime_config.add_runtime("opencode", _arn("cx-2"))
    runtime_config.add_runtime("opencode", _arn("cx-3"))
    arns = [a for a, _ in runtime_config.instances("opencode")]
    assert arns == [_arn("cx-1"), _arn("cx-2"), _arn("cx-3")]


def test_add_runtime_dedups():
    runtime_config.add_runtime("opencode", _arn("cx-1"))
    runtime_config.add_runtime("opencode", _arn("cx-1"))  # same ARN twice -> no-op
    assert len(runtime_config.instances("opencode")) == 1


def test_save_runtime_replaces_the_fleet():
    """save_runtime SETs a role to a single instance, dropping any prior fleet."""
    runtime_config.add_runtime("opencode", _arn("cx-1"))
    runtime_config.add_runtime("opencode", _arn("cx-2"))
    runtime_config.save_runtime("opencode", _arn("cx-only"))
    assert [a for a, _ in runtime_config.instances("opencode")] == [_arn("cx-only")]


def test_pick_round_robins_across_the_fleet():
    """pick() spreads dispatch across a role's instances so concurrent runs don't
    all hit the same runtime; resolve() still returns the first (single answer)."""
    for tag in ("cx-1", "cx-2", "cx-3"):
        runtime_config.add_runtime("opencode", _arn(tag))
    picks = [runtime_config.pick("opencode")[0] for _ in range(7)]
    # 7 picks over 3 instances: each instance used, and it cycles in order.
    assert picks[:3] == [_arn("cx-1"), _arn("cx-2"), _arn("cx-3")]
    assert picks[3] == _arn("cx-1")  # wrapped
    assert set(picks) == {_arn("cx-1"), _arn("cx-2"), _arn("cx-3")}
    # resolve stays the single-instance (first) answer for presence/cost views.
    assert runtime_config.resolve("opencode")[0] == _arn("cx-1")


def test_pick_singleton_always_returns_the_one():
    runtime_config.save_runtime("claude-code", _arn("cc"))
    assert {runtime_config.pick("claude-code")[0] for _ in range(5)} == {_arn("cc")}


def test_pick_unwired_is_none():
    assert runtime_config.pick("kiro") is None


def test_env_carries_a_comma_separated_fleet(monkeypatch):
    """A role's env var may wire a whole fleet as a comma-separated list; env wins
    over the file (whole override, never half-merged)."""
    runtime_config.add_runtime("opencode", _arn("file-cx"))
    monkeypatch.setenv(runtime_config._env_key("opencode"),
                       f"{_arn('env-1')},{_arn('env-2')}")
    arns = [a for a, src in runtime_config.instances("opencode")]
    srcs = {src for _, src in runtime_config.instances("opencode")}
    assert arns == [_arn("env-1"), _arn("env-2")]
    assert srcs == {"environment"}  # file fleet ignored while env is set


def test_fleet_map_and_resolve_map():
    runtime_config.save_runtime("claude-code", _arn("cc"))
    for tag in ("cx-1", "cx-2"):
        runtime_config.add_runtime("opencode", _arn(tag))
    # resolve_map is the back-compat single-ARN map (first instance per role).
    rm = runtime_config.resolve_map()
    assert rm["opencode"] == _arn("cx-1") and rm["claude-code"] == _arn("cc")
    # fleet_map is the full per-role list.
    fm = runtime_config.fleet_map()
    assert fm["opencode"] == [_arn("cx-1"), _arn("cx-2")]
    assert fm["claude-code"] == [_arn("cc")]


def test_status_reports_fleet_count():
    for tag in ("cx-1", "cx-2"):
        runtime_config.add_runtime("opencode", _arn(tag))
    runtime_config.save_runtime("claude-code", _arn("cc"))
    st = runtime_config.status()
    cx = next(r for r in st["roles"] if r["role"] == "opencode")
    cc = next(r for r in st["roles"] if r["role"] == "claude-code")
    assert cx["count"] == 2 and len(cx["instances"]) == 2
    assert cx["wired"] is True and cx["arn"] == _arn("cx-1")  # first, back-compat
    assert cc["count"] == 1


def test_fleet_round_trips_through_the_file():
    """A multi-instance role persists as a JSON list and reloads as a fleet; a
    single-instance role persists as a bare string (back-compat shape)."""
    import json as _json
    for tag in ("cx-1", "cx-2"):
        runtime_config.add_runtime("opencode", _arn(tag))
    runtime_config.save_runtime("kiro", _arn("k"))
    with open(runtime_config._settings_path(), encoding="utf-8") as f:
        raw = _json.load(f)["runtimes"]
    assert isinstance(raw["opencode"], list) and len(raw["opencode"]) == 2
    assert isinstance(raw["kiro"], str)  # one instance -> bare string
    # reloads identically
    assert len(runtime_config.instances("opencode")) == 2
    assert len(runtime_config.instances("kiro")) == 1


# --- per-role descriptions (U17): what each agent does, read by the chatbot ---
# Descriptions are PER INSTANCE (keyed by ARN), set only on a wired instance.
def test_save_and_read_instance_description():
    runtime_config.save_runtime("claude-code", "claude_code-ID01")
    runtime_config.save_description("claude-code", "claude_code-ID01", "Builds the backend MCP server")
    assert runtime_config.describe_arn("claude_code-ID01") == "Builds the backend MCP server"
    # describe(role) surfaces the first instance's description
    assert runtime_config.describe("claude-code") == "Builds the backend MCP server"
    assert runtime_config.describe_map()["claude-code"] == "Builds the backend MCP server"


def test_each_instance_has_its_own_description():
    runtime_config.save_runtime("opencode", "opencode-ID01")
    runtime_config.add_runtime("opencode", "opencode-ID02")
    runtime_config.save_description("opencode", "opencode-ID01", "Frontend builder A")
    runtime_config.save_description("opencode", "opencode-ID02", "Frontend builder B")
    assert runtime_config.describe_arn("opencode-ID01") == "Frontend builder A"
    assert runtime_config.describe_arn("opencode-ID02") == "Frontend builder B"
    st = runtime_config.status()
    opencode = next(r for r in st["roles"] if r["role"] == "opencode")
    by_arn = {i["arn"]: i["description"] for i in opencode["instances"]}
    assert by_arn["opencode-ID01"] == "Frontend builder A"
    assert by_arn["opencode-ID02"] == "Frontend builder B"


def test_description_survives_other_instance_writes():
    runtime_config.save_runtime("opencode", "opencode-ID01")
    runtime_config.save_description("opencode", "opencode-ID01", "Builds the chatbot UI")
    runtime_config.add_runtime("opencode", "opencode-ID02")  # grow the fleet
    assert runtime_config.describe_arn("opencode-ID01") == "Builds the chatbot UI"


def test_empty_description_clears_one_instance():
    runtime_config.save_runtime("kiro", "kiro-ID01")
    runtime_config.save_description("kiro", "kiro-ID01", "Writes the gate")
    runtime_config.save_description("kiro", "kiro-ID01", "")
    assert runtime_config.describe_arn("kiro-ID01") == ""


def test_removing_instance_does_not_describe_unwired_arn():
    """A description only attaches to a wired instance ARN."""
    out = runtime_config.save_description("kiro", "kiro-NOTWIRED", "x")
    assert "error" in out


def test_unknown_role_description_rejected():
    out = runtime_config.save_description("nope", "arn-x", "x")
    assert "error" in out


def test_chat_agent_prompt_includes_wired_descriptions():
    """The orchestrator's system prompt must surface wired descriptions dynamically
    (no hardcoded blurb), so dispatch targets are described from Settings."""
    import chat
    runtime_config.save_runtime("claude-code", "claude_code-ID01")
    runtime_config.save_description("claude-code", "claude_code-ID01", "ZZZ-UNIQUE-BACKEND-MARKER")
    section = chat._dynamic_agent_section()
    assert "ZZZ-UNIQUE-BACKEND-MARKER" in section
    assert "dispatch_backend" in section


# ---- deployed-ARN auto-discovery (the event pre-provisions Codex/Kiro) --------
def _write_deployed(role: str, arn: str) -> None:
    """Write a harness runtime_config.json the way deploy.py does, under the
    isolated coding-agents dir the fixture points auto-discovery at."""
    ca = os.environ["WORKSHOP_CODING_AGENTS_DIR"]
    os.makedirs(os.path.join(ca, role), exist_ok=True)
    with open(os.path.join(ca, role, "runtime_config.json"), "w", encoding="utf-8") as f:
        json.dump({"runtime_arn": arn}, f)


def test_deployed_runtime_config_is_auto_discovered():
    """A pre-provisioned harness (its deploy.py wrote runtime_config.json) shows as
    wired with source 'deployed', with NO Settings entry: this is what surfaces the
    event's pre-deployed Codex/Kiro as already wired in the console."""
    _write_deployed("opencode", _arn("opencode-DEPLOYED"))
    hit = runtime_config.resolve("opencode")
    assert hit == (_arn("opencode-DEPLOYED"), "deployed")
    # a role with no file stays unwired
    assert runtime_config.resolve("kiro") is None


def test_settings_and_env_win_over_a_deployed_file():
    """The ladder is environment > settings > deployed: an attendee's explicit
    wiring always overrides the auto-discovered pre-deployment."""
    _write_deployed("opencode", _arn("opencode-DEPLOYED"))
    runtime_config.save_runtime("opencode", _arn("opencode-SETTINGS"))
    assert runtime_config.resolve("opencode") == (_arn("opencode-SETTINGS"), "settings")


def test_deployed_role_carries_a_default_description():
    """An auto-discovered instance is described by the role default (never blank),
    so the console card explains what the agent does without the attendee typing."""
    _write_deployed("kiro", _arn("kiro-DEPLOYED"))
    st = runtime_config.status()
    kiro = next(r for r in st["roles"] if r["role"] == "kiro")
    assert kiro["wired"] is True and kiro["source"] == "deployed"
    assert "Validator" in kiro["description"]


def test_a_malformed_deployed_file_is_ignored():
    """A missing runtime_arn (or junk) in the harness file leaves the role unwired,
    never a crash: 'not deployed yet' reads as unwired."""
    ca = os.environ["WORKSHOP_CODING_AGENTS_DIR"]
    os.makedirs(os.path.join(ca, "opencode"), exist_ok=True)
    with open(os.path.join(ca, "opencode", "runtime_config.json"), "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert runtime_config.resolve("opencode") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
