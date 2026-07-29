"""One command that answers "what is wrong with this box", safe to paste anywhere.

At an event, a facilitator helping one attendee has to ask for the same six things
every time: is the gateway wired, which role Runtimes exist, what did the last run
do, what did the gate say, what is in the engine log. Each answer is a different
command in a different directory, and the attendee is usually already stuck and
short on patience. So this collects them once.

Idea from awslabs/aidlc-workflows v2, whose `--doctor --export` writes a shareable
report rather than only printing a verdict.

**Redaction is allowlist-based, and that is the whole safety argument.** A bundle
exists to be pasted into a chat with a facilitator, so "we removed the things we
thought were secret" is the wrong model -- one unanticipated field and a credential
is in a group chat forever. Instead, every value in the output is either a literal
this module names, or it passed through `_safe`, which reports only the SHAPE of what
it found (present/absent, a length, a suffix). Account ids inside ARNs are masked for
the same reason. The bundle never reads a `.pem`, a token, an API key, or any
Secrets Manager value. Its GitHub doctor check may idempotently prepare the empty
`workshop/doctor` branch; it writes no file and opens no pull request.

It is a diagnostic, not a status API: nothing in the engine reads it back, and it is
never on the verdict path.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 12-digit AWS account id inside an ARN. Not a secret in the credential sense, but a
# bundle gets pasted into shared channels, and an account id is the one identifier an
# attendee cannot rotate afterwards.
_ACCOUNT_RE = re.compile(r"(\d{4})\d{4}(\d{4})")

# How much of the run's own event log to carry. The tail is where the failure is; the
# head is queue/admission noise.
_LOG_TAIL = 40


def _mask_arn(arn: str) -> str:
    """An ARN with its account id partly masked, still readable as an ARN."""
    return _ACCOUNT_RE.sub(r"\1****\2", arn or "")


def _safe(name: str, value: str | None) -> dict[str, Any]:
    """Report a value's SHAPE, never the value.

    Used for anything whose content this module has not deliberately decided is
    publishable. A facilitator debugging wiring needs to know a thing is set and
    roughly what it looks like; they never need its bytes.
    """
    if not value:
        return {"name": name, "set": False}
    text = str(value)
    return {"name": name, "set": True, "length": len(text),
            "ends_with": text[-6:] if len(text) > 12 else "(short)"}


def _github_section() -> dict[str, Any]:
    """Wiring + the read-only doctor verdict. Publishable fields only.

    A gateway URL and an ``owner/repo`` are not credentials (github.py says so at
    its top, and both appear in the workshop content), so they are shown: a wrong
    owner is the single most common Lab 2 fault and hiding it would defeat the point.
    """
    try:
        import github  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"error": f"github module unavailable: {exc}"}
    out: dict[str, Any] = {}
    try:
        cfg = github._gateway_config()
        out["wired"] = cfg is not None
        if cfg:
            out["gateway_url"] = cfg["gateway_url"]
            out["repo"] = cfg["repo"]
            out["target"] = cfg["target"]
            out["region"] = cfg["region"]
            out["default_branch"] = github.repository_default_branch(cfg)
            out["config_source"] = cfg["source"]
    except Exception as exc:  # noqa: BLE001
        out["config_error"] = str(exc)
    # Doctor idempotently prepares workshop/doctor to prove write permission.
    try:
        out["doctor"] = github.doctor()
    except Exception as exc:  # noqa: BLE001
        out["doctor_error"] = str(exc)
    return out


def _runtime_section() -> dict[str, Any]:
    """Which roles are served and which have a Runtime wired.

    Generated from the registry, never from a literal roster: WORKSHOP_ROLES decides
    what is served, so a bundle that assumed three roles would misreport a box
    running a different team.
    """
    out: dict[str, Any] = {}
    try:
        import roles as roles_mod  # noqa: PLC0415
        import runtime_config  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"error": f"registry unavailable: {exc}"}
    try:
        out["served_roles"] = list(roles_mod.roster_ids())
        out["builders"] = list(roles_mod.builder_ids())
        out["checkers"] = list(roles_mod.checker_ids())
        wired = runtime_config.fleet_map()
        out["wired"] = {role: [_mask_arn(a) for a in arns]
                        for role, arns in wired.items()}
        out["not_wired"] = [r for r in out["served_roles"] if r not in wired]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _env_section() -> dict[str, Any]:
    """The env that decides behaviour, split by whether the VALUE is publishable.

    The two lists are the redaction policy made visible: names on the left are
    settings (a region, a role list, a model id, a bucket name), and everything else
    is reported by shape only.
    """
    shown = ("WORKSHOP_ROLES", "WORKSHOP_MODEL",
             "WORKSHOP_REVIEW_MODEL", "WORKSHOP_BEDROCK_REGION", "AWS_REGION",
             "WORKSHOP_RUNS_DIR", "WORKSHOP_S3FILES_DIR", "WORKSHOP_REPO_ROOT",
             "WORKSHOP_RUNTIME_BUCKET", "GITHUB_REPO", "GITHUB_GATEWAY_TARGET",
             "WORKSHOP_MAX_WORK_DIRS", "MAX_REVIEW_ROUNDS")
    # Set on a wired box and worth CONFIRMING, but never worth printing.
    by_shape = ("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "AWS_SECRET_ACCESS_KEY",
                "GITHUB_APP_PRIVATE_KEY", "GITHUB_TOKEN", "KIRO_API_KEY",
                "ANTHROPIC_API_KEY")
    out: dict[str, Any] = {"settings": {}, "credentials_present": []}
    for key in shown:
        val = os.environ.get(key)
        if val:
            out["settings"][key] = val
    for key in by_shape:
        out["credentials_present"].append(_safe(key, os.environ.get(key)))
    # Every AGENTCORE_RUNTIME_* target, masked: these are how a role resolves.
    out["runtime_env"] = {k: _mask_arn(v) for k, v in os.environ.items()
                          if k.startswith("AGENTCORE_RUNTIME_")}
    return out


def _last_runs_section(limit: int = 3) -> list[dict[str, Any]]:
    """The last few runs' verdicts, from the durable store.

    This is the half of the bundle that only works because run state is persisted:
    before that, a bundle collected after the session expired could say nothing at
    all about what the attendee had actually run.
    """
    try:
        import engine  # noqa: PLC0415
        import run_store  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"run store unavailable: {exc}"}]
    rows: list[dict[str, Any]] = []
    try:
        for saved in run_store.recent(engine._RUNS_DIR, limit=limit):
            gate = saved.get("gate") or {}
            rows.append({
                "run_id": saved.get("run_id"),
                "status": saved.get("status"),
                "fail_reason": saved.get("fail_reason"),
                "next_action": saved.get("next_action"),
                "gate_passed": bool(gate.get("passed")),
                "gate_summary": gate.get("summary", ""),
                "iterations": saved.get("iterations"),
                "pr_url": saved.get("pr_url"),
                "merge_state": saved.get("merge_state"),
                "composed_from": saved.get("composed_from"),
                "saved_at": saved.get("_saved_at"),
            })
    except Exception as exc:  # noqa: BLE001
        rows.append({"error": str(exc)})
    return rows


def _host_section() -> dict[str, Any]:
    """Enough about the box to explain a class of failure, and nothing identifying."""
    out: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "repo_root": _REPO,
    }
    try:
        import engine  # noqa: PLC0415
        runs = engine._RUNS_DIR
        out["runs_dir"] = runs
        out["runs_dir_exists"] = os.path.isdir(runs)
        mount = os.environ.get("WORKSHOP_S3FILES_DIR", "/mnt/s3files")
        out["s3files_mount"] = mount
        out["s3files_path_exists"] = os.path.isdir(mount)
        out["s3files_mounted"] = os.path.ismount(mount)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _section(name: str, fn, default):
    """Collect one section, converting ANY failure into a reported error.

    Each section already guards its own internals, but a section function itself
    could still raise (an import that fails in a new way, a monkeypatched seam).
    This is what makes "never raises" true rather than merely intended: the bundle is
    what someone runs when things are ALREADY broken, and a diagnostic that dies on
    the broken part is worthless exactly when it is needed.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 (a diagnostic must always return)
        if isinstance(default, dict):
            return {"error": f"could not collect {name}: {exc}"}
        return [{"error": f"could not collect {name}: {exc}"}]


