"""Declared bundles cannot crowd out authored source or create a review pass."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import reviewer
from review_build_outputs import declared_outputs
from work_items import WorkItem


_COMMAND = (
    "esbuild src/main.jsx --bundle --minify "
    "--outfile=public/assets/game.js --loader:.css=css"
)
_GREEN_GATE = {
    "passed": True, "checks": [{"check": "authored acceptance", "passed": True}],
    "summary": "passed",
}


def _tree(tmp_path, command=_COMMAND):
    tree = tmp_path / "project"
    (tree / "src").mkdir(parents=True)
    (tree / "public/assets").mkdir(parents=True)
    (tree / "src/main.jsx").write_text(
        'import { score } from "./game.js";\nexport const view = () => score(2);\n'
    )
    source = "export const score = hits => hits * 7;\n"
    (tree / "src/game.js").write_text(source)
    bundle = tree / "public/assets/game.js"
    bundle.write_text("(()=>{const n=1;})();" * 12_000)
    (tree / "package.json").write_text(json.dumps({
        "scripts": {"build": command}, "devDependencies": {"esbuild": "^0.25.0"},
    }))
    item = WorkItem.create(
        "run_bundle", "codex", "frontend-builder", "frontend",
        base_branch="main", token="bundle",
    )
    item.changed_files = [
        "package.json", "public/assets/game.js", "src/game.js", "src/main.jsx",
    ]
    check = tmp_path / "checker"
    check.write_text("#!/bin/sh\nprintf 'independent executable gate\\n'\n")
    run = SimpleNamespace(
        task="an arbitrary requested change", final_base_branch="main",
        work_items={item.agent: item}, _item_checks={item.work_id: str(check)},
        item_tree_dir=lambda _work_id: str(tree),
    )
    return tree, run, item


def _model_capture(monkeypatch, item, *, allow):
    import llm

    prompts = []
    monkeypatch.setattr(llm, "available", lambda: True)

    def invoke(_model, prompt, **_kwargs):
        if not allow:
            pytest.fail("An incomplete or ambiguous source tree must not reach the model.")
        prompts.append(prompt)
        return {"text": json.dumps({
            "approve": False, "reasons": ["Independent fixture review requests changes."],
            "work_item_evidence": {item.work_id: "The complete source remains available."},
            "adversarial_assessment": "Independent fixture finding.",
            "design_assessment": "Independent fixture assessment.",
        })}

    monkeypatch.setattr(llm, "invoke", invoke)
    return prompts


def test_declared_esbuild_bundle_outside_generated_directories_is_listed_with_basis(
        monkeypatch, tmp_path):
    tree, run, item = _tree(tmp_path)
    prompts = _model_capture(monkeypatch, item, allow=True)
    verdict = reviewer.assess(run, _GREEN_GATE, 1, subject=item)
    assert not verdict.review_unavailable
    assert not verdict.lgtm and verdict.state == "changes_requested"
    assert len(prompts) == 1
    prompt = prompts[0]
    for name in ["package.json", "src/game.js", "src/main.jsx"]:
        assert (tree / name).read_text() in prompt
    bundle = tree / "public/assets/game.js"
    assert bundle.read_text() not in prompt
    line = next(line for line in prompt.splitlines()
                if line.startswith("- ") and "changed public/assets/game.js" in line)
    assert f"{bundle.stat().st_size:,} bytes" in line
    assert 'package.json scripts["build"]' in line
    assert "esbuild --bundle --minify from src/main.jsx" in line
    assert "committed output was not reproduced" in line
    assert "independent executable gate" in prompt


@pytest.mark.parametrize("command", [
    "node build.js",
    "echo " + _COMMAND,
    _COMMAND + " && node cleanup.js",
    _COMMAND.replace("public/assets/game.js", "$OUTPUT"),
    _COMMAND.replace("src/main.jsx", "src/*.jsx"),
    _COMMAND.replace("--outfile=public/assets/game.js", "--outdir=public/assets"),
    _COMMAND.replace("--outfile=public/assets/game.js", "--outfile public/assets/game.js"),
    _COMMAND.replace(" --minify", ""),
    _COMMAND.replace(" --bundle", ""),
    _COMMAND + " --unknown-option",
    _COMMAND + " --outfile=public/assets/other.js",
    _COMMAND + " src/game.js",
    _COMMAND.replace("src/main.jsx", "missing.jsx"),
    _COMMAND.replace("src/main.jsx", "public/assets/game.js"),
])
def test_ambiguous_or_unsupported_commands_leave_large_file_in_review(
        monkeypatch, tmp_path, command):
    tree, run, item = _tree(tmp_path, command)
    assert declared_outputs(str(tree)) == {}
    _model_capture(monkeypatch, item, allow=False)
    verdict = reviewer.assess(run, _GREEN_GATE, 1, subject=item)
    assert verdict.review_unavailable and not verdict.lgtm
    assert "public/assets/game.js" in " ".join(verdict.reasons)


def test_a_genuine_large_authored_file_is_never_exempted_by_its_path_or_shape(
        monkeypatch, tmp_path):
    tree, run, item = _tree(tmp_path)
    authored = tree / "public/assets/authored.js"
    authored.write_text("export const authoredValues = [" + "123," * 50_000 + "0];\n")
    item.changed_files.insert(0, "public/assets/authored.js")
    _model_capture(monkeypatch, item, allow=False)
    verdict = reviewer.assess(run, _GREEN_GATE, 1, subject=item)
    assert verdict.review_unavailable and not verdict.lgtm
    assert "public/assets/authored.js" in " ".join(verdict.reasons)


@pytest.mark.parametrize("source", [
    b"// source prefix\n" * 1000 + b"\xff",
    b"// source prefix\n" * 1000 + b"\0",
])
def test_binary_tail_in_declared_entry_does_not_exempt_bundle(monkeypatch, tmp_path, source):
    tree, run, item = _tree(tmp_path)
    (tree / "src/main.jsx").write_bytes(source)
    _model_capture(monkeypatch, item, allow=False)
    verdict = reviewer.assess(run, _GREEN_GATE, 1, subject=item)
    assert verdict.review_unavailable and not verdict.lgtm
    assert "public/assets/game.js" in " ".join(verdict.reasons)


@pytest.mark.parametrize("entry", [".private/main.jsx", "node_modules/dependency.js"])
def test_an_entry_excluded_from_source_review_cannot_justify_omission(tmp_path, entry):
    tree, _run, _item = _tree(tmp_path, _COMMAND.replace("src/main.jsx", entry))
    path = tree / entry
    path.parent.mkdir(parents=True)
    path.write_text("export const privateCode = 1;\n")
    assert declared_outputs(str(tree)) == {}


def test_output_with_conflicting_script_declarations_remains_source(tmp_path):
    tree, _run, _item = _tree(tmp_path)
    (tree / "package.json").write_text(json.dumps({"scripts": {
        "build": _COMMAND,
        "build:other": _COMMAND.replace("src/main.jsx", "src/game.js"),
    }}))
    assert declared_outputs(str(tree)) == {}


def test_declared_source_is_not_hidden_as_another_scripts_output(tmp_path):
    tree, _run, _item = _tree(tmp_path)
    (tree / "package.json").write_text(json.dumps({"scripts": {
        "build": _COMMAND.replace("src/main.jsx", "src/game.js"),
        "prepare": "esbuild src/main.jsx --bundle --minify --outfile=src/game.js",
    }}))
    output = declared_outputs(str(tree))
    assert "src/game.js" not in output


@pytest.mark.parametrize("body", [
    '{"scripts":{"build":"incomplete"',
    '{"scripts":{"build":null}}',
    '{"scripts":{"build":"esbuild src/main.jsx --bundle --minify --outfile=public/assets/game.js",'
    '"build":"node custom.js"}}',
])
def test_unreadable_or_duplicate_manifest_does_not_hide_files(tmp_path, body):
    tree, _run, _item = _tree(tmp_path)
    (tree / "package.json").write_text(body)
    assert declared_outputs(str(tree)) == {}


@pytest.mark.parametrize("which", ["manifest", "entry", "output"])
def test_symlink_does_not_establish_generated_provenance(tmp_path, which):
    tree, _run, _item = _tree(tmp_path)
    path = tree / {
        "manifest": "package.json", "entry": "src/main.jsx", "output": "public/assets/game.js",
    }[which]
    external = tmp_path / "external"
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)
    assert declared_outputs(str(tree)) == {}


def test_nested_package_resolves_literal_quoted_paths_from_its_own_directory(tmp_path):
    project = tmp_path / "project"
    nested = project / "frontend"
    (nested / "source folder").mkdir(parents=True)
    (nested / "public/assets").mkdir(parents=True)
    (nested / "source folder/main.jsx").write_text("export const view = 1;\n")
    (nested / "public/assets/client app.js").write_text("(()=>{})();")
    (nested / "package.json").write_text(json.dumps({"scripts": {"build": (
        'esbuild "source folder/main.jsx" --bundle --minify '
        '--outfile="public/assets/client app.js"'
    )}}))
    output = declared_outputs(str(project))
    assert list(output) == ["frontend/public/assets/client app.js"]
    claim = output["frontend/public/assets/client app.js"]
    assert claim.source == "frontend/source folder/main.jsx"
    assert "frontend/package.json" in claim.reason


def test_a_bundle_on_the_base_branch_uses_the_same_rule(tmp_path):
    tree, run, item = _tree(tmp_path)
    item.changed_files = ["src/game.js"]
    omitted = []
    files = reviewer._artifact_files(run, item, omitted=omitted)
    assert all(path != str(tree / "public/assets/game.js") for _label, path in files)
    assert any(label == "on the base branch: public/assets/game.js"
               and "declared generated build output" in reason
               for label, _size, reason in omitted)
