"""The reviewer reads with tools and ranks its findings (November 18, after Foxl's RV1).

    python3 -m pytest orchestrator/test_review_agent.py -q
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reviewer  # noqa: E402
import review_tools  # noqa: E402
from work_items import WorkItem  # noqa: E402

_GREEN = {"passed": True, "checks": [{"check": "authored", "passed": True, "detail": "green"}],
          "summary": "green"}


class _Run:
    def __init__(self, tree, item):
        self.task = "a game with a leaderboard"
        self.final_base_branch = "main"
        self.work_items = {item.agent: item}
        self._item_checks = {}
        self._tree = str(tree)

    def item_tree_dir(self, _work_id):
        return self._tree


def _pr(tmp_path):
    item = WorkItem.create("run_r", "codex", "frontend-builder", "frontend",
                           base_branch="main", token="r")
    tree = tmp_path / "tree"
    (tree / "public").mkdir(parents=True)
    (tree / "server").mkdir()
    (tree / "public" / "app.js").write_text("fetch('api/scores').then(r => r.json()).then(d => show(d.items));\n")
    (tree / "server" / "app.js").write_text("app.get('/api/scores', (q, s) => s.json({scores: top()}));\n")
    item.changed_files = ["public/app.js"]
    return _Run(tree, item), item, tree


def _verdict(item, **extra):
    body = {"approve": True, "reasons": [],
            "work_item_evidence": {item.work_id: "The page fetches api/scores."},
            "adversarial_assessment": "Traced the payload.", "design_assessment": "Inside its role."}
    body.update(extra)
    return body


def test_the_reviewer_reads_the_base_with_a_tool_then_submits(monkeypatch, tmp_path):
    import llm
    run, item, _tree = _pr(tmp_path)
    calls = []

    def invoke(_model, prompt, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert {t["toolSpec"]["name"] for t in kwargs["tools"]} == review_tools.TOOL_NAMES
            return {"text": "", "tool_input": None, "content": [{"toolUse": {
                        "toolUseId": "t1", "name": "read_file", "input": {"path": "server/app.js"}}}],
                    "tool_calls": [{"id": "t1", "name": "read_file", "input": {"path": "server/app.js"}}]}
        # The tool result reached the model as the next user message.
        last = kwargs["messages"][-1]["content"][0]["toolResult"]
        assert last["toolUseId"] == "t1" and "scores: top()" in last["content"][0]["text"]
        finding = {"priority": "P1", "title": "The page reads items, the server sends scores",
                   "place": "public/app.js:1", "failure": "The leaderboard is always empty.",
                   "fix": "Read d.scores."}
        submitted = _verdict(item, approve=False, reasons=["items vs scores"], findings=[finding])
        return {"text": json.dumps(submitted), "tool_input": submitted, "tool_calls": [
            {"id": "t2", "name": "submit_review", "input": submitted}], "content": []}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "invoke", invoke)
    verdict = reviewer.assess(run, _GREEN, 1, subject=item)
    assert len(calls) == 2
    assert not verdict.lgtm and not verdict.review_unavailable
    assert verdict.reasons and verdict.reasons[0].startswith("integrated: P1: The page reads items")
    assert "### Must fix" in verdict.assessment and "Read d.scores." in verdict.assessment
    # The base file was read with the tool, never pasted into the opening message.
    assert "scores: top()" not in calls[0]["messages"][0]["content"][0]["text"]


def _one_shot(monkeypatch, item, verdict_body):
    import llm

    def invoke(_model, _prompt, **_kwargs):
        return {"text": json.dumps(verdict_body), "tool_input": verdict_body, "tool_calls": []}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "invoke", invoke)


def test_minor_findings_are_posted_and_never_block(monkeypatch, tmp_path):
    run, item, _tree = _pr(tmp_path)
    findings = [{"priority": "P3", "title": "README names the wrong default port",
                 "place": "README.md:3", "failure": "It says 3000; the server uses 8080.",
                 "fix": "Say 8080."},
                {"priority": "P2", "title": "No test for an empty name", "failure": "Untested."}]
    # Even a model that also set approve=false is decided by the priorities.
    _one_shot(monkeypatch, item, _verdict(item, approve=False, reasons=["README port"],
                                          findings=findings))
    verdict = reviewer.assess(run, _GREEN, 1, subject=item)
    assert verdict.lgtm and verdict.state == "approved"
    assert "### Worth fixing (does not block the merge)" in verdict.assessment
    assert "README names the wrong default port" in verdict.assessment


def test_a_p0_or_p1_blocks_even_when_the_model_says_approve(monkeypatch, tmp_path):
    run, item, _tree = _pr(tmp_path)
    findings = [{"priority": "P0", "title": "Scores are lost on restart",
                 "failure": "The board is empty after a restart.", "fix": "Persist to disk."}]
    _one_shot(monkeypatch, item, _verdict(item, approve=True, reasons=[], findings=findings))
    verdict = reviewer.assess(run, _GREEN, 1, subject=item)
    assert not verdict.lgtm and verdict.state == "changes_requested"
    assert any("P0: Scores are lost on restart" in r for r in verdict.reasons)


def test_the_tools_stay_inside_the_tree_and_page_long_files(tmp_path):
    tree = tmp_path / "t"
    (tree / "src").mkdir(parents=True)
    (tree / "node_modules" / "x").mkdir(parents=True)
    (tree / "node_modules" / "x" / "i.js").write_text("installed")
    (tree / "src" / "long.js").write_text("".join(f"line {n}\n" for n in range(1, 3001)))
    tools = review_tools.ReviewTree(str(tree))
    first = tools.read_file("src/long.js")
    assert "line 1500" in first and "line 1501" not in first and "start_line=1501" in first
    assert "line 3000" in tools.read_file("src/long.js", start_line=1501)
    assert tools.run("read_file", {"path": "../outside"}).startswith("ERROR")
    assert tools.run("read_file", {"path": "node_modules/x/i.js"}).startswith("ERROR")
    assert "src/long.js:42: line 42" in tools.search_code(r"^line 42$")
    listing = tools.list_files()
    assert "src/long.js" in listing and "node_modules" not in listing