def bundle(run_id: str | None = None) -> dict[str, Any]:
    """The whole diagnostic, as data. Read-only and never raises.

    Never raising is deliberate: this is what someone runs when things are already
    broken, so a section that cannot be collected reports its own error and the rest
    of the bundle still arrives.
    """
    out: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": _section("host", _host_section, {}),
        "roles": _section("roles", _runtime_section, {}),
        "github": _section("github", _github_section, {}),
        "env": _section("env", _env_section, {}),
        "recent_runs": _section("recent runs", _last_runs_section, []),
    }
    if run_id:
        out["run"] = _section("run", lambda: _one_run(run_id), {})
    return out


def _one_run(run_id: str) -> dict[str, Any]:
    """One named run in more depth: its verdict plus the tail of its own log."""
    try:
        import engine  # noqa: PLC0415
        import run_store  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    saved = run_store.load(engine._RUNS_DIR, run_id)
    if saved is None:
        return {"run_id": run_id, "found": False,
                "hint": "no persisted state for that run id; `list_runs` shows what "
                        "this box has"}
    events = saved.get("events") or []
    return {"run_id": run_id, "found": True,
            "status": saved.get("status"),
            "next_action": saved.get("next_action"),
            "gate": saved.get("gate"),
            "review_state": (saved.get("review") or {}).get("state"),
            "pr_url": saved.get("pr_url"),
            "log_tail": [f"[{e.get('elapsed_s')}s {e.get('level')}] {e.get('message')}"
                         for e in events[-_LOG_TAIL:]]}


