"""Review orchestrator: a SEPARATE pen that judges what the build produced.

The build engine never approves its own work. A distinct orchestrator owns the
review side of the lifecycle, which is how all three reference implementations
draw the line:

  * a dedicated read-only critique agent
    reviews the implementer's diff BEFORE the PR opens; the exact string
    ``LGTM: no changes needed`` is the pass token, and a non-LGTM critique buys
    exactly one re-implement pass. We keep the token and the bound.
  * the PR side maps a branch back to the EXACT
    task that produced it via a strict suffix guard (``branchTaskSuffix``), and the
    PR lifecycle (opened → review → merged/cancelled) drives task status: the
    webhook, not the agent, is the source of truth. ``branch_run_id`` is that
    guard, ported.
  * review is a separate, read-only workflow
    whose verdict travels WITH the PR (build/lint PASS-FAIL embedded in the body).
    Our critique report is committed next to the artifacts for the same reason.

The verdict has two layers. The pytest acceptance gate plus the mechanical
critique stay deterministic and remain the FLOOR: a red gate can never pass. On
top of that, an LLM judge reviews the artifacts and the gate result and can
request changes even when the gate is green (catching the "passes the tests but
the code is wrong" class the mechanical checks miss). The judge is FAIL-OPEN and
injectable: with no model reachable (offline tests, no credentials) it abstains
and the verdict is exactly the deterministic gate+critique, so nothing here ever
blocks on a model being available.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))

# Grading contracts are per-usecase modules that share names (adapters, contract).
# All imports of them go through load_grading() under this lock so two concurrent
# runs on different usecases never cross-import each other's contract.
_IMPORT_LOCK = threading.Lock()


def load_grading(grading_dir: str):
    """Import (grade, InProcessClient, RemoteMCPClient) from a usecase's grading dir."""
    with _IMPORT_LOCK:
        for stale in ("adapters", "contract"):
            sys.modules.pop(stale, None)
        if grading_dir in sys.path:
            sys.path.remove(grading_dir)
        sys.path.insert(0, grading_dir)
        import adapters  # noqa: PLC0415
        import contract  # noqa: PLC0415
        return contract.grade, adapters.InProcessClient, adapters.RemoteMCPClient

LGTM_TOKEN = "LGTM: no changes needed"   # the exact pass token, kept verbatim
# One bounded re-implement pass, then a human. THE SINGLE SOURCE OF TRUTH for
# the bound: the engine derives its iteration cap from this (cap = rounds + 1),
# so editing this number actually changes behavior.
MAX_REVIEW_ROUNDS = 1

_RUN_BRANCH = re.compile(r"^run/(run_[0-9]{6}_[0-9]{3})$")


def branch_run_id(branch: str | None) -> str | None:
    """Map a branch name back to the exact run that produced it, or None.

    A strict branch-suffix guard: the engine always branches as
    ``run/<run_id>`` and run ids match a strict pattern, so anything else,
    including SQL-LIKE-wildcard or lookalike branches, refuses to match rather
    than falling back to a most-recent heuristic.
    """
    if not branch:
        return None
    m = _RUN_BRANCH.match(branch)
    return m.group(1) if m else None


@dataclass
class ReviewVerdict:
    """The review orchestrator's structured output for one round."""

    state: str = "in_review"        # in_review | approved | changes_requested
    gate: dict | None = None        # the pytest acceptance gate result
    critique: list[dict] = field(default_factory=list)
    report: str = ""                # the committed critique report (markdown)
    lgtm: bool = False
    round: int = 1

    def public(self) -> dict[str, Any]:
        return {"state": self.state, "lgtm": self.lgtm, "round": self.round,
                "gate": self.gate, "critique": self.critique}


