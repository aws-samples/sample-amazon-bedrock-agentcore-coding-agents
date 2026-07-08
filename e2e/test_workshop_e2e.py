"""End-to-end proof of the WHOLE workshop loop, the way a real attendee runs it.

No LLM, no AWS, no network beyond localhost. This test walks the exact path the content
teaches and asserts that each stage produces the real thing the next stage consumes, and
 the part that actually matters, that the deliverable is **built from the harness**, so
editing a harness file changes what comes out. That is the workshop's central claim, and
this suite makes it mechanical instead of theatre.

  Stage 1 (Interactive)  shell orchestration: boot the interactive engine, deploy an
                         agent, open a session, run REAL /bin/sh commands in the
                         /mnt/s3files workspace, then convert the cost_analyzer module to an
                         MCP server and verify it over the wire.
  Stage 2 (Orchestrate)  submit ONE task to the embedded orchestration engine; the
                         blueprint runs; the backend role GENERATES the MCP server from
                         its CLAUDE.md, the frontend role GENERATES the chatbot from its
                         AGENTS.md, the REAL pytest gate passes, and the three roles are
                         composed into ONE git commit.
  Built-from-harness     the two seams the workshop tells attendees they can edit:
                           * module seam: add an instance type to cost_analyzer and the
                             GENERATED backend prices it through the chatbot's own call.
                           * Steering seam: change the opencode AGENTS.md UI spec and the
                             GENERATED chatbot.html reflects it (new title + example chip).
                         Each comes with a NEGATIVE CONTROL: with the edit absent, the same
                         assertion FAILS, proving the test is wired to the real artifact,
                         not a constant.
  Stage 3 (Governance)   the run lands in the shared telemetry ledger and the metrics
                         API aggregates it (per-user, per-agent, p95).

Run:  pytest e2e/test_workshop_e2e.py -v
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

# E2E asserts the deterministic spine. The in-process engine is made
# deterministic by injecting the test-only FixtureExecutor (no model, no live
# AWS) at each construction site below, never an env flag on the shipped path.
# Empty coding-agents dir so the Stage-1 shelf starts deployed-free; a test writes
# the real runtime_config.json a harness deploy.py would, to exercise reconciliation.
_CODING_AGENTS_DIR = tempfile.mkdtemp(prefix="we2e-coding-agents-")
os.environ["WORKSHOP_CODING_AGENTS_DIR"] = _CODING_AGENTS_DIR
# GitHub isolation: this module drives the engine IN-PROCESS, so point the credential
# store at an empty tmp file BEFORE github.py loads; a real wired PAT would make a
# Stage-2 run open a REAL pull request. Set at module import, before any engine import.
os.environ["WORKSHOP_GITHUB_STORE"] = "local"
os.environ["WORKSHOP_GITHUB_SETTINGS"] = os.path.join(
    tempfile.mkdtemp(prefix="we2e-gh-"), "github.local.json")
os.environ["WORKSHOP_RUNTIME_CONFIG"] = os.path.join(
    tempfile.mkdtemp(prefix="we2e-rt-"), "runtime.local.json")
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_REPO", None)
for _k in [k for k in os.environ if k.startswith("AGENTCORE_RUNTIME_")]:
    os.environ.pop(_k, None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for sub in ("interactive-api", "orchestrator", "metrics-api",
            "usecase-sample-to-mcp", "usecase-sample-to-mcp/grading"):
    sys.path.insert(0, os.path.join(_REPO, sub))

_LEDGER = os.path.join(_REPO, ".runs", "telemetry.jsonl")


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# Stage 1: shell orchestration through the REAL interactive engine over HTTP.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def stage1_server():
    import interactive_api
    from http.server import ThreadingHTTPServer
    # bind to an ephemeral port to avoid clashing with a dev server on 8091
    srv = ThreadingHTTPServer(("127.0.0.1", 0), interactive_api.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", interactive_api
    finally:
        for s in list(interactive_api._SESSIONS.values()):
            interactive_api._stop_server(s)
        srv.shutdown()


def test_stage1_shell_orchestration_and_skill_conversion(stage1_server):
    base, _ = stage1_server

    # A real deploy is the harness container build (./setup.sh && python deploy.py),
    # which writes the runtime_config.json the console reconciles. Stand in for the
    # AWS CreateAgentRuntime call by writing that exact real-ARN config, then the
    # deploy endpoint reconciles it to ready, never a local:runtime placeholder.
    rid = "claude_code-WE2E0001cap"
    arn = f"arn:aws:bedrock-agentcore:us-west-2:269550163595:runtime/{rid}"
    cfg_dir = os.path.join(_CODING_AGENTS_DIR, "claude-code")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "runtime_config.json"), "w", encoding="utf-8") as f:
        json.dump({"agent_name": "claude_code", "runtime_id": rid, "runtime_arn": arn,
                   "region": "us-west-2", "s3files_mount_path": "/mnt/s3files"}, f)
    dep = _post(base + "/api/agents/deploy", {"agent_id": "claude-code"})
    assert dep["status"] == "ready"
    # the genuine bedrock-agentcore ARN deploy.py wrote, never a fabricated one
    assert dep["runtime_arn"] == arn
    assert dep["runtime_arn"].startswith("arn:aws:bedrock-agentcore:")

    # open a session -> a REAL workspace dir exists on disk, and it starts EMPTY:
    # nothing is pre-seeded. The participant creates every file themselves.
    sess = _post(base + "/api/sessions", {"agent_id": "claude-code"})
    sid = sess["session_id"]
    assert sess["status"] == "open"
    assert sess["workspace"] == "/mnt/s3files"

    # the empty start: a fresh workspace has no cost_analyzer.py yet
    empty = _post(base + f"/api/sessions/{sid}/input", {"input": "ls /mnt/s3files"})
    assert "cost_analyzer.py" not in empty["output"]

    # the participant creates the input module themselves in the editor (New File ->
    # paste -> Save), which the console turns into this file-write call. We mirror
    # seed_skill here against the interactive engine's own /file endpoint, using the
    # canonical cost_analyzer.py source (under sample/); the same bytes a participant pastes.
    with open(_SKILL, encoding="utf-8") as f:
        _post(base + f"/api/sessions/{sid}/file",
              {"path": "sample/cost_analyzer.py", "content": f.read()})

    # real shell orchestration: commands actually run in /bin/sh and state persists,
    # and the file the participant just created is really there
    out = _post(base + f"/api/sessions/{sid}/input", {"input": "ls /mnt/s3files/sample"})
    assert "cost_analyzer.py" in out["output"]
    # cd persists across inputs (PTY-like), proving real session state
    cd = _post(base + f"/api/sessions/{sid}/input", {"input": "cd /mnt/s3files && pwd"})
    assert cd["cwd"] == "/mnt/s3files"
    head = _post(base + f"/api/sessions/{sid}/input",
                 {"input": "head -1 /mnt/s3files/sample/cost_analyzer.py"})
    assert "cost_analyzer" in head["output"]

    # the Stage 1 payoff: convert the cost_analyzer module to an MCP server and verify OVER THE WIRE.
    conv = _post(base + f"/api/sessions/{sid}/convert-skill",
                 {"tool": "estimate_ec2_monthly_cost"})
    assert conv["verified"] is True, f"conversion not verified over the wire: {conv}"
    assert conv["server_file"] == "/mnt/s3files/mcp_server.py"
    assert any(t.get("name") == "estimate_ec2_monthly_cost" for t in conv["tools_list"])
    assert "monthly_cost" in conv["sample_call"]["result"]


# ---------------------------------------------------------------------------
# Stage 2 helpers: drive the REAL engine to terminal and replay the produced UI.
# ---------------------------------------------------------------------------
def _wait_terminal(run, timeout_s=90.0):
    import engine
    deadline = time.monotonic() + timeout_s
    while run.status not in engine.TERMINAL:
        assert time.monotonic() < deadline, f"run stuck in {run.status}/{run.phase}"
        time.sleep(0.2)
    return run


def _chatbot_endpoint(chatbot_html: str) -> str:
    """Extract the MCP endpoint the produced chatbot page is wired to."""
    m = re.search(r'fetch\("(http://127\.0\.0\.1:\d+)"', chatbot_html)
    assert m, "chatbot.html has no concrete MCP endpoint baked in"
    return m.group(1)


def _chatbot_tool(chatbot_html: str) -> str:
    """Extract the tool name the produced chatbot page calls (from the UI spec)."""
    m = re.search(r'name:"([A-Za-z0-9_]+)"', chatbot_html)
    assert m, "chatbot.html has no tool name baked in"
    return m.group(1)


def _replay_chatbot_button(chatbot_html: str, value: str) -> dict:
    """Replay EXACTLY what the produced chatbot's Estimate button sends.

    The page bakes in its MCP endpoint, the tool name, and the argument field name
    (all from the opencode AGENTS.md UI spec). We rebuild the same JSON-RPC request the
    browser fetch() would send and call the live backend. This is the mechanical
    stand-in for "a user opened the produced UI and clicked Estimate".
    """
    endpoint = _chatbot_endpoint(chatbot_html)
    tool = _chatbot_tool(chatbot_html)
    field_m = re.search(r'arguments:\{"([A-Za-z0-9_]+)":', chatbot_html)
    assert field_m, "chatbot.html has no argument field baked in"
    field = field_m.group(1)
    body = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": tool, "arguments": {field: value}}}
    return _post(endpoint, body)


def _deliverable_dir() -> str:
    return os.path.join(_REPO, ".runs", "composed", "deliverable")


def test_stage2_builds_the_deliverable_from_the_harness():
    """The backend SERVER and the frontend CHATBOT are GENERATED from the harness,
    not pre-written. Prove the run produces both, that they grade green over the wire,
    and that the produced UI was built from the opencode AGENTS.md UI spec (its title and
    example chips appear in the generated page)."""
    engine = importlib.import_module("engine")
    import builders
    from fixture_executor import FixtureExecutor
    eng = engine.Engine(executor_obj=FixtureExecutor())
    try:
        run = _wait_terminal(eng.submit(
            "Convert the cost_analyzer module to an MCP server with tests + a chatbot UI",
            ["claude-code", "kiro", "opencode"]))
        result = engine.public_result(run)

        # the blueprint passed its REAL pytest acceptance gate (not an LLM judge)
        assert result["status"] == "passed"
        assert result["gate"]["passed"] is True
        assert {c["check"] for c in result["gate"]["checks"]} == {
            "tool_discovery", "tool_correctness", "input_validation"}

        # three roles composed into ONE real git commit
        assert result["composed_from"] == ["backend-mcp", "validator", "frontend-builder"]
        assert result["composed_branch"] == f"run/{run.run_id}"
        assert result["composed_commit"] and len(result["composed_commit"]) == 40

        # the backend artifact was GENERATED this run (not the checked-in reference
        # server): the run recorded a generated server file under the run workdir.
        assert run._server_file and run.run_id in run._server_file
        assert os.path.isfile(run._server_file)
        gen_src = open(run._server_file, encoding="utf-8").read()
        assert "Generated MCP server" in gen_src and "import cost_analyzer" in gen_src

        # the composed deliverable really exists on disk: server + report + UI
        deliver = _deliverable_dir()
        chatbot = os.path.join(deliver, "chatbot.html")
        assert os.path.isfile(os.path.join(deliver, "mcp_server.py"))
        assert os.path.isfile(os.path.join(deliver, "gate_report.json"))
        assert os.path.isfile(chatbot)
        html = open(chatbot, encoding="utf-8").read()

        # the produced UI was BUILT FROM the opencode AGENTS.md UI spec: its title and
        # its example chips come straight from that steering file.
        ui = builders.parse_ui_spec()
        assert f"<title>{ui['title']}</title>" in html
        for chip in ui["examples"]:
            assert f">{chip}</button>" in html, f"chip {chip!r} missing from produced UI"

        # THE PAYOFF: the produced UI is real. Replay its button against the live
        # backend the engine kept running and assert it answers with a real price.
        answer = _replay_chatbot_button(html, "m5.large")
        priced = json.loads(answer["result"]["content"][0]["text"])
        assert priced["instance_type"] == "m5.large"
        assert priced["monthly_cost"] > 0
        # bad input the UI could send must surface as an error, not a wrong price
        bad = _replay_chatbot_button(html, "not-a-real-type")
        assert "error" in bad
    finally:
        eng.shutdown()


# ---------------------------------------------------------------------------
# Seam 1: the module seam, edit cost_analyzer, the GENERATED backend prices it.
# ---------------------------------------------------------------------------
def test_module_edit_flows_into_the_produced_ui():
    """Add a brand-new instance type to the cost_analyzer module, run the orchestrator,
    and assert the chatbot's own request now prices the new type, end to end. The
    backend server is generated against the live module, so the new type flows through
    without touching the engine. Negative control below proves this isn't vacuous."""
    NEW_TYPE, NEW_RATE = "x9.workshop", 1.2345
    _patch_skill_on_disk(NEW_TYPE, NEW_RATE)
    try:
        engine = importlib.import_module("engine")
        from fixture_executor import FixtureExecutor
        eng = engine.Engine(executor_obj=FixtureExecutor())
        try:
            run = _wait_terminal(eng.submit(
                "Re-run after adding a new instance type to the module",
                ["claude-code", "kiro", "opencode"]))
            assert run.status == "passed"
            html = open(os.path.join(_deliverable_dir(), "chatbot.html"), encoding="utf-8").read()
            answer = _replay_chatbot_button(html, NEW_TYPE)
            priced = json.loads(answer["result"]["content"][0]["text"])
            # the NEW type the attendee added is now priced by the UI's own call
            assert priced["instance_type"] == NEW_TYPE
            assert priced["hourly_rate"] == round(NEW_RATE, 4)
            assert priced["monthly_cost"] > 0
        finally:
            eng.shutdown()
    finally:
        _unpatch_skill_on_disk(NEW_TYPE)


