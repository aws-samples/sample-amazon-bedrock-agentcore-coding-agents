"""What the integrated review reads, and what it posts.

October 19-20 (test event 5b5c823d): a GPT-6.1 frontend committed its Vite bundle and
a ``.ttf`` font. The font was read as text, the bundle filled the context, and the
review abstained (``REVIEW_UNAVAILABLE``) on green pull requests. Binary files and
generated build output now stay out of the review context and are LISTED there with
their sizes; every source file still arrives complete.

The same runs showed Sonnet 5.5 and Opus 5.5 under tool choice ``auto`` leaving
tags such as ``</design_assessment>`` or ``</invoke>`` in a lens. Those are removed
from the posted assessment, and quoted code keeps them.

    python3 -m pytest orchestrator/test_review_input.py -v
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reviewer  # noqa: E402
from work_items import WorkItem  # noqa: E402

# Tool-call markup in a model's own namespace, built so this file never carries the
# literal tag.
_NS = "antml" + ":"
_NS_OPEN = "<" + _NS + 'invoke name="submit_review">'
_NS_CLOSE = "</" + _NS + "parameter>"

_GREEN_GATE = {"passed": True, "checks": [
    {"check": "acceptance_test_authored", "passed": True, "detail": "green"}],
    "summary": "all checks green"}


class _Run:
    def __init__(self, tree, item):
        self.task = "a small game"
        self.final_base_branch = "main"
        self.work_items = {item.agent: item}
        self._item_checks = {}
        self._tree = str(tree)

    def item_tree_dir(self, _work_id):
        return self._tree


def _frontend_tree(tmp_path):
    """A frontend pull request that commits source, its bundle, and a font."""
    item = WorkItem.create("run_1", "codex", "frontend-builder", "frontend",
                           base_branch="main", token="front")
    tree = tmp_path / item.work_id
    (tree / "src").mkdir(parents=True)
    (tree / "dist" / "assets").mkdir(parents=True)
    (tree / "public" / "fonts").mkdir(parents=True)
    (tree / "server").mkdir()
    source = "export function score(hits) {\n" + "  // keep\n" * 3000 + "  return hits * 10;\n}\n"
    (tree / "src" / "game.js").write_text(source)
    (tree / "dist" / "assets" / "index-abc123.js").write_text("var a=1;" * 40_000)
    (tree / "dist" / "index.html").write_text("<script src=assets/index-abc123.js>")
    (tree / "public" / "app.min.js").write_text("!function(){}();" * 100)
    (tree / "public" / "app.js.map").write_text('{"version":3,"mappings":"AAAA"}')
    (tree / "public" / "fonts" / "pixel.ttf").write_bytes(
        b"\x00\x01\x00\x00\x00\x0fOS/2" + bytes(range(256)) * 20)
    # No telling extension: binary by content alone (invalid UTF-8, no NUL byte).
    (tree / "server" / "seed.dat").write_bytes(b"\xff\xfe\xfa" * 500)
    (tree / "server" / "notes.txt").write_text("café, naïve: a real UTF-8 note\n")
    item.changed_files = ["src/game.js", "dist/assets/index-abc123.js", "dist/index.html",
                          "public/app.min.js", "public/app.js.map",
                          "public/fonts/pixel.ttf"]
    return _Run(tree, item), item, tree, source


def _review(monkeypatch, run, item, verdict=None):
    import llm

    prompts = []

    def invoke(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return {"model_id": "m", "text": json.dumps(verdict or {
            "approve": True, "reasons": [],
            "work_item_evidence": {item.work_id: "The page calls score() on each hit."},
            "adversarial_assessment": "No defect found.",
            "design_assessment": "No defect found.",
        })}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "invoke", invoke)
    return reviewer._default_judge(run, _GREEN_GATE, item), prompts


@pytest.mark.parametrize("rel,reason", [
    ("public/fonts/pixel.ttf", "binary file"),
    ("dist/assets/index-abc123.js", "generated build output"),
    ("web/.vite/deps/react.js", "generated build output"),
    ("build/main.js", "generated build output"),
    ("public/app.min.js", "generated build output"),
    ("public/style.min.css", "generated build output"),
    ("public/app.js.map", "generated build output"),
    ("assets/sprite.PNG", "binary file"),
    ("src/game.js", ""),
    ("src/distance.js", ""),
])
def test_omission_by_path(tmp_path, rel, reason):
    full = tmp_path / "f"
    full.write_text("plain text\n")
    assert reviewer._review_omission(rel, str(full)) == reason


def test_omission_by_content(tmp_path):
    nul = tmp_path / "blob"
    nul.write_bytes(b"GIF89a\x00\x00\x01")
    latin = tmp_path / "seed.dat"
    latin.write_bytes(b"\xff\xfe\xfa" * 10)
    text = tmp_path / "notes"
    text.write_text("café\n")
    # A multi-byte character cut at the sniff boundary is still text.
    cut = tmp_path / "long.txt"
    cut.write_bytes(b"a" * (reviewer._SNIFF_BYTES - 1) + "é".encode() + b"tail")
    assert reviewer._review_omission("blob", str(nul)).startswith("binary")
    assert reviewer._review_omission("seed.dat", str(latin)) == "binary file (not UTF-8 text)"
    assert reviewer._review_omission("notes", str(text)) == ""
    assert reviewer._review_omission("long.txt", str(cut)) == ""


def test_binaries_and_bundles_are_listed_not_read_and_source_stays_complete(
        monkeypatch, tmp_path):
    run, item, tree, source = _frontend_tree(tmp_path)
    review, prompts = _review(monkeypatch, run, item)
    assert review["approve"] is True and not review["review_unavailable"]
    prompt = prompts[0]

    # Source text arrives complete, including a non-ASCII UTF-8 file.
    assert source in prompt
    assert "café, naïve: a real UTF-8 note" in prompt
    # No bundle, map, minified file or font content reaches the model.
    assert "var a=1;var a=1;" not in prompt
    assert "!function(){}();" not in prompt
    assert '"mappings"' not in prompt
    assert "OS/2" not in prompt and "�" not in prompt

    # Each omitted file is named with its size and why, so the reviewer knows.
    listing = prompt[prompt.index("Left out of this review"):]
    for rel in ("dist/assets/index-abc123.js", "dist/index.html", "public/app.min.js",
                "public/app.js.map", "public/fonts/pixel.ttf", "server/seed.dat"):
        size = os.path.getsize(tree / rel)
        assert any(rel in line and f"{size:,} bytes" in line
                   for line in listing.splitlines()), (rel, listing)
    assert f"{item.work_id} (frontend) changed dist/assets/index-abc123.js: " in listing
    assert "on the base branch: server/seed.dat: 1,500 bytes, binary" in listing


def test_omitted_files_do_not_count_toward_the_file_limit(monkeypatch, tmp_path):
    run, item, tree, _source = _frontend_tree(tmp_path)
    for n in range(80):
        (tree / "dist" / "assets" / f"chunk-{n}.js").write_text("x")
    monkeypatch.setattr(reviewer, "_MAX_JUDGE_FILES", 3)
    omitted = []
    files = reviewer._artifact_files(run, item, omitted=omitted)
    assert all(path for _label, path in files)          # no limit NOTE
    assert len(omitted) == 86
    section = reviewer._omitted_section(omitted)
    listed = [line for line in section.splitlines()[1:]
              if line.startswith("- ") and not line.startswith("- and ")]
    assert len(listed) == reviewer._MAX_OMITTED_LISTED
    assert "and 26 more files" in section


def test_a_source_tree_over_the_limit_is_still_review_unavailable(monkeypatch, tmp_path):
    run, item, _tree, _source = _frontend_tree(tmp_path)
    monkeypatch.setattr(reviewer, "_MAX_JUDGE_CONTENT_CHARS", 1000)
    import llm
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "invoke",
        lambda *a, **k: pytest.fail("an incomplete source tree must not be judged"))
    verdict = reviewer.assess(run, _GREEN_GATE, 1, subject=item)
    assert verdict.lgtm is False and verdict.review_unavailable is True
    assert "review context limit" in " ".join(verdict.reasons)


def test_an_authored_check_that_is_not_utf8_is_still_shown(monkeypatch, tmp_path):
    run, item, _tree, _source = _frontend_tree(tmp_path)
    check = tmp_path / "acceptance_check"
    check.write_bytes(b"#!/bin/sh\n# caf\xe9\nexit 0\n")
    run._item_checks = {item.work_id: str(check)}
    _review_out, prompts = _review(monkeypatch, run, item)
    assert "--- the validator's authored acceptance check ---\n#!/bin/sh" in prompts[0]


# ------------------------------------------------------------ stray tool markup
def test_stray_markup_leaves_the_posted_assessment_but_quoted_code_keeps_it(
        monkeypatch, tmp_path):
    run, item, _tree, _source = _frontend_tree(tmp_path)
    review, _prompts = _review(monkeypatch, run, item, verdict={
        "approve": False,
        "reasons": ["The score resets on restart.</parameter>\n</invoke>"],
        "work_item_evidence": {item.work_id: "score() is wired.</work_item_evidence>"},
        "adversarial_assessment": (
            "- The restart path loses the score.</invoke>\n"
            "- The check quotes `</invoke>` in its parser.\n\n"
            "```html\n<parameter name=\"x\">kept</parameter>\n</invoke>\n```"),
        "design_assessment": ("Ownership is clean.\n</design_assessment>\n"
                              + _NS_CLOSE + "\n</submit_review>"),
    })
    body = review["assessment"]
    assert "</invoke>" not in body.replace("`</invoke>`", "").split("```html")[0]
    for tag in ("</design_assessment>", "</work_item_evidence>", "</submit_review>",
                _NS_CLOSE, "</parameter>\n</invoke>"):
        assert tag not in body.split("```html")[0], tag
    # Quoted code is left exactly as written: an inline span and a fenced block.
    assert "`</invoke>`" in body
    assert "```html\n<parameter name=\"x\">kept</parameter>\n</invoke>\n```" in body
    assert "- The restart path loses the score.\n" in body
    assert "Ownership is clean." in body
    assert review["reasons"] == ["integrated: The score resets on restart."]


def test_strip_stray_markup_unit():
    strip = reviewer._strip_stray_markup
    assert strip("All good.\n</design_assessment>") == "All good."
    assert strip("Fix it.</invoke>") == "Fix it."
    assert strip('<parameter name="reasons">x</parameter>') == "x"
    assert strip("Use <div> and a <Parameter-free> list.") == "Use <div> and a <Parameter-free> list."
    assert strip("a < b and c > d") == "a < b and c > d"
    assert strip("```\n</design_assessment>\n```") == "```\n</design_assessment>\n```"
    assert strip("see `<invoke>` here</invoke>") == "see `<invoke>` here"
    assert strip("unterminated ```\n</invoke>") == "unterminated ```\n</invoke>"
    assert strip(_NS_OPEN + "x") == "x"