def _critique_checks(run: Any, usecase_module: str) -> list[dict]:
    """Mechanical, read-only critique of the produced artifacts. Each check is
    {check, passed, detail}.

    Role-aware: the critique only judges what the route dispatched. A backend
    patch is never failed for the chatbot it was not asked to build, and a
    review run's branch maps to the run UNDER review, not the review itself.
    """
    checks: list[dict] = []
    routed = set(getattr(run, "agents", []) or [])
    read_only = bool((getattr(run, "route", None) or {}).get("read_only"))

    # 1. The server must IMPORT the module, not embed a copy of its data.
    # Checked whenever a server artifact exists (routed backend, infra, or the
    # review target's server).
    if run._server_file and os.path.isfile(run._server_file):
        with open(run._server_file, encoding="utf-8") as f:
            server_src = f.read()
        imports_module = bool(re.search(rf"import {re.escape(usecase_module)}\b", server_src))
        checks.append({
            "check": "server_imports_module", "passed": imports_module,
            "detail": (f"server imports {usecase_module} live (no copied logic)"
                       if imports_module else
                       f"server does not import {usecase_module}; pricing/logic may be duplicated"),
        })

    # 2. The frontend must hold no business logic: thin fetch to the endpoint
    # only. Judged only when the frontend role was actually dispatched (or the
    # review target produced one).
    if "opencode" in routed or (read_only and run._chatbot_file):
        chatbot_src = ""
        if run._chatbot_file and os.path.isfile(run._chatbot_file):
            with open(run._chatbot_file, encoding="utf-8") as f:
                chatbot_src = f.read()
        thin = ("tools/call" in chatbot_src) and ("fetch(" in chatbot_src)
        checks.append({
            "check": "frontend_is_thin", "passed": thin,
            "detail": ("chatbot delegates every answer to tools/call over the wire"
                       if thin else "chatbot does not call the MCP endpoint; logic may be local"),
        })

    # 3. Branch discipline: the composed branch must map back to the run that
    # produced it: THIS run for a build, the TARGET run for a review.
    expected_id = (getattr(run, "_review_target", None) or run.run_id) if read_only else run.run_id
    mapped = branch_run_id(run.composed_branch) == expected_id if run.composed_branch else False
    checks.append({
        "check": "branch_maps_to_run", "passed": mapped or run.composed_branch is None,
        "detail": (f"branch {run.composed_branch} maps to {expected_id}" if mapped
                   else "no composed branch yet" if run.composed_branch is None
                   else f"branch {run.composed_branch} does NOT map to {expected_id}"),
    })

    # 4. Project runs: the deliverable is a runnable mini-project, so the reviewer
    # actually RUNS the agent's smoke test against the server it produced. This is
    # the loop-engineering "checker" doing real work, not just reading the file: a
    # server that imports cleanly but does not answer the wire contract fails here
    # and drives the bounded iterate loop. The smoke test itself is the agent's;
    # the reviewer only executes it and reads the exit code.
    #
    # Role-aware, exactly like the frontend check above: judged only when the
    # BACKEND role authored the project (claude-code routed), or a review run whose
    # target did. A pure frontend patch (opencode only) builds no server of its
    # own -- the engine wires it to an infra endpoint -- so it is not graded on a
    # smoke test it was never asked to produce.
    backend_built = "claude-code" in routed or (read_only and run._server_file)
    if backend_built and run._server_file and os.path.isfile(run._server_file):
        smoke = getattr(run, "_smoke_file", None)
        if smoke and os.path.isfile(smoke):
            ok, detail = _run_smoke(smoke, run)
            checks.append({"check": "project_smoke_runs", "passed": ok, "detail": detail})
        else:
            checks.append({
                "check": "project_smoke_runs", "passed": False,
                "detail": "no smoke_test.py in the deliverable; the build must ship "
                          "a runnable proof (python smoke_test.py) next to the server"})
    return checks