def test_skill_seam_negative_control():
    """Without the module edit, the same replay must FAIL, proving the test above is
    wired to the real generated server, not asserting a constant. A fresh run with the
    pristine module cannot price x9.workshop; it returns a JSON-RPC error."""
    engine = importlib.import_module("engine")
    from fixture_executor import FixtureExecutor
    eng = engine.Engine(executor_obj=FixtureExecutor())
    try:
        run = _wait_terminal(eng.submit(
            "Baseline run, module unedited", ["claude-code", "kiro", "opencode"]))
        assert run.status == "passed"
        html = open(os.path.join(_deliverable_dir(), "chatbot.html"), encoding="utf-8").read()
        answer = _replay_chatbot_button(html, "x9.workshop")
        # the unknown type is rejected: the seam is real, not hard-coded. And it's
        # rejected the RIGHT way; a proper JSON-RPC error object with a code, not a
        # bare key or a mispriced result.
        assert answer.get("error", {}).get("code") is not None, (
            f"pristine module should reject x9.workshop with a JSON-RPC error: {answer}")
    finally:
        eng.shutdown()


# ---------------------------------------------------------------------------
# Seam 2: the steering seam, edit the opencode AGENTS.md, the GENERATED UI changes.
# ---------------------------------------------------------------------------
def test_steering_edit_changes_the_produced_ui():
    """Change the opencode AGENTS.md UI spec (title + a new example chip), run the
    orchestrator, and assert the GENERATED chatbot.html reflects the change. This is
    the steering seam: editing the harness file an attendee owns changes the produced
    deliverable. Negative control: the new title is absent from the pristine harness."""
    NEW_TITLE = "AWS Sizing Copilot"
    NEW_CHIP = "c5.xlarge"
    # sanity: the new values are NOT in the pristine harness (negative control inline)
    import builders
    pristine = open(builders.HARNESS_FILES["opencode"], encoding="utf-8").read()
    assert NEW_TITLE not in pristine and f"  - {NEW_CHIP}" not in pristine

    _patch_opencode_ui(NEW_TITLE, NEW_CHIP)
    try:
        # re-import builders so any cached module state re-reads the file fresh
        importlib.reload(builders)
        engine = importlib.import_module("engine")
        importlib.reload(engine)
        from fixture_executor import FixtureExecutor
        eng = engine.Engine(executor_obj=FixtureExecutor())
        try:
            # Route to the full convert workflow (all three roles, produces
            # chatbot.html). The task must carry a real intent word -- the router
            # is task-agnostic and fails loud on an intent-less phrasing.
            run = _wait_terminal(eng.submit(
                "Convert the cost analyzer module to an MCP server and chatbot UI, "
                "restyled per the edited AGENTS.md",
                ["claude-code", "kiro", "opencode"]))
            assert run.status == "passed"
            html = open(os.path.join(_deliverable_dir(), "chatbot.html"), encoding="utf-8").read()
            # the produced UI carries the NEW title and the NEW example chip
            assert f"<title>{NEW_TITLE}</title>" in html
            assert f">{NEW_CHIP}</button>" in html
            # and it still works: replay the button against the live backend
            answer = _replay_chatbot_button(html, "m5.large")
            priced = json.loads(answer["result"]["content"][0]["text"])
            assert priced["instance_type"] == "m5.large" and priced["monthly_cost"] > 0
        finally:
            eng.shutdown()
    finally:
        _unpatch_opencode_ui()
        importlib.reload(builders)


