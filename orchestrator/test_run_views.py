"""run_views is the reporting projection of a run, split out of engine.py.

It must stay on the reporting side: importing it must never load the engine, the
model client, or the reviewer, so the watcher, the progress check, and the run
advice can describe a run without the verdict path. The engine re-exports every
name, so ``engine.public_result`` and ``engine.next_action`` stay the same objects.
"""
import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402
import run_views  # noqa: E402


def test_run_views_imports_no_engine_model_or_reviewer():
    tree = ast.parse(open(os.path.join(HERE, "run_views.py"), encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"engine", "llm", "reviewer", "boto3", "strands"}, imported
    probe = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import run_views; "
         "print(sorted(m for m in ('engine', 'llm', 'reviewer') if m in sys.modules))", HERE],
        capture_output=True, text=True, timeout=60)
    assert probe.stdout.strip() == "[]", probe.stdout + probe.stderr


def test_engine_re_exports_the_same_objects():
    for name in ("public_result", "public_run", "public_progress", "public_terminals",
                 "public_events", "public_submitter", "next_action", "resubmission_allowed",
                 "_NEXT_ACTION", "_RESUBMITTABLE_REASONS", "_published_rows", "_redact"):
        assert getattr(engine, name) is getattr(run_views, name), name