def _run_smoke(smoke_path: str, run: Any) -> tuple[bool, str]:
    """Execute the agent's smoke test and return (passed, detail). The server it
    boots must import the usecase module, so point COST_ANALYZER_DIR at the run's
    module dir. Fail-closed: any non-zero exit, timeout, or crash is a fail with
    the tail of stderr, never a silent pass."""
    import router  # noqa: PLC0415 (sibling; lazy so offline import graph stays light)
    try:
        module_dir = router.usecase_paths(run.usecase)["dir"]
    except Exception:
        module_dir = os.path.dirname(smoke_path)
    env = {**os.environ, "COST_ANALYZER_DIR": module_dir}
    try:
        proc = subprocess.run([sys.executable, smoke_path], capture_output=True,
                              text=True, env=env, timeout=90,
                              cwd=os.path.dirname(smoke_path))
    except subprocess.TimeoutExpired:
        return False, "smoke test timed out (server never answered)"
    except Exception as exc:  # noqa: BLE001
        return False, f"smoke test could not run: {type(exc).__name__}: {exc}"
    out = (proc.stdout or "").strip().splitlines()
    if proc.returncode == 0:
        return True, out[-1] if out else "smoke test passed"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, f"smoke test exited {proc.returncode}: {tail[-1] if tail else 'no output'}"


def _render_report(run: Any, gate: dict, critique: list[dict], lgtm: bool) -> str:
    """The critique report that travels with the deliverable (the verdict in
    the PR body; critique.md committed before the PR opens)."""
    lines = [f"# Critique Report: {run.run_id}", ""]
    lines.append(f"Round {run.iterations} · gate "
                 f"{'GREEN' if gate.get('passed') else 'RED'} · "
                 f"{len([c for c in critique if c['passed']])}/{len(critique)} critique checks green")
    lines.append("")
    lines.append("## Acceptance gate (pytest, deterministic)")
    for c in gate.get("checks", []):
        lines.append(f"- [{'x' if c['passed'] else ' '}] {c['check']}: {c['detail']}")
    lines.append("")
    lines.append("## Critique (mechanical, read-only)")
    for c in critique:
        lines.append(f"- [{'x' if c['passed'] else ' '}] {c['check']}: {c['detail']}")
    lines.append("")
    lines.append(LGTM_TOKEN if lgtm else
                 "Changes requested: address the unchecked items above. "
                 f"One bounded re-implement pass is allowed (max {MAX_REVIEW_ROUNDS}).")
    return "\n".join(lines) + "\n"


# The judge model is wirable (same surface as the orchestrator's own model id),
# defaulting to a fast mid-tier Claude; the review is a read, not a build.
JUDGE_MODEL = os.environ.get("WORKSHOP_REVIEW_MODEL", "claude-sonnet-4-6")

_JUDGE_SYSTEM = (
    "You are a meticulous senior code reviewer acting as a SEPARATE review pen for "
    "a multi-agent coding harness. You judge an artifact that ALREADY passed (or "
    "failed) a deterministic pytest gate. Your job is to catch what mechanical "
    "tests miss: logic that is wrong despite green tests, security problems, copied "
    "instead of imported logic, and obvious quality defects. You do NOT rewrite "
    "code. Reply with STRICT JSON only: "
    '{"approve": true|false, "reasons": ["..."]}. Approve only if the artifact is '
    "genuinely correct and shippable. Be decisive; default to approve when the gate "
    "is green and you see no real defect."
)


def _default_judge(run: Any, gate: dict, usecase_module: str) -> dict | None:
    """The LLM judge: one model call over the artifacts + gate result.

    FAIL-OPEN: returns ``None`` (abstain) whenever a model cannot be reached (no
    credentials, no access, or any transport error), so offline/unit runs behave
    exactly like the deterministic gate+critique. Returns
    ``{"approve": bool, "reasons": [...]}`` when the judge actually ran.
    """
    try:
        import llm  # noqa: PLC0415 (lazy; offline tests never import boto3)
    except Exception:
        return None
    if not llm.available():
        return None

    # Hand the judge the artifacts it can read plus the gate it must respect.
    parts: list[str] = [f"Task: {getattr(run, 'task', '')!r}",
                        f"pytest gate passed: {gate.get('passed')}",
                        f"gate checks: {json.dumps(gate.get('checks', []))[:2000]}"]
    for label, attr in (("server (mcp_server.py)", "_server_file"),
                        ("frontend (chatbot.html)", "_chatbot_file")):
        path = getattr(run, attr, None)
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                parts.append(f"--- {label} ---\n{f.read()[:6000]}")
    prompt = ("Review this artifact and decide whether it is correct and "
              "shippable.\n\n" + "\n\n".join(parts))
    try:
        out = llm.invoke(JUDGE_MODEL, prompt, system=_JUDGE_SYSTEM, max_tokens=1000)
    except Exception:
        return None  # fail-open: a judge outage never blocks the deterministic gate
    text = (out.get("text") or "").strip()
    try:
        start, end = text.find("{"), text.rfind("}")
        parsed = json.loads(text[start:end + 1]) if start != -1 and end != -1 else {}
    except Exception:
        return None
    return {"approve": bool(parsed.get("approve")),
            "reasons": [str(r) for r in (parsed.get("reasons") or [])][:5]}