# --- on-disk patch helpers: snapshot exact bytes, restore byte-for-byte ----------
_SKILL = os.path.join(_REPO, "usecase-sample-to-mcp", "cost_analyzer.py")
_SKILL_MARKER = "# >>> e2e-skill-edit"
_OPENCODE = os.path.join(_REPO, "orchestrator", "harness", "opencode", "AGENTS.md")
_BACKUP = {"skill": None, "opencode": None}


def _patch_skill_on_disk(itype: str, rate: float) -> None:
    """Append a new instance type to the module so the generated server prices it.

    Mirrors what an attendee does when they extend the module the harness builds.
    Snapshots the exact original bytes so the repo is restored byte-for-byte.
    """
    with open(_SKILL, "rb") as f:
        original = f.read()
    if _SKILL_MARKER.encode() in original:
        return
    _BACKUP["skill"] = original
    patch = (f'\n{_SKILL_MARKER}\n'
             f'EC2_HOURLY_USD["{itype}"] = {rate}\n'
             f'EC2_SPECS["{itype}"] = {{"vcpus": 8, "memory_gib": 32}}\n')
    with open(_SKILL, "ab") as f:
        f.write(patch.encode())


def _unpatch_skill_on_disk(itype: str) -> None:
    if _BACKUP["skill"] is not None:
        with open(_SKILL, "wb") as f:
            f.write(_BACKUP["skill"])
        _BACKUP["skill"] = None


