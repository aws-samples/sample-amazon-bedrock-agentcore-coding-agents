"""Harness-steering tests: the three role files turned green as you write them.

Stage 2 has attendees fill in three steering files, one per role, each with a
fenced ``harness:<kind>`` block the engine reads to compose deterministically:

  * page 2  claude-code/CLAUDE.md          ``harness:build``  (server_name/version/expose)
  * page 3  opencode/AGENTS.md                ``harness:ui``     (title/tool/.../examples)
  * page 4  kiro/.kiro/steering/validator  ``harness:gate``   (contract/checks/max_iterations)

This is the red→green checkpoint for those three writes, and the answer-key guard
that the content's blocks match what the engine parses. It needs no model and no
server: each case reads the file the attendee edits and asserts the parsed spec.

    python3 -m pytest orchestrator/test_builders.py -v

The stub files ship with empty/TODO blocks, so before you fill them in these cases
fail (the parser falls back to defaults and the raw block carries no keys). When the
three blocks match the values the pages give you, every case is green.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import builders  # noqa: E402
from builders import (  # noqa: E402
    _fenced_block,
    _read,
    harness_file,
    parse_build_spec,
    parse_gate_spec,
    parse_ui_spec,
)


def _active_keys(block: str) -> set[str]:
    """The keys present on real (non-comment) lines of a fenced block.

    The stub ships its keys only as ``#`` comment placeholders, so a plain
    substring check would pass an unfilled file. A key counts as written only
    when it appears on a line the parser actually reads (comments are ignored),
    which is exactly the line the attendee adds when they fill the block in.
    """
    keys: set[str] = set()
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        keys.add(line.split(":", 1)[0].strip())
    return keys


# ------------------------------------------------------ page 2: the backend build spec
def test_backend_build_block_is_filled_in():
    """The ``harness:build`` block names the three keys (not the TODO placeholder)."""
    keys = _active_keys(_fenced_block(_read(harness_file("claude-code")), "harness:build"))
    assert {"server_name", "server_version", "expose"} <= keys


def test_backend_build_spec_parses_to_the_workshop_values():
    # The parser's safe defaults happen to equal the workshop values, so first
    # require the keys to be actually written (not commented); otherwise an
    # unfilled file would pass on the defaults alone and the checkpoint would be
    # green before you build anything.
    keys = _active_keys(_fenced_block(_read(harness_file("claude-code")), "harness:build"))
    assert {"server_name", "server_version", "expose"} <= keys
    spec = parse_build_spec()
    assert spec["server_name"] == "cost-analyzer-mcp"
    assert spec["server_version"] == "1.0.0"
    # The acceptance gate requires all five tools, so the backend exposes them all.
    assert spec["expose"] == "all"


# ------------------------------------------------------ page 3: the frontend UI spec
def test_frontend_ui_block_is_filled_in():
    keys = _active_keys(_fenced_block(_read(harness_file("opencode")), "harness:ui"))
    assert {"title", "tool", "input_field", "examples"} <= keys


def test_frontend_ui_spec_parses_to_the_workshop_values():
    spec = parse_ui_spec()
    assert spec["title"] == "Cost Analyzer Chat"
    assert spec["tool"] == "estimate_ec2_monthly_cost"
    assert spec["input_field"] == "instance_type"
    # The example chips are the steering seam most visibly your own; the stub
    # ships none, so an unfilled file leaves this empty and the case fails.
    assert spec["examples"] == ["m5.large", "t3.micro", "r5.xlarge"]


# ------------------------------------------------------ page 4: the validator gate spec
def test_validator_gate_block_is_filled_in():
    keys = _active_keys(_fenced_block(_read(harness_file("kiro")), "harness:gate"))
    assert {"contract", "checks", "max_iterations"} <= keys


def test_validator_gate_spec_parses_to_the_three_checks():
    spec = parse_gate_spec()
    assert spec["contract"] == "usecase-sample-to-mcp/grading/"
    # The three deterministic checks the contract defines as "done". The stub
    # ships an empty block, so checks == [] and this case fails until you write them.
    assert spec["checks"] == ["tool_discovery", "tool_correctness", "input_validation"]
    assert spec["max_iterations"] == 2


# --------------------------------------------- project-scale deliverable builders
def test_build_smoke_test_actually_boots_the_generated_server(tmp_path):
    """The offline smoke-test stand-in is a real runnable proof: generate the
    server + the smoke test against the live cost_analyzer, run it, expect SMOKE OK.
    This is what the validator's project_smoke_runs check executes."""
    import subprocess
    usecase_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "..", "usecase-sample-to-mcp"))
    builders.build_mcp_server(str(tmp_path), usecase_dir, module_name="cost_analyzer")
    smoke = builders.build_smoke_test(str(tmp_path), usecase_subdir="usecase-sample-to-mcp",
                                      module_name="cost_analyzer")
    env = dict(os.environ, COST_ANALYZER_DIR=usecase_dir)
    r = subprocess.run([sys.executable, smoke], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "SMOKE OK" in r.stdout


def test_build_readme_names_the_run_and_run_command(tmp_path):
    """The README is generated from the run's own facts (never hardcoded) and tells
    a cloner how to run the project."""
    out = builders.build_readme(str(tmp_path), "run_x_007", "convert the module",
                                "convert/sample-to-mcp-v1", "cost_analyzer",
                                ["claude-code", "opencode"], has_frontend=True,
                                gate_line="10 passed in 0.04s")
    body = open(out, encoding="utf-8").read()
    assert "run_x_007" in body
    assert "python deliverable/smoke_test.py" in body
    assert "chatbot.html" in body           # frontend row present when has_frontend
    assert "10 passed in 0.04s" in body
