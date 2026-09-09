"""Small action checks shared by the coordinator and the Governance preview.

``chat.exec_command`` and the engine's host-side ``Run.term`` call ``screen``
before invoking a shell. These string predicates are not an OS sandbox and do
not intercept tools used inside a coding agent's native CLI. ``write_file`` and
``read_file`` are also supported by the evaluator; the current coordinator only
wires the shell-command boundary. No deployed Cedar policy is implied.

Hard rules refuse an action. Soft rules also refuse automatic execution and
require human handling; this module does not implement an approval/resume queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# The two-tier rule set. Order matters only for display; matching is by predicate.
POLICIES: list[dict[str, str]] = [
    {"tier": "hard", "rule_id": "forbid_rm_root", "effect": "forbid",
     "summary": "destructive removes of an absolute/root path (rm -rf /, /*) are denied"},
    {"tier": "hard", "rule_id": "forbid_write_git_internals", "effect": "forbid",
     "summary": "direct .git/ paths in screened commands and writes are denied"},
    {"tier": "hard", "rule_id": "forbid_write_in_readonly_workflow", "effect": "forbid",
     "summary": "a read-only workflow (e.g. review/pr) may never write a file"},
    {"tier": "soft", "rule_id": "gate_write_credentials", "effect": "gate",
     "summary": "writing a credential/secret file is held for human approval"},
    {"tier": "soft", "rule_id": "gate_force_push_main", "effect": "gate",
     "summary": "git push --force to main is held for human approval"},
]


@dataclass
class Decision:
    """A policy verdict for one action. ``allowed`` False blocks it outright;
    ``gated`` True means held for a human (also not run automatically)."""

    allowed: bool
    rule_id: str = ""
    tier: str = ""
    reason: str = ""

    @property
    def gated(self) -> bool:
        return self.tier == "soft" and not self.allowed

    def public(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "rule_id": self.rule_id,
                "tier": self.tier, "reason": self.reason}


_ALLOW = Decision(allowed=True)

# --- predicates, evaluated against the action + target -----------------------
# rm with a recursive/force flag targeting an absolute root (/, /*, ~), tolerant
# of surrounding quotes (e.g. inside an os.system("rm -rf /") call) and trailing
# args. Removing a RELATIVE path is fine; only an absolute root is denied.
_RM_ROOT = re.compile(r"\brm\b[^\n]*?-[a-z]*[rf][a-z]*\b[^\n]*?\s/(?=[\s'\"*]|$)")
_RM_ROOT_SIMPLE = re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+/(?=[\s'\"*]|$)")
_CRED_PATH = re.compile(r"(^|/)(\.env|credentials|\.aws/credentials|id_rsa|"
                        r"id_ed25519|\.pem|\.key|secrets?\.(ya?ml|json|txt))$", re.I)
_FORCE_PUSH_MAIN = re.compile(r"git\s+push\b[^\n]*--force\b[^\n]*\b(main|master)\b"
                              r"|git\s+push\b[^\n]*\b(main|master)\b[^\n]*--force", re.I)


def screen(action: str, target: str = "", *, read_only: bool = False) -> Decision:
    """Screen one agent action against the policy. Deterministic, no model.

    ``action`` is the kind of operation (``write_file``, ``run_code``,
    ``run_command``); ``target`` is the path or command text it touches.
    Returns ``Decision(allowed=False, ...)`` for a hard deny or a soft gate.
    """
    text = f"{action} {target}".strip()

    # hard: destructive root removes (in a command or run_code body)
    if _RM_ROOT_SIMPLE.search(text) or _RM_ROOT.search(text):
        return Decision(False, "forbid_rm_root", "hard",
                        "destructive remove of a root/absolute path")

    # hard: writes under .git/
    if action in ("write_file", "run_command", "run_code"):
        if re.search(r"(^|/|\s)\.git/", target) or re.search(r"(^|/|\s)\.git/", text):
            return Decision(False, "forbid_write_git_internals", "hard",
                            "write under .git/ is forbidden")

    # hard: any write in a read-only workflow
    if read_only and action in ("write_file", "run_code"):
        return Decision(False, "forbid_write_in_readonly_workflow", "hard",
                        "read-only workflow may not write")

    # soft: force-push to main (held for a human)
    if _FORCE_PUSH_MAIN.search(text):
        return Decision(False, "gate_force_push_main", "soft",
                        "force push to main requires human approval")

    # soft: writing a credential/secret file (held for a human)
    if action == "write_file" and _CRED_PATH.search(target or ""):
        return Decision(False, "gate_write_credentials", "soft",
                        "writing a credential file requires human approval")

    return _ALLOW


def get_policies() -> dict[str, Any]:
    """The governance view: the SAME rules ``screen()`` enforces.

    metrics_lib re-exports this for ``GET /api/policies`` so the Governance page
    renders the enforced rule set, not a separate hand-kept copy.
    """
    return {
        "policies": [dict(p) for p in POLICIES], "enforced": True,
        "scope": "Coordinator exec_command and engine Run.term shell commands",
        "note": ("Native coding-agent tools and the Development terminal are outside "
                 "this checker. File-action rules can be evaluated here but are not "
                 "wired to a file-write tool. Soft gates require human handling; "
                 "there is no automatic approval or resume."),
    }