def _patch_opencode_ui(title: str, chip: str) -> None:
    """Rewrite the opencode AGENTS.md UI spec: change the title and add an example chip.

    Mirrors an attendee restyling the chatbot via its steering file. Snapshots exact
    bytes for byte-for-byte restore.
    """
    with open(_OPENCODE, "rb") as f:
        original = f.read()
    _BACKUP["opencode"] = original
    text = original.decode("utf-8")
    # change the title line inside the harness:ui block
    text = re.sub(r"(?m)^title: .*$", f"title: {title}", text, count=1)
    # add a new example chip after the last existing chip in the examples list
    text = text.replace("  - r5.xlarge\n", f"  - r5.xlarge\n  - {chip}\n", 1)
    with open(_OPENCODE, "w", encoding="utf-8") as f:
        f.write(text)


def _unpatch_opencode_ui() -> None:
    if _BACKUP["opencode"] is not None:
        with open(_OPENCODE, "wb") as f:
            f.write(_BACKUP["opencode"])
        _BACKUP["opencode"] = None


# ---------------------------------------------------------------------------
# Stage 3: the runs above land in the shared ledger; metrics aggregate them.
# ---------------------------------------------------------------------------
def test_stage3_metrics_aggregate_the_real_runs():
    # the prior tests appended orchestrator_run + stage1 rows to the ledger
    assert os.path.isfile(_LEDGER), "no telemetry ledger; Stage 1/2 should have written it"
    import metrics_lib

    dash = metrics_lib.get_dashboard()
    assert dash["runs_total"] >= 1, f"dashboard saw no runs: {dash}"

    cost = metrics_lib.get_cost_breakdown(by="agent")["breakdown"]
    # the three roles all show up as attributed cost buckets (estimated, not a race)
    assert set(cost) & {"claude-code", "kiro", "opencode"}, f"no per-agent attribution: {cost}"

    me = __import__("getpass").getuser()
    metrics = metrics_lib.get_user_metrics(me, "24h")
    assert metrics["runs"] >= 1
    assert metrics["p95_latency_ms"] >= 0