def review(run: Any, grading_dir: str, usecase_module: str,
           round_no: int, judge: Any = _default_judge) -> ReviewVerdict:
    """One review round: run the gate + the critique + the LLM judge, render the
    verdict.

    Pure function of the run's artifacts and the grading contract for the
    deterministic layer; the build engine calls this but cannot influence it
    (separate module, separate pen). The ``judge`` is injectable (tests pass a
    fake or ``lambda *a: None`` to disable it); it defaults to the real LLM judge,
    which is FAIL-OPEN so a missing model never changes the deterministic verdict.
    """
    verdict = ReviewVerdict(round=round_no)

    # The deterministic pytest acceptance gate, over the wire.
    endpoint = run.artifact_endpoint or ""
    env = {**os.environ, "MCP_ENDPOINT_URL": endpoint}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", grading_dir, "-q", "--no-header"],
        capture_output=True, text=True, env=env, timeout=90,
    )
    grade, _, RemoteMCPClient = load_grading(grading_dir)
    try:
        graded = grade(RemoteMCPClient(endpoint))
    except Exception as exc:
        graded = {"passed": False,
                  "checks": [{"check": "endpoint_reachable", "passed": False,
                              "detail": f"{type(exc).__name__}: {exc}"}]}
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    checks = list(graded["checks"])
    if proc.returncode != 0 and graded["passed"]:
        # The two gate halves diverged (pytest failed at collection/import while
        # the in-process grade is green). Surface WHY the gate is red so the
        # displayed checks can never contradict the verdict.
        checks.append({"check": "pytest_run", "passed": False,
                       "detail": f"pytest exited {proc.returncode}: "
                                 f"{tail[-1] if tail else 'no output'}"})
    verdict.gate = {"passed": proc.returncode == 0 and graded["passed"],
                    "checks": checks,
                    "pytest": tail[-1] if tail else ""}

    verdict.critique = _critique_checks(run, usecase_module)

    # The LLM judge layers ON TOP of the deterministic floor. It runs only when
    # the gate+critique are already clean (a red gate is never overridden green),
    # and it is FAIL-OPEN: abstain / unavailable adds no check and changes nothing.
    # When it actively disapproves, it appends a failing critique check, so it can
    # withhold LGTM on green tests but never fabricate a pass.
    deterministic_ok = verdict.gate["passed"] and all(c["passed"] for c in verdict.critique)
    if deterministic_ok and judge is not None:
        try:
            jv = judge(run, verdict.gate, usecase_module)
        except Exception:
            jv = None  # fail-open at the call site too
        if jv is not None and not jv.get("approve", True):
            reasons = "; ".join(jv.get("reasons") or []) or "the reviewer found defects"
            verdict.critique.append({
                "check": "llm_review", "passed": False,
                "detail": f"LLM reviewer requested changes: {reasons}"})
        elif jv is not None and jv.get("approve"):
            verdict.critique.append({
                "check": "llm_review", "passed": True,
                "detail": "LLM reviewer approved the artifact"})

    verdict.lgtm = verdict.gate["passed"] and all(c["passed"] for c in verdict.critique)
    verdict.state = "approved" if verdict.lgtm else "changes_requested"
    verdict.report = _render_report(run, verdict.gate, verdict.critique, verdict.lgtm)

    # Persist the report next to the run's artifacts so compose can commit it.
    os.makedirs(run.workdir, exist_ok=True)
    report_path = os.path.join(run.workdir, "critique.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(verdict.report)
    return verdict