def render(data: dict[str, Any]) -> str:
    """The bundle as markdown, because it gets pasted into a chat, not parsed."""
    lines: list[str] = ["# Workshop diagnostic bundle",
                        f"generated {data.get('generated_at', '')}", ""]
    host = data.get("host", {})
    if host.get("s3files_mounted"):
        mount_state = "mounted"
    elif host.get("s3files_path_exists"):
        mount_state = "NOT MOUNTED (the path is only a directory)"
    else:
        mount_state = "NOT MOUNTED (the path does not exist)"
    lines += ["## Host", f"- platform: {host.get('platform')}",
              f"- python: {host.get('python')}",
              f"- repo: {host.get('repo_root')}",
              f"- S3 Files mount {host.get('s3files_mount')}: {mount_state}", ""]

    r = data.get("roles", {})
    lines += ["## Roles"]
    if r.get("error"):
        lines.append(f"- could not read the registry: {r['error']}")
    else:
        lines.append(f"- served: {', '.join(r.get('served_roles', [])) or '(none)'}")
        for role, arns in (r.get("wired") or {}).items():
            lines.append(f"- {role}: {len(arns)} runtime(s) wired ({arns[0]})")
        if r.get("not_wired"):
            lines.append(f"- **NOT WIRED**: {', '.join(r['not_wired'])}")
    lines.append("")

    g = data.get("github", {})
    lines += ["## GitHub"]
    if g.get("error") or g.get("config_error"):
        # "not wired" would be a lie here: we never got far enough to know. A
        # facilitator sent to the wiring step for a collection failure loses the
        # time this bundle exists to save.
        lines.append(f"- could not determine the wiring: "
                     f"{g.get('error') or g.get('config_error')}")
    elif g.get("wired"):
        lines += [f"- repo: `{g.get('repo')}`",
                  f"- gateway: `{g.get('gateway_url')}` (from {g.get('config_source')})"]
    else:
        lines.append("- not wired (no gateway URL and/or no repo)")
    doctor = g.get("doctor") or {}
    if doctor:
        lines.append(f"- doctor: {'READY' if doctor.get('ok') else 'NOT READY'}")
        for check in doctor.get("checks", []):
            mark = "PASS" if check["passed"] else "FAIL"
            lines.append(f"  - {mark} {check['check']}: {check['detail']}")
        if doctor.get("hint"):
            lines.append(f"  - next: {doctor['hint']}")
    lines.append("")

    lines += ["## Recent runs"]
    rows = data.get("recent_runs", [])
    if not rows:
        lines.append("- none recorded on this box")
    for row in rows:
        if row.get("error"):
            lines.append(f"- could not read: {row['error']}")
            continue
        lines.append(f"- `{row.get('run_id')}` {row.get('status')} "
                     f"(gate {'green' if row.get('gate_passed') else 'RED'}: "
                     f"{row.get('gate_summary') or 'n/a'})"
                     + (f" -> {row.get('pr_url')}" if row.get("pr_url") else ""))
        if row.get("next_action"):
            lines.append(f"  - next: {row['next_action']}")
    lines.append("")

    run = data.get("run")
    if run:
        lines += [f"## Run {run.get('run_id')}"]
        if not run.get("found"):
            lines.append(f"- not found: {run.get('hint', '')}")
        else:
            lines.append(f"- status: {run.get('status')}")
            # Only print a field that has something in it: an empty "next:" reads as
            # "there is nothing to do", which is a claim, not a blank.
            if run.get("next_action"):
                lines.append(f"- next: {run['next_action']}")
            if run.get("review_state"):
                lines.append(f"- review: {run['review_state']}")
            gate = run.get("gate") or {}
            if gate:
                lines.append(f"- gate: {'green' if gate.get('passed') else 'RED'}"
                             f" ({gate.get('summary') or 'no summary'})")
            if run.get("log_tail"):
                lines += ["", "<details><summary>engine log tail</summary>", "",
                          "```"] + run["log_tail"] + ["```", "", "</details>"]
        lines.append("")

    env = data.get("env", {})
    lines += ["## Environment", "", "Settings:"]
    for key, val in (env.get("settings") or {}).items():
        lines.append(f"- {key}={val}")
    present = [c["name"] for c in env.get("credentials_present", []) if c.get("set")]
    lines += ["", f"Credentials present (values never collected): "
                  f"{', '.join(present) if present else 'none'}"]
    for key, val in (env.get("runtime_env") or {}).items():
        lines.append(f"- {key}={val}")
    return "\n".join(lines) + "\n"


def _main(argv: list[str]) -> int:
    """`python3 orchestrator/diagnose.py [run_id] [--json] [--out PATH]`."""
    args = list(argv)
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    out_path = None
    if "--out" in args:
        i = args.index("--out")
        if i + 1 >= len(args):
            print("usage: diagnose.py [run_id] [--json] [--out PATH]")
            return 2
        out_path = args[i + 1]
        del args[i:i + 2]
    run_id = args[0] if args else None
    data = bundle(run_id)
    text = json.dumps(data, indent=2) if as_json else render(data)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {out_path} ({len(text)} bytes). It contains no credentials; "
              "read it before sharing.")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
